"""P3-8 增量 checkpoint 的测试。

覆盖：

- ``save_task_delta`` 追加 JSONL 增量（格式正确）；
- ``load_with_deltas`` 正确叠加未合并增量；
- 累计到阈值后自动 compaction（delta 合并进 tasks.json 并清空 delta 文件）；
- 原有全量 ``save_tasks`` / ``load_board`` 行为不变（向后兼容）；
- resume（``load_board``）自动应用 delta。
"""

import json
from datetime import datetime, timedelta

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
)
from agentcrew.task.task import Task, TaskResult, TaskStatus
from agentcrew.task.task_board import TaskBoard


def _mk_task(task_id: str, status: TaskStatus = TaskStatus.READY) -> Task:
    return Task(
        id=task_id,
        title=f"任务 {task_id}",
        description="描述",
        role="backend_dev",
        acceptance_criteria=["完成"],
        deliverables=[
            ArtifactSpec(
                path=f"out/{task_id}.md", kind="markdown", description="产物"
            )
        ],
        status=status,
    )


def _mk_store(tmp_path) -> CheckpointStore:
    run_dir = tmp_path / "out"
    run_dir.mkdir()
    return CheckpointStore(run_dir)


def test_save_task_delta_appends_jsonl(tmp_path):
    store = _mk_store(tmp_path)
    board = TaskBoard([_mk_task("T-1"), _mk_task("T-2")])
    store.save_tasks(board)  # 全量基线

    task = board.get("T-1").model_copy(update={"status": TaskStatus.RUNNING})
    store.save_task_delta(task)

    assert store._delta_path.exists()
    lines = store._delta_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["op"] == "update"
    assert record["task_id"] == "T-1"
    assert record["changes"]["status"] == TaskStatus.RUNNING.value


def test_load_with_deltas_applies_increment(tmp_path):
    store = _mk_store(tmp_path)
    board = TaskBoard([_mk_task("T-1"), _mk_task("T-2")])
    store.save_tasks(board)

    # T-1 在内存里推进到 SUCCEEDED，但只写增量（tasks.json 仍是旧快照）
    now = datetime(2026, 1, 1)
    task = board.get("T-1").model_copy(
        update={
            "status": TaskStatus.SUCCEEDED,
            "result": TaskResult(
                task_id="T-1", role="backend_dev", summary="做完了",
                artifacts=[], started_at=now, finished_at=now + timedelta(seconds=1),
                duration_ms=1,
            ),
        }
    )
    store.save_task_delta(task)

    loaded = store.load_with_deltas()
    assert loaded["T-1"].status is TaskStatus.SUCCEEDED
    assert loaded["T-1"].result.summary == "做完了"
    # T-2 不受影响
    assert loaded["T-2"].status is TaskStatus.READY


def test_multiple_deltas_compact_into_tasks_json(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentcrew.runtime.checkpoint.DELTA_COMPACTION_THRESHOLD", 3
    )
    store = _mk_store(tmp_path)
    board = TaskBoard([_mk_task("T-1")])
    store.save_tasks(board)

    # 写 3 次增量 → 第三次触发 compaction
    for i in range(3):
        task = board.get("T-1").model_copy(
            update={"description": f"第 {i} 次更新"}
        )
        store.save_task_delta(task)

    # compaction 后 delta 文件被清空删除
    assert not store._delta_path.exists()
    # tasks.json 已是合并后的最新状态
    data = json.loads(store._tasks_path.read_text(encoding="utf-8"))
    t1 = next(t for t in data["tasks"] if t["id"] == "T-1")
    assert t1["description"] == "第 2 次更新"


def test_full_save_load_unchanged_without_deltas(tmp_path):
    """无 delta 文件时，全量 save/load 与改造前行为逐字一致。"""
    store = _mk_store(tmp_path)
    board = TaskBoard([_mk_task("T-1"), _mk_task("T-2")])
    store.save_tasks(board)

    restored = store.load_board()
    assert [t.id for t in restored.all()] == ["T-1", "T-2"]
    assert not store._delta_path.exists()


def test_resume_load_board_auto_applies_deltas(tmp_path):
    """resume 时 load_board 自动应用未合并 delta。"""
    store = _mk_store(tmp_path)
    board = TaskBoard([_mk_task("T-1"), _mk_task("T-2")])
    store.save_tasks(board)

    # 进程在 T-1 执行中死掉：内存态 RUNNING 只落了增量
    task = board.get("T-1").model_copy(update={"status": TaskStatus.RUNNING})
    store.save_task_delta(task)

    # 新 store 实例（模拟重新打开 run 目录）→ resume
    resumed = CheckpointStore(tmp_path / "out")
    board2 = resumed.load_board()
    assert board2.get("T-1").status is TaskStatus.RUNNING
    assert board2.get("T-2").status is TaskStatus.READY


def test_delta_supports_add_op(tmp_path):
    """``add`` op 的增量也能被正确应用（格式兼容）。"""
    store = _mk_store(tmp_path)
    board = TaskBoard([_mk_task("T-1")])
    store.save_tasks(board)

    new_task = _mk_task("T-2", status=TaskStatus.READY)
    record = {"op": "add", "task": new_task.model_dump(mode="json")}
    with open(store._delta_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    loaded = store.load_with_deltas()
    assert set(loaded) == {"T-1", "T-2"}


def test_apply_deltas_returns_count_and_clears(tmp_path):
    store = _mk_store(tmp_path)
    board = TaskBoard([_mk_task("T-1")])
    store.save_tasks(board)
    for i in range(2):
        store.save_task_delta(
            board.get("T-1").model_copy(update={"description": f"v{i}"})
        )
    applied = store.apply_deltas()
    assert applied == 2
    assert not store._delta_path.exists()
    # 再调一次是 no-op
    assert store.apply_deltas() == 0
