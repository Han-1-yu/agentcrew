"""请求路径规范化：**授权与分发必须认同一份规范路径**（安全止血项）。

为什么需要这个模块：改造前授权端拿**未解码**的原始 path 去匹配
:data:`~agentcrew.api.rbac._ROUTE_SCOPES`，分发端却先 ``unquote`` 再按段路由。
两侧看到的不是同一个字符串，于是 ``POST /%6beys`` 在授权端匹配不到任何规则
（``required_scope`` 返回 ``None`` = 登录即可），在分发端却解成 ``/keys`` 走
「新建密钥」handler —— 一个 ``viewer`` 角色因此能建出 ``admin`` 密钥。这不是
「正则写得不够严」，而是**同一条路径被解释了两次**。

收敛办法：只在这里解码/规整一次，中间件与分发都用 :func:`canonical_target`
的输出，解码不出第二种解释。

拒绝而不是「尽力修好」的输入（宁可 400，不可放行）：

- 编码过的分隔符 ``%2f`` / ``%5c`` / ``%00`` —— 解码后段边界会变，等于换了
  一条路由，这正是绕过的入口；
- 编码后的控制字符（``%0a`` / ``%7f`` 等）—— 请求行里不该出现；
- ``.`` / ``..`` 段 —— 目录穿越的语义标签，虽然 run_id 另有校验，但路径层面
  就不该有；
- 多层编码（解 3 次仍在变）—— 只有攻击者会叠这么多层。

多段连续斜杠 ``//`` 折叠为单斜杠：分发端本来就用 ``if s`` 过滤空段，折叠只是
让授权端与分发端对齐，不改变可达的资源。
"""

from __future__ import annotations

import re
from urllib.parse import unquote

__all__ = [
    "MAX_PATH_CHARS",
    "PathRejected",
    "canonical_path",
    "canonical_target",
]

#: 路径长度上限（字符）。合法路由最长的形如 ``/runs/<id>/tasks/<id>/agent``，
#: 远小于这个数；设上限是为了不把超长串喂给 re 匹配。
MAX_PATH_CHARS = 2048

#: 最多解码层数。正常请求 1 层就稳定；两层以上只可能是人为构造。
_MAX_UNQUOTE_PASSES = 3

#: 解码后出现的这些字节会让段边界/路径语义发生变化，或本身就是非法字符。
_FORBIDDEN_CHARS = frozenset({"/", "\\", "\x00"})

_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


class PathRejected(ValueError):
    """路径不可规范化（伪造分隔符 / 控制字符 / 穿越段 / 超长）。"""


def _reject_encoded_separators(raw: str) -> None:
    """挡掉「解码后变成分隔符或控制字符」的百分号转义。

    必须在解码**之前**逐字节判断：``%2f`` 解出来就是 ``/``，那时已无法与路径
    里本来就有的 ``/`` 区分，段边界早就变了。
    """
    for match in _PERCENT_ESCAPE.finditer(raw):
        byte = chr(int(match.group(1), 16))
        if byte in _FORBIDDEN_CHARS:
            raise PathRejected(
                f"路径包含编码后的分隔符/空字符（{match.group(0)}）")
        if ord(byte) < 0x20 or ord(byte) == 0x7F:
            raise PathRejected(
                f"路径包含编码后的控制字符（{match.group(0)}）")


def _decode_once(raw: str) -> str:
    try:
        return unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PathRejected(f"路径的百分号转义不是合法 UTF-8：{exc}") from None


def canonical_path(raw_path: str) -> str:
    """原始路径（可含查询串之外的一切）→ 规范路径；非法时抛 :class:`PathRejected`。

    返回值恒以 ``/`` 开头、无尾斜杠（根路径为 ``/``）、无重复斜杠、无 ``.`` /
    ``..`` 段、无未解码的百分号转义。授权正则与路由分发都只看这个值。
    """
    if not raw_path:
        raise PathRejected("路径为空")
    if len(raw_path) > MAX_PATH_CHARS:
        raise PathRejected(f"路径过长（上限 {MAX_PATH_CHARS} 字符）")
    if not raw_path.startswith("/"):
        raise PathRejected("路径必须以 / 开头（不支持绝对 URI 形式）")

    decoded = raw_path
    for _ in range(_MAX_UNQUOTE_PASSES):
        _reject_encoded_separators(decoded)
        unquoted = _decode_once(decoded)
        if unquoted == decoded:
            break
        decoded = unquoted
    else:
        # for 正常结束（没 break）= 每一轮都还在变 → 编码层数可疑。
        _reject_encoded_separators(decoded)
        if _decode_once(decoded) != decoded:
            raise PathRejected("路径编码层数过多")

    for char in decoded:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise PathRejected("路径包含控制字符")
        if char == "\\":
            raise PathRejected("路径包含反斜杠")

    segments: list[str] = []
    for segment in decoded.split("/"):
        if not segment:
            continue                      # 折叠重复斜杠与尾斜杠
        if segment in (".", ".."):
            raise PathRejected(f"路径包含穿越段（{segment!r}）")
        segments.append(segment)
    if not segments:
        return "/"
    return "/" + "/".join(segments)


def canonical_target(target: str) -> tuple[str, str]:
    """请求行里的 target → ``(规范路径, 原始查询串)``。

    查询串原样返回（由调用方 ``parse_qs`` 自行解码）：路径与查询串的解码规则
    不同（``+`` 在查询串里是空格），混在一起处理只会制造第二种解释。
    """
    path, _, query = target.partition("?")
    return canonical_path(path), query
