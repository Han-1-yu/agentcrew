"""MCP (Model Context Protocol) 客户端（V2 第 6 项；P3-9 传输可插拔）。

实现 MCP 协议的最小可用子集：
- JSON-RPC 2.0 请求/响应（含通知）
- initialize 握手 + notifications/initialized
- tools/list 发现工具
- tools/call 执行工具

传输层（stdio / SSE / HTTP）由 :class:`~agentcrew.mcp.transports.MCPTransport`
抽象承载（P3-9）：客户端只面向收发接口编程，通过 ``from_config`` 按配置选择
传输。不依赖官方 MCP SDK，零额外依赖。协议参考：
https://modelcontextprotocol.io/specification/2025-03-26/
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agentcrew.mcp.tool import MCPTool, MCPToolResult
from agentcrew.mcp.transports import (
    HTTPTransport,
    MCPTransport,
    MCPTransportError,
    SSETransport,
    StdioTransport,
)

_logger = logging.getLogger(__name__)

#: MCP 协议版本
_PROTOCOL_VERSION = "2025-03-26"
#: JSON-RPC 响应超时（秒）
_RPC_TIMEOUT = 30


class MCPError(RuntimeError):
    """MCP 协议错误。"""


class MCPClient:
    """连接一个 MCP 服务器（默认 stdio），发现并调用其工具。

    用法（旧 API 保持兼容）::

        async with MCPClient(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]) as client:
            tools = await client.list_tools()
            result = await client.call_tool("read_file", {"path": "/tmp/x.txt"})

    P3-9：也可直接传入一个 :class:`~agentcrew.mcp.transports.MCPTransport`，或用
    :meth:`from_config` 按配置选择 stdio / SSE / HTTP。
    """

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        transport: MCPTransport | None = None,
    ) -> None:
        if transport is not None:
            self._transport = transport
        else:
            if command is None:
                raise MCPError("MCPClient 需要 command 或 transport")
            self._transport = StdioTransport(command, env=env)
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._initialized = False

    @classmethod
    def from_config(cls, server_config: dict[str, Any]) -> "MCPClient":
        """按 ``mcp_servers`` 配置构造客户端，``transport`` 字段选通道。

        - ``stdio``（默认）：``command`` + 可选 ``env``；
        - ``sse`` / ``http``：``url`` + 可选 ``headers``。
        """
        transport_type = str(server_config.get("transport", "stdio")).lower()
        if transport_type == "stdio":
            command = server_config.get("command")
            if not command:
                raise MCPError("stdio 传输需要 command 字段")
            return cls(list(command), env=server_config.get("env"))
        if transport_type == "sse":
            url = server_config.get("url")
            if not url:
                raise MCPError("sse 传输需要 url 字段")
            return cls(
                transport=SSETransport(str(url), headers=server_config.get("headers"))
            )
        if transport_type == "http":
            url = server_config.get("url")
            if not url:
                raise MCPError("http 传输需要 url 字段")
            return cls(
                transport=HTTPTransport(str(url), headers=server_config.get("headers"))
            )
        raise MCPError(f"未知 MCP 传输类型: {transport_type!r}")

    async def start(self) -> None:
        """建立传输连接并完成 initialize 握手。"""
        try:
            await self._transport.connect()
        except MCPTransportError as exc:
            raise MCPError(str(exc)) from exc
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._initialize()

    async def stop(self) -> None:
        """关闭传输连接。"""
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        await self._transport.close()
        self._initialized = False

    async def __aenter__(self) -> MCPClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def list_tools(self) -> list[MCPTool]:
        """发现服务器提供的全部工具。"""
        self._ensure_initialized()
        response = await self._request("tools/list", {})
        tools_data = response.get("result", {}).get("tools", [])
        return [MCPTool(**t) for t in tools_data]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> MCPToolResult:
        """调用一个工具，返回执行结果。"""
        self._ensure_initialized()
        response = await self._request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })
        result_data = response.get("result", {})
        return MCPToolResult(**result_data)

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            raise MCPError("MCP 客户端未初始化：请先调用 start() 或使用 async with")

    async def _initialize(self) -> None:
        response = await self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agentcrew", "version": "2.0.0"},
        })
        if "error" in response:
            raise MCPError(f"initialize 失败：{response['error']}")
        # 发送 initialized 通知（不需要响应）
        await self._notify("notifications/initialized", {})
        self._initialized = True

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """发送 JSON-RPC 请求并等待响应。"""
        self._request_id += 1
        req_id = self._request_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        try:
            await self._transport.send(message)
        except MCPTransportError as exc:
            self._pending.pop(req_id, None)
            raise MCPError(str(exc)) from exc
        try:
            return await asyncio.wait_for(future, timeout=_RPC_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise MCPError(f"RPC 超时（{_RPC_TIMEOUT}s）：{method}") from None

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """发送 JSON-RPC 通知（无 id，不需要响应）。"""
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        try:
            await self._transport.send(message)
        except MCPTransportError as exc:
            raise MCPError(str(exc)) from exc

    async def _read_loop(self) -> None:
        """持续从传输层读消息，分发 JSON-RPC 响应。"""
        while True:
            try:
                message = await self._transport.receive()
            except MCPTransportError:
                break
            if message is None:
                break
            # 响应（有 id）→ 唤醒对应 future
            msg_id = message.get("id")
            if msg_id is not None and msg_id in self._pending:
                future = self._pending.pop(msg_id)
                if not future.done():
                    future.set_result(message)
            # 通知（无 id）→ 目前忽略（服务器主动推送）
            elif msg_id is None:
                _logger.debug("MCP 通知：%s", message.get("method"))
