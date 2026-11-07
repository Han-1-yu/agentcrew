"""R15：Webhook 持久化重放队列。

R12/R14 的边界是「进程退出时队列里没投出去的通知直接丢」。这里验证的是：
事件在**入队前**就落盘、成功即删、失败留盘、下次启动按原 delivery 补投，
以及密钥/URL 不落盘、坏记录有淘汰上限。

投递类用例一律跑真实本地 HTTP 服务（沿用 R12 的口径），不用内存假投递。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentcrew.runtime.webhook import (
    DELIVERY_HEADER,
    SIGNATURE_HEADER,
    SPOOL_DIR_NAME,
    TIMESTAMP_HEADER,
    PendingDelivery,
    WebhookConfig,
    WebhookDispatcher,
    WebhookSpool,
    WebhookTarget,
    _target_key,
    verify_signature,
)


@pytest.fixture
def server_factory():
    """起任意多个本地接收端；``fail_times`` 控制前几次返回 500。"""
    servers: list[ThreadingHTTPServer] = []

    def make():
        state: dict[str, object] = {"received": [], "fail_times": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                remaining = state["fail_times"]
                status = 200
                if isinstance(remaining, int) and remaining > 0:
                    state["fail_times"] = remaining - 1
                    status = 500
                state["received"].append({  # type: ignore[union-attr]
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                    "status": status,
                })
                self.send_response(status)
                self.end_headers()
                if status == 200:
                    self.wfile.write(b"ok")

            def log_message(self, *args: object) -> None:
                """静音。"""

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/hook", state

    try:
        yield make
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def _record(**overrides) -> PendingDelivery:
    data = {
        "event": "run_finished",
        "payload": {"run_id": "R1", "status": "completed"},
        "delivery": "d" * 32,
        "timestamp": str(int(time.time())),
        "target_key": _target_key("https://h/hook"),
        "created_at": time.time(),
    }
    data.update(overrides)
    return PendingDelivery(**data)


def _spool_files(directory) -> list:
    return sorted(directory.glob("*.json"))


def _dispatcher(url, spool_dir, **overrides):
    sink_events: list[tuple[str, dict]] = []
    config = WebhookConfig(url=url, events=("*",), **overrides)
    dispatcher = WebhookDispatcher(
        config,
        sink=lambda name, data: sink_events.append((name, data)),
        spool_dir=spool_dir,
    )
    return dispatcher, sink_events


# ── spool 文件层 ──────────────────────────────────────────────────────────


def test_spool_roundtrip_preserves_identity(tmp_path):
    spool = WebhookSpool(tmp_path / "s")
    original = _record()

    path = spool.add(original)
    (pending, discarded) = spool.load()

    assert path is not None and path.exists()
    assert discarded == []
    assert len(pending) == 1
    assert pending[0][1] == original


def test_spool_redacts_secrets_before_writing(tmp_path):
    spool = WebhookSpool(tmp_path / "s", secrets=("s3cret-token",))
    original = _record(payload={"note": "key=s3cret-token", "nested": ["s3cret-token"]})

    path = spool.add(original)

    assert path is not None
    assert "s3cret-token" not in path.read_text(encoding="utf-8")
    assert spool.load()[0][0][1].payload == {
        "note": "key=***", "nested": ["***"]}


def test_target_key_is_hash_not_url():
    key = _target_key("https://h/hook?token=abc")

    assert len(key) == 16
    assert "hook" not in key and "token" not in key
    assert key == _target_key("https://h/hook?token=abc")
    assert key != _target_key("https://h/other")


def test_spool_drops_corrupt_file(tmp_path):
    spool = WebhookSpool(tmp_path / "s")
    spool.directory.mkdir(parents=True)
    bad = spool.directory / "broken.json"
    bad.write_text("{not json", encoding="utf-8")

    pending, discarded = spool.load()

    assert pending == []
    assert discarded == [("", "corrupt")]
    assert not bad.exists()


def test_spool_drops_bad_shape(tmp_path):
    spool = WebhookSpool(tmp_path / "s")
    spool.add(_record())
    path = _spool_files(spool.directory)[0]
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["version"] = 99
    path.write_text(json.dumps(raw), encoding="utf-8")

    pending, discarded = spool.load()

    assert pending == []
    assert discarded == [("", "bad_shape")]
    assert not path.exists()


def test_spool_drops_expired_and_over_attempts(tmp_path):
    spool = WebhookSpool(tmp_path / "s", max_age_seconds=1, max_attempts=2)
    stale = _record(created_at=time.time() - 10, delivery="a" * 32)
    tired = _record(attempts=3, delivery="b" * 32)
    spool.add(stale)
    spool.add(tired)

    pending, discarded = spool.load()

    assert pending == []
    assert sorted(reason for _, reason in discarded) == ["expired", "max_attempts"]
    assert _spool_files(spool.directory) == []


def test_spool_retry_increments_then_removes(tmp_path):
    spool = WebhookSpool(tmp_path / "s", max_attempts=2)
    record = _record()
    path = spool.add(record)
    assert path is not None

    for expected in (1, 2, 3):
        spool.retry(path, _record(attempts=expected - 1))
        files = _spool_files(spool.directory)
        if expected <= 2:
            assert json.loads(files[0].read_text(encoding="utf-8"))["attempts"] == expected
    assert _spool_files(spool.directory) == []


# ── 分发器：落盘 / 删除 / 补投 ────────────────────────────────────────────


def test_without_spool_dir_behavior_is_r14(server_factory, tmp_path):
    """不传 spool_dir：零落盘、结果不带 replayed，与 R14 逐字一致。"""
    url, state = server_factory()
    sink_events: list[tuple[str, dict]] = []
    dispatcher = WebhookDispatcher(
        WebhookConfig(url=url, events=("*",)),
        sink=lambda name, data: sink_events.append((name, data)),
    )

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert dispatcher.spool_enabled is False
    assert not (tmp_path / SPOOL_DIR_NAME).exists()
    assert len(state["received"]) == 1  # type: ignore[arg-type]
    assert "replayed" not in sink_events[0][1]


def test_emit_persists_before_delivery_and_keeps_failed_record(
        server_factory, tmp_path):
    """入队即落盘；投递失败后记录留着等补投。"""
    url, state = server_factory()
    state["fail_times"] = 99
    spool_dir = tmp_path / SPOOL_DIR_NAME
    dispatcher, sink_events = _dispatcher(url, spool_dir, max_retries=0, timeout_seconds=0.5)

    dispatcher.emit("run_finished", {"run_id": "R1"})
    # 关键：不等后台线程、不等 close，此刻记录已经在盘上——进程被强杀也不丢。
    assert len(_spool_files(spool_dir)) == 1
    dispatcher.close()

    files = _spool_files(spool_dir)
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["attempts"] == 1
    assert sink_events[-1][0] == "webhook_failed"


def test_successful_delivery_removes_spool_file(server_factory, tmp_path):
    url, _ = server_factory()
    spool_dir = tmp_path / SPOOL_DIR_NAME
    dispatcher, sink_events = _dispatcher(url, spool_dir, max_retries=0)

    dispatcher.emit("run_finished", {"run_id": "R1"})
    dispatcher.close()

    assert _spool_files(spool_dir) == []
    assert [name for name, _ in sink_events] == ["webhook_delivered"]


def test_replay_after_simulated_crash_keeps_delivery_and_signature(
        server_factory, tmp_path):
    """核心回归：第一次全失败留盘，第二次启动自动补投，delivery 与签名不变。"""
    url, state = server_factory()
    state["fail_times"] = 99
    spool_dir = tmp_path / SPOOL_DIR_NAME
    secret = "s3cret-token"

    first, _ = _dispatcher(url, spool_dir, max_retries=0, secret=secret, timeout_seconds=0.5)
    first.emit("run_finished", {"run_id": "R1", "status": "failed"})
    first.close()
    failed_request = state["received"][-1]  # type: ignore[index]
    delivery = failed_request["headers"][DELIVERY_HEADER.lower()]
    timestamp = failed_request["headers"][TIMESTAMP_HEADER.lower()]
    assert len(_spool_files(spool_dir)) == 1

    # 对端恢复，模拟新进程用同一份配置启动
    state["fail_times"] = 0
    sink_events: list[tuple[str, dict]] = []
    second = WebhookDispatcher(
        WebhookConfig(url=url, events=("*",), secret=secret, max_retries=0),
        sink=lambda name, data: sink_events.append((name, data)),
        spool_dir=spool_dir,
    )
    second.emit("run_started", {"run_id": "R2"})  # 触发 drain，不干扰补投
    second.close()

    replayed = [item for item in state["received"]  # type: ignore[union-attr]
                if item["headers"][DELIVERY_HEADER.lower()] == delivery]
    assert len(replayed) == 2  # 第一次失败 + 补投成功
    ok_request = replayed[-1]
    assert json.loads(ok_request["body"])["data"]["run_id"] == "R1"
    assert ok_request["headers"][TIMESTAMP_HEADER.lower()] == timestamp
    assert verify_signature(
        secret, timestamp, ok_request["body"],
        ok_request["headers"][SIGNATURE_HEADER.lower()])
    assert any(name == "webhook_delivered" and data.get("replayed") is True
               for name, data in sink_events)
    assert _spool_files(spool_dir) == []  # 补投成功后清理，新事件也投完删掉了


def test_replay_discards_record_when_target_changed(server_factory, tmp_path):
    """URL 变了就不该把旧事件发到新地址。"""
    old_url, old_state = server_factory()
    new_url, new_state = server_factory()
    state = old_state
    state["fail_times"] = 99
    spool_dir = tmp_path / SPOOL_DIR_NAME
    first, _ = _dispatcher(old_url, spool_dir, max_retries=0, timeout_seconds=0.5)
    first.emit("run_finished", {"run_id": "R1"})
    first.close()
    assert len(_spool_files(spool_dir)) == 1

    sink_events: list[tuple[str, dict]] = []
    second = WebhookDispatcher(
        WebhookConfig(url=new_url, events=("*",)),
        sink=lambda name, data: sink_events.append((name, data)),
        spool_dir=spool_dir,
    )
    second.close()

    assert new_state["received"] == []  # type: ignore[union-attr]
    assert _spool_files(spool_dir) == []
    assert any(name == "webhook_failed"
               and data.get("error") == "replay_target_missing"
               for name, data in sink_events)


def test_replay_only_retries_the_failed_target(server_factory, tmp_path):
    """多接收端下，一个成功一个失败：补投只给失败的那个，不重复轰炸成功的。"""
    good_url, good_state = server_factory()
    flaky_url, flaky_state = server_factory()
    flaky_state["fail_times"] = 99
    spool_dir = tmp_path / SPOOL_DIR_NAME

    config = WebhookConfig(
        targets=(
            WebhookTarget(name="good", url=good_url),
            WebhookTarget(name="flaky", url=flaky_url),
        ),
        max_retries=0,
    )
    first = WebhookDispatcher(config, spool_dir=spool_dir)
    first.emit("run_finished", {"run_id": "R1"})
    first.close()
    assert len(good_state["received"]) == 1  # type: ignore[arg-type]
    assert len(_spool_files(spool_dir)) == 1  # 只剩 flaky

    flaky_state["fail_times"] = 0
    sink_events: list[tuple[str, dict]] = []
    second = WebhookDispatcher(
        config, sink=lambda name, data: sink_events.append((name, data)),
        spool_dir=spool_dir)
    second.close()

    assert len(good_state["received"]) == 1  # type: ignore[arg-type]
    assert len(flaky_state["received"]) == 2  # 失败一次 + 补投一次 # type: ignore[arg-type]
    assert _spool_files(spool_dir) == []


def test_replay_failure_does_not_raise_and_stays_pending(server_factory, tmp_path):
    """补投又失败：不抛给调用方，记录继续留着并累加 attempts。"""
    url, state = server_factory()
    state["fail_times"] = 99
    spool_dir = tmp_path / SPOOL_DIR_NAME
    first, _ = _dispatcher(url, spool_dir, max_retries=0, timeout_seconds=0.5)
    first.emit("run_finished", {"run_id": "R1"})
    first.close()

    second, sink_events = _dispatcher(url, spool_dir, max_retries=0, timeout_seconds=0.5)
    second.close()

    files = _spool_files(spool_dir)
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["attempts"] == 2
    assert any(name == "webhook_failed" and data.get("replayed") is True
               for name, data in sink_events)


def test_spool_is_not_treated_as_a_run_directory(tmp_path):
    """clean / list_runs 只认含 run.json 的目录，spool 不会被误删。"""
    from agentcrew.runtime.checkpoint import CheckpointStore

    spool = tmp_path / SPOOL_DIR_NAME
    spool.mkdir()
    (spool / "pending.json").write_text("{}", encoding="utf-8")

    assert CheckpointStore.list_runs(tmp_path) == []


# ── Crew 装配层 ───────────────────────────────────────────────────────────


def test_crew_wires_spool_and_secret_redaction(tmp_path, monkeypatch):
    """入口接线：Crew 传 spool_dir 与 secret_values；不能直接构造分发器替代。"""
    from agentcrew.config import Settings
    from agentcrew.crew.crew import Crew
    from agentcrew.runtime.ledger import Ledger

    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load().model_copy(update={
        "webhook_url": "https://h/hook",
        "webhook_secret": "s3cret-token",
    })
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    spool_dir = tmp_path / SPOOL_DIR_NAME

    dispatcher = Crew(settings)._build_webhook(ledger, spool_dir=spool_dir)

    assert dispatcher is not None and dispatcher.spool_enabled
    event = list(ledger.events("webhook_enabled"))[0]
    assert event.data["spool_enabled"] is True
    assert "s3cret-token" not in json.dumps(event.data)
    dispatcher.close()


def test_crew_without_spool_dir_still_works(tmp_path, monkeypatch):
    """_build_webhook 的新参数必须可选：库层旧调用一行不改。"""
    from agentcrew.config import Settings
    from agentcrew.crew.crew import Crew
    from agentcrew.runtime.ledger import Ledger

    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load().model_copy(update={"webhook_url": "https://h/hook"})
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")

    dispatcher = Crew(settings)._build_webhook(ledger)

    assert dispatcher is not None and not dispatcher.spool_enabled
    dispatcher.close()


# ── 端到端：真 CLI run + 真 HTTP 接收端 ──────────────────────────────────


def _run_sample(env: dict[str, str]) -> subprocess.CompletedProcess:
    root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "main.py", "run", "--sample", "linear",
         "--no-live", "--auto"],
        cwd=root, env=env, capture_output=True, text=True, timeout=180,
        errors="replace",
    )


def test_end_to_end_run_replays_failed_notifications(server_factory, tmp_path):
    """两次真实 CLI run：第一次对端全挂（留盘），第二次启动自动补投。

    单测覆盖了分发器与 Crew 入口，但只有真 run 能验证 ``_start`` 是否把
    spool_dir 接上、以及 ``_finalize`` 的收尾顺序没有把待投记录提前清掉。
    """
    url, state = server_factory()
    state["fail_times"] = 99
    output_dir = tmp_path / "output"
    env = {
        **os.environ,
        "AGENTCREW_MOCK": "true",
        "AGENTCREW_OUTPUT_DIR": str(output_dir),
        "AGENTCREW_WEBHOOK_URL": url,
        "AGENTCREW_WEBHOOK_SECRET": "s3cret-token",
        "PYTHONPATH": "",
    }

    first = _run_sample(env)
    assert first.returncode == 0, first.stdout + first.stderr
    spool = output_dir / SPOOL_DIR_NAME
    assert len(_spool_files(spool)) >= 1
    first_deliveries = {
        item["headers"][DELIVERY_HEADER.lower()]
        for item in state["received"]  # type: ignore[union-attr]
    }
    assert first_deliveries

    # 对端恢复：新 run 启动时必须补投上一次没投出去的通知
    state["fail_times"] = 0
    second = _run_sample(env)
    assert second.returncode == 0, second.stdout + second.stderr
    ok_deliveries = {
        item["headers"][DELIVERY_HEADER.lower()]
        for item in state["received"]  # type: ignore[union-attr]
        if item["status"] == 200
    }
    # 上一轮失败的那些 delivery 必须以 2xx 重新到达（id 不变 → 接收方可幂等）
    assert first_deliveries <= ok_deliveries
    assert _spool_files(spool) == []

    ledgers = list(output_dir.glob("*/ledger.jsonl"))
    texts = "\n".join(p.read_text(encoding="utf-8") for p in ledgers)
    assert '"spool_enabled": true' in texts or '"spool_enabled":true' in texts
    assert "webhook_delivered" in texts
    assert '"replayed": true' in texts or '"replayed":true' in texts
