"""P3-9：MCP 传输层（SSE / HTTP / 工厂 / 错误）测试。

用 :func:`asyncio.start_server` 起本地 http mock，零第三方依赖。覆盖：
- HTTPTransport 请求-响应；
- SSETransport 端点事件 + 数据事件 + POST 回发；
- :meth:`MCPClient.from_config` 按 transport 字段选通道；
- 异常统一抛 :class:`MCPTransportError`；
- stdio 向后兼容（旧 ``MCPClient([cmd])`` API 仍工作）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agentcrew.mcp import (
    HTTPTransport,
    MCPClient,
    MCPError,
    MCPTransport,
    MCPTransportError,
    SSETransport,
    StdioTransport,
)

# ── 本地 HTTP/SSE mock ────────────────────────────────────────────────


async def _read_http(reader: asyncio.StreamReader) -> tuple[str, str, bytes, dict[str, str]]:
    """读一个 HTTP 请求：请求行 + 头 + body；返回 (method, path, body, headers)。"""
    request_line = await reader.readline()
    parts = request_line.decode("ascii").split()
    method, path = parts[0], parts[1]
    headers: dict[str, str] = {}
    content_length = 0
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        key, _, value = line.decode("ascii").partition(":")
        headers[key.strip().lower()] = value.strip()
        if key.strip().lower() == "content-length":
            content_length = int(value.strip())
    body = await reader.readexactly(content_length) if content_length else b""
    return method, path, body, headers


def _write_json(writer: asyncio.StreamWriter, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    writer.write(
        f"HTTP/1.1 {status} OK\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n".encode("ascii") + body
    )


def _mcp_response(body: bytes) -> dict:
    """根据 JSON-RPC body 返回对应的 mock 响应。"""
    msg = json.loads(body.decode("utf-8")) if body else {}
    method = msg.get("method", "")
    req_id = msg.get("id")
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "serverInfo": {"name": "mock", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": "ping", "description": "p", "inputSchema": {}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "pong"}], "isError": False}
    else:
        result = {}
    if req_id is None:
        return {}  # 通知：无响应体
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


async def _handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    method, path, body, _ = await _read_http(reader)
    try:
        if method == "GET":
            # SSE 流：先推 endpoint 事件，再推一条数据事件，然后挂起保持长连接。
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            await writer.drain()
            writer.write(b"event: endpoint\ndata: /messages?session=abc\n\n")
            await writer.drain()
            writer.write(
                b'event: message\ndata: '
                + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode()
                + b"\n\n"
            )
            await writer.drain()
            await asyncio.sleep(1)  # 保持 SSE 长连接片刻，测试结束时取消
        else:
            payload = _mcp_response(body)
            if payload:
                _write_json(writer, 200, payload)
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


@pytest.fixture
async def mock_server():
    server = await asyncio.start_server(_handle_http, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        await server.start_serving()
        yield f"http://127.0.0.1:{port}"


# ── HTTPTransport ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_transport_request_response(mock_server):
    transport = HTTPTransport(f"{mock_server}/messages")
    await transport.connect()
    await transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    msg = await asyncio.wait_for(transport.receive(), timeout=3)
    assert msg["id"] == 1
    assert msg["result"]["serverInfo"]["name"] == "mock"
    await transport.close()


@pytest.mark.asyncio
async def test_http_transport_notification_no_response(mock_server):
    transport = HTTPTransport(f"{mock_server}/messages")
    await transport.connect()
    # 通知（无 id）：服务器回空体，队列保持空，不应阻塞。
    await transport.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    await transport.close()


# ── SSETransport ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sse_transport_endpoint_and_data(mock_server):
    transport = SSETransport(f"{mock_server}/sse")
    await transport.connect()
    # 第一条数据事件（endpoint 被内部消费并记录 post_url）。
    msg = await asyncio.wait_for(transport.receive(), timeout=3)
    assert msg["id"] == 1
    assert msg["result"] == {"ok": True}
    # send() 应 POST 到记录的 endpoint（/messages?session=abc），不报错。
    await transport.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    await transport.close()


# ── MCPClient.from_config ─────────────────────────────────────────────


def test_from_config_selects_stdio_by_default():
    client = MCPClient.from_config({"command": ["python", "-c", "pass"]})
    assert isinstance(client._transport, StdioTransport)


def test_from_config_selects_sse():
    client = MCPClient.from_config({"transport": "sse", "url": "http://x/sse"})
    assert isinstance(client._transport, SSETransport)


def test_from_config_selects_http():
    client = MCPClient.from_config({"transport": "http", "url": "http://x/messages"})
    assert isinstance(client._transport, HTTPTransport)


def test_from_config_stdio_requires_command():
    with pytest.raises(MCPError):
        MCPClient.from_config({"transport": "stdio"})


def test_from_config_sse_requires_url():
    with pytest.raises(MCPError):
        MCPClient.from_config({"transport": "sse"})


def test_from_config_http_requires_url():
    with pytest.raises(MCPError):
        MCPClient.from_config({"transport": "http"})


def test_from_config_unknown_transport():
    with pytest.raises(MCPError):
        MCPClient.from_config({"transport": "websocket", "url": "http://x"})


# ── 异常统一为 MCPTransportError ───────────────────────────────────────


@pytest.mark.asyncio
async def test_https_rejected_as_transport_error():
    with pytest.raises(MCPTransportError):
        await HTTPTransport("https://x/messages").connect()
    with pytest.raises(MCPTransportError):
        await SSETransport("https://x/sse").connect()


@pytest.mark.asyncio
async def test_connection_refused_is_transport_error():
    # 连一个肯定没人监听的端口。
    transport = HTTPTransport("http://127.0.0.1:1/messages")
    await transport.connect()  # connect 仅校验 URL，不联网
    with pytest.raises(MCPTransportError):
        await transport.send({"jsonrpc": "2.0", "id": 1, "method": "x", "params": {}})


# ── stdio 向后兼容（旧 API）────────────────────────────────────────────


def test_stdio_transport_is_mcp_transport():
    client = MCPClient(["python", "-c", "pass"])
    assert isinstance(client._transport, StdioTransport)
    assert isinstance(client._transport, MCPTransport)
