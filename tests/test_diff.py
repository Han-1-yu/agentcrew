"""``diff`` 子命令测试（E-2）。

覆盖 brief 要求：相同 run 无差异、配置差异、任务数量差异、产物差异、run 不存在
退 1、``--json`` 合法 JSON。
"""

from __future__ import annotations

import json
from pathlib import Path

import main as cli
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.cli.diff import diff_runs, print_diff
from agentcrew.crew.crew import EXIT_CODES
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunRecord,
    RunStatus,
)
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskStatus
from agentcrew.task.task_board import TaskBoard


def _make_run(
    output_dir: Path,
    run_id: str,
    *,
    config_snapshot: dict | None = None,
    status: RunStatus = RunStatus.COMPLETED,
) -> tuple[CheckpointStore, RunRecord]:
    """建一个最小 run 目录，返回 (store, record)。"""
    store = CheckpointStore.create(
        output_dir, project_goal="g", config_snapshot=config_snapshot or {},
        run_id=run_id,
    )
    record = store.load_record()
    record.status = status
    store.save_record(record)
    return store, record


def _make_task(task_id: str, *, role: str = "backend_dev") -> Task:
    return Task(
        id=task_id, title="t", description="d", role=role,
        acceptance_criteria=["ok"],
        deliverables=[
            ArtifactSpec(path=f"out/{task_id}.txt", kind="text", description="x"),
        ],
        status=TaskStatus.SUCCEEDED,
    )


def _write_tasks(store: CheckpointStore, tasks: list[Task]) -> None:
    store.save_tasks(TaskBoard(tasks))


# ── diff_runs 纯函数 ──────────────────────────────────────────────────────


def test_identical_runs_have_no_diff(tmp_path):
    """两个同配置、无任务/产物/事件的 run → 各段全空。"""
    out = tmp_path / "o"
    sa, ra = _make_run(out, "run-a", config_snapshot={"default_model": "openai:m"})
    sb, rb = _make_run(out, "run-b", config_snapshot={"default_model": "openai:m"})
    diff = diff_runs(ra, rb, sa, sb)
    assert diff.run_a == "run-a" and diff.run_b == "run-b"
    assert diff.config_diff == {}
    assert diff.task_diff == {}
    assert diff.artifact_diff == {}
    assert diff.event_diff == {}
    assert diff.exit_diff == {}


def test_config_diff_reports_changed_fields(tmp_path):
    """不同 default_model / gate → config_diff 列出差异字段。"""
    out = tmp_path / "o"
    sa, ra = _make_run(out, "run-a", config_snapshot={
        "default_model": "openai:mini", "gate": "none", "mock": False})
    sb, rb = _make_run(out, "run-b", config_snapshot={
        "default_model": "openai:large", "gate": "none", "mock": False})
    diff = diff_runs(ra, rb, sa, sb)
    assert "default_model" in diff.config_diff
    assert diff.config_diff["default_model"] == ["openai:mini", "openai:large"]
    assert "gate" not in diff.config_diff  # 两侧相同，不列出


def test_task_count_diff(tmp_path):
    """任务数量不同 → task_diff 报告 count 与新增任务。"""
    out = tmp_path / "o"
    sa, ra = _make_run(out, "run-a")
    sb, rb = _make_run(out, "run-b")
    _write_tasks(sa, [_make_task("T-1"), _make_task("T-2")])
    _write_tasks(sb, [_make_task("T-1"), _make_task("T-2"), _make_task("T-3")])
    diff = diff_runs(ra, rb, sa, sb)
    assert diff.task_diff["count"] == [2, 3]
    assert diff.task_diff["added"] == ["T-3"]
    assert "removed" not in diff.task_diff


def test_task_removed_diff(tmp_path):
    """一侧有另一侧没有的任务 → removed。"""
    out = tmp_path / "o"
    sa, ra = _make_run(out, "run-a")
    sb, rb = _make_run(out, "run-b")
    _write_tasks(sa, [_make_task("T-1"), _make_task("T-X")])
    _write_tasks(sb, [_make_task("T-1")])
    diff = diff_runs(ra, rb, sa, sb)
    assert diff.task_diff["removed"] == ["T-X"]


