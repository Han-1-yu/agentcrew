"""跨进程安全的 append-only JSONL（C14）。

多 worker 同时写 ``experiences.jsonl`` / ``entities.jsonl`` 时会撞两个坑，8 worker
实测（``bench/queue-2026-11-16.md``）直接让 8 个 run 崩在 ``Experience`` 校验上：

1. **写**：Python 的 ``open("a")`` 带缓冲，一条记录可能被拆成多次 write，与别的
   进程交错后落成半行。这里用「跨进程文件锁 + 单次 ``os.write``」——锁保证同一
   时刻只有一个写者，单次写保证一条记录在系统调用层面连续。
2. **读**：读方随时可能撞上别人正在写的半行。**读必须宽容**——跳过坏行，而不是
   让一条坏记录把整个 run 崩掉（经验库是增强，不是正确性前提）。

锁的边界：本地多进程用 ``msvcrt`` / ``fcntl`` 是可靠的；NFS 上的锁语义取决于
挂载参数。所以读宽容是**必须**的第二层，不能只靠锁。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["append_line", "read_lines"]


@contextmanager
def _exclusive(fd: int) -> Iterator[None]:
    """对 fd 加排它锁；平台不支持锁时退化为「单次写」（读宽容兜底）。"""
    try:
        if os.name == "nt":  # pragma: no cover - 只在 Windows 上走到
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - POSIX
            import fcntl

            # Windows 的类型存根里没有 fcntl 的成员，这一支只在 POSIX 走到。
            fcntl.flock(fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except (ImportError, OSError):
        # 某些文件系统不支持锁：不阻塞写入，靠单次 write + 读宽容兜底。
        yield


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
    lock_fd = os.open(
        target.with_name(target.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        with _exclusive(lock_fd):
            fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                os.close(fd)
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
