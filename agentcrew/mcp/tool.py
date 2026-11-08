"""MCP 工具定义与执行结果（V2 第 6 项）。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MCPTool(BaseModel):
    """一个 MCP 工具的定义（来自 tools/list 响应）。"""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str = ""
    inputSchema: dict[str, Any] = Field(default_factory=dict)  # noqa: N815 - MCP 规范字段名
    #: 结构化输出 schema（MCP 2025-06-18 起）。``None`` = 服务端没声明，
    #: 结果是纯文本 / 非结构化。只读透传，不参与本地校验——输出长什么样由
    #: 服务端保证，本地强行校验只会把「合法但超出预期」的结果判成错误。
    outputSchema: dict[str, Any] | None = None  # noqa: N815 - MCP 规范字段名


class MCPToolResult(BaseModel):
    """tools/call 的执行结果。"""

    model_config = ConfigDict(extra="allow")

    content: list[dict[str, Any]] = Field(default_factory=list)
    #: 结构化结果（MCP 2025-06-18 起）。工具返回结构化数据时用它，而不是 content。
    structuredContent: dict[str, Any] | None = None  # noqa: N815 - MCP 规范字段名
    isError: bool = False  # noqa: N815 - MCP 规范字段名

    @property
    def text(self) -> str:
        """把结果变成能回灌给模型的字符串。

        优先级：①``content`` 里的 ``text`` 块（拼接）；②``structuredContent``
        （序列化成 JSON——**没有这一条，结构化结果会被整个丢掉**，模型看到的
        是空串）；③非文本块（image / resource …）给一句类型提示而不是空串。
        """
        parts: list[str] = []
        for item in self.content:
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        if parts:
            return "\n".join(parts)
        if self.structuredContent:
            return json.dumps(self.structuredContent, ensure_ascii=False)
        if self.content:
            kinds = [str(item.get("type", "?")) for item in self.content]
            return f"（返回了 {len(self.content)} 个非文本内容块：{', '.join(kinds)}）"
        return ""
