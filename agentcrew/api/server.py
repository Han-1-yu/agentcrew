"""HTTP API + SSE 实时事件流（V2 第 5 项）。

零依赖实现：用 ``asyncio.start_server`` + 手写 HTTP/1.1 解析，不引入
aiohttp/fastapi。实时推送用 SSE（Server-Sent Events）而非 WebSocket——
SSE 是标准 HTTP 长连接，单向推送足够覆盖「运行状态实时更新」场景，
且无需实现 WebSocket 握手/帧协议。事件源与 Ledger 同构（``on_event`` 回调）。

端点：
- ``GET /health`` → 健康检查
- ``GET /runs/{run_id}/status`` → 运行状态（任务统计）
- ``GET /runs/{run_id}/events`` → SSE 事件流
- ``POST /runs/{run_id}/events`` → 注入测试事件（仅测试/调试用）
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunState:
    """一个运行的内存状态快照（供 API 查询）。"""

    run_id: str
    goal: str = ""
    status: str = "pending"  # pending | running | completed | failed
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """发布/订阅事件总线：SSE 客户端订阅，Crew 的 on_event 发布。"""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    def publish(self, event: str, data: dict[str, Any]) -> None:
        """发布事件到所有订阅者（非阻塞）。"""
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # 慢客户端丢弃，不阻塞发布者

    def subscribe(self) -> asyncio.Queue[str]:
        """订阅事件流，返回一个队列；用完须调用 :meth:`unsubscribe`。"""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class ApiServer:
    """轻量 async HTTP 服务器 + SSE 事件流。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        event_bus: EventBus | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.bus = event_bus or EventBus()
        self.runs: dict[str, RunState] = {}
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> ApiServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def on_event(self, event: str, data: dict[str, Any]) -> None:
        """Crew 的 on_event 回调入口：推送到 SSE 客户端并更新内存状态。"""
        self.bus.publish(event, data)
        run_id = data.get("run_id", "")
        if run_id and run_id not in self.runs:
            self.runs[run_id] = RunState(run_id=run_id)
        if event == "task_transition":
            task_id = data.get("task_id", "")
            if task_id:
                self.runs[run_id].tasks[task_id] = {
                    "status": data.get("to_status", ""),
                    "role": data.get("role", ""),
                }
        elif event == "run_completed":
            self.runs[run_id].status = "completed"
        elif event == "run_failed":
            self.runs[run_id].status = "failed"

    # ── HTTP 处理 ────────────────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, path, _ = request_line.decode("utf-8", errors="replace").split(" ", 2)
            # 读取并丢弃请求头
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            if method == "GET":
                await self._handle_get(path, writer)
            elif method == "POST":
                # POST 只用于测试注入，body 可选（最多读 64KB）
                body = await reader.read(65536)
                await self._handle_post(path, body, writer)
            else:
                await self._send_json(writer, 405, {"error": "Method Not Allowed"})
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_get(self, path: str, writer: asyncio.StreamWriter) -> None:
        if path == "/" or path == "/index.html":
            from agentcrew.api.dashboard import DASHBOARD_HTML
            payload = DASHBOARD_HTML.encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"\r\n" + payload
            )
            await writer.drain()
            return
        if path == "/health":
            await self._send_json(writer, 200, {"status": "ok"})
            return
        if path.startswith("/runs/") and path.endswith("/events"):
            run_id = path.split("/")[2]
            await self._handle_sse(run_id, writer)
            return
        if path.startswith("/runs/") and path.endswith("/status"):
            run_id = path.split("/")[2]
            run = self.runs.get(run_id)
            if run is None:
                await self._send_json(writer, 404, {"error": "run not found"})
                return
            await self._send_json(writer, 200, {
                "run_id": run.run_id,
                "goal": run.goal,
                "status": run.status,
                "task_count": len(run.tasks),
                "tasks": run.tasks,
            })
            return
        await self._send_json(writer, 404, {"error": "Not Found"})

    async def _handle_post(
        self, path: str, body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        if path.startswith("/runs/") and path.endswith("/events"):
            run_id = path.split("/")[2]
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                await self._send_json(writer, 400, {"error": "invalid JSON"})
                return
            event = data.pop("_event", "custom")
            data.setdefault("run_id", run_id)
            self.on_event(event, data)
            await self._send_json(writer, 202, {"accepted": True})
            return
        await self._send_json(writer, 404, {"error": "Not Found"})

    async def _handle_sse(self, run_id: str, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        await writer.drain()
        queue = self.bus.subscribe()
        try:
            # 发送初始连接事件
            writer.write(f"event: connected\ndata: {json.dumps({'run_id': run_id})}\n\n".encode())
            await writer.drain()
            async for payload in self._iter_queue(queue):
                writer.write(payload.encode())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.bus.unsubscribe(queue)

    @staticmethod
    async def _iter_queue(queue: asyncio.Queue[str]) -> AsyncIterator[str]:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=30)
                yield item
            except asyncio.TimeoutError:
                # SSE 心跳注释行，防止代理超时
                yield ": ping\n\n"

    @staticmethod
    async def _send_json(
        writer: asyncio.StreamWriter, status: int, body: dict[str, Any],
    ) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        reason = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}.get(status, "OK")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode()
            + b"\r\n"
            + payload
        )
        await writer.drain()
