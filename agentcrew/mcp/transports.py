"""MCP 传输层（P3-9）：把 JSON-RPC 消息的收发抽象成可替换的通道。

:class:`MCPClient` 只面向 :class:`MCPTransport` 接口编程，因此换传输不需要改
协议层。三种内置实现：

- :class:`StdioTransport`：子进程 stdin/stdout（原来硬编码在客户端里的那套）；
- :class:`SSETransport`：GET 长连接读服务器推送、POST 发请求（简化：单次连接、
  不自动重连）；
- :class:`HTTPTransport`：纯 HTTP 请求-响应（每次 send 即一次 POST，响应进接收队列）。

零第三方依赖：HTTP/SSE 基于 :func:`asyncio.open_connection` 手写最小 HTTP/1.1
客户端，仅支持 ``http://``（localhost 测试与内网部署足够；``https`` 留给后续接
入成熟 HTTP 库）。传输层任何 IO / 协议帧错误统一抛 :class:`MCPTransportError`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)


class MCPTransportError(RuntimeError):
    """传输层错误（连接断开 / IO 失败 / 协议帧损坏）的统一类型。"""


class MCPTransport(ABC):
    """JSON-RPC 消息收发通道。"""

    @abstractmethod
    async def connect(self) -> None:
        """建立连接；失败抛 :class:`MCPTransportError`。"""

    @abstractmethod
    async def send(self, message: dict[str, Any]) -> None:
        """发送一条 JSON-RPC 消息；失败抛 :class:`MCPTransportError`。"""

    @abstractmethod
    async def receive(self) -> dict[str, Any] | None:
        """读取一条服务器消息；流结束 / 关闭返回 ``None``。"""

    @abstractmethod
    async def close(self) -> None:
        """释放连接（幂等）。"""


class StdioTransport(MCPTransport):
    """子进程 stdin/stdout 传输（原硬编码在 ``MCPClient`` 里的实现）。"""

    def __init__(
        self, command: list[str], *, env: dict[str, str] | None = None
    ) -> None:
        self._command = list(command)
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None

    async def connect(self) -> None:
        """启动服务器子进程。"""
        # Windows：create_subprocess_exec 不解析 PATHEXT（.cmd/.bat），
        # 用 shutil.which 解析完整路径，否则 npx/npm 等命令找不到。
        cmd = list(self._command)
        if sys.platform == "win32":
            resolved = shutil.which(cmd[0])
            if resolved:
                cmd[0] = resolved
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
        except OSError as exc:
            raise MCPTransportError(f"启动 MCP 子进程失败: {exc}") from exc

    async def send(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPTransportError("stdio 传输未连接")
        data = (json.dumps(message) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionError) as exc:
            raise MCPTransportError(f"写入 MCP 子进程失败: {exc}") from exc

    async def receive(self) -> dict[str, Any] | None:
        if self._proc is None or self._proc.stdout is None:
            return None
        try:
            line = await self._proc.stdout.readline()
        except (ConnectionError, asyncio.CancelledError):
            return None
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            _logger.warning("MCP 服务器输出非 JSON: %s", line[:200])
            return None

    async def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                self._proc.kill()
                await self._proc.wait()
            except (ProcessLookupError, asyncio.TimeoutError):
                pass
        finally:
            # 管道显式关闭：进程退出并不会关掉 asyncio 侧的传输句柄，
            # 留给 GC 会报 "unclosed transport" 警告（审计遗留小项）。
            # stdin 是 StreamWriter（公开 close）；stdout/stderr 是
            # StreamReader——asyncio 子进程包装没有暴露公开的关闭口，
            # 只能用它持有的底层 transport（``_transport``，与 asyncio
            # 内部实现同源，3.10–3.13 均稳定存在）。
            for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
                if stream is None:
                    continue
                if hasattr(stream, "close"):
                    stream.close()
                elif getattr(stream, "_transport", None) is not None:
                    stream._transport.close()  # type: ignore[attr-defined]
        self._proc = None


#: streamable HTTP 的默认协议版本。2025-06-18 版起，客户端在 ``initialize``
#: **之后**的每个请求都要带 ``MCP-Protocol-Version`` 头；服务端可在 initialize
#: 结果里协商成别的值，所以这是个可被覆盖的默认值而非常量。
PROTOCOL_VERSION_DEFAULT = "2025-06-18"

#: 单次响应的字节上限。MCP 服务器是**外部进程或外部服务**，不能假设它只回几 KB：
#: 一个畸形/恶意的实现可以回一个无限长的 SSE 流，把客户端卡在「读响应」上直到
#: 进程被 OOM 杀掉。上限不是性能参数，是「别把挂死当等待」的止损线。
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: 单次响应允许的最大 SSE 事件数。响应是靠「id 对上」来结束的，服务端只要一直
#: 推别人的通知就永远不会结束——字节上限之外再加一道事件数上限。
MAX_SSE_EVENTS = 1000


class MCPSessionExpired(MCPTransportError):
    """服务端已不认识当前会话（``404``）——调用方应重新 ``initialize``。

    单独一个类型是因为**处理方式完全不同**：普通传输错误要报给人，会话过期则是
    「握手重来一次就好」。用字符串匹配错误信息来判断这种事迟早会错。
    """


async def _open_http(
    url: str,
    method: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], asyncio.StreamReader, asyncio.StreamWriter]:
    """建一条 HTTP/1.1 连接并发出请求，返回 ``(状态码, 响应头, reader, writer)``。

    只负责「把请求发出去、把响应头读回来」——body 怎么读由调用方决定：JSON 响应
    一次读完，SSE 响应按行增量读（这正是 streamable HTTP 与「一次性 JSON」的
    唯一结构差异）。响应头键统一转小写，省得每个调用点各写一遍 ``.lower()``。

    ``writer`` 交给调用方关闭：SSE 场景必须**先读完流再关**，提前关会把「响应还
    没到」变成「连接被重置」。
    """
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise MCPTransportError(
            f"仅支持 http:// 传输（TLS 由反向代理终止），收到 {parsed.scheme!r}"
        )
    host = parsed.hostname
    if not host:
        raise MCPTransportError(f"非法 URL: {url!r}")
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    req_headers = {
        "Host": host if port == 80 else f"{host}:{port}",
        "Accept": "application/json, text/event-stream",
    }
    if headers:
        req_headers.update(headers)
    if body is not None:
        req_headers["Content-Length"] = str(len(body))
        req_headers.setdefault("Content-Type", "application/json")
    request = f"{method} {path} HTTP/1.1\r\n"
    request += "".join(f"{k}: {v}\r\n" for k, v in req_headers.items())
    request += "\r\n"
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        raise MCPTransportError(f"连接 {url} 失败: {exc}") from exc
    try:
        writer.write(request.encode("utf-8"))
        if body is not None:
            writer.write(body)
        await writer.drain()
        status_line = await reader.readline()
        if not status_line:
            raise MCPTransportError("服务器关闭了连接（无响应）")
        try:
            status = int(status_line.decode("ascii", "replace").split()[1])
        except (ValueError, IndexError) as exc:
            raise MCPTransportError(
                f"无法解析状态行: {status_line!r}") from exc
        headers_out: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode("iso-8859-1").partition(":")
            headers_out[key.strip().lower()] = value.strip()
        return status, headers_out, reader, writer
    except BaseException:
        writer.close()
        raise


async def _iter_payload(
    reader: asyncio.StreamReader, headers: dict[str, str]
) -> AsyncIterator[bytes]:
    """按 ``Transfer-Encoding`` 把响应体拆成「任意长度的字节片段」。

    分段而不是分行：chunked 的块边界与行边界毫无关系，先按行 yield 会在一行被
    切成两块时解析出半行。分行的事交给 :func:`_iter_lines` 用缓冲解决。
    """
    if "chunked" not in headers.get("transfer-encoding", "").lower():
        # 有 Content-Length 就只读这么多：读「到连接关闭」会在 keep-alive 的
        # 服务器上一直挂着（SSE 长连接同理，那种响应不带长度，走下面的分支）。
        length = headers.get("content-length")
        if length is not None:
            try:
                expected = int(length)
            except ValueError:
                expected = -1
            if expected >= 0:
                if expected == 0:
                    return
                try:
                    yield await reader.readexactly(expected)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        yield exc.partial
                return
        while True:
            piece = await reader.read(8192)
            if not piece:
                return
            yield piece
        return
    while True:
        size_line = await reader.readline()
        if not size_line:
            return
        try:
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return
        if size == 0:
            # trailer：读到空行为止（多数服务端直接一个 CRLF）
            while True:
                trailer = await reader.readline()
                if trailer in (b"\r\n", b"\n", b""):
                    break
            return
        remaining = size
        while remaining:
            chunk = await reader.readexactly(remaining)
            remaining -= len(chunk)
            yield chunk
        await reader.readline()      # 块尾 CRLF


async def _iter_lines(pieces: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """把字节片段拼回「完整行」（跨片段行会被缓冲到凑齐为止）。"""
    buffer = b""
    async for piece in pieces:
        buffer += piece
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                break
            yield buffer[: index + 1]
            buffer = buffer[index + 1:]
    if buffer:
        yield buffer


async def _iter_sse_messages(
    lines: AsyncIterator[bytes],
) -> AsyncIterator[str]:
    """把 SSE 行流转成「一条事件一个字符串」（只取 ``data:``，其余字段忽略）。

    空行是事件分隔符；多行 ``data:`` 按规范用换行拼接；``:`` 开头是注释（服务端
    常用它做保活），直接跳过。
    """
    data_lines: list[str] = []
    async for raw in lines:
        line = raw.rstrip(b"\r\n").decode("utf-8", "replace")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


async def _read_body(
    reader: asyncio.StreamReader, headers: dict[str, str]
) -> bytes:
    """读完整个响应体（JSON 响应用），超过 :data:`MAX_RESPONSE_BYTES` 直接报错。"""
    pieces: list[bytes] = []
    total = 0
    async for piece in _iter_payload(reader, headers):
        total += len(piece)
        if total > MAX_RESPONSE_BYTES:
            raise MCPTransportError(
                f"MCP 响应超过 {MAX_RESPONSE_BYTES} 字节上限，已中止读取")
        pieces.append(piece)
    return b"".join(pieces)


async def _http_request(
    url: str,
    method: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """一次性请求-响应（SSE 传输用它 POST 回发消息）。"""
    status, resp_headers, reader, writer = await _open_http(
        url, method, body=body, headers=headers)
    try:
        return status, await _read_body(reader, resp_headers)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.IncompleteReadError):
            pass


class SSETransport(MCPTransport):
    """SSE 传输：GET 长连接读服务器推送，POST 到 ``endpoint`` 地址发请求。

    简化实现（单次连接、不自动重连）：``connect()`` 打开对 ``url`` 的 GET
    长连接；服务器首条 ``event: endpoint`` 事件给出 POST 地址，之后每条
    ``data:`` 事件是一条 JSON-RPC 消息。
    """

    def __init__(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._post_url: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        parsed = urlparse(self._url)
        if parsed.scheme != "http":
            raise MCPTransportError(f"仅支持 http://，收到 {parsed.scheme!r}")
        host = parsed.hostname
        if not host:
            raise MCPTransportError(f"非法 SSE URL: {self._url!r}")
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            raise MCPTransportError(f"连接 SSE 端点失败: {exc}") from exc
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host if port == 80 else f'{host}:{port}'}\r\n"
            "Accept: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
        )
        request += "".join(f"{k}: {v}\r\n" for k, v in self._headers.items())
        request += "\r\n"
        writer.write(request.encode("ascii"))
        await writer.drain()
        status_line = await reader.readline()
        if not status_line:
            writer.close()
            raise MCPTransportError("SSE 服务器无响应")
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        self._reader = reader
        self._writer = writer

    async def send(self, message: dict[str, Any]) -> None:
        url = self._post_url or self._url
        body = json.dumps(message).encode("utf-8")
        status, _ = await _http_request(url, "POST", body=body, headers=self._headers)
        if status >= 400:
            raise MCPTransportError(f"POST {url} 返回 HTTP {status}")

    def _resolve_endpoint(self, target: str) -> str:
        """endpoint 事件可能给绝对 URL 或相对路径；相对路径拼到本服务来源。"""
        if "://" in target:
            return target
        base = urlparse(self._url)
        origin = f"{base.scheme}://{base.hostname or ''}"
        if base.port:
            origin += f":{base.port}"
        return origin + (target if target.startswith("/") else f"/{target}")

    async def receive(self) -> dict[str, Any] | None:
        if self._reader is None:
            return None
        event_name = "message"
        while True:
            line = await self._reader.readline()
            if not line:
                return None
            stripped = line.strip()
            if not stripped:
                event_name = "message"
                continue
            if stripped.startswith(b"event:"):
                event_name = stripped[6:].strip().decode("ascii", "ignore")
                continue
            if stripped.startswith(b"data:"):
                data = stripped[5:].strip().decode("utf-8", "ignore")
                if event_name == "endpoint":
                    self._post_url = self._resolve_endpoint(data)
                    continue
                try:
                    return json.loads(data)
                except json.JSONDecodeError:
                    _logger.warning("SSE 非 JSON 数据: %s", data[:200])
                    continue
            # 其余字段（id: / retry:）忽略

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (OSError, asyncio.IncompleteReadError):
                pass
        self._writer = None
        self._reader = None


class HTTPTransport(MCPTransport):
    """MCP **streamable HTTP** 传输（2025-03-26 起的官方形态）。

    与「POST 一次拿一个 JSON」的朴素实现相比，差在四件事——每一件都是 §G-15 原来
    记的那个缺口的组成部分：

    1. **会话**：``initialize`` 的响应头带 ``Mcp-Session-Id``，之后每个请求回带；
       丢了会话就丢上下文。
    2. **协议版本**：``initialize`` 之后的请求带 ``MCP-Protocol-Version``；版本取
       服务端在 initialize 结果里协商的值，不是本地硬编码。
    3. **SSE 响应**：服务端可以用 ``text/event-stream`` 回一批消息（先来通知、
       最后才是响应）。只读一次 JSON 会在这种服务端上直接超时。
    4. **会话失效**：服务端回 ``404`` 表示会话没了，必须重新握手——抛
       :class:`MCPSessionExpired`，由 :class:`~agentcrew.mcp.client.MCPClient`
       重建一次；用「字符串里有没有 404」来判断这种事迟早会错。

    仍只支持 ``http://``：TLS 由反向代理终止（与主服务的部署约定一致）。
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        protocol_version: str = PROTOCOL_VERSION_DEFAULT,
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._session_id = ""
        self._protocol_version = protocol_version
        self._handshake_started = False

    @property
    def session_id(self) -> str:
        """当前会话 id（空串 = 还没握手）。测试与排障用。"""
        return self._session_id

    async def connect(self) -> None:
        """校验 URL 形态；streamable HTTP 不需要预连接。"""
        parsed = urlparse(self._url)
        if parsed.scheme != "http":
            raise MCPTransportError(
                f"仅支持 http://（TLS 由反向代理终止），收到 {parsed.scheme!r}")

    def _request_headers(self) -> dict[str, str]:
        """本次 POST 的头：会话与协议版本只在握手之后带。"""
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._handshake_started:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    async def send(self, message: dict[str, Any]) -> None:
        """发一条 JSON-RPC 消息，把响应（或一组消息）放进接收队列。

        通知（无 ``id``）按规范回 ``202`` 无 body，此时直接返回——**不能**当作
        错误，也不能等队列（会把每轮 ``notifications/initialized`` 卡住一个超时）。
        """
        method = str(message.get("method") or "")
        body = json.dumps(message).encode("utf-8")
        status, resp_headers, reader, writer = await _open_http(
            self._url, "POST", body=body, headers=self._request_headers())
        try:
            if method == "initialize":
                # 握手**之后**的请求才带协议版本（规范如此）。置位放在请求真的
                # 发出去之后：紧跟着的 `notifications/initialized` 就会带上它，
                # 而 initialize 自己不带——少一个「服务端可能不认」的头。
                self._handshake_started = True
            self._absorb_session(resp_headers)
            if status == 404 and self._session_id:
                # 会话被服务端丢弃：清掉本地 id，让上层重新握手。
                self._session_id = ""
                raise MCPSessionExpired(
                    "MCP 会话已失效（HTTP 404），需要重新 initialize")
            if status >= 400:
                raise MCPTransportError(f"POST 返回 HTTP {status}")
            content_type = resp_headers.get("content-type", "").lower()
            if "text/event-stream" in content_type:
                await self._consume_sse(reader, resp_headers, message.get("id"))
            else:
                await self._consume_json(await _read_body(reader, resp_headers))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.IncompleteReadError):
                pass

    def _absorb_session(self, headers: dict[str, str]) -> None:
        """记下服务端发的会话 id / 协商后的协议版本（忽略空值）。"""
        session = headers.get("mcp-session-id")
        if session:
            self._session_id = session
        version = headers.get("mcp-protocol-version")
        if version:
            self._protocol_version = version

    async def _consume_sse(
        self,
        reader: asyncio.StreamReader,
        headers: dict[str, str],
        request_id: Any,
    ) -> None:
        """读 SSE 响应直到「本次请求的响应」到达（或流结束）。

        服务端可能先推若干通知再推响应；到齐即停，不为了「等服务端关流」多挂
        一个超时——规范允许服务端保持连接不关。
        """
        pieces = _iter_payload(reader, headers)
        lines = _iter_lines(pieces)
        events = 0
        total = 0
        async for payload in _iter_sse_messages(lines):
            events += 1
            total += len(payload)
            if events > MAX_SSE_EVENTS or total > MAX_RESPONSE_BYTES:
                # 服务端一直推别人的消息、始终不给本次响应时，这里是唯一的出口。
                raise MCPTransportError(
                    f"MCP 响应超过上限（{events} 事件 / {total} 字节），已中止；"
                    "可能是服务端未按协议返回本次请求的结果")
            data = self._decode(payload)
            if data is None:
                continue
            if isinstance(data, dict):
                await self._queue.put(data)
                self._absorb_protocol_version(data)
                if request_id is not None and data.get("id") == request_id:
                    return
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        await self._queue.put(item)
                        self._absorb_protocol_version(item)
                        if request_id is not None and item.get("id") == request_id:
                            return

    async def _consume_json(self, body: bytes) -> None:
        """一次性 JSON 响应：对象或数组（批量）都接。"""
        if not body:
            return
        data = self._decode(body.decode("utf-8", "replace"))
        if data is None:
            return
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                await self._queue.put(item)
                self._absorb_protocol_version(item)

    def _absorb_protocol_version(self, message: dict[str, Any]) -> None:
        """initialize 结果里的 ``protocolVersion`` 是**协商值**，以它为准。"""
        result = message.get("result")
        if isinstance(result, dict):
            version = result.get("protocolVersion")
            if isinstance(version, str) and version:
                self._protocol_version = version

    @staticmethod
    def _decode(raw: str) -> Any | None:
        """解析一条 JSON-RPC 消息；坏数据只记日志、不中断流。

        中断的代价太大：一条格式错误的通知会让整个工具的响应丢掉。
        """
        text = raw.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            _logger.warning("MCP 传输收到非 JSON 载荷: %.200s", text)
            return None

    async def receive(self) -> dict[str, Any] | None:
        return await self._queue.get()

    async def close(self) -> None:
        self._session_id = ""
        await self._queue.put(None)
