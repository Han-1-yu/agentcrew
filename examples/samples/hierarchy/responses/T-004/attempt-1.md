按设计实现索引层：dict[str, set[str]] 倒排表，add/remove/query 三个方法，多词求交后按命中词数与文档 id 排序。

```python file=kb/index.py
"""索引层：倒排索引与排序。

数据结构用 dict[str, set[str]]（词 → 文档 id 集合）：
求交是核心操作，集合交集 O(min(m,n)) 且天然去重；代价是丢掉词频，
这与 PRD §4「不引入 TF-IDF、排序必须可复现」的取舍一致。
"""

from __future__ import annotations

import re

#: 中文按双字切分，英文按小写整词。两侧规则必须是同一个函数，
#: 否则会出现「索引里有、搜不到」的静默错误（docs/design.md §3）。
_SPLIT_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]{2}")


def tokenize(text: str) -> list[str]:
    """把一段文本切成检索词。"""
    return _SPLIT_RE.findall(text.lower())


class InvertedIndex:
    """纯内存倒排索引。"""

    def __init__(self) -> None:
        self._postings: dict[str, set[str]] = {}
        self._docs: dict[str, str] = {}

    def add(self, doc_id: str, text: str) -> None:
        """索引一篇文档；重复添加时先清掉旧词条，避免幽灵命中。"""
        if doc_id in self._docs:
            self.remove(doc_id)
        self._docs[doc_id] = text
        for term in set(tokenize(text)):
            self._postings.setdefault(term, set()).add(doc_id)

    def remove(self, doc_id: str) -> bool:
        """删除一篇文档，返回是否真的删掉了。"""
        text = self._docs.pop(doc_id, None)
        if text is None:
            return False
        for term in set(tokenize(text)):
            bucket = self._postings.get(term)
            if bucket is None:
                continue
            bucket.discard(doc_id)
            if not bucket:
                del self._postings[term]
        return True

    def document(self, doc_id: str) -> str | None:
        return self._docs.get(doc_id)

    def query(self, terms: list[str]) -> list[tuple[str, int]]:
        """多关键词求交，返回 (文档 id, 命中词数) 并按 PRD §4 排序。

        排序两级：命中词数降序；同分按文档 id 升序（保证结果可复现）。
        """
        normalized = [t for term in terms for t in tokenize(term)]
        if not normalized:
            return []

        hits: dict[str, int] = {}
        for term in set(normalized):
            for doc_id in self._postings.get(term, set()):
                hits[doc_id] = hits.get(doc_id, 0) + 1

        # 「与」语义：只有命中全部关键词的文档才算结果
        total = len(set(normalized))
        ranked = [(doc_id, n) for doc_id, n in hits.items() if n == total]
        ranked.sort(key=lambda pair: (-pair[1], pair[0]))
        return ranked
```

索引层不依赖任何第三方库，add/remove/query 三个方法即可支撑服务层的检索路由。
