"""``TaskBoard``：任务状态的唯一事实源（spec §3.1 原则 3、§6、§7.3）。

项目原则「单一事实源」：任何模块都不得缓存任务状态副本，也不得直接写
``Task.status``——所有状态变更必须经 :meth:`TaskBoard.transition` 走状态机
校验。本类因此是状态的唯一写入点，并且：

- :meth:`transition` 校验跃迁后用 ``model_copy`` 生成**新** ``Task`` 替换旧
  对象，而不是原地修改：外部若仍持有旧引用，看到的仍是变更前的完整对象。
- :meth:`get` 交出板上存活的 ``Task`` 对象本身（不是副本）——:meth:`refresh`
  依赖的「人工修复上游状态」路径需要这一点。
- 唯一绕过状态机的入口是 :meth:`reset_failed`（``FAILED`` 无出边）：它显式
  记录 ``reason="manual-retry"`` 的 ``StatusChange``，让绕过在历史里可见。
- 本类不做任何 I/O：持久化由调用方通过 ``on_change`` 回调完成（每次状态跃迁
  后触发，供 Task 15 的 checkpoint 落盘 ``tasks.json``）。

额度语义（spec §6.2）：``attempts`` 由 :meth:`take_ready` 在派发 ``READY`` 任务时
扣除（``REWORK`` 重新派发**不**扣，返工额度记在 ``rework_count`` 上），
``rework_count`` 由调用方在 ``REVIEW → REWORK`` 之后调 :meth:`bump_rework`
扣除；:meth:`transition` 本身不会隐式自增任何一个计数器。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from agentcrew.task.graph import TaskGraph, ready_ids
from agentcrew.task.state_machine import assert_transition
from agentcrew.task.state_machine import is_terminal as is_terminal_status
from agentcrew.task.task import (
    ReviewRecord,
    RunStats,
    StatusChange,
    Task,
    TaskResult,
    TaskStatus,
)

#: 永久终止、会把传递下游一并拖死的上游状态（spec §7.3「失败传播」）。
_FAILED_UPSTREAM_STATES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class TaskBoard:
    """内存中的任务状态存储：状态跃迁入口、就绪派发与快照。

    构造后 ``tasks`` 的 ``dependencies`` 不再变化（依赖图只在 ``__init__``
    建一次）；状态与运行时字段则随 :meth:`transition` / ``bump_*`` 更新为
    新的 ``Task`` 对象。
    """

    def __init__(
        self,
        tasks: Sequence[Task],
        *,
        on_change: Callable[[Task, StatusChange], None] | None = None,
    ) -> None:
        """``tasks`` 的 id 必须唯一（由 Task 9 的 planner 校验）。"""
        self._tasks: dict[str, Task] = {task.id: task for task in tasks}
        self._graph = TaskGraph.build(tasks)
        self._on_change = on_change

    # ---- 读取 ----

    @property
    def graph(self) -> TaskGraph:
        """依赖图；``dependencies`` 不随状态变更变化，故只建一次。"""
        return self._graph

    def get(self, task_id: str) -> Task:
        """返回板上存活的 ``Task`` 对象本身；未知 id 抛 ``KeyError``。

        故意不返回副本：调用方（如人工修复流程）可以通过它读到最新状态，
        且 :meth:`refresh` 的「上游事后恢复」场景要求看到真实对象。
        """
        return self._tasks[task_id]

    def add_task(self, task: Task) -> None:
        """P2-1：运行时动态把一个新任务加入面板，初始状态直接为 ``READY``。

        与 :meth:`transition` 同属「状态写入点」，但**绕过状态机**：新任务不存在
        于板上，没有 ``from_status``，故合成一条 ``PENDING → READY``、理由
        ``dynamic-created`` 的 ``StatusChange`` 让这次注入在历史里可见（与
        :meth:`reset_failed` 同类的显式绕过）。

        动态任务按约定不依赖任何上游（子任务独立调度），因此无需经
        ``refresh()`` 的依赖判定即可被 :meth:`take_ready` 在下一轮拾取。重复 id
        抛 ``ValueError``——id 是面板的主键，静默覆盖会丢历史。
        """
        if task.id in self._tasks:
            raise ValueError(f"任务 id 已存在，无法动态添加：{task.id}")
        now = datetime.now()
        change = StatusChange(
            from_status=TaskStatus.PENDING,
            to_status=TaskStatus.READY,
            reason="dynamic-created",
            at=now,
        )
        new_task = task.model_copy(
            update={
                "status": TaskStatus.READY,
                "updated_at": now,
                "status_history": [*task.status_history, change],
            }
        )
        self._tasks[task.id] = new_task
        if self._on_change is not None:
            self._on_change(new_task, change)

    def children_of(self, parent_id: str) -> list[Task]:
        """P2-1：某任务的全部直接子任务（按 id 升序）。

        直接扫 ``_tasks`` 的 ``parent_id`` 字段，不读依赖图——运行时动态新增的
        任务不进 ``__init__`` 建的那张图（子任务独立调度，无需父先于子），读图
        会把它们漏掉。
        """
        return [
            self._tasks[task_id]
            for task_id in sorted(self._tasks)
            if self._tasks[task_id].parent_id == parent_id
        ]

    def all(self) -> list[Task]:
        """全部任务，按 id 升序（确定性输出）。"""
        return [self._tasks[task_id] for task_id in sorted(self._tasks)]

    def ids(self) -> list[str]:
        """全部任务 id，按 id 升序。"""
        return sorted(self._tasks)

    def map(self) -> dict[str, Task]:
        """``id → Task`` 浅拷贝：映射结构不可被外部改写，值是存活引用。"""
        return dict(self._tasks)

    # ---- 状态写入 ----

    def transition(self, task_id: str, to: TaskStatus, *, reason: str = "") -> Task:
        """唯一的状态写入入口：校验 → 换新对象 → 记历史 → 回调 → 返回。

        非法跃迁抛 :class:`IllegalTransitionError`，且板上状态原样保留。
        ``on_change`` 在新 ``Task`` 落板之后调用，携带 ``(新 Task, StatusChange)``。
        """
        task = self._tasks[task_id]
        from_status = task.status
        assert_transition(from_status, to, task_id=task_id)
        now = datetime.now()
        change = StatusChange(
            from_status=from_status, to_status=to, reason=reason, at=now
        )
        new_task = task.model_copy(
            update={
                "status": to,
                "updated_at": now,
                "status_history": [*task.status_history, change],
            }
        )
        self._tasks[task_id] = new_task
        if self._on_change is not None:
            self._on_change(new_task, change)
        return new_task

    def bump_attempt(self, task_id: str) -> Task:
        """``attempts += 1``，不改状态（派发时的额度扣除，spec §6.2）。"""
        task = self._tasks[task_id]
        return self._replace(task, attempts=task.attempts + 1)

    def bump_rework(self, task_id: str) -> Task:
        """``rework_count += 1``，不改状态；状态跃迁由调用方另行 transition。"""
        task = self._tasks[task_id]
        return self._replace(task, rework_count=task.rework_count + 1)

    def record_review(self, task_id: str, record: ReviewRecord) -> None:
        """追加一条评审记录；不改状态。

        与 :meth:`set_result` 一样不触发 ``on_change``（没有 ``StatusChange``）：
        评审结果由紧随其后的 ``REVIEW → {SUCCEEDED|REWORK|FAILED}`` 跃迁一并
        带上板，checkpoint 不会丢。
        """
        task = self._tasks[task_id]
        self._replace(task, reviews=[*task.reviews, record])

    def set_result(self, task_id: str, result: TaskResult) -> None:
        """写入执行结果；不改状态（状态流转由调用方 transition）。"""
        task = self._tasks[task_id]
        self._replace(task, result=result)

    def reset_failed(self, task_id: str) -> Task:
        """人工重置 ``FAILED → READY``（spec §6.1 脚注，V1 默认不启用）。

        ``FAILED`` 无出边、不可能走状态机，故这里直接改状态；为让绕过可见而
        不是静默，显式追加 ``reason="manual-retry"`` 的 ``StatusChange``。
        只接受当前确实处于 ``FAILED`` 的任务——否则历史会记下一次并未发生的
        跃迁。
        """
        task = self._tasks[task_id]
        if task.status != TaskStatus.FAILED:
            raise ValueError(
                f"reset_failed 只接受 FAILED 任务：task_id={task_id} "
                f"当前状态 {task.status.value}"
            )
        now = datetime.now()
        change = StatusChange(
            from_status=TaskStatus.FAILED,
            to_status=TaskStatus.READY,
            reason="manual-retry",
            at=now,
        )
        new_task = task.model_copy(
            update={
                "status": TaskStatus.READY,
                "updated_at": now,
                "status_history": [*task.status_history, change],
            }
        )
        self._tasks[task_id] = new_task
        if self._on_change is not None:
            self._on_change(new_task, change)
        return new_task

    # ---- 重算与派发 ----

    def refresh(self) -> int:
        """重算未开工任务的状态（每轮调度调用），返回变更条数。

        四步顺序固定：

        (i) 任一 ``FAILED`` / ``CANCELLED`` 任务的**传递**下游中，所有未开工的
            （``PENDING`` / ``READY``）任务 ``→ BLOCKED``。这正是 spec §7.3 的
            「失败传播」：``TaskGraph.descendants()`` 决定范围，已是终态的下游
            （``SUCCEEDED`` 等）不受影响。``blocked_ids`` 只覆盖**直接**依赖，
            不足以表达传递闭包，故这里直接走图算法。
        (ii) ``PENDING → SKIPPED``：任一依赖 ``SKIPPED``（``graph.py`` 故意
             不做这一跳，是 ``TaskBoard`` 的职责）；
        (iii) ``PENDING → READY``：全部依赖 ``SUCCEEDED``；
        (iv) ``BLOCKED → READY``：依赖事后全部变为 ``SUCCEEDED``（人工修复等）。

        (i) 必须先于 (ii)，这是**可观测**的：同时依赖 ``FAILED`` 与 ``SKIPPED``
        上游的任务先被判为 ``BLOCKED``。(iii) 与 (iv) 的候选状态集合互斥
        （``PENDING`` / ``BLOCKED``），且任一步的输出都不满足另一步的前置条件，
        故二者先后不改变本轮结果；多级传播由调度循环的下一轮调用收敛。
        """
        changed = 0

        for task_id in self._blocked_by_failed_upstream():
            self.transition(task_id, TaskStatus.BLOCKED, reason="upstream-failed")
            changed += 1

        for task_id in self._pending_ids():
            deps = self._tasks[task_id].dependencies
            if any(self._status_of(dep) == TaskStatus.SKIPPED for dep in deps):
                self.transition(task_id, TaskStatus.SKIPPED, reason="upstream-skipped")
                changed += 1

        for task_id in ready_ids(self._tasks):
            self.transition(task_id, TaskStatus.READY, reason="dependencies-met")
            changed += 1

        for task_id in self._recovered_ids():
            self.transition(task_id, TaskStatus.READY, reason="dependencies-recovered")
            changed += 1

        return changed

    def take_ready(self, limit: int) -> list[Task]:
        """先 :meth:`refresh`，再按 ``(priority, id)`` 升序派发至多 ``limit`` 个。

        候选是 ``READY`` **与** ``REWORK`` 两种状态：``REWORK`` 等的是「带上
        fix_hints 重新执行」，spec §6.1 把 ``REWORK → RUNNING`` 明确划给调度器
        （「调度器取得槽位并派发」）；只取 ``READY`` 会让返工任务永远无人认领
        （死锁）。``refresh()`` 不动 ``REWORK``，它只在派发时离开该状态。

        两条派发路径**故意不对称**（spec §6.2），勿擅自「统一」：

        - ``READY → RUNNING``：先扣一次 ``attempts``。额度在**派发时**扣除，
          进程中断也消耗一次，从机制上杜绝「中断 → resume → 再中断」的无限
          循环；reason ``dispatched``。
        - ``REWORK → RUNNING``：**不扣** ``attempts``——返工是质量驱动的重执行，
          额度记在 ``rework_count`` 上（``REVIEW → REWORK`` 时已由调用方 bump）；
          reason ``rework-dispatched``。

        两个额度各自有界：每轮返工至多 ``max_attempts`` 次执行、全局至多
        ``max_reworks`` 轮，不存在无限循环。被 ``limit`` 截断的任务保持原状态、
        不扣额度。返回按派发顺序排列的 RUNNING 任务。
        """
        self.refresh()
        candidates = sorted(
            (
                task
                for task in self._tasks.values()
                if task.status in (TaskStatus.READY, TaskStatus.REWORK)
            ),
            key=lambda task: (task.priority, task.id),
        )
        taken: list[Task] = []
        for task in candidates[: max(0, limit)]:
            if task.status == TaskStatus.READY:
                self.bump_attempt(task.id)
                taken.append(
                    self.transition(task.id, TaskStatus.RUNNING, reason="dispatched")
                )
            else:
                taken.append(
                    self.transition(
                        task.id, TaskStatus.RUNNING, reason="rework-dispatched"
                    )
                )
        return taken

    # ---- 汇总 ----

    def is_terminal(self) -> bool:
        """所有任务都在终态（空板视为终态）。"""
        return all(is_terminal_status(task.status) for task in self._tasks.values())

    def stats(self) -> RunStats:
        """按状态计数。

        token / 调用次数 / 成本字段留零：它们由 Task 29 的 Crew 从
        ``ModelFactory`` 汇总后填入。
        """
        counts = Counter(task.status for task in self._tasks.values())
        return RunStats(
            total_tasks=len(self._tasks),
            succeeded=counts[TaskStatus.SUCCEEDED],
            failed=counts[TaskStatus.FAILED],
            blocked=counts[TaskStatus.BLOCKED],
            skipped=counts[TaskStatus.SKIPPED],
        )

    def explain_stall(self) -> str:
        """多行人类可读的停滞诊断（spec §7.4）。

        对每个非终态任务列出其状态与所有未满足的上游 id 及其状态，另单独列出
        依赖了板上不存在 id 的任务；读者无需翻日志即可定位「谁在等谁」。
        状态一律用 ``str`` 值（``TaskStatus`` 是 ``str`` 枚举，直接插值会得到
        ``TaskStatus.PENDING`` 而非 ``pending``）。
        """
        stalled = [task for task in self.all() if not is_terminal_status(task.status)]
        if not stalled:
            return "停滞诊断：所有任务均已到达终态，无停滞。"
        lines = [f"停滞诊断：{len(stalled)} 个任务未终结。"]
        dangling: list[str] = []
        for task in stalled:
            blockers: list[str] = []
            for dep in sorted(set(task.dependencies)):
                dep_status = self._status_of(dep)
                if dep_status == TaskStatus.SUCCEEDED:
                    continue
                if dep_status is None:
                    blockers.append(f"{dep} [依赖缺失]")
                    dangling.append(f"- {task.id} → {dep}")
                else:
                    blockers.append(f"{dep} [{dep_status.value}]")
            if blockers:
                lines.append(
                    f"- {task.id} [{task.status.value}] ← 阻塞上游：{'、'.join(blockers)}"
                )
            else:
                lines.append(f"- {task.id} [{task.status.value}] ← 无未满足的上游")
        if dangling:
            lines.append("依赖了板上不存在的任务 id：")
            lines.extend(dangling)
        return "\n".join(lines)

    # ---- 快照 ----

    def snapshot(self) -> list[dict[str, Any]]:
        """全量 JSON 化快照（供 ``tasks.json``；含 reviews / status_history）。"""
        return [task.model_dump(mode="json") for task in self.all()]

    @classmethod
    def from_snapshot(cls, data: Sequence[dict[str, Any]]) -> TaskBoard:
        """由快照重建；未知状态字符串等非法字段由 Pydantic 直接拒绝。"""
        return cls([Task.model_validate(item) for item in data])

    # ---- 内部 ----

    def _replace(self, task: Task, **updates: Any) -> Task:
        """用 ``model_copy`` 生成新 ``Task`` 并落板；不触发 ``on_change``。"""
        new_task = task.model_copy(update={**updates, "updated_at": datetime.now()})
        self._tasks[new_task.id] = new_task
        return new_task

    def _status_of(self, task_id: str) -> TaskStatus | None:
        """依赖的状态；依赖了板上不存在的 id 时返回 ``None``。"""
        task = self._tasks.get(task_id)
        return None if task is None else task.status

    def _pending_ids(self) -> list[str]:
        return [task.id for task in self.all() if task.status == TaskStatus.PENDING]

    def _blocked_by_failed_upstream(self) -> list[str]:
        """永久失败上游的全部传递下游中，尚未开工者（按 id 升序）。

        spec §7.3「失败传播」：任一任务 ``FAILED`` / ``CANCELLED`` 后，
        ``graph.descendants()`` 中所有未开始的任务转 ``BLOCKED``；已经
        ``SUCCEEDED`` 的下游不受影响。终态（``SUCCEEDED`` / ``SKIPPED`` /
        ``CANCELLED``）与在跑（``RUNNING`` / ``REVIEW`` / ``REWORK``）的一律
        不动——``SUCCEEDED → BLOCKED`` 本就是非法跃迁，跳过它们既是语义要求，
        也避免无谓抛错。
        """
        doomed: set[str] = set()
        for task in self.all():
            if task.status in _FAILED_UPSTREAM_STATES:
                doomed |= self._graph.descendants(task.id)
        return [
            task.id
            for task in self.all()
            if task.id in doomed
            and task.status in (TaskStatus.PENDING, TaskStatus.READY)
        ]

    def _recovered_ids(self) -> list[str]:
        """依赖已全部 ``SUCCEEDED`` 的 ``BLOCKED`` 任务（可解除阻塞）。"""
        return [
            task.id
            for task in self.all()
            if task.status == TaskStatus.BLOCKED
            and all(
                self._status_of(dep) == TaskStatus.SUCCEEDED
                for dep in task.dependencies
            )
        ]
