"""``DeferredPrompter``：把 G3 人工卡点换成可跨进程恢复的审批请求（R4）。

设计规格 §19.2 早就写明「G3 已抽象为『询问 → 决策』，换 UI 实现即可」，但
此前只有一个同步阻塞的 ``CliPrompter``：``await`` 一次就必须拿到答案，run 无法
把决策权交出去。本模块实现同一个 :class:`~agentcrew.gate.gatekeeper.Prompter`
Protocol，但把「问」改成三步：

1. 卡点上没有未消费的审批单 → 创建 ``pending`` 审批单，抛
   :class:`~agentcrew.runtime.approvals.ApprovalPending`；``Crew`` 捕获后把 run
   落成 ``awaiting_approval`` 并**正常退出**（不是失败、不是取消）。
2. 人在另一个进程里通过 CLI / HTTP API / Studio 写 ``decide``。
3. ``resume`` 时引擎回到同一卡点：看到 ``decided`` → ``consume`` 并转成
   ``PlanDecision`` / ``DeliveryDecision`` 继续执行；仍 ``pending`` → 复用同一张
   审批单再次挂起（不重复建单、不重复烧 token）。

``CliPrompter`` / ``NullPrompter`` 一行未改，默认 ``approval_mode=interactive``
时行为与 R4 之前逐字一致。``bind_approvals`` 由 ``Crew._start`` 在 run 目录
确定后调用（run_id 在 ``CheckpointStore.create`` 时才生成，构造期无从得知）。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentcrew.gate.gatekeeper import DeliveryDecision, PlanDecision
from agentcrew.runtime.approvals import (
    ApprovalError,
    ApprovalPending,
    ApprovalStore,
)
from agentcrew.runtime.checkpoint import RunRecord
from agentcrew.task.task import Task


class DeferredPrompter:
    """满足 ``Prompter`` Protocol：不读 stdin，只读写审批单。"""

    def __init__(self, run_dir: Path | None = None, *,
                 timeout_seconds: int = 0) -> None:
        self._store: ApprovalStore | None = (
            ApprovalStore(run_dir) if run_dir is not None else None
        )
        #: R30：审批单等待窗口（0 = 永不超时，与 R30 之前一致）。
        self.timeout_seconds = int(timeout_seconds or 0)

    def bind_approvals(self, run_dir: Path) -> None:
        """由 ``Crew`` 在 run 目录就绪后注入；也可库调用方直接传 run_dir 构造。"""
        self._store = ApprovalStore(run_dir)

    @property
    def store(self) -> ApprovalStore:
        if self._store is None:
            raise ApprovalError(
                "DeferredPrompter 尚未绑定 run 目录；请由 Crew._start 注入"
            )
        return self._store

    # ---- Prompter Protocol ----

    async def ask_plan(self, tasks: Sequence[Task]) -> PlanDecision:
        opened = self.store.open_for("plan")
        if opened is None:
            request = self.store.create(
                "plan",
                context={
                    "task_ids": [task.id for task in tasks],
                    "task_count": len(tasks),
                    "titles": [task.title for task in tasks][:20],
                },
                timeout_seconds=self.timeout_seconds,
            )
            raise ApprovalPending(request)
        if opened.status == "pending":
            raise ApprovalPending(opened)
        self.store.consume(opened.id)
        return self._plan_decision(opened.decision, tasks)

    async def ask_delivery(self, record: RunRecord | None) -> DeliveryDecision:
        opened = self.store.open_for("delivery")
        if opened is None:
            request = self.store.create(
                "delivery",
                context={
                    "project_goal": (record.project_goal if record else "")[:200],
                    "stats": (record.stats.model_dump(mode="json") if record else {}),
                },
                timeout_seconds=self.timeout_seconds,
            )
            raise ApprovalPending(request)
        if opened.status == "pending":
            raise ApprovalPending(opened)
        self.store.consume(opened.id)
        return self._delivery_decision(opened.decision)

    # ---- 裁决载荷 → 既有决策对象 ----

    @staticmethod
    def _plan_decision(
        payload: dict[str, Any], tasks: Sequence[Task]
    ) -> PlanDecision:
        action = payload.get("action")
        if action == "approve":
            return PlanDecision(action="approve", tasks=list(tasks))
        if action == "cancel":
            return PlanDecision(action="cancel")
        if action == "save_exit":
            return PlanDecision(action="save_exit")
        raise ApprovalError(f"plan 卡点不支持的裁决：{action!r}")

    @staticmethod
    def _delivery_decision(payload: dict[str, Any]) -> DeliveryDecision:
        action = payload.get("action")
        if action == "accept":
            return DeliveryDecision(action="accept")
        if action == "abort":
            return DeliveryDecision(action="abort")
        if action == "rework":
            return DeliveryDecision(
                action="rework",
                task_ids=[str(item) for item in payload["task_ids"]],
            )
        raise ApprovalError(f"delivery 卡点不支持的裁决：{action!r}")
