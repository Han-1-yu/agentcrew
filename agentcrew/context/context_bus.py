"""上下文包组装：把上游产物压缩成 Agent 真正看得到的上下文（spec §11，B5）。

八个 Agent 如果互相全文投喂产物，任何窗口都会被撑爆；本模块因此按 spec §11.2 的
六级优先级把「项目目标 + 本任务 + 上游摘要 + 决策 + 文件树 + 关键节选 + 更远祖先」
装进一个 :class:`ContextPack`：默认只给摘要与文件路径，正文只在预算允许时给节选。

组装规则（resolutions 已钉死，勿擅改）：

1. **不可压缩**：项目目标 + 本任务描述/验收标准 + ``fix_hints`` —— 无条件装入，
   且不占预算（``budget_tokens=1`` 时目标与任务本身也必须存活）。
2. 直接上游（``task.dependencies``）的 ``brief`` + ``summary``，每个截到
   ``_DIGEST_MAX_TOKENS`` 以内（spec §11.2「每个 ≤400 tok」），按截断后的
   **实际大小**计费 —— 不按上限预留，小摘要不占大预算。
3. 全局决策记录（最近 20 条，最新的在前）。
4. ``file_tree``：超 ``tree_max_entries`` 时折叠出 ``"... (N more)"`` 一行。
5. ``key_excerpts``：按「被下游依赖次数降序、path 升序」逐个装入，单个文件的
   节选上限 = **剩余预算** × ``excerpt_max_ratio``（spec §11.2「最多占剩余预算的
   40%」），按行截断并置 ``FileExcerpt.truncated``。
6. 仍有余量才装入更远的祖先摘要（追加在直接上游之后）。

任一非第 1 级项因预算装不下而被跳过，或任一节选被截断，``pack.truncated=True``；
``BaseAgent`` 据此在提示词末尾追加 :data:`TRUNCATION_NOTICE`，避免模型误以为
「没给的就是不存在」。
"""

import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.artifacts.spec import ArtifactRecord
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.context.budget import TokenBudget
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard
from agentcrew.utils.files import tree
from agentcrew.utils.tokens import estimate_tokens

#: spec §11.2 的固定提示句；``BaseAgent`` 在 ``pack.truncated`` 时逐字追加。
TRUNCATION_NOTICE = "注意：为控制上下文长度，部分上游产物未全文展示；如需查看，请依据文件路径读取。"

#: 单个上游摘要（第 2/6 级）的截断上限（spec §11.2「每个 ≤400 tok」）；计费按截断后实际大小。
_DIGEST_MAX_TOKENS = 400

#: 上下文包携带的决策记录条数上限（spec §11.2「最近 20 条」）。
_MAX_PACK_DECISIONS = 20

#: 折叠 ``file_tree`` 时用来完整列举一次、数出真实条数的上限（``tree`` 用切片截断，
#: 需要一个具体整数；``+1`` 惯例只能判断「还有更多」，不能计数）。
_UNBOUNDED_ENTRIES = sys.maxsize


class UpstreamDigest(BaseModel):
    """一个上游任务的摘要：默认只给 ``brief`` + ``summary`` + 文件路径，不给全文。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    role: str
    title: str
    brief: str
    summary: str
    files: list[str] = Field(default_factory=list)


class FileExcerpt(BaseModel):
    """一个文件的正文节选；``truncated`` 表示因预算只给了开头。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    truncated: bool = False


class DecisionRecord(BaseModel):
    """一条全局决策记录（spec §11.3 的轻量共享消息池）。"""

    model_config = ConfigDict(extra="forbid")

    at: datetime
    task_id: str
    role: str
    decision: str


class ContextPack(BaseModel):
    """一个任务实际拿到的上下文（spec §11.1），可 JSON 往返序列化。"""

    model_config = ConfigDict(extra="forbid")

    project_goal: str
    task: Task
    upstream: list[UpstreamDigest] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    file_tree: list[str] = Field(default_factory=list)
    key_excerpts: list[FileExcerpt] = Field(default_factory=list)
    total_estimated_tokens: int = 0
    truncated: bool = False


