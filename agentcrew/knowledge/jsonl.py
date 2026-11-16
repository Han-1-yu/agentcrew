"""跨进程安全的 append-only JSONL（C14）。

多 worker 同时写 ``experiences.jsonl`` / ``entities.jsonl`` 时会撞两个坑，8 worker
实测（``bench/queue-2026-11-16.md``）直接让 8 个 run 崩在 ``Experience`` 校验上：

1. **写**：Python 的 ``open("a")`` 带缓冲，一条记录可能被拆成多次 write，与别的
   进程交错后落成半行。这里用「跨进程文件锁 + 单次 ``os.write``」——锁保证同一
   时刻只有一个写者，单次写保证一条记录在系统调用层面连续。
2. **读**：读方随时可能撞上别人正在写的半行。**读必须宽容**——跳过坏行，而不是
   让一条坏记录把整个 run 崩掉（经验库是增强，不是正确性前提）。

锁本身也要容错：多个进程同时 ``O_CREAT`` 同一个刚出现的锁文件时，Windows 会直接
回 ``PermissionError``（共享冲突；实测 4 进程并发写约一半概率命中）。「打不开锁」
与「拿不到锁」是两件事——前者退化为无锁单次写并告警，**不让写方抛出去**（见
:func:`_open_lock`）。

锁的边界：本地多进程用 ``msvcrt`` / ``fcntl`` 是可靠的；NFS 上的锁语义取决于
挂载参数。所以读宽容是**必须**的第二层，不能只靠锁。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

__all__ = ["append_line", "read_lines"]


@contextmanager
def _exclusive(fd: int) -> Iterator[None]:
    """对 fd 加排它锁；平台不支持锁时退化为「单次写」（读宽容兜底）。"""
    releaser: Callable[[], None] | None = None
    try:
        if os.name == "nt":  # pragma: no cover - 只在 Windows 上走到
            import msvcrt

            # 模块级 Any 别名：mypy 在 Linux 上跑时 msvcrt 存根没有这些成员，
            # 反过来 fcntl 在 Windows 存根里也没有。Any 让两支在两端都通过，
            # 又不必逐平台写 type: ignore（ruff 的 B009 还会把 getattr 改回去）。
            mod: Any = msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            # 必须用非阻塞重试：LK_LOCK 等不到锁会直接抛 OSError，退化成无锁
            # 写就会在高并发下产生半行（4 进程实测偶发）。宁可等 10 秒。
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    mod.locking(fd, mod.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.005)

            def _release_nt() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                mod.locking(fd, mod.LK_UNLCK, 1)

            releaser = _release_nt
        else:  # pragma: no cover - POSIX
            import fcntl

            posix: Any = fcntl
            posix.flock(fd, posix.LOCK_EX)

            def _release_posix() -> None:
                posix.flock(fd, posix.LOCK_UN)

            releaser = _release_posix
    except (ImportError, OSError):
        # 文件系统不支持锁（或 10 秒仍拿不到）：不阻塞写入，读宽容兜底。
        yield
        return
    try:
        yield
    finally:
        if releaser is not None:
            try:
                releaser()
            except OSError:
                pass


def _open_lock(path: Path) -> int | None:
    """打开（必要时创建）锁文件；**短暂争用重试**，仍拿不到返回 ``None``。

    为什么不能直接 ``os.open``：多个进程**同时** ``O_CREAT`` 同一个刚出现的锁文件
    时，Windows 上会直接回 ``PermissionError: [Errno 13]``（共享冲突）——实测 4
    进程并发写 ``experiences.jsonl`` 时约一半概率命中，且发生在**第一行**：写方
    直接抛错崩掉，整个 run 陪葬。这不是「锁没拿到」，是文件根本没打开成功，所以
    :func:`_exclusive` 里的重试管不到它。

    退化策略与「文件系统不支持锁」一致（见模块 docstring）：拿不到锁就不锁，退回
    「单次 ``os.write``」，由读侧宽容兜底。经验库是增强，不是正确性前提——为它让
    run 崩掉是本末倒置。
    """
    deadline = time.monotonic() + 2.0
    while True:
        try:
            return os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        except PermissionError:
            if time.monotonic() >= deadline:
                _logger.warning(
                    "锁文件打不开（%s），本次退回无锁追加；读侧会跳过坏行。", path)
                return None
            time.sleep(0.005)


def _append_once(target: Path, data: bytes) -> None:
    """单次 ``os.write`` 落一整块（加锁路径与退化路径共用）。"""
    fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def append_line(path: Path, line: str) -> None:
    """把一行（或多行拼接的块）追加进 JSONL；自动补 ``\\n`` 并建父目录。"""
    data = line.encode("utf-8")
    if not data.endswith(b"\n"):
        data += b"\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 锁放在**独立的锁文件**上，不锁数据文件：Windows 上对已加区域锁的数据文件
    # 再 open(O_WRONLY) 会直接 PermissionError（实测），而锁一个从不参与写的数据
    # 文件旁边的空文件没有这个问题。锁文件常驻、不删——删除会引入新的竞态。
    lock_fd = _open_lock(target.with_name(target.name + ".lock"))
    if lock_fd is None:
        _append_once(target, data)     # 退化：无锁单次写，读宽容兜底
        return
    try:
        with _exclusive(lock_fd):
            _append_once(target, data)
    finally:
        os.close(lock_fd)


def read_lines(path: Path) -> Iterator[str]:
    """逐行产出非空行；文件不存在返回空迭代。**不校验 JSON**（调用方负责宽容）。"""
    target = Path(path)
    if not target.is_file():
        return
    try:
        with open(target, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    yield stripped
    except OSError:
        return
