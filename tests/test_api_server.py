"""HTTP API + SSE 事件流的测试（V2 第 5 项）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentcrew.api.server import ApiServer, EventBus, RunState
from agentcrew.runtime.checkpoint import CheckpointStore


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

    @pytest.mark.asyncio
    async def test_dashboard_served_at_root(self):
        server = ApiServer(port=0)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read(65536)
            writer.close()
            await writer.wait_closed()
            text = response.decode()
            assert "200 OK" in text
            assert "text/html" in text
            assert "AgentCrew" in text
        finally:
            await server.stop()


# ── P1-5：创建 / 取消 / 恢复 / 列出 run ─────────────────────────────────────


class _FakeCrew:
    """测试用 crew 替身：duck-type ``run()`` / ``cancel()``，事件经 ``_on_event``。"""

    def __init__(self, run_id: str = "fake-run-1", hold: bool = False) -> None:
        self.run_id = run_id
        self.hold = hold
        self.cancelled = False
        self._on_event = None
        self.goal: str | None = None

    async def run(self, goal: str) -> None:
        self.goal = goal
        if self._on_event is not None:
            self._on_event("run_started", {"run_id": self.run_id, "goal": goal})
        await asyncio.sleep(0)  # 让 POST 响应先登记 active_crews
        if self.hold:
            for _ in range(300):
                if self.cancelled:
                    break
                await asyncio.sleep(0.01)
        if self._on_event is not None:
            status = "cancelled" if self.cancelled else "completed"
            self._on_event("run_finished", {
                "run_id": self.run_id, "status": status})

    def cancel(self) -> None:
        self.cancelled = True


async def _request(
    port: int, method: str, path: str, body: dict | None = None
) -> tuple[int, dict]:
    """发一个裸 HTTP 请求，返回 ``(状态码, 解析后的 JSON body)``。"""
    payload = json.dumps(body).encode() if body is not None else b""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if body is not None:
        request += f"Content-Length: {len(payload)}\r\n"
    request += "\r\n"
    writer.write(request.encode() + payload)
    await writer.drain()
    raw = await reader.read(65536)
    writer.close()
    await writer.wait_closed()
    text = raw.decode("utf-8", errors="replace")
    head, _, body_text = text.partition("\r\n\r\n")
    status = int(head.split("\r\n")[0].split(" ")[1])
    try:
        return status, json.loads(body_text)
    except json.JSONDecodeError:
        return status, {}


class TestRunLifecycle:
    @pytest.fixture
    async def server(self, tmp_path: Path):
        srv = ApiServer(port=0, output_dir=tmp_path)
        await srv.start()
        try:
            yield srv
        finally:
            for task in list(srv._run_tasks.values()):
                task.cancel()
            await srv.stop()

    @pytest.mark.asyncio
    async def test_post_runs_creates_run(self, server: ApiServer):
        crew = _FakeCrew(hold=True)
        server._crew_factory = lambda goal, options: crew
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(
            port, "POST", "/runs", {"goal": "做个 Todo 应用"})
        assert status == 202
        assert data["run_id"] == "fake-run-1"
        assert data["status"] == "running"
        assert crew.goal == "做个 Todo 应用"
        assert server.active_crews["fake-run-1"] is crew
        # 取消后后台任务收尾、注册表摘除
        crew.cancel()
        await asyncio.wait_for(server._run_tasks["fake-run-1"], timeout=3)
        assert "fake-run-1" not in server.active_crews

    @pytest.mark.asyncio
    async def test_post_runs_missing_goal(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(
            port, "POST", "/runs", {"options": {}})
        assert status == 400
        assert "goal" in data["error"]

    @pytest.mark.asyncio
    async def test_post_runs_invalid_json(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST /runs HTTP/1.1\r\nHost: localhost\r\n"
                     b"Content-Length: 5\r\n\r\n{bad")
        await writer.drain()
        raw = await reader.read(4096)
        writer.close()
        await writer.wait_closed()
        assert "400" in raw.decode()

    @pytest.mark.asyncio
    async def test_post_cancel_unknown_run(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(
            port, "POST", "/runs/nope/cancel")
        assert status == 404

    @pytest.mark.asyncio
    async def test_post_cancel_running_run(self, server: ApiServer):
        crew = _FakeCrew(hold=True)
        server._crew_factory = lambda goal, options: crew
        port = server._server.sockets[0].getsockname()[1]
        await _request(port, "POST", "/runs", {"goal": "g"})
        # task 引用必须在取消**前**捕获：crew 退出时会把自己从 _run_tasks 摘除，
        # 取消响应回来再查字典就变成与调度器的时序竞争（间歇 KeyError）。
        run_task = server._run_tasks["fake-run-1"]
        assert crew.cancelled is False
        status, data = await _request(
            port, "POST", "/runs/fake-run-1/cancel")
        assert status == 202
        assert data["cancelled"] is True
        assert crew.cancelled is True
        await asyncio.wait_for(run_task, timeout=3)

    @pytest.mark.asyncio
    async def test_post_resume_active_run_conflicts(self, server: ApiServer):
        crew = _FakeCrew(hold=True)
        server._crew_factory = lambda goal, options: crew
        port = server._server.sockets[0].getsockname()[1]
        await _request(port, "POST", "/runs", {"goal": "g"})
        status, _ = await _request(
            port, "POST", "/runs/fake-run-1/resume")
        assert status == 409

    @pytest.mark.asyncio
    async def test_post_resume_without_settings_503(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, _ = await _request(
            port, "POST", "/runs/whatever/resume")
        assert status == 503

    @pytest.mark.asyncio
    async def test_get_runs_lists_disk_runs(self, server: ApiServer, tmp_path: Path):
        CheckpointStore.create(
            tmp_path, project_goal="第一个", config_snapshot={})
        CheckpointStore.create(
            tmp_path, project_goal="第二个", config_snapshot={})
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs")
        assert status == 200
        runs = data["runs"]
        assert len(runs) == 2
        goals = {r["goal"] for r in runs}
        assert goals == {"第一个", "第二个"}
        # 按创建时间倒序返回（created_at 唯一可区分）
        created = [r["created_at"] for r in runs]
        assert created == sorted(created, reverse=True)

    @pytest.mark.asyncio
    async def test_get_runs_overlays_active(self, server: ApiServer):
        crew = _FakeCrew(hold=True)
        server._crew_factory = lambda goal, options: crew
        port = server._server.sockets[0].getsockname()[1]
        await _request(port, "POST", "/runs", {"goal": "g"})
        status, data = await _request(port, "GET", "/runs")
        assert status == 200
        mine = next(r for r in data["runs"] if r["run_id"] == "fake-run-1")
        assert mine["status"] == "running"
        assert mine["active"] is True
        crew.cancel()
        await asyncio.wait_for(server._run_tasks["fake-run-1"], timeout=3)


# ── A4/A5：Provider 目录、模型发现与测速端点 ────────────────────────────


class TestProvidersApi:
    @pytest.fixture
    async def server(self, tmp_path, monkeypatch):
        from agentcrew.config import Settings
        monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-supersecret-value-12345")
        settings = Settings.load()
        srv = ApiServer(port=0, output_dir=tmp_path, settings=settings)
        await srv.start()
        try:
            yield srv
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_get_providers_lists_directory(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/providers")
        assert status == 200
        providers = data["providers"]
        names = {p["name"] for p in providers}
        assert "openai" in names
        assert "openrouter" in names
        openai = next(p for p in providers if p["name"] == "openai")
        assert openai["kind"] == "builtin"
        assert openai["implementation"] == "openai_compat"
        assert openai["api_key_env"] == "OPENAI_API_KEY"
        assert "key_configured" in openai

    @pytest.mark.asyncio
    async def test_get_providers_does_not_leak_secret_value(self, server: ApiServer):
        """红线：GET /providers 响应绝不包含密钥值。"""
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/providers")
        assert status == 200
        body_str = json.dumps(data, ensure_ascii=False)
        assert "sk-supersecret-value-12345" not in body_str

    @pytest.mark.asyncio
    async def test_get_providers_custom_provider(self, server: ApiServer):
        from agentcrew.config import Settings
        settings = server._settings.model_copy(update={
            "custom_providers": {
                "myrelay": {
                    "implementation": "openai_compat",
                    "base_url": "http://localhost:9/v1",
                    "api_key_env": "MYRELAY_KEY",
                    "capabilities": {},
                }
            }
        })
        server._settings = settings
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/providers")
        assert status == 200
        relay = next(p for p in data["providers"] if p["name"] == "myrelay")
        assert relay["kind"] == "custom"
        assert relay["base_url"] == "http://localhost:9/v1"

    @pytest.mark.asyncio
    async def test_post_provider_test_no_factory_503(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "POST", "/providers/openai/test")
        assert status == 503

    @pytest.mark.asyncio
    async def test_get_provider_models_not_found(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/providers/nosuch/models")
        assert status == 404
