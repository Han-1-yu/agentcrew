"""G3 门禁编排：三层顺序执行、短路，并独占门禁裁决引发的状态跃迁（spec §10、§6.2）。

``Gatekeeper.evaluate`` 是任务离开 ``REVIEW`` 的唯一入口。三层里 G1（静态校验，
零 token）与 G2（LLM 评审）由本模块顺序执行，G3（人工闸门）拆成两个卡点方法，
供调度器在计划完成与交付封版时调用：

- **G1 驳回短路 G2**。LLM 评审要花钱，且**永远救不回**结构性损坏的产物 ——
  缺文件、语法错误不是判断题。G1 的 ``reject`` 直接进入驳回路径，一次模型调用
  都不发。
- **状态决策是本模块的核心职责**。三个出口：通过 → ``SUCCEEDED``；驳回且
  ``rework_count < max_reworks`` → ``REWORK``；驳回且额度用尽 → ``FAILED``。
  本模块**只**把任务移出 ``REVIEW``：``READY → BLOCKED``、失败传播都是调度器
  （Task 28）与 ``TaskBoard.refresh`` 的业务。
- **``REVIEW → REWORK`` 必须配对 ``board.bump_rework``**（Task 8 的
  carry-forward）：``transition`` 不隐式自增任何计数器，而 ``REWORK → RUNNING``
  又不消耗 ``attempts``（spec §6.2）。漏掉这次自增，``max_reworks`` 永不生效，
  返工回路无界 —— 不是少记一个数，是死循环。
- **``fix_hints`` 活到两个地方**：``GateDecision.fix_hints``（返工方据此修复）
  与 ``ContextBus.record_rework_decision``（spec §11.3 的全局决策，供下游 Agent
  的上下文包引用）。后者要求任务 ``rework_count >= 1``，因此自增**先于**记录 ——
  顺序颠倒会把本轮的返工意见静默丢掉。
- **人工闸门由 gate 模式开关**：``none`` 直接返回放行裁决且**不触碰 prompter**
  （``NullPrompter`` 是零 IO 的全自动实现，但真正抑制提示的是模式判断）；
  ``plan`` / ``delivery`` 各只开一个卡点，``both`` 两个都开。**开了卡点却没传
  prompter 是构造期错误**（``__init__`` 抛 ``ValueError``）：人工闸门静默降级为
  自动放行比没有闸门更危险 —— 操作者会照着「有人工卡点」去规划，实际却一次都
  没问过。显式传入 ``NullPrompter`` 仍然合法：那是「我知道自己在跑全自动」的
  明示选择，被拒的只有「忘了传」。

每条门禁裁决都落两处账：``board.record_review``（任务历史，每层都记）与**每层
恰好一条** ``gate_result`` 审计事件（spec §14.1 的 ``layer``/``verdict``/``score``/
``issues``）。事件按层归属：G2 的 ``gate_result`` 由 ``Reviewer`` 自己发（裁决、
分数、评审人身份都在它手里），因此本模块**只为 G1 发** —— 两层各一条、不重不漏。
谁都不许重复记账：Task 30 的运行报告按这些事件统计各层驳回数（G1 拦截 N /
G2 驳回 M），重复一条就是报告里的错数。状态跃迁的 ``reason`` 一律取
``TaskStatus.value``：``str`` 混入枚举在 f-string 里会渲染成 ``TaskStatus.REWORK``。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import GateMode
from agentcrew.context.context_bus import ContextBus
from agentcrew.gate.reviewer import Reviewer, normalize_issue_text
from agentcrew.gate.static_checks import (
    ExtendedCheckResult,
    run_extended_checks,
    run_static,
)
from agentcrew.runtime.checkpoint import RunRecord
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import ReviewRecord, Task, TaskStatus
from agentcrew.task.task_board import TaskBoard

#: 开启计划卡点的 gate 模式（spec §10.3 卡点一）。
_PLAN_GATES: frozenset[str] = frozenset({"plan", "both"})

#: 开启交付卡点的 gate 模式（spec §10.3 卡点二）。
_DELIVERY_GATES: frozenset[str] = frozenset({"delivery", "both"})


class GateDecision(BaseModel):
    """一次 :meth:`Gatekeeper.evaluate` 的裁决。

    ``record`` 是作出裁决的那一层（短路时为 G1）；``next_status`` 是**已落到
    板上**的目标状态，不是建议；``fix_hints`` 是各层提示的并集，返工方据此修复。
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["approve", "reject"]
    record: ReviewRecord
    next_status: TaskStatus
    fix_hints: list[str] = Field(default_factory=list)


