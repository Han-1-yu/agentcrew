"""R16：官方 Python SDK 的回归用例。

与 Webhook  suites 同口径：跑**真实的本地 HTTP 服务**，验证方法、路径、查询串、
请求头与 body，而不是在内存里假装有响应。错误映射、SSE 帧解析与示例可执行性
也都在这里覆盖。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from agentcrew_sdk import (
    AgentCrewClient,
    AgentCrewError,
    APIError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    RateLimitError,
    ResponseError,
    TransportError,
    WaitTimeoutError,
)


class _FakeAPI:
    """可编排的假 AgentCrew API：按 (method, path) 返回契约形状的响应。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[tuple[str, str], tuple[int, Any, str, dict]] = {}
        self.stream: bytes | None = None
        self.stream_status = 200

    def route(
        self, method: str, path: str, payload: Any, status: int = 200,
        *, headers: dict[str, str] | None = None,
        content_type: str = "application/json",
    ) -> None:
        self.routes[(method, path)] = (
            status, payload, content_type, headers or {})

    def last(self) -> dict[str, Any]:
        return self.requests[-1]


@pytest.fixture
def api():
    state = _FakeAPI("")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def do_PUT(self) -> None:  # noqa: N802
            self._handle()

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle()

        def _handle(self) -> None:
            parsed = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length else b""
            state.requests.append({
                "method": self.command,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": raw_body,
            })
            if (self.command == "GET" and parsed.path.endswith("/events")
                    and state.stream is not None):
                self.send_response(state.stream_status)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(state.stream)
                return
            entry = state.routes.get((self.command, parsed.path))
            if entry is None:
                status, payload = 404, {"error": "not found"}
                ctype, extra = "application/json", {}
            else:
                status, payload, ctype, extra = entry
                if callable(payload):
                    payload = payload(parse_qs(parsed.query), raw_body)
            body = (payload if isinstance(payload, bytes)
                    else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    if ctype == "application/json"
                    else str(payload).encode("utf-8"))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for key, value in extra.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """静音。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def client(api):
    return AgentCrewClient(api.url, api_key="secret-key")


# ── 基本请求 / 认证 ──────────────────────────────────────────────────────


def test_health_sends_auth_and_accept_headers(api, client):
    api.route("GET", "/health", {"status": "ok"})

    assert client.health() == {"status": "ok"}
    request = api.last()
    assert request["headers"]["authorization"] == "Bearer secret-key"
    assert request["headers"]["accept"] == "application/json"
    assert request["headers"]["user-agent"] == "agentcrew-sdk/1"


def test_without_api_key_omits_authorization(api):
    api.route("GET", "/health", {"status": "ok"})
    client = AgentCrewClient(api.url)

    client.health()

    assert "authorization" not in api.last()["headers"]


def test_base_url_trailing_slash_and_custom_headers(api):
    api.route("GET", "/health", {"status": "ok"})
    client = AgentCrewClient(
        api.url + "/", headers={"X-Tenant": "team-a"})

    assert client.base_url == api.url
    assert client.health() == {"status": "ok"}
    assert api.last()["headers"]["x-tenant"] == "team-a"


def test_create_run_sends_goal_and_options(api, client):
    api.route("POST", "/runs", {"run_id": "R1"}, status=202)

    result = client.create_run("做一个应用", options={"mock": True})

    assert result == {"run_id": "R1"}
    assert json.loads(api.last()["body"]) == {
        "goal": "做一个应用", "options": {"mock": True}}
    assert api.last()["headers"]["content-type"] == "application/json"


def test_create_run_without_options_omits_key(api, client):
    api.route("POST", "/runs", {"run_id": "R1"}, status=202)

    client.create_run("目标")

    assert json.loads(api.last()["body"]) == {"goal": "目标"}


def test_list_runs_returns_runs_array(api, client):
    api.route("GET", "/runs", {"runs": [{"run_id": "R1", "status": "completed"}]})

    assert client.list_runs()[0]["run_id"] == "R1"


# ── 只读端点 ────────────────────────────────────────────────────────────


def test_status_stats_and_ledger_params(api, client):
    api.route("GET", "/runs/R1/status", {"run_id": "R1", "status": "running"})
    api.route("GET", "/runs/R1/stats", {"total_events": 4})
    api.route("GET", "/runs/R1/ledger", {"events": [{"event": "llm_call"}], "total": 1})

    assert client.get_status("R1")["status"] == "running"
    assert client.get_stats("R1")["total_events"] == 4
    page = client.get_ledger("R1", event="llm_call", limit=7, offset=3)

    assert page["total"] == 1
    assert api.last()["query"] == {
        "event": ["llm_call"], "limit": ["7"], "offset": ["3"]}


def test_iter_ledger_follows_offset_until_total(api, client):
    def page(query, _body):
        offset = int(query.get("offset", ["0"])[0])
        events = ([{"event": "e1"}, {"event": "e2"}] if offset == 0
                  else [{"event": "e3"}])
        return {"events": events, "total": 3}

    api.route("GET", "/runs/R1/ledger", page)

    events = list(client.iter_ledger("R1", event="llm_call", page_size=2))

    assert [item["event"] for item in events] == ["e1", "e2", "e3"]
    offsets = [request["query"]["offset"][0]
               for request in api.requests]
    assert offsets == ["0", "2"]


def test_iter_ledger_stops_on_empty_page(api, client):
    api.route("GET", "/runs/R1/ledger", {"events": [], "total": 999})

    assert list(client.iter_ledger("R1")) == []
    assert len(api.requests) == 1


def test_run_id_is_percent_encoded(api, client):
    api.route("GET", "/runs/a%20b%2Fc/status", {"status": "completed"})

    client.get_status("a b/c")

    assert api.last()["path"] == "/runs/a%20b%2Fc/status"


# ── 写操作 ──────────────────────────────────────────────────────────────


def test_lifecycle_actions(api, client):
    api.route("POST", "/runs/R1/cancel", {"cancelled": True}, status=202)
    api.route("POST", "/runs/R1/resume", {"resumed": True}, status=202)
    api.route("DELETE", "/runs/R1", {"freed_bytes": 42})

    assert client.cancel_run("R1")["cancelled"] is True
    assert client.resume_run("R1")["resumed"] is True
    assert client.delete_run("R1")["freed_bytes"] == 42
    assert api.last()["query"] == {"confirm": ["1"]}


def test_model_get_and_set(api, client):
    api.route("GET", "/runs/R1/model", {"default": "openai:gpt-4o-mini"})
    api.route("PUT", "/runs/R1/model", {"default": "deepseek:deepseek-chat"})

    assert client.get_model("R1")["default"] == "openai:gpt-4o-mini"
    assert client.set_model("R1", "deepseek:deepseek-chat",
                            role="architect")["default"].startswith("deepseek")
    assert json.loads(api.last()["body"]) == {
        "alias": "deepseek:deepseek-chat", "role": "architect"}


def test_approvals_list_and_decision_shape(api, client):
    api.route("GET", "/runs/R1/approvals",
              {"run_id": "R1", "approvals": [{"id": "AP-1"}]})
    api.route("POST", "/runs/R1/approvals", {"id": "AP-1", "status": "decided"})

    assert client.get_approvals("R1")[0]["id"] == "AP-1"
    result = client.decide_approval(
        "R1", "AP-1", "rework", task_ids=["T-1"], decided_by="ops")

    assert result["status"] == "decided"
    assert json.loads(api.last()["body"]) == {
        "request_id": "AP-1",
        "decision": {"action": "rework", "task_ids": ["T-1"]},
        "decided_by": "ops",
    }


def test_inject_event_uses_underscore_event_key(api, client):
    api.route("POST", "/runs/R1/events", {"ok": True}, status=202)

    client.inject_event("R1", {"note": "ping"}, event="custom")

    assert json.loads(api.last()["body"]) == {
        "note": "ping", "_event": "custom"}


def test_request_is_an_escape_hatch(api, client):
    api.route("GET", "/providers", {"providers": ["openai"]})

    assert client.request("GET", "/providers")["providers"] == ["openai"]


# ── 错误映射 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [(400, APIError), (401, AuthenticationError), (404, NotFoundError),
     (409, ConflictError), (429, RateLimitError), (500, APIError)],
)
def test_error_status_mapping(api, client, status, expected):
    api.route("GET", "/health", {"error": "出问题了"}, status=status,
              headers={"Retry-After": "7"} if status == 429 else None)

    with pytest.raises(expected) as caught:
        client.health()

    assert caught.value.status_code == status
    assert "出问题了" in str(caught.value)
    if status == 429:
        assert caught.value.retry_after == 7.0


def test_non_json_error_body_falls_back_to_text(api, client):
    api.route("GET", "/health", b"upstream exploded", status=503,
              content_type="text/plain")

    with pytest.raises(APIError) as caught:
        client.health()

    assert "upstream exploded" in str(caught.value)


def test_non_json_success_body_raises_response_error(api, client):
    api.route("GET", "/health", b"<html>oops</html>",
              content_type="text/html")

    with pytest.raises(ResponseError):
        client.health()


def test_unreachable_host_raises_transport_error():
    client = AgentCrewClient("http://127.0.0.1:9", timeout=0.3)

    with pytest.raises(TransportError) as caught:
        client.health()

    assert "失败" in str(caught.value)


def test_closed_client_refuses_requests(api, client):
    api.route("GET", "/health", {"status": "ok"})
    client.close()

    with pytest.raises(TransportError):
        client.health()
    assert api.requests == []


def test_context_manager_closes(api):
    api.route("GET", "/health", {"status": "ok"})
    with AgentCrewClient(api.url) as client:
        assert client.health() == {"status": "ok"}

    with pytest.raises(TransportError):
        client.health()


# ── SSE ─────────────────────────────────────────────────────────────────


def test_iter_events_parses_frames_and_skips_heartbeat(api, client):
    api.stream = (
        b"event: connected\ndata: {\"run_id\": \"R1\"}\n\n"
        b": ping\n\n"
        b"event: task_transition\ndata: {\"task_id\": \"T-1\"}\n\n"
        b"event: custom\ndata: not-json\n\n"
        b"event: run_finished\ndata: {\"status\": \"completed\"}\n\n"
    )

    events = list(client.iter_events("R1"))

    assert [item.name for item in events] == [
        "connected", "task_transition", "custom", "run_finished"]
    assert events[0].data == {"run_id": "R1"}
    assert events[2].data == {"raw": "not-json"}
    assert events[3].data == {"status": "completed"}
    assert api.last()["headers"]["accept"] == "text/event-stream"
    assert api.last()["headers"]["authorization"] == "Bearer secret-key"


def test_iter_events_maps_http_error(api, client):
    api.stream = b'{"error": "Unauthorized"}'
    api.stream_status = 401

    with pytest.raises(AuthenticationError):
        list(client.iter_events("R1"))


# ── wait_for_run ────────────────────────────────────────────────────────


def test_wait_for_run_polls_until_terminal(api, client):
    calls = {"n": 0}

    def status(_query, _body):
        calls["n"] += 1
        return {"status": "running" if calls["n"] < 3 else "completed"}

    api.route("GET", "/runs/R1/status", status)

    final = client.wait_for_run("R1", poll_interval=0.0, timeout=5.0)

    assert final["status"] == "completed"
    assert calls["n"] == 3


def test_wait_for_run_timeout_keeps_last_status(api, client):
    api.route("GET", "/runs/R1/status", {"status": "awaiting_approval"})

    with pytest.raises(WaitTimeoutError) as caught:
        client.wait_for_run("R1", poll_interval=0.0, timeout=0.0)

    assert caught.value.payload["status"] == "awaiting_approval"


# ── 示例可执行性 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("script", ["quickstart.py", "stream_events.py"])
def test_examples_are_runnable(script):
    """示例装好 SDK 后至少能打印 --help；不依赖真实服务。"""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root / "sdk")}
    result = subprocess.run(
        [sys.executable, str(root / "sdk" / "examples" / script), "--help"],
        cwd=root, env=env, capture_output=True, text=True,
        timeout=60, errors="replace",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout


def test_sdk_exports_error_base_class():
    assert issubclass(AuthenticationError, AgentCrewError)
    assert issubclass(APIError, AgentCrewError)


# ── 端到端：真 serve + 真 run ───────────────────────────────────────────


def test_end_to_end_against_real_serve(tmp_path, monkeypatch):
    """假 API 测不出装配问题：起一次真 ``main.py serve``，用 SDK 跑完一个 mock run。

    覆盖 health / list_runs / create_run / wait_for_run / get_ledger / get_stats /
    get_approvals 全链路；SSE 是长连接，解析已在上面的真实本地服务里验证过。

    环境必须**白名单**：宿主机可能注入 ``PYTHONPATH`` shim（会让子进程 import ssl
    崩）与 HTTP 代理（会把 127.0.0.1 的请求拖到超时），两者都与 API 本身无关。
    """
    for key in list(os.environ):
        if "proxy" in key.lower():
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON") and "PROXY" not in key.upper()
    }
    env.update({
        "AGENTCREW_MOCK": "true",
        "AGENTCREW_OUTPUT_DIR": str(tmp_path / "output"),
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    log_path = tmp_path / "serve.log"
    with log_path.open("w", encoding="utf-8") as log:
        # R36 起认证 fail-closed：无密钥 serve 直接退 3（回环也一样）。本用例验的是
        # SDK 与真实 serve 的装配，不是认证，所以给一个显式 key 让 serve 起得来。
        process = subprocess.Popen(
            [sys.executable, "main.py", "serve", "--host", "127.0.0.1",
             "--port", str(port), "--api-key", "sdk-test-key"],
            cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            client = AgentCrewClient(
                f"http://127.0.0.1:{port}", api_key="sdk-test-key", timeout=30.0)
            ready = False
            for _ in range(50):
                try:
                    ready = client.health() == {"status": "ok"}
                    if ready:
                        break
                except AgentCrewError:
                    pass
                time.sleep(0.1)
            assert ready, f"serve 未就绪：{log_path.read_text(encoding='utf-8')}"

            assert client.list_runs() == []
            created = client.create_run(
                "SDK 端到端冒烟目标", options={"mock": True})
            run_id = created["run_id"]
            final = client.wait_for_run(
                run_id, poll_interval=0.2, timeout=90.0)
            assert final["status"] == "completed"

            ledger = client.get_ledger(run_id, limit=5)
            assert ledger["total"] > 0
            assert isinstance(client.get_stats(run_id), dict)
            assert client.get_approvals(run_id) == []
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
