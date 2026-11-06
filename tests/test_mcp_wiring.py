"""R13：MCP 服务器生产接线。

``agentcrew/mcp`` 此前只有客户端：能连服务器、能列工具、能调工具，但**没有一条路
把它交给 Agent**——``mcp_servers`` 因此是一条死配置，Agent 永远只看得见内置工具。

这一层补那条路：``tools_enabled`` 时把配置里声明的服务器连上、发现工具、登记进
注册表，收尾时关连接。

覆盖：注册名规范化（防撞名）、适配器转发与错误语义、真实 mock 服务器的端到端接线、
单服务器失败不拖垮 run、未开工具链时零连接。
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.mcp.adapter import MCPToolAdapter, adapt_tools, qualified_tool_name
from agentcrew.mcp.tool import MCPTool
from agentcrew.runtime.ledger import Ledger
from agentcrew.tools.registry import ToolRegistry

#: 内联 mock MCP 服务器：响应 initialize / tools/list / tools/call。
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
            pass
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
                {"name": "echo", "description": "回显输入",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                {"name": "boom", "description": "总是失败",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}})
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}],
                    "isError": False,
                }})
            else:
                send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": "内部炸了"}],
                    "isError": True,
                }})
""")


def _command() -> list[str]:
    return [sys.executable, "-c", _MOCK_SERVER]


# ── 注册名 ────────────────────────────────────────────────────────────────


def test_qualified_name_prefixes_server():
    """带前缀是防撞的核心：第三方工具名不该覆盖内置的 read_file。"""
    assert qualified_tool_name("fs", "read_file") == "fs__read_file"


def test_qualified_name_normalizes_case_and_symbols():
    assert qualified_tool_name("File-System", "read.File") == "file_system__read_file"


def test_qualified_name_survives_empty_parts():
    assert qualified_tool_name("", "echo") == "echo"
    assert qualified_tool_name("fs", "") == "fs"
    assert qualified_tool_name("", "") == "mcp_tool"


# ── 适配器 ────────────────────────────────────────────────────────────────


class _StubClient:
    """只记录调用、按预设返回的客户端替身。"""

    def __init__(self, *, is_error: bool = False, text: str = "ok") -> None:
        self.calls: list[tuple[str, dict]] = []
        self._is_error = is_error
        self._text = text

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))

        class _Result:
            text = self._text

        result = _Result()
        result.isError = self._is_error  # type: ignore[attr-defined]
        return result


def test_adapt_tools_keeps_schema_and_marks_source():
    specs = [MCPTool(name="echo", description="回显",
                     inputSchema={"type": "object", "properties": {"t": {"type": "string"}}})]
    client = _StubClient()

    adapted = adapt_tools("mock", client, specs)

    assert [tool.name for tool in adapted] == ["mock__echo"]
    assert adapted[0].description.startswith("[MCP:mock]")
    # schema 原样透传，不替服务端改写
    assert adapted[0].input_schema == specs[0].inputSchema
    assert adapted[0].remote_name == "echo"


async def test_adapter_uses_remote_name_not_registered_name():
    client = _StubClient(text="echo:hi")
    tool = MCPToolAdapter(
        name="mock__echo", description="d", input_schema={},
        server="mock", remote_name="echo", client=client)

    text = await tool.run(text="hi")

    assert text == "echo:hi"
    # 转发时用服务端原始名，不是带前缀的注册名
    assert client.calls == [("echo", {"text": "hi"})]


async def test_adapter_turns_is_error_into_error_text():
    client = _StubClient(is_error=True, text="内部炸了")
    tool = MCPToolAdapter(
        name="mock__boom", description="d", input_schema={},
        server="mock", remote_name="boom", client=client)

    assert await tool.run() == "错误：内部炸了"


async def test_adapter_without_client_reports_clearly():
    tool = MCPToolAdapter(name="x", description="d", server="s", remote_name="x")

    assert "未绑定" in await tool.run()


# ── 端到端接线 ────────────────────────────────────────────────────────────


@pytest.fixture
def crew_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    base = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    return dict(base=base, ledger=ledger, tmp_path=tmp_path)


def _crew(env, servers: dict, *, tools_enabled: bool = True) -> Crew:
    settings = env["base"].model_copy(update={
        "tools_enabled": tools_enabled,
        "mcp_servers": servers,
    })
    return Crew(settings)


async def test_attaches_tools_from_real_server(crew_env):
    """真连一个 stdio MCP 服务器，工具带前缀登记进注册表。"""
    crew = _crew(crew_env, {"mock": {"command": _command()}})
    registry = ToolRegistry(ledger=crew_env["ledger"])

    await crew._attach_mcp_tools(registry, crew_env["ledger"])
    try:
        tool = registry.get("mock__echo")
        assert tool is not None
        assert await tool.run(text="hi") == "echo:hi"
        events = list(crew_env["ledger"].events("mcp_connected"))
        assert events[0].data["server"] == "mock"
        assert "mock__echo" in events[0].data["tools"]
    finally:
        await crew._close_mcp()


async def test_failed_server_is_recorded_and_skipped(crew_env):
    """命令不存在：记 mcp_failed，其它服务器照常接通。"""
    crew = _crew(crew_env, {
        "broken": {"command": ["definitely-not-a-real-binary-xyz"]},
        "mock": {"command": _command()},
    })
    registry = ToolRegistry(ledger=crew_env["ledger"])

    await crew._attach_mcp_tools(registry, crew_env["ledger"])
    try:
        failed = list(crew_env["ledger"].events("mcp_failed"))
        assert failed and failed[0].data["server"] == "broken"
        # 坏的那个不影响好的这个
        assert registry.get("mock__echo") is not None
    finally:
        await crew._close_mcp()


async def test_bad_config_shape_is_recorded(crew_env):
    crew = _crew(crew_env, {"weird": "not-a-mapping"})  # type: ignore[dict-item]
    registry = ToolRegistry(ledger=crew_env["ledger"])

    await crew._attach_mcp_tools(registry, crew_env["ledger"])

    failed = list(crew_env["ledger"].events("mcp_failed"))
    assert failed[0].data["server"] == "weird"
    assert "映射" in failed[0].data["error"]


async def test_no_registry_means_no_connection(crew_env):
    """tools_enabled=False：注册表为 None，一个子进程都不起。"""
    crew = _crew(crew_env, {"mock": {"command": _command()}}, tools_enabled=False)

    await crew._attach_mcp_tools(None, crew_env["ledger"])

    assert crew._mcp_clients == []
    assert crew_env["ledger"].count("mcp_connected") == 0


async def test_close_is_idempotent(crew_env):
    crew = _crew(crew_env, {"mock": {"command": _command()}})
    registry = ToolRegistry(ledger=crew_env["ledger"])
    await crew._attach_mcp_tools(registry, crew_env["ledger"])

    await crew._close_mcp()
    await crew._close_mcp()  # 再关一次不抛、不留连接

    assert crew._mcp_clients == []
