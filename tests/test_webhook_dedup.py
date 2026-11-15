"""C3：补投去重表——堵住「多进程同时扫到同一份 spool 文件」的双投。

R15 是 at-least-once：load() 只 glob+read，没有原子认领；两个进程都 POST 成功后
才各自 remove()，后者 unlink 抛 OSError 被吞，但对端已经收了两次。本表在投递
成功后追加一行 (delivery, target_key)，补投入口命中即跳过。

真实性优先：端到端用例跑真实本地 HTTP 服务，断言请求次数而不是只看内存状态。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentcrew.runtime.webhook import (
    DEDUP_DIR_NAME,
    DEDUP_FILE_NAME,
    DEDUP_MAX_AGE_SECONDS,
    SPOOL_DIR_NAME,
    DeliveryDedup,
    PendingDelivery,
    WebhookConfig,
    WebhookDispatcher,
    WebhookSpool,
    _target_key,
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


def mk_record(url: str, delivery: str = "D-1") -> PendingDelivery:
    return PendingDelivery(
        event="run_finished", payload={"run_id": "R-1", "status": "completed"},
        delivery=delivery, timestamp=str(int(time.time())),
        target_key=_target_key(url), created_at=time.time(),
    )


def mk_dispatcher(url: str, spool_dir, dedup_dir):
    sink: list[tuple[str, dict]] = []
    dispatcher = WebhookDispatcher(
        WebhookConfig(url=url, max_retries=0),
        sink=lambda name, data: sink.append((name, data)),
        spool_dir=spool_dir, dedup_dir=dedup_dir,
    )
    return dispatcher, sink


# ── 存储层 ────────────────────────────────────────────────────────────────


def test_mark_seen_and_reload_across_instances(tmp_path):
    record = mk_record("http://127.0.0.1:9/hook")
    dedup = DeliveryDedup(tmp_path)
    assert dedup.seen(record) is False
    dedup.mark(record)
    assert dedup.seen(record) is True
    assert DeliveryDedup(tmp_path).seen(record) is True
    assert dedup.path.name == DEDUP_FILE_NAME
    # 这里直接把 tmp_path 当 dedup_dir；生产路径的目录名常量由
    # test_dedup_never_pollutes_spool_namespace 与 Crew._start 保证。
    assert dedup.path.parent == tmp_path


def test_key_includes_target_key(tmp_path):
    """delivery 在多接收端间共享；只按 delivery 去重会误杀正常的另一端补投。"""
    record = mk_record("http://127.0.0.1:9/a")
    dedup = DeliveryDedup(tmp_path)
    dedup.mark(record)
    other = replace(record, target_key=_target_key("http://127.0.0.1:9/b"))
    assert dedup.seen(record) is True
    assert dedup.seen(other) is False


def test_old_and_bad_lines_are_ignored(tmp_path):
    path = tmp_path / DEDUP_FILE_NAME
    live = mk_record("http://127.0.0.1:9/hook", "D-live")
    old = mk_record("http://127.0.0.1:9/hook", "D-old")
    path.write_text(
        "{not json\n"
        + json.dumps({"delivery": old.delivery, "target_key": old.target_key,
                      "ts": time.time() - DEDUP_MAX_AGE_SECONDS - 1}) + "\n"
        + json.dumps({"delivery": live.delivery, "target_key": live.target_key,
                      "ts": time.time()}) + "\n",
        encoding="utf-8")
    dedup = DeliveryDedup(tmp_path)
    assert dedup.seen(live) is True
    assert dedup.seen(old) is False


def test_dedup_never_pollutes_spool_namespace(tmp_path):
    """spool.load() 只 glob *.json；去重表必须独立目录 + .jsonl。"""
    spool_dir, dedup_dir = tmp_path / SPOOL_DIR_NAME, tmp_path / DEDUP_DIR_NAME
    WebhookSpool(spool_dir).add(mk_record("http://127.0.0.1:9/hook"))
    DeliveryDedup(dedup_dir).mark(mk_record("http://127.0.0.1:9/hook"))
    assert len(list(spool_dir.glob("*.json"))) == 1
    assert len(list(dedup_dir.glob("*.json"))) == 0
    assert len(list(dedup_dir.glob("*.jsonl"))) == 1


# ── 端到端 ────────────────────────────────────────────────────────────────


def test_already_delivered_replay_is_dropped_without_post(tmp_path, hook_server):
    url, state = hook_server
    spool_dir, dedup_dir = tmp_path / SPOOL_DIR_NAME, tmp_path / DEDUP_DIR_NAME
    record = mk_record(url)
    WebhookSpool(spool_dir).add(record)
    DeliveryDedup(dedup_dir).mark(record)   # 模拟另一个进程已经投成功

    dispatcher, sink = mk_dispatcher(url, spool_dir, dedup_dir)
    dispatcher.close()

    assert state["received"] == []
    assert list(spool_dir.glob("*.json")) == []
    assert any(
        name == "webhook_failed" and data.get("error") == "replay_duplicate"
        for name, data in sink)


def test_success_marks_dedup_and_suppresses_leftover_replay(tmp_path, hook_server):
    url, state = hook_server
    spool_dir, dedup_dir = tmp_path / SPOOL_DIR_NAME, tmp_path / DEDUP_DIR_NAME
    record = mk_record(url)
    WebhookSpool(spool_dir).add(record)

    first, sink1 = mk_dispatcher(url, spool_dir, dedup_dir)
    first.close()
    assert len(state["received"]) == 1
    assert list(spool_dir.glob("*.json")) == []
    assert DeliveryDedup(dedup_dir).seen(record) is True
    assert any(name == "webhook_delivered" for name, _ in sink1)

    # 模拟 remove 失败 / 崩溃窗口：残留文件重现，下一次启动不得再发一次。
    WebhookSpool(spool_dir).add(record)
    second, sink2 = mk_dispatcher(url, spool_dir, dedup_dir)
    second.close()
    assert len(state["received"]) == 1
    assert any(
        name == "webhook_failed" and data.get("error") == "replay_duplicate"
        for name, data in sink2)
    assert list(spool_dir.glob("*.json")) == []


def test_dedup_disabled_keeps_old_replay_behavior(tmp_path, hook_server):
    """dedup_dir=None 时零文件、零扫描，R15 原路径不变。"""
    url, state = hook_server
    spool_dir = tmp_path / SPOOL_DIR_NAME
    record = mk_record(url)
    WebhookSpool(spool_dir).add(record)
    sink: list[tuple[str, dict]] = []
    dispatcher = WebhookDispatcher(
        WebhookConfig(url=url, max_retries=0),
        sink=lambda name, data: sink.append((name, data)),
        spool_dir=spool_dir,          # 故意不传 dedup_dir
    )
    assert dispatcher.dedup_enabled is False
    dispatcher.close()
    assert len(state["received"]) == 1
    assert not (tmp_path / DEDUP_DIR_NAME).exists()