def test_artifact_added_removed_modified(tmp_path):
    """产物按路径+大小比对：added / removed / modified。"""
    out = tmp_path / "o"
    sa, ra = _make_run(out, "run-a")
    sb, rb = _make_run(out, "run-b")
    (sa.artifacts_dir / "shared.txt").write_text("same", encoding="utf-8")
    (sa.artifacts_dir / "only-a.txt").write_text("aaa", encoding="utf-8")
    (sb.artifacts_dir / "shared.txt").write_text("different-content", encoding="utf-8")
    (sb.artifacts_dir / "only-b.txt").write_text("bbb", encoding="utf-8")
    diff = diff_runs(ra, rb, sa, sb)
    added = {i["path"] for i in diff.artifact_diff["added"]}
    removed = {i["path"] for i in diff.artifact_diff["removed"]}
    modified = {i["path"] for i in diff.artifact_diff["modified"]}
    assert added == {"only-b.txt"}
    assert removed == {"only-a.txt"}
    assert modified == {"shared.txt"}


def test_event_count_diff(tmp_path):
    """ledger 事件数不同 → event_diff 列出差异。"""
    out = tmp_path / "o"
    sa, ra = _make_run(out, "run-a")
    sb, rb = _make_run(out, "run-b")
    la = Ledger(sa.run_dir / "ledger.jsonl", run_id="run-a")
    la.emit("llm_call", role="x", model="m", prompt_tokens=1, completion_tokens=1,
            estimated_cost_usd=0.01, latency_ms=10, purpose="p")
    lb = Ledger(sb.run_dir / "ledger.jsonl", run_id="run-b")
    lb.emit("llm_call", role="x", model="m", prompt_tokens=1, completion_tokens=1,
            estimated_cost_usd=0.01, latency_ms=10, purpose="p")
    lb.emit("tool_call", tool="read_file", success=True, duration_ms=5)
    diff = diff_runs(ra, rb, sa, sb)
    assert diff.event_diff["event_counts"]["tool_call"] == [0, 1]


def test_exit_status_diff(tmp_path):
    """最终状态不同 → exit_diff.status 报告。"""
    out = tmp_path / "o"
    sa, ra = _make_run(out, "run-a", status=RunStatus.COMPLETED)
    sb, rb = _make_run(out, "run-b", status=RunStatus.FAILED)
    diff = diff_runs(ra, rb, sa, sb)
    assert diff.exit_diff["status"] == ["completed", "failed"]


# ── 终端输出 ───────────────────────────────────────────────────────────


def test_print_diff_shows_arrows(capsys):
    from agentcrew.cli.diff import RunDiff
    diff = RunDiff(
        run_a="a", run_b="b",
        config_diff={"gate": ["both", "none"]},
        task_diff={}, artifact_diff={}, event_diff={}, exit_diff={},
    )
    print_diff(diff)
    out = capsys.readouterr().out
    assert "gate" in out and "→" in out


def test_print_diff_identical(capsys):
    from agentcrew.cli.diff import RunDiff
    diff = RunDiff(run_a="a", run_b="b", config_diff={}, task_diff={},
                   artifact_diff={}, event_diff={}, exit_diff={})
    print_diff(diff)
    out = capsys.readouterr().out
    assert "无差异" in out


# ── CLI 集成 ─────────────────────────────────────────────────────────────


def test_diff_cli_happy_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    _make_run(tmp_path / "o", "run-a", config_snapshot={"default_model": "openai:mini"})
    _make_run(tmp_path / "o", "run-b", config_snapshot={"default_model": "openai:large"})
    code = cli.main(["diff", "run-a", "run-b"])
    assert code == 0
    out = capsys.readouterr().out
    assert "default_model" in out


def test_diff_cli_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    _make_run(tmp_path / "o", "run-a", config_snapshot={"default_model": "openai:mini"})
    _make_run(tmp_path / "o", "run-b", config_snapshot={"default_model": "openai:large"})
    code = cli.main(["diff", "run-a", "run-b", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)  # 合法 JSON
    assert data["run_a"] == "run-a" and data["run_b"] == "run-b"
    assert "default_model" in data["config_diff"]
    assert code == 0


def test_diff_missing_run_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    _make_run(tmp_path / "o", "run-a")
    code = cli.main(["diff", "run-a", "no-such-run"])
    assert code == EXIT_CODES["failed"] == 1
    assert "no-such-run" in capsys.readouterr().err
