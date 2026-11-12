"""HTTP/1.1 传输层原语：请求解析、响应写出、SSE 帧格式、客户端 IP。

与 ``api/server.py`` 的分工是**传输** vs **应用**：这里只认字节（读到哪一行、
写什么头、怎么算 Content-Length），不认业务概念（run、任务、模型）；``ApiServer``
只认业务，不碰字节。零依赖实现（``asyncio.start_server`` + 手写 HTTP/1.1）保持
不变，因此这些原语必须自己处理三件容易出错的事：

1. **Content-Length 而不是「读到 EOF」**：无 body 的 POST 若继续 ``read()``，
   客户端还在等响应、服务端还在等字节，双方互等到超时；
2. **请求头小写键**：认证要查 ``Authorization`` / ``X-API-Key``，限流要查
   ``X-Forwarded-For``，大小写敏感性留给 HTTP 语义而不是调用方；
3. **SSE 帧格式**：``event:`` + ``data:`` + 空行，少一个空行浏览器就不派发事件。

所有函数都是模块级、无状态，可脱离服务器单测。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "REASONS",
    "Request",
    "clamp_int",
    "client_ip",
    "read_request",
    "send_html",
    "send_json",
    "sse_frame",
    "sse_headers",
    "valid_event_name",
    "write_sse",
]

#: 状态码 → reason phrase。只登记本项目实际用到的码；未登记的回落 ``OK``
#: （与改造前逐字节一致：手写响应从不为未知状态码编造短语）。
REASONS: dict[int, str] = {
    200: "OK", 201: "Created", 202: "Accepted", 400: "Bad Request",
    401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed",
    409: "Conflict", 413: "Payload Too Large", 429: "Too Many Requests",
    500: "Internal Server Error", 503: "Service Unavailable",
    504: "Gateway Timeout",
}

#: 请求体上限（内部控制面也需要一个硬边界）：声明超过该长度的 body 直接拒绝，
#: 避免「声明极大 Content-Length 却不发包」把连接挂在读等待上。
MAX_BODY_BYTES = 4 * 1024 * 1024


@dataclass(slots=True)
class Request:
    """一条已解析的请求行 + 请求头（body 由调用方按 ``content_length`` 读）。"""

    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    content_length: int = 0


async def read_request(reader: asyncio.StreamReader) -> Request | None:
    """读请求行与全部请求头；连接已关闭（EOF）时返回 ``None``。

    请求行格式非法时的行为与改造前一致：``split(" ", 2)`` 解包失败直接抛
    ``ValueError``，由 asyncio 记录并关闭该连接 —— 不在传输层编造 400 响应，
    那种「宽容」会让协议级错误永远看不见。
    """
    request_line = await reader.readline()
    if not request_line:
        return None
    method, path, _ = request_line.decode("utf-8", errors="replace").split(" ", 2)
    request = Request(method=method, path=path)
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, sep, raw_value = line.partition(b":")
        if not sep:
            continue
        key = name.strip().decode("ascii", errors="replace").lower()
        value = raw_value.strip().decode("latin-1", errors="replace")
        request.headers[key] = value
        if key == "content-length":
            try:
                request.content_length = max(0, int(value))
            except ValueError:
                request.content_length = 0
    return request


def clamp_int(
    raw: str, *, default: int, minimum: int, maximum: int | None = None,
) -> int:
    """把查询参数里的整数读出来；非法 / 缺失用 ``default``，压到 ``[minimum, maximum]``。

    ``maximum`` 为 ``None`` 时不设上界（B-8：分页 ``limit`` 必须设上界，否则
    ``limit=999999999`` 会把整份 ledger 物化进内存）。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


#: 已解析的可信代理网段类型（``ipaddress.ip_network`` 的产物）。
TrustedNetworks = Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]


def _in_trusted_networks(addr_text: str, trusted: TrustedNetworks) -> bool:
    """地址是否落在任一可信网段；解析失败按不可信处理（fail-closed）。"""
    if not trusted or not addr_text:
        return False
    try:
        addr = ipaddress.ip_address(addr_text)
    except ValueError:
        return False
    return any(addr in net for net in trusted)


def _is_ip(addr_text: str) -> bool:
    """文本是否是合法 IP 地址（XFF 里的垃圾跳不能当 client_ip 用）。"""
    try:
        ipaddress.ip_address(addr_text)
    except ValueError:
        return False
    return True


def client_ip(
    headers: Mapping[str, str],
    writer: asyncio.StreamWriter,
    *,
    trusted_proxies: TrustedNetworks = (),
) -> str:
    """客户端 IP：默认**只用 socket peer**；仅当 peer 是可信代理才解析 XFF。

    P0-4：改造前无条件优先取 ``X-Forwarded-For`` 首地址——默认部署是直连
    （无反代），客户端每请求换一个 XFF 就换一个限流分桶，限流恒不触发，
    ``security_events`` 里的 client_ip 也被污染。

    现在：peer 落在 ``trusted_proxies`` 网段内时，从 XFF **从右往左**取第一个
    不可信地址（「最右侧不可信跳」语义，伪造的中间跳不能冒充真实客户端）；
    全部可信则取最左（原始客户端）。都取不到时记 ``unknown``。
    """
    peername = writer.get_extra_info("peername")
    peer = str(peername[0]) if peername else ""
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded and _in_trusted_networks(peer, trusted_proxies):
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        for hop in reversed(hops):
            # 垃圾跳（非 IP 文本）不可用，继续向左；可信跳跳过。
            if _is_ip(hop) and not _in_trusted_networks(hop, trusted_proxies):
                return hop
        # 全部可信 → 取最左（原始客户端）；它也得是合法 IP 才可用。
        if hops and _is_ip(hops[0]):
            return hops[0]
    return peer or "unknown"


