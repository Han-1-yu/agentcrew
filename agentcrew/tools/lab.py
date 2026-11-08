"""R24：工具实验台——把注册表里的工具单独拎出来试跑。

管理页、CLI 与 HTTP API 共用这一份实现，四条边界：

1. **同一条校验**：试跑用 :func:`validate_tool_args` 校验入参，与 Agent 的
   ``<tool_call>`` 路径完全一致——「页面上能跑」与「Agent 能跑」不会出现
   两套语义。
2. **沙箱隔离**：每次试跑在 ``<output_dir>/_tool_trial/<id>/`` 下建空目录；
   读类工具以它为根，写工具只写它的 ``workspace/``（与 Crew 装配口径一致）。
   试跑永远碰不到 run 目录、产物与仓库文件。
3. **默认关闭**：``tools_enabled=False`` 时拒绝试跑由调用方执行（CLI / API
   返回明确错误）——「调试页」不能把工具执行面暴露给没开工具的部署。
4. **墙钟护栏**：工具自身的 ``timeout`` 之外，这里再加一层 ``asyncio.wait_for``，
   防止第三方工具（MCP 适配器）内部死循环把请求挂死。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentcrew.tools.builtins import (
    GrepTool,
    ListFilesTool,
    ReadFileTool,
    RunPythonTool,
    WriteFileTool,
)
from agentcrew.tools.network import WebFetchTool
from agentcrew.tools.registry import ToolRegistry
from agentcrew.tools.schema import validate_tool_args

#: 试跑沙箱的根目录名（相对 ``output_dir``）。
TRIAL_DIR_NAME = "_tool_trial"

#: 单次试跑的墙钟上限（秒）。
TRIAL_TIMEOUT_SECONDS = 60.0


class ToolLabError(ValueError):
    """试跑请求本身不合法（未知工具 / 入参校验失败）。运行时失败走返回值。"""


def build_tools(
    root: Path, *, create_workspace: bool = True, code_exec: bool = False,
) -> list[Any]:
    """在一组根目录上装配内置工具（与 ``Crew._build_tool_registry`` 同源）。

    ``create_workspace=False`` 用于**只读元数据**场景（管理页列清单）：
    构造工具本身不建目录，避免为了一次列清单在 CWD 里留下 ``workspace/``。

    ``code_exec``（P1）与生产装配保持同一口径：``run_python`` 只在
    ``settings.code_exec_enabled`` 打开时才出现。否则「Agent 调不到、实验台却能
    跑」——调试入口就成了绕过那道开关的后门，而它恰恰是最容易被暴露的一面。
    """
    workspace = root / "workspace"
    if create_workspace:
        workspace.mkdir(parents=True, exist_ok=True)
    tools: list[Any] = [
        ReadFileTool(workdir=root),
        ListFilesTool(workdir=root),
        GrepTool(workdir=root),
    ]
    if code_exec:
        tools.append(RunPythonTool(workdir=root))
    tools.extend((WriteFileTool(workdir=workspace), WebFetchTool()))
    return tools


def build_trial_registry(
    root: Path, *, create_workspace: bool = True, code_exec: bool = False,
) -> ToolRegistry:
    """试跑用注册表（无角色权限：实验台是管理员显式动作，不走 Agent 的授权链）。"""
    registry = ToolRegistry()
    for tool in build_tools(
            root, create_workspace=create_workspace, code_exec=code_exec):
        registry.register(tool)
    return registry


def _kind(tool: Any) -> str:
    return "network" if tool.name == "web_fetch" else "sandbox"


def describe_tools(code_exec: bool = False) -> list[dict[str, Any]]:
    """工具清单：名称 / 说明 / 入参 schema / 类别。管理页与 CLI 共用。

    构造工具只注入 ``workdir`` 字段，不建目录、不执行——纯元数据读取。
    ``code_exec`` 为假时 ``run_python`` 不在清单里（它没被注册，列出来是误导）。
    """
    registry = build_trial_registry(
        Path("."), create_workspace=False, code_exec=code_exec)
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "kind": _kind(tool),
            "input_schema": tool.input_schema,
        }
        for tool in sorted(registry.list_tools(), key=lambda item: item.name)
    ]


def new_sandbox(output_dir: Path) -> Path:
    """为一次试跑建独立沙箱目录并返回。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(output_dir) / TRIAL_DIR_NAME / f"{stamp}-{uuid4().hex[:6]}"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def run_trial(
    tool_name: str,
    args: dict[str, Any],
    *,
    output_dir: Path,
    sandbox: Path | None = None,
    timeout: float = TRIAL_TIMEOUT_SECONDS,
    code_exec: bool = False,
) -> dict[str, Any]:
    """跑一次单工具试跑，返回结果字典（**不抛运行时异常**）。

    返回：``{tool, ok, result, error, duration_ms, sandbox}``；
    ``sandbox`` 是相对 ``output_dir`` 的路径，便于人去盘上复核。

    ``sandbox`` 省略时每次新建独立目录；显式给出则复用（调试时先跑
    ``write_file`` 造夹具，再在同一目录试 ``read_file`` / ``grep``）。

    入参形状不合法（未知工具 / schema 不符）抛 :class:`ToolLabError`，
    由调用方映射成 400/404——那是请求错误，不是工具执行失败。
    """
    root = Path(sandbox) if sandbox is not None else new_sandbox(output_dir)
    registry = build_trial_registry(root, code_exec=code_exec)
    tool = registry.get(tool_name)
    if tool is None:
        known = ", ".join(sorted(item.name for item in registry.list_tools()))
        raise ToolLabError(f"未知工具：{tool_name!r}（可用：{known}）")
    errors = validate_tool_args(tool.input_schema, args)
    if errors:
        raise ToolLabError("入参校验失败：" + "；".join(errors))

    started = time.perf_counter()
    ok = True
    result: str | None = None
    error: str | None = None
    try:
        result = await asyncio.wait_for(tool.run(**args), timeout=timeout)
    except asyncio.TimeoutError:
        ok = False
        error = f"试跑超时（>{timeout:.0f}s），工具已中止"
    except Exception as exc:  # noqa: BLE001 —— 试跑不得把异常抛给调用方
        ok = False
        error = f"{type(exc).__name__}：{exc}"
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "tool": tool_name,
        "ok": ok,
        "result": result,
        "error": error,
        "duration_ms": duration_ms,
        "sandbox": str(root.relative_to(Path(output_dir))).replace("\\", "/"),
    }
