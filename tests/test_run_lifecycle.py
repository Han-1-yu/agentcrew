"""R17：run 生命周期加固（心跳 / 僵尸收敛 / resume 锁）。

重点验证两件容易出事的事：**活 run 绝不能被误判成僵尸**（新鲜心跳优先于
``updated_at``），以及**锁在正常与异常路径都一定释放**（否则一次失败会让这个
run 永远无法续跑）。最后一条用真实 CLI 启动验证入口接线。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.runtime.lifecycle import (
    HEARTBEAT_FILE,
    LIVENESS_STALE_AFTER,
    LOCK_FILE,
    ResumeLock,
    ResumeLockError,
    RunHeartbeat,
    clear_heartbeat,
    is_orphaned_running,
    read_heartbeat,
    reconcile_stale_runs,
    write_heartbeat,
)
from agentcrew.task.task_board import TaskBoard

NOW = datetime(2026, 11, 7, 12, 0, 0)


def _make_run(
    output_dir: Path,
    run_id: str,
    *,
    status: RunStatus = RunStatus.RUNNING,
    updated_at: datetime | None = None,
) -> Path:
    store = CheckpointStore.create(
        output_dir, project_goal="R17 测试目标", config_snapshot={},
        run_id=run_id,
    )
    record = store.load_record()
    record.status = status
    record.updated_at = updated_at or NOW
    store.save_record(record)
    store.save_tasks(TaskBoard([]))
    return store.run_dir


def _status(run_dir: Path) -> str:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["status"]


# ── 心跳 ────────────────────────────────────────────────────────────────


def test_write_read_clear_heartbeat(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()

    assert read_heartbeat(run_dir) is None
    write_heartbeat(run_dir, now=NOW)
    # A-7：read_heartbeat 恒返回 aware datetime（naive 旧数据按本机时区补齐），
    # 与 naive 的 NOW 直接相等比较会失败，须归一后再比。
    assert read_heartbeat(run_dir) == NOW.astimezone()
    clear_heartbeat(run_dir)
    assert read_heartbeat(run_dir) is None
    clear_heartbeat(run_dir)  # 幂等


def test_corrupt_heartbeat_returns_none(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / HEARTBEAT_FILE).write_text("{bad", encoding="utf-8")

    assert read_heartbeat(run_dir) is None


def test_heartbeat_thread_writes_first_frame_and_clears_on_stop(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    heartbeat = RunHeartbeat(run_dir, interval=0.05)

    heartbeat.start()
    assert read_heartbeat(run_dir) is not None
    heartbeat.start()  # 幂等：不会起第二个线程
    heartbeat.stop()

    assert not (run_dir / HEARTBEAT_FILE).exists()
    heartbeat.stop()  # 幂等


# ── 孤儿判定 ────────────────────────────────────────────────────────────


def test_no_heartbeat_falls_back_to_24h_rule(tmp_path):
    output = tmp_path / "o"
    old = _make_run(output, "R-old", updated_at=NOW - timedelta(hours=25))
    fresh = _make_run(output, "R-fresh", updated_at=NOW - timedelta(hours=1))

    assert is_orphaned_running(old, now=NOW) is True
    assert is_orphaned_running(fresh, now=NOW) is False


def test_fresh_heartbeat_protects_a_run_with_stale_updated_at(tmp_path):
    """长任务期间没有状态跃迁，updated_at 会很旧——心跳必须压过它。"""
    output = tmp_path / "o"
    run_dir = _make_run(output, "R-live", updated_at=NOW - timedelta(hours=25))
    write_heartbeat(run_dir, now=NOW - timedelta(minutes=1))

    assert is_orphaned_running(run_dir, now=NOW) is False


def test_expired_heartbeat_marks_orphan(tmp_path):
    output = tmp_path / "o"
    run_dir = _make_run(output, "R-dead", updated_at=NOW - timedelta(minutes=1))
    write_heartbeat(run_dir, now=NOW - LIVENESS_STALE_AFTER - timedelta(minutes=1))

    assert is_orphaned_running(run_dir, now=NOW) is True


@pytest.mark.parametrize("status", [
    RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED,
    RunStatus.INTERRUPTED, RunStatus.AWAITING_APPROVAL,
])
def test_non_running_statuses_are_never_orphaned(tmp_path, status):
    """awaiting_approval 的进程本就该退出，绝不能被当成僵尸。"""
    output = tmp_path / "o"
    run_dir = _make_run(
        output, f"R-{status.value}", status=status,
        updated_at=NOW - timedelta(hours=25))

    assert is_orphaned_running(run_dir, now=NOW) is False


def test_unreadable_run_json_is_not_orphaned(tmp_path):
    output = tmp_path / "o"
    run_dir = _make_run(output, "R-bad", updated_at=NOW - timedelta(hours=25))
    (run_dir / "run.json").write_text("{broken", encoding="utf-8")

    assert is_orphaned_running(run_dir, now=NOW) is False


# ── reconcile ───────────────────────────────────────────────────────────


def test_reconcile_dry_run_does_not_modify(tmp_path):
    output = tmp_path / "o"
    run_dir = _make_run(output, "R-old", updated_at=NOW - timedelta(hours=25))

    result = reconcile_stale_runs(output, now=NOW, dry_run=True)

    assert len(result) == 1 and result[0]["dry_run"] is True
    assert result[0]["reason"] == "no_heartbeat_stale"
    assert _status(run_dir) == RunStatus.RUNNING.value
    assert not (run_dir / "ledger.jsonl").exists()


def test_reconcile_marks_orphan_and_writes_event(tmp_path):
    output = tmp_path / "o"
    run_dir = _make_run(output, "R-old", updated_at=NOW - timedelta(hours=25))

    result = reconcile_stale_runs(output, now=NOW)

    assert len(result) == 1 and result[0]["ok"] is True
    assert _status(run_dir) == RunStatus.INTERRUPTED.value
    ledger = (run_dir / "ledger.jsonl").read_text(encoding="utf-8")
    assert "run_reconciled" in ledger
    assert "no_heartbeat_stale" in ledger


def test_reconcile_only_touches_orphans(tmp_path):
    output = tmp_path / "o"
    orphan = _make_run(output, "R-orphan", updated_at=NOW - timedelta(hours=25))
    live = _make_run(output, "R-live", updated_at=NOW - timedelta(hours=25))
    write_heartbeat(live, now=NOW - timedelta(minutes=1))
    approval = _make_run(
        output, "R-approval", status=RunStatus.AWAITING_APPROVAL,
        updated_at=NOW - timedelta(hours=25))
    done = _make_run(
        output, "R-done", status=RunStatus.COMPLETED,
        updated_at=NOW - timedelta(hours=25))

    result = reconcile_stale_runs(output, now=NOW)

    assert [item["run_id"] for item in result] == ["R-orphan"]
    assert _status(orphan) == RunStatus.INTERRUPTED.value
    assert _status(live) == RunStatus.RUNNING.value
    assert _status(approval) == RunStatus.AWAITING_APPROVAL.value
    assert _status(done) == RunStatus.COMPLETED.value


def test_reconcile_reports_bad_record_without_raising(tmp_path):
    output = tmp_path / "o"
    run_dir = _make_run(output, "R-bad", updated_at=NOW - timedelta(hours=25))
    raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    raw["schema_version"] = 999
    (run_dir / "run.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    result = reconcile_stale_runs(output, now=NOW)

    assert len(result) == 1 and result[0]["ok"] is False
    assert "SchemaMismatchError" in result[0]["error"]


# ── resume 锁 ───────────────────────────────────────────────────────────


def test_lock_blocks_second_holder_until_released(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    first, second = ResumeLock(run_dir), ResumeLock(run_dir)

    first.acquire()
    with pytest.raises(ResumeLockError) as caught:
        second.acquire()
    assert "另一个进程" in str(caught.value)
    assert first.held and not second.held

    first.release()
    second.acquire()
    assert second.held
    second.release()
    assert not (run_dir / LOCK_FILE).exists()


def test_lock_release_is_idempotent(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    lock = ResumeLock(run_dir)

    lock.acquire()
    lock.release()
    lock.release()

    assert not lock.held
    assert not (run_dir / LOCK_FILE).exists()


def test_stale_lock_is_taken_over(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / LOCK_FILE).write_text(json.dumps({
        "owner": "dead-process", "pid": 1,
        "created_at": (NOW - timedelta(hours=1)).isoformat(),
    }), encoding="utf-8")

    lock = ResumeLock(run_dir)
    lock.acquire(now=NOW)

    assert lock.held
    assert json.loads((run_dir / LOCK_FILE).read_text(encoding="utf-8"))[
        "owner"] != "dead-process"
    lock.release()


def test_fresh_heartbeat_prevents_taking_over_an_old_lock(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / LOCK_FILE).write_text(json.dumps({
        "owner": "still-alive", "pid": 1,
        "created_at": (NOW - timedelta(hours=1)).isoformat(),
    }), encoding="utf-8")
    write_heartbeat(run_dir, now=NOW - timedelta(minutes=1))

    with pytest.raises(ResumeLockError):
        ResumeLock(run_dir).acquire(now=NOW)

    assert json.loads((run_dir / LOCK_FILE).read_text(encoding="utf-8"))[
        "owner"] == "still-alive"


def test_release_does_not_delete_another_owners_lock(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    lock = ResumeLock(run_dir)
    lock.acquire()
    raw = json.loads((run_dir / LOCK_FILE).read_text(encoding="utf-8"))
    raw["owner"] = "someone-else"
    (run_dir / LOCK_FILE).write_text(
        json.dumps(raw), encoding="utf-8")

    lock.release()

    assert (run_dir / LOCK_FILE).exists()
    assert not lock.held


def test_lock_context_manager_releases_on_exception(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()

    with pytest.raises(RuntimeError):
        with ResumeLock(run_dir) as lock:
            assert lock.held
            raise RuntimeError("boom")

    assert not (run_dir / LOCK_FILE).exists()


async def test_crew_resume_releases_lock_and_heartbeat_on_failure(tmp_path, monkeypatch):
    """_start 抛异常时 finally 必须解锁；否则这个 run 再也无法续跑。"""
    output = tmp_path / "o"
    run_dir = _make_run(output, "R-resume")
    settings = Settings.load().model_copy(
        update={"mock": True, "output_dir": output})
    crew = Crew.resume_from(settings, "R-resume")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("_start 炸了")

    monkeypatch.setattr(crew, "_start", boom)
    with pytest.raises(RuntimeError, match="_start 炸了"):
        await crew.resume()

    assert not (run_dir / LOCK_FILE).exists()
    assert not (run_dir / HEARTBEAT_FILE).exists()


# ── 入口接线：真 CLI 启动 ───────────────────────────────────────────────


def test_cli_run_reconciles_orphan_on_startup(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "output"
    output.mkdir()
    orphan = _make_run(
        output, "R-orphan",
        updated_at=datetime.now() - timedelta(hours=25))
    live = _make_run(
        output, "R-live",
        updated_at=datetime.now() - timedelta(hours=25))
    write_heartbeat(live)  # 模拟另一个进程正在跑，心跳新鲜

    # 与 R16 端到端同款白名单环境：宿主机 PYTHONPATH shim / HTTP_PROXY 会干扰子进程。
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON") and "PROXY" not in key.upper()
    }
    env.update({
        "AGENTCREW_MOCK": "true",
        "AGENTCREW_OUTPUT_DIR": str(output),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    result = subprocess.run(
        [sys.executable, "main.py", "run", "--sample", "linear",
         "--no-live", "--auto"],
        cwd=root, env=env, capture_output=True, text=True,
        timeout=180, errors="replace",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _status(orphan) == RunStatus.INTERRUPTED.value
    assert _status(live) == RunStatus.RUNNING.value
    assert "R-orphan" in result.stderr
    # 新 run 的心跳必须被正常收尾删掉，只留下模拟活进程的那一份。
    assert [path.parent.name for path in output.glob(f"*/{HEARTBEAT_FILE}")] == [
        "R-live"]
