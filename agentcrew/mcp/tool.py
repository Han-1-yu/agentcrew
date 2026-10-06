"""MCP 工具定义与执行结果（V2 第 6 项）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MCPTool(BaseModel):
    """一个 MCP 工具的定义（来自 tools/list 响应）。"""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str = ""
    inputSchema: dict[str, Any] = Field(default_factory=dict)  # noqa: N815 - MCP 规范字段名


class MCPToolResult(BaseModel):
    """tools/call 的执行结果。"""

    model_config = ConfigDict(extra="allow")

    content: list[dict[str, Any]] = Field(default_factory=list)
    isError: bool = False  # noqa: N815 - MCP 规范字段名

    @property
    def text(self) -> str:
        """把 content 里的 text 类型拼接成字符串。"""
        parts: list[str] = []
        for item in self.content:
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
