"""2026-11-06 按板块补全的回归测试。

钉住本轮四类改动，防止后续重构把它们悄悄还原：

1. 配置 / CLI：坏 YAML 收敛成 ValueError；`--max-cost` / `--max-llm-calls` /
   `--port` 的数值校验；`switch` 的 run_id 边界与损坏覆盖文件拒绝覆写；
2. HTTP API：options 白名单、body 形状、DELETE 越界、reason phrase、
   `/status` 磁盘回落；
3. 引擎 / 工具：状态跃迁刷新停滞计时、符号链接逐段拒绝、
   `run_python` 的 timeout 校验、`max_review_attempts` 死字段接线；
4. 前端统一组件：states.js / validate.js 的存在与关键导出（静态护栏）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import main as cli
from agentcrew.api import http as http_transport
from agentcrew.api.server import ApiServer, _validate_run_options
from agentcrew.config import Settings
from agentcrew.crew import scheduler as scheduler_module
from agentcrew.crew.crew import EXIT_CODES
from agentcrew.gate.gatekeeper import Gatekeeper
from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.task.task import TaskStatus
from agentcrew.tools import builtins as builtins_module

STUDIO = Path(__file__).resolve().parents[1] / "studio"


# ── 1. 配置 / CLI ──────────────────────────────────────────────────────────


def test_broken_yaml_is_reported_as_value_error(tmp_path, monkeypatch):
    """agentcrew.yaml 语法错误必须收敛成 ValueError，不能让所有子命令栈崩。"""
    bad = tmp_path / "agentcrew.yaml"
    bad.write_text("models:\n  default: ok\n::: not yaml :::\n", encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(bad))
    with pytest.raises(ValueError, match="YAML 解析失败"):
        Settings.load()


def test_broken_yaml_cli_reports_config_error_not_traceback(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "agentcrew.yaml"
    bad.write_text("a: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(bad))
    assert cli.main(["roles"]) == EXIT_CODES["config"]
    err = capsys.readouterr().err
    assert "配置错误" in err and "Traceback" not in err


def _nums(**kw) -> argparse.Namespace:
    base = dict(command="run", max_concurrency=None, context_budget=None,
                max_cost=None, max_llm_calls=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_invalid_max_cost_rejected(value):
    # NaN / inf 会让 `total_cost > limit` 恒为 False（上限静默失效），负值
    # 会让第一次 LLM 调用就以「预算耗尽」失败。
    assert cli._invalid_numbers(_nums(max_cost=value)) is not None


def test_invalid_max_llm_calls_rejected():
    assert cli._invalid_numbers(_nums(max_llm_calls=-5)) is not None


def test_valid_numeric_flags_still_pass():
    assert cli._invalid_numbers(_nums(max_cost=0, max_llm_calls=0,
                                      max_concurrency=4, context_budget=8000)) is None


def test_serve_rejects_out_of_range_port():
    assert cli.main(["serve", "--port", "70000"]) == EXIT_CODES["config"]


def test_serve_refuses_auth_without_any_key(monkeypatch):
    monkeypatch.setenv("AGENTCREW_API_AUTH_ENABLED", "1")
    assert cli.main(["serve", "--port", "18765"]) == EXIT_CODES["config"]


def test_switch_rejects_run_id_outside_output_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    (tmp_path / "o").mkdir()
    assert cli.main(["switch", "../outside", "backend_dev", "default"]) == EXIT_CODES["config"]
    assert "非法 run_id" in capsys.readouterr().err


def test_switch_refuses_to_overwrite_corrupt_overrides(tmp_path, monkeypatch, capsys):
    """覆盖文件损坏时 fail loud，不静默清空已有内容再写回。"""
    out = tmp_path / "o"
    store = CheckpointStore.create(out, project_goal="g", config_snapshot={},
                                   run_id="20260101-000000-aaaa")
    overrides = store.run_dir / "binding_overrides.json"
    overrides.write_text("{ broken json", encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(out))
    assert cli.main(["switch", "20260101-000000-aaaa", "backend_dev", "default"]) \
        == EXIT_CODES["config"]
    assert overrides.read_text(encoding="utf-8") == "{ broken json"
    assert "覆盖文件损坏" in capsys.readouterr().err


# ── 2. HTTP API ────────────────────────────────────────────────────────────


def test_reason_phrases_cover_all_emitted_statuses():
    for code, phrase in {409: "Conflict", 503: "Service Unavailable",
                         504: "Gateway Timeout"}.items():
        assert http_transport.REASONS[code] == phrase


@pytest.mark.parametrize("options", [
    {"totally_unknown_key": 42},
    {"mock": "false"},              # 字符串 truthy，会把 mock 强制打开
    {"max_concurrency": -1},
    {"max_cost_usd": float("nan")},
    {"max_cost_usd": "oops"},
    {"max_llm_calls": 1.5},
    {"gate_config": []},
])
def test_run_options_reject_unknown_keys_and_bad_values(options):
    assert _validate_run_options(options) is not None


@pytest.mark.parametrize("options", [
    {},
    {"mock": False, "default_model": "deepseek:deepseek-chat"},
    {"max_concurrency": 4, "max_cost_usd": 1.5, "max_llm_calls": 0},
    {"max_cost_usd": None, "model_tiers": {}, "gate_config": {"g1_enabled": True}},
])
def test_run_options_accept_valid_values(options):
    assert _validate_run_options(options) is None


async def _http(port: int, method: str, path: str,
                raw: bytes | None = None) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if raw:
        head += f"Content-Length: {len(raw)}\r\n"
    writer.write(head.encode() + b"\r\n" + (raw or b""))
    await writer.drain()
    data = await reader.read(65536)
    writer.close()
    await writer.wait_closed()
    text = data.decode("utf-8", errors="replace")
    first, _, body = text.partition("\r\n\r\n")
    status = int(first.split("\r\n")[0].split(" ")[1])
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, {}


@pytest.mark.asyncio
async def test_post_endpoints_reject_non_object_body(tmp_path):
    srv = ApiServer(port=0, output_dir=tmp_path)
    await srv.start()
    try:
        port = srv._server.sockets[0].getsockname()[1]
        cases = [
            ("POST", "/runs/R/agents", b"[]"),
            ("POST", "/runs/R/tasks/T-1/agent", b"null"),
            ("POST", "/runs/R/events", b'"x"'),
            ("POST", "/runs", b"[]"),
        ]
        for method, path, body in cases:
            status, payload = await _http(port, method, path, body)
            assert status == 400, (path, status, payload)
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_reset_must_be_boolean():
    srv = ApiServer(port=0)
    await srv.start()
    try:
        port = srv._server.sockets[0].getsockname()[1]
        body = json.dumps({"role": "backend_dev", "reset": "false"}).encode()
        status, _ = await _http(port, "POST", "/runs/R/agents", body)
        assert status == 400
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_delete_invalid_run_id_is_400_not_500(tmp_path):
    srv = ApiServer(port=0, output_dir=tmp_path)
    await srv.start()
    try:
        port = srv._server.sockets[0].getsockname()[1]
        status, payload = await _http(port, "DELETE", "/runs/..%2Fetc?confirm=1")
        assert status == 400, payload
        assert "非法 run_id" in payload.get("error", "")
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_status_falls_back_to_disk_after_restart(tmp_path):
    store = CheckpointStore.create(tmp_path, project_goal="disk goal",
                                   config_snapshot={},
                                   run_id="20260101-000000-bbbb")
    record = store.load_record()
    record.status = RunStatus.COMPLETED
    store.save_record(record)
    srv = ApiServer(port=0, output_dir=tmp_path)
    await srv.start()
    try:
        port = srv._server.sockets[0].getsockname()[1]
        status, payload = await _http(
            port, "GET", "/runs/20260101-000000-bbbb/status")
        assert status == 200, payload
        assert payload["status"] == "completed"
        assert payload["goal"] == "disk goal"
        assert payload["source"] == "disk"
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_trailing_slash_and_query_on_runs_are_accepted(tmp_path):
    srv = ApiServer(port=0, output_dir=tmp_path)
    await srv.start()
    try:
        port = srv._server.sockets[0].getsockname()[1]
        for path in ("/runs/", "/runs?foo=1"):
            status, _ = await _http(port, "GET", path)
            assert status == 200, path
    finally:
        await srv.stop()


# ── 3. 引擎 / 工具 ─────────────────────────────────────────────────────────


def test_transition_refreshes_stall_clock():
    """慢门禁期间只有状态跃迁、没有完成事件，也必须刷新 _last_progress_at。"""
    scheduler = object.__new__(scheduler_module.Scheduler)
    board = MagicMock()
    board.get.return_value = SimpleNamespace(status=SimpleNamespace(value="review"))
    scheduler._board = board
    scheduler._ledger = MagicMock()
    scheduler._last_progress_at = 0.0
    scheduler._transition("T-1", TaskStatus.REWORK, reason="gate-rework")
    assert scheduler._last_progress_at > 0.0


def test_crosses_link_detects_link_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        builtins_module, "is_link_like",
        lambda p: "link" in Path(p).parts,
    )
    assert builtins_module._crosses_link(tmp_path, Path("link/secret.txt")) is True
    assert builtins_module._crosses_link(tmp_path, Path("src/app.py")) is False


def test_resolve_rejects_path_through_link(monkeypatch, tmp_path):
    monkeypatch.setattr(
        builtins_module, "is_link_like",
        lambda p: "link" in Path(p).parts,
    )
    assert builtins_module._resolve(tmp_path, "link/secret.txt") is None
    assert builtins_module._resolve(tmp_path, "src/app.py") == tmp_path / "src/app.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", -1, float("nan"), 0, 1e9])
async def test_run_python_rejects_invalid_timeout(tmp_path, bad):
    tool = builtins_module.RunPythonTool(workdir=tmp_path)
    out = await tool.run(code="print('x')", timeout=bad)
    assert out.startswith("错误：")


@pytest.mark.asyncio
async def test_run_python_accepts_valid_timeout(tmp_path):
    tool = builtins_module.RunPythonTool(workdir=tmp_path)
    out = await tool.run(code="print(7)", timeout=2)
    assert "7" in out and "exit_code=0" in out


def _gatekeeper(cfg):
    return Gatekeeper(
        MagicMock(), MagicMock(), MagicMock(),
        gate="none", ledger=MagicMock(), gate_config=cfg,
    )


def test_max_review_attempts_now_caps_rework_rounds():
    task = SimpleNamespace(id="T-1", rework_count=1, max_reworks=5)
    assert _gatekeeper({"max_review_attempts": 1})._reject(task, []) is TaskStatus.FAILED
    # 未配置时与改造前一致：默认 3 > 任务 max_reworks=2，仍允许返工。
    task2 = SimpleNamespace(id="T-2", rework_count=1, max_reworks=2)
    assert _gatekeeper({})._reject(task2, []) is TaskStatus.REWORK


def test_max_review_attempts_bad_value_falls_back_to_default():
    assert _gatekeeper({"max_review_attempts": "oops"})._max_review_attempts == 3


# ── 4. 前端统一组件（静态护栏） ────────────────────────────────────────────


def test_studio_shared_modules_exist_and_export_expected_names():
    states = (STUDIO / "src/ui/states.js").read_text(encoding="utf-8")
    validate = (STUDIO / "src/lib/validate.js").read_text(encoding="utf-8")
    for name in ("loadingState", "emptyState", "errorState", "isStaleRender"):
        assert name in states
    for name in ("readNumber", "readUrl", "readAlias", "showFieldError"):
        assert name in validate
    css = (STUDIO / "styles/components/empty.css").read_text(encoding="utf-8")
    assert ".page-state" in css
