"""安全路径校验与原子写（spec §9.3）。

``safe_relpath`` 是所有 LLM 可控字符串进入文件系统前的唯一闸门，按 spec §9.3 的
7 条规则逐条校验；``atomic_write_*`` 用同目录临时文件 + ``os.replace`` 保证落盘
要么完整、要么不发生。
"""

import hashlib
import os
import re
import secrets
from datetime import datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

#: Windows 保留设备名（去扩展名后大小写不敏感比较）。
_RESERVED_NAMES = frozenset(
    ("CON", "PRN", "AUX", "NUL")
    + tuple(f"COM{i}" for i in range(1, 10))
    + tuple(f"LPT{i}" for i in range(1, 10))
)


class UnsafePathError(ValueError):
    """不安全的相对路径：绝对路径、路径穿越、Windows 保留名等。"""


def safe_relpath(raw: str) -> PurePosixPath:
    """校验并规范化一个相对路径；任一条规则不过即抛 ``UnsafePathError``。

    规则（spec §9.3）：非空且无 NUL 字符；拒绝绝对路径（``/`` 开头、盘符、UNC）；
    拒绝任何 ``..`` 片段（在规范化之前逐段检查，``a/./../b`` 也必须拒绝）；拒绝
    Windows 保留名（去扩展名后大小写不敏感）；拒绝以空格或点结尾的片段（Windows
    会静默截断）；规范化重复分隔符与 ``.`` 片段。非 ASCII 文件名（如中文）放行。
    """
    if "\x00" in raw:
        raise UnsafePathError(f"路径含 NUL 字符: {raw!r}")

    # 先统一反斜杠，盘符与 UNC 才会以正斜杠形式暴露出来。
    unified = raw.replace("\\", "/")
    if unified.startswith("//"):
        raise UnsafePathError(f"拒绝 UNC 路径: {raw!r}")
    if unified.startswith("/"):
        raise UnsafePathError(f"拒绝绝对路径: {raw!r}")

    segments: list[str] = []
    for segment in unified.split("/"):
        if segment == "..":
            raise UnsafePathError(f"拒绝 '..' 片段: {raw!r}")
        if segment in ("", "."):
            continue
        if segment[-1] in (" ", "."):
            raise UnsafePathError(
                f"片段以空格或点结尾（Windows 会静默截断）: {segment!r}"
                f"（原始输入 {raw!r}）"
            )
        if segment.split(".", 1)[0].upper() in _RESERVED_NAMES:
            raise UnsafePathError(f"拒绝 Windows 保留名: {segment!r}（原始输入 {raw!r}）")
        segments.append(segment)

    if not segments:
        raise UnsafePathError(f"空路径: {raw!r}")
    # 盘符检查放在去掉 "." / 空片段之后（"./C:/x" 同样从盘符逃逸），且按前缀匹配：
    # "C:x" 是 drive-relative 路径，join 到 root 后仍会逃逸。Windows 文件名不允许
    # 冒号，因此不存在被误伤的合法名。
    if re.match(r"[A-Za-z]:", segments[0]):
        raise UnsafePathError(f"拒绝盘符路径: {raw!r}")

    return PurePosixPath(*segments)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写入字节：同目录临时文件写入后 ``os.replace`` 覆盖目标。

    临时文件与目标同目录（跨盘 ``os.replace`` 非原子且会直接失败），命名
    ``.{name}.{uuid}.tmp``；任何失败路径都在 ``finally`` 中清理残留。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """以 UTF-8（不加 BOM）原子写入文本。"""
    atomic_write_bytes(path, text.encode("utf-8"))


def read_text(path: Path) -> str:
    """以 UTF-8 读取文本（与 ``atomic_write_text`` 对称，不依赖系统区域编码）。"""
    return path.read_text(encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    """返回数据的 sha256 摘要：64 位小写十六进制。"""
    return hashlib.sha256(data).hexdigest()


def tree(root: Path, max_entries: int = 300) -> list[str]:
    """列出 ``root`` 下所有文件的 POSIX 相对路径，排序后截断到 ``max_entries`` 条。

    只含文件（目录本身与空目录不出现）；不跟随符号链接目录（``rglob`` 既有语义）。
    """
    entries = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )
    return entries[:max_entries]


def slugify_run_id(now: datetime) -> str:
    """生成 run_id：``YYYYMMDD-HHMMSS-<4 位小写 hex>``，如 ``20261005-120000-abcd``。"""
    return now.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
