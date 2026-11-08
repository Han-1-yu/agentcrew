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
import logging
from typing import Any

from agentcrew.mcp.tool import MCPTool, MCPToolResult
from agentcrew.mcp.transports import (
    PROTOCOL_VERSION_DEFAULT,
    HTTPTransport,
    MCPSessionExpired,
    MCPTransport,
    MCPTransportError,
    SSETransport,
    StdioTransport,
)

_logger = logging.getLogger(__name__)

#: MCP 协议版本（与传输层同一默认值；服务端可在 initialize 结果里协商成别的值）
_PROTOCOL_VERSION = PROTOCOL_VERSION_DEFAULT
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
        #: 握手期内已经用过一次「重建会话」的额度（防 404 ↔ 重连死循环）
        self._handshake_retry_used = False
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._initialized = False

    @classmethod
    def from_config(cls, server_config: dict[str, Any]) -> MCPClient:
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
        # 发送 initialized 通知（不需要响应）。
        #
        # 通知也可能撞上「会话刚建好就被服务端丢掉」（重启、网关换后端）。这种
        # 情况重握手一次就能恢复，**但只给一次额度**：否则服务端持续 404 时会变成
        # 「重连 ↔ 404」的死循环，表现为整个 run 卡在这里不报错。
        try:
            await self._notify("notifications/initialized", {})
        except MCPSessionExpired as exc:
            if self._handshake_retry_used:
                self._initialized = False
                raise MCPError(
                    f"{exc}；握手后会话仍未建立，请检查 MCP 服务端") from exc
            self._handshake_retry_used = True
            _logger.warning("MCP 握手后会话失效，重建一次：%s", exc)
            await self._initialize()
            return
        self._initialized = True
        self._handshake_retry_used = False

    async def _request(
        self, method: str, params: dict[str, Any], *, allow_reinit: bool = True,
    ) -> dict[str, Any]:
        """发送 JSON-RPC 请求并等待响应。

        ``allow_reinit``：streamable HTTP 的服务端可以随时丢掉会话（重启、过期），
        此时 POST 回 404。这种情况**重握手一次就能恢复**，比让整次 run 失败好得多；
        所以这里捕获 :class:`MCPSessionExpired` → 重新 ``initialize`` → 重发一次，
        且**只重试一次**（``allow_reinit=False``），避免 404 ↔ 重连的死循环。
        """
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
        except MCPSessionExpired as exc:
            self._pending.pop(req_id, None)
            if method == "initialize" or not allow_reinit:
                # 握手本身都建不起来：再试也是同一个结果，如实报错。
                self._initialized = False
                raise MCPError(f"{exc}；请重新 start() 以重建会话") from exc
            _logger.warning("MCP 会话失效，重新握手后重试 %s：%s", method, exc)
            self._initialized = False
            await self._initialize()
            return await self._request(method, params, allow_reinit=False)
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
        except MCPSessionExpired:
            # 原样上抛：调用方要能区分「会话没了，重建即可」与「真的发不出去」。
            raise
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
