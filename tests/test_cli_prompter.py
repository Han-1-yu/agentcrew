"""G3 人工闸门的交互实现（spec §10.3）。全部用注入的 input_fn，不读真实 stdin。

与 brief 的两处差异，均为「让实现与已提交接口一致」所必需：

1. ``test_plan_edit_returns_edited_tasks`` 断言 ``action == "edit"`` 而不是
   ``"approve"``。``Crew._plan_phase`` 只在 ``action == "edit"`` 时用
   ``decision.tasks`` 重建面板（见 ``agentcrew/crew/crew.py`` 与
   ``tests/test_crew.py::test_plan_gate_edit_uses_edited_tasks``）；返回
   ``approve`` + 编辑后的任务，编辑结果会被引擎**静默丢弃**。
2. ``test_plan_edit_validation_failure_reasks`` 用真正的迭代器喂答案
   （brief 里的 ``lambda _: next(iter(["e", "q"]))`` 每次都返回 ``"e"``，
   在「校验失败后重新提示」的实现上必然死循环）。
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from agentcrew.gate import cli_prompter
from agentcrew.gate.cli_prompter import CliPrompter, edit_tasks_in_editor
from agentcrew.runtime.checkpoint import RunRecord, RunStatus
from agentcrew.task.planner import PlanValidationError
from agentcrew.task.task import RunStats, Task


def mk(tid, role="backend_dev"):
    return Task(id=tid, title=f"任务{tid}", description="d", role=role,
                acceptance_criteria=["a"], deliverables=[])


def record():
    return RunRecord(run_id="R", project_goal="做个 Todo 应用",
                     status=RunStatus.RUNNING, created_at=datetime.now(),
                     updated_at=datetime.now(), config_snapshot={},
                     stats=RunStats(total_tasks=2, succeeded=2))


def prompter(answers):
    it = iter(answers)
    return CliPrompter(input_fn=lambda _: next(it))


# ── 计划卡点 ──────────────────────────────────────────────────────────────


async def test_plan_a_approves_and_returns_tasks():
    tasks = [mk("T-1"), mk("T-2")]
    d = await prompter(["a"]).ask_plan(tasks)
    assert d.action == "approve" and [t.id for t in d.tasks] == ["T-1", "T-2"]


async def test_plan_q_cancels():
    assert (await prompter(["q"]).ask_plan([mk("T-1")])).action == "cancel"


async def test_plan_s_saves_and_exits():
    assert (await prompter(["s"]).ask_plan([mk("T-1")])).action == "save_exit"


async def test_plan_reasks_on_invalid_input():
    d = await prompter(["zzz", "x", "a"]).ask_plan([mk("T-1")])
    assert d.action == "approve"


async def test_plan_empty_input_defaults_to_approve():
    assert (await prompter([""]).ask_plan([mk("T-1")])).action == "approve"


async def test_plan_accepts_uppercase_and_whitespace():
    assert (await prompter(["  A  "]).ask_plan([mk("T-1")])).action == "approve"


async def test_plan_edit_returns_edited_tasks():
    edited = [mk("T-1"), mk("T-3")]
    p = CliPrompter(input_fn=lambda _: "e",
                    edit_fn=lambda tasks: edited)
    d = await p.ask_plan([mk("T-1")])
    # action="edit" 是 Crew 采用 decision.tasks 的唯一信号（brief 写的是
    # "approve"，那会让编辑被引擎丢弃；见模块 docstring）。
    assert d.action == "edit" and [t.id for t in d.tasks] == ["T-1", "T-3"]


async def test_plan_edit_validation_failure_reasks():
    def bad_edit(tasks):
        raise PlanValidationError(["产物路径冲突：a.py 被 T-1 与 T-2 同时声明"])
    it = iter(["e", "q"])
    p = CliPrompter(input_fn=lambda _: next(it), edit_fn=bad_edit)
    assert (await p.ask_plan([mk("T-1")])).action == "cancel"


# ── 交付卡点 ──────────────────────────────────────────────────────────────


async def test_delivery_a_accepts():
    assert (await prompter(["a"]).ask_delivery(record())).action == "accept"


async def test_delivery_q_aborts():
    assert (await prompter(["q"]).ask_delivery(record())).action == "abort"


async def test_delivery_r_collects_task_ids():
    d = await prompter(["r", "T-003, T-005"]).ask_delivery(record())
    assert d.action == "rework" and d.task_ids == ["T-003", "T-005"]


async def test_delivery_r_with_empty_list_reasks():
    d = await prompter(["r", "", "a"]).ask_delivery(record())
    assert d.action == "accept"


async def test_delivery_empty_input_defaults_to_accept():
    assert (await prompter([""]).ask_delivery(record())).action == "accept"


async def test_delivery_reasks_on_invalid_input():
    d = await prompter(["nope", "a"]).ask_delivery(record())
    assert d.action == "accept"


# ── 编辑器路径 ────────────────────────────────────────────────────────────


def _fake_editor(monkeypatch, mutate):
    """把 ``subprocess.run`` 换成写文件的假编辑器；``mutate`` 收到已解析的 JSON。"""
    def fake_run(cmd, **kwargs):
        path = Path(cmd[1])
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(mutate(data), ensure_ascii=False),
                        encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(cli_prompter.subprocess, "run", fake_run)


def test_edit_tasks_in_editor_roundtrip(tmp_path, monkeypatch):
    """编辑后的 JSON 读回后重走 to_tasks → with_final_task → validate_plan。"""
    _fake_editor(monkeypatch, lambda data: {
        "tasks": [t for t in data["tasks"] if t["id"] != "T-001"]})
    edited = edit_tasks_in_editor([mk("T-001"), mk("T-002")], editor="ed")
    assert [t.id for t in edited] == ["T-002", "T-FINAL"]


def test_edit_tasks_in_editor_writes_only_draft_fields(monkeypatch):
    """写进编辑器的 JSON 必须是 ``PlanOutput`` 的形状（``extra="forbid"``）。"""
    seen: dict[str, object] = {}

    def mutate(data):
        seen.update(data)
        return data
    _fake_editor(monkeypatch, mutate)
    edit_tasks_in_editor([mk("T-001")], editor="ed")
    draft = seen["tasks"][0]
    assert set(draft) == {"id", "title", "description", "role",
                          "acceptance_criteria", "deliverables",
                          "dependencies", "priority", "brief"}


def test_edit_tasks_in_editor_raises_on_broken_json(monkeypatch):
    def fake_run(cmd, **kwargs):
        Path(cmd[1]).write_text("{ 不是 JSON", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(cli_prompter.subprocess, "run", fake_run)
    with pytest.raises(PlanValidationError):
        edit_tasks_in_editor([mk("T-001")], editor="ed")


def test_edit_tasks_in_editor_raises_on_conflict(monkeypatch):
    """冲突路径由 validate_plan 判定，异常类型必须是 PlanValidationError。"""
    from agentcrew.artifacts.spec import ArtifactSpec

    def mutate(data):
        for draft in data["tasks"]:
            draft["deliverables"] = [
                {"path": "shared.py", "kind": "python", "description": "x"}]
        return data
    _fake_editor(monkeypatch, mutate)
    tasks = [
        Task(id="T-1", title="a", description="d", role="backend_dev",
             acceptance_criteria=["a"],
             deliverables=[ArtifactSpec(path="a.py", kind="python", description="x")]),
        Task(id="T-2", title="b", description="d", role="backend_dev",
             acceptance_criteria=["a"],
             deliverables=[ArtifactSpec(path="b.py", kind="python", description="x")]),
    ]
    with pytest.raises(PlanValidationError):
        edit_tasks_in_editor(tasks, editor="ed")


def test_editor_command_prefers_env(monkeypatch):
    monkeypatch.setenv("AGENTCREW_EDITOR", "nano")
    monkeypatch.setenv("EDITOR", "vim")
    assert cli_prompter._editor_command(None) == "nano"
    assert cli_prompter._editor_command("code") == "code"
    monkeypatch.delenv("AGENTCREW_EDITOR")
    assert cli_prompter._editor_command(None) == "vim"
    monkeypatch.delenv("EDITOR")
    assert cli_prompter._editor_command(None) in ("notepad", "vi")
