"""API 认证与速率限制中间件的测试（E-3）。

覆盖：
- :class:`~agentcrew.api.auth.APIKeyAuth` 的有效 / 无效 / 未启用路径；
- :class:`~agentcrew.api.auth.RateLimiter` 的窗口计数 / 超限 / 滑动窗口过期；
- :class:`~agentcrew.api.server.ApiServer` 集成：auth=None 时完全向后兼容、
  启用认证后 401/200、启用限流后 429 + ``Retry-After``，以及两类被拒事件
  都写进 ``server.security_events``（且绝不携带 key 值）；
- ``Settings`` 的新字段经环境变量 / 直接构造正确加载。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from agentcrew.api.auth import APIKeyAuth, RateLimiter
from agentcrew.api.server import ApiServer
from agentcrew.config import Settings

# ── APIKeyAuth 单元测试 ────────────────────────────────────────────────────


class TestAPIKeyAuth:
    def test_valid_key_via_bearer(self):
        auth = APIKeyAuth(["secret-1", "secret-2"])
        assert auth.enabled is True
        assert auth.authenticate({"authorization": "Bearer secret-1"}) is True

    def test_valid_key_via_x_api_key(self):
        auth = APIKeyAuth(["secret-1"])
        assert auth.authenticate({"x-api-key": "secret-1"}) is True

    def test_invalid_key_rejected(self):
        auth = APIKeyAuth(["secret-1"])
        assert auth.authenticate({"authorization": "Bearer wrong"}) is False
        assert auth.authenticate({"x-api-key": "wrong"}) is False

    def test_missing_headers_rejected(self):
        auth = APIKeyAuth(["secret-1"])
        assert auth.authenticate({}) is False
        # Bearer 前缀后为空串不算有效 key
        assert auth.authenticate({"authorization": "Bearer "}) is False

    def test_empty_list_disables_auth(self):
        auth = APIKeyAuth([])
        assert auth.enabled is False
        # 未启用时一律放行（向后兼容）
        assert auth.authenticate({}) is True
        assert auth.authenticate({"x-api-key": "anything"}) is True

    def test_blank_keys_are_ignored(self):
        auth = APIKeyAuth(["", "   ", "real"])
        assert auth.enabled is True
        assert auth.authenticate({"x-api-key": "real"}) is True
        assert auth.authenticate({"x-api-key": ""}) is False


# ── RateLimiter 单元测试 ────────────────────────────────────────────────────


class TestRateLimiter:
    def test_allows_up_to_max_then_blocks(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        assert limiter.enabled is True
        results = [limiter.check("ip-a") for _ in range(5)]
        assert [allowed for allowed, _ in results] == [True, True, True, False, False]
        # 剩余次数随每次允许递减
        assert [remaining for _, remaining in results[:3]] == [2, 1, 0]

    def test_zero_max_disables(self):
        limiter = RateLimiter(max_requests=0, window_seconds=60)
        assert limiter.enabled is False
        allowed, remaining = limiter.check("ip-a")
        assert allowed is True
        assert remaining == 0

    def test_per_ip_isolation(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        assert limiter.check("ip-1")[0] is True
        assert limiter.check("ip-1")[0] is False
        # 另一个 IP 不受影响
        assert limiter.check("ip-2")[0] is True

    def test_sliding_window_expires_and_recovers(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        # 塞两条「2 分钟前」的过期记录，窗口应把它们滑出去
        old = datetime.now() - timedelta(seconds=120)
        limiter._hits["ip-x"] = [old, old]
        allowed, remaining = limiter.check("ip-x")
        assert allowed is True
        # 过期记录被清掉，本次新记 1 条 → 上限 2 剩 1
        assert remaining == 1

    def test_window_seconds_exposed_for_retry_after(self):
        assert RateLimiter(max_requests=10, window_seconds=45).window_seconds == 45


# ── ApiServer 集成 ─────────────────────────────────────────────────────────


async def _raw(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    """发裸请求，返回 ``(状态码, 完整响应头原文)``。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    for name, value in (headers or {}).items():
        request += f"{name}: {value}\r\n"
    request += "\r\n"
    writer.write(request.encode())
    await writer.drain()
    raw = await reader.read(65536)
    writer.close()
    await writer.wait_closed()
    head = raw.decode("utf-8", errors="replace").split("\r\n\r\n", 1)[0]
    status = int(head.split("\r\n")[0].split(" ")[1])
    return status, head


class TestServerBackwardCompat:
    @pytest.mark.asyncio
    async def test_no_middleware_is_unchanged(self):
        """auth=None 且 rate_limiter=None 时，现有端点行为完全不变。"""
        server = ApiServer(port=0)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            status, head = await _raw(port, "GET", "/health")
            assert status == 200
            assert "security_events" not in server.__dict__ or server.security_events == []
        finally:
            await server.stop()


