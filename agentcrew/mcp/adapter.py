"""把 MCP 工具适配成 Agent 可调用的 :class:`~agentcrew.tools.tool.Tool`。

``agentcrew/mcp`` 此前只有客户端：能连服务器、能列工具、能调工具，但**没有一条路
把它交给 Agent**——``mcp_servers`` 因而是一条死配置。这个适配器就是那条路。

两条刻意的设计：

1. **注册名带服务器前缀**（``fs__read_file``）。MCP 工具名由第三方定义，直接登记会
   与内置工具（``read_file`` …）撞名，而 ``ToolRegistry.register`` 的语义是「后登记
   覆盖先登记」——撞上就等于第三方脚本悄悄替换了内置工具。
2. **schema 原样透传，不做裁剪**。``MCPTool.inputSchema`` 是服务端声明的完整
   JSON Schema，运行前的强校验（R3 的 ``tools/schema.py``）只认它认识的子集，
   其余按「未声明」放行——这比替服务端改写 schema 安全，也不会把合法工具判成非法。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from agentcrew.mcp.client import MCPClient
from agentcrew.mcp.tool import MCPTool
from agentcrew.tools.tool import Tool

#: 注册名里只保留小写字母、数字、下划线；其余字符一律折成下划线。
_NAME_SAFE_RE = re.compile(r"[^a-z0-9_]+")


def qualified_tool_name(server: str, tool: str) -> str:
    """``server`` + ``tool`` → 注册名（``fs__read_file``）。

    统一小写并折叠非法字符：模型在 ``<tool_call name=…>`` 里引用它，任何奇怪字符
    都会让解析失败；而提示词里出现的名字必须与实际登记的一字不差。
    """
    server_part = _NAME_SAFE_RE.sub("_", (server or "").strip().lower()).strip("_")
    tool_part = _NAME_SAFE_RE.sub("_", (tool or "").strip().lower()).strip("_")
    if server_part and tool_part:
        return f"{server_part}__{tool_part}"
    return tool_part or server_part or "mcp_tool"


class MCPToolAdapter(Tool):
    """一个 MCP 工具在本地注册表里的表示。

    ``client`` 不参与序列化（``exclude``）：它是活连接，写进提示词或快照毫无意义，
    还会把进程句柄带进去。
    """

    #: 工具所属的 MCP 服务器名（配置里的键）。
    server: str = ""
    #: 服务端声明的原始工具名；调用时用它，不是注册名。
    remote_name: str = ""
    client: Any = Field(default=None, exclude=True)

    async def run(self, **kwargs: Any) -> str:
        """转发到 ``tools/call``；服务端报错转成 ``错误：…`` 文本。

        异常本身向上抛（由 Agent 统一捕获并回灌），这里只处理「调用成功但结果
        是错误」这种正常路径上的业务失败。
        """
        if self.client is None:
            return "错误：MCP 客户端未绑定（工具未正确装配）"
        result = await self.client.call_tool(self.remote_name, kwargs)
        text = result.text
        if result.isError:
            return f"错误：{text or 'MCP 工具执行失败'}"
        return text or "（MCP 工具无文本输出）"


def adapt_tools(
    server: str, client: MCPClient, specs: list[MCPTool]
) -> list[MCPToolAdapter]:
    """把一次 ``tools/list`` 的结果批量适配成 ``Tool``。

    ``description`` 前缀补上服务器名：模型看到 ``fs__read_file`` 时未必知道它来自
    哪个服务，一句话说明能减少误用。
    """
    adapted: list[MCPToolAdapter] = []
    for spec in specs:
        description = (spec.description or "").strip() or f"{spec.name}（来自 MCP 服务）"
        adapted.append(MCPToolAdapter(
            name=qualified_tool_name(server, spec.name),
            description=f"[MCP:{server}] {description}",
            input_schema=dict(spec.inputSchema or {}),
            server=server,
            remote_name=spec.name,
            client=client,
        ))
    return adapted