async def send_json(
    writer: asyncio.StreamWriter,
    status: int,
    body: dict[str, Any],
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> None:
    """写一条 JSON 响应（``Content-Type: application/json`` + 精确 Content-Length）。"""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    await _write_response(
        writer,
        status,
        b"Content-Type: application/json\r\n",
        payload,
        extra_headers=extra_headers,
    )


#: 看板 HTML 的安全响应头（P1-2）。
#:
#: 看板是单文件内联脚本 + 内联事件属性，因此 ``script-src`` 必须放 ``'unsafe-inline'``
#: —— 光靠 CSP 挡不住注入，真正的修法是渲染时一律 ``textContent``（见 dashboard）。
#: 这里加的是**纵深防御**：即使某处漏了一处拼接，``connect-src 'self'`` 也把外发
#: 通道堵死（XSS 拿不到把 key/内容送出站的通路），``frame-ancestors 'none'`` 阻
#: 止被嵌套点击劫持，``nosniff`` 阻止内容嗅探。
_HTML_SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


async def send_html(writer: asyncio.StreamWriter, html: str) -> None:
    """写一条 200 HTML 响应（看板页面用），带 CSP / nosniff 等安全头。"""
    await _write_response(
        writer,
        200,
        b"Content-Type: text/html; charset=utf-8\r\n",
        html.encode("utf-8"),
        extra_headers=_HTML_SECURITY_HEADERS,
    )


async def send_text(
    writer: asyncio.StreamWriter,
    text: str,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
) -> None:
    """写一条纯文本响应（D-7：``/metrics`` 的 Prometheus 文本格式）。"""
    await _write_response(
        writer,
        status,
        f"Content-Type: {content_type}\r\n".encode(),
        text.encode("utf-8"),
        extra_headers={"Cache-Control": "no-store"},
    )


async def _write_response(
    writer: asyncio.StreamWriter,
    status: int,
    content_type: bytes,
    payload: bytes,
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> None:
    """状态行 + Content-Type/Length + 附加头 + 空行 + body。"""
    reason = REASONS.get(status, "OK")
    header_lines = (
        f"HTTP/1.1 {status} {reason}\r\n".encode()
        + content_type
        + f"Content-Length: {len(payload)}\r\n".encode()
    )
    for name, value in (extra_headers or {}).items():
        header_lines += f"{name}: {value}\r\n".encode()
    writer.write(header_lines + b"\r\n" + payload)
    await writer.drain()


#: SSE 事件名白名单（P0-3）：``event`` 的值可能来自 ``POST /runs/{id}/events``
#: 的 body；不约束的话，携带换行的名字可以闭合当前帧、向订阅者伪造任意
#: ``event:`` / ``data:`` 行（JSON 序列化的 data 会转义换行，裸字符串 event 不会）。
_EVENT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def valid_event_name(event: Any) -> bool:
    """事件名是否合法：1–64 位字母 / 数字 / ``.`` / ``_`` / ``-``。"""
    return (isinstance(event, str)
            and _EVENT_NAME_RE.fullmatch(event) is not None)


def sse_frame(event: str, data: Any) -> str:
    """一条 SSE 帧（``event:`` + ``data:`` + 空行）。

    ``data`` 用 ``ensure_ascii=False`` 序列化：中文事件内容在浏览器 DevTools 里
    应当可读；``default=str`` 兜住 ``Path`` / ``datetime`` 这类不可直接序列化的值。

    注入防御（P0-3，纵深第二层；第一层在 ``POST /runs/{id}/events`` 入口）：

    * ``event`` 不在白名单内一律降级为 ``custom``——本函数是所有 SSE 出口的
      唯一拼帧点，保证任何调用路径都不可能把换行写进 ``event:`` 行；
    * 字符串型 ``data`` 的换行按 SSE 规范拆成多条 ``data:`` 行。JSON 序列化
      天然没有裸换行，此分支只影响直接传 ``str`` 的调用方（行为对无换行输入
      与旧实现逐字节一致）。
    """
    if not valid_event_name(event):
        event = "custom"
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, default=str)
    normalized = data.replace("\r\n", "\n").replace("\r", "\n")
    data_lines = "".join(f"data: {line}\n" for line in normalized.split("\n"))
    return f"event: {event}\n{data_lines}\n"


async def sse_headers(writer: asyncio.StreamWriter) -> None:
    """写 SSE 响应头（长连接，无 Content-Length）。"""
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: keep-alive\r\n"
        b"\r\n"
    )
    await writer.drain()


async def write_sse(writer: asyncio.StreamWriter, frame: str) -> None:
    """写一帧并立刻 flush（SSE 的语义要求事件尽快到达客户端）。"""
    writer.write(frame.encode())
    await writer.drain()
