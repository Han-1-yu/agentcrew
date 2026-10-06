"""P3-9：ToolRegistry 角色级工具权限管控测试。

覆盖：
- 默认所有角色可使用所有工具（向后兼容）；
- 设置角色白名单后只放行白名单工具；
- :meth:`ToolRegistry.is_allowed` / :meth:`ToolRegistry.filter_by_role`；
- :meth:`ToolRegistry.enforce` 拒绝时记 ``tool_permission_denied`` ledger 事件；
- ``["*"]`` 与空列表表示全部允许。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.runtime.ledger import Ledger
from agentcrew.tools import ToolRegistry
from agentcrew.tools.tool import Tool


class _EchoTool(Tool):
    """占位工具。"""

    description: str = "回显"

    async def run(self, **kwargs):
        return "ok"


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_EchoTool(name="read_file"))
    reg.register(_EchoTool(name="write_file"))
    reg.register(_EchoTool(name="run_python"))
    return reg


# ── 默认放行（向后兼容）────────────────────────────────────────────────


def test_default_all_roles_allowed(registry: ToolRegistry):
    assert registry.is_allowed("anyone", "read_file") is True
    assert registry.is_allowed("anyone", "run_python") is True
    assert registry.is_allowed("anyone", "nonexistent") is True


def test_filter_by_role_default_passes_all(registry: ToolRegistry):
    tools = registry.list_tools()
    assert registry.filter_by_role("anyone", tools) == tools


# ── 白名单 ───────────────────────────────────────────────────────────


def test_whitelist_restricts_role(registry: ToolRegistry):
    registry.set_role_permissions("viewer", ["read_file"])
    assert registry.is_allowed("viewer", "read_file") is True
    assert registry.is_allowed("viewer", "write_file") is False
    assert registry.is_allowed("viewer", "run_python") is False
    # 其它角色不受影响
    assert registry.is_allowed("admin", "write_file") is True


def test_filter_by_role_applies_whitelist(registry: ToolRegistry):
    registry.set_role_permissions("viewer", ["read_file"])
    names = {t.name for t in registry.filter_by_role("viewer", registry.list_tools())}
    assert names == {"read_file"}


# ── ["*"] 与空列表 = 全部允许 ──────────────────────────────────────────


def test_star_means_all(registry: ToolRegistry):
    registry.set_role_permissions("viewer", ["read_file"])
    assert registry.is_allowed("viewer", "write_file") is False
    registry.set_role_permissions("viewer", ["*"])
    assert registry.is_allowed("viewer", "write_file") is True


def test_empty_list_means_all(registry: ToolRegistry):
    registry.set_role_permissions("viewer", ["read_file"])
    assert registry.is_allowed("viewer", "run_python") is False
    registry.set_role_permissions("viewer", [])
    assert registry.is_allowed("viewer", "run_python") is True


# ── enforce + ledger 留痕 ──────────────────────────────────────────────


def test_enforce_allowed_returns_true_no_event(registry: ToolRegistry, tmp_path: Path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    registry.bind_ledger(ledger)
    assert registry.enforce("viewer", "read_file") is True
    assert ledger.count("tool_permission_denied") == 0


def test_enforce_denied_records_ledger(registry: ToolRegistry, tmp_path: Path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    registry.bind_ledger(ledger)
    registry.set_role_permissions("viewer", ["read_file"])
    assert registry.enforce("viewer", "run_python") is False
    assert ledger.count("tool_permission_denied") == 1
    events = list(ledger.events("tool_permission_denied"))
    assert events[0].data["role"] == "viewer"
    assert events[0].data["tool_name"] == "run_python"


def test_enforce_without_ledger_still_denies(registry: ToolRegistry):
    # 未绑定 ledger 时，enforce 仍返回 False（不抛异常）。
    registry.set_role_permissions("viewer", ["read_file"])
    assert registry.enforce("viewer", "run_python") is False
