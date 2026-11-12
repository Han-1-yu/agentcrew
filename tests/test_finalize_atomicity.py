"""D-16 剩余：封版写盘的原子性与重试边界。

注入点是 ``os.replace``——标准库调用，而不是 ``_finalize`` 里的内部方法名。
这样 checkpoint 的内部实现重构（换写法、拆函数）不会让测试假失败，而它守住的
契约（「目标文件任何时刻只可能是旧的完整内容或新的完整内容」）是稳的。

覆盖三件事：
1. **硬失败**（磁盘满 ENOSPC）：抛错、旧内容完好、临时文件被清理；
2. **瞬时钟**（Windows 杀软 PermissionError）：有界重试后成功；
3. **重试用尽**：原样抛出，不吞。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.task.task_board import TaskBoard


def _store(tmp_path: Path) -> CheckpointStore:
    store = CheckpointStore.create(
        tmp_path, project_goal="D-16 原子性", config_snapshot={},
        run_id="R-atomic")
    store.save_tasks(TaskBoard([]))
    return store


def _on_disk(store: CheckpointStore) -> dict:
    return json.loads((store.run_dir / "run.json").read_text(encoding="utf-8"))


def _leftovers(store: CheckpointStore) -> list[str]:
    return sorted(p.name for p in store.run_dir.iterdir() if p.name.endswith(".tmp"))


def test_hard_failure_keeps_previous_content_and_cleans_tmp(tmp_path, monkeypatch):
    """ENOSPC 这类不可重试错误：抛出去，但盘上不能出现半截文件。"""
    store = _store(tmp_path)
    assert _on_disk(store)["status"] == RunStatus.RUNNING.value

    def enospc(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", enospc)
    record = store.load_record()
    record.status = RunStatus.COMPLETED
    with pytest.raises(OSError):
        store.save_record(record)
    monkeypatch.undo()

    # 旧内容是完整的 RUNNING，而不是空文件 / 半截 JSON
    assert _on_disk(store)["status"] == RunStatus.RUNNING.value
    assert _leftovers(store) == [], "临时文件必须被 finally 清理"


def test_transient_permission_error_is_retried_and_succeeds(tmp_path, monkeypatch):
    """Windows 杀软瞬时抢占文件：重试若干次后应成功，而不是整个 run 崩掉。"""
    store = _store(tmp_path)
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "file in use by antivirus")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    record = store.load_record()
    record.status = RunStatus.COMPLETED
    store.save_record(record)
    monkeypatch.undo()

    assert calls["n"] >= 3
    assert _on_disk(store)["status"] == RunStatus.COMPLETED.value
    assert _leftovers(store) == []


def test_retry_exhausted_raises_instead_of_swallowing(tmp_path, monkeypatch):
    """重试用尽必须抛出——静默成功会让调用方以为落盘了。"""
    store = _store(tmp_path)
    calls = {"n": 0}

    def always_busy(src, dst):
        calls["n"] += 1
        raise PermissionError(13, "still locked")

    monkeypatch.setattr(os, "replace", always_busy)
    record = store.load_record()
    record.status = RunStatus.FAILED
    with pytest.raises(PermissionError):
        store.save_record(record)
    monkeypatch.undo()

    assert calls["n"] >= 2, "应当重试后才放弃"
    assert _on_disk(store)["status"] == RunStatus.RUNNING.value


def test_write_is_recoverable_after_failure(tmp_path, monkeypatch):
    """失败后重试成功：这就是 resume 依赖的语义——盘上永远可解析、可继续。"""
    store = _store(tmp_path)

    def boom(src, dst):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "replace", boom)
    record = store.load_record()
    record.status = RunStatus.COMPLETED
    with pytest.raises(OSError):
        store.save_record(record)
    monkeypatch.undo()

    # 失败后仍能读回旧状态（说明文件没坏）
    recovered = store.load_record()
    assert recovered.status == RunStatus.RUNNING

    recovered.status = RunStatus.COMPLETED
    store.save_record(recovered)
    assert _on_disk(store)["status"] == RunStatus.COMPLETED.value


def test_tasks_and_record_can_diverge_without_corruption(tmp_path, monkeypatch):
    """A-4/A-20：tasks 写成功、record 写失败时的中间态必须仍可 resume。

    这不是「原子」能消除的（两个文件），而是「顺序」保证的：record 最后写，
    所以 record 未更新时 tasks 已是新快照 —— resume 重放不会丢任务进度。
    """
    store = _store(tmp_path)
    real_replace = os.replace
    seen: list[str] = []

    def fail_on_record(src, dst):
        seen.append(Path(dst).name)
        if Path(dst).name == "run.json":
            raise OSError(28, "disk full")
        return real_replace(src, dst)

    from agentcrew.task.task import Task, TaskStatus

    board = TaskBoard([
        Task(id="T-1", title="t", description="d", role="backend_dev",
             acceptance_criteria=["a"], deliverables=[],
             status=TaskStatus.READY),
    ])
    board.transition("T-1", TaskStatus.RUNNING, reason="probe")

    monkeypatch.setattr(os, "replace", fail_on_record)
    store.save_tasks(board)                      # tasks 成功
    record = store.load_record()
    record.status = RunStatus.COMPLETED
    with pytest.raises(OSError):
        store.save_record(record)                # record 失败
    monkeypatch.undo()

    # 中间态：tasks 新、record 旧 —— 仍可解析，resume 会重放
    tasks = json.loads((store.run_dir / "tasks.json").read_text(encoding="utf-8"))
    assert tasks["tasks"][0]["status"] == TaskStatus.RUNNING.value
    assert _on_disk(store)["status"] == RunStatus.RUNNING.value
    assert "run.json" in seen, "record 必须是最后写的那个文件"
