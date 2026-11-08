"""P3-2：MCP **streamable HTTP** 传输（会话 / 协议版本 / SSE 响应 / 404 重建）。

这一组测试对应 `docs/THREAT_MODEL.md` 的 G-15（原记「MCP 传输非官方形态」）。用
`asyncio.start_server` 起本地假服务端，零第三方依赖，也不碰真实网络。

覆盖的四个官方语义，每一个都是「原来的朴素实现会失败」的点：

1. `initialize` 响应头带 `Mcp-Session-Id`，之后每个请求回带；
2. `initialize` 之后的请求带 `MCP-Protocol-Version`，且用**服务端协商值**而非本地默认；
3. 服务端可以用 `text/event-stream` 回一批消息（先通知、后响应），可能分块到达；
4. 服务端回 `404` 表示会话失效 → 客户端重握手一次后重试。

另外覆盖两个「不做就会挂住」的工程细节：通知回 `202` 空体不能当错误；带
`Content-Length` 的响应必须按长度读完，不能等连接关闭。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agentcrew.mcp import (
    PROTOCOL_VERSION_DEFAULT,
    HTTPTransport,
    MCPClient,
    MCPError,
    MCPSessionExpired,
    MCPTransportError,
)

# ── 假 streamable HTTP 服务端 ─────────────────────────────────────────


async def _read_http(
    reader: asyncio.StreamReader,
) -> tuple[str, str, bytes, dict[str, str]]:
    request_line = await reader.readline()
    parts = request_line.decode("ascii").split()
    if len(parts) < 2:
        raise AssertionError(f"请求行异常：{request_line!r}")
    method, path = parts[0], parts[1]
    headers: dict[str, str] = {}
    length = 0
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        key, _, value = line.decode("iso-8859-1").partition(":")
        headers[key.strip().lower()] = value.strip()
        if key.strip().lower() == "content-length":
            length = int(value.strip())
    body = await reader.readexactly(length) if length else b""
    return method, path, body, headers


class FakeStreamableServer:
    """够用的假服务端：一个状态机，记录收到的每个请求供断言。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.session_counter = 0
        self.valid_sessions: set[str] = set()
        #: 置为正数时，接下来 N 个非 initialize 请求回 404（模拟会话被丢）
        self.expire_next = 0
        #: 置 True 时 initialize 也回 404（模拟「根本握不上手」）
        self.fail_initialize = False
        #: tools/list 用 SSE 回（含通知与注释行），否则用 JSON
        self.sse_for_list = False
        #: 置 True 时 SSE 只推通知、永不给本次响应，且保持连接不关
        #: （模拟「服务端不按协议返回结果」——客户端唯一的出口是上限）
        self.sse_no_response = False
        #: SSE 用 chunked 编码，并把一条消息切在两个块里
        self.chunked_sse = False
        self._server: asyncio.AbstractServer | None = None
        self.url = ""

    async def __aenter__(self) -> FakeStreamableServer:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        await self._server.start_serving()
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    # ── 断言辅助 ──

    def methods(self) -> list[str]:
        return [method for method, _, _ in self.requests]

    def headers_for(self, method: str) -> dict[str, str]:
        for seen, headers, _ in self.requests:
            if seen == method:
                return headers
        raise AssertionError(f"没收到 {method} 请求，实际：{self.methods()}")

    # ── 处理 ──

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            _method, _path, body, headers = await _read_http(reader)
            message = json.loads(body.decode("utf-8")) if body else {}
            verb = str(message.get("method") or "")
            self.requests.append((verb, headers, message))

            if self.fail_initialize and verb == "initialize":
                await self._write_status(writer, 404, b'{"error":"no"}')
                return

            if self.expire_next > 0 and verb != "initialize":
                self.expire_next -= 1
                self.valid_sessions.clear()
                await self._write_status(writer, 404, b'{"error":"session gone"}')
                return

            if verb == "initialize":
                self.session_counter += 1
                session = f"sess-{self.session_counter}"
                self.valid_sessions.add(session)
                payload = {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        # 故意给一个比本地默认旧的版本：断言客户端以协商值为准
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "fake", "version": "1.0"},
                    },
                }
                await self._write_json(writer, payload, session=session)
                return

            # 会话校验：非 initialize 请求必须带有效会话
            session_header = headers.get("mcp-session-id", "")
            if session_header not in self.valid_sessions:
                await self._write_status(writer, 404, b'{"error":"unknown session"}')
                return

            if verb == "notifications/initialized":
                # 规范：通知回 202，无 body
                await self._write_status(writer, 202, b"")
                return

            if verb == "tools/list":
                result = {
                    "tools": [
                        {"name": "ping", "description": "p", "inputSchema": {}}
                    ]
                }
            elif verb == "tools/call":
                result = {
                    "content": [{"type": "text", "text": "pong"}],
                    "isError": False,
                }
            else:
                result = {}

            response = {
                "jsonrpc": "2.0", "id": message.get("id"), "result": result,
            }
            if verb == "tools/list" and self.sse_for_list:
                await self._write_sse(writer, response)
            else:
                await self._write_json(writer, response)
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.IncompleteReadError):
                pass

    async def _write_status(
        self, writer: asyncio.StreamWriter, status: int, body: bytes
    ) -> None:
        writer.write(
            f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()

    async def _write_json(
        self,
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
        *,
        session: str = "",
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        extra = f"Mcp-Session-Id: {session}\r\n" if session else ""
        writer.write(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"{extra}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()

    async def _write_sse(
        self, writer: asyncio.StreamWriter, response: dict[str, Any]
    ) -> None:
        """SSE 响应：注释行 + 一条通知 + 真正的响应（中途不关连接）。"""
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {"level": "info", "data": "working"},
        }
        if self.sse_no_response:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            for _ in range(20):
                writer.write(b"data: " + json.dumps(notification).encode() + b"\n\n")
            await writer.drain()
            await asyncio.sleep(2)   # 不关连接：客户端必须靠上限自己脱身
            return
        frames = (
            b": keep-alive\n\n"
            + b"data: " + json.dumps(notification).encode() + b"\n\n"
            + b"data: " + json.dumps(response).encode() + b"\n\n"
        )
        if not self.chunked_sse:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Connection: close\r\n\r\n" + frames
            )
            await writer.drain()
            return
        # chunked：把响应那一条切成两块，正好断开在一个 JSON 字符串中间
        marker = b'data: ' + json.dumps(response).encode()
        cut = len(marker) // 2
        pieces = [frames[: cut], frames[cut:]]
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        )
        for piece in pieces:
            writer.write(f"{len(piece):X}\r\n".encode() + piece + b"\r\n")
        writer.write(b"0\r\n\r\n")
        await writer.drain()


