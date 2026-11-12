"""内置沙箱工具：读文件 / 列目录 / grep / 跑 Python 片段（P1-1）。

**全部文件操作都被限制在构造时给定的 ``workdir`` 根内**：模型给的路径先过
:func:`agentcrew.utils.files.safe_relpath`，``../``、绝对路径、盘符、Windows
保留名一律拒绝（返回错误文本回灌，不抛断 Agent）。这样模型即使被诱导也读不出
项目目录之外的东西。

:class:`RunPythonTool` 用独立子进程执行代码：``-I`` 隔离模式、``cwd=workdir``、
裁剪过的环境变量、默认 10 秒超时——生成的代码不在宿主进程内跑，超时即杀。

C2 起「在哪里跑」抽成 :class:`~agentcrew.tools.sandbox.ExecutionBackend`：
默认仍是上面这条 local 路径（行为逐字不变），显式选 ``sandbox_backend=docker``
时才换成容器执行（禁网、非 root、资源上限、只挂工作区）。
"""

from __future__ import annotations

import math
import re
from pathlib import Path, PurePath
from typing import Any

from pydantic import Field

from agentcrew.tools.sandbox import ExecutionBackend, LocalBackend
from agentcrew.tools.tool import Tool
from agentcrew.utils.files import (
    UnsafePathError,
    atomic_write_text,
    is_link_like,
    read_text,
    safe_relpath,
)


def _crosses_link(workdir: Path, rel: PurePath) -> bool:
    """从 ``workdir`` 到 ``workdir / rel`` 的每一段是否经过符号链接 / 重解析点。

    :func:`safe_relpath` 只做词法规范化：``link/file.txt`` 完全合法，但如果
    ``link`` 是指向项目外的符号链接，``target.is_file()`` / ``read_text()``
    会跟着链接把项目外的文件读出来。``ArtifactStore`` 早已用 ``is_link_like``
    逐段拒绝写入路径，这里把同一条边界补到读工具上。
    """
    current = workdir
    for part in rel.parts:
        current = current / part
        try:
            if is_link_like(current):
                return True
        except OSError:
            # 该段（及其后代）不存在，不可能再冒出链接；让调用方给出
            # 「文件不存在」而不是误报「路径越界」。
            return False
    return False


def resolve_in_workdir(workdir: Path, raw: str) -> Path | None:
    """把模型给的相对路径安全解析到 ``workdir`` 内；越界或经链接返回 ``None``。

    ``.`` 与空串视作根目录本身（列目录 / grep 从项目根开始是常见调用）。
    公开出来是给 ``run_tests`` 等同类工具复用同一条路径边界——每个工具各写一份
    路径校验，迟早会漂移出一条更宽松的。
    """
    if raw.strip() in ("", "."):
        return workdir
    try:
        rel = safe_relpath(raw)
    except UnsafePathError:
        return None
    if _crosses_link(workdir, rel):
        return None
    return workdir / rel