class TestServerAuth:
    @pytest.fixture
    async def server(self):
        srv = ApiServer(port=0, auth=APIKeyAuth(["top-secret"]))
        await srv.start()
        try:
            yield srv
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_missing_key_returns_401(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, head = await _raw(port, "GET", "/health")
        assert status == 401
        assert "WWW-Authenticate" in head

    @pytest.mark.asyncio
    async def test_wrong_key_returns_401(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, _ = await _raw(
            port, "GET", "/health", {"Authorization": "Bearer nope"})
        assert status == 401

    @pytest.mark.asyncio
    async def test_valid_bearer_key_allowed(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, _ = await _raw(
            port, "GET", "/health", {"Authorization": "Bearer top-secret"})
        assert status == 200

    @pytest.mark.asyncio
    async def test_valid_x_api_key_allowed(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, _ = await _raw(port, "GET", "/health", {"X-API-Key": "top-secret"})
        assert status == 200

    @pytest.mark.asyncio
    async def test_auth_failure_recorded_without_key_value(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        await _raw(port, "GET", "/health", {"Authorization": "Bearer nope"})
        events = [e for e in server.security_events if e["event"] == "api_auth_failed"]
        assert len(events) == 1
        data = events[0]["data"]
        assert data["path"] == "/health"
        assert data["client_ip"]  # 127.0.0.1
        # 安全红线：被拒的 key 值绝不进审计记录
        assert "nope" not in json.dumps(server.security_events)


class TestServerRateLimit:
    @pytest.mark.asyncio
    async def test_exceed_limit_returns_429_with_retry_after(self):
        limiter = RateLimiter(max_requests=2, window_seconds=30)
        server = ApiServer(port=0, rate_limiter=limiter)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            s1, _ = await _raw(port, "GET", "/health")
            s2, _ = await _raw(port, "GET", "/health")
            s3, head3 = await _raw(port, "GET", "/health")
            assert (s1, s2) == (200, 200)
            assert s3 == 429
            assert "Retry-After: 30" in head3
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_rate_limit_event_recorded(self):
        limiter = RateLimiter(max_requests=1, window_seconds=30)
        server = ApiServer(port=0, rate_limiter=limiter)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            await _raw(port, "GET", "/health")  # 占掉唯一配额
            status, _ = await _raw(port, "GET", "/health")
            assert status == 429
            events = [
                e for e in server.security_events
                if e["event"] == "api_rate_limited"]
            assert len(events) == 1
            data = events[0]["data"]
            assert data["path"] == "/health"
            assert data["remaining"] == 0
            assert data["client_ip"]
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_limit_checks_x_forwarded_for_ip(self):
        """``X-Forwarded-For`` 优先作为客户端 IP（反代场景）。"""
        limiter = RateLimiter(max_requests=1, window_seconds=30)
        server = ApiServer(port=0, rate_limiter=limiter)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            s1, _ = await _raw(
                port, "GET", "/health", {"X-Forwarded-For": "203.0.113.7"})
            s2, _ = await _raw(
                port, "GET", "/health", {"X-Forwarded-For": "203.0.113.7"})
            assert s1 == 200
            assert s2 == 429
            event = server.security_events[-1]["data"]
            assert event["client_ip"] == "203.0.113.7"
        finally:
            await server.stop()


# ── Settings 字段加载 ─────────────────────────────────────────────────────


class TestConfigFields:
    def test_defaults(self, monkeypatch):
        """P0：认证默认**开启**（此前默认关 = 忘了配就裸奔）。"""
        s = Settings.load()
        assert s.api_auth_enabled is True
        assert s.api_keys == []
        assert s.api_rate_limit_enabled is False
        assert s.api_rate_limit_max == 100
        assert s.api_rate_limit_window == 60

    def test_env_api_keys_comma_separated(self, monkeypatch):
        monkeypatch.setenv("AGENTCREW_API_KEYS", " k1 , k2 ,,k3 ")
        assert Settings.load().api_keys == ["k1", "k2", "k3"]

    def test_env_auth_and_rate_limit_flags(self, monkeypatch):
        monkeypatch.setenv("AGENTCREW_API_AUTH_ENABLED", "true")
        monkeypatch.setenv("AGENTCREW_API_RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("AGENTCREW_API_RATE_LIMIT_MAX", "42")
        s = Settings.load()
        assert s.api_auth_enabled is True
        assert s.api_rate_limit_enabled is True
        assert s.api_rate_limit_max == 42

    def test_api_keys_merge_across_sources_dedup(self, monkeypatch):
        """env 与 CLI 的 api_keys 按序合并去重（_MAPPING_FIELDS 列表型合并）。"""
        monkeypatch.setenv("AGENTCREW_API_KEYS", "a,b")
        s = Settings.load(api_keys=["b", "c"])
        assert s.api_keys == ["a", "b", "c"]
