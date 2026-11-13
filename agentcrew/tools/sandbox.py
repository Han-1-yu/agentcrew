"""可插拔执行后端（C2）：``local`` 默认、``docker`` 可选，接口统一。

**为什么要有这一层。** R35 起 ``run_python`` 默认不注册，理由写在
``docs/adr/0002-run-python-off-by-default.md``：它有 ``-I`` / 裁剪环境 / ``cwd``
限制 / 超时杀进程，但**没有**网络、资源与系统调用隔离 —— 一段模型写的代码可以
出网、可以吃满 CPU。也就是说，缺的从来不是「能不能跑」，而是「敢不敢跑」。

本模块把「在哪里跑」抽成后端：

- :class:`LocalBackend`（默认）：与 R35 之前**逐字一致**的进程内子进程执行
  （``-I``、裁剪环境、``cwd``、超时即杀），不新增任何隔离承诺；
- :class:`DockerBackend`（可选）：容器内执行 —— 默认禁网、非 root、cap 全丢、
  只挂载工作区、CPU/内存/PID 上限。没有 Docker 时**直接报错**（fail loud），
  绝不静默退回 local：那会让操作者以为自己有沙箱。

两条边界是刻意的：

- **不引入第三方依赖**：Docker 后端就是拼 ``docker`` CLI 参数，不引 docker SDK；
  和仓库「零重型依赖」的口径一致。
- **不假装 microVM**：gVisor / Firecracker 留在接口后面（新增一个后端即可），
  本轮不实现，也不在文档里暗示已经做到。

工具层（:class:`~agentcrew.tools.builtins.RunPythonTool`、
:class:`~agentcrew.tools.testing.RunTestsTool`）只认 :class:`ExecutionBackend`，
因此「换后端」不需要改任何工具代码。本模块位于 ``tools`` 层，不依赖
``runtime`` / ``crew`` / ``agents``（架构护栏）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_DOCKER_IMAGE",
    "DockerBackend",
    "ExecutionBackend",
    "ExecutionResult",
    "LocalBackend",
    "SandboxError",
    "build_backend",
    "docker_available",
    "default_container_user",
]

#: 容器内的工作区挂载点。宿主机 ``workdir`` 一一对应挂到这里。
CONTAINER_WORKDIR = "/workspace"

#: docker 后端默认镜像。跑 ``run_tests`` 需要镜像里带 pytest —— 生产换成自己的
#: 镜像（``sandbox_image``）时请把 pytest 一起打进去。
DEFAULT_DOCKER_IMAGE = "python:3.12-slim"

#: 子进程保留的环境变量（与 P1 的 ``run_python`` 完全同一份，local 模式不允许
#: 因为重构而多漏一个变量出去）。
ENV_WHITELIST: tuple[str, ...] = ("PATH", "SYSTEMROOT", "TEMP", "TMP")

#: 单次执行回灌给模型的最大字符数（stdout + stderr 各自截断）。
DEFAULT_MAX_OUTPUT_CHARS = 6000

#: docker 参数里给 /tmp 的大小：pytest 要写临时文件，只读根文件系统下必须给
#: 一个可写的 tmpfs，否则「能跑」变成「跑不起来」。
_TMPFS_SIZE = "64m"


class SandboxError(RuntimeError):
    """后端装配/可用性错误（不是执行结果）。

    设计上**只**用于「选了这个后端但环境不满足」这一类问题：Docker 不可用、
    镜像名为空等。它必须冒泡到运维眼前（fail loud），不能被收敛成一段错误
    文本让模型自己重试 —— 模型改不了宿主机的 Docker。
    """


@dataclass(frozen=True)
class ExecutionResult:
    """一次执行的结果。``timed_out=True`` 时 ``exit_code`` 是本实现约定的 ``-1``。"""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    backend: str = "local"
    #: 后端自己补充的说明（例如容器名、被杀的进程组），只进审计不进模型上下文。
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def tail(self, limit: int = DEFAULT_MAX_OUTPUT_CHARS) -> str:
        """把 stdout/stderr 拼成可直接回灌模型的文本（各自截断到 ``limit``）。"""
        parts: list[str] = []
        if self.stdout.strip():
            parts.append("stdout:\n" + _clip(self.stdout, limit))
        if self.stderr.strip():
            parts.append("stderr:\n" + _clip(self.stderr, limit))
        if self.timed_out:
            parts.append("（执行超时，进程已被终止）")
        parts.append(f"exit_code={self.exit_code}")
        return "\n".join(parts)


def _clip(text: str, limit: int) -> str:
    """保留尾部：报错的有效信息通常在最后几行。"""
    stripped = text.rstrip()
    if len(stripped) <= limit:
        return stripped
    return f"…（前面省略 {len(stripped) - limit} 字符）\n" + stripped[-limit:]


def _crashed_without_output(result: ExecutionResult) -> bool:
    """子进程以「崩溃码 + 零输出」结束：解释器/运行时没起来，而不是代码报错。

    Windows 的崩溃退出码落在 ``0xC0000000`` 以上（如 ``0xC0000374`` 堆损坏），
    POSIX 被信号杀死是负数。正常失败（pytest exit 1、语法错 exit 1）不在判定内。
    """
    if result.timed_out or result.stdout.strip() or result.stderr.strip():
        return False
    return result.exit_code < 0 or result.exit_code >= 0xC0000000


def _trimmed_env() -> dict[str, str]:
    """只保留白名单环境变量（local 后端的既有口径，逐字不变）。"""
    env = {name: os.environ.get(name, "") for name in ENV_WHITELIST}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class ExecutionBackend(ABC):
    """执行后端的统一接口：``run(argv)`` + ``run_python(code)``。"""

    #: 后端标识（进 ``tool_result`` 与 ledger，便于回答「这段代码在哪跑的」）。
    name: str = "local"

    @property
    @abstractmethod
    def python(self) -> str:
        """容器/进程内可用的 Python 可执行文件名或路径。

        ``run_tests`` 等工具拿它拼 argv：local 是 ``sys.executable``，docker 是
        容器里的 ``python3``。**不要让工具自己判断后端**——那是这一层的事。
        """

    @abstractmethod
    async def run(
        self,
        argv: Sequence[str],
        *,
        workdir: Path,
        timeout: float,
    ) -> ExecutionResult:
        """在 ``workdir`` 里执行一条命令，返回结果（超时即杀，不抛异常）。"""

    async def run_python(
        self, code: str, *, workdir: Path, timeout: float
    ) -> ExecutionResult:
        """执行一段 Python 源码（默认实现：``python -I -c code``）。"""
        return await self.run(
            [self.python, "-I", "-c", code], workdir=workdir, timeout=timeout
        )


class LocalBackend(ExecutionBackend):
    """进程内子进程执行：**与 R35 之前逐字一致**，不新增隔离承诺。

    已有的边界（都有对应测试）：``-I`` 隔离模式、裁剪环境变量、``cwd`` 锁在
    工作区内、超时即 ``kill``。没有的边界：网络隔离、CPU/内存上限、系统调用
    沙箱 —— 需要这些请显式选 ``sandbox_backend=docker``。
    """

    name = "local"

    @property
    def python(self) -> str:
        return sys.executable

    async def run(
        self,
        argv: Sequence[str],
        *,
        workdir: Path,
        timeout: float,
    ) -> ExecutionResult:
        result = await self._run_once(argv, workdir=workdir, timeout=timeout)
        # Windows 上偶发「子进程启动即崩、零输出」（STATUS_HEAP_CORRUPTION 一类，
        # 本机跑 15 次脚本复现 0 次、pytest 里连续快速起进程时偶发）。这类崩溃
        # 发生在解释器启动阶段、与被执行的代码无关，**重试一次**既能把环境噪声
        # 消掉，又不会掩盖真实问题：同样的代码崩两次仍然如实返回失败。
        if _crashed_without_output(result):
            result = await self._run_once(argv, workdir=workdir, timeout=timeout)
        return result

    async def _run_once(
        self,
        argv: Sequence[str],
        *,
        workdir: Path,
        timeout: float,
    ) -> ExecutionResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_trimmed_env(),
            )
        except OSError as exc:
            return ExecutionResult(
                exit_code=-1, stderr=f"无法启动子进程：{exc}", backend=self.name
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecutionResult(
                exit_code=-1, timed_out=True, backend=self.name
            )
        return ExecutionResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            backend=self.name,
        )


def docker_available(which: Callable[[str], str | None] | None = None) -> bool:
    """宿主机上是否有可用的 ``docker`` 可执行文件（纯探测，不启动容器）。"""
    probe = which or shutil.which
    return probe("docker") is not None


def default_container_user() -> str:
    """容器内默认跑什么 uid:gid。

    取宿主当前 uid/gid：唯一挂进容器的 ``/workspace`` 属于宿主用户，容器 uid
    与它不一致时模型连自己的产物都写不进去（真容器实测 ``Permission denied``）。
    Windows 没有 uid 概念，退回 ``65534:65534``（nobody）。两种取值都**不是 root**。
    """
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if callable(getuid) and callable(getgid):
        uid, gid = getuid(), getgid()
        if uid != 0:
            return f"{uid}:{gid}"
    return "65534:65534"


class DockerBackend(ExecutionBackend):
    """容器内执行：默认禁网、非 root、cap 全丢、资源有上限、只挂工作区。

    参数选择的理由（每一条都能被逃逸用例验证）：

    - ``--network none``：模型写的代码不能出网（数据外发、内网探测都断掉）。
      需要联网时显式 ``sandbox_allow_network=true``，这是运维决定，不是模型决定。
    - ``--user <宿主 uid>:<gid>``（Windows 上退回 ``65534:65534``）+ ``--cap-drop
      ALL`` + ``--security-opt no-new-privileges``：容器内**非 root**、拿不到额外
      能力、无法提权。之所以优先用宿主 uid 而不是固定的 nobody：唯一挂进来的
      ``/workspace`` 是宿主目录，容器 uid 与宿主属主不一致时，模型连自己的产物都
      写不进去（真容器上实测 ``Permission denied: 'out.txt'``）；宿主 uid 在
      绝大多数部署里本来就不是 0，所以「非 root」这条并没有让步。
    - ``--read-only`` + 只读根文件系统 + ``/tmp`` tmpfs：写不进镜像层，也改不了
      宿主；唯一可写的持久位置是挂进来的工作区。
    - ``--memory`` / ``--cpus`` / ``--pids-limit``：内存、CPU、进程数三道上限，
      防「一行 fork 炸弹把宿主机拖死」。
    - ``-v <workdir>:/workspace:rw`` + ``-w /workspace``：**只**挂载本次 run 的
      目录，宿主其余路径在容器里不存在。

    没有 Docker 时构造不报错（便于单测拼参数），但 :meth:`run` 会抛
    :class:`SandboxError`；生产装配走 :func:`build_backend`，那里在构造期就检查，
    早失败早发现。
    """

    name = "docker"

    def __init__(
        self,
        *,
        image: str = DEFAULT_DOCKER_IMAGE,
        memory_mb: int = 512,
        cpus: float = 1.0,
        pids_limit: int = 256,
        allow_network: bool = False,
        binary: str = "docker",
        user: str | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        if not image.strip():
            raise SandboxError("sandbox_image 不能为空")
        if memory_mb < 64:
            raise SandboxError("sandbox_memory_mb 不得小于 64")
        if cpus <= 0:
            raise SandboxError("sandbox_cpus 必须大于 0")
        self.user = user or default_container_user()
        if self.user.split(":", 1)[0].strip() in {"0", "root"}:
            raise SandboxError(
                "容器内不得以 root 运行（--user 的 uid 部分不能是 0）；"
                "留空则自动取宿主当前 uid，Windows 上退回 65534"
            )
        self.image = image.strip()
        self.memory_mb = int(memory_mb)
        self.cpus = float(cpus)
        self.pids_limit = int(pids_limit)
        self.allow_network = bool(allow_network)
        self.binary = binary
        self._which = which

    @property
    def python(self) -> str:
        return "python3"

    def available(self) -> bool:
        return docker_available(self._which)

    def build_argv(
        self, argv: Sequence[str], *, workdir: Path, name: str
    ) -> list[str]:
        """拼出完整的 ``docker run`` 命令行（纯函数，便于单测逐项断言）。"""
        host = Path(workdir).resolve().as_posix()
        flags = [
            self.binary, "run", "--rm", "--name", name,
            "--network", "bridge" if self.allow_network else "none",
            "--user", self.user,
            "--read-only",
            "--tmpfs", f"/tmp:rw,nosuid,nodev,size={_TMPFS_SIZE}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.pids_limit),
            "--memory", f"{self.memory_mb}m",
            "--cpus", f"{self.cpus:g}",
            "-v", f"{host}:{CONTAINER_WORKDIR}:rw",
            "-w", CONTAINER_WORKDIR,
            self.image,
        ]
        return flags + list(argv)

    async def run(
        self,
        argv: Sequence[str],
        *,
        workdir: Path,
        timeout: float,
    ) -> ExecutionResult:
        if not self.available():
            raise SandboxError(
                "sandbox_backend=docker 但宿主机上没有可用的 docker 可执行文件；"
                "请安装/启动 Docker，或改回 sandbox_backend=local（不隔离）"
            )
        if not Path(workdir).is_dir():
            return ExecutionResult(
                exit_code=-1,
                stderr=f"工作目录不存在：{workdir}",
                backend=self.name,
            )
        name = f"agentcrew-{os.getpid()}-{abs(hash((str(workdir), tuple(argv)))) % 10**8}"
        full = self.build_argv(argv, workdir=workdir, name=name)
        # 容器内命令不带宿主机的解释器路径：调用方用 backend.python 拼 argv，
        # 这里不动 argv（换后端不该改语义）。
        try:
            proc = await asyncio.create_subprocess_exec(
                *full,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxError(f"无法启动 docker：{exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            # docker run 被杀后容器可能还在跑，尽力清理（失败不影响结论）。
            await _docker_kill(self.binary, name)
            return ExecutionResult(
                exit_code=-1,
                timed_out=True,
                backend=self.name,
                notes={"container": name},
            )
        return ExecutionResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            backend=self.name,
            notes={"container": name},
        )


async def _docker_kill(binary: str, name: str) -> None:
    """超时后尽力杀掉容器；任何失败都吞掉（结论已经由 timed_out 表达）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "kill", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (OSError, asyncio.TimeoutError):
        return


def build_backend(
    kind: str,
    *,
    image: str = DEFAULT_DOCKER_IMAGE,
    memory_mb: int = 512,
    cpus: float = 1.0,
    allow_network: bool = False,
    which: Callable[[str], str | None] | None = None,
) -> ExecutionBackend:
    """按配置选后端；``docker`` 选了但不可用时**构造期**就报错（fail loud）。

    ``local`` 是默认值，行为与 C2 之前逐字一致；未知取值同样报错——配置拼错
    不该被静默当成 local。
    """
    normalized = (kind or "local").strip().lower()
    if normalized == "local":
        return LocalBackend()
    if normalized == "docker":
        backend = DockerBackend(
            image=image, memory_mb=memory_mb, cpus=cpus,
            allow_network=allow_network, which=which,
        )
        if not backend.available():
            raise SandboxError(
                "sandbox_backend=docker 但宿主机上没有可用的 docker 可执行文件；"
                "请安装/启动 Docker，或改回 sandbox_backend=local（不隔离）"
            )
        return backend
    raise SandboxError(
        f"未知 sandbox_backend：{kind!r}（可选 local / docker）"
    )
