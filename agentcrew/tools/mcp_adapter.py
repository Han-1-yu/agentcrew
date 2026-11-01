"""把 MCP 工具适配成统一的 :class:`~agentcrew.tools.tool.Tool` 接口（P1-1）。

外部 MCP 服务器（``tools/list`` 发现的 :class:`~agentcrew.mcp.tool.MCPTool`）经
本适配器包一层：``name`` / ``description`` / ``input_schema`` 原样透传，
:meth:`run` 委托给构造时注入的 ``caller``（通常是
``lambda name, args: client.call_tool(name, args).text``）。适配器不持有 MCP
连接生命周期——连接的 start/stop 由 Crew 层负责，这样工具实例可安全登记进
:class:`~agentcrew.tools.registry.ToolRegistry` 并按 run 复用。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import Field

from agentcrew.mcp.tool import MCPTool
from agentcrew.tools.tool import Tool

#: MCP 调用回调：``(tool_name, arguments)`` → 文本结果。
MCPCaller = Callable[[str, dict[str, Any]], Awaitable[str]]


class MCPToolAdapter(Tool):
    """一个 :class:`~agentcrew.mcp.tool.MCPTool` 的 :class:`Tool` 包装。"""

    caller: MCPCaller = Field(exclude=True)
    """真正发起 MCP ``tools/call`` 的回调（构造期注入，不进 dump / 提示词）。"""

    @classmethod
    def from_mcp(cls, tool: MCPTool, caller: MCPCaller) -> MCPToolAdapter:
        """从 MCP ``tools/list`` 的一条记录构造适配器。"""
        return cls(
            name=tool.name,
            description=tool.description,
            input_schema=tool.inputSchema,
            caller=caller,
        )

    async def run(self, **kwargs: Any) -> str:
        """委托给注入的 ``caller``；异常由 Agent 兜底转成错误文本。"""
        return await self.caller(self.name, kwargs)
