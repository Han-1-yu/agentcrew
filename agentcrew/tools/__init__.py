"""Agent 可调用的工具包（P1-1 / R3）：统一 ``Tool`` 接口 + 注册表 + 内置工具 + MCP 适配。

- :class:`~agentcrew.tools.tool.Tool`：工具基类；
- :class:`~agentcrew.tools.registry.ToolRegistry`：name → Tool 登记表；
- 本地沙箱工具 :class:`ReadFileTool` / :class:`ListFilesTool` /
  :class:`GrepTool` / :class:`RunPythonTool` / :class:`WriteFileTool`；
- 只读网络工具 :class:`WebFetchTool`（带 SSRF 防护）；
- :func:`validate_tool_args` / :func:`schema_hint`：执行前的入参强校验与形状提示；
- :class:`~agentcrew.tools.mcp_adapter.MCPToolAdapter`：把外部 MCP 工具包成
  同一接口。
"""

from agentcrew.tools.builtins import (
    GrepTool,
    ListFilesTool,
    ReadFileTool,
    RunPythonTool,
    WriteFileTool,
)
from agentcrew.tools.mcp_adapter import MCPToolAdapter
from agentcrew.tools.network import WebFetchTool
from agentcrew.tools.registry import ToolRegistry
from agentcrew.tools.schema import schema_hint, validate_tool_args
from agentcrew.tools.tool import Tool

__all__ = [
    "Tool",
    "ToolRegistry",
    "ReadFileTool",
    "ListFilesTool",
    "GrepTool",
    "RunPythonTool",
    "WriteFileTool",
    "WebFetchTool",
    "validate_tool_args",
    "schema_hint",
    "MCPToolAdapter",
]
