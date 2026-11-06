"""多 Agent 协作执行器：一个角色、多个执行体。

调度器只认 ``execute(task, pack, fix_hints=...)``，所以这里做成"看起来像 Agent"
的包装：内部分流、并行、汇总，对外仍返回一个 ``TaskResult``。这样调度器、
门禁、checkpoint 全都不用改。

三种协作模式：

- ``split_by_path``：按 ``path_scope`` 切开任务契约，各写各的，天然不冲突；
- ``primary_with_review``：主笔产出后由 review agent 出一份意见（不落盘、不改裁决）；
- ``best_of`` / ``merge``：各写一份到影子任务（独立 task id，不进本任务的 MANIFEST），
  再由 judge 择优或按路径拼接。

**降级必须可见**：复核取不到意见时记 ``agent_degraded``，不会悄悄当成"审过了"。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime

from agentcrew.agents.base_agent import AgentExecutionError, BaseAgent
from agentcrew.agents.plan import ResolvedAgent, pick_primary
from agentcrew.artifacts.spec import ArtifactRecord
from agentcrew.utils.files import path_in_scope
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.contracts import EventSink
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import Message
from agentcrew.roles.binding import BindingError, Duty, RoleAgents
from agentcrew.roles.spec import RoleSpec
from agentcrew.task.task import Task, TaskResult
from agentcrew.utils.tokens import estimate_tokens

#: 复核/裁判看多少产物内容：够判断"有没有按标准做"，不追求全文。
_EXCERPT_TOKENS = 1800

#: 影子产物的目录前缀（best_of / merge 的草稿）。独立 task id，不进正式清单。
_DRAFT_DIR = "drafts"


class TeamAgent:
    """把多个执行体包装成一个可被调度器直接调用的"角色"。"""

    def __init__(
        self,
        spec: RoleSpec,
        binding: RoleAgents,
        members: Sequence[ResolvedAgent],
        *,
        get_agent: Callable[[str], BaseAgent | TeamAgent],
        models: ModelFactory,
        store: ArtifactStore,
        ledger: EventSink,
        settings: Settings,
        run_id: str,
    ) -> None:
        self.spec = spec
        self.binding = binding
        self.members = list(members)
        self._get_agent = get_agent
        self._models = models
        self._store = store
        self._ledger = ledger
        self._settings = settings
        self._run_id = run_id

    # ── 入口 ──────────────────────────────────────────────────────────────

    async def execute(
        self, task: Task, pack: object, *, fix_hints: Sequence[str] = ()
    ) -> TaskResult:
        mode = self.binding.aggregation.mode if self.binding.aggregation else "single"
        if mode == "single" or len(self.members) <= 1:
            return await self._single(task, pack, fix_hints)
        self._ledger.emit(
            "agent_team_start", task_id=task.id, role=self.spec.key, mode=mode,
            members=[m.profile for m in self.members])
        if mode == "split_by_path":
            return await self._split(task, pack, fix_hints)
        if mode == "primary_with_review":
            return await self._with_review(task, pack, fix_hints)
        if mode == "best_of":
            return await self._best_of(task, pack, fix_hints)
        if mode == "merge":
            return await self._merge(task, pack, fix_hints)
        raise BindingError(f"未知的汇总模式：{mode}")

    # ── 单执行体（等价原路径）──────────────────────────────────────────────

    async def _single(
        self, task: Task, pack: object, fix_hints: Sequence[str]
    ) -> TaskResult:
        primary = pick_primary(
            [m for m in self.members if m.duty in (Duty.PRIMARY, Duty.SECTION)],
            run_id=self._run_id, task_id=task.id, attempt=task.attempts, role=self.spec.key)
        return await self._get_agent(primary.profile).execute(
            task, pack, fix_hints=fix_hints)

    # ── split_by_path ─────────────────────────────────────────────────────

    def _split_plan(self, task: Task) -> list[tuple[ResolvedAgent, list]]:
        """把契约按 path_scope 分给各 section；分不到产物的成员不参与。"""
        assignments: list[tuple[ResolvedAgent, list]] = []
        for member in self.members:
            if member.duty not in (Duty.PRIMARY, Duty.SECTION):
                continue
            specs = [
                spec for spec in task.deliverables
                if not member.path_scope
                or path_in_scope(spec.path, member.path_scope)
            ]
            if specs:
                assignments.append((member, specs))
        return assignments

    async def _split(
        self, task: Task, pack: object, fix_hints: Sequence[str]
    ) -> TaskResult:
        assignments = self._split_plan(task)
        if not assignments:
            raise AgentExecutionError(
                f"角色 {self.spec.key} 的 path_scope 覆盖不到任务 {task.id} 的任何产物",
                task_id=task.id, role=self.spec.key)
        started = datetime.now()
        results = await asyncio.gather(*[
            self._get_agent(member.profile).execute(
                task.model_copy(update={"deliverables": specs}), pack, fix_hints=fix_hints)
            for member, specs in assignments
        ], return_exceptions=True)

        ok = [r for r in results if isinstance(r, TaskResult)]
        failed = [r for r in results if isinstance(r, BaseException)]
        for member, _ in assignments:
            self._ledger.emit("agent_participated", task_id=task.id,
                              role=self.spec.key, profile=member.profile, mode="split")
        quorum = (self.binding.aggregation.quorum if self.binding.aggregation else 1)
        if len(ok) < quorum:
            first = failed[0] if failed else None
            raise AgentExecutionError(
                f"角色 {self.spec.key} 的分工执行未达 quorum（{len(ok)}/{quorum}）：{first}",
                task_id=task.id, role=self.spec.key)
        return self._merge_results(task, ok, started, mode="split_by_path")

    # ── primary_with_review ───────────────────────────────────────────────

    async def _with_review(
        self, task: Task, pack: object, fix_hints: Sequence[str]
    ) -> TaskResult:
        started = datetime.now()
        primary = pick_primary(
            [m for m in self.members if m.duty in (Duty.PRIMARY, Duty.SECTION)],
            run_id=self._run_id, task_id=task.id, attempt=task.attempts, role=self.spec.key)
        result = await self._get_agent(primary.profile).execute(
            task, pack, fix_hints=fix_hints)
        self._ledger.emit("agent_participated", task_id=task.id,
                          role=self.spec.key, profile=primary.profile, mode="primary")

        reviews: list[str] = []
        for reviewer in [m for m in self.members if m.duty is Duty.REVIEW]:
            try:
                memo = await self._review_once(reviewer, task, result)
            except Exception as exc:  # noqa: BLE001 —— 复核失败不得吞掉主笔成果
                self._ledger.emit(
                    "agent_degraded", task_id=task.id, role=self.spec.key,
                    profile=reviewer.profile, duty="review",
                    reason=f"{type(exc).__name__}: {exc}")
                reviews.append(f"[{reviewer.profile}] 复核不可用：{exc}")
                continue
            self._ledger.emit(
                "agent_review", task_id=task.id, role=self.spec.key,
                profile=reviewer.profile, chars=len(memo))
            reviews.append(f"[{reviewer.profile}] {memo.strip()}")
        if not reviews:
            return result
        note = "\n\n".join(["## 复核意见", *reviews])
        merged = self._merge_results(task, [result], started, mode="primary_with_review")
        return merged.model_copy(update={"notes": (merged.notes + "\n\n" + note).strip()})

    async def _review_once(
        self, reviewer: ResolvedAgent, task: Task, result: TaskResult
    ) -> str:
        material = self._artifact_excerpt(result)
        messages: list[Message] = [
            {"role": "system", "content": (
                f"你是{self.spec.title}的复核者（{reviewer.profile}）。"
                "只输出问题清单：每条一行，指向具体产物与具体差距；"
                "没有问题就输出「未发现问题」。不要重写产物，不要给鼓励性评语。")},
            {"role": "user", "content": (
                f"## 任务\n{task.title}\n{task.description}\n\n"
                f"## 验收标准\n" + "\n".join(
                    f"{i}. {c}" for i, c in enumerate(task.acceptance_criteria, 1)) +
                f"\n\n## 产物节选\n{material}")},
        ]
        completion = await self._models.call(
            reviewer.alias, messages,
            temperature=0.1, max_tokens=800,
            purpose="review", role=self.spec.key)
        return completion.text

    def _artifact_excerpt(self, result: TaskResult) -> str:
        blocks: list[str] = []
        remaining = _EXCERPT_TOKENS
        for record in result.artifacts:
            if remaining <= 0:
                break
            try:
                text = self._store.read(record.path)
            except OSError:
                continue
            if estimate_tokens(text) > remaining:
                text = text[: remaining * 3] + "\n…（截断）"
            remaining -= estimate_tokens(text)
            blocks.append(f"### {record.path}\n{text}")
        return "\n\n".join(blocks) or "（无产物）"

    # ── best_of / merge ───────────────────────────────────────────────────

    def _shadow_task(self, task: Task, profile: str) -> Task:
        """影子任务：独立 task id + drafts/ 前缀，草稿不进正式产物清单。"""
        specs = [
            spec.model_copy(update={"path": f"{_DRAFT_DIR}/{profile}/{spec.path}"})
            for spec in task.deliverables
        ]
        return task.model_copy(update={
            "id": f"{task.id}#{profile}",
            "deliverables": specs,
        })

    async def _run_shadows(
        self, task: Task, pack: object, fix_hints: Sequence[str]
    ) -> list[tuple[ResolvedAgent, TaskResult]]:
        primaries = [m for m in self.members if m.duty in (Duty.PRIMARY, Duty.SECTION)]
        if not primaries:
            raise AgentExecutionError(
                f"角色 {self.spec.key} 没有 primary agent", task_id=task.id, role=self.spec.key)
        results = await asyncio.gather(*[
            self._get_agent(member.profile).execute(
                self._shadow_task(task, member.profile), pack, fix_hints=fix_hints)
            for member in primaries
        ], return_exceptions=True)
        pairs: list[tuple[ResolvedAgent, TaskResult]] = []
        for member, outcome in zip(primaries, results, strict=True):
            if isinstance(outcome, TaskResult):
                pairs.append((member, outcome))
        if not pairs:
            raise AgentExecutionError(
                f"角色 {self.spec.key} 的候选执行体全部失败", task_id=task.id, role=self.spec.key)
        return pairs

    async def _best_of(
        self, task: Task, pack: object, fix_hints: Sequence[str]
    ) -> TaskResult:
        started = datetime.now()
        pairs = await self._run_shadows(task, pack, fix_hints)
        judge = self.binding.judge
        if judge is None:
            raise BindingError(f"角色 {self.spec.key} 的 best_of 缺少 judge")
        winner = await self._judge(task, pairs, judge.ref)
        chosen = next((r for m, r in pairs if m.profile == winner), pairs[0][1])
        self._ledger.emit(
            "agent_aggregated", task_id=task.id, role=self.spec.key,
            mode="best_of", participants=[m.profile for m, _ in pairs], winner=winner)
        result = self._promote(task, chosen)
        return self._merge_results(task, [result], started, mode="best_of")

    async def _judge(
        self, task: Task, pairs: list[tuple[ResolvedAgent, TaskResult]], judge_ref: str
    ) -> str:
        sections: list[str] = []
        for member, result in pairs:
            sections.append(
                f"### 候选 {member.profile}\n{self._artifact_excerpt(result)}")
        messages: list[Message] = [
            {"role": "system", "content": (
                "你是评审裁判。按验收标准比较候选产物，"
                "只输出一行：WINNER: <候选名>。不要解释。")},
            {"role": "user", "content": (
                "## 验收标准\n" + "\n".join(
                    f"{i}. {c}" for i, c in enumerate(task.acceptance_criteria, 1)) +
                "\n\n" + "\n\n".join(sections))},
        ]
        completion = await self._models.call(
            self._resolve_alias(judge_ref), messages,
            temperature=0.0, max_tokens=60, purpose="review", role=self.spec.key)
        text = completion.text or ""
        marker = "WINNER:"
        winner = text.split(marker, 1)[1].strip().split()[0] if marker in text else ""
        winner = winner.strip("`*: ")
        names = {m.profile for m, _ in pairs}
        if winner not in names:
            raise AgentExecutionError(
                f"裁判输出无法解析为候选名：{text[:80]!r}",
                task_id=task.id, role=self.spec.key)
        return winner

    async def _merge(
        self, task: Task, pack: object, fix_hints: Sequence[str]
    ) -> TaskResult:
        started = datetime.now()
        pairs = await self._run_shadows(task, pack, fix_hints)
        contents: dict[str, list[tuple[str, str]]] = {}
        for member, _result in pairs:
            for spec in task.deliverables:
                draft = f"{_DRAFT_DIR}/{member.profile}/{spec.path}"
                try:
                    contents.setdefault(spec.path, []).append(
                        (member.profile, self._store.read(draft)))
                except OSError:
                    continue
        records: list[ArtifactRecord] = []
        for spec in task.deliverables:
            parts = contents.get(spec.path)
            if not parts:
                continue
            merged = "\n\n".join(
                f"<!-- {profile} -->\n{text}" for profile, text in parts)
            records.append(self._store.commit(spec.path, merged, spec=spec, task=task))
        self._ledger.emit(
            "agent_aggregated", task_id=task.id, role=self.spec.key, mode="merge",
            participants=[m.profile for m, _ in pairs])
        finished = datetime.now()
        return TaskResult(
            task_id=task.id, role=self.spec.key,
            summary=f"合并 {len(pairs)} 个执行体的产物",
            artifacts=records, notes="", started_at=started, finished_at=finished,
            duration_ms=int((finished - started).total_seconds() * 1000))

    def _promote(self, task: Task, result: TaskResult) -> TaskResult:
        """把中标草稿从 ``drafts/<profile>/`` 复制到正式路径。

        正式落盘用**原任务**提交：契约、冲突检测、MANIFEST 归属都回到正轨，
        草稿只留在 drafts/ 供审计。
        """
        records: list[ArtifactRecord] = []
        for record in result.artifacts:
            if not record.path.startswith(_DRAFT_DIR + "/"):
                continue
            relative = record.path.split("/", 2)[-1]
            spec = next((d for d in task.deliverables if d.path == relative), None)
            if spec is None:
                continue
            records.append(self._store.commit(
                spec.path, self._store.read(record.path), spec=spec, task=task))
        return result.model_copy(update={"artifacts": records})

    def _resolve_alias(self, profile: str) -> str:
        for member in self.members:
            if member.profile == profile:
                return member.alias
        raise BindingError(f"角色 {self.spec.key} 没有 profile {profile}")

    # ── 结果汇总 ──────────────────────────────────────────────────────────

    def _merge_results(
        self, task: Task, results: Sequence[TaskResult], started: datetime, *, mode: str
    ) -> TaskResult:
        artifacts: list[ArtifactRecord] = []
        for result in results:
            artifacts.extend(result.artifacts)
        summary = "；".join(r.summary for r in results if r.summary) or f"{mode} 完成"
        notes = "\n".join(r.notes for r in results if r.notes)
        finished = datetime.now()
        return TaskResult(
            task_id=task.id, role=self.spec.key, summary=summary,
            artifacts=artifacts, notes=notes, started_at=started, finished_at=finished,
            duration_ms=int((finished - started).total_seconds() * 1000))
