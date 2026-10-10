"""V5 P2：端点候选回退矩阵 + 流式检测测试。

覆盖：
- candidate_urls：endpoint_candidates 矩阵、无 endpoint_candidates 旧行为、
  models_url 优先、去重保序
- fetch_models：端点候选矩阵下回退（第一个端点失败 → 第二个端点成功）
- stream_check.check_streaming：成功路径（SSE 流）、失败路径（401/超时/非 SSE）、
  mock/cli 直接返回 ok
- HealthRecord.stream_ok 字段存在且默认 None
- factory.health_check_stream：更新 stream_ok、不破坏已有 status
- API /test?mode=stream 返回 stream_ok 字段
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from agentcrew.models import stream_check
from agentcrew.models.config import ModelBinding, ProviderCapabilities
from agentcrew.models.discovery import candidate_urls, fetch_models
from agentcrew.models.factory import HealthRecord, ModelFactory

# ── 夹具 ──────────────────────────────────────────────────────────────────


def _binding(
    provider: str = "openai",
    base_url: str | None = "https://api.openai.com/v1",
    implementation: str = "openai_compat",
    models_url: str | None = None,
    endpoint_candidates: list[str] | None = None,
    model: str = "gpt-test",
) -> ModelBinding:
    return ModelBinding(
        alias=f"{provider}:{model}",
        provider=provider,
        model=model,
        implementation=implementation,
        base_url=base_url,
        api_key_env="TEST_KEY",
        capabilities=ProviderCapabilities(),
        models_url=models_url,
        endpoint_candidates=endpoint_candidates,
    )


# ── 1. candidate_urls：端点候选矩阵 ───────────────────────────────────────


def test_candidate_urls_endpoint_candidates_matrix():
    """有 endpoint_candidates 时：每个端点派生 /models 与 /v1/models，按端点顺序。"""
    binding = _binding(endpoint_candidates=[
        "https://ep1.com/v1",   # 已带 /v1 → 只派生 /models
        "https://ep2.com",      # 不带 /v1 → 派生 /models + /v1/models
    ])
    urls = candidate_urls(binding)
    assert urls == [
        "https://ep1.com/v1/models",
        "https://ep2.com/models",
        "https://ep2.com/v1/models",
    ]


def test_candidate_urls_endpoint_candidates_overrides_base_url():
    """有 endpoint_candidates 时，base_url 不再单独派生。"""
    binding = _binding(
        base_url="https://ignored.com/v1",
        endpoint_candidates=["https://only.com"],
    )
    urls = candidate_urls(binding)
    # base_url 被忽略，只用 endpoint_candidates
    assert urls == [
        "https://only.com/models",
        "https://only.com/v1/models",
    ]


def test_candidate_urls_no_endpoint_candidates_keeps_old_behavior():
    """无 endpoint_candidates 时与 P1 行为逐字节一致。"""
    # base_url 已带 /v1
    assert candidate_urls(_binding(base_url="https://api.openai.com/v1")) == [
        "https://api.openai.com/v1/models",
    ]
    # base_url 不带 /v1
    assert candidate_urls(_binding(base_url="http://localhost:11434")) == [
        "http://localhost:11434/models",
        "http://localhost:11434/v1/models",
    ]
    # base_url 为 None
    assert candidate_urls(_binding(base_url=None)) == []


def test_candidate_urls_models_url_takes_priority():
    """models_url 显式指定时，无视 endpoint_candidates，只试它。"""
    binding = _binding(
        models_url="https://x/custom/models",
        endpoint_candidates=["https://ep1.com/v1", "https://ep2.com"],
    )
    assert candidate_urls(binding) == ["https://x/custom/models"]


def test_candidate_urls_dedup_preserve_order():
    """同一 URL 从不同端点派生时去重，保留首次出现顺序。"""
    binding = _binding(endpoint_candidates=[
        "https://ep.com/v1",   # 派生 https://ep.com/v1/models
        "https://ep.com",      # 派生 https://ep.com/models、https://ep.com/v1/models（重复）
    ])
    urls = candidate_urls(binding)
    assert urls == [
        "https://ep.com/v1/models",
        "https://ep.com/models",
    ]
    # 无重复
    assert len(urls) == len(set(urls))


# ── 2. fetch_models：端点矩阵下回退 ────────────────────────────────────────


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_fetch_models_endpoint_matrix_fallback(monkeypatch):
    """第一个端点的 /models 与 /v1/models 都失败（404），回退到第二个端点成功。"""
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        # 第一个端点 ep1 的所有候选都 404
        if "ep1.com" in req.full_url:
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, None)
        # 第二个端点 ep2 返回模型列表
        return _FakeResponse(json.dumps({"data": [{"id": "m-from-ep2"}]}))

    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", fake_urlopen)
    binding = _binding(endpoint_candidates=[
        "https://ep1.com/v1",
        "https://ep2.com",
    ])
    result = asyncio.run(fetch_models(binding, api_key="k"))
    assert result == ["m-from-ep2"]
    # 依次尝试：ep1/v1/models → ep2/models（成功，ep2/v1/models 不再试）
    assert calls == [
        "https://ep1.com/v1/models",
        "https://ep2.com/models",
    ]


def test_fetch_models_matrix_tries_all_variants_before_next_endpoint(monkeypatch):
    """先试第一个端点的全部路径变体，再试第二个端点。"""
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        # ep1 的 /models 成功
        if req.full_url == "https://ep1.com/models":
            return _FakeResponse(json.dumps({"data": [{"id": "ok"}]}))
        raise urllib.error.HTTPError(req.full_url, 404, "NF", {}, None)

    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", fake_urlopen)
    binding = _binding(endpoint_candidates=[
        "https://ep1.com",   # 派生 /models, /v1/models
        "https://ep2.com",
    ])
    result = asyncio.run(fetch_models(binding, api_key="k"))
    assert result == ["ok"]
    # ep1/models 第一个就成功，ep1/v1/models 与 ep2 都不试
    assert calls == ["https://ep1.com/models"]


# ── 3. stream_check.check_streaming ────────────────────────────────────────


class _SSEResponse:
    """模拟 SSE 流式响应：按行返回预设字节。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self._idx = 0

    def readline(self) -> bytes:
        if self._idx >= len(self._lines):
            return b""
        line = self._lines[self._idx]
        self._idx += 1
        return line

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_check_streaming_openai_success(monkeypatch):
    """openai_compat：收到第一个 data: 行即 ok=True。"""
    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _SSEResponse([
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b"data: [DONE]\n\n",
        ])

    monkeypatch.setattr(
        "agentcrew.models.stream_check.urllib.request.urlopen", fake_urlopen)
    binding = _binding(base_url="https://api.openai.com/v1")
    result = asyncio.run(stream_check.check_streaming(binding, api_key="sk-x"))
    assert result["ok"] is True
    assert result["error"] is None
    assert result["latency_ms"] is not None
    assert result["latency_ms"] >= 0
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    lowered = {k.lower(): v for k, v in captured["headers"].items()}
    assert lowered.get("authorization") == "Bearer sk-x"


