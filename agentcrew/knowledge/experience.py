"""成功经验模型与全局经验库（P3-5 / P3-7）。

``Experience`` 是一条「任务描述 → 解决方案」的沉淀记录：它来自某一次成功 run 里
一个已通过全部门禁的任务，供后续 run 检索复用。经验库落在
``output_dir/knowledge/experiences.jsonl``（JSONL，每行一条），**全局共享、跨 run**
——不在任何 run 目录之下，因此 run 被 ``clean`` 删掉后经验依然保留。

检索用 BM25（R11，见 :mod:`agentcrew.knowledge.retrieval`）：中文按单字 + 相邻
二元组切分，IDF 自动压低虚词、长度归一避免长文本占优。此前是「单字命中计数」，
「写代码」与「写文档」会互相命中，一个「的」字就足以把无关经验顶上来。

**为什么不用向量检索**：全仓没有 embedding 接口，接一个就要引 provider 方法、网络
调用与计费，而项目红线是「离线可跑」。BM25 是这条红线下最实的一步；等 embedding
通道存在了，替换点只在 :meth:`ExperienceStore.query` 这一处。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentcrew.knowledge.embeddings import cosine
from agentcrew.knowledge.retrieval import BM25Index


class Experience(BaseModel):
    """一条从成功 run 沉淀下来的经验（任务描述 → 解决方案映射）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    task_description: str  # 任务描述
    role: str  # 执行角色（RoleSpec.key）
    solution_summary: str  # 解决方案摘要
    artifacts: list[str]  # 产出文件路径（相对 artifacts/ 根）
    key_decisions: list[str]  # 关键决策（来自 ContextBus）
    success: bool  # 是否成功
    run_id: str  # 来源 run
    created_at: datetime


class ExperienceStore:
    """全局经验库的读写器：``output_dir/knowledge/experiences.jsonl``。

    追加写（``add``）与 Ledger 同理——历史经验不允许被覆盖；读取宽容（文件不存在
    视为空库，坏行跳过并记 warning，与 Ledger 的「读取宽」一致）。
    """

    def __init__(self, output_dir: Path) -> None:
        self._path = Path(output_dir) / "knowledge" / "experiences.jsonl"

    @property
    def path(self) -> Path:
        """经验库文件路径（``output_dir/knowledge/experiences.jsonl``）。"""
        return self._path

    def add(self, experience: Experience) -> None:
        """追加一条经验到 JSONL；父目录不存在时自动创建。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = experience.model_dump_json() + "\n"
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line)

    def all(self) -> list[Experience]:
        """返回全部经验（按落盘顺序）；文件不存在视为空库。"""
        if not self._path.is_file():
            return []
        out: list[Experience] = []
        with open(self._path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    out.append(Experience.model_validate_json(stripped))
        return out

    def query(
        self,
        task_description: str,
        *,
        role: str | None = None,
        limit: int = 5,
        doc_vectors: Sequence[Sequence[float]] | None = None,
        query_vector: Sequence[float] | None = None,
        vector_weight: float = 0.5,
    ) -> list[Experience]:
        """按任务描述检索相关经验，返回得分最高的前 ``limit`` 条。

        ``role`` 给定时先按角色过滤——这是硬约束，索引也只建在过滤后的候选上，
        免得某个角色独有的词把 IDF 带偏。零命中时返回空列表，而不是把整库端上来：
        「相关」必须至少真的沾边。

        R27 向量混合：给了 ``doc_vectors``（与 ``self.all()`` 同序）和
        ``query_vector`` 时按 ``(1-w)*BM25归一 + w*余弦`` 打分；权重 0 等价于
        纯 BM25（行为与 R11 一致）。向量通道不可用时调用方不传这两个参数即可。
        """
        all_experiences = self.all()
        candidates = [
            (offset, exp) for offset, exp in enumerate(all_experiences)
            if role is None or exp.role == role
        ]
        if not candidates:
            return []
        index = BM25Index([self.haystack(exp) for _, exp in candidates])
        bm25_hits = index.search(task_description, max(0, limit) * 4 or 0)
        bm25_scores = {offset: score for offset, score in bm25_hits}
        if (doc_vectors is None or query_vector is None
                or len(doc_vectors) != len(all_experiences)):
            return [candidates[offset][1] for offset, _ in bm25_hits[:max(0, limit)]]

        weight = min(1.0, max(0.0, float(vector_weight)))
        ceiling = max(bm25_scores.values(), default=0.0) or 1.0
        scored: list[tuple[int, float]] = []
        for offset, _exp in candidates:
            text_score = bm25_scores.get(offset, 0.0) / ceiling
            semantic = max(0.0, cosine(query_vector, doc_vectors[offset]))
            total = (1.0 - weight) * text_score + weight * semantic
            if total > 0:
                scored.append((offset, total))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [candidates[offset][1] for offset, _ in scored[:max(0, limit)]]

    @staticmethod
    def haystack(exp: Experience) -> str:
        """一条经验参与检索的全文：任务描述 + 解决方案 + 关键决策。

        产物路径不参与：它们区分度低（大量 ``utils.py`` 之类），又容易让同一批
        经验靠路径互相命中。
        """
        return "\n".join(
            (exp.task_description, exp.solution_summary,
             " ".join(exp.key_decisions))
        )

    def inject_context(
        self,
        goal: str,
        *,
        limit: int = 3,
        doc_vectors: Sequence[Sequence[float]] | None = None,
        query_vector: Sequence[float] | None = None,
        vector_weight: float = 0.5,
    ) -> str:
        """检索与项目目标相关的历史经验，渲染成可直接拼进 PM 上下文的文本。

        无相关经验（空库或零命中）时返回空串——调用方据此不注入任何内容，行为与
        改造前逐字一致（向后兼容）。渲染格式固定：

        ```
        ## 历史经验（来自之前的成功 run）
        ### 经验 1：<task_description>
        - 角色：<role>
        - 解决方案：<solution_summary>
        - 关键决策：<key_decisions>
        ```
        """
        exps = self.query(goal, limit=limit, doc_vectors=doc_vectors,
                         query_vector=query_vector, vector_weight=vector_weight)
        if not exps:
            return ""
        lines = ["## 历史经验（来自之前的成功 run）"]
        for index, exp in enumerate(exps, start=1):
            lines.append(f"### 经验 {index}：{exp.task_description}")
            lines.append(f"- 角色：{exp.role}")
            lines.append(f"- 解决方案：{exp.solution_summary}")
            if exp.key_decisions:
                lines.append("- 关键决策：" + "；".join(exp.key_decisions))
        return "\n".join(lines)