def _resolve(workdir: Path, raw: str) -> Path | None:
    """内部别名（保留旧名，避免一次性改掉所有调用点）。"""
    return resolve_in_workdir(workdir, raw)


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
        for file_path in sorted(root.rglob("*")):
            if not file_path.is_file():
                continue
            # rglob 不会下钻链接目录，但链接到项目外的**文件**仍会被 is_file()
            # 认作普通文件；逐段拒掉，避免把外部内容塞进模型上下文。
            try:
                if _crosses_link(self.workdir, file_path.relative_to(self.workdir)):
                    continue
            except (OSError, ValueError):
                continue
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
    #: C2：执行后端（构造期注入）。``None`` = local，与引入后端之前完全一致。
    #: 注解用 ``Any`` 是为了让 pydantic 直接接受后端实例（Tool 带 extra=forbid，
    #: 任意类型字段需要 arbitrary_types_allowed，这里不值得为它放开整个模型）。
    backend: Any = None

    @property
    def _backend(self) -> ExecutionBackend:
        backend = self.backend
        if backend is None:
            return LocalBackend()
        return backend

    async def run(self, code: str = "", timeout: float | None = None,
                  **kwargs: Any) -> str:
        if not code:
            return "错误：缺少必填参数 code"
        if timeout is None:
            limit = self.default_timeout
        else:
            # 模型给的参数不经过任何 schema 强校验：``"abc"`` 会让 float()
            # 抛 ValueError 冒泡断掉 Agent，负数/nan/极大值会让 wait_for 立刻
            # 超时或永远不超时。统一收成可读的错误文本回灌。
            try:
                limit = float(timeout)
            except (TypeError, ValueError):
                return "错误：timeout 必须是数值（秒）"
            if not math.isfinite(limit) or not 0 < limit <= 60:
                return "错误：timeout 必须落在 (0, 60] 秒内"
        result = await self._backend.run_python(
            code, workdir=self.workdir, timeout=limit)
        if result.timed_out:
            return f"错误：执行超时（>{limit:g}s），已终止进程"
        if result.exit_code < 0:
            # 后端连进程都没起来（解释器/容器不可用）：这是环境问题，不是代码问题。
            return f"错误：{result.stderr.strip() or '无法启动子进程'}"
        parts: list[str] = []
        if result.stdout.strip():
            parts.append("stdout:\n" + result.stdout.rstrip())
        if result.stderr.strip():
            parts.append("stderr:\n" + result.stderr.rstrip())
        parts.append(f"exit_code={result.exit_code}")
        return "\n".join(parts)


class WriteFileTool(Tool):
    """在工作区里写一个文本文件（R3：受控写，与产物目录严格隔离）。

    与 :class:`ReadFileTool` 的边界一致（``safe_relpath`` + 逐段链接检查），但
    **只写不提交**：这里的文件不是产物，不进 ``MANIFEST.json``、不过 G1 契约校验，
    也不参与交付。Crew 把它的 ``workdir`` 设成 ``<run_dir>/workspace``，因此模型
    再怎么给路径也碰不到 ``artifacts/``、``tasks.json`` 或 run 目录之外的东西。

    用途是 ReAct 循环里的中间草稿：模型可以先把查到的资料、生成的片段写下来，
    下一轮再用 ``read_file`` 读回去，最终产物仍走带契约的 ``file=`` 围栏。
    """

    name: str = "write_file"
    description: str = (
        "把一个文本文件写入本次 run 的私有工作区，供后续工具读回（中间草稿）。"
        "这里写的文件不是交付产物，不会进入产物清单；最终交付仍须用 file= 围栏输出。"
    )
    input_schema: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "path": {
                "type": "string", "minLength": 1, "maxLength": 200,
                "description": "工作区内的相对路径，如 notes/api.md",
            },
            "content": {
                "type": "string", "maxLength": 200000,
                "description": "要写入的 UTF-8 文本",
            },
        },
        "required": ["path", "content"],
    })
    workdir: Path
    """私有工作区根目录（构造期注入，对模型不可见）。"""
    max_bytes: int = 65536
    """单次写入的 UTF-8 字节上限，防止模型把整篇文档塞进上下文再回灌。"""

    async def run(self, path: str = "", content: str = "", **kwargs: Any) -> str:
        if not isinstance(path, str) or not path.strip():
            return "错误：缺少必填参数 path"
        if not isinstance(content, str):
            return "错误：content 必须是字符串"
        raw = content.encode("utf-8")
        if len(raw) > self.max_bytes:
            return (
                f"错误：内容 {len(raw)} 字节超过上限 {self.max_bytes} 字节，"
                "请拆成多次写入或压缩内容"
            )
        target = _resolve(self.workdir, path)
        if target is None:
            return f"错误：路径越界或非法：{path!r}（只允许工作区内的相对路径）"
        if target == self.workdir or (target.exists() and target.is_dir()):
            return f"错误：path 必须是文件名，不能是目录：{path}"
        try:
            atomic_write_text(target, content)
        except OSError as exc:
            return f"错误：写入失败：{exc}"
        return f"已写入 {path}（{len(raw)} 字节）"
