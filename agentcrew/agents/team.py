"""多 Agent 协作执行器：一个角色、多个执行体。

调度器只认 ``execute(task, pack, fix_hints=...)``，所以这里做成"看起来像 Agent"
的包装：内部分流、并行、汇总，对外仍返回一个 ``TaskResult``。这样调度器、
门禁、checkpoint 全都不用改。

三种协作模式：

- ``split_by_path``：按 ``path_scope`` 切开任务契约，各写各的，天然不冲突；
- ``primary_with_review``：主笔产出后由 review agent 出一份意见（不落盘、不改裁决）；
  配了 ``review_rounds`` 时还会把问题清单回灌主笔修订，再复核，最多 N 轮；
- ``best_of`` / ``merge``：各写一份到影子任务（独立 task id，不进本任务的 MANIFEST），
  再由 judge 择优或按路径拼接。

**降级必须可见**：复核取不到意见时记 ``agent_degraded``，不会悄悄当成"审过了"。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from agentcrew.agents.base_agent import AgentExecutionError, BaseAgent
from agentcrew.agents.plan import ResolvedAgent, pick_primary
from agentcrew.artifacts.spec import ArtifactRecord
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextPack, UpstreamDigest
from agentcrew.contracts import EventSink
from agentcrew.gate.verdict import ReviewVerdict, derive_review, is_malformed
from agentcrew.models.agent_budget import bind_budget, release_budget
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import CompletionResult, Message
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.roles.binding import BindingError, Duty, RoleAgents
from agentcrew.roles.spec import RoleSpec
from agentcrew.task.task import Task, TaskResult
from agentcrew.utils.clock import seconds_between
from agentcrew.utils.files import path_in_scope
from agentcrew.utils.tokens import estimate_tokens
from agentcrew.utils.trace import TraceContext, reset_trace, set_trace

#: 复核/裁判看多少产物内容：够判断"有没有按标准做"，不追求全文。
_EXCERPT_TOKENS = 1800

#: 影子产物的目录前缀（best_of / merge 的草稿）。独立 task id，不进正式清单。
_DRAFT_DIR = "drafts"

#: 复核裁决在 ledger 与 mock 回放里的 purpose（``MockProvider`` 据此查 reviews.json）。
_REVIEW_PURPOSE = "review"


def _topological_order(nodes: list[str], deps: dict[str, list[str]]) -> list[str]:
    """稳定的拓扑序：无依赖者按给定顺序先跑。

    成环在配置期已被 ``validate_bindings`` 拒绝；真到这里（例如手工构造的
    ``TeamAgent``）就退回原顺序继续跑，绝不死循环——一个坏配置不该把进程挂住。
    """
    ordered: list[str] = []
    placed: set[str] = set()
    remaining = list(nodes)
    while remaining:
        progressed = False
        for node in list(remaining):
            needs = [d for d in deps.get(node, ()) if d in nodes]
            if all(need in placed for need in needs):
                ordered.append(node)
                placed.add(node)
                remaining.remove(node)
                progressed = True
        if not progressed:
            ordered.extend(remaining)
            break
    return ordered


class _Circuit:
    """run 内的执行体连续失败计数（只在内存在活，不落盘、不跨 run）。

    为什么按"连续"而不是"累计"：一个执行体偶尔被长上下文噎住，不该被永久判死；
    成功一次就清零，熔断的只是"一直失败"这种状态。

    为什么不在 provider 层做：``models/health.py`` 管的是模型不可用（402/429/5xx），
    管不了"这个执行体本身有问题"——工具权限配错、路径域写不进去、输出协议总是不合。
    """

    def __init__(self, threshold: int) -> None:
        #: 阈值下限 2：连一次失败都没发生过就熔断没有意义。
        self.threshold = max(2, int(threshold))
        self._failures: dict[str, int] = {}
        self._opened: set[str] = set()

    def ok(self, profile: str) -> bool:
        return profile not in self._opened

    def success(self, profile: str) -> None:
        self._failures.pop(profile, None)
        self._opened.discard(profile)

    def failure(self, profile: str) -> bool:
        """记一次失败；返回 ``True`` 表示"刚刚跨过阈值"（只报一次）。"""
        count = self._failures.get(profile, 0) + 1
        self._failures[profile] = count
        if count >= self.threshold and profile not in self._opened:
            self._opened.add(profile)
            return True
        return False

    @property
    def opened(self) -> set[str]:
        return set(self._opened)


@dataclass(frozen=True)
class _ReviewOutcome:
    """一个复核执行体对一份产物的结构化结论（不含任何自由文本裁决）。"""

    profile: str
    approved: bool
    issues: list[str]
    """未满足验收标准的说明（含标准原文），写进报告。"""
    hints: list[str]
    """可直接回灌主笔的修订指令；``approved=True`` 时为空。"""
    suggestions: list[str]
    """「还能更好」的建议：进报告，**不触发修订**。"""

    @property
    def summary(self) -> str:
        if self.approved:
            base = "未发现问题"
        else:
            base = "；".join(self.issues) or "有验收标准未满足"
        if self.suggestions:
            base += "（建议：" + "；".join(self.suggestions) + "）"
        return base


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
        #: 执行体级熔断：默认关闭（配置开关），关闭时整条路径零开销。
        self._circuit = (
            _Circuit(settings.agent_circuit_breaker_threshold)
            if settings.agent_circuit_breaker_enabled else None)

    # ── 执行体可用性（熔断）────────────────────────────────────────────────

    def _primary_pool(self) -> list[ResolvedAgent]:
        """本次可用的 primary/section 池；熔断掉的成员被剔除。

        全部被熔断时**退回原池子**而不是返回空：让失败如实发生、由 quorum/重试
        去处理，比"假装没有执行体"更诚实——后者会让任务以一个莫名其妙的原因失败。
        """
        primaries = [m for m in self.members if m.duty in (Duty.PRIMARY, Duty.SECTION)]
        if self._circuit is None:
            return primaries
        usable = [m for m in primaries if self._circuit.ok(m.profile)]
        return usable or primaries

    async def _run_member(
        self, member: ResolvedAgent, task: Task, pack: object,
        fix_hints: Sequence[str],
    ) -> TaskResult:
        """以某个执行体跑一次任务，并维护熔断计数。

        只在**异常**上记失败：任务被门禁驳回（``TaskResult`` 正常返回）不是执行体
        的错，把返工也算进熔断会让"标准太高"变成"模型不行"。
        """
        try:
            result = await self._get_agent(member.profile).execute(
                task, pack, fix_hints=fix_hints)
        except Exception as exc:
            if self._circuit is not None and self._circuit.failure(member.profile):
                self._ledger.emit(
                    "agent_circuit_open", task_id=task.id, role=self.spec.key,
                    profile=member.profile, threshold=self._circuit.threshold,
                    reason=f"{type(exc).__name__}: {exc}")
            raise
        if self._circuit is not None:
            self._circuit.success(member.profile)
        return result

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
            self._primary_pool(),
            run_id=self._run_id, task_id=task.id, attempt=task.attempts, role=self.spec.key)
        return await self._run_member(primary, task, pack, fix_hints)

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
        aggregation = self.binding.aggregation
        if aggregation is not None and aggregation.intra_deps:
            results = await self._split_ordered(
                task, pack, fix_hints, assignments, aggregation.intra_deps)
        else:
            results = await asyncio.gather(*[
                self._run_member(
                    member, task.model_copy(update={"deliverables": specs}), pack, fix_hints)
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

    async def _split_ordered(
        self, task: Task, pack: object, fix_hints: Sequence[str],
        assignments: list[tuple[ResolvedAgent, list]],
        deps: dict[str, list[str]],
    ) -> list[TaskResult | BaseException]:
        """按 ``intra_deps`` 的拓扑顺序执行各 section，前序产物进后者上下文。

        与并行版的关键差别只有两点：

        1. **有先后**：依赖方在依赖项完成（或被判定失败）之后才开工；
        2. **看得见**：依赖方拿到的上下文里多了 ``UpstreamDigest``——被依赖 section
           的摘要与产物路径。文件本来就在盘上，缺的只是"知道它存在"。

        某一项失败**不阻塞**下游：仍按顺序继续跑，由 `quorum` 决定整体成败。
        否则一个 section 失败会让整条分工链静默停摆，连"哪几份产物缺失"都报不出来。
        """
        by_profile = {member.profile: (member, specs) for member, specs in assignments}
        order = _topological_order(list(by_profile), deps)
        outcomes: dict[str, TaskResult | BaseException] = {}
        for profile in order:
            member, specs = by_profile[profile]
            sub_pack = self._pack_with_deps(pack, profile, deps, outcomes)
            try:
                outcomes[profile] = await self._run_member(
                    member, task.model_copy(update={"deliverables": specs}),
                    sub_pack, fix_hints)
            except Exception as exc:  # noqa: BLE001 —— 失败要记录，不能中断整链
                outcomes[profile] = exc
        return [outcomes[member.profile] for member, _ in assignments]

    def _pack_with_deps(
        self, pack: object, profile: str, deps: dict[str, list[str]],
        outcomes: dict[str, TaskResult | BaseException],
    ) -> object:
        """把已完成的依赖 section 作为 ``UpstreamDigest`` 注入上下文。

        只注入**成功**的依赖；``pack`` 不是 ContextPack（单测里常传 ``None``）时
        原样返回——拓扑顺序仍然生效，只是没有可注入的摘要。
        """
        needed = [
            dep for dep in deps.get(profile, ())
            if isinstance(outcomes.get(dep), TaskResult)
        ]
        if not needed or not isinstance(pack, ContextPack):
            return pack
        extra = []
        for dep in needed:
            result = outcomes[dep]
            assert isinstance(result, TaskResult)  # 上面已用 isinstance 过滤
            extra.append(UpstreamDigest(
                task_id=result.task_id, role=self.spec.key,
                title=f"同任务分工成员 {dep}",
                brief="与本任务并行分工的成员，产物已落盘可直接读取",
                summary=result.summary,
                files=[record.path for record in result.artifacts]))
        return pack.model_copy(update={"upstream": [*pack.upstream, *extra]})

    # ── primary_with_review ───────────────────────────────────────────────

    async def _with_review(
        self, task: Task, pack: object, fix_hints: Sequence[str]
    ) -> TaskResult:
        started = datetime.now()
        primary = pick_primary(
            self._primary_pool(),
            run_id=self._run_id, task_id=task.id, attempt=task.attempts, role=self.spec.key)
        result = await self._run_member(primary, task, pack, fix_hints)
        self._ledger.emit("agent_participated", task_id=task.id,
                          role=self.spec.key, profile=primary.profile, mode="primary")

        reviews: list[str] = []
        actionable: list[str] = []
        aggregation = self.binding.aggregation
        max_rounds = max(0, aggregation.review_rounds) if aggregation else 0
        rounds_used = 0
        # ``max_rounds=0``（默认）时循环只跑一次，行为与引入修订回路之前逐字一致：
        # 只收集意见、不回灌。开启后每轮都重新复核**修订后**的产物。
        for round_index in range(max_rounds + 1):
            lines, actionable = await self._collect_reviews(task, result, round_index + 1)
            # 多轮时**累加**而不是覆盖：否则"这一轮要求改什么"会被下一轮的
            # 「未发现问题」冲掉，报告里只剩一句"没问题"，返工过程无从追溯。
            if max_rounds:
                reviews.extend(f"第 {round_index + 1} 轮：{line}" for line in lines)
            else:
                reviews.extend(lines)
            if not actionable or round_index >= max_rounds:
                break
            self._ledger.emit(
                "agent_revise", task_id=task.id, role=self.spec.key,
                profile=primary.profile, round=round_index + 1,
                issues=len(actionable))
            result = await self._run_member(
                primary, task, pack, [*fix_hints, *actionable])
            rounds_used += 1

        if not reviews:
            return result
        note = "\n\n".join(["## 复核意见", *reviews])
        if rounds_used:
            note += f"\n\n（已按复核意见修订 {rounds_used} 轮）"
        merged = self._merge_results(task, [result], started, mode="primary_with_review")
        return merged.model_copy(update={"notes": (merged.notes + "\n\n" + note).strip()})

    async def _collect_reviews(
        self, task: Task, result: TaskResult, round_no: int = 1
    ) -> tuple[list[str], list[str]]:
        """收一轮复核裁决：返回（给报告看的意见行，可回灌主笔的修订指令）。

        **只认结构化裁决**：复核者按验收标准逐条判定，:func:`derive_review`
        推导通过与否。因此"模型换一种说法描述问题"不再影响裁决——这是把角色内
        复核从子串匹配（``"未发现问题"``）里救出来的关键，也是与 G2 门禁对齐的地方。

        复核本身失败（格式错误 / 调用异常）只记 ``agent_degraded`` 并跳过：
        **不**吞掉主笔成果，也**不**触发修订——拿不到裁决不等于"有问题"。

        ``round_no``（从 1 起）会传给 mock 回放作为 ``attempt``，让夹具能表达
        "第 1 轮不满足、修订后第 2 轮满足"这种收敛路径。
        """
        lines: list[str] = []
        actionable: list[str] = []
        for reviewer in [m for m in self.members if m.duty is Duty.REVIEW]:
            try:
                outcome = await self._review_once(reviewer, task, result, round_no)
            except Exception as exc:  # noqa: BLE001 —— 复核失败不得吞掉主笔成果
                self._ledger.emit(
                    "agent_degraded", task_id=task.id, role=self.spec.key,
                    profile=reviewer.profile, duty="review",
                    reason=f"{type(exc).__name__}: {exc}")
                lines.append(f"[{reviewer.profile}] 复核不可用：{exc}")
                continue
            self._ledger.emit(
                "agent_review", task_id=task.id, role=self.spec.key,
                profile=reviewer.profile, approved=outcome.approved,
                issues=len(outcome.issues), chars=len(outcome.summary),
                # 只落条数的话，"驳回"在事后无法复盘：知道有 2 条不满足、却不知道
                # 是哪 2 条。截断到 400 字：够看清问题，又不至于让 ledger 爆掉。
                detail=outcome.summary[:400])
            lines.append(f"[{reviewer.profile}] {outcome.summary}")
            if not outcome.approved:
                actionable.extend(outcome.hints)
        return lines, actionable

    async def _review_once(
        self, reviewer: ResolvedAgent, task: Task, result: TaskResult,
        round_no: int = 1,
    ) -> _ReviewOutcome:
        """让一个复核执行体对产物做一次**逐条判定**，返回结构化结论。

        裁决不采信"复核者说有/没有问题"，而由 ``derive_review`` 从"哪几条验收
        标准不满足"推导；覆盖率不足或判不满足却不给依据（``is_malformed``）视为
        **复核器故障**并抛错，由调用方记为 ``agent_degraded``——绝不把一次坏输出
        当成"驳回"，那会让主笔照着空气返工。
        """
        material = self._artifact_excerpt(result)
        criteria = "\n".join(
            f"{i}. {c}" for i, c in enumerate(task.acceptance_criteria))
        messages: list[Message] = [
            {"role": "system", "content": (
                f"你是{self.spec.title}的复核者（{reviewer.profile}）。"
                "只做一件事：按编号逐条判定下方验收标准是否被产物满足。"
                "必须判定每一条、且只判定这些条（编号从 0 起，不多不少）。"
                "satisfied=false 时，evidence 写成「产物里缺什么、要改成什么」，"
                "它会直接成为主笔的返工指令。不要重写产物，不要给鼓励性评语；"
                "「还能更好」请写进 suggestions，不要因此判为不满足。")},
            {"role": "user", "content": (
                f"## 任务\n{task.title}\n{task.description}\n\n"
                f"## 验收标准（编号从 0 起）\n{criteria}\n\n"
                f"## 产物节选\n{material}")},
        ]
        verdict = await self._call_structured(
            reviewer, task, messages, ReviewVerdict,
            temperature=0.1, max_tokens=800, review_round=round_no)
        if is_malformed(verdict, task):
            raise AgentExecutionError(
                f"复核裁决不可用：{len(verdict.judgements)} 条判定，"
                f"应为 {len(task.acceptance_criteria)} 条且每条都带依据",
                task_id=task.id, role=self.spec.key)
        approved, issues, hints = derive_review(verdict, task)
        return _ReviewOutcome(
            profile=reviewer.profile, approved=approved, issues=issues, hints=hints,
            suggestions=[s.strip() for s in verdict.suggestions if s.strip()])


    @contextmanager
    def _as(self, member: ResolvedAgent, task: Task) -> Iterator[None]:
        """以某个执行体的身份绑定**追踪**与**预算**两套上下文。

        复核/裁判不经过 ``BaseAgent.execute``，这两件事都不会自动发生：

        1. 追踪（``TraceContext``）——否则 ``llm_call`` 没有 ``profile``，
           复核开销在 ``/stats`` 的 ``by_profile`` 里无处可归；
        2. 预算/用量（``agent_budget``）——否则 ``run.json`` 的 ``agent_costs``
           与运行报告会漏掉复核花费（曾出现"总调用 15 次、执行体只认 2 次"）。

        只绑 profile 不设上限：上限归 ``AgentProfile`` 管，复核调用沿用既有语义
        （此前同样不受执行体上限约束）。``finally`` 释放，异常不会污染后续调用。
        """
        trace_token = set_trace(TraceContext(
            task_id=task.id, role=self.spec.key, attempt=task.attempts,
            profile=member.profile))
        budget_token = bind_budget(member.profile, None, None)
        try:
            yield
        finally:
            release_budget(budget_token)
            reset_trace(trace_token)

    async def _call_as(
        self,
        member: ResolvedAgent,
        task: Task,
        messages: list[Message],
        *,
        temperature: float,
        max_tokens: int,
    ) -> CompletionResult:
        """以某个执行体的身份直连模型（自由文本，用于裁判这类开放式输出）。"""
        with self._as(member, task):
            return await self._models.call(
                member.alias, messages,
                temperature=temperature, max_tokens=max_tokens,
                purpose=_REVIEW_PURPOSE, role=self.spec.key)

    async def _call_structured(
        self,
        member: ResolvedAgent,
        task: Task,
        messages: list[Message],
        schema: type[ReviewVerdict],
        *,
        temperature: float,
        max_tokens: int,
        review_round: int = 1,
    ) -> ReviewVerdict:
        """以某个执行体的身份做**结构化**调用（复核裁决走这里）。

        必须走 ``ModelFactory.complete_structured`` 而不是 ``models/structured.py``
        的裸阶梯：后者只认 Provider，会绕过全局信号量与 ``llm_call`` 记账。
        mock 回放同样需要 ``set_purpose`` 才能命中 ``reviews.json`` 夹具；
        ``review_round`` 作为回放的 ``attempt`` 传入，夹具可用 ``"T-1@2"`` 表达
        "修订后第二轮"的裁决。
        """
        with self._as(member, task):
            provider = self._models.get(member.alias)
            if isinstance(provider, MockProvider):
                set_purpose(provider, _REVIEW_PURPOSE, role=self.spec.key,
                            task_id=task.id, attempt=review_round)
            result = await self._models.complete_structured(
                member.alias, messages, schema,
                temperature=temperature, max_tokens=max_tokens,
                purpose=_REVIEW_PURPOSE, role=self.spec.key)
        return cast(ReviewVerdict, result)

    def _artifact_excerpt(self, result: TaskResult) -> str:
        """复核材料：本任务**实际写过**的文件（不止契约声明的交付物）。

        预算按**文件数均分**，而不是"先到先得"：后者会让前面的大文件吃掉全部
        额度，后面的文件连"存在"都体现不出来。实测后果很荒谬——README 明明写了
        （ledger 里 4 次 ``artifact_written``），复核却因为没看到而判"未提供
        README"，且**这条意见靠修订永远修不掉**（文件一直在，只是没被展示）。

        每个文件至少给一个文件名 + 开头若干字符；超出均分额度的部分标注截断。
        """
        records = list(result.artifacts)
        declared = {record.path for record in records}
        try:
            written = self._store.by_task(result.task_id)
        except Exception:  # noqa: BLE001 —— 读清单失败不该让复核整段失败
            written = []
        latest: dict[str, ArtifactRecord] = {}
        for record in written:
            latest[record.path] = record  # 后写的覆盖先写的
        records.extend(
            record for path, record in latest.items() if path not in declared)
        if not records:
            return "（无产物）"

        # 均分预算，但给每份保底 180 token：够看清目录结构与接口签名。
        share = max(180, _EXCERPT_TOKENS // len(records))
        blocks: list[str] = []
        for record in records:
            try:
                text = self._store.read(record.path)
            except OSError:
                blocks.append(f"### {record.path}\n（读取失败）")
                continue
            if estimate_tokens(text) > share:
                # 3 字符/token 的粗估与既有实现一致，宁可多截也不要超预算
                text = text[: share * 3] + "\n…（超出本节选预算，已截断）"
            blocks.append(f"### {record.path}\n{text}")
        return "\n\n".join(blocks)

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
        primaries = self._primary_pool()
        if not primaries:
            raise AgentExecutionError(
                f"角色 {self.spec.key} 没有 primary agent", task_id=task.id, role=self.spec.key)
        results = await asyncio.gather(*[
            self._run_member(
                member, self._shadow_task(task, member.profile), pack, fix_hints)
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
        # 裁判同样直连模型，走与复核同一条「带 profile 记账」的通道。
        judge_member = next(
            (m for m in self.members if m.profile == judge_ref), None)
        if judge_member is None:
            raise BindingError(f"角色 {self.spec.key} 没有 profile {judge_ref}")
        completion = await self._call_as(
            judge_member, task, messages, temperature=0.0, max_tokens=60)
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
            duration_ms=int(seconds_between(started, finished) * 1000))

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
            duration_ms=int(seconds_between(started, finished) * 1000))
