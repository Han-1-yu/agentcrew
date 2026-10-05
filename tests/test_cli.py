"""Task 32：CLI 入口（spec §15.1）。

用例覆盖 brief 给出的十条，另补三条把「退出码而非崩溃」「人工闸门不静默降级」
「clean 真删」「cancel 落盘」钉死：

- ``run`` 既不给 ``--goal`` 也不给 ``--demo``（或两者都给）→ stderr 报错 + 退出码 3；
- 显式 ``--gate both`` 但无 TTY：**不静默换 NullPrompter**，而是把闸门显式降级为
  ``none`` 并告知（Task 27 关掉的那条 fail-open 通道不得从 CLI 重新打开）；
- ``clean --dry-run`` 只列不删（brief），``clean`` 不带 dry-run 真删旧 run；
- ``cancel <run_id>`` 把 ``run.json`` 的 ``status`` 置为 ``cancelled``，供下次
  resume 直接收尾（spec §13.4）。

``test_demo_mock_run_end_to_end`` / ``test_status_reads_completed_run`` 会真正跑完整
mock 流程（约 1–2 秒），是本节最有价值的两条：CLI 的全部装配只有在这里才被真正执行。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from rich.console import Console

import main as cli
from agentcrew.config import Settings
from agentcrew.crew.crew import EXIT_CODES, PlanFailedError, RunCancelled
from agentcrew.gate.cli_prompter import CliPrompter
from agentcrew.gate.gatekeeper import NullPrompter
from agentcrew.models.factory import ContextBudgetError, ModelFactoryError
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunRecord,
    RunStatus,
)
from agentcrew.task.task import RunStats
from agentcrew.task.task_board import TaskBoard
from examples.todo_web_project import PROJECT_GOAL

ROOT = Path(__file__).resolve().parents[1]


# ── 解析与分派 ────────────────────────────────────────────────────────────


def test_parser_has_all_subcommands():
    p = cli.build_parser()
    subs = {a.dest for a in p._subparsers._group_actions}
    assert "command" in subs
    for name in ("run", "resume", "status", "roles", "clean", "cancel"):
        assert p.parse_args([name] + (["R1"] if name in
                                      ("resume", "status", "cancel") else [])).command == name


def test_run_requires_goal_or_demo(capsys):
    assert cli.main(["run"]) == EXIT_CODES["config"] == 3
    assert "--goal" in capsys.readouterr().err


def test_goal_and_demo_are_mutually_exclusive(capsys):
    assert cli.main(["run", "--goal", "x", "--demo"]) == 3
    assert "--goal" in capsys.readouterr().err


def test_no_args_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_no_args_shows_role_count_and_recent_runs(tmp_path, monkeypatch, capsys):
    """spec §15.1：裸命令 = 帮助 + 已注册角色数 + 最近 3 次 run 概览。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    for run_id in ("20260101-000000-aaaa", "20260102-000000-bbbb",
                   "20260103-000000-cccc", "20260104-000000-dddd"):
        _fake_run_dir(tmp_path / "o", run_id)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower()
    assert "已注册角色 8 个" in out
    for run_id in ("20260104-000000-dddd", "20260103-000000-cccc",
                   "20260102-000000-bbbb"):
        assert run_id in out
    assert "20260101-000000-aaaa" not in out          # 只列最近 3 个


def test_no_args_without_any_run_still_returns_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    assert cli.main([]) == 0
    assert "最近 3 次 run" in capsys.readouterr().out


def test_unknown_command_is_rejected():
    """未知子命令交给 argparse（退出码 2），绝不静默当成 ``run``。"""
    with pytest.raises(SystemExit) as exc:
        cli.main(["frobnicate"])
    assert exc.value.code == 2


# ── 退出码与闸门选择 ──────────────────────────────────────────────────────


def test_exit_codes_come_from_crew():
    assert cli.EXIT_CODES is EXIT_CODES


def test_gate_none_and_auto_use_null_prompter():
    for gate in ("none",):
        chosen, prompter, downgraded = cli._resolve_gate(gate, interactive=True)
        assert chosen == "none" and isinstance(prompter, NullPrompter)
        assert not downgraded


def test_human_gate_on_tty_uses_cli_prompter():
    chosen, prompter, downgraded = cli._resolve_gate("both", interactive=True)
    assert chosen == "both" and isinstance(prompter, CliPrompter)
    assert not downgraded