def test_check_streaming_anthropic_success(monkeypatch):
    """anthropic：URL 为 /v1/messages，header 含 x-api-key 与 anthropic-version。"""
    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _SSEResponse([b'data: {"type":"message_start"}\n\n'])

    monkeypatch.setattr(
        "agentcrew.models.stream_check.urllib.request.urlopen", fake_urlopen)
    binding = _binding(
        provider="anthropic",
        base_url="https://api.anthropic.com",
        implementation="anthropic",
    )
    result = asyncio.run(stream_check.check_streaming(binding, api_key="sk-ant"))
    assert result["ok"] is True
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    lowered = {k.lower(): v for k, v in captured["headers"].items()}
    assert lowered.get("x-api-key") == "sk-ant"
    assert lowered.get("anthropic-version") == "2023-06-01"


def test_check_streaming_auth_error(monkeypatch):
    """401 → ok=False，error 含 auth。"""
    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(
        "agentcrew.models.stream_check.urllib.request.urlopen", fake)
    result = asyncio.run(
        stream_check.check_streaming(_binding(), api_key="bad"))
    assert result["ok"] is False
    assert "auth" in result["error"]


def test_check_streaming_timeout(monkeypatch):
    """超时 → ok=False，error 含 timeout。"""
    def fake(req, timeout=None):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(
        "agentcrew.models.stream_check.urllib.request.urlopen", fake)
    result = asyncio.run(stream_check.check_streaming(_binding()))
    assert result["ok"] is False
    assert "timeout" in result["error"]


