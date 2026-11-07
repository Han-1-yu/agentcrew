"""2026-11-05 第二轮加固回归测试。

钉住四条链的确定性缺陷：

1. ``gate_config`` 在真实 Crew 装配中漏接线；``g3_enabled`` 是死字段；
2. ``path_scope`` 用字符串前缀判定，导致越权写入 / 全量误拒；
3. API 读链路：OSError 逃逸成空连接、大 body 被截断、账本容错、请求体无上限；
4. 配额：异常路径泄漏预扣、check 失败误释放他人预扣、缓存命中被误拒、
   结构化输出整条路径不设防。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentcrew.config import Settings
from agentcrew.crew import crew as crew_module
from agentcrew.gate.gatekeeper import Gatekeeper
from agentcrew.roles.binding import AgentProfile
from agentcrew.utils.files import normalize_path_scope, path_in_scope

_MESSAGES = [{"role": "user", "content": "hi"}]


# ── 1. 门禁配置接线 ───────────────────────────────────────────────────────


def test_gate_config_is_wired_from_settings(monkeypatch):
    """Crew 装配 Gatekeeper 时必须把 gate_config 透传下去。"""
    cfg = {"g1_enabled": False, "g2_reviewers": 2, "convergence_threshold": 3}
    crew = crew_module.Crew(Settings(gate_config=cfg, gate="none"))
    captured: dict = {}

    class _Spy:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(crew_module, "Gatekeeper", _Spy)
    crew._build_gatekeeper(MagicMock(), MagicMock(), None)
    assert captured["gate_config"] == cfg


@pytest.mark.asyncio
async def test_g3_disabled_skips_human_gates_without_prompter():
    """g3_enabled=False 时不应要求 prompter，也不该触碰人工闸门。"""
    gatekeeper = Gatekeeper(
        MagicMock(), MagicMock(), MagicMock(),
        gate="both", ledger=MagicMock(), prompter=None,
        gate_config={"g3_enabled": False},
    )
    assert (await gatekeeper.human_plan_gate([])).action == "approve"
    assert (await gatekeeper.human_delivery_gate(None)).action == "accept"


def test_g3_enabled_still_requires_prompter():
    """默认 g3_enabled=True 的构造期守卫必须原样生效。"""
    with pytest.raises(ValueError, match="prompter"):
        Gatekeeper(
            MagicMock(), MagicMock(), MagicMock(),
            gate="plan", ledger=MagicMock(), prompter=None,
        )


# ── 2. 路径域按路径段判定 ────────────────────────────────────────────────


def test_path_scope_is_segment_aware_not_string_prefix():
    assert path_in_scope("backend/app/main.py", ["backend"])
    assert path_in_scope("backend", ["backend/"])
    assert not path_in_scope("backend_secret/keys.py", ["backend"])
    assert not path_in_scope("frontend/x.py", ["backend/"])


def test_path_scope_normalizes_config_variants():
    assert normalize_path_scope("./backend/") == "backend"
    assert normalize_path_scope("backend\\") == "backend"
    assert path_in_scope("backend/x.py", ["./backend/"])
    assert path_in_scope("backend/x.py", ["backend\\"])


def test_agent_profile_allows_rejects_sibling_prefix():
    profile = AgentProfile.model_construct(path_scope=["backend"])
    assert profile.allows("backend/app/x.py")
    assert not profile.allows("backend_secret/keys.py")


# ── 3. API 读链路 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_large_json_body_split_across_chunks_is_not_truncated():
    """合法大 body 跨 TCP 段到达时不得被截成半截 JSON（误报 400）。"""
    from agentcrew.api.server import ApiServer

    server = ApiServer(port=0)  # 无 settings：/runs 会回 503，但绝不该是 400
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        body = json.dumps({"goal": "x" * 20000}).encode()
        header = (f"POST /runs HTTP/1.1\r\nHost: localhost\r\n"
                  f"Content-Length: {len(body)}\r\n\r\n").encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(header + body[:32])
        await writer.drain()
        await asyncio.sleep(0.02)
        writer.write(body[32:])
        await writer.drain()
        response = await reader.read(8192)
        writer.close()
        await writer.wait_closed()
        assert b"invalid JSON" not in response
        assert b"400 Bad Request" not in response
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_oversized_body_is_rejected_without_waiting():
    from agentcrew.api.server import ApiServer

    server = ApiServer(port=0)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST /runs HTTP/1.1\r\nHost: localhost\r\n"
                     b"Content-Length: 99999999\r\n\r\n")
        await writer.drain()
        response = await reader.read(4096)
        writer.close()
        await writer.wait_closed()
        assert b"413" in response
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_ledger_oserror_returns_500_not_empty_connection(tmp_path, monkeypatch):
    """读端点里的 OSError 必须变成 HTTP 响应，而不是让连接空着关闭。"""
    from agentcrew.api.server import ApiServer
    from agentcrew.runtime.ledger import LedgerReader

    output = tmp_path / "o"
    (output / "R").mkdir(parents=True)

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(LedgerReader, "query_events", denied)
    server = ApiServer(port=0, output_dir=output)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /runs/R/ledger HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await reader.read(4096)
        writer.close()
        await writer.wait_closed()
        assert b"500" in response
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unexpected_handler_error_returns_500(monkeypatch):
    """顶层兜底：任何未预料异常都要给客户端一个 HTTP 响应。"""
    from agentcrew.api.server import ApiServer

    async def boom(self, path, writer):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ApiServer, "_handle_get", boom)
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
        assert b"500" in response
    finally:
        await server.stop()


def test_ledger_unreadable_path_reads_as_empty(tmp_path: Path):
    """ledger.jsonl 被误建成目录时按零事件处理，不让整条读链路崩掉。"""
    from agentcrew.runtime.ledger import Ledger

    path = tmp_path / "ledger.jsonl"
    path.mkdir()
    assert list(Ledger(path, run_id="R").events()) == []


# ── 4. 配额预扣 ──────────────────────────────────────────────────────────


def _factory(**settings_kw):
    from agentcrew.models.factory import ModelFactory
    return ModelFactory(Settings(mock=True, **settings_kw))


@pytest.mark.asyncio
async def test_reservation_released_on_unexpected_exception(monkeypatch):
    """非白名单异常（含普通 RuntimeError）也必须归还预扣。"""
    factory = _factory(quotas={"mock:m": {"daily": {"max_calls": 10}}})
    provider = factory.get("mock:m")

    async def boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(provider, "complete", boom)
    with pytest.raises(RuntimeError, match="exploded"):
        await factory._attempt(
            "mock:m", _MESSAGES, temperature=0.0, max_tokens=50,
            purpose="t", role="t", use_cache=False)
    reserved = factory._quota_tracker._get_reserved("mock:m", "daily")
    assert reserved.calls == 0
    assert reserved.tokens == 0


@pytest.mark.asyncio
async def test_check_failure_does_not_release_others_reservation():
    """check 失败时本次从未预扣，绝不能释放并发请求的预扣（否则超卖）。"""
    from agentcrew.models.quota import QuotaExhaustedError

    factory = _factory(quotas={"mock:m": {"daily": {"max_calls": 1}}})
    tracker = factory._quota_tracker
    tracker.reserve("mock:m", 10)  # 模拟另一并发请求已预扣

    with pytest.raises(QuotaExhaustedError):
        await factory._attempt(
            "mock:m", _MESSAGES, temperature=0.0, max_tokens=50,
            purpose="t", role="t", use_cache=False)
    assert tracker._get_reserved("mock:m", "daily").calls == 1


@pytest.mark.asyncio
async def test_cache_hit_never_calls_quota_check(monkeypatch):
    """缓存命中零成本；配额刚耗尽也不该把可复用的结果误拒。"""
    from agentcrew.models.cache import LLMCache

    factory = _factory()
    factory._resp_cache = LLMCache()
    kwargs = dict(temperature=0.0, max_tokens=50, purpose="t", role="t")
    await factory._attempt("mock:m", _MESSAGES, **kwargs)  # 填缓存

    def forbidden(alias):
        raise AssertionError("缓存命中时不应调用配额 check")

    monkeypatch.setattr(factory._quota_tracker, "check", forbidden)
    result = await factory._attempt("mock:m", _MESSAGES, **kwargs)
    assert result is not None


@pytest.mark.asyncio
async def test_structured_path_now_checks_quota():
    """PM 拆解 / G2 评审走的结构化路径也受配额约束（此前完全失效）。"""
    from pydantic import BaseModel

    from agentcrew.models.quota import QuotaExhaustedError

    class _Out(BaseModel):
        ok: bool

    factory = _factory(quotas={"mock:m": {"daily": {"max_calls": 0}}})
    with pytest.raises(QuotaExhaustedError):
        await factory._structured_attempt(
            "mock:m", _MESSAGES, _Out, temperature=0.0, max_tokens=50,
            purpose="t", role="t", repair_attempts=0, on_degrade=None,
            use_cache=False)
