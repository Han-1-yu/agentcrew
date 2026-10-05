"""任务状态的唯一权威跃迁表（spec §6.1）。

任何模块都不得直接写 ``Task.status``：状态变更一律经
``TaskBoard.transition()``，由它调用本模块的 :func:`assert_transition`。

``FAILED`` 的出边集合为空：人工重试**不是**状态机边，而是走
``TaskBoard.reset_failed()`` 的显式接口（记录 ``reason="manual-retry"``）。
"""

from typing import Mapping

from agentcrew.task.task import TaskStatus

#: 起始态 → 允许跃迁到的状态集合。十个状态全部有键，终态映射为空集。
ALLOWED_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {
            TaskStatus.READY,  # 全部依赖 SUCCEEDED
            TaskStatus.BLOCKED,  # 任一依赖 FAILED / CANCELLED
            TaskStatus.SKIPPED,  # 任一依赖 SKIPPED
            TaskStatus.CANCELLED,  # 用户取消
        }
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.RUNNING,  # 取得并发槽位并派发
            TaskStatus.BLOCKED,  # 派发前发现依赖已失败
            TaskStatus.CANCELLED,  # 用户取消
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.REVIEW,  # Agent 正常返回且产物解析成功
            TaskStatus.READY,  # 执行异常且 attempts < max_attempts（重试）
            TaskStatus.FAILED,  # 执行异常且 attempts >= max_attempts
            TaskStatus.CANCELLED,  # 用户硬取消
        }
    ),
    TaskStatus.REVIEW: frozenset(
        {
            TaskStatus.SUCCEEDED,  # G1 + G2 + G3 全 approve
            TaskStatus.REWORK,  # 任一门禁 reject 且 rework_count < max_reworks
            TaskStatus.FAILED,  # 门禁 reject 且 rework_count >= max_reworks
            TaskStatus.CANCELLED,  # 用户取消
        }
    ),
    TaskStatus.REWORK: frozenset(
        {
            TaskStatus.RUNNING,  # 再次派发（携带 fix_hints）
            TaskStatus.CANCELLED,  # 用户取消
        }
    ),
    TaskStatus.SUCCEEDED: frozenset(),  # 终态
    TaskStatus.FAILED: frozenset(),  # 终态；人工重试走 reset_failed()，不经状态机
    TaskStatus.BLOCKED: frozenset(
        {
            TaskStatus.READY,  # 每轮重算：全部依赖变为 SUCCEEDED
        }
    ),
    TaskStatus.CANCELLED: frozenset(),  # 终态
    TaskStatus.SKIPPED: frozenset(),  # 终态
}


class IllegalTransitionError(Exception):
    """非法状态跃迁；携带 ``task_id`` / ``from_status`` / ``to_status``。"""

    def __init__(
        self, task_id: str, from_status: TaskStatus, to_status: TaskStatus
    ) -> None:
        self.task_id = task_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"非法状态跃迁：task_id={task_id} "
            f"{from_status.value} -> {to_status.value}"
        )


def can_transition(src: TaskStatus, dst: TaskStatus) -> bool:
    """``src → dst`` 是否为合法跃迁（只判断，不抛）。"""
    return dst in ALLOWED_TRANSITIONS[src]


def assert_transition(src: TaskStatus, dst: TaskStatus, *, task_id: str) -> None:
    """合法则返回 ``None``，否则抛 :class:`IllegalTransitionError`。"""
    if not can_transition(src, dst):
        raise IllegalTransitionError(task_id, src, dst)


def is_terminal(status: TaskStatus) -> bool:
    """终态定义为「出边集合为空」：SUCCEEDED / FAILED / CANCELLED / SKIPPED。"""
    return not ALLOWED_TRANSITIONS[status]
