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
  ``plan`` / ``delivery`` 各只开一个卡点，``both`` 两个都开。

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
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import GateMode
from agentcrew.context.context_bus import ContextBus
from agentcrew.gate.reviewer import Reviewer
from agentcrew.gate.static_checks import run_static
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
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "edit", "save_exit", "cancel"]
    tasks: list[Task] | None = None


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
    ) -> None:
        self._reviewer = reviewer
        self._store = store
        self._board = board
        self._gate = gate
        self._ledger = ledger
        self._prompter = prompter
        self._context_bus = context_bus

    # ---- 自动门禁 ----

    async def evaluate(self, task: Task, *, round_no: int) -> GateDecision:
        """跑 G1（必要时 G2），落账、驱动状态跃迁并返回裁决。

        ``task`` 只用来定位任务：计数与状态一律以 ``board.get(task.id)`` 的存活
        对象为准 —— 调用方手里的 ``Task`` 可能是早先跃迁留下的旧副本，其
        ``rework_count`` 停在自增之前，据此判断额度会永远判不出去。
        """
        task_id = task.id
        static = run_static(self._board.get(task_id), self._store, round_no=round_no)
        self._board.record_review(task_id, static)
        self._emit(static)          # G2 的 gate_result 由 Reviewer 自己发
        records = [static]
        if static.verdict == "approve":
            llm = await self._reviewer.review(
                self._board.get(task_id), round_no=round_no)
            self._board.record_review(task_id, llm)
            records.append(llm)

        primary = records[-1]
        fix_hints = _dedup(hint for record in records for hint in record.fix_hints)
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

    # ---- 人工闸门（G3）----

    async def human_plan_gate(self, tasks: Sequence[Task]) -> PlanDecision:
        """计划卡点：``gate`` 开启且确有 prompter 时询问，否则放行原拆解。"""
        planned = list(tasks)
        if self._gate in _PLAN_GATES and self._prompter is not None:
            return await self._prompter.ask_plan(planned)
        return PlanDecision(action="approve", tasks=planned)

    async def human_delivery_gate(self, record: RunRecord | None) -> DeliveryDecision:
        """交付卡点：``gate`` 开启且确有 prompter 时询问，否则直接接受。"""
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
