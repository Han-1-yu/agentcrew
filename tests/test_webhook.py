"""R12：出站 Webhook。

这套编排器此前没有任何对外通知手段：run 结束、审批挂起、任务失败，外部系统一概
不知道。这一层把关键节点 POST 出去。

真实性优先：投递测试跑在**真实的本地 HTTP 服务**上，验证请求头、body 与 HMAC
签名，而不是只看内存里的队列。签名与重试尤其需要真请求才能验。

覆盖：配置校验（URL/超时/重试）、事件过滤、签名正确性、真实投递的头与体、
重试与耗尽、close 排空、未启用零开销、URL 打码。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentcrew.config import Settings
from agentcrew.runtime.webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookConfig,
    WebhookDispatcher,
    WebhookTarget,
    verify_signature,
)


# ── 本地接收端 ────────────────────────────────────────────────────────────


@pytest.fixture
def hook_server():
    """一个真实的本地 HTTP 服务，记录收到的请求；``fail_times`` 控制前几次 500。"""
    state: dict[str, object] = {"received": [], "fail_times": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            state["received"].append({               # type: ignore[union-attr]
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            })
            remaining = state["fail_times"]
            if isinstance(remaining, int) and remaining > 0:
                state["fail_times"] = remaining - 1
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            """静音：pytest 输出里不需要一行一个请求。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook"
    try:
        yield url, state
    finally:
        server.shutdown()
        server.server_close()


def _dispatcher(url: str, **overrides) -> tuple[WebhookDispatcher, list]:
    sink_events: list[tuple[str, dict]] = []
    config = WebhookConfig(url=url, **overrides)
    return WebhookDispatcher(
        config, sink=lambda name, data: sink_events.append((name, data))), sink_events


# ── 配置 ──────────────────────────────────────────────────────────────────


def test_config_matches_event():
    config = WebhookConfig(url="https://h/hook", events=("run_finished",))

    assert config.matches("run_finished")
    assert not config.matches("task_completed")


def test_config_wildcard_matches_everything():
    config = WebhookConfig(url="https://h/hook", events=("*",))

    assert config.matches("task_completed")
    assert config.matches("anything_at_all")


def test_empty_url_means_disabled():
    assert not WebhookConfig().enabled
    assert WebhookConfig(url="https://h/hook").enabled