def test_human_gate_without_tty_downgrades_explicitly():
    """无 TTY 时降级为 ``none``（且带 ``downgraded`` 标记），不静默塞 NullPrompter。"""
    chosen, prompter, downgraded = cli._resolve_gate("plan", interactive=False)
    assert chosen == "none"
    assert isinstance(prompter, NullPrompter)
    assert downgraded


def test_auto_wins_over_gate_flag():
    assert cli._effective_gate("both", auto=True) == "none"
    assert cli._effective_gate("both", auto=False) == "both"
    assert cli._effective_gate("none", auto=False) == "none"


# ── 只读子命令 ────────────────────────────────────────────────────────────


def test_roles_lists_eight(capsys):
    assert cli.main(["roles"]) == 0
    out = capsys.readouterr().out
    for key in ("project_manager", "product_manager", "architect", "backend_dev",
                "frontend_dev", "qa_engineer", "devops_engineer", "technical_writer"):
        assert key in out


def test_roles_shows_contract_columns(capsys):
    """roles 是 Task 34 README 指向的入口：职责 / 边界 / 产出都要在。"""
    cli.main(["roles"])
    out = capsys.readouterr().out
    assert "职责" in out and "边界" in out and "产出" in out


def _fake_run_dir(output_dir: Path, run_id: str, status: RunStatus = RunStatus.RUNNING) -> Path:
    """建一个只含 run.json 的最小 run 目录（status / cancel / clean 用）。"""
    store = CheckpointStore.create(
        output_dir, project_goal="g", config_snapshot={}, run_id=run_id)
    record = store.load_record()
    record.status = status
    store.save_record(record)
    return store.run_dir


