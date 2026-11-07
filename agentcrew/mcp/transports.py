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


async def _http_request(
    url: str,
    method: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """最小 HTTP/1.1 请求（仅 ``http://``）；返回 ``(status, body_bytes)``。"""
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise MCPTransportError(f"仅支持 http:// 传输，收到 {parsed.scheme!r}")
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
        writer.write(request.encode("ascii"))
        if body is not None:
            writer.write(body)
        await writer.drain()
        status_line = await reader.readline()
        if not status_line:
            raise MCPTransportError("服务器关闭了连接（无响应）")
        try:
            status = int(status_line.decode("ascii").split()[1])
        except (ValueError, IndexError) as exc:
            raise MCPTransportError(
                f"无法解析状态行: {status_line!r}"
            ) from exc
        content_length: int | None = None
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            if line[:15].lower() == b"content-length:":
                try:
                    content_length = int(line.decode("ascii").split(":", 1)[1].strip())
                except ValueError:
                    content_length = None
        if content_length is not None:
            try:
                return status, await reader.readexactly(content_length)
            except asyncio.IncompleteReadError as exc:
                return status, exc.partial
        # 无 Content-Length：读到连接关闭。
        return status, await reader.read()
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
    """纯 HTTP 请求-响应传输：每次 send 即一次 POST，响应体进接收队列。

    简化实现：不维持长连接。请求（有 ``id``）的 POST 响应体作为
    :meth:`receive` 的返回值；通知（无 ``id``）的响应被忽略。
    """

    def __init__(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def connect(self) -> None:
        """校验 URL 形态；纯 HTTP 无需预连接。"""
        parsed = urlparse(self._url)
        if parsed.scheme != "http":
            raise MCPTransportError(f"仅支持 http://，收到 {parsed.scheme!r}")

    async def send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode("utf-8")
        status, resp = await _http_request(
            self._url, "POST", body=body, headers=self._headers
        )
        if status >= 400:
            raise MCPTransportError(f"POST 返回 HTTP {status}")
        if not resp:
            return
        try:
            data = json.loads(resp.decode("utf-8"))
        except json.JSONDecodeError:
            _logger.warning("HTTP 响应非 JSON: %s", resp[:200])
            return
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._queue.put(item)
        elif isinstance(data, dict):
            await self._queue.put(data)

    async def receive(self) -> dict[str, Any] | None:
        return await self._queue.get()

    async def close(self) -> None:
        await self._queue.put(None)