class ContextBus:
    """按预算组装 :class:`ContextPack`，并持有全局决策记录。"""

    def __init__(
        self,
        goal: str,
        board: TaskBoard,
        store: ArtifactStore,
        *,
        budget_tokens: int = 24000,
        excerpt_max_ratio: float = 0.4,
        tree_max_entries: int = 300,
    ) -> None:
        self._goal = goal
        self._board = board
        self._store = store
        self._budget_tokens = budget_tokens
        self._excerpt_max_ratio = excerpt_max_ratio
        self._tree_max_entries = tree_max_entries
        self._decisions: list[DecisionRecord] = []

    @property
    def store(self) -> ArtifactStore:
        """本 run 的产物仓库（组装上下文包时读取节选所用的那一个）。

        调度器在派发返工任务前要清理该任务的旧产物（``discard_task``），必须用
        与门禁、Agent 完全相同的仓库实例；它就是这里持有的这一个。
        """
        return self._store

    # ---- 组装 ----

    def build(self, task: Task, *, fix_hints: Sequence[str] = ()) -> ContextPack:
        """为一个任务组装上下文包（六级优先级，装不下即跳过该项）。

        ``upstream`` 只放**直接**依赖；更远的祖先只在第 6 级、且预算允许时追加。
        """
        budget = TokenBudget(self._budget_tokens)
        tasks = self._board.map()
        truncated = False

        # 级 1：不可压缩，无条件装入、不占预算。
        incompressible = _incompressible_text(self._goal, task, fix_hints)

        # 级 2：直接上游摘要（每个截到 400 tok 内，按截断后的实际大小计费）。
        upstream: list[UpstreamDigest] = []
        direct = [tasks[dep] for dep in dict.fromkeys(task.dependencies) if dep in tasks]
        for dep in direct:
            digest = self._digest(dep)
            if budget.try_consume(_digest_text(digest)):
                upstream.append(digest)
            else:
                truncated = True

        # 级 3：决策记录（最近 20 条，最新的在前）。
        decisions: list[DecisionRecord] = []
        recent = list(reversed(self._decisions[-_MAX_PACK_DECISIONS:]))
        if recent:
            if budget.try_consume("".join(r.model_dump_json() for r in recent)):
                decisions = recent
            else:
                truncated = True

        # 级 4：文件树快照（超阈值折叠为一行）。
        file_tree: list[str] = []
        lines = self._file_tree_lines()
        if lines:
            if budget.try_consume("\n".join(lines)):
                file_tree = lines
            else:
                truncated = True

        # 级 5：关键节选（单个文件最多占剩余预算的 excerpt_max_ratio）。
        key_excerpts: list[FileExcerpt] = []
        for record in self._excerpt_candidates(task):
            cap = int(budget.remaining * self._excerpt_max_ratio)
            if cap <= 0:
                truncated = True
                break
            # 3*cap 字节足够覆盖任何 token 估算 ≤ cap 的前缀（estimate >= len//3）。
            content = self._store.read(record.path, max_bytes=3 * cap + 3)
            excerpt, was_truncated = _truncate_lines(content, cap)
            if not excerpt:
                truncated = True
                continue
            if not budget.try_consume(f"{record.path}\n{excerpt}"):
                truncated = True
                continue
            key_excerpts.append(
                FileExcerpt(path=record.path, content=excerpt, truncated=was_truncated)
            )
            if was_truncated:
                truncated = True

        # 级 6：更远祖先的摘要（追加在直接上游之后，同样按实际大小计费）。
        for ancestor_id in self._distant_ancestor_ids(task, tasks):
            digest = self._digest(tasks[ancestor_id])
            if budget.try_consume(_digest_text(digest)):
                upstream.append(digest)
            else:
                truncated = True

        return ContextPack(
            project_goal=self._goal,
            task=task,
            upstream=upstream,
            decisions=decisions,
            file_tree=file_tree,
            key_excerpts=key_excerpts,
            total_estimated_tokens=estimate_tokens(incompressible) + budget.used,
            truncated=truncated,
        )

    def render_notice(self) -> str:
        """返回固定的截断说明句（``pack.truncated`` 为真时由 ``BaseAgent`` 追加）。"""
        return TRUNCATION_NOTICE

    # ---- 决策记录（spec §11.3）----

    def add_decision(self, record: DecisionRecord) -> None:
        """追加一条决策记录。"""
        self._decisions.append(record)

    def decisions(self) -> list[DecisionRecord]:
        """全部决策记录的副本（写入顺序）。"""
        return list(self._decisions)

    def save_decisions(self, path: Path) -> None:
        """把全部决策记录以 JSONL **追加**写入：每行一个 ``model_dump_json()``。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for record in self._decisions:
                handle.write(record.model_dump_json() + "\n")

    def load_decisions(self, path: Path) -> None:
        """从 JSONL 读入决策记录并追加到内存表。

        文件不存在视为空（新 run 尚无决策）；空行跳过；非法 JSON 行抛
        ``ValueError``（带行号），不静默吞掉损坏的记录。
        """
        path = Path(path)
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = DecisionRecord.model_validate_json(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"决策记录 JSONL 第 {number} 行不是合法 JSON: {path}"
                    ) from exc
                self._decisions.append(record)

    def record_rework_decision(
        self, task: Task, fix_hints: Sequence[str]
    ) -> DecisionRecord | None:
        """把返工意见沉淀为一条决策记录（spec §11.3）。

        供 Gatekeeper 每次驳回后**无条件**调用：任务返工次数 ``>= 1`` 且
        ``fix_hints`` 非空时才产生记录，否则返回 ``None``。
        """
        hints = [hint.strip() for hint in fix_hints if hint.strip()]
        if task.rework_count < 1 or not hints:
            return None
        record = DecisionRecord(
            at=datetime.now(),
            task_id=task.id,
            role=task.role,
            decision="；".join(hints),
        )
        self.add_decision(record)
        return record

    # ---- 内部 ----

    def _digest(self, task: Task) -> UpstreamDigest:
        """把一个上游任务压成摘要：brief + summary（截到 400 tok 内）+ 文件路径。"""
        summary = task.result.summary if task.result is not None else ""
        brief, summary = _fit_digest_fields(task.brief, summary)
        files = sorted(record.path for record in self._store.by_task(task.id))
        return UpstreamDigest(
            task_id=task.id,
            role=task.role,
            title=task.title,
            brief=brief,
            summary=summary,
            files=files,
        )

    def _file_tree_lines(self) -> list[str]:
        """文件树快照；超 ``tree_max_entries`` 时折叠为末尾一行 ``"... (N more)"``。

        ``utils.files.tree`` 的 ``+1`` 惯例只能判断「还有更多」；折叠行要给出**真实**
        条数，因此超阈值时再完整列举一次来计数。
        """
        paths = tree(self._store.root, max_entries=self._tree_max_entries + 1)
        if len(paths) <= self._tree_max_entries:
            return paths
        total = len(tree(self._store.root, max_entries=_UNBOUNDED_ENTRIES))
        hidden = total - self._tree_max_entries
        return [*paths[: self._tree_max_entries], f"... ({hidden} more)"]

    def _excerpt_candidates(self, task: Task) -> list[ArtifactRecord]:
        """节选候选：除本任务自己产物外的全部落盘记录，按「被下游依赖次数降序, path 升序」。"""
        counts: dict[str, int] = {}
        for other in self._board.all():
            for dep in set(other.dependencies):
                counts[dep] = counts.get(dep, 0) + 1
        records = [
            record
            for record in self._store.records()
            if record.produced_by_task != task.id
        ]
        return sorted(
            records,
            key=lambda record: (-counts.get(record.produced_by_task, 0), record.path),
        )

    def _distant_ancestor_ids(self, task: Task, tasks: dict[str, Task]) -> list[str]:
        """``task`` 的传递祖先中**不是**直接依赖的那些（BFS 序，去重、防环）。

        直接依赖本身由第 2 级装入，这里从它们的父节点继续向外展开。
        """
        seen: dict[str, None] = {}
        direct = set(task.dependencies)
        queue: list[str] = []
        for dep_id in task.dependencies:
            parent = tasks.get(dep_id)
            if parent is not None:
                queue.extend(parent.dependencies)
        while queue:
            task_id = queue.pop(0)
            if task_id in seen or task_id in direct or task_id == task.id:
                continue
            seen[task_id] = None
            ancestor = tasks.get(task_id)
            if ancestor is not None:
                queue.extend(ancestor.dependencies)
        return list(seen)


def _digest_text(digest: UpstreamDigest) -> str:
    """摘要进提示词时要占的正文（字段换行拼接，不含 JSON 包装）；第 2/6 级按它计费。"""
    parts = [
        digest.task_id,
        digest.role,
        digest.title,
        digest.brief,
        digest.summary,
        *digest.files,
    ]
    return "\n".join(part for part in parts if part)


def _incompressible_text(goal: str, task: Task, fix_hints: Sequence[str]) -> str:
    """第 1 级不可压缩块的文本：目标 + 本任务元信息 + 验收标准 + 返工意见。"""
    parts = [
        goal,
        task.id,
        task.role,
        task.title,
        task.description,
        *task.acceptance_criteria,
        *fix_hints,
    ]
    return "\n".join(parts)


def _fit_digest_fields(brief: str, summary: str) -> tuple[str, str]:
    """把一条摘要的 ``brief`` + ``summary`` 整体压进 ``_DIGEST_MAX_TOKENS``。

    ``brief`` 是 PM 写给下游的一句话，优先完整保留；放不下时先截 ``summary``，
    连 ``brief`` 本身都超限才截 ``brief`` 并丢弃 ``summary``。
    """
    combined = f"{brief}\n{summary}" if summary else brief
    if estimate_tokens(combined) <= _DIGEST_MAX_TOKENS:
        return brief, summary
    if estimate_tokens(brief) > _DIGEST_MAX_TOKENS:
        return _truncate_lines(brief, _DIGEST_MAX_TOKENS)[0], ""
    # 估算非严格可加，留 1 token 余量（len 的整除项最多多算 1）。
    room = max(0, _DIGEST_MAX_TOKENS - estimate_tokens(brief) - 1)
    return brief, _truncate_lines(summary, room)[0]


def _truncate_lines(text: str, max_tokens: int) -> tuple[str, bool]:
    """按行截断到 ``estimate_tokens <= max_tokens`` 的最大前缀。

    返回 ``(截断后的文本, 是否发生截断)``；二分查找行数，避免超长文件上的二次方开销。
    """
    if estimate_tokens(text) <= max_tokens:
        return text, False
    lines = text.splitlines(keepends=True)
    low, high = 0, len(lines)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens("".join(lines[:middle])) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return "".join(lines[:low]), True