def test_status_tolerates_missing_tasks_json(tmp_path, monkeypatch, capsys):
    """规划就失败的 run 没有 tasks.json：status 照常打印记录，不抛。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    _fake_run_dir(tmp_path / "o", "20260101-000000-aaaa")
    assert cli.main(["status", "20260101-000000-aaaa"]) == 0
    out = capsys.readouterr().out
    assert "20260101-000000-aaaa" in out and "running" in out


def test_status_missing_run_reports_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    assert cli.main(["status", "no-such-run"]) == EXIT_CODES["config"]
    assert "no-such-run" in capsys.readouterr().err


def test_cancel_marks_run_cancelled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    run_dir = _fake_run_dir(tmp_path / "o", "20260101-000000-bbbb")
    assert cli.main(["cancel", "20260101-000000-bbbb"]) == 0
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "cancelled"
    assert "cancelled" in capsys.readouterr().out


def test_clean_removes_old_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    for run_id in ("20260101-000000-aaaa", "20260102-000000-bbbb",
                   "20260103-000000-cccc"):
        _fake_run_dir(tmp_path / "o", run_id)
    assert cli.main(["clean", "--keep", "1"]) == 0
    remaining = [p.name for p in (tmp_path / "o").iterdir()]
    assert remaining == ["20260103-000000-cccc"]
    assert "20260103-000000-cccc" not in capsys.readouterr().out


def test_clean_dry_run_lists_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    cli.main(["run", "--demo", "--mock", "--auto", "--no-live"])
    assert cli.main(["clean", "--keep", "0", "--dry-run"]) == 0
    assert next((tmp_path / "o").iterdir(), None) is not None   # 没被删
    assert "dry-run" in capsys.readouterr().out.lower()


# ── 完整流程 ──────────────────────────────────────────────────────────────


def test_yaml_api_key_never_reaches_output_or_stderr(tmp_path, monkeypatch, capfd):
    """I1：``agentcrew.yaml`` 里写的密钥不能出现在任何落盘文件或终端里。

    ``provider_overrides`` 是自由形状的用户输入，修复前它会**明文**进
    ``run.json`` 的 ``config_snapshot``（第一处泄漏），而 ``resume`` 的配置差异行
    会把它打到 stderr（第二处）。这里从 CLI 顶层走一遍：配置读取期就该被拒，
    退出码 3，密钥在 stdout/stderr 与 ``output/`` 下任何文件里都找不到。
    """
    secret = "sk-YAMLSECRET_0123456789abcdef"
    cfg = tmp_path / "agentcrew.yaml"
    cfg.write_text(
        f"models:\n  providers:\n    openai:\n      api_key: {secret}\n",
        encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(cfg))
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.chdir(ROOT)

    code = cli.main(["run", "--demo", "--mock", "--auto", "--no-live"])
    captured = capfd.readouterr()

    assert code == EXIT_CODES["config"] == 3
    assert secret not in captured.out and secret not in captured.err
    assert "api_key" in captured.err          # 报错必须点名是哪个字段
    output_dir = tmp_path / "o"
    leaked = [
        str(path)
        for path in (output_dir.rglob("*") if output_dir.exists() else [])
        if path.is_file()
        and secret in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert leaked == [], f"密钥落进了这些文件：{leaked}"


def test_config_diff_redacts_secrets_in_an_old_snapshot(tmp_path, monkeypatch):
    """第二处泄漏点：``resume`` 把差异行打到 stderr，旧快照可能是明文。"""
    secret = "sk-OLDSNAPSHOT_0123456789abcdef"
    old = {"provider_overrides": {"openai": {"api_key": secret}}}
    current = Settings.load().model_copy(update={
        "provider_overrides": {"openai": {"implementation": "mock"}}})
    lines = cli._config_diff(old, current)
    assert lines, "两边的 provider_overrides 不同，差异行不该为空"
    assert secret not in "\n".join(lines)
    assert "***" in "\n".join(lines)


def test_demo_mock_run_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.chdir(ROOT)
    code = cli.main(["run", "--demo", "--mock", "--auto", "--no-live"])
    assert code == 0
    run_dirs = list((tmp_path / "o").iterdir())
    assert len(run_dirs) == 1
    art = run_dirs[0] / "artifacts"
    assert (art / "README.md").exists() and (art / "DELIVERY.md").exists()
    assert (art / "backend" / "app" / "main.py").exists()


def test_status_reads_completed_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.chdir(ROOT)
    assert cli.main(["run", "--demo", "--mock", "--auto", "--no-live"]) == 0
    rid = next((tmp_path / "o").iterdir()).name
    assert cli.main(["status", rid]) == 0
    out = capsys.readouterr().out
    assert rid in out


def test_run_prints_artifacts_dir(tmp_path, monkeypatch, capsys):
    """spec §7.4：退出时必定打印产物目录的绝对路径；``--demo`` 用内置目标。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.chdir(ROOT)
    cli.main(["run", "--demo", "--mock", "--auto", "--no-live"])
    out = capsys.readouterr().out
    assert str(tmp_path / "o") in out and "artifacts" in out
    record = json.loads(
        (next((tmp_path / "o").iterdir()) / "run.json").read_text(encoding="utf-8"))
    assert record["project_goal"] == PROJECT_GOAL


# ── 结束方式 → 退出码（要求 4 / 5）────────────────────────────────────────


class _StubCrew:
    """``_drive`` / ``_run_exit_code`` 的最小替身：只用到 ``checkpoint`` / ``stalled``。"""

    def __init__(self, checkpoint=None, *, stalled: bool = False) -> None:
        self.checkpoint = checkpoint
        self.stalled = stalled


def _hook() -> cli._BoardHook:
    return cli._BoardHook("g", enabled=False, console=Console())


def _record(status: RunStatus, **stats: int) -> RunRecord:
    now = datetime.now()
    return RunRecord(
        run_id="R", project_goal="g", status=status, created_at=now, updated_at=now,
        config_snapshot={}, stats=RunStats(**stats))


def test_run_failed_tasks_map_to_1():
    assert cli._run_exit_code(
        _StubCrew(), _record(RunStatus.FAILED, failed=1)) == EXIT_CODES["failed"] == 1


def test_run_blocked_tasks_map_to_1():
    assert cli._run_exit_code(
        _StubCrew(), _record(RunStatus.FAILED, blocked=1)) == 1


def test_completed_run_maps_to_0():
    assert cli._run_exit_code(
        _StubCrew(), _record(RunStatus.COMPLETED, succeeded=3)) == EXIT_CODES["ok"] == 0


def test_cancelled_run_with_prior_failures_exits_130():
    """判定顺序必须先看状态：审计记录里 run_finished 写的是 130，进程不能退 1。"""
    record = _record(RunStatus.CANCELLED, failed=2, blocked=1, skipped=1)
    assert cli._run_exit_code(_StubCrew(), record) == EXIT_CODES["cancelled"] == 130


