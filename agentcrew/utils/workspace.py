"""run 工作区上下文（contextvars 实现；零依赖，任何分层都可安全 import）。

R60：外部 coding CLI（Claude Code / Codex）作为**真 agentic 执行体**时，需要在
run 自己的工作区里自主读写文件，而不是在服务进程的当前目录里乱写。工作区路径
是 run 级概念（``<run_dir>/workspace``），由 :mod:`agentcrew.crew.crew` 在 run
开始、``run_dir`` 确定后写入；:class:`~agentcrew.models.providers.cli_agentic.
AgenticCliProvider` 在起子进程时读取它作为 ``cwd``。

与 :mod:`agentcrew.utils.trace` 同一套模式与约束：

- **写入点**：crew 在 run 执行入口 :func:`set_workspace`，退出 :func:`reset_workspace`
  （finally 保证）；
- **读取点**：agentic CLI provider 起子进程前 :func:`current_workspace`；
- **隔离性**：contextvar 按协程隔离；未设置时返回 ``None``——此时 agentic provider
  明确报错（绝不退回到服务进程 cwd 乱写），普通 provider 完全不读它，行为不变。

纯内存、零 IO、零锁：它只是「当前协程的 run 工作区在哪」的声明，不是文件系统抽象。
"""

from __future__ import annotations

import contextvars
from pathlib import Path

_workspace_var: contextvars.ContextVar[Path | None] = (
    contextvars.ContextVar("agentcrew_workspace", default=None))


def current_workspace() -> Path | None:
    """返回当前协程的 run 工作区；未设置时返回 ``None``。"""
    return _workspace_var.get()


def set_workspace(path: Path) -> contextvars.Token[Path | None]:
    """设置 run 工作区并返回 token（配对 :func:`reset_workspace` 使用）。"""
    return _workspace_var.set(path)


def reset_workspace(token: contextvars.Token[Path | None]) -> None:
    """恢复到 :func:`set_workspace` 之前的状态（``finally`` 中调用）。"""
    _workspace_var.reset(token)
