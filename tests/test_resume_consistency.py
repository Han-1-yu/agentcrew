"""D-16：中断 / 恢复的一致性负向用例组。

这一组覆盖三类「只在异常路径才出现」的边界，都是靠手工验证过的场景，容易在
重构里悄悄回归：

1. **心跳被删**（A-6）：活跃进程心跳写失败 → 放行 resume 就是双写 checkpoint；
2. **时钟回拨**（A-7）：心跳时间在未来 → 判定必须作废，不能改写活 run；
3. **锁的实际互斥**（R17）：两个实例（模拟两个进程）不能同时持有。

写盘注入 OSError 的场景（A-4/A-20）没有放进这里：它需要 patch 到 ``_finalize``
内部的原子写，注入点与实现细节耦合太紧，维护成本高于它守住的回归。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.runtime.lifecycle import (
    HEARTBEAT_FILE,
    LOCK_FILE,
    ResumeLock,
    ResumeLockError,
    is_orphaned_running,
    reconcile_stale_runs,
    write_heartbeat,
)
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard


def _make_run(
    output_dir: Path, *, updated_at: datetime | None = None,
    run_id: str = "R-consistency",
) -> CheckpointStore:
    store = CheckpointStore.create(
        output_dir, project_goal="D-16 一致性", config_snapshot={},
        run_id=run_id)
    store.save_tasks(TaskBoard([
        Task(id="T-1", title="t", description="d", role="backend_dev",
             acceptance_criteria=["a"], deliverables=[]),
    ]))
    record = store.load_record()
    record.status = RunStatus.RUNNING
    if updated_at is not None:
        record.updated_at = updated_at
    store.save_record(record)
    return store


def _status(run_dir: Path) -> str:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["status"]


# ── 1. 心跳被删（A-6） ────────────────────────────────────────────────────


async def test_resume_refused_when_heartbeat_missing_but_run_recent(tmp_path):
    """A-6：RUNNING + 无心跳 + updated_at 尚新 → 拒绝。

    这正是「活跃进程心跳写失败」的窗口：盘上看不出有人在跑，但 run 刚更新过。
    放行的代价是两个进程同写 checkpoint。
    """
    _make_run(tmp_path, updated_at=datetime.now())
    settings = Settings.load(output_dir=tmp_path)

    with pytest.raises(ValueError, match="无心跳"):
        Crew.resume_from(settings, "R-consistency")


async def test_resume_allowed_when_heartbeat_missing_and_run_old(tmp_path):
    """A-6 的另一侧：无心跳且超过 24h 僵尸判据 → 认定为死 run，放行恢复。"""
    _make_run(tmp_path, updated_at=datetime.now() - timedelta(hours=25))
    settings = Settings.load(output_dir=tmp_path)

    crew = Crew.resume_from(settings, "R-consistency")
    assert crew is not None


# ── 2. 时钟回拨（A-7） ────────────────────────────────────────────────────


def test_reconcile_skips_run_when_heartbeat_in_future(tmp_path):
    """A-7：心跳时间在未来（时钟回拨）→ 不收敛，并在结果里说明原因。"""
    store = _make_run(tmp_path)
    write_heartbeat(store.run_dir, now=datetime.now() + timedelta(hours=3))

    results = reconcile_stale_runs(tmp_path)

    assert _status(store.run_dir) == RunStatus.RUNNING.value
    reasons = {r["run_id"]: r.get("reason") for r in results}
    assert reasons.get("R-consistency") == "clock_skew"


def test_reconcile_still_converges_genuine_orphan(tmp_path):
    """对照：心跳确实过期时照常收敛成 interrupted。"""
    store = _make_run(tmp_path)
    write_heartbeat(store.run_dir, now=datetime.now() - timedelta(hours=2))

    results = reconcile_stale_runs(tmp_path)

    assert _status(store.run_dir) == RunStatus.INTERRUPTED.value
    assert any(r["reason"] == "heartbeat_expired" for r in results)


def test_orphan_check_ignores_run_with_live_heartbeat(tmp_path):
    store = _make_run(tmp_path)
    write_heartbeat(store.run_dir)          # 刚写的一帧
    assert is_orphaned_running(store.run_dir) is False


# ── 3. 锁的实际互斥（R17） ────────────────────────────────────────────────


def test_two_holders_cannot_acquire_same_run(tmp_path):
    """模拟两个进程：第二个 acquire 必须失败（而不是静默共写）。"""
    store = _make_run(tmp_path)
    first = ResumeLock(store.run_dir)
    second = ResumeLock(store.run_dir)

    first.acquire()
    try:
        with pytest.raises(ResumeLockError, match="另一个进程"):
            second.acquire()
        # 锁文件仍在，且属于 first
        assert (store.run_dir / LOCK_FILE).is_file()
    finally:
        first.release()


def test_lock_can_be_reacquired_after_release(tmp_path):
    """正常收尾释放后，下一次 resume 应能拿到锁（否则 run 永久卡死）。"""
    store = _make_run(tmp_path)
    first = ResumeLock(store.run_dir)
    first.acquire()
    first.release()

    second = ResumeLock(store.run_dir)
    second.acquire()
    second.release()
    assert not (store.run_dir / LOCK_FILE).exists()


def test_release_does_not_remove_foreign_lock(tmp_path):
    """只删自己持有的锁：owner 不匹配时不能替别人解锁。"""
    store = _make_run(tmp_path)
    first = ResumeLock(store.run_dir)
    first.acquire()

    bogus = ResumeLock(store.run_dir)
    bogus.release()          # 未持有 → 幂等空操作
    assert (store.run_dir / LOCK_FILE).is_file()
    first.release()


# ── 4. 恢复入口的四类状态（汇总） ─────────────────────────────────────────


@pytest.mark.parametrize("status,expect_error", [
    (RunStatus.COMPLETED, True),     # 终局成功，续跑没有意义
    (RunStatus.FAILED, False),       # 崩溃兜底会记 FAILED，必须能恢复
    (RunStatus.INTERRUPTED, False),  # 孤儿收敛后的标准恢复入口
])
async def test_resume_status_matrix(tmp_path, status, expect_error):
    store = _make_run(tmp_path, updated_at=datetime.now() - timedelta(hours=25))
    record = store.load_record()
    record.status = status
    store.save_record(record)
    settings = Settings.load(output_dir=tmp_path)

    if expect_error:
        with pytest.raises(ValueError):
            Crew.resume_from(settings, "R-consistency")
    else:
        assert Crew.resume_from(settings, "R-consistency") is not None


def test_heartbeat_file_is_removed_on_clean_finish(tmp_path):
    """收尾必须删心跳：留着会让后续 resume 误判「还有活进程」。"""
    from agentcrew.runtime.lifecycle import clear_heartbeat

    store = _make_run(tmp_path)
    write_heartbeat(store.run_dir)
    assert (store.run_dir / HEARTBEAT_FILE).is_file()

    clear_heartbeat(store.run_dir)
    assert not (store.run_dir / HEARTBEAT_FILE).exists()
