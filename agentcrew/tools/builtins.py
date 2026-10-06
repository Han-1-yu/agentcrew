"""内置沙箱工具：读文件 / 列目录 / grep / 跑 Python 片段（P1-1）。

**全部文件操作都被限制在构造时给定的 ``workdir`` 根内**：模型给的路径先过
:func:`agentcrew.utils.files.safe_relpath`，``../``、绝对路径、盘符、Windows
保留名一律拒绝（返回错误文本回灌，不抛断 Agent）。这样模型即使被诱导也读不出
项目目录之外的东西。

:class:`RunPythonTool` 用独立子进程执行代码：``-I`` 隔离模式、``cwd=workdir``、
裁剪过的环境变量、默认 10 秒超时——生成的代码不在宿主进程内跑，超时即杀。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import Field

from agentcrew.tools.tool import Tool
from agentcrew.utils.files import UnsafePathError, read_text, safe_relpath


def _resolve(workdir: Path, raw: str) -> Path | None:
    """把模型给的相对路径安全解析到 ``workdir`` 内；越界返回 ``None``。

    ``.`` 与空串视作根目录本身（列目录 / grep 从项目根开始是常见调用）。
    """
    if raw.strip() in ("", "."):
        return workdir
    try:
        rel = safe_relpath(raw)
    except UnsafePathError:
        return None
    return workdir / rel


class ReadFileTool(Tool):
    """读取项目目录内的一个文本文件，返回全文。"""

    name: str = "read_file"
    description: str = (
        "读取项目目录内的一个文本文件并返回其全文内容。用于查看上游产物或"
        "项目文件后再动手写代码。"
    )
    input_schema: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "项目内相对路径，如 src/app.py"},
        },
        "required": ["path"],
    })
    workdir: Path
    """沙箱根目录（构造期注入，对模型不可见）。"""

    async def run(self, path: str = "", **kwargs: Any) -> str:
        target = _resolve(self.workdir, path)
        if target is None:
            return f"错误：路径越界或非法：{path!r}（只允许项目目录内的相对路径）"
        if not target.is_file():
            return f"错误：文件不存在：{path}"
        try:
            return read_text(target)
        except OSError as exc:
            return f"错误：读取失败：{exc}"


class ListFilesTool(Tool):
    """列出项目目录内某子目录下的一层条目（文件 + 子目录）。"""

    name: str = "list_files"
    description: str = (
        "列出项目目录内某子目录下的一层文件与子目录（不递归）。"
        "path 默认 '.'（项目根）。"
    )
    input_schema: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "项目内相对路径，默认 '.'"},
        },
    })
    workdir: Path

    async def run(self, path: str = ".", **kwargs: Any) -> str:
        target = _resolve(self.workdir, path)
        if target is None:
            return f"错误：路径越界或非法：{path!r}（只允许项目目录内的相对路径）"
        if not target.is_dir():
            return f"错误：目录不存在：{path}"
        try:
            entries = sorted(
                child.name + ("/" if child.is_dir() else "")
                for child in target.iterdir()
            )
        except OSError as exc:
            return f"错误：列目录失败：{exc}"
        return "\n".join(entries) if entries else "（空目录）"


class GrepTool(Tool):
    """在项目目录内递归正则搜索，返回匹配的文件 / 行号 / 行内容。"""

    name: str = "grep"
    description: str = (
        "在项目目录内递归搜索正则表达式，返回匹配行（文件:行号: 内容）。"
        "path 默认 '.'；结果超过 100 条会截断。"
    )
    input_schema: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索起始目录，默认 '.'"},
        },
        "required": ["pattern"],
    })
    workdir: Path
    _MAX_MATCHES = 100

    async def run(self, pattern: str = "", path: str = ".", **kwargs: Any) -> str:
        if not pattern:
            return "错误：缺少必填参数 pattern"
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"错误：非法正则：{exc}"
        root = _resolve(self.workdir, path)
        if root is None:
            return f"错误：路径越界或非法：{path!r}（只允许项目目录内的相对路径）"
        if not root.is_dir():
            return f"错误：目录不存在：{path}"
        matches: list[str] = []
        for file_path in sorted(p for p in root.rglob("*") if p.is_file()):
            try:
                text = read_text(file_path)
            except (OSError, UnicodeDecodeError):
                continue
            rel = file_path.relative_to(self.workdir).as_posix()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{rel}:{lineno}: {line}")
                    if len(matches) >= self._MAX_MATCHES:
                        return "\n".join(matches) + f"\n…（已截断到 {self._MAX_MATCHES} 条）"
        return "\n".join(matches) or "（无匹配）"


class RunPythonTool(Tool):
    """在独立子进程里执行一段 Python 片段，返回 stdout / stderr / exit code。"""

    name: str = "run_python"
    description: str = (
        "在隔离的子进程中执行一段 Python 代码片段并返回 stdout/stderr。"
        "工作目录被限制在项目根内；默认超时 10 秒。"
    )
    input_schema: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码片段"},
            "timeout": {"type": "number", "description": "超时秒数，默认 10"},
        },
        "required": ["code"],
    })
    workdir: Path
    default_timeout: float = 10.0

    async def run(self, code: str = "", timeout: float | None = None,
                  **kwargs: Any) -> str:
        if not code:
            return "错误：缺少必填参数 code"
        limit = float(timeout) if timeout else self.default_timeout
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
            "PYTHONIOENCODING": "utf-8",
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", code,
                cwd=str(self.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            return f"错误：无法启动子进程：{exc}"
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=limit)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"错误：执行超时（>{limit:g}s），已终止进程"
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        parts: list[str] = []
        if out.strip():
            parts.append("stdout:\n" + out.rstrip())
        if err.strip():
            parts.append("stderr:\n" + err.rstrip())
        parts.append(f"exit_code={proc.returncode}")
        return "\n".join(parts)
