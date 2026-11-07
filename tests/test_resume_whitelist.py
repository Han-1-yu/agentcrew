"""R22：resume 状态白名单（六层审计遗留——R17 只补了进程锁那一半）。

拒绝只有两种（``Crew.resume_from``）：completed（终局成功，续跑没有意义）与
running 且心跳文件新鲜（R17+ 活进程每 30s 写心跳，「正被另一进程跑着」的明确
信号，再 resume 会双写坏 checkpoint）。其余全部放行——**failed 也是恢复路径**
（未处理异常兜底会把崩溃当场封成 FAILED），无心跳的老 run 保留既有直接恢复行为。

终局拒绝走 ``ValueError``：CLI ``resume`` 捕获后打印可读提示（退出码 3），
API ``/runs/{id}/resume`` 转 4xx——与「无法读取 run」同一条错误通道。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.runtime.lifecycle import write_heartbeat
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard


def _make_run(tmp_path, *, status: RunStatus, updated_at: datetime | None = None):
    """建一个最小可续跑的 run 目录：任务面板 + 指定状态的 run.json。"""
    store = CheckpointStore.create(
        tmp_path, project_goal="R22 白名单", config_snapshot={},
        run_id="R-whitelist")
    board = TaskBoard([
        Task(id="T-1", title="t", description="d", role="backend_dev",
             acceptance_criteria=["a"], deliverables=[]),
    ])
    store.save_tasks(board)
    record = store.load_record()
    record.status = status
    if updated_at is not None:
        record.updated_at = updated_at
    store.save_record(record)
    return store


async def test_resume_allowed_awaiting_approval(tmp_path):
    """awaiting_approval 是合法恢复状态，但必须有未消费审批单（R4 纪律）。"""
    from agentcrew.runtime.approvals import ApprovalStore

    _make_run(tmp_path, status=RunStatus.AWAITING_APPROVAL)
    ApprovalStore(tmp_path / "R-whitelist").create(
        "plan", context={"task_ids": ["T-1"]})
    settings = Settings.load(output_dir=tmp_path)
    crew = Crew.resume_from(settings, "R-whitelist")
    assert crew is not None


@pytest.mark.parametrize(
    "status",
    [RunStatus.INTERRUPTED, RunStatus.CANCELLED, RunStatus.FAILED],
)
async def test_resume_allowed_statuses(tmp_path, status):
    """可恢复状态照常构造（既有路径行为不变；FAILED 也是恢复路径——
    未处理异常兜底会把崩溃当场封成 FAILED，拒了就堵死崩溃恢复）。"""
    _make_run(tmp_path, status=status)
    settings = Settings.load(output_dir=tmp_path)
    crew = Crew.resume_from(settings, "R-whitelist")
    assert crew is not None


async def test_resume_refuses_completed(tmp_path):
    """终局成功不是崩溃产物，续跑没有意义：拒绝并给出可读原因。"""
    _make_run(tmp_path, status=RunStatus.COMPLETED)
    settings = Settings.load(output_dir=tmp_path)
    with pytest.raises(ValueError, match="已终局"):
        Crew.resume_from(settings, "R-whitelist")


async def test_resume_refuses_running_with_fresh_heartbeat(tmp_path):
    """心跳新鲜的 RUNNING：正被另一进程跑着，双写会坏 checkpoint。"""
    _make_run(tmp_path, status=RunStatus.RUNNING)
    write_heartbeat(tmp_path / "R-whitelist")
    settings = Settings.load(output_dir=tmp_path)
    with pytest.raises(ValueError, match="另一进程"):
        Crew.resume_from(settings, "R-whitelist")


async def test_resume_allows_running_without_heartbeat(tmp_path):
    """无心跳文件的 RUNNING（R17 之前 / 心跳写失败）：无法区分死活，保留既有
    直接恢复行为——repair_statuses 兜底，不把硬杀恢复路径堵死。"""
    _make_run(tmp_path, status=RunStatus.RUNNING)   # updated_at = 现在，无心跳
    settings = Settings.load(output_dir=tmp_path)
    crew = Crew.resume_from(settings, "R-whitelist")
    assert crew is not None


async def test_resume_allows_running_with_stale_updated_at(tmp_path):
    """硬杀孤儿（无心跳 + updated_at 超 24h）：允许走既有 repair_statuses 恢复。"""
    _make_run(
        tmp_path, status=RunStatus.RUNNING,
        updated_at=datetime.now() - timedelta(hours=25))
    settings = Settings.load(output_dir=tmp_path)
    crew = Crew.resume_from(settings, "R-whitelist")
    assert crew is not None


async def test_resume_allows_running_with_expired_heartbeat(tmp_path):
    """有心跳但已过期 15 分钟：同样是孤儿，放行。"""
    _make_run(
        tmp_path, status=RunStatus.RUNNING,
        updated_at=datetime.now() - timedelta(hours=1))
    stale = datetime.now() - timedelta(minutes=20)
    write_heartbeat(tmp_path / "R-whitelist", now=stale)
    settings = Settings.load(output_dir=tmp_path)
    crew = Crew.resume_from(settings, "R-whitelist")
    assert crew is not None
