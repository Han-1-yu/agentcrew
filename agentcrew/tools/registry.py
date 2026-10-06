"""工具注册表：按名称登记工具，并按角色的 ``allowed_tools`` 过滤（P1-1）。

注册表是 run 级对象：一个 Crew / 一次 run 构造一份，把内置沙箱工具与连接好的
MCP 工具实例都登记进去。:meth:`ToolRegistry.filter_by_allowed` 是角色能力边界
的落点——``RoleSpec.allowed_tools`` 列什么，该角色的 Agent 在系统提示词里就
只看得见什么；``["*"]`` 表示放行注册表全部工具。
"""

from __future__ import annotations

from agentcrew.tools.tool import Tool


class ToolRegistry:
    """``name → Tool`` 的登记表。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """登记一个工具；同名后登记覆盖先登记（run 级覆盖，不跨 run 泄漏）。"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """按名取工具；不存在返回 ``None``。"""
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """全部已登记工具（登记顺序即字典插入顺序）。"""
        return list(self._tools.values())

    def filter_by_allowed(self, allowed: list[str]) -> list[Tool]:
        """按角色 ``allowed_tools`` 过滤。

        - ``["*"]``（或缺省空列表）→ 全部放行；
        - 否则只返回 ``name`` 在白名单里的工具——模型在提示词里看不见未授权的
          工具，ReAct 循环里即便模型写出未授权名字也会被 :class:`BaseAgent`
          以「未授权」错误回灌。
        """
        if not allowed or "*" in allowed:
            return list(self._tools.values())
        allowed_set = set(allowed)
        return [tool for tool in self._tools.values() if tool.name in allowed_set]
