"""R4 审批能力的接口层验收：CLI 跨进程链路 + HTTP API + SSE 状态。

与 test_flow_runtime.py 的分工：那里直接调 Crew API 验证引擎，这里验证**人在
另一个进程 / 另一条通道**里能读到审批单、写裁决、再让 run 从卡点继续。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentcrew.api.server import ApiServer, _validate_run_options
from agentcrew.gate.deferred import DeferredPrompter
from agentcrew.runtime.approvals import ApprovalStore
from agentcrew.runtime.checkpoint import RunStatus
from main import _resolve_gate

ROOT = Path(__file__).resolve().parents[1]


def _cli(args: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    clean_env = {
        k: v for k, v in os.environ.items() if not k.startswith("AGENTCREW_")
    }
    clean_env["PYTHONPATH"] = str(ROOT)
    clean_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        cwd=str(ROOT), env=clean_env, capture_output=True, text=True,
        encoding="utf-8", timeout=90,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ── 配置与装配 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["interactive", "deferred"])
def test_run_options_accept_approval_mode(mode):
    assert _validate_run_options({"approval_mode": mode}) is None


@pytest.mark.parametrize("value", ["defer", 1, None, True, ""])
def test_run_options_reject_bad_approval_mode(value):
    assert _validate_run_options({"approval_mode": value}) is not None


def test_deferred_gate_does_not_downgrade_without_tty():
    gate, prompter, downgraded = _resolve_gate(
        "plan", interactive=False, approval_mode="deferred"
    )
    assert gate == "plan"
    assert isinstance(prompter, DeferredPrompter)
    assert downgraded is False


def test_interactive_gate_still_downgrades_without_tty():
    gate, prompter, downgraded = _resolve_gate("plan", interactive=False)
    assert gate == "none" and downgraded is True
    assert not isinstance(prompter, DeferredPrompter)


# ── CLI：真的跨进程 ────────────────────────────────────────────────────

def test_cli_deferred_approval_roundtrip(tmp_path: Path):
    out = tmp_path / "o"
    env = {
        "AGENTCREW_OUTPUT_DIR": str(out),
        "AGENTCREW_GATE": "plan",
        "AGENTCREW_APPROVAL_MODE": "deferred",
    }
    common = ["--no-live", "--gate", "plan", "--approval-mode", "deferred"]

    code, stdout, stderr = _cli(
        ["run", "--sample", "linear", *common], env
    )
    assert code == 0, f"stdout={stdout[-1200:]}\nstderr={stderr[-1200:]}"
    runs = sorted(p for p in out.iterdir() if p.is_dir())
    assert len(runs) == 1
    run_dir = runs[0]
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == RunStatus.AWAITING_APPROVAL.value

    code, stdout, stderr = _cli(["approvals", run_dir.name], env)
    assert code == 0, stderr
    match = re.search(r"AP-[0-9a-f]+", stdout)
    assert match, stdout
    approval_id = match.group(0)
    assert "pending" in stdout

    code, stdout, stderr = _cli(
        ["approve", run_dir.name, approval_id, "--decision", "approve",
         "--by", "pytest"], env
    )
    assert code == 0, stderr
    assert "已裁决" in stdout
    assert ApprovalStore(run_dir).get(approval_id).status == "decided"

    # 第三次进程：resume 必须回到计划卡点并跑完，而不是重拆或绕过审批
    code, stdout, stderr = _cli(
        ["resume", run_dir.name, "--sample", "linear", "--force", *common], env
    )
    assert code == 0, f"stdout={stdout[-1200:]}\nstderr={stderr[-1200:]}"
    final = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert final["status"] == RunStatus.COMPLETED.value
    assert ApprovalStore(run_dir).get(approval_id).status == "consumed"


def test_cli_approve_rejects_unknown_request(tmp_path: Path):
    out = tmp_path / "o"
    run_dir = out / "R-EMPTY"
    run_dir.mkdir(parents=True)
    store = ApprovalStore(run_dir)
    store.create("plan")
    code, _, err = _cli(
        ["approve", "R-EMPTY", "AP-nope", "--decision", "approve"],
        {"AGENTCREW_OUTPUT_DIR": str(out)},
    )
    assert code != 0
    assert "裁决失败" in err


# ── HTTP API ───────────────────────────────────────────────────────────

async def _http(
    port: int, method: str, path: str, body: dict | None = None,
) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    raw = json.dumps(body).encode() if body is not None else b""
    head = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if raw:
        head += f"Content-Length: {len(raw)}\r\nContent-Type: application/json\r\n"
    writer.write(head.encode() + b"\r\n" + raw)
    await writer.drain()
    data = await reader.read(65536)
    writer.close()
    await writer.wait_closed()
    text = data.decode("utf-8", errors="replace")
    status = int(re.search(r"HTTP/1\.1 (\d+)", text).group(1))
    payload = json.loads(text.split("\r\n\r\n", 1)[1]) if "\r\n\r\n" in text else {}
    return status, payload


@pytest.fixture
def api(tmp_path: Path):
    output = tmp_path / "o"
    run_dir = output / "R-API-1"
    run_dir.mkdir(parents=True)
    store = ApprovalStore(run_dir)
    request = store.create("delivery", context={"project_goal": "demo"})
    return output, run_dir, store, request


async def test_api_lists_and_decides_approvals(api):
    output, run_dir, store, request = api
    server = ApiServer(port=0, output_dir=output)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        status, payload = await _http(port, "GET", "/runs/R-API-1/approvals")
        assert status == 200
        assert payload["approvals"][0]["id"] == request.id
        assert payload["approvals"][0]["status"] == "pending"

        status, decided = await _http(
            port, "POST", "/runs/R-API-1/approvals",
            {"request_id": request.id, "decision": {"action": "accept"},
             "decided_by": "pytest"},
        )
        assert status == 200, decided
        assert decided["status"] == "decided"
        assert decided["decision"]["action"] == "accept"
        assert store.get(request.id).status == "decided"
        assert server.security_events == []  # 认证关闭时零副作用
    finally:
        await server.stop()


async def test_api_approval_error_shapes(api):
    output, run_dir, store, request = api
    server = ApiServer(port=0, output_dir=output)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        # 形状错误 → 400
        status, _ = await _http(port, "POST", "/runs/R-API-1/approvals", {"x": 1})
        assert status == 400
        # 不存在的单 → 404
        status, _ = await _http(
            port, "POST", "/runs/R-API-1/approvals",
            {"request_id": "AP-nope", "decision": {"action": "accept"}},
        )
        assert status == 404
        # 非法动作 → 409（存储层拒绝）
        status, _ = await _http(
            port, "POST", "/runs/R-API-1/approvals",
            {"request_id": request.id, "decision": {"action": "approve"}},
        )
        assert status == 409
        # 合法裁决后再来一次 → 409
        await _http(
            port, "POST", "/runs/R-API-1/approvals",
            {"request_id": request.id, "decision": {"action": "accept"}},
        )
        status, _ = await _http(
            port, "POST", "/runs/R-API-1/approvals",
            {"request_id": request.id, "decision": {"action": "accept"}},
        )
        assert status == 409
        # 非法 run_id → 400
        status, _ = await _http(port, "GET", "/runs/..%2Foutside/approvals")
        assert status in (400, 404)
    finally:
        await server.stop()


def test_run_paused_event_updates_run_state():
    server = ApiServer(port=0)
    server.on_event("run_started", {"run_id": "R-1", "goal": "x"})
    assert server.runs["R-1"].status == "running"
    server.on_event("run_paused", {
        "run_id": "R-1", "status": "awaiting_approval",
        "approval_id": "AP-1", "checkpoint": "plan",
    })
    assert server.runs["R-1"].status == "awaiting_approval"


def test_deferred_build_crew_keeps_human_gate(tmp_path: Path):
    """API 路径此前硬编码 gate=none；deferred 模式必须真的保留人工卡点。"""
    from agentcrew.config import Settings
    server = ApiServer(port=0, output_dir=tmp_path, settings=Settings.load())
    crew = server._build_crew("目标", {"approval_mode": "deferred"})
    assert crew is not None
    assert crew._settings.gate != "none"
    assert isinstance(crew._prompter, DeferredPrompter)
    plain = server._build_crew("目标", {})
    assert plain._settings.gate == "none"