def test_interrupted_run_exits_130():
    assert cli._run_exit_code(
        _StubCrew(), _record(RunStatus.INTERRUPTED, failed=1)) == 130


def test_stalled_maps_to_4():
    crew = _StubCrew(stalled=True)
    assert cli._run_exit_code(crew, _record(RunStatus.FAILED)) == EXIT_CODES["stalled"] == 4


def test_plan_failure_maps_to_2(capsys):
    def boom():
        raise PlanFailedError(["产物路径冲突"])
    assert cli._drive(_StubCrew(), boom, hook=_hook(), console=Console()) == 2
    assert "产物路径冲突" in capsys.readouterr().err


def test_config_error_maps_to_3(capsys):
    def boom():
        raise ModelFactoryError("缺少 OPENAI_API_KEY")
    assert cli._drive(_StubCrew(), boom, hook=_hook(), console=Console()) == 3
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_context_budget_error_maps_to_3():
    def boom():
        raise ContextBudgetError("m", 99999, 8192, 4096)
    assert cli._drive(_StubCrew(), boom, hook=_hook(), console=Console()) == 3


def test_run_cancelled_maps_to_130_and_points_at_resume(tmp_path, capsys):
    store = CheckpointStore(_fake_run_dir(tmp_path / "o", "20260101-000000-dddd"))
    def boom():
        raise RunCancelled("计划卡点取消（cancel）")
    code = cli._drive(_StubCrew(store), boom, hook=_hook(), console=Console())
    assert code == EXIT_CODES["cancelled"] == 130
    assert "20260101-000000-dddd" in capsys.readouterr().err


def test_keyboard_interrupt_maps_to_130_and_points_at_resume(tmp_path, capsys):
    """Ctrl-C：checkpoint 由 Crew 落盘，CLI 只负责打印 resume 命令并返回 130。"""
    store = CheckpointStore(_fake_run_dir(tmp_path / "o", "20260101-000000-eeee"))
    def boom():
        raise KeyboardInterrupt()
    code = cli._drive(_StubCrew(store), boom, hook=_hook(), console=Console())
    assert code == EXIT_CODES["cancelled"] == 130
    assert "python main.py resume 20260101-000000-eeee" in capsys.readouterr().err


def test_plan_gate_cancel_does_not_suggest_resume(tmp_path, capsys):
    """计划卡点 cancel 落盘为 CANCELLED：续跑没有意义，别喊 resume。"""
    store = CheckpointStore(
        _fake_run_dir(tmp_path / "o", "20260101-000000-gggg", RunStatus.CANCELLED))
    def boom():
        raise RunCancelled("计划卡点取消（cancel），run 结束为 cancelled")
    assert cli._drive(_StubCrew(store), boom, hook=_hook(), console=Console()) == 130
    err = capsys.readouterr().err
    assert "无需 resume" in err and "python main.py resume" not in err


def test_second_ctrl_c_does_not_traceback(monkeypatch, capsys):
    """第二次 Ctrl-C 落在分派层也不留 traceback。"""
    def boom(args: argparse.Namespace) -> int:
        raise KeyboardInterrupt()
    monkeypatch.setitem(cli._HANDLERS, "roles", boom)
    assert cli.main(["roles"]) == EXIT_CODES["cancelled"]
    assert "Traceback" not in capsys.readouterr().err


def test_resume_integrity_error_reports_config(tmp_path, monkeypatch, capsys):
    """run 目录与 MANIFEST/配置不一致时：可读消息 + 退出码 3，而不是 traceback。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    store = CheckpointStore(_fake_run_dir(tmp_path / "o", "20260101-000000-ffff"))
    store.save_tasks(TaskBoard([]))

    async def boom(self):
        raise ValueError("MANIFEST.run_id='x' 与本次 run_id='y' 不一致")
    monkeypatch.setattr(cli.Crew, "resume", boom)
    assert cli.main(["resume", "20260101-000000-ffff", "--force"]) == EXIT_CODES["config"]
    assert "MANIFEST" in capsys.readouterr().err


# ── 脚本入口 ──────────────────────────────────────────────────────────────


def test_module_runs_as_script():
    r = subprocess.run([sys.executable, str(ROOT / "main.py"), "roles"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and "architect" in r.stdout


def test_help_works():
    r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--help"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0
