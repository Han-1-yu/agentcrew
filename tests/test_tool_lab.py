"""R24：工具实验台——单工具试跑（沙箱隔离、同一条入参校验）。

覆盖三层：元数据（清单不建目录、内容稳定）、试跑的沙箱语义（写工具只写
workspace、读工具以沙箱为根、路径越界只报错）与错误分流（未知工具 / 入参
不合法抛 ToolLabError，工具运行时失败走返回值 ok=false）。
"""

from __future__ import annotations

import pytest

from agentcrew.tools.lab import (
    TRIAL_DIR_NAME,
    ToolLabError,
    build_trial_registry,
    describe_tools,
    run_trial,
)


def test_describe_tools_lists_builtins():
    """清单是纯元数据：内置工具、按名字排序、字段齐全。

    ``run_python`` 默认**不在**清单里（code_exec 默认关）；打开后才出现。
    """
    tools = describe_tools()
    names = [item["name"] for item in tools]
    assert names == sorted(names)
    assert set(names) == {
        "read_file", "list_files", "grep", "write_file", "web_fetch",
    }
    for item in tools:
        assert item["description"]
        assert item["kind"] in ("sandbox", "network")
        assert item["input_schema"]["type"] == "object"
    assert next(t for t in tools if t["name"] == "web_fetch")["kind"] == "network"


def test_describe_tools_includes_run_python_when_code_exec_enabled():
    """打开 code_exec 后 run_python 才进清单——与 run 内注册表同一口径。"""
    names = [item["name"] for item in describe_tools(code_exec=True)]
    assert "run_python" in names
    assert len(names) == 6


async def test_write_then_read_roundtrip_in_sandbox(tmp_path):
    """写工具只写 workspace，读工具能从沙箱根读回来——同一目录树的两端。"""
    written = await run_trial(
        "write_file", {"path": "notes/a.txt", "content": "工具实验台"},
        output_dir=tmp_path)
    assert written["ok"] is True
    sandbox = tmp_path / written["sandbox"]
    assert (sandbox / "workspace" / "notes" / "a.txt").is_file()
    assert (sandbox / "workspace" / "notes" / "a.txt").read_text(
        encoding="utf-8") == "工具实验台"

    # 复用同一沙箱：先造夹具再试读（调试会话的常见动作）
    read_back = await run_trial(
        "read_file", {"path": "workspace/notes/a.txt"},
        output_dir=tmp_path, sandbox=sandbox)
    assert read_back["ok"] is True
    assert read_back["result"] == "工具实验台"
    assert read_back["sandbox"] == written["sandbox"]

    # 默认每次新建独立沙箱
    fresh = await run_trial("list_files", {}, output_dir=tmp_path)
    assert fresh["sandbox"] != written["sandbox"]


async def test_write_file_cannot_escape_workspace(tmp_path):
    """写工具的相对路径越界由工具自身拒绝（返回错误文本，不抛异常）。"""
    result = await run_trial(
        "write_file", {"path": "../escape.txt", "content": "x"},
        output_dir=tmp_path)
    assert result["ok"] is True            # 工具正常返回（错误在结果文本里）
    assert "错误" in (result["result"] or "")
    assert not (tmp_path / "escape.txt").exists()


async def test_read_file_escape_reports_instead_of_reading(tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("不该被读到", encoding="utf-8")
    result = await run_trial(
        "read_file", {"path": "../outside.txt"}, output_dir=tmp_path)
    assert result["ok"] is True
    assert "越界" in (result["result"] or "")
    assert "不该被读到" not in (result["result"] or "")


async def test_run_python_requires_code_exec_flag(tmp_path):
    """P1：默认不注册 run_python——试跑入口不得成为绕过该开关的后门。"""
    with pytest.raises(ToolLabError, match="未知工具"):
        await run_trial("run_python", {"code": "print(1)"}, output_dir=tmp_path)


async def test_run_python_executes_in_sandbox(tmp_path):
    result = await run_trial(
        "run_python", {"code": "print(6 * 7)"}, output_dir=tmp_path,
        code_exec=True)
    assert result["ok"] is True
    assert "42" in (result["result"] or "")


async def test_unknown_tool_raises_lab_error(tmp_path):
    with pytest.raises(ToolLabError, match="未知工具"):
        await run_trial("no_such_tool", {}, output_dir=tmp_path)


async def test_invalid_args_raise_lab_error(tmp_path):
    with pytest.raises(ToolLabError, match="入参校验失败"):
        await run_trial("read_file", {}, output_dir=tmp_path)


async def test_trial_sandbox_lives_under_trial_dir(tmp_path):
    result = await run_trial("list_files", {}, output_dir=tmp_path)
    assert result["ok"] is True
    assert result["sandbox"].startswith(TRIAL_DIR_NAME + "/")
    assert (tmp_path / result["sandbox"]).is_dir()


def test_trial_registry_has_no_role_permissions(tmp_path):
    """实验台是管理员显式动作，不走 Agent 的角色授权链。"""
    registry = build_trial_registry(tmp_path)
    assert registry.is_allowed("backend_dev", "read_file") is True
    assert len(registry.list_tools()) == 5            # 默认不含 run_python
    assert len(build_trial_registry(tmp_path, code_exec=True).list_tools()) == 6
