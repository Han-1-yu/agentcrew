"""``StepGate``：单步断点（R20）——``debug_step`` 开启时每个任务派发前挂起。

R4 的 ``DeferredPrompter`` 把 plan/delivery 卡点换成了「建单 → 跨进程裁决 →
resume 消费」；本模块把同一套纪律搬到**任务粒度**：调度器每次要派发新任务
之前先过这道门，门内没有已放行的候选就建一张 ``step`` 审批单并抛
:class:`~agentcrew.runtime.approvals.ApprovalPending`——``Crew`` 捕获后把 run
落成 ``awaiting_approval``，人用 ``approve --decision step|run|abort`` 裁决，
``resume`` 后从未放行的下一个任务继续。

三条纪律（与 DeferredPrompter 逐条对应）：

1. **不重复建单**：该卡点上已有 ``pending`` 审批单时复用同一张再次挂起
   （用户还没裁决就 ``resume`` 是普通操作，不是新断点）。
2. **裁决必须被消费才生效**：decided 未消费的单由 ``Crew`` 的 resume 路径
   统一消费（:meth:`Crew._apply_step_decision`——``abort`` 要在那里转成
   ``CANCELLED``，gate 只管「暂停还是放行」），消费后的任务 id 进
   ``_consumed``，此后派发不再为它停留。
3. **默认关闭时零参与**：``debug_step`` 未开启时 ``Crew`` 根本不构造本类，
   调度器拿到的 ``step_gate=None``，执行路径与 R20 之前逐字一致。

并发语义：门只在**没有在跑 worker** 时才可能停留（该条件由调度器侧保证，
在跑 worker 未收尾就挂起会留下 RUNNING 孤儿）。因此 ``max_concurrency > 1``
时表现为「攒批」——已放行的任务凑够一批一起派发，每个新任务派发前仍各停
一次。这是刻意的：断点的单位是「任务的派发权」，不是「时间片」。
"""

from __future__ import annotations

from collections.abc import Sequence

from agentcrew.runtime.approvals import ApprovalPending, ApprovalStore
from agentcrew.task.task import Task


class StepGate:
    """调度器派发前的断点门：无未放行候选 → 放行；否则建单挂起。"""

    def __init__(self, store: ApprovalStore, consumed: frozenset[str]) -> None:
        self._store = store
        #: 已放行的任务 id（会话开始时从 ``ApprovalStore.consumed_steps()``
        #: 快照；本会话内挂起即结束，不存在快照过期问题）。
        self._consumed = consumed

    def __call__(self, candidates: Sequence[Task]) -> None:
        """调度器主循环在 ``take_ready`` 之前调用；停留时抛 ``ApprovalPending``。

        ``candidates`` 是 ``TaskBoard.peek_ready`` 的只读候选（调度器传整个
        批次，本门自己挑第一个未放行的——放行权按派发顺序逐个授予）。
        """
        opened = self._store.open_for("step")
        if opened is not None and opened.status == "pending":
            # 纪律 1：未裁决的旧单直接复用，不重复建单。
            raise ApprovalPending(opened)
        nxt = next(
            (task for task in candidates if task.id not in self._consumed), None
        )
        if nxt is None:
            return  # 批次内全部已放行：本轮派发不停留。
        request = self._store.create(
            "step",
            context={
                "task_id": nxt.id,
                "title": nxt.title,
                "role": nxt.role,
                "priority": nxt.priority,
                "attempt": nxt.attempts,
            },
        )
        raise ApprovalPending(request)