def test_settings_reject_bad_webhook_url(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_WEBHOOK_URL", "ftp://example.com/hook")

    with pytest.raises(ValueError, match="http/https"):
        Settings.load()


def test_settings_reject_url_with_credentials(tmp_path, monkeypatch):
    """URL 里塞账号密码会进代理日志；签名该走 webhook_secret。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_WEBHOOK_URL", "https://user:pass@example.com/hook")

    with pytest.raises(ValueError, match="用户名密码"):
        Settings.load()


def test_settings_reject_bad_timeout_and_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_WEBHOOK_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError, match="必须 > 0"):
        Settings.load()


def test_settings_parse_events_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_WEBHOOK_EVENTS", "run_finished, run_stalled")

    assert Settings.load().webhook_events == ["run_finished", "run_stalled"]


def test_secret_is_redacted_in_snapshot(tmp_path, monkeypatch):
    """密钥随 run.json 落盘就等于泄露。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_WEBHOOK_SECRET", "topsecret")

    settings = Settings.load()

    assert settings.config_snapshot()["webhook_secret"] == "***"
    assert "topsecret" not in json.dumps(settings.config_snapshot(), ensure_ascii=False)
    # 同时必须进脱敏词表：任何误写进事件的同值都要被抹掉
    assert "topsecret" in settings.secret_values()


# ── 签名 ──────────────────────────────────────────────────────────────────


def test_signature_verifies_roundtrip():
    body = b'{"event":"run_finished"}'
    signature = _sign("s3cret", "1700000000", body)

    assert signature.startswith("sha256=")
    assert verify_signature("s3cret", "1700000000", body, signature)


def test_signature_rejects_wrong_secret_or_timestamp():
    body = b'{"a":1}'
    signature = _sign("s3cret", "1700000000", body)

    assert not verify_signature("other", "1700000000", body, signature)
    # 时间戳参与签名：换一个时间戳就不能通过，这正是防重放的关键
    assert not verify_signature("s3cret", "1700000001", body, signature)


def test_signature_rejects_tampered_body():
    signature = _sign("s3cret", "1700000000", b'{"a":1}')

    assert not verify_signature("s3cret", "1700000000", b'{"a":2}', signature)


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    from agentcrew.runtime.webhook import _sign as sign

    return sign(secret, timestamp, body)


# ── 真实投递 ──────────────────────────────────────────────────────────────


def test_delivers_real_request_with_headers_and_body(hook_server):
    url, state = hook_server
    dispatcher, _ = _dispatcher(url, secret="s3cret", events=("run_finished",))

    dispatcher.emit("run_finished", {"run_id": "R1", "status": "completed"})
    dispatcher.close()

    assert len(state["received"]) == 1
    request = state["received"][0]
    headers = request["headers"]
    assert headers[EVENT_HEADER.lower()] == "run_finished"
    assert headers[DELIVERY_HEADER.lower()]
    assert headers["content-type"].startswith("application/json")

    body = request["body"]
    envelope = json.loads(body)
    assert envelope["event"] == "run_finished"
    assert envelope["data"] == {"run_id": "R1", "status": "completed"}
    # body 里的时间戳必须与头部一致，否则接收方校验会失败
    assert str(envelope["timestamp"]) == headers[TIMESTAMP_HEADER.lower()]
    # 签名覆盖 <timestamp>.<body>
    assert verify_signature(
        "s3cret", headers[TIMESTAMP_HEADER.lower()], body,
        headers[SIGNATURE_HEADER.lower()])


def test_no_signature_header_without_secret(hook_server):
    url, state = hook_server
    dispatcher, _ = _dispatcher(url, events=("run_finished",))

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert SIGNATURE_HEADER.lower() not in state["received"][0]["headers"]


def test_unsubscribed_events_are_not_sent(hook_server):
    url, state = hook_server
    dispatcher, _ = _dispatcher(url, events=("run_finished",))

    dispatcher.emit("task_completed", {"task_id": "T-1"})
    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    events = [json.loads(item["body"])["event"] for item in state["received"]]
    assert events == ["run_finished"]


def test_disabled_dispatcher_sends_nothing(hook_server):
    url, state = hook_server
    dispatcher, _ = _dispatcher("", events=("*",))

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert state["received"] == []
    assert dispatcher.enabled is False


def test_retries_then_succeeds(hook_server):
    """前两次 500、第三次 200：应重试并最终判成功。"""
    url, state = hook_server
    state["fail_times"] = 2
    dispatcher, sink_events = _dispatcher(
        url, events=("run_finished",), max_retries=2)

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert len(state["received"]) == 3
    assert [name for name, _ in sink_events] == ["webhook_delivered"]
    # 重试之间 delivery id 不变，接收方可据此幂等
    deliveries = {
        item["headers"][DELIVERY_HEADER.lower()] for item in state["received"]}
    assert len(deliveries) == 1


def test_exhausted_retries_report_failure(hook_server):
    url, state = hook_server
    state["fail_times"] = 99
    dispatcher, sink_events = _dispatcher(
        url, events=("run_finished",), max_retries=1)

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert len(state["received"]) == 2
    assert sink_events[-1][0] == "webhook_failed"
    assert "HTTP 500" in sink_events[-1][1]["error"]


def test_unreachable_endpoint_reports_failure():
    """连不上也要如实记为失败，且不抛异常给调用方。"""
    dispatcher, sink_events = _dispatcher(
        "http://127.0.0.1:9/hook", events=("run_finished",), max_retries=0,
        timeout_seconds=0.5)

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert sink_events and sink_events[-1][0] == "webhook_failed"
    assert dispatcher.failed == 1


def test_close_drains_queue(hook_server):
    """close 必须等队列排空：run_finished 往往就是最后一条通知。"""
    url, state = hook_server
    dispatcher, _ = _dispatcher(url, events=("*",))

    for index in range(5):
        dispatcher.emit("run_started", {"n": index})
    dispatcher.close()

    assert len(state["received"]) == 5


def test_emit_after_close_is_ignored(hook_server):
    url, state = hook_server
    dispatcher, _ = _dispatcher(url, events=("*",))
    dispatcher.close()

    dispatcher.emit("run_finished", {"run_id": "R1"})

    assert state["received"] == []


def test_delivery_result_lands_in_ledger(tmp_path, hook_server):
    """回归：结果字典曾用 ``event`` 作键，与 ``ledger.emit(event, **data)`` 的
    位置参数撞名，抛 TypeError 把整个 run 打挂（实测 exit_code=1）。

    这条用例跑真实 Ledger——内存里的假 sink 发现不了参数名冲突。
    """
    from agentcrew.runtime.ledger import Ledger

    url, _ = hook_server
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    config = WebhookConfig(url=url, events=("run_finished",))
    dispatcher = WebhookDispatcher(
        config, sink=lambda name, data: ledger.emit(name, **data))

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert ledger.count("webhook_delivered") == 1
    assert ledger.count("webhook_failed") == 0


# ── R14：多接收端与条件过滤 ──────────────────────────────────────────────

def _write_yaml(tmp_path, body: str) -> None:
    """写配置文件（用 ``dedent`` 保住缩进，避免转义字符串被写坏）。"""
    from textwrap import dedent

    (tmp_path / "agentcrew.yaml").write_text(dedent(body).strip() + "\n",
                                             encoding="utf-8")



@pytest.fixture
def server_factory():
    """按需启动多个本地接收端；退出时统一关闭。

    多接收端必须用**互不相同**的端口才能验证「各收各的」，单例 fixture 做不到。
    """
    started: list[ThreadingHTTPServer] = []

    def _make() -> tuple[str, dict[str, object]]:
        state: dict[str, object] = {"received": [], "fail_times": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                state["received"].append({          # type: ignore[union-attr]
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                })
                remaining = state["fail_times"]
                if isinstance(remaining, int) and remaining > 0:
                    state["fail_times"] = remaining - 1
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/hook", state

    try:
        yield _make
    finally:
        for server in started:
            server.shutdown()
            server.server_close()


def _multi(url_a: str, url_b: str, **kw) -> tuple[WebhookDispatcher, list]:
    sink_events: list[tuple[str, dict]] = []
    config = WebhookConfig(targets=(
        WebhookTarget(name="a", url=url_a, **kw),
        WebhookTarget(name="b", url=url_b, **kw),
    ))
    return WebhookDispatcher(
        config, sink=lambda n, d: sink_events.append((n, d))), sink_events


def test_each_target_receives(server_factory):
    url_a, state_a = server_factory()
    url_b, state_b = server_factory()
    dispatcher, _ = _multi(url_a, url_b, events=("run_finished",))

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert len(state_a["received"]) == 1
    assert len(state_b["received"]) == 1
    # 同一次事件共享 delivery id，接收方之间可对账
    id_a = state_a["received"][0]["headers"][DELIVERY_HEADER.lower()]
    id_b = state_b["received"][0]["headers"][DELIVERY_HEADER.lower()]
    assert id_a == id_b


def test_target_event_filters_are_independent(server_factory):
    url_all, state_all = server_factory()
    url_only_failed, state_only = server_factory()
    sink_events: list[tuple[str, dict]] = []
    config = WebhookConfig(targets=(
        WebhookTarget(name="all", url=url_all, events=("*",)),
        WebhookTarget(name="fin", url=url_only_failed, events=("run_finished",)),
    ))
    dispatcher = WebhookDispatcher(
        config, sink=lambda n, d: sink_events.append((n, d)))

    dispatcher.emit("run_started", {"run_id": "R1"})
    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert len(state_all["received"]) == 2
    # 只订阅 run_finished 的那个收不到 run_started
    assert len(state_only["received"]) == 1


def test_only_failures_skips_success(server_factory):
    url, state = server_factory()
    sink_events: list[tuple[str, dict]] = []
    config = WebhookConfig(targets=(
        WebhookTarget(name="alerts", url=url, only_failures=True),
    ))
    dispatcher = WebhookDispatcher(
        config, sink=lambda n, d: sink_events.append((n, d)))

    dispatcher.emit("run_finished", {"status": "completed"})
    dispatcher.emit("run_finished", {"status": "failed"})
    dispatcher.close()

    bodies = [json.loads(item["body"]) for item in state["received"]]
    assert [b["data"]["status"] for b in bodies] == ["failed"]


def test_only_failures_accepts_incident_events(server_factory):
    """deadlock / run_stalled / error 一律算坏消息，即使 payload 没有 status。"""
    url, state = server_factory()
    sink_events: list[tuple[str, dict]] = []
    config = WebhookConfig(targets=(
        WebhookTarget(name="alerts", url=url, only_failures=True),
    ))
    dispatcher = WebhookDispatcher(
        config, sink=lambda n, d: sink_events.append((n, d)))

    dispatcher.emit("deadlock", {})
    dispatcher.emit("run_finished", {"status": "cancelled"})
    dispatcher.close()

    assert len(state["received"]) == 2


def test_targets_override_legacy_single_url(server_factory):
    """同时配旧字段与 targets：以 targets 为准，避免同一地址被推两遍。"""
    url_legacy, state_legacy = server_factory()
    url_new, state_new = server_factory()
    config = WebhookConfig(
        url=url_legacy,
        events=("run_finished",),
        targets=(WebhookTarget(name="new", url=url_new, events=("run_finished",)),
                 ),
    )
    dispatcher = WebhookDispatcher(config)

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert len(state_new["received"]) == 1
    assert state_legacy["received"] == []


def test_each_target_signs_with_own_secret(server_factory):
    url_a, state_a = server_factory()
    url_b, state_b = server_factory()
    config = WebhookConfig(targets=(
        WebhookTarget(name="a", url=url_a, secret="secret-a",
                      events=("run_finished",)),
        WebhookTarget(name="b", url=url_b, secret="secret-b",
                      events=("run_finished",)),
    ))
    dispatcher = WebhookDispatcher(config)

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    for state, secret in ((state_a, "secret-a"), (state_b, "secret-b")):
        item = state["received"][0]
        headers = item["headers"]
        assert verify_signature(
            secret, headers[TIMESTAMP_HEADER.lower()], item["body"],
            headers[SIGNATURE_HEADER.lower()])
        # 拿别人的密钥验不过
        other = "secret-b" if secret == "secret-a" else "secret-a"
        assert not verify_signature(
            other, headers[TIMESTAMP_HEADER.lower()], item["body"],
            headers[SIGNATURE_HEADER.lower()])


def test_one_target_failure_does_not_block_other(server_factory):
    url_ok, state_ok = server_factory()
    url_bad, state_bad = server_factory()
    state_bad["fail_times"] = 99
    sink_events: list[tuple[str, dict]] = []
    config = WebhookConfig(
        max_retries=0,
        targets=(
            WebhookTarget(name="bad", url=url_bad, events=("run_finished",)),
            WebhookTarget(name="ok", url=url_ok, events=("run_finished",)),
        ),
    )
    dispatcher = WebhookDispatcher(
        config, sink=lambda n, d: sink_events.append((n, d)))

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert len(state_ok["received"]) == 1, "好接收端不该被坏的拖累"
    names = {name for name, _ in sink_events}
    assert names == {"webhook_failed", "webhook_delivered"}
    # 台账里带接收端名，能看出是哪一条失败
    failed = [data for name, data in sink_events if name == "webhook_failed"]
    assert failed[0]["target"] == "bad"


def test_settings_validate_webhooks_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_CONFIG", str(tmp_path / "agentcrew.yaml"))
    _write_yaml(tmp_path, """
        webhooks:
          - name: a
        """)

    with pytest.raises(ValueError, match="缺少 url"):
        Settings.load()


def test_settings_reject_bad_scheme_in_webhooks(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_CONFIG", str(tmp_path / "agentcrew.yaml"))
    _write_yaml(tmp_path, """
        webhooks:
          - name: a
            url: ftp://h/h
        """)

    with pytest.raises(ValueError, match="http/https"):
        Settings.load()


def test_settings_normalize_webhooks(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_CONFIG", str(tmp_path / "agentcrew.yaml"))
    _write_yaml(tmp_path, """
        webhooks:
          - name: slack
            url: https://h/h
            only_failures: true
            events: [run_finished, deadlock]
        """)

    target = Settings.load().webhooks[0]

    assert target["name"] == "slack"
    assert target["only_failures"] is True
    assert target["events"] == ["run_finished", "deadlock"]


def test_crew_builds_dispatcher_from_webhooks_only(tmp_path, monkeypatch):
    """回归：``_build_webhook`` 曾只看 ``webhook_url``，只配 ``webhooks`` 时整条
    链路静默失效（端到端实跑时才暴露：接收端一条都收不到）。

    单测层面必须覆盖 Crew 的入口判断——直接构造 ``WebhookConfig`` 发现不了它。
    """
    from agentcrew.crew.crew import Crew
    from agentcrew.runtime.ledger import Ledger

    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load().model_copy(update={
        "webhooks": [{"name": "slack", "url": "https://h/hook",
                      "only_failures": True, "events": [], "secret": ""}],
    })
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")

    dispatcher = Crew(settings)._build_webhook(ledger)

    assert dispatcher is not None
    assert dispatcher.enabled
    events = list(ledger.events("webhook_enabled"))
    assert events[0].data["targets"][0]["name"] == "slack"
    assert events[0].data["targets"][0]["only_failures"] is True
    dispatcher.close()


def test_crew_without_any_target_returns_none(tmp_path, monkeypatch):
    from agentcrew.crew.crew import Crew
    from agentcrew.runtime.ledger import Ledger

    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")

    assert Crew(Settings.load())._build_webhook(ledger) is None
    assert ledger.count("webhook_enabled") == 0
