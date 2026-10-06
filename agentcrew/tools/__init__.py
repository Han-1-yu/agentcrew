"""Agent 可调用的工具包（P1-1）：统一 ``Tool`` 接口 + 注册表 + 内置沙箱 + MCP 适配。

- :class:`~agentcrew.tools.tool.Tool`：工具基类；
- :class:`~agentcrew.tools.registry.ToolRegistry`：name → Tool 登记表；
- 内置沙箱工具 :class:`ReadFileTool` / :class:`ListFilesTool` /
  :class:`GrepTool` / :class:`RunPythonTool`；
- :class:`~agentcrew.tools.mcp_adapter.MCPToolAdapter`：把外部 MCP 工具包成
  同一接口。
"""

from agentcrew.tools.builtins import (
    GrepTool,
    ListFilesTool,
    ReadFileTool,
    RunPythonTool,
)
from agentcrew.tools.mcp_adapter import MCPToolAdapter
from agentcrew.tools.registry import ToolRegistry
from agentcrew.tools.tool import Tool

__all__ = [
    "Tool",
    "ToolRegistry",
    "ReadFileTool",
    "ListFilesTool",
    "GrepTool",
    "RunPythonTool",
    "MCPToolAdapter",
]
