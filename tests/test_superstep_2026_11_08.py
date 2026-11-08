"""P2-2：super-step 标记 + pending tool writes（崩溃半步可恢复）。

方案 §4.2 的结论是「不引 Temporal，用本地 JSONL 拿到 pending 恢复」。本文件钉住
三件事：

1. **标记的读写语义**：原子写、index 递增、坏文件不炸、正常收尾回 idle；
2. **pending 推导**：从 ledger 的 ``tool_call_started`` 与配对 ``tool_call`` 推出
   「开始了但没结束」的调用（原生按 ``call_id``、围栏按 ``fence_index``、老事件
   按工具名），且不会把已完成的误报成 pending；
3. **Crew 的恢复留痕**：首次运行不冒事件；崩在半步上才发 ``superstep_recovered``。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew, _Run
from agentcrew.runtime.checkpoint import CheckpointStore
from agentcrew.runtime.ledger import Ledger
from agentcrew.runtime.superstep import (
    SUPERSTEP_FILE,
    SuperStepJournal,
    SuperStepState,
)

# ── 1. 标记读写 ────────────────────────────────────────────────────────


def test_begin_and_end_roundtrip(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="R")
    journal = SuperStepJournal(tmp_path, ledger)
    assert journal.load() is None

    state = journal.begin(running=["T-1", "T-2"])
    assert state.index == 1 and state.phase == "dispatch"
    assert state.running_tasks == ["T-1", "T-2"]
    assert (tmp_path / SUPERSTEP_FILE).is_file()
    assert journal.load() == state

    journal.begin(running=["T-3"])
    assert journal.load().index == 2, "每轮自增"

    finished = journal.end()
    assert finished.phase == "idle" and finished.running_tasks == []
    assert finished.index == 2, "收尾不重置轮次，恢复时才知道跑到第几轮"

    # 事件也留了痕
    events = [e.event for e in ledger.events()]
    assert events.count("superstep_begin") == 2


def test_state_file_is_atomic_and_tolerates_garbage(tmp_path: Path):
    journal = SuperStepJournal(tmp_path)
    journal.begin(running=["T-1"])
    # 模拟被写坏的半截文件：load 必须返回 None，而不是把 resume 直接炸掉
    (tmp_path / SUPERSTEP_FILE).write_text('{"index": 3, "pha', encoding="utf-8")
    assert journal.load() is None
    # 坏文件也能被下一条 begin 覆盖回可用状态
    assert journal.begin().index == 1


def test_state_model_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        SuperStepState.model_validate({"index": 1, "nope": 2})


# ── 2. pending 推导 ────────────────────────────────────────────────────


def _journal(tmp_path: Path) -> tuple[SuperStepJournal, Ledger]:
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="R")
    return SuperStepJournal(tmp_path, ledger), ledger


def test_started_without_finish_is_pending(tmp_path: Path):
    journal, ledger = _journal(tmp_path)
    ledger.emit("tool_call_started", task_id="T-1", role="backend_dev",
                tool_name="run_python", call_id="c1", fence_index=0)
    pending = journal.pending_tool_calls()
    assert [(p.task_id, p.tool_name, p.call_id) for p in pending] == [
        ("T-1", "run_python", "c1")]


def test_finished_call_is_not_pending(tmp_path: Path):
    journal, ledger = _journal(tmp_path)
    ledger.emit("tool_call_started", task_id="T-1", role="r",
                tool_name="read_file", call_id="c1")
    ledger.emit("tool_call", task_id="T-1", role="r", tool_name="read_file",
                call_id="c1", success=True)
    assert journal.pending_tool_calls() == []


def test_fence_path_pairs_by_index(tmp_path: Path):
    """围栏路径没有 call_id，同一工具连调两次也必须分开配对。"""
    journal, ledger = _journal(tmp_path)
    for index in (0, 1):
        ledger.emit("tool_call_started", task_id="T-1", role="r",
                    tool_name="read_file", fence_index=index)
    ledger.emit("tool_call", task_id="T-1", role="r",
                tool_name="read_file", fence_index=0, success=True)
    pending = journal.pending_tool_calls()
    assert len(pending) == 1
    assert pending[0].tool_name == "read_file"


def test_legacy_events_without_keys_pair_by_tool_name(tmp_path: Path):
    """升级前的 ledger 没有 call_id / fence_index，按工具名兜底配对。"""
    journal, ledger = _journal(tmp_path)
    ledger.emit("tool_call_started", task_id="T-1", role="r", tool_name="grep")
    ledger.emit("tool_call", task_id="T-1", role="r", tool_name="grep",
                success=True)
    assert journal.pending_tool_calls() == []


def test_pending_is_capped(tmp_path: Path):
    journal, ledger = _journal(tmp_path)
    for index in range(40):
        ledger.emit("tool_call_started", task_id=f"T-{index}", role="r",
                    tool_name="read_file", fence_index=index)
    assert len(journal.pending_tool_calls(limit=5)) == 5


def test_recover_summary_shape(tmp_path: Path):
    journal, ledger = _journal(tmp_path)
    journal.begin(running=["T-9"])
    ledger.emit("tool_call_started", task_id="T-9", role="r",
                tool_name="write_file", call_id="c9")
    summary = journal.recover()
    assert summary["last_index"] == 1
    assert summary["last_phase"] == "dispatch"
    assert summary["running_tasks"] == ["T-9"]
    assert summary["pending_tool_calls"][0]["tool_name"] == "write_file"


# ── 3. Crew 侧恢复留痕 ────────────────────────────────────────────────


def _make_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Run:
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    store = CheckpointStore.create(
        tmp_path / "o", project_goal="g", config_snapshot={},
        run_id="20260101-000000-p22")
    ledger = Ledger(store.run_dir / "ledger.jsonl", run_id=store.run_dir.name)
    return _Run(
        goal="g", record=store.load_record(), checkpoint=store,
        ledger=ledger, store=object(), models=object(), agents=object(),
        base_stats=store.load_record().stats,
        journal=SuperStepJournal(store.run_dir, ledger),
    )


def test_fresh_run_emits_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = _make_run(tmp_path, monkeypatch)
    crew = Crew(Settings.load())
    assert crew._recover_superstep(run) is None
    assert [e.event for e in run.ledger.events("superstep_recovered")] == []


def test_interrupted_run_emits_recovered_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = _make_run(tmp_path, monkeypatch)
    # 模拟崩溃现场：本轮开始于派发中途，且一次 run_python 只写了 started
    run.journal.begin(running=["T-1"])
    run.ledger.emit("tool_call_started", task_id="T-1", role="backend_dev",
                    tool_name="run_python", call_id="c1")
    crew = Crew(Settings.load())
    summary = crew._recover_superstep(run)
    assert summary is not None
    event = list(run.ledger.events("superstep_recovered"))[0]
    assert event.data["last_phase"] == "dispatch"
    assert event.data["pending_count"] == 1
    assert event.data["pending_tool_calls"][0]["tool_name"] == "run_python"
    assert event.data["running_tasks"] == ["T-1"]


def test_clean_idle_run_emits_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = _make_run(tmp_path, monkeypatch)
    run.journal.begin(running=["T-1"])
    run.journal.end()
    crew = Crew(Settings.load())
    assert crew._recover_superstep(run) is None
    assert [e.event for e in run.ledger.events("superstep_recovered")] == []


def test_recovered_event_is_registered_in_webhook_filter_docs():
    """事件名进入可订阅集合（Webhook ``events`` 过滤按名字匹配，不需要白名单）。

    这条断言只是防止有人把事件改名后忘记同步文档：README/OPERATIONS 里写的名字
    必须与代码一致。
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    text = (root / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "superstep_recovered" in text
