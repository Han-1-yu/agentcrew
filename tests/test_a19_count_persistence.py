"""A-19：非跃迁写入必须落盘（评审记录 / 返工计数 / 派发额度）。

场景：门禁是 ``record_review → G2 长评审（几十秒） → transition``。此前三个
写入方法都只改内存、等随后的 ``transition`` 一起落盘——这段窗口被硬杀，评审
轮次与返工额度就凭空回滚，resume 后按旧值重算（额度白送、收敛历史丢失）。

这里钉住两件事：
1. ``record_review`` / ``bump_rework`` / ``bump_attempt`` / ``set_result`` /
   ``reassign`` 都会触发 ``on_persist``；
2. 未接线 ``on_persist`` 时行为不变（纯内存面板，供直连 TaskBoard 的老用法）。
"""

from __future__ import annotations

from datetime import datetime

from agentcrew.task.task import (
    ReviewRecord,
    Task,
    TaskResult,
    TaskStatus,
)
from agentcrew.task.task_board import TaskBoard


def _task(task_id: str = "T-1") -> Task:
    return Task(id=task_id, title="t", description="d", role="backend_dev",
                acceptance_criteria=["ok"], deliverables=[],
                status=TaskStatus.REVIEW)


def _record(task_id: str = "T-1") -> ReviewRecord:
    return ReviewRecord(task_id=task_id, round=1, layer="static", verdict="approve",
                        score=None, issues=[], fix_hints=[], reviewer="static",
                        created_at=datetime.now())


class _Recorder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def test_record_review_persists_immediately():
    """A-19 核心：G2 评审前的那条 G1 记录必须已经落盘。"""
    rec = _Recorder()
    board = TaskBoard([_task()], on_persist=rec)
    board.record_review("T-1", _record())
    assert rec.calls == 1
    assert len(board.get("T-1").reviews) == 1


def test_bump_rework_persists_immediately():
    """返工额度自增后立即落盘——否则硬杀会让额度白送。"""
    rec = _Recorder()
    board = TaskBoard([_task()], on_persist=rec)
    board.bump_rework("T-1")
    assert rec.calls == 1
    assert board.get("T-1").rework_count == 1


def test_bump_attempt_persists_immediately():
    rec = _Recorder()
    board = TaskBoard([_task()], on_persist=rec)
    board.bump_attempt("T-1")
    assert rec.calls == 1
    assert board.get("T-1").attempts == 1


def test_set_result_persists_immediately():
    rec = _Recorder()
    board = TaskBoard([_task()], on_persist=rec)
    now = datetime.now()
    board.set_result("T-1", TaskResult(
        task_id="T-1", role="backend_dev", summary="done", artifacts=[],
        started_at=now, finished_at=now, duration_ms=0))
    assert rec.calls == 1


def test_reassign_persists_immediately():
    rec = _Recorder()
    task = _task()
    task.status = TaskStatus.REWORK
    board = TaskBoard([task], on_persist=rec)
    board.reassign("T-1", "frontend_dev")
    assert rec.calls == 1
    assert board.get("T-1").role == "frontend_dev"


def test_persist_is_optional():
    """未接线 on_persist：行为与 A-19 之前一致（不炸、不写盘）。"""
    board = TaskBoard([_task()])
    board.record_review("T-1", _record())
    board.bump_rework("T-1")
    assert board.get("T-1").rework_count == 1


def test_transition_still_uses_on_change_not_persist():
    """状态跃迁走 on_change（含 MANIFEST 重写），不该重复触发 on_persist。"""
    changes = []
    rec = _Recorder()
    board = TaskBoard([_task()], on_change=lambda t, c: changes.append(c),
                      on_persist=rec)
    board.transition("T-1", TaskStatus.SUCCEEDED, reason="ok")
    assert len(changes) == 1
    assert rec.calls == 0
