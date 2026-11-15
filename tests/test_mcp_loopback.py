"""C9：本仓 MCP client ↔ 本仓 ``main.py mcp-serve`` 的端到端回环。

这是「双向打通」唯一能在 CI 里自动重跑的证据：此前只有一个需要手工 API key、
手工起 ``serve``、不进 CI 的 Node 脚本（``output/_eval/mcp-interop/probe.mjs``）。
回环一旦红，协议漂移、stdout 污染、配置形状退化都会当场暴露，而不是靠人读代码。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agentcrew.api.server import ApiServer
from agentcrew.mcp.client import MCPClient, MCPError

REPO = Path(__file__).resolve().parents[1]

TOOL_NAMES = {
    "agentcrew_create_run",
    "agentcrew_run_status",
    "agentcrew_list_approvals",
    "agentcrew_approve",
    "agentcrew_wait_for_run",
    "agentcrew_list_artifacts",
    "agentcrew_read_artifact",
}


@pytest.fixture
async def api_port():
    server = ApiServer(port=0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        await server.stop()


def mk_client(port: int) -> MCPClient:
    # 显式 UTF-8：Windows 上子进程 stdout 若按 GBK 解，JSON 帧会直接坏掉。
    return MCPClient(
        [sys.executable, "main.py", "mcp-serve",
         "--api-url", f"http://127.0.0.1:{port}"],
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )


@pytest.mark.asyncio
async def test_client_talks_to_our_own_stdio_server(api_port):
    client = mk_client(api_port)
    try:
        # main.py 冷启动会 import 整张依赖图（实测 >8s），预算给足。
        await asyncio.wait_for(client.start(), timeout=120)
        tools = await asyncio.wait_for(client.list_tools(), timeout=30)
        assert {tool.name for tool in tools} == TOOL_NAMES
        assert all(tool.description for tool in tools)
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_business_error_is_tool_error_not_protocol_error(api_port):
    """不存在的 run 应该是一条 ``isError`` 工具结果，而不是协议层崩溃。"""
    client = mk_client(api_port)
    try:
        await asyncio.wait_for(client.start(), timeout=120)
        result = await asyncio.wait_for(
            client.call_tool("agentcrew_run_status", {"run_id": "R-absent"}),
            timeout=30)
        assert result.isError is True
        assert result.text
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_call_before_start_is_rejected():
    client = mk_client(1)
    with pytest.raises(MCPError):
        await client.call_tool("agentcrew_run_status", {"run_id": "R-1"})


def test_wait_is_bounded_below_rpc_timeout():
    """单次 wait 必须小于 MCP 客户端 30s RPC 超时，否则客户端先超时、服务端还卡着。"""
    from agentcrew.mcp.server import _MAX_POLL_SECONDS, ApiBridge

    assert _MAX_POLL_SECONDS <= 25.0
    calls = {"n": 0}

    def fake(method, path, payload=None):  # noqa: ANN001
        calls["n"] += 1
        return 200, {"status": "running"}

    bridge = ApiBridge("http://127.0.0.1:9", request_fn=fake)
    result = bridge.wait_for_run("R-1", timeout_seconds=1.0)
    assert result["timed_out"] is True
    assert result["waited_seconds"] >= 1.0
    assert "再次调用" in result["hint"]
    assert calls["n"] >= 2

