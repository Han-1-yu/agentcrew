"""R11：知识检索的升级——中文二元组 + BM25。

原来的检索是「按单字切分、数命中个数」。两个问题在真实数据上都会咬人：

1. **单字太粗**：「写代码」和「写文档」共享一个「写」就互相命中；「后端」与
   「后端」被拆成两个字分别匹配，顺序信息全丢。
2. **计数不分轻重**：「的」「是」「了」与「幂等」「事务」权重完全相同，短文本天然
   吃亏、长文本天然占优，一个「的」字就能把无关经验顶上来。

这一版换两件事，都不引入外部依赖（不引入 jieba，也不引入向量库）：

- **分词**：英文数字按词，CJK 取单字 **并补相邻二元组**。「写代码」因此得到
  ``写 / 代 / 码 / 写代 / 代码``——只共享一个「写」时相似度自然很低。
- **打分**：BM25（TF 饱和 + 文档长度归一 + IDF 加权）。高频虚词的 df 高、IDF 低，
  自动被压下去，不需要维护停用词表。

**为什么不是向量检索**：全仓没有 embedding 接口，接一个就要引 provider 方法、
网络调用与按量计费，而项目的红线是「离线可跑、样例不依赖网络」。BM25 在纯本地
做到「词组级匹配 + 权重归一」，是这条红线下能做到的最实的一步。真正的向量检索
需要独立的 embedding 通道，等那条通道存在了再谈。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

#: 英文数字按词切；CJK 逐字切（二元组在 :func:`tokenize` 里另行补齐）。
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")
#: 连续的 CJK 片段：二元组只在段内生成，不跨标点或空格拼接。
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """把文本切成检索用的 token 序列（英文词 + CJK 单字 + CJK 相邻二元组）。

    重复保留：BM25 需要词频（TF），去重是查询侧的事。
    """
    lowered = (text or "").lower()
    tokens = _TOKEN_RE.findall(lowered)
    for run in _CJK_RUN_RE.findall(lowered):
        tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


class BM25Index:
    """一份内存 BM25 索引：``search`` 返回 ``(文档下标, 分数)`` 降序。

    经验库规模在几百到几千条、每次检索重建索引的成本远低于一次模型调用，所以
    不做持久化索引——少一个需要失效管理的缓存，就少一类「索引过期」的 bug。
    """

    def __init__(
        self,
        documents: Sequence[str],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        """``k1`` 控 TF 饱和、``b`` 控长度归一；默认值是 BM25 的通行取值。"""
        self._k1 = k1
        self._b = b
        self._tfs: list[Counter[str]] = []
        self._lengths: list[int] = []
        document_freq: Counter[str] = Counter()
        for document in documents:
            tf = Counter(tokenize(document))
            self._tfs.append(tf)
            self._lengths.append(sum(tf.values()))
            document_freq.update(tf.keys())
        self._count = len(self._tfs)
        total = sum(self._lengths)
        self._avg_length = total / self._count if self._count else 0.0
        # 加 0.5 的平滑让从未出现的词也有正 IDF；1 + 的偏移保证恒正，
        # 避免「只有一篇文档含有该词」时 IDF 变负把总分拉成负数。
        self._idf: dict[str, float] = {
            token: math.log(
                1.0 + (self._count - freq + 0.5) / (freq + 0.5))
            for token, freq in document_freq.items()
        }

    def search(self, query: str, limit: int) -> list[tuple[int, float]]:
        """按 BM25 打分取前 ``limit`` 条；零分或只有单字命中的文档不返回。

        查询 token 去重——同一个词在问题里出现三次不代表它更重要，BM25 的 TF
        该由**文档**侧决定。

        **「单字命中不算相关」**：中文里「理」这种字散落在各处，只共享一个单字
        就返回，等于把无关经验端进提示词（实测「量子引力弦论」会因一个「理」字
        命中「整理需求文档」）。所以要求至少一个命中 token 长度 > 1 —— 即
        CJK 二元组或英文词。纯单字查询（如只输入一个汉字）会得到空结果，这是
        刻意的退化：那种查询本身不足以说清要找什么。
        """
        query_tokens = list(dict.fromkeys(tokenize(query)))
        if not query_tokens or self._count == 0 or limit <= 0:
            return []
        hits: list[tuple[int, float]] = []
        for index, tf in enumerate(self._tfs):
            length_ratio = (
                self._lengths[index] / self._avg_length
                if self._avg_length else 0.0
            )
            norm = self._k1 * (1.0 - self._b + self._b * length_ratio)
            score = 0.0
            strong_hit = False
            for token in query_tokens:
                frequency = tf.get(token, 0)
                if not frequency:
                    continue
                if len(token) > 1:
                    strong_hit = True
                score += (
                    self._idf.get(token, 0.0)
                    * frequency * (self._k1 + 1.0)
                    / (frequency + norm)
                )
            if score > 0 and strong_hit:
                hits.append((index, score))
        hits.sort(key=lambda item: (-item[1], item[0]))
        return hits[:limit]
