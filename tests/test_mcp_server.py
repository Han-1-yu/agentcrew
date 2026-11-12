"""C7：把 AgentCrew 暴露为 MCP server（stdio）。

两层验证：

1. **协议层**（纯函数 + 内存流）：initialize / tools/list / tools/call /
   ping / 未知方法 / 通知不回帧 / 坏帧不崩；
2. **桥接层**：``ApiBridge`` 对**真实 in-process ApiServer** 发请求，验证
   翻译出的路径、方法、鉴权头与错误收敛（不真跑 run——那是 API 测试的事）。
"""

from __future__ import annotations

import io
import json

from agentcrew.api.server import ApiServer
from agentcrew.mcp.server import (
    PROTOCOL_VERSION,
    TOOL_DEFINITIONS,
    ApiBridge,
    handle_message,
    run_stdio,
)


def _msg(method: str, params: dict | None = None, id_=1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method,
            "params": params or {}}


def _result_text(resp: dict) -> str:
    return resp["result"]["content"][0]["text"]


# ── 协议层 ─────────────────────────────────────────────────────────────

def test_initialize_returns_protocol_and_server_info():
    resp = handle_message(ApiBridge("http://x"), _msg("initialize"))
    assert resp is not None and "error" not in resp
    assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert resp["result"]["serverInfo"]["name"] == "agentcrew"


def test_notification_returns_none():
    assert handle_message(ApiBridge("http://x"), {
        "jsonrpc": "2.0", "method": "notifications/initialized",
    }) is None


def test_tools_list_exposes_five_tools():
    resp = handle_message(ApiBridge("http://x"), _msg("tools/list"))
    tools = resp["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == [
        "agentcrew_create_run", "agentcrew_run_status",
        "agentcrew_list_approvals", "agentcrew_approve",
        "agentcrew_wait_for_run",
    ]
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
    assert [item["name"] for item in TOOL_DEFINITIONS] == names


def test_ping_and_unknown_method():
    assert handle_message(ApiBridge("http://x"), _msg("ping"))["result"] == {}
    resp = handle_message(ApiBridge("http://x"), _msg("no/such/method"))
    assert resp["error"]["code"] == -32601


def test_unknown_tool_is_protocol_error():
    resp = handle_message(ApiBridge("http://x"),
                          _msg("tools/call", {"name": "not_a_tool"}))
    assert resp["error"]["code"] == -32602
    assert "not_a_tool" in resp["error"]["message"]


def test_handler_exception_becomes_protocol_error():
    class _Boom(ApiBridge):
        def run_status(self, run_id):  # noqa: ANN001
            raise RuntimeError("disk exploded")

    resp = handle_message(_Boom("http://x"),
                          _msg("tools/call", {"name": "agentcrew_run_status",
                                              "arguments": {"run_id": "r1"}}))
    assert resp["error"]["code"] == -32603
    assert "RuntimeError" in resp["error"]["message"]


def test_tool_argument_validation_is_friendly_text():
    resp = handle_message(ApiBridge("http://x"), _msg(
        "tools/call", {"name": "agentcrew_create_run", "arguments": {}}))
    assert resp["result"]["isError"] is True
    assert "goal" in _result_text(resp)


# ── 桥接层：注入式 request_fn ─────────────────────────────────────────

def test_bridge_sends_post_body_to_runs_endpoint():
    seen: dict = {}

    def fake(method: str, path: str, payload: dict | None):
        seen.update(method=method, path=path, payload=payload)
        return 202, {"run_id": "20261112-100000-ab12", "status": "running"}

    data = ApiBridge("http://api:1", request_fn=fake).create_run(
        "做个东西", {"mock": True})
    assert data["run_id"] == "20261112-100000-ab12"
    assert seen == {
        "method": "POST", "path": "/runs",
        "payload": {"goal": "做个东西", "options": {"mock": True}},
    }


def test_bridge_converts_http_error_to_readable_error():
    def fake(method: str, path: str, payload: dict | None):
        return 429, {"error": "并发 run 数已达上限"}

    data = ApiBridge("http://api:1", request_fn=fake).create_run("x")
    assert data["error"] == "并发 run 数已达上限"
    assert data["status"] == 429


def test_wait_for_run_stops_on_terminal_status():
    calls: list[str] = []

    def fake(method: str, path: str, payload: dict | None):
        calls.append(path)
        return 200, {"run_id": "r1", "status": "COMPLETED"}

    data = ApiBridge("http://api:1", request_fn=fake).wait_for_run("r1", 5)
    assert data["status"] == "COMPLETED"
    assert len(calls) == 1  # 终态第一轮就停


# ── stdio 主循环 ───────────────────────────────────────────────────────

def test_stdio_loop_end_to_end():
    reader = io.StringIO(
        json.dumps(_msg("initialize")) + "\n"
        + json.dumps({"jsonrpc": "2.0",
                      "method": "notifications/initialized"}) + "\n"
        + "not-json\n"
        + json.dumps(_msg("tools/call", {"name": "agentcrew_create_run",
                                         "arguments": {"goal": "x"}})) + "\n"
    )
    writer = io.StringIO()
    code = run_stdio(ApiBridge("http://api:1", request_fn=lambda m, p, b: (
        503, {"error": "server 未配置"})), reader, writer)
    assert code == 0
    frames = [json.loads(line) for line in writer.getvalue().splitlines()]
    # initialize / 坏帧 / tools/call 三帧；通知（initialized）不回帧
    assert len(frames) == 3
    assert frames[0]["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert frames[1]["error"]["code"] == -32700
    assert frames[2]["result"]["isError"] is True


def test_stdio_loop_empty_input_returns_zero():
    assert run_stdio(ApiBridge("http://api:1"), io.StringIO(""), io.StringIO()) == 0


# ── 桥接层：对真实 in-process ApiServer ────────────────────────────────

async def test_bridge_against_real_server(tmp_path):
    # 同步 bridge（urllib）会阻塞事件循环，必须在别的线程里跑
    import asyncio

    async with ApiServer(port=0, output_dir=tmp_path) as srv:
        port = srv._server.sockets[0].getsockname()[1]
        bridge = ApiBridge(f"http://127.0.0.1:{port}")

        async def call(payload: dict) -> dict | None:
            return await asyncio.to_thread(handle_message, bridge, payload)

        # 未配置 crew_factory/settings：create_run 明确 503，而不是崩溃
        resp = await call(_msg("tools/call", {
            "name": "agentcrew_create_run", "arguments": {"goal": "写个东西"}}))
        assert resp is not None and "error" not in resp
        assert resp["result"]["isError"] is True
        assert "503" in _result_text(resp) or "server 未配置" in _result_text(resp)

        # 不存在的 run：状态与审批都是可读错误
        fake_run = "20261112-100000-ab12"
        resp = await call(_msg(
            "tools/call", {"name": "agentcrew_run_status",
                           "arguments": {"run_id": fake_run}}))
        assert resp["result"]["isError"] is True

        resp = await call(_msg(
            "tools/call", {"name": "agentcrew_list_approvals",
                           "arguments": {"run_id": fake_run}}))
        assert "error" not in resp["result"]
        body = json.loads(_result_text(resp))
        assert body["run_id"] == fake_run
        assert body["approvals"] == []