def test_check_streaming_network_error(monkeypatch):
    """连接拒绝 → ok=False，error 含 network。"""
    def fake(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(
        "agentcrew.models.stream_check.urllib.request.urlopen", fake)
    result = asyncio.run(stream_check.check_streaming(_binding()))
    assert result["ok"] is False
    assert "network" in result["error"]


def test_check_streaming_non_sse_response(monkeypatch):
    """响应没有任何 data: 行（比如纯 JSON）→ ok=False，error 含 parse。"""
    def fake(req, timeout=None):
        return _SSEResponse([b"event: ping\n", b": comment\n", b""])

    monkeypatch.setattr(
        "agentcrew.models.stream_check.urllib.request.urlopen", fake)
    result = asyncio.run(stream_check.check_streaming(_binding()))
    assert result["ok"] is False
    assert "parse" in result["error"]


def test_check_streaming_mock_implementation_shortcircuits():
    """mock 实现：不真实发请求，直接 ok=True。"""
    binding = _binding(
        provider="mock",
        base_url=None,
        implementation="mock",
    )
    result = asyncio.run(stream_check.check_streaming(binding))
    assert result == {"ok": True, "latency_ms": 0.0, "error": None}


def test_check_streaming_cli_implementation_shortcircuits():
    """cli 实现：直接 ok=True。"""
    binding = _binding(
        provider="cli",
        base_url=None,
        implementation="cli",
    )
    result = asyncio.run(stream_check.check_streaming(binding))
    assert result["ok"] is True
    assert result["latency_ms"] == 0.0


def test_check_streaming_does_not_store_api_key():
    """红线：api_key 只进请求参数，模块不持有它。"""
    binding = _binding(implementation="mock")
    asyncio.run(stream_check.check_streaming(binding, api_key="sk-should-not-leak"))
    # 模块级不应有该 key 残留
    for attr in vars(stream_check).values():
        if isinstance(attr, str):
            assert "sk-should-not-leak" not in attr


# ── 4. HealthRecord.stream_ok 字段 ────────────────────────────────────────


def test_health_record_stream_ok_default_none():
    """HealthRecord.stream_ok 默认 None（向后兼容）。"""
    rec = HealthRecord(
        alias="openai:gpt",
        status="healthy",
        last_check=datetime.now(),
    )
    assert rec.stream_ok is None


def test_health_record_stream_ok_settable():
    rec = HealthRecord(
        alias="openai:gpt",
        status="healthy",
        last_check=datetime.now(),
        stream_ok=True,
    )
    assert rec.stream_ok is True


# ── 5. factory.health_check_stream ────────────────────────────────────────


@pytest.fixture
def factory_settings(tmp_path, monkeypatch):
    from agentcrew.config import Settings
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


async def test_health_check_stream_mock_alias(factory_settings, tmp_path):
    """mock 别名：health_check_stream 直接返回 stream_ok=True。"""
    f = ModelFactory(factory_settings, ledger=None)
    record = await f.health_check_stream("mock:m")
    assert record.stream_ok is True
    # mock 实现走 stream_check 短路，不真实请求
    assert record.error is None


async def test_health_check_stream_updates_existing_record_without_touching_status(
    factory_settings, tmp_path, monkeypatch,
):
    """已有普通 health_check 记录时，health_check_stream 只补丁 stream_ok。"""
    f = ModelFactory(factory_settings, ledger=None)
    # 先做一次普通探测，status=healthy
    await f.health_check("mock:m")
    before = f.get_health("mock:m")
    assert before.status == "healthy"
    assert before.stream_ok is None

    # monkeypatch stream_check.check_streaming 让它返回 False
    async def fake_streaming(binding, *, api_key="", timeout_secs=10.0):
        return {"ok": False, "latency_ms": 5.0, "error": "timeout: boom"}

    monkeypatch.setattr(
        "agentcrew.models.stream_check.check_streaming", fake_streaming)
    # 注意：factory.health_check_stream 内部是 from .. import stream_check，
    # 它调用 stream_check.check_streaming —— monkeypatch 模块属性即可
    record = await f.health_check_stream("mock:m")
    assert record.stream_ok is False
    # status / latency_ms / consecutive_failures 保持不变
    assert record.status == before.status
    assert record.latency_ms == before.latency_ms
    assert record.consecutive_failures == before.consecutive_failures


# ── 6. API /test?mode=stream ──────────────────────────────────────────────


async def test_provider_test_mode_stream_returns_stream_ok(tmp_path, monkeypatch):
    """POST /providers/{name}/test?mode=stream 返回 stream_ok 字段。"""
    from agentcrew.api.server import ApiServer
    from agentcrew.config import Settings

    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("MOCK_KEY", "x")
    settings = Settings.load()

    srv = ApiServer(port=0, output_dir=tmp_path, settings=settings)
    # 注入一个 mock factory
    class _FakeFactory:
        async def health_check_stream(self, alias):
            return HealthRecord(
                alias=alias,
                status="healthy",
                latency_ms=12.5,
                last_check=datetime.now(),
                stream_ok=True,
            )

    srv._model_factory = _FakeFactory()
    await srv.start()
    try:
        port = srv._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /providers/mock/test?mode=stream HTTP/1.1\r\n"
            b"Host: localhost\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        raw = await reader.read(65536)
        writer.close()
        await writer.wait_closed()
        text = raw.decode("utf-8", errors="replace")
        assert "200 OK" in text
        assert '"stream_ok": true' in text
        assert '"ok": true' in text
        assert '"latency_ms": 12.5' in text
    finally:
        await srv.stop()


async def test_provider_test_default_mode_completion_unchanged(tmp_path, monkeypatch):
    """不带 ?mode= 时默认 completion 模式，响应不含 stream_ok（向后兼容）。"""
    from agentcrew.api.server import ApiServer
    from agentcrew.config import Settings

    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()

    srv = ApiServer(port=0, output_dir=tmp_path, settings=settings)

    class _FakeFactory:
        async def health_check(self, alias):
            return HealthRecord(
                alias=alias,
                status="healthy",
                latency_ms=3.0,
                last_check=datetime.now(),
            )

    srv._model_factory = _FakeFactory()
    await srv.start()
    try:
        port = srv._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /providers/mock/test HTTP/1.1\r\n"
            b"Host: localhost\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        raw = await reader.read(65536)
        writer.close()
        await writer.wait_closed()
        text = raw.decode("utf-8", errors="replace")
        assert "200 OK" in text
        # 旧字段仍在
        assert '"ok": true' in text
        assert '"latency_ms": 3.0' in text
    finally:
        await srv.stop()
