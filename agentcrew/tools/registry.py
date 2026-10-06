"""工具注册表：按名称登记工具，并按角色能力边界过滤（P1-1 + P3-9 权限管控）。

注册表是 run 级对象：一个 Crew / 一次 run 构造一份，把内置沙箱工具与连接好的
MCP 工具实例都登记进去。两层能力边界：

- P1-1：:meth:`filter_by_allowed` 按 ``RoleSpec.allowed_tools`` 决定模型在系统
  提示词里**看得见**哪些工具（``["*"]`` 放行全部）；
- P3-9：角色级工具权限白名单 :meth:`is_allowed` / :meth:`enforce` 是执行期闸门
  —— 即使模型写出某个工具名，只要该角色未被授权，:meth:`enforce` 就拒绝执行
  并记一条 ``tool_permission_denied`` ledger 事件（含 ``role`` / ``tool_name``），
  拒绝的工具绝不真正运行。

未配置任何角色权限时，所有角色可使用所有工具（向后兼容）。
"""

from __future__ import annotations

from typing import Any, Protocol

from agentcrew.tools.tool import Tool


class LedgerLike(Protocol):
    """工具权限拒绝事件的最小协议子集；用 Protocol 避免反向依赖运行时包。"""

    def emit(self, event: str, **data: Any) -> object:
        """记录一次事件。"""
        ...


class ToolRegistry:
    """``name → Tool`` 的登记表，附带角色级权限白名单。"""

    def __init__(self, *, ledger: LedgerLike | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        #: 角色 → 允许的工具名集合；角色不在表中 = 全部允许（向后兼容）。
        self._permissions: dict[str, set[str]] = {}
        self._ledger: LedgerLike | None = ledger

    def bind_ledger(self, ledger: LedgerLike) -> None:
        """运行时绑定 ledger（用于 ``tool_permission_denied`` 事件留痕）。"""
        self._ledger = ledger

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
        """按角色 ``allowed_tools`` 过滤（P1-1）。

        - ``["*"]``（或缺省空列表）→ 全部放行；
        - 否则只返回 ``name`` 在白名单里的工具——模型在提示词里看不见未授权的
          工具，ReAct 循环里即便模型写出未授权名字也会被 :class:`BaseAgent`
          以「未授权」错误回灌。
        """
        if not allowed or "*" in allowed:
            return list(self._tools.values())
        allowed_set = set(allowed)
        return [tool for tool in self._tools.values() if tool.name in allowed_set]

    # ── P3-9 角色级工具权限管控 ─────────────────────────────────────────

    def set_role_permissions(
        self, role: str, allowed_tools: list[str]
    ) -> None:
        """设置某角色的工具白名单。

        - ``["*"]`` 或空列表 → 该角色**全部允许**（从白名单表移除，等同默认）；
        - 否则只有列出的工具名被允许——其余工具即使已登记也不得被该角色执行。
        """
        allowed = set(allowed_tools)
        if not allowed or "*" in allowed:
            self._permissions.pop(role, None)
            return
        self._permissions[role] = allowed

    def is_allowed(self, role: str, tool_name: str) -> bool:
        """``role`` 是否有权使用 ``tool_name``。

        角色未配置权限（默认）→ 全部允许；配置后仅白名单内放行。
        """
        allowed = self._permissions.get(role)
        if allowed is None:
            return True
        return tool_name in allowed

    def enforce(self, role: str, tool_name: str) -> bool:
        """执行期权限闸门：不通过时记 ``tool_permission_denied`` 并返回 ``False``。

        安全约定：返回 ``False`` 时调用方**不得**真正执行该工具。
        """
        if self.is_allowed(role, tool_name):
            return True
        if self._ledger is not None:
            self._ledger.emit(
                "tool_permission_denied", role=role, tool_name=tool_name
            )
        return False

    def filter_by_role(self, role: str, tools: list[Tool]) -> list[Tool]:
        """按角色权限白名单过滤一组工具（模型可见 / 可执行的并集）。"""
        return [tool for tool in tools if self.is_allowed(role, tool.name)]
