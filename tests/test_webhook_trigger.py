"""C8：入站 Webhook 触发 run（``POST /webhooks/trigger``）。

钉住五件事：

1. **HMAC 即认证**：正确签名（R12 出站同语义：`<timestamp>.<body>` 的
   ``sha256=<hex>``）免 API Key 就能触发；
2. **防重放**：时间戳缺失 / 偏离 300 秒一律 401；
3. **签名失败留痕**：``webhook_signature_invalid`` 进 ``security_events``，
   且不产生任何 run 目录；
4. **来源可审计**：触发成功的 run ledger 里有 ``run_triggered(source=webhook)``；
5. **认证不变量**：不带签名头时与其他路由同形（认证开启 + 无 key = 401），
   不给探测者任何「这个端点存在但没开」的额外信息。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcrew.api.rbac import SCOPE_WEBHOOK_TRIGGER, required_scope
from agentcrew.api.server import ApiServer
from agentcrew.runtime.ledger import Ledger

SECRET = "trigger-secret"


def _sign(ts: str, body: bytes) -> str:
    digest = hmac.new(
        SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class _FakeCrew:
    """与 test_api_server 的替身同形：duck-type ``run()`` / ``cancel()``。"""

    def __init__(self, seq: list[int]) -> None:
        seq[0] += 1
        self.run_id = f"20261112-1000{seq[0]:02d}-ab12"
        self.cancelled = False
        self._on_event = None
        self.goal: str | None = None

    async def run(self, goal: str) -> None:
        self.goal = goal
        if self._on_event is not None:
            self._on_event("run_started", {"run_id": self.run_id, "goal": goal})
        await asyncio.sleep(0)
        if self._on_event is not None:
            self._on_event("run_finished",
                           {"run_id": self.run_id, "status": "completed"})


async def _request(
    port: int, path: str, *, headers: dict[str, str], body: bytes = b"",
) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    writer.write(
        f"POST {path} HTTP/1.1\r\nHost: localhost\r\n{extra}"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    await writer.drain()
    raw = await reader.read(65536)
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass
    head, _, body_text = raw.decode("utf-8", errors="replace").partition("\r\n\r\n")
    status = int(head.split("\r\n")[0].split(" ")[1])
    try:
        return status, json.loads(body_text)
    except json.JSONDecodeError:
        return status, {}


@pytest.fixture
async def server(tmp_path: Path):
    seq = [0]
    srv = ApiServer(
        port=0, output_dir=tmp_path,
        settings=SimpleNamespace(webhook_trigger_secret=SECRET),
        crew_factory=lambda goal, options: _FakeCrew(seq),
    )
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        yield srv, port, tmp_path
    finally:
        for task in list(srv._run_tasks.values()):
            task.cancel()
        await srv.stop()


def _signed_headers(body: bytes, ts: str | None = None) -> dict[str, str]:
    stamp = ts if ts is not None else str(int(time.time()))
    return {
        "X-AgentCrew-Timestamp": stamp,
        "X-AgentCrew-Signature": _sign(stamp, body),
        "Content-Type": "application/json",
    }


async def test_valid_signature_triggers_run(server):
    srv, port, tmp_path = server
    body = json.dumps({"goal": "夜里跑一遍回归"}).encode()
    status, data = await _request(
        port, "/webhooks/trigger", headers=_signed_headers(body), body=body)
    assert status == 202, data
    run_id = data["run_id"]
    # 来源进 ledger：「这次 run 是谁发起的」必须能从审计直接读出来
    events = [event.event for event in
              Ledger(tmp_path / run_id / "ledger.jsonl", run_id=run_id).events()]
    assert "run_triggered" in events
    triggered = [event for event in
                 Ledger(tmp_path / run_id / "ledger.jsonl",
                        run_id=run_id).events()
                 if event.event == "run_triggered"]
    assert triggered[0].data["source"] == "webhook"


async def test_bad_signature_rejected_and_recorded(server):
    srv, port, tmp_path = server
    body = json.dumps({"goal": "不该跑起来"}).encode()
    headers = _signed_headers(body)
    headers["X-AgentCrew-Signature"] = "sha256=" + "0" * 64
    status, data = await _request(
        port, "/webhooks/trigger", headers=headers, body=body)
    assert status == 401
    assert any(event["event"] == "webhook_signature_invalid"
               for event in srv.security_events)
    assert not any(tmp_path.iterdir()), "签名失败不该留下任何 run 目录"


async def test_stale_timestamp_rejected(server):
    srv, port, _ = server
    body = json.dumps({"goal": "重放"}).encode()
    stale = str(int(time.time()) - 3600)
    status, _ = await _request(
        port, "/webhooks/trigger", headers=_signed_headers(body, ts=stale),
        body=body)
    assert status == 401
    assert any(event["data"].get("reason") == "stale_timestamp"
               for event in srv.security_events
               if event["event"] == "webhook_signature_invalid")


async def test_missing_timestamp_rejected(server):
    srv, port, _ = server
    body = json.dumps({"goal": "x"}).encode()
    headers = {"X-AgentCrew-Signature": _sign("", body)}
    status, _ = await _request(port, "/webhooks/trigger",
                               headers=headers, body=body)
    assert status == 401


async def test_disabled_without_secret_returns_503():
    srv = ApiServer(
        port=0, settings=SimpleNamespace(webhook_trigger_secret=""))
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        body = json.dumps({"goal": "x"}).encode()
        status, data = await _request(
            port, "/webhooks/trigger", headers=_signed_headers(body), body=body)
        assert status == 503
        assert "未启用" in data["error"]
    finally:
        await srv.stop()


async def test_no_signature_header_follows_normal_auth_path(tmp_path):
    """认证开启 + 无签名头 + 无 key → 401（与其他路由同形，不给探测者额外信息）。"""
    from agentcrew.api.auth import APIKeyAuth

    seq = [0]
    srv = ApiServer(
        port=0, output_dir=tmp_path,
        settings=SimpleNamespace(webhook_trigger_secret=SECRET),
        crew_factory=lambda goal, options: _FakeCrew(seq),
        auth=APIKeyAuth(api_keys=["contract-key"]),
    )
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        body = json.dumps({"goal": "x"}).encode()
        status, _ = await _request(port, "/webhooks/trigger",
                                   headers={}, body=body)
        assert status == 401
        # 带正确 key（签名路径之外的第二条认证路径）→ 走到业务层
        status, _ = await _request(
            port, "/webhooks/trigger",
            headers={"Authorization": "Bearer contract-key"}, body=body)
        assert status == 202
    finally:
        await srv.stop()


def test_required_scope_is_webhook_trigger():
    assert required_scope("POST", "/webhooks/trigger") == SCOPE_WEBHOOK_TRIGGER


def test_trigger_secret_masked_in_snapshot_and_redacted():
    from agentcrew.config import Settings

    settings = Settings.load(webhook_trigger_secret="leak-me")
    assert settings.config_snapshot()["webhook_trigger_secret"] == "***"
    assert "leak-me" in settings.secret_values()