class PlanDecision(BaseModel):
    """计划卡点的人工裁决（spec §10.3 卡点一）。

    ``edit`` 携带编辑后的任务列表；``save_exit`` / ``cancel`` 时 ``tasks`` 无意义。
    ``incremental_edits``（P2-7）记录增量编辑子菜单里发生的每一次 add/remove/
    modify（全量编辑器为空列表），Crew 据此落 ``plan_incremental_edit`` 审计事件；
    新字段有默认值，老的裁决构造方式不受影响。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "edit", "save_exit", "cancel"]
    tasks: list[Task] | None = None
    incremental_edits: list[dict[str, Any]] = Field(default_factory=list)


class DeliveryDecision(BaseModel):
    """交付卡点的人工裁决（spec §10.3 卡点二）。

    ``rework`` 时 ``task_ids`` 指出被打回重做的任务；``accept`` / ``abort`` 时为空。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "rework", "abort"]
    task_ids: list[str] = Field(default_factory=list)


class Prompter(Protocol):
    """人工闸门的询问接口（CLI 与全自动实现各自满足它，spec §10.3）。"""

    async def ask_plan(self, tasks: Sequence[Task]) -> PlanDecision:
        """询问是否批准（可能是编辑后的）任务拆解。"""
        ...

    async def ask_delivery(self, record: RunRecord | None) -> DeliveryDecision:
        """询问是否接受本次交付。"""
        ...


class NullPrompter:
    """全自动放行：不读输入、不打印、不做任何 IO（测试与 ``--auto`` 用）。"""

    async def ask_plan(self, tasks: Sequence[Task]) -> PlanDecision:
        """放行原拆解（``edit`` / ``save_exit`` / ``cancel`` 都需要真人与 IO）。"""
        return PlanDecision(action="approve", tasks=list(tasks))

    async def ask_delivery(self, record: RunRecord | None) -> DeliveryDecision:
        """接受交付。"""
        return DeliveryDecision(action="accept")


