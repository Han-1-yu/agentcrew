"""HTTP API + SSE 事件流的测试（V2 第 5 项）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentcrew.api.server import ApiServer, EventBus, RunState


class TestEventBus:
    def test_publish_subscribe(self):
        bus = EventBus()
        queue = bus.subscribe()
        assert bus.subscriber_count == 1
        bus.publish("test_event", {"key": "value"})
        item = queue.get_nowait()
        assert "event: test_event" in item
        assert '"key": "value"' in item

    def test_unsubscribe(self):
        bus = EventBus()
        queue = bus.subscribe()
        bus.unsubscribe(queue)
        assert bus.subscriber_count == 0

    def test_multiple_subscribers(self):
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.publish("evt", {"x": 1})
        assert q1.get_nowait() is not None
        assert q2.get_nowait() is not None


class TestRunState:
    def test_defaults(self):
        state = RunState(run_id="R-1")
        assert state.status == "pending"
        assert state.tasks == {}


class TestApiServer:
    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        server = ApiServer(port=0)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            text = response.decode()
            assert "200 OK" in text
            assert '"status": "ok"' in text
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_status_endpoint_404(self):
        server = ApiServer(port=0)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /runs/nonexistent/status HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            assert "404" in response.decode()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_status_endpoint_after_event(self):
        server = ApiServer(port=0)
        await server.start()
        try:
            server.on_event("task_transition", {
                "run_id": "R-1", "task_id": "T-1",
                "to_status": "running", "role": "backend_dev",
            })
            port = server._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /runs/R-1/status HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            text = response.decode()
            assert "200 OK" in text
            assert '"run_id": "R-1"' in text
            assert '"T-1"' in text
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_post_event_injection(self):
        server = ApiServer(port=0)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            body = json.dumps({"_event": "custom", "msg": "hello"}).encode()
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"POST /runs/R-1/events HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"\r\n" + body
            )
            await writer.drain()
            response = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            assert "202" in response.decode()
            assert "R-1" in server.runs
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_sse_receives_events(self):
        server = ApiServer(port=0)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /runs/R-1/events HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            # 读取 connected 事件
            first = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert b"text/event-stream" in first
            assert b"event: connected" in first
            # 发布一个事件
            server.on_event("task_transition", {"run_id": "R-1", "task_id": "T-1"})
            second = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert b"event: task_transition" in second
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with ApiServer(port=0) as server:
            assert server._server is not None
            port = server._server.sockets[0].getsockname()[1]
            assert port > 0
        assert server._server is None
