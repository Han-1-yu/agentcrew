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
import json
from collections.abc import Mapping
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
    "write_sse",
]

#: 状态码 → reason phrase。只登记本项目实际用到的码；未登记的回落 ``OK``
#: （与改造前逐字节一致：手写响应从不为未知状态码编造短语）。
REASONS: dict[int, str] = {
    200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
    404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
    413: "Payload Too Large", 429: "Too Many Requests",
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


def clamp_int(raw: str, *, default: int, minimum: int) -> int:
    """把查询参数里的整数读出来；非法 / 缺失用 ``default``，并压到 ``minimum`` 以上。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def client_ip(headers: Mapping[str, str], writer: asyncio.StreamWriter) -> str:
    """客户端 IP：优先 ``X-Forwarded-For`` 首地址（反代后真实客户端），否则
    取 socket peer 地址；都取不到时记 ``unknown``。"""
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    peername = writer.get_extra_info("peername")
    if peername:
        return str(peername[0])
    return "unknown"


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


def sse_frame(event: str, data: Any) -> str:
    """一条 SSE 帧（``event:`` + ``data:`` + 空行）。

    ``data`` 用 ``ensure_ascii=False`` 序列化：中文事件内容在浏览器 DevTools 里
    应当可读；``default=str`` 兜住 ``Path`` / ``datetime`` 这类不可直接序列化的值。
    """
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {data}\n\n"


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