class Gatekeeper:
    """三层门禁的编排者，也是门禁裁决引发的状态跃迁的唯一执行者。"""

    def __init__(
        self,
        reviewer: Reviewer,
        store: ArtifactStore,
        board: TaskBoard,
        *,
        gate: GateMode,
        ledger: Ledger,
        prompter: Prompter | None = None,
        context_bus: ContextBus | None = None,
        review_score_threshold: float | None = None,
        convergence_threshold: int | None = None,
        gate_config: dict[str, Any] | None = None,
    ) -> None:
        if prompter is None and (gate in _PLAN_GATES or gate in _DELIVERY_GATES):
            raise ValueError(
                f"gate={gate!r} 包含人工卡点，但没有传入 prompter："
                "人工闸门不得静默降级为自动放行；请传入 Prompter，"
                "或改用 gate=\"none\"（全自动路径）"
            )
        self._reviewer = reviewer
        self._store = store
        self._board = board
        self._gate = gate
        self._ledger = ledger
        self._prompter = prompter
        self._context_bus = context_bus
        # P2-5：门禁规则从 gate_config 读取，缺省回落构造参数 / 硬编码默认值；
        # gate_config=None 时行为与原来完全一致。
        cfg = dict(gate_config or {})
        self._g1_enabled: bool = cfg.get("g1_enabled", True)
        self._g2_enabled: bool = cfg.get("g2_enabled", True)
        self._g3_enabled: bool = cfg.get("g3_enabled", True)
        self._max_review_attempts: int = cfg.get("max_review_attempts", 3)
        # gate_config 中的阈值显式覆盖构造参数（构造参数本身即旧默认值）。
        self._review_score_threshold = cfg.get(
            "review_score_threshold", review_score_threshold)
        self._convergence_threshold = cfg.get(
            "convergence_threshold", convergence_threshold)
        # g2_reviewers 限制并行评审人数：写到 Reviewer 上，review() 截取前 N 个。
        g2_reviewers = cfg.get("g2_reviewers")
        if g2_reviewers is not None:
            reviewer.max_reviewers = g2_reviewers
        # task_id → 历次 LLM 驳回的规范化 issue 集合指纹（跨返工轮次的内存追踪）。
        self._convergence_history: dict[str, list[frozenset[str]]] = {}

    # ---- 自动门禁 ----

    async def evaluate(self, task: Task, *, round_no: int) -> GateDecision:
        """跑 G1（必要时 G2），落账、驱动状态跃迁并返回裁决。

        ``task`` 只用来定位任务：计数与状态一律以 ``board.get(task.id)`` 的存活
        对象为准 —— 调用方手里的 ``Task`` 可能是早先跃迁留下的旧副本，其
        ``rework_count`` 停在自增之前，据此判断额度会永远判不出去。
        """
        task_id = task.id
        if self._g1_enabled:
            static = run_static(self._board.get(task_id), self._store, round_no=round_no)
            # P2-4：扩展代码可执行性校验 —— 在既有契约检查之后跑，error 并入 G1 issues
            # （阻断），warning 只记 ledger。未产出的路径不读（缺失已由 run_static 报）。
            ext = self._run_extended_checks(self._board.get(task_id))
            self._ledger.emit(
                "g1_extended_check",
                task_id=task_id,
                round=round_no,
                errors=list(ext.errors),
                warnings=list(ext.warnings),
            )
            if ext.errors:
                static = static.model_copy(update={
                    "verdict": "reject",
                    "issues": static.issues + list(ext.errors),
                    "fix_hints": static.fix_hints + [
                        f"修正 {issue}" for issue in ext.errors],
                })
        else:
            # P2-5：g1_enabled=False → 跳过静态校验，记一条「已停用」的放行记录。
            static = ReviewRecord(
                task_id=task_id, round=round_no, layer="static",
                verdict="approve", score=None, issues=[], fix_hints=[],
                reviewer="static:disabled", created_at=datetime.now())
        self._board.record_review(task_id, static)
        self._emit(static)          # G2 的 gate_result 由 Reviewer 自己发
        records = [static]
        if static.verdict == "approve" and self._g2_enabled:
            llm = await self._reviewer.review(
                self._board.get(task_id), round_no=round_no)
            self._board.record_review(task_id, llm)
            records.append(llm)

        primary = records[-1]
        fix_hints = _dedup(hint for record in records for hint in record.fix_hints)

        # P0-1: score 阈值旁路 —— LLM 评审 reject 但分数够高时改为放行。
        # 仅对 LLM 层生效（score 仅该层有值）；G1 静态校验的 score 恒为 None，
        # 不会被误旁路。旁路后记一条补充 gate_result：verdict 标 approve、issues
        # 原样保留（审计可见被绕过的驳回理由）、reviewer 追加 :score-bypass。
        if (self._review_score_threshold is not None
                and primary.verdict == "reject"
                and primary.score is not None
                and primary.score >= self._review_score_threshold):
            bypassed_reviewer = f"{primary.reviewer}:score-bypass"
            self._ledger.emit(
                "gate_result",
                task_id=primary.task_id,
                round=primary.round,
                layer=primary.layer,
                verdict="approve",
                score=primary.score,
                issues=primary.issues,
                reviewer=bypassed_reviewer,
            )
            primary = primary.model_copy(update={
                "verdict": "approve",
                "reviewer": bypassed_reviewer,
            })

        # P2-3: 收敛旁路 —— 与 score 阈值旁路同构，只对 LLM 层生效。连续
        # ``convergence_threshold`` 轮驳回意见完全相同，说明返工再多次也只会撞
        # 同一堵墙（产物已无法朝评审意见改进），改为放行。G1 静态校验的结构性
        # 损坏（缺文件 / 语法错）不会「收敛」，不参与此判定。
        if primary.verdict == "reject" and primary.layer == "llm":
            converged, rounds = self._track_convergence(task_id, primary.issues)
            if converged:
                self._ledger.emit(
                    "review_converged",
                    task_id=task_id,
                    round=primary.round,
                    rounds=rounds,
                    issues=primary.issues,
                )
                converged_reviewer = f"{primary.reviewer}:converged"
                self._ledger.emit(
                    "gate_result",
                    task_id=primary.task_id,
                    round=primary.round,
                    layer=primary.layer,
                    verdict="approve",
                    score=primary.score,
                    issues=primary.issues,
                    reviewer=converged_reviewer,
                )
                primary = primary.model_copy(update={
                    "verdict": "approve",
                    "reviewer": converged_reviewer,
                })

        if primary.verdict == "approve":
            next_status = self._to_succeeded(task_id)
        else:
            next_status = self._reject(self._board.get(task_id), fix_hints)
        return GateDecision(
            verdict=primary.verdict,
            record=primary,
            next_status=next_status,
            fix_hints=fix_hints,
        )

    # ---- 收敛检测（P2-3）----

    def check_convergence(self, task_id: str, issues: list[str]) -> bool:
        """记录本轮 LLM 驳回意见并检测是否连续 N 轮不变（P2-3）。

        把规范化后的 issue 集合指纹追加进 ``task_id`` 的历史；末尾连续
        ``convergence_threshold`` 轮指纹完全相同时判收敛。``threshold`` 为
        0 / None 时不启用（直接返回 ``False``，也不留痕）。
        """
        converged, _ = self._track_convergence(task_id, issues)
        return converged

    def _track_convergence(
        self, task_id: str, issues: list[str]
    ) -> tuple[bool, int]:
        """``check_convergence`` 的记账版：返回 ``(是否收敛, 连续相同轮数)``。"""
        threshold = self._convergence_threshold
        if not threshold or threshold <= 0:
            return False, 0
        fingerprint = _issue_fingerprint(issues)
        history = self._convergence_history.setdefault(task_id, [])
        history.append(fingerprint)
        consecutive = 1
        for prev in reversed(history[:-1]):
            if prev == fingerprint:
                consecutive += 1
            else:
                break
        return consecutive >= threshold, consecutive

    def _run_extended_checks(self, task: Task) -> ExtendedCheckResult:
        """对本任务已落盘产物跑 P2-4 扩展检查，按各自 kind 分组调用。

        未产出的路径不读（缺失已由 ``run_static`` 报出）；只对 ``kind="python"``
        做语法 / 危险导入检查，其余 kind 只做非空检查。
        """
        produced = {record.path for record in self._store.by_task(task.id)}
        by_kind: dict[str, list[tuple[str, str]]] = {}
        for spec in task.deliverables:
            if spec.path not in produced:
                continue
            by_kind.setdefault(spec.kind, []).append(
                (spec.path, self._store.read(spec.path)))
        result = ExtendedCheckResult()
        for kind, artifacts in by_kind.items():
            partial = run_extended_checks(artifacts, kind)
            result.errors.extend(partial.errors)
            result.warnings.extend(partial.warnings)
        return result

    # ---- 人工闸门（G3）----

    async def human_plan_gate(self, tasks: Sequence[Task]) -> PlanDecision:
        """计划卡点：``gate`` 开启时询问 prompter，否则放行原拆解。

        「``gate`` 开启而 prompter 为空」在构造期已被 ``ValueError`` 挡掉，
        这里的 ``None`` 判断只是类型收窄，不是运行期的降级路径。
        """
        planned = list(tasks)
        if self._gate in _PLAN_GATES and self._prompter is not None:
            return await self._prompter.ask_plan(planned)
        return PlanDecision(action="approve", tasks=planned)

    async def human_delivery_gate(self, record: RunRecord | None) -> DeliveryDecision:
        """交付卡点：``gate`` 开启时询问 prompter，否则直接接受。

        同 :meth:`human_plan_gate`：``prompter=None`` + 开关开启的组合不可达。
        """
        if self._gate in _DELIVERY_GATES and self._prompter is not None:
            return await self._prompter.ask_delivery(record)
        return DeliveryDecision(action="accept")

    # ---- 状态决策 ----

    def _emit(self, record: ReviewRecord) -> None:
        """落一条 ``gate_result`` 审计事件（spec §14.1 的字段 + 定位字段）。

        只有本模块独占的层（G1）走这里：G2 的事件由 ``Reviewer`` 自持的裁决、
        分数与评审人身份直接发出。两层各恰好一条 —— 重复记账会让按事件统计的
        各层驳回数翻倍（Task 30 报告），漏记则让 G1 拦截数凭空消失。
        """
        self._ledger.emit(
            "gate_result",
            task_id=record.task_id,
            round=record.round,
            layer=record.layer,
            verdict=record.verdict,
            score=record.score,
            issues=record.issues,
            reviewer=record.reviewer,
        )

    def _to_succeeded(self, task_id: str) -> TaskStatus:
        """``REVIEW → SUCCEEDED``（全部门禁通过）。"""
        return self._transition(task_id, TaskStatus.SUCCEEDED)

    def _reject(self, task: Task, fix_hints: list[str]) -> TaskStatus:
        """驳回路径：还有返工额度就返工，额度用尽则失败。"""
        if task.rework_count < task.max_reworks:
            return self._to_rework(task.id, fix_hints)
        return self._transition(task.id, TaskStatus.FAILED)

    def _to_rework(self, task_id: str, fix_hints: list[str]) -> TaskStatus:
        """``REVIEW → REWORK``，并强制配对额度自增与返工决策记录。

        顺序不可换：``record_rework_decision`` 要求 ``rework_count >= 1``，在
        自增之前调用会把本轮的返工意见静默丢掉。
        """
        self._board.transition(
            task_id, TaskStatus.REWORK, reason=_reason(TaskStatus.REWORK))
        bumped = self._board.bump_rework(task_id)
        if self._context_bus is not None:
            self._context_bus.record_rework_decision(bumped, fix_hints)
        return TaskStatus.REWORK

    def _transition(self, task_id: str, to: TaskStatus) -> TaskStatus:
        """经状态机跃迁并返回目标状态；``reason`` 取 ``.value``，不取枚举。"""
        self._board.transition(task_id, to, reason=_reason(to))
        return to


def _reason(to: TaskStatus) -> str:
    """门禁裁决引发的跃迁原因（如 ``gate-rework``）；取 ``.value`` 而非枚举。"""
    return f"gate-{to.value}"


def _dedup(items: Iterable[str]) -> list[str]:
    """去重保序：跨层 ``fix_hints`` 的并集语义，与 ``Reviewer._dedup`` 同义。"""
    return list(dict.fromkeys(items))


def _issue_fingerprint(issues: list[str]) -> frozenset[str]:
    """一轮驳回意见的收敛指纹：规范化后的 issue 集合（顺序无关、空意见剔除）。

    与 :func:`agentcrew.gate.reviewer.normalize_issue_text` 共用同一规范化，
    保证「同一批意见」在不同轮次给出同一指纹。空 issue（approve）不进历史。
    """
    return frozenset(
        normalized
        for issue in issues
        if (normalized := normalize_issue_text(issue))
    )
