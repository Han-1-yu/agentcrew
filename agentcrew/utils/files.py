"""安全路径校验与原子写（spec §9.3）。

``safe_relpath`` 是所有 LLM 可控字符串进入文件系统前的唯一闸门，按 spec §9.3 的
7 条规则逐条校验；``atomic_write_*`` 用同目录临时文件 + ``os.replace`` 保证落盘
要么完整、要么不发生。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import time
from collections.abc import Sequence
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

    规则（spec §9.3）：非空且无 NUL 字符；拒绝绝对路径（``/`` 开头、UNC）；拒绝
    任何 ``..`` 片段（在规范化之前逐段检查，``a/./../b`` 也必须拒绝）；拒绝任何含
    冒号的片段（Windows 文件名不允许冒号，``C:``/``C:x`` 盘符与 ``x:y`` 数据流一并
    封死）；拒绝 Windows 保留名（去扩展名后大小写不敏感）；拒绝以空格或点结尾的
    片段（Windows 会静默截断）；规范化重复分隔符与 ``.`` 片段。非 ASCII 文件名
    （如中文）放行。
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
        if ":" in segment:
            # 冒号在 Windows 上要么形成盘符（"C:" 逃逸、drive-relative 的 "C:x"
            # join 后同样逃逸 root），要么形成数据流（".x:y.tmp" 会写出基文件
            # ".x"，os.replace 失败且 unlink 删不掉基文件，残渣留在输出目录）。
            raise UnsafePathError(
                f"拒绝含冒号的片段（Windows 文件名不允许冒号）: {segment!r}"
                f"（原始输入 {raw!r}）"
            )
        if segment.split(".", 1)[0].upper() in _RESERVED_NAMES:
            raise UnsafePathError(f"拒绝 Windows 保留名: {segment!r}（原始输入 {raw!r}）")
        segments.append(segment)

    if not segments:
        raise UnsafePathError(f"空路径: {raw!r}")

    return PurePosixPath(*segments)


def normalize_path_scope(scope: str) -> str:
    """把 ``path_scope`` 的配置原值规范化成可比较的路径段前缀。

    配置里写 ``./backend/``、``backend\\``、``backend`` 都应等价于 ``backend``；
    统一反斜杠、去掉前导 ``./``、去掉首尾分隔符。空 scope 返回空串（调用方跳过）。
    """
    unified = scope.replace("\\", "/").strip()
    while unified.startswith("./"):
        unified = unified[2:]
    return unified.strip("/")


def path_in_scope(path: str, scopes: Sequence[str]) -> bool:
    """``path`` 是否落在任一 ``scope`` 之下，按**路径段**比较而非字符串前缀。

    ``startswith("backend")`` 会把 ``backend_secret/x.py`` 误判为在 ``backend``
    域内（多执行体写保护被绕过，可覆盖他人产物）；带尾斜杠的 ``backend/`` 又
    会拒绝恰好等于 ``backend`` 的路径。这里统一成「相等或以 ``scope + '/'``
    开头」，且两侧都先规范化。非法路径一律判 False（fail-closed）。
    """
    try:
        normalized = safe_relpath(path).as_posix()
    except UnsafePathError:
        return False
    for scope in scopes:
        prefix = normalize_path_scope(scope)
        if prefix and (normalized == prefix or normalized.startswith(prefix + "/")):
            return True
    return False


#: os.replace 遇 Windows 瞬时锁（杀软实时扫描 / 并发读取，WinError 5）时的重试参数。
_REPLACE_RETRIES = 5
_REPLACE_RETRY_DELAY = 0.05


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写入字节：同目录临时文件写入后 ``os.replace`` 覆盖目标。

    临时文件与目标同目录（跨盘 ``os.replace`` 非原子且会直接失败），命名
    ``.{name}.{uuid}.tmp``；任何失败路径都在 ``finally`` 中清理残留。

    Windows 上杀软实时扫描或并发读取会对 tmp / 目标持有瞬时共享锁，使
    ``os.replace`` 抛 ``PermissionError``——不重试会让整个 run 因一次文件
    写入崩溃退出（历史故障：tasks.json 被锁 → 进程死亡、run 卡在 running）。
    有界线性退避重试只吸收瞬时钟，真锁超时后原样抛出。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(_REPLACE_RETRY_DELAY * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """以 UTF-8（不加 BOM）原子写入文本。"""
    atomic_write_bytes(path, text.encode("utf-8"))


def read_text(path: Path) -> str:
    """以 UTF-8 读取文本，与 ``atomic_write_text`` 字节对称。

    ``newline=""`` 关闭通用换行转换（``Path.read_text`` 到 3.13 才有该参数）：
    否则写回 ``"a\\r\\nb"`` 会读成 ``"a\\nb"``。
    """
    with open(path, encoding="utf-8", newline="") as file:
        return file.read()


def sha256_bytes(data: bytes) -> str:
    """返回数据的 sha256 摘要：64 位小写十六进制。"""
    return hashlib.sha256(data).hexdigest()


def is_link_like(path: Path) -> bool:
    """符号链接，或 Windows 重解析点（目录联接、挂载点等）。

    ``is_symlink`` 对目录联接（junction）返回 False，必须查 ``lstat`` 的
    ``st_file_attributes`` 重解析位；该属性与对应常量均为 Windows 专有，
    POSIX 上取不到（回退 0，等价于只认 ``is_symlink``）。

    ``ArtifactStore.resolve`` 也用它逐段拒绝写入路径上的链接，故为公开接口。
    """
    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


#: 旧私有名保留为别名：模块内引用已改用公开名，导出时保留以防既有外部引用断裂。
_is_link_like = is_link_like


def tree(root: Path, max_entries: int = 300) -> list[str]:
    """列出 ``root`` 下所有文件的 POSIX 相对路径，排序后截断到 ``max_entries`` 条。

    只含文件（目录本身与空目录不出现）；符号链接、Windows 目录联接与其他重解析点
    既不遍历也不列出——它们可能指向 root 之外，而本列表会直接进入 LLM 上下文。
    ``max_entries`` 截断无法从返回值察觉：调用方若要折叠 ``"... (N more)"``，
    请传 ``max_entries + 1`` 以判断是否还有更多。
    """
    entries: list[str] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = list(directory.iterdir())
        except OSError:
            continue  # 目录不存在或不可读：跳过，不中断整次列举
        for child in children:
            if is_link_like(child):
                continue
            if child.is_dir():
                pending.append(child)
            elif child.is_file():
                entries.append(child.relative_to(root).as_posix())
    return sorted(entries)[:max_entries]


def slugify_run_id(now: datetime) -> str:
    """生成 run_id：``YYYYMMDD-HHMMSS-<4 位小写 hex>``，如 ``20261005-120000-abcd``。"""
    return now.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
