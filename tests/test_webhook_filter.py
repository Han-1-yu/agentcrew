"""C4：Webhook 事件级等值过滤（默认空表达式 = 行为逐字不变）。

用真实本地 HTTP 服务钉住「过滤真的拦住了请求」，其余用纯函数与配置对象覆盖：
过滤表达式来自服务端配置，不能引入 eval / 正则 / 比较运算符。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentcrew.config import _ENV_TO_FIELD, _YAML_SCALAR_KEYS, Settings
from agentcrew.runtime.webhook import (
    WebhookConfig,
    WebhookDispatcher,
    WebhookTarget,
    match_filter,
)


@pytest.fixture
def hook_server():
    state: dict[str, object] = {"received": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            state["received"].append(json.loads(self.rfile.read(length) or b"{}"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            """静音。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/hook", state
    finally:
        server.shutdown()
        server.server_close()


# ── 纯函数 ────────────────────────────────────────────────────────────────


def test_empty_expression_never_filters():
    assert match_filter("", "anything", {"a": 1}) is True
    assert match_filter("   ", "anything", None) is True


def test_single_and_conjunctive_clauses():
    payload = {"status": "failed", "run_id": "R-1"}
    assert match_filter("status=failed", "run_finished", payload) is True
    assert match_filter("status=failed,run_id=R-1", "run_finished", payload) is True
    assert match_filter("status=failed,run_id=R-2", "run_finished", payload) is False


def test_event_key_and_dotted_path():
    assert match_filter("event=run_finished", "run_finished", {}) is True
    assert match_filter("event=run_started", "run_finished", {}) is False
    nested = {"context": {"task_id": "T-002"}}
    assert match_filter("context.task_id=T-002", "gate_result", nested) is True
    assert match_filter("context.task_id=T-003", "gate_result", nested) is False
    assert match_filter("context.missing=x", "gate_result", nested) is False


def test_star_only_requires_key_presence():
    assert match_filter("task_id=*", "task_done", {"task_id": "T-1"}) is True
    assert match_filter("task_id=*", "task_done", {}) is False


def test_string_coercion_is_deliberate():
    """int / bool 用 str() 比较，不引入类型运算。"""
    assert match_filter("round=2", "gate_result", {"round": 2}) is True
    assert match_filter("ok=True", "gate_result", {"ok": True}) is True


@pytest.mark.parametrize("expr", [
    "status", "=failed", "status=", "status=failed,", " ,status=failed",
])
def test_bad_syntax_fails_closed(expr):
    """写错的表达式保守判不匹配：宁可少发，不能全量外推。"""
    assert match_filter(expr, "run_finished", {"status": "failed"}) is False


def test_filter_does_not_mutate_payload():
    payload = {"status": "failed"}
    match_filter("event=run_finished,status=failed", "run_finished", payload)
    assert payload == {"status": "failed"}      # body 字节断言的前提


# ── 配置对象 ──────────────────────────────────────────────────────────────


def test_target_filter_is_and_with_events_and_only_failures():
    target = WebhookTarget(
        events=("run_finished",), filter="status=failed")
    assert target.matches("run_finished", {"status": "failed"}) is True
    assert target.matches("run_finished", {"status": "completed"}) is False
    assert target.matches("run_started", {"status": "failed"}) is False
    strict = WebhookTarget(
        events=("*",), only_failures=True, filter="status=failed")
    assert strict.matches("run_finished", {"status": "failed"}) is True
    assert strict.matches("run_finished", {"status": "completed"}) is False


def test_default_target_inherits_config_filter():
    config = WebhookConfig(url="http://127.0.0.1:9/hook",
                           filter="status=failed")
    target = config.resolved()[0]
    assert target.filter == "status=failed"
    assert config.matches("run_finished", {"status": "failed"}) is True
    assert config.matches("run_finished", {"status": "completed"}) is False


def test_multi_target_filters_are_independent():
    config = WebhookConfig(targets=(
        WebhookTarget(name="a", url="http://127.0.0.1:9/a",
                      filter="status=failed"),
        WebhookTarget(name="b", url="http://127.0.0.1:9/b"),
    ))
    assert [t.name for t in config.resolved()
            if t.matches("run_finished", {"status": "failed"})] == ["a", "b"]
    assert [t.name for t in config.resolved()
            if t.matches("run_finished", {"status": "completed"})] == ["b"]


def test_config_channels_are_registered():
    assert _ENV_TO_FIELD["AGENTCREW_WEBHOOK_FILTER"] == "webhook_filter"
    assert "webhook_filter" in _YAML_SCALAR_KEYS
    assert Settings().webhook_filter == ""
    assert Settings(webhook_filter="status=failed").webhook_filter == "status=failed"


def test_env_channel_loads_filter(monkeypatch):
    monkeypatch.setenv("AGENTCREW_WEBHOOK_FILTER", "status=failed")
    assert Settings.load().webhook_filter == "status=failed"


def test_yaml_channel_keeps_flat_and_per_target_filter(tmp_path):
    """YAML 顶层与 webhooks[] 两条通道都不能被归一化丢掉。"""
    from agentcrew.config import _yaml_values

    path = tmp_path / "agentcrew.yaml"
    path.write_text(
        'webhook_filter: "status=failed"\n'
        "webhooks:\n"
        "  - name: slack\n"
        "    url: https://hooks.example.com/a\n"
        '    filter: "event=run_finished,status=failed"\n',
        encoding="utf-8")
    values = _yaml_values(path, strict=True)   # strict 会拒未知键，顺带验登记
    assert values["webhook_filter"] == "status=failed"
    assert values["webhooks"][0]["filter"] == "event=run_finished,status=failed"


# ── 真实投递 ──────────────────────────────────────────────────────────────


def test_filter_blocks_unwanted_post_on_real_server(hook_server):
    url, state = hook_server
    sink: list[tuple[str, dict]] = []
    dispatcher = WebhookDispatcher(
        WebhookConfig(url=url, filter="status=failed", max_retries=0),
        sink=lambda name, data: sink.append((name, data)),
    )
    dispatcher.emit("run_finished", {"run_id": "R-1", "status": "completed"})
    dispatcher.emit("run_finished", {"run_id": "R-1", "status": "failed"})
    dispatcher.close()
    received = state["received"]
    assert len(received) == 1
    assert received[0]["data"]["status"] == "failed"
    assert [name for name, _ in sink] == ["webhook_delivered"]
