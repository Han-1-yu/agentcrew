"""成功经验模型与全局经验库（P3-5 / P3-7）。

``Experience`` 是一条「任务描述 → 解决方案」的沉淀记录：它来自某一次成功 run 里
一个已通过全部门禁的任务，供后续 run 检索复用。经验库落在
``output_dir/knowledge/experiences.jsonl``（JSONL，每行一条），**全局共享、跨 run**
——不在任何 run 目录之下，因此 run 被 ``clean`` 删掉后经验依然保留。

检索是朴素的关键词匹配（P3-5 不引入向量检索）：把查询与经验的任务描述 / 解决方案 /
关键决策统一切成「英文词 + 单字 CJK」token，按命中数排序取前 N。足够粗糙、足够
可测，且不引入任何外部依赖。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

#: 经验描述 / 摘要进关键词倒排前的分词：英文数字按词切，CJK 按单字切。
#: 中文没有天然空格分词，按单字切是「够用的关键词匹配」——它对「待办」「后端」
#: 这类词能命中，又不引入 jieba 等外部依赖。
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> set[str]:
    """把一段文本切成用于关键词匹配的 token 集合（小写、去重）。"""
    return set(_TOKEN_RE.findall(text.lower()))


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
    ) -> list[Experience]:
        """按任务描述的关键词匹配查询相关经验，返回命中数最高的前 ``limit`` 条。

        ``role`` 给定时先按角色过滤；无任何 token 命中（或查询无有效 token）时返回
        空列表，而不是把整库端上来——「相关」必须至少有一个词沾边。
        """
        candidates = self.all()
        if role is not None:
            candidates = [exp for exp in candidates if exp.role == role]
        scored = [
            (self._score(task_description, exp), exp) for exp in candidates
        ]
        scored = [(score, exp) for score, exp in scored if score > 0]
        scored.sort(key=lambda item: (-item[0], item[1].created_at))
        return [exp for _, exp in scored[: max(0, limit)]]

    def inject_context(self, goal: str, *, limit: int = 3) -> str:
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
        exps = self.query(goal, limit=limit)
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

    @staticmethod
    def _score(query: str, exp: Experience) -> int:
        """一条查询与一条经验的关键词命中数（查询 token 在经验文本中出现的个数）。"""
        query_tokens = _tokenize(query)
        if not query_tokens:
            return 0
        haystack = " ".join(
            (
                exp.task_description,
                exp.solution_summary,
                " ".join(exp.key_decisions),
            )
        )
        haystack_tokens = _tokenize(haystack)
        return sum(1 for token in query_tokens if token in haystack_tokens)