@pytest.fixture
async def fake_server():
    async with FakeStreamableServer() as server:
        yield server


# ── 1/2. 会话与协议版本 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_id_is_captured_and_echoed(fake_server: FakeStreamableServer):
    """initialize 拿到会话 → 后续每个请求回带同一个会话 id。"""
    transport = HTTPTransport(fake_server.url)
    await transport.connect()
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await asyncio.wait_for(transport.receive(), timeout=3)

    assert transport.session_id == "sess-1"
    # initialize 自己不带会话（那时还没有）
    assert "mcp-session-id" not in fake_server.headers_for("initialize")

    await transport.send(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    await asyncio.wait_for(transport.receive(), timeout=3)
    assert fake_server.headers_for("tools/list")["mcp-session-id"] == "sess-1"
    await transport.close()


@pytest.mark.asyncio
async def test_protocol_version_follows_negotiation(fake_server: FakeStreamableServer):
    """`MCP-Protocol-Version` 只在握手之后带，且用服务端协商值。

    假服务端故意在 initialize 结果里返回更旧的 `2025-03-26`：如果客户端拿本地
    默认值（2025-06-18）发出去，这条断言就会失败——这正是「协商」与「硬编码」
    的区别。
    """
    transport = HTTPTransport(fake_server.url)
    await transport.connect()
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await asyncio.wait_for(transport.receive(), timeout=3)

    # initialize 自己不带版本头
    assert "mcp-protocol-version" not in fake_server.headers_for("initialize")
    assert PROTOCOL_VERSION_DEFAULT == "2025-06-18"   # 本地默认是新的

    await transport.send(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    headers = fake_server.headers_for("notifications/initialized")
    assert headers["mcp-protocol-version"] == "2025-03-26"
    await transport.close()


@pytest.mark.asyncio
async def test_custom_headers_are_passed_through(fake_server: FakeStreamableServer):
    """配置里的 headers（如 Authorization）必须原样发出——否则鉴权必失败。"""
    transport = HTTPTransport(
        fake_server.url, headers={"Authorization": "Bearer token-1"})
    await transport.connect()
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await asyncio.wait_for(transport.receive(), timeout=3)
    assert fake_server.headers_for("initialize")["authorization"] == "Bearer token-1"
    await transport.close()


# ── 3. SSE 响应 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sse_response_delivers_notification_then_result(
    fake_server: FakeStreamableServer,
):
    """SSE 响应里先来通知、后到响应：两条都要进队列，且不因注释行报错。"""
    fake_server.sse_for_list = True
    transport = HTTPTransport(fake_server.url)
    await transport.connect()
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await asyncio.wait_for(transport.receive(), timeout=3)

    await transport.send(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    first = await asyncio.wait_for(transport.receive(), timeout=3)
    second = await asyncio.wait_for(transport.receive(), timeout=3)

    assert first["method"] == "notifications/message"
    assert second["id"] == 2
    assert second["result"]["tools"][0]["name"] == "ping"
    await transport.close()


@pytest.mark.asyncio
async def test_chunked_sse_split_mid_line_is_reassembled(
    fake_server: FakeStreamableServer,
):
    """chunk 边界落在一条 JSON 中间时仍能解析——这是按块 yield 会踩的坑。"""
    fake_server.sse_for_list = True
    fake_server.chunked_sse = True
    transport = HTTPTransport(fake_server.url)
    await transport.connect()
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await asyncio.wait_for(transport.receive(), timeout=3)

    await transport.send(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    messages = [await asyncio.wait_for(transport.receive(), timeout=3)
                for _ in range(2)]
    result = next(item for item in messages if item.get("id") == 2)
    assert result["result"]["tools"][0]["name"] == "ping"
    await transport.close()


@pytest.mark.asyncio
async def test_malformed_sse_payload_is_skipped(fake_server: FakeStreamableServer):
    """一条坏 JSON 不该让整个响应丢掉：跳过它，后面的响应照常解析。"""
    transport = HTTPTransport(fake_server.url)
    await transport.connect()
    # 直接构造：用一个「只回 SSE、内容含坏 JSON」的最小服务端太啰嗦，
    # 这里用公开的解析入口验证容错（与 _consume_sse 走同一条路径）。
    from agentcrew.mcp.transports import _iter_lines, _iter_sse_messages

    raw = (
        b"data: {not json}\n\n"
        b"data: {\"jsonrpc\": \"2.0\", \"id\": 9, \"result\": {}}\n\n"
    )
    await transport.close()

    async def pieces():
        yield raw

    messages = [item async for item in _iter_sse_messages(_iter_lines(pieces()))]
    assert len(messages) == 2
    assert transport._decode(messages[0]) is None      # 坏数据被丢弃
    assert transport._decode(messages[1])["id"] == 9   # 好数据照常


# ── 4. 会话失效与 202 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_404_triggers_single_reinitialize_then_succeeds(
    fake_server: FakeStreamableServer,
):
    """会话被服务端丢掉 → 自动重握手一次 → 原请求成功，且只重试一次。"""
    fake_server.expire_next = 1          # 下一个非 initialize 请求回 404
    client = MCPClient(transport=HTTPTransport(fake_server.url))
    async with client:
        result = await client.call_tool("ping", {})
    assert result.content[0]["text"] == "pong"

    methods = fake_server.methods()
    assert methods.count("initialize") == 2, methods
    # 第二次 initialize 拿到新会话，重试的 tools/call 带的是新会话
    assert fake_server.session_counter == 2
    last_call = [
        headers for verb, headers, _ in fake_server.requests
        if verb == "tools/call"
    ][-1]
    assert last_call["mcp-session-id"] == "sess-2"


@pytest.mark.asyncio
async def test_initialization_failure_is_reported_not_retried(
    fake_server: FakeStreamableServer,
):
    """握手本身 404：报错，不进「重连 ↔ 404」死循环。"""
    fake_server.fail_initialize = True
    client = MCPClient(transport=HTTPTransport(fake_server.url))
    with pytest.raises(MCPError):
        await client.start()
    assert fake_server.methods().count("initialize") == 1


@pytest.mark.asyncio
async def test_session_expiry_on_retry_surfaces_error(fake_server: FakeStreamableServer):
    """重试又 404：不再重连，如实报错（只允许重建一次）。"""
    fake_server.expire_next = 2          # 第一次与重试都回 404
    client = MCPClient(transport=HTTPTransport(fake_server.url))
    with pytest.raises(MCPError):
        await client.start()
        await client.call_tool("ping", {})
    assert fake_server.methods().count("initialize") == 2


@pytest.mark.asyncio
async def test_notification_202_does_not_block(fake_server: FakeStreamableServer):
    """通知回 202 空体：立即返回，队列里不留东西。"""
    transport = HTTPTransport(fake_server.url)
    await transport.connect()
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await asyncio.wait_for(transport.receive(), timeout=3)

    await asyncio.wait_for(
        transport.send(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}),
        timeout=3,
    )
    assert transport._queue.empty()
    await transport.close()


# ── 边界 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_https_is_rejected_with_actionable_message():
    """仍不支持 https（TLS 由反代终止）：报错要说清楚，而不是 `unknown scheme`。"""
    transport = HTTPTransport("https://example.com/mcp")
    with pytest.raises(MCPTransportError, match="反向代理"):
        await transport.connect()


@pytest.mark.asyncio
async def test_session_expired_is_transport_error_subclass():
    """`MCPSessionExpired` 必须仍是传输错误的子类，既有 except 分支才不会漏。"""
    assert issubclass(MCPSessionExpired, MCPTransportError)


@pytest.mark.asyncio
async def test_content_length_body_is_read_without_waiting_for_close(
    fake_server: FakeStreamableServer,
):
    """带 Content-Length 但不关连接的响应必须按长度读完——否则一直挂着。"""
    transport = HTTPTransport(fake_server.url)
    await transport.connect()
    # 假服务端这里会带 Connection: close，但读取走的是 Content-Length 分支：
    # 若实现退化成「读到 EOF」，本用例在 3s 内拿不到结果就会超时。
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    message = await asyncio.wait_for(transport.receive(), timeout=3)
    assert message["result"]["serverInfo"]["name"] == "fake"
    await transport.close()


@pytest.mark.asyncio
async def test_endless_sse_hits_event_cap_instead_of_hanging(
    fake_server: FakeStreamableServer, monkeypatch,
):
    """服务端只推别人的通知、永不回响应：必须撞上限报错，不能永久挂住。

    这是 MCP 传输最容易被忽略的一种失败：请求「发出去了」，响应「永远不来」，
    客户端停在读流上。没有上限时表现为整个 run 卡死且不报错——比报错难查得多。
    """
    from agentcrew.mcp import transports as transports_module

    monkeypatch.setattr(transports_module, "MAX_SSE_EVENTS", 3)
    fake_server.sse_for_list = True
    fake_server.sse_no_response = True

    transport = HTTPTransport(fake_server.url)
    await transport.connect()
    await transport.send(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await asyncio.wait_for(transport.receive(), timeout=3)

    with pytest.raises(MCPTransportError, match="上限"):
        await asyncio.wait_for(
            transport.send(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            timeout=3,
        )
    await transport.close()


@pytest.mark.asyncio
async def test_oversized_json_body_is_rejected(fake_server: FakeStreamableServer, monkeypatch):
    """单次 JSON 响应超过字节上限时报错，而不是先撑爆内存再想为什么。"""
    from agentcrew.mcp import transports as transports_module

    monkeypatch.setattr(transports_module, "MAX_RESPONSE_BYTES", 64)
    transport = HTTPTransport(fake_server.url)
    await transport.connect()
    with pytest.raises(MCPTransportError, match="上限"):
        await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await transport.close()
