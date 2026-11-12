"""2026-11-10 P0 加固回归（对应 docs/GAP-ANALYSIS-2026-11-10.md 批次 1）。

覆盖六组行为，不锁实现细节：

1. Webhook SSRF 收口（P0-1/B-7）：``POST /runs`` options 不再接受 ``webhook_url``；
   重试 / 超时有上界；``WebhookConfig`` 对超大值 clamp。
2. SSE 帧注入（P0-3）：事件名白名单，带换行的名字不可能拼进 ``event:`` 行；
   字符串型 data 按行拆分多条 ``data:``。
3. 错误响应脱敏（P0-5）：500 响应体不含异常详情（类型名以外的内容）。
4. XFF 信任策略（P0-4）：无可信代理时 XFF 被忽略；可信时取最右侧不可信跳；
   ``Settings.trusted_proxies`` 拒绝坏值。
5. 事件注入端点（P0-2）：``POST /runs/{id}/events`` 的 ``_event`` 白名单与
   ``run_id`` 强制覆盖。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agentcrew.api import http as http_transport
from agentcrew.api.run_options import validate_run_options
from agentcrew.config import Settings
from agentcrew.runtime.webhook import WebhookConfig

# ── 1. Webhook SSRF 收口 ─────────────────────────────────────────────────


class TestWebhookOptionsHardening:
    def test_webhook_url_is_not_an_allowed_run_option(self):
        """P0-1：接收端地址属于服务端配置——从 options 白名单移除后，
        任意 ``runs:write`` key 不能再让服务端向任意地址（含云元数据 /
        内网）发出带 HMAC 签名的 POST。"""
        error = validate_run_options({
            "webhook_url": "http://169.254.169.254/latest/meta-data"})
        assert error is not None
        assert "不支持的键" in error
        assert "webhook_url" in error

    def test_webhook_url_via_full_injection_shape_is_rejected(self):
        """完整的攻击载荷（URL + 全事件 + 大重试）在入口就被拒。"""
        error = validate_run_options({
            "webhook_url": "https://attacker.example/collect",
            "webhook_events": ["*"],
            "webhook_max_retries": 3,
        })
        assert error is not None

    def test_webhook_max_retries_upper_bound(self):
        assert validate_run_options({"webhook_max_retries": 5}) is None
        error = validate_run_options({"webhook_max_retries": 6})
        assert error is not None and "<= 5" in error

    def test_webhook_timeout_upper_bound(self):
        assert validate_run_options({"webhook_timeout_seconds": 30}) is None
        error = validate_run_options({"webhook_timeout_seconds": 31})
        assert error is not None and "<= 30" in error

    def test_webhook_behavior_options_still_valid(self):
        """行为参数（events/timeout/retries）仍在白名单内——它们只作用于
        服务端配置的接收端，不构成出站目标选择。"""
        assert validate_run_options({
            "webhook_events": ["run_finished"],
            "webhook_timeout_seconds": 10,
            "webhook_max_retries": 2,
        }) is None


class TestWebhookConfigClamp:
    def test_oversized_timeout_is_clamped(self):
        cfg = WebhookConfig(url="http://example.test/hook",
                            timeout_seconds=3600.0)
        assert cfg.timeout_seconds <= 30.0

    def test_oversized_retries_is_clamped(self):
        cfg = WebhookConfig(url="http://example.test/hook", max_retries=1000000)
        assert cfg.max_retries <= 5

    def test_normal_values_are_untouched(self):
        cfg = WebhookConfig(url="http://example.test/hook",
                            timeout_seconds=5.0, max_retries=2)
        assert cfg.timeout_seconds == 5.0
        assert cfg.max_retries == 2


# ── 2. SSE 帧注入 ────────────────────────────────────────────────────────


class TestSseFrameHardening:
    def test_valid_event_name_passes(self):
        assert http_transport.valid_event_name("run_started") is True
        assert http_transport.valid_event_name("task.transition-1") is True
        assert http_transport.valid_event_name("custom") is True

    def test_invalid_event_names_are_rejected(self):
        for bad in ("", "a b", "a\nb", "a\rb", "héllo", "a" * 65, 123, None):
            assert http_transport.valid_event_name(bad) is False

    def test_injected_event_cannot_forge_frames(self):
        """带换行的 event 只能变成一个降级的 ``custom`` 帧，无法闭合 /
        追加任意 ``event:`` / ``data:`` 行。"""
        payload = "x\ndata: {\"status\":\"completed\"}\n\nevent: y"
        frame = http_transport.sse_frame(payload, {"status": "running"})
        lines = frame.split("\n")
        # 第一行必须是唯一且合法的 event 行
        assert lines[0] == "event: custom"
        event_lines = [ln for ln in lines if ln.startswith("event:")]
        assert event_lines == ["event: custom"]
        # data 只有一行，且注入串没有逃进任何 data 行
        data_lines = [ln for ln in lines if ln.startswith("data:")]
        assert len(data_lines) == 1
        assert "event: y" not in frame.replace("event: custom", "")

    def test_multiline_string_data_is_split_per_line(self):
        """SSE 规范：data 的每一行都要单独 ``data:`` 前缀。"""
        frame = http_transport.sse_frame("run_note", "第一行\n第二行")
        assert "data: 第一行\n" in frame
        assert "data: 第二行\n" in frame

    def test_json_data_frame_is_byte_compatible_with_legacy(self):
        """JSON 序列化无裸换行——帧格式与旧实现逐字节一致（向后兼容）。"""
        frame = http_transport.sse_frame("run_started", {"run_id": "R-1"})
        assert frame == 'event: run_started\ndata: {"run_id": "R-1"}\n\n'

    def test_crlf_in_string_data_is_normalized(self):
        frame = http_transport.sse_frame("note", "a\r\nb\rc")
        assert "\r" not in frame


# ── 3. 错误响应脱敏 ──────────────────────────────────────────────────────


class TestInternalErrorRedaction:
    def _server(self):
        from agentcrew.api.server import ApiServer

        return ApiServer(port=0)

    def test_body_contains_type_name_but_not_detail(self):
        server = self._server()
        exc = OSError("C:\\Users\\secret\\config.json 拒绝访问")
        body = server._internal_error_body(exc)
        assert "OSError" in body["error"]
        assert "secret" not in body["error"]
        assert "config.json" not in body["error"]

    def test_detail_is_redacted_for_logging(self):
        server = self._server()
        exc = ValueError("key sk-abcdef123456 rejected")
        detail = server._internal_detail(exc)
        assert "sk-abcdef123456" not in detail


# ── 4. XFF 信任策略 ──────────────────────────────────────────────────────


def _writer(peer: str):
    return SimpleNamespace(
        get_extra_info=lambda name: (peer, 12345) if name == "peername" else None)


class TestClientIpTrustPolicy:
    def test_xff_ignored_without_trusted_proxies(self):
        """直连（无可信代理）：伪造任意 XFF 都不改变 client_ip。"""
        ip = http_transport.client_ip(
            {"x-forwarded-for": "203.0.113.7"}, _writer("192.168.1.10"))
        assert ip == "192.168.1.10"

    def test_xff_used_when_peer_is_trusted(self):
        import ipaddress

        trusted = (ipaddress.ip_network("192.168.1.0/24"),)
        ip = http_transport.client_ip(
            {"x-forwarded-for": "203.0.113.7"}, _writer("192.168.1.10"),
            trusted_proxies=trusted)
        assert ip == "203.0.113.7"

    def test_rightmost_untrusted_hop_wins(self):
        """XFF 里伪造的中间跳不能冒充真实客户端。"""
        import ipaddress

        trusted = (ipaddress.ip_network("10.0.0.0/8"),)
        ip = http_transport.client_ip(
            {"x-forwarded-for": "203.0.113.7, 10.0.0.9, 10.0.0.8"},
            _writer("10.0.0.1"), trusted_proxies=trusted)
        assert ip == "203.0.113.7"

    def test_all_trusted_hops_fall_back_to_leftmost(self):
        import ipaddress

        trusted = (ipaddress.ip_network("10.0.0.0/8"),)
        ip = http_transport.client_ip(
            {"x-forwarded-for": "10.0.0.9, 10.0.0.8"},
            _writer("10.0.0.1"), trusted_proxies=trusted)
        assert ip == "10.0.0.9"

    def test_garbage_xff_from_trusted_peer_falls_back_to_peer(self):
        import ipaddress

        trusted = (ipaddress.ip_network("127.0.0.0/8"),)
        ip = http_transport.client_ip(
            {"x-forwarded-for": "not-an-ip"}, _writer("127.0.0.1"),
            trusted_proxies=trusted)
        # 非法跳不可信 → 但也解析不出结果，回落到 peer
        assert ip == "127.0.0.1"


class TestTrustedProxiesSettings:
    def test_valid_networks_load(self):
        s = Settings.load().model_copy(
            update={"trusted_proxies": ["10.0.0.0/8", "192.168.1.5"]})
        assert s.trusted_proxies == ["10.0.0.0/8", "192.168.1.5"]

    def test_bad_value_is_rejected_at_load(self):
        # 直接构造走 pydantic 校验（model_copy 会绕过 validator）；
        # pydantic ValidationError 是 ValueError 的子类。
        with pytest.raises(ValueError):
            Settings(trusted_proxies=["not-a-network"])

    def test_env_comma_separated_is_split(self, monkeypatch):
        monkeypatch.setenv("AGENTCREW_TRUSTED_PROXIES", "10.0.0.0/8, 127.0.0.1")
        s = Settings.load()
        assert s.trusted_proxies == ["10.0.0.0/8", "127.0.0.1"]


# ── 5. 事件注入端点（P0-2/P0-3 入口层）──────────────────────────────────


async def _post_json(
    port: int, path: str, payload: dict | None,
) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps(payload or {}).encode("utf-8")
    request = (
        f"POST {path} HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    writer.write(request)
    await writer.drain()
    raw = await reader.read(65536)
    writer.close()
    await writer.wait_closed()
    head, _, rest = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n")[0].split(b" ")[1])
    try:
        data = json.loads(rest) if rest else {}
    except json.JSONDecodeError:
        data = {}
    return status, data


class TestEventsEndpointHardening:
    @pytest.mark.asyncio
    async def test_newline_event_name_is_rejected_with_400(self):
        from agentcrew.api.server import ApiServer

        server = ApiServer(port=0)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            status, data = await _post_json(port, "/runs/R-1/events", {
                "_event": "x\ndata: {\"status\":\"completed\"}\n\nevent: y"})
            assert status == 400
            assert "事件名" in data.get("error", "")
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_run_id_is_force_overwritten(self):
        """body 自带的 run_id 不能把事件记到别的 run 名下。"""
        from agentcrew.api.server import ApiServer

        received: list[tuple[str, dict]] = []

        def on_event(event: str, data: dict) -> None:
            received.append((event, data))

        server = ApiServer(port=0, event_bus=None)
        server.on_event = on_event
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            status, _ = await _post_json(port, "/runs/R-1/events", {
                "run_id": "R-OTHER", "_event": "custom", "k": 1})
            assert status == 202
            assert received, "事件应当到达 on_event"
            event, data = received[-1]
            assert event == "custom"
            assert data["run_id"] == "R-1"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_valid_custom_event_still_accepted(self):
        from agentcrew.api.server import ApiServer

        server = ApiServer(port=0)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            status, _ = await _post_json(port, "/runs/R-1/events", {
                "_event": "custom", "note": "hello"})
            assert status == 202
        finally:
            await server.stop()


# ── 6. 出站重定向（P0-1 传输层）─────────────────────────────────────────


class TestWebhookNoRedirect:
    def test_dispatcher_opener_is_shared_and_redirect_free(self):
        """投递用 opener 不跟随重定向：可信公网端点被劫持后 302 到任意地址
        也不会带着签名头发过去。"""
        from urllib.request import HTTPRedirectHandler

        import agentcrew.runtime.webhook as webhook_module

        opener = webhook_module._OPENER
        assert opener is not None
        redirect_handlers = [
            h for h in opener.handlers if isinstance(h, HTTPRedirectHandler)]
        assert redirect_handlers, "应当注册 _NoRedirect"
        # 唯一的 redirect handler 是我们自己的 _NoRedirect（redirect_request 恒 None）
        for h in redirect_handlers:
            assert h.redirect_request(object(), None, 302, "", {}, "") is None
