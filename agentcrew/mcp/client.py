"""MCP (Model Context Protocol) stdio 客户端（V2 第 6 项）。

实现 MCP 协议的最小可用子集：
- 通过 stdio 启动外部 MCP 服务器
- JSON-RPC 2.0 请求/响应（含通知）
- initialize 握手 + notifications/initialized
- tools/list 发现工具
- tools/call 执行工具

不依赖官方 MCP SDK，零额外依赖。协议参考：
https://modelcontextprotocol.io/specification/2025-03-26/
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from typing import Any

from agentcrew.mcp.tool import MCPTool, MCPToolResult

_logger = logging.getLogger(__name__)

#: MCP 协议版本
_PROTOCOL_VERSION = "2025-03-26"
#: JSON-RPC 响应超时（秒）
_RPC_TIMEOUT = 30


class MCPError(RuntimeError):
    """MCP 协议错误。"""


class MCPClient:
    """连接一个 stdio MCP 服务器，发现并调用其工具。

    用法::

        async with MCPClient(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]) as client:
            tools = await client.list_tools()
            result = await client.call_tool("read_file", {"path": "/tmp/x.txt"})
            print(result.text)
    """

    def __init__(self, command: list[str], *, env: dict[str, str] | None = None) -> None:
        self._command = command
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._initialized = False

    async def start(self) -> None:
        """启动服务器进程并完成 initialize 握手。"""
        # Windows：create_subprocess_exec 不解析 PATHEXT（.cmd/.bat），
        # 用 shutil.which 解析完整路径，否则 npx/npm 等命令找不到。
        cmd = list(self._command)
        if sys.platform == "win32":
            resolved = shutil.which(cmd[0])
            if resolved:
                cmd[0] = resolved
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._initialize()

    async def stop(self) -> None:
        """停止服务器进程。"""
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                self._proc.kill()
                await self._proc.wait()
            self._proc = None
        self._initialized = False

    async def __aenter__(self) -> "MCPClient":
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
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("MCP 进程未启动")
        self._request_id += 1
        req_id = self._request_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        message = json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }) + "\n"
        self._proc.stdin.write(message.encode("utf-8"))
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=_RPC_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise MCPError(f"RPC 超时（{_RPC_TIMEOUT}s）：{method}") from None

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """发送 JSON-RPC 通知（无 id，不需要响应）。"""
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("MCP 进程未启动")
        message = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }) + "\n"
        self._proc.stdin.write(message.encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        """持续读取服务器 stdout，分发 JSON-RPC 响应。"""
        if self._proc is None or self._proc.stdout is None:
            return
        while True:
            try:
                line = await self._proc.stdout.readline()
            except (asyncio.CancelledError, ConnectionError):
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                _logger.warning("MCP 服务器输出非 JSON：%s", line[:200])
                continue
            # 响应（有 id）→ 唤醒对应 future
            msg_id = message.get("id")
            if msg_id is not None and msg_id in self._pending:
                future = self._pending.pop(msg_id)
                if not future.done():
                    future.set_result(message)
            # 通知（无 id）→ 目前忽略（服务器主动推送）
            elif msg_id is None:
                _logger.debug("MCP 通知：%s", message.get("method"))
