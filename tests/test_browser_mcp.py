"""C6 第一步：借现成浏览器 MCP server——配置模板与工具白名单可用。

「先借后建」的零开发验证：模板里的 ``mcp_servers`` / ``tool_permissions`` 必须能
被配置层接受、浏览器工具能以 ``server__tool`` 前缀登记进注册表，且
``untrusted_inputs`` 与 MCP 工具互斥这条边界有测试钉住（G-1 注入面与浏览器
自动化是二选一，不能既「只读预设」又「自动操作网页」）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "agentcrew.browser.example.yaml"


@pytest.fixture
def template_text() -> str:
    assert TEMPLATE.is_file(), f"模板缺失：{TEMPLATE}"
    return TEMPLATE.read_text(encoding="utf-8")


def test_template_loads_through_config_layer(tmp_path, monkeypatch, template_text):
    from agentcrew.config import Settings

    cfg = tmp_path / "agentcrew.yaml"
    cfg.write_text(template_text, encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(cfg))
    settings = Settings.load()
    assert settings.tools_enabled is True
    assert settings.untrusted_inputs is False
    assert "playwright" in settings.mcp_servers
    assert settings.mcp_servers["playwright"]["command"] == "npx"
    # 白名单确按「读优先」收窄：写类浏览器工具不在默认授权里
    pm = settings.tool_permissions["project_manager"]
    assert "playwright__browser_navigate" in pm
    assert "playwright__browser_click" not in pm


def test_browser_tools_register_with_server_prefix(tmp_path):
    """R13 约定：MCP 工具以 ``server__tool`` 前缀登记，防撞名。"""
    from agentcrew.mcp.adapter import qualified_tool_name

    assert (qualified_tool_name("playwright", "browser_navigate")
            == "playwright__browser_navigate")


async def test_browser_server_tools_can_be_adapted(tmp_path):
    """MCP 工具适配器接受浏览器工具的 schema 并登记进注册表（不启动真进程）。"""
    from agentcrew.mcp.adapter import adapt_tools
    from agentcrew.mcp.tool import MCPTool, MCPToolResult
    from agentcrew.tools.registry import ToolRegistry

    spec = MCPTool(
        name="browser_navigate",
        description="打开一个网页",
        inputSchema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    )
    class _StubClient:
        """duck-type ``call_tool``：不启动真进程，验证适配器转发语义。"""

        async def call_tool(self, name, arguments=None):  # noqa: ANN001
            return MCPToolResult(
                content=[{"type": "text", "text": "页面已打开"}],
                isError=False)

    client = _StubClient()
    tools = adapt_tools("playwright", client, [spec])
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    tool = registry.get("playwright__browser_navigate")
    assert tool is not None
    result = await tool.run(url="http://127.0.0.1:1/")
    assert result == "页面已打开"


def test_untrusted_inputs_documented_as_exclusive(template_text):
    """模板必须写清「untrusted_inputs 与 MCP 工具互斥」——这是注入面的关键边界。"""
    assert "untrusted_inputs" in template_text
    assert "跳过" in template_text or "禁用" in template_text
