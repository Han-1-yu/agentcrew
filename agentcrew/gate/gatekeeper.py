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
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.artifacts.spec import BINARY_KINDS
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import GateMode
from agentcrew.context.context_bus import ContextBus
from agentcrew.gate.execute_verify import ExecutionVerifier, run_exec_checks
from agentcrew.gate.reassign import ReassignPolicy
from agentcrew.gate.reviewer import Reviewer, normalize_issue_text
from agentcrew.gate.static_checks import (
    ExtendedCheckResult,
    run_extended_checks,
    run_static,
)
from agentcrew.gate.supervisor import SupervisorReviewer
from agentcrew.runtime.checkpoint import RunRecord
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import ReviewRecord, Task, TaskStatus
from agentcrew.task.task_board import TaskBoard
from agentcrew.utils.clock import utc_now

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
        supervisor: SupervisorReviewer | None = None,
        reassign: ReassignPolicy | None = None,
        exec_verifier: ExecutionVerifier | None = None,
    ) -> None:
        cfg = dict(gate_config or {})
        g3_enabled = bool(cfg.get("g3_enabled", True))
        # g3_enabled=False 是「显式关闭人工卡点」：此时 gate 模式虽含卡点，
        # 也不该再要求 prompter（否则配置形同虚设）。默认 True 时守卫原样生效。
        if (prompter is None and g3_enabled
                and (gate in _PLAN_GATES or gate in _DELIVERY_GATES)):
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
        # P2-5：门禁规则从 gate_config 读取（cfg 已在构造期守卫前解析），
        # 缺省回落构造参数 / 硬编码默认值；gate_config=None 时行为与原来完全一致。
        self._g1_enabled: bool = cfg.get("g1_enabled", True)
        self._g2_enabled: bool = cfg.get("g2_enabled", True)
        self._g3_enabled: bool = cfg.get("g3_enabled", True)
        # 该键此前只赋值、全仓无读取点（死配置）。按 config.py 的文档把它接成
        # 「全局返工/评审轮次上限」，在 _reject 里与任务自身 max_reworks 取较小值。
        # 默认 3 大于任务默认 max_reworks=2，因此旧行为逐字不变。
        try:
            self._max_review_attempts = max(0, int(cfg.get("max_review_attempts", 3)))
        except (TypeError, ValueError):
            self._max_review_attempts = 3
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
        #: R7：主管审核（谁派的活谁验收）。``None``（默认）= 不启用，行为与
        #: R7 之前逐字一致；Crew 在 ``supervisor_review_enabled`` 且非 mock 时注入。
        self._supervisor = supervisor
        #: R10：主管驳回后换人做的策略。``None`` = 不换人（默认），驳回仍退回原
        #: 执行者；没有 ``role_fallback`` 映射时即使注入也永不生效。
        self._reassign = reassign
        #: C3：G1 执行校验档。``None`` = 关闭（默认），行为与引入前逐字一致。
        #: 打开后只对「静态校验已放行」的任务跑它自己交付的测试，失败并入 G1
        #: issue 走既有返工通道——不新增第二条返工路径。
        self._exec_verifier = exec_verifier

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
            # C3：执行校验档（默认关，``gate_config.exec_verify=true`` 才注入）。
            # 只在结构已经放行时跑：缺文件 / 语法错的任务进容器也只是白烧一次
            # 启动时间，而这些问题的修复提示已经足够明确。
            exec_findings: list[tuple[str, str]] = []
            if (self._exec_verifier is not None
                    and static.verdict == "approve" and not ext.errors):
                exec_findings = await run_exec_checks(
                    self._exec_verifier, self._board.get(task_id),
                    self._store, round_no=round_no)
            if exec_findings:
                static = static.model_copy(update={
                    "verdict": "reject",
                    "issues": static.issues + [
                        issue for issue, _ in exec_findings],
                    "fix_hints": static.fix_hints + [
                        hint for _, hint in exec_findings],
                })
        else:
            # P2-5：g1_enabled=False → 跳过静态校验，记一条「已停用」的放行记录。
            static = ReviewRecord(
                task_id=task_id, round=round_no, layer="static",
                verdict="approve", score=None, issues=[], fix_hints=[],
                reviewer="static:disabled", created_at=utc_now())
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

        # R7：主管审核——只在 G1/G2 都放行、且这是动态委派任务时发生
        # （``SupervisorReviewer`` 自己判断 ``delegated_by``，非委派任务返回 None）。
        # 刻意放在 score / 收敛旁路**之后**：那两个旁路是对机器评审意见的容错，
        # 派活的人说不行就是不行，不该被分数或「意见收敛」绕过。
        if primary.verdict == "approve" and self._supervisor is not None:
            supervised = await self._supervisor.review(
                self._board.get(task_id), round_no=round_no)
            if supervised is not None:
                self._board.record_review(task_id, supervised)
                records.append(supervised)
                primary = supervised
                # 主管驳回时它的 fix_hints 必须一起进返工提示，否则下游只会
                # 拿到机器评审的旧意见，改不到点子上。
                fix_hints = _dedup(
                    hint for record in records for hint in record.fix_hints)

        if primary.verdict == "approve":
            next_status = self._to_succeeded(task_id)
        else:
            next_status = self._reject(
                self._board.get(task_id), fix_hints,
                layer=primary.layer, supervisor=primary.reviewer)
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
        by_kind: dict[str, list[tuple[Path | str, str]]] = {}
        for spec in task.deliverables:
            if spec.path not in produced:
                continue
            # 二进制产物没有文本可查：``store.read`` 会按 UTF-8 解码 PNG 并抛
            # ``UnicodeDecodeError``；尺寸判据已由 G1 的 ``run_static`` 覆盖。
            if spec.kind in BINARY_KINDS:
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
        if not self._g3_enabled:
            # g3_enabled=False：显式关闭人工卡点，放行原拆解且不触碰 prompter。
            return PlanDecision(action="approve", tasks=planned)
        if self._gate in _PLAN_GATES and self._prompter is not None:
            return await self._prompter.ask_plan(planned)
        return PlanDecision(action="approve", tasks=planned)

    async def human_delivery_gate(self, record: RunRecord | None) -> DeliveryDecision:
        """交付卡点：``gate`` 开启时询问 prompter，否则直接接受。

        同 :meth:`human_plan_gate`：``prompter=None`` + 开关开启的组合不可达。
        ``g3_enabled=False`` 时直接接受，不触碰 prompter。
        """
        if not self._g3_enabled:
            return DeliveryDecision(action="accept")
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

    def _reject(
        self, task: Task, fix_hints: list[str], *,
        layer: str = "", supervisor: str = "",
    ) -> TaskStatus:
        """驳回路径：还有返工额度就返工，额度用尽则失败。

        额度 = ``min(任务自带 max_reworks, gate_config.max_review_attempts)``：
        前者是单任务上限，后者是全局评审轮次上限。两者默认值 2 / 3，故未配置
        时与改造前完全一致；只有显式把全局上限调小才会提前收口。

        R10：主管驳回（``layer="supervisor"``）且配了备选角色时，先把活换给别人
        （``_maybe_reassign``）再返工——换人**也**消耗同一次返工额度，不是免费
        加次数；额度用尽照样 FAILED。
        """
        rework_limit = min(task.max_reworks, self._max_review_attempts)
        if task.rework_count < rework_limit:
            self._maybe_reassign(task, layer=layer, supervisor=supervisor)
            return self._to_rework(task.id, fix_hints)
        return self._transition(task.id, TaskStatus.FAILED)

    def _maybe_reassign(
        self, task: Task, *, layer: str, supervisor: str
    ) -> None:
        """R10：主管驳回且还有重派额度时，把执行角色换成备选角色。

        只改 ``task.role``、不改状态：随后照常走 ``REWORK``，下次派发自然找新
        角色。没有备选映射 / 额度用尽 / 替代就是主管本人时什么都不做——驳回仍
        退回原执行者，行为与 R10 之前一致。
        """
        if layer != "supervisor" or self._reassign is None:
            return
        replacement = self._reassign.pick(task, supervisor=supervisor)
        if replacement is None:
            return
        previous_role = task.role
        updated = self._board.reassign(task.id, replacement)
        self._ledger.emit(
            "supervisor_reassigned",
            task_id=task.id,
            from_role=previous_role,
            to_role=replacement,
            supervisor=supervisor,
            reassign_count=updated.reassign_count,
        )

    def _to_rework(self, task_id: str, fix_hints: list[str]) -> TaskStatus:
        """``REVIEW → REWORK``，并强制配对额度自增与返工决策记录。

        A-18：``bump_rework`` 先于 ``transition``。此前 ``transition`` 经
        ``on_change`` 立即把板落盘（此刻 ``rework_count`` 还是旧值），
        ``bump_rework`` 走 ``_replace`` 不触发持久化——进程恰在两者之间被杀，
        resume 读到 ``status=REWORK`` 但 ``rework_count`` 少 1，白送一次返工
        额度。自增在前保证落盘快照里的计数已经是新值。

        ``record_rework_decision`` 要求 ``rework_count >= 1``，因此在自增之后
        调用（本轮返工意见不会被丢）。
        """
        bumped = self._board.bump_rework(task_id)
        self._board.transition(
            task_id, TaskStatus.REWORK, reason=_reason(TaskStatus.REWORK))
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
