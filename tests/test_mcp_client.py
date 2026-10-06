"""MCP 客户端测试（V2 第 6 项）。

用一个内联 Python 脚本作为 mock MCP 服务器，通过 stdio 测试完整 JSON-RPC 流程。
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from agentcrew.mcp.client import MCPClient, MCPError
from agentcrew.mcp.tool import MCPTool, MCPToolResult

#: mock MCP 服务器脚本：响应 initialize / tools/list / tools/call
_MOCK_SERVER = textwrap.dedent("""
    import sys, json

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        req_id = req.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock", "version": "1.0"},
            }})
        elif method == "notifications/initialized":
            pass  # 通知，无响应
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
                {"name": "echo", "description": "回显输入",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                {"name": "add", "description": "两数相加",
                 "inputSchema": {"type": "object", "properties": {
                     "a": {"type": "number"}, "b": {"type": "number"}}}},
            ]}})
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                content = [{"type": "text", "text": args.get("text", "")}]
            elif name == "add":
                content = [{"type": "text", "text": str(args.get("a", 0) + args.get("b", 0))}]
            else:
                content = [{"type": "text", "text": f"unknown tool: {name}"}]
            send({"jsonrpc": "2.0", "id": req_id, "result": {"content": content, "isError": False}})
""")


@pytest.fixture
def mock_server_script(tmp_path):
    script = tmp_path / "mock_mcp_server.py"
    script.write_text(_MOCK_SERVER, encoding="utf-8")
    return str(script)


class TestMCPClient:
    @pytest.mark.asyncio
    async def test_list_tools(self, mock_server_script):
        async with MCPClient([sys.executable, mock_server_script]) as client:
            tools = await client.list_tools()
            assert len(tools) == 2
            assert tools[0].name == "echo"
            assert "回显" in tools[0].description
            assert tools[1].name == "add"

    @pytest.mark.asyncio
    async def test_call_tool_echo(self, mock_server_script):
        async with MCPClient([sys.executable, mock_server_script]) as client:
            result = await client.call_tool("echo", {"text": "hello world"})
            assert isinstance(result, MCPToolResult)
            assert result.text == "hello world"
            assert result.isError is False

    @pytest.mark.asyncio
    async def test_call_tool_add(self, mock_server_script):
        async with MCPClient([sys.executable, mock_server_script]) as client:
            result = await client.call_tool("add", {"a": 3, "b": 4})
            assert result.text == "7"

    @pytest.mark.asyncio
    async def test_call_tool_unknown(self, mock_server_script):
        async with MCPClient([sys.executable, mock_server_script]) as client:
            result = await client.call_tool("nonexistent", {})
            assert "unknown tool" in result.text

    @pytest.mark.asyncio
    async def test_not_initialized_raises(self):
        client = MCPClient([sys.executable, "-c", "pass"])
        with pytest.raises(MCPError, match="未初始化"):
            await client.list_tools()

    def test_mcp_tool_model(self):
        tool = MCPTool(name="test", description="d", inputSchema={"type": "object"})
        assert tool.name == "test"
        assert tool.inputSchema["type"] == "object"

    def test_mcp_tool_result_text(self):
        result = MCPToolResult(content=[
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
            {"type": "image", "data": "abc"},
        ])
        assert result.text == "hello\nworld"

    @pytest.mark.asyncio
    async def test_start_stop_explicit(self, mock_server_script):
        client = MCPClient([sys.executable, mock_server_script])
        await client.start()
        assert client._initialized
        tools = await client.list_tools()
        assert len(tools) == 2
        await client.stop()
        assert not client._initialized
