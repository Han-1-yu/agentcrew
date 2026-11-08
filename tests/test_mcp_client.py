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

    def test_mcp_tool_output_schema(self):
        """tools/list 里的 outputSchema 要被解析成字段，而不是丢进 extra。"""
        tool = MCPTool(name="q", outputSchema={
            "type": "object", "properties": {"count": {"type": "integer"}}})
        assert tool.outputSchema["type"] == "object"

    def test_mcp_tool_result_structured_content(self):
        """结构化结果（无 text 块）→ 序列化成 JSON 回灌，不能被整个丢掉。

        这是真实兼容缺口：MCP 2025-06-18 起工具可以只回 structuredContent，
        没有这一条时模型看到的 text 是空串。
        """
        result = MCPToolResult(structuredContent={"count": 42, "unit": "ms"})
        assert '"count": 42' in result.text
        assert '"unit": "ms"' in result.text

    def test_mcp_tool_result_text_beats_structured_content(self):
        """两者都有时文本优先（既有语义不变）。"""
        result = MCPToolResult(
            content=[{"type": "text", "text": "plain"}],
            structuredContent={"count": 42},
        )
        assert result.text == "plain"

    def test_mcp_tool_result_non_text_blocks_placeholder(self):
        """只有 image/resource 等非文本块时给类型提示，而不是空串。"""
        result = MCPToolResult(content=[{"type": "image", "data": "aa"}])
        assert "image" in result.text and result.text


    @pytest.mark.asyncio
    async def test_start_stop_explicit(self, mock_server_script):
        client = MCPClient([sys.executable, mock_server_script])
        await client.start()
        assert client._initialized
        tools = await client.list_tools()
        assert len(tools) == 2
        await client.stop()
        assert not client._initialized

    @pytest.mark.asyncio
    async def test_windows_resolves_cmd_extension(self, monkeypatch):
        """Windows 上 create_subprocess_exec 不解析 PATHEXT，必须用 shutil.which 预解析。"""
        import asyncio
        import shutil

        captured: list[str] = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured.extend(args)
            # 不真正启动进程，直接抛异常让 start() 失败
            raise RuntimeError("captured")

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(shutil, "which", lambda cmd: f"C:\\fake\\{cmd}.cmd")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        client = MCPClient(["npx", "-y", "server"])
        with pytest.raises(RuntimeError, match="captured"):
            await client.start()
        # 第一个参数应该是 shutil.which 解析后的完整路径，而不是原始的 "npx"
        assert captured[0] == "C:\\fake\\npx.cmd"
        assert captured[1] == "-y"
        assert captured[2] == "server"

    @pytest.mark.asyncio
    async def test_non_windows_skips_which_resolution(self, monkeypatch):
        """非 Windows 平台不应调用 shutil.which，命令原样传递。"""
        import asyncio
        import shutil

        captured: list[str] = []
        which_called = {"count": 0}

        def fake_which(cmd):
            which_called["count"] += 1
            return f"/usr/bin/{cmd}"

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured.extend(args)
            raise RuntimeError("captured")

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", fake_which)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        client = MCPClient(["npx", "-y", "server"])
        with pytest.raises(RuntimeError, match="captured"):
            await client.start()
        assert which_called["count"] == 0
        assert captured[0] == "npx"
