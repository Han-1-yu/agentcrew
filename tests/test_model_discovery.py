"""A3：模型发现服务测试。

覆盖：
- candidate_urls 从 base_url 派生（含 /v1 后缀判断、models_url 覆写）
- 成功拉取 OpenAI / Anthropic 格式的模型列表
- 错误分类：auth(401/403) / not_found(404) / network / timeout / parse
- URL 候选回退：第一个失败（非 auth/parse），第二个成功
- 结果去重排序
- anthropic 特殊 header
"""

import asyncio
import json
import urllib.error

import pytest

from agentcrew.models.config import ModelBinding, ProviderCapabilities
from agentcrew.models.discovery import (
    DISCOVERY_TIMEOUT_SECS,
    ModelDiscoveryError,
    candidate_urls,
    fetch_models,
)


def _binding(provider="openai", base_url="https://api.openai.com/v1",
             implementation="openai_compat", models_url=None) -> ModelBinding:
    return ModelBinding(
        alias=f"{provider}:test",
        provider=provider,
        model="test",
        implementation=implementation,
        base_url=base_url,
        api_key_env="TEST_KEY",
        capabilities=ProviderCapabilities(),
        models_url=models_url,
    )


# ── candidate_urls ──────────────────────────────────────────────────────


def test_candidate_urls_v1_suffix():
    urls = candidate_urls(_binding(base_url="https://api.openai.com/v1"))
    assert urls == ["https://api.openai.com/v1/models"]


def test_candidate_urls_no_v1_suffix():
    urls = candidate_urls(_binding(base_url="http://localhost:11434"))
    assert urls == [
        "http://localhost:11434/models",
        "http://localhost:11434/v1/models",
    ]


def test_candidate_urls_models_url_override():
    urls = candidate_urls(_binding(models_url="http://x/custom/models"))
    assert urls == ["http://x/custom/models"]


def test_candidate_urls_no_base_url():
    urls = candidate_urls(_binding(base_url=None))
    assert urls == []


# ── fetch_models 成功路径 ───────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_fetch_models_openai_format(monkeypatch):
    payload = json.dumps({"data": [
        {"id": "gpt-4o"},
        {"id": "gpt-4o-mini"},
        {"id": "gpt-4o"},  # 重复
    ]})
    captured_urls: list[str] = []

    def fake_urlopen(req, timeout=None):
        captured_urls.append(req.full_url)
        return _FakeResponse(payload)

    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", fake_urlopen)
    result = asyncio.run(fetch_models(_binding(), api_key="sk-test"))
    assert result == ["gpt-4o", "gpt-4o-mini"]  # 去重排序


def test_fetch_models_anthropic_headers(monkeypatch):
    payload = json.dumps({"data": [{"id": "claude-3.5-sonnet"}]})
    captured_headers: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):
        captured_headers.update(req.headers)
        return _FakeResponse(payload)

    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", fake_urlopen)
    binding = _binding(provider="anthropic",
                       base_url="https://api.anthropic.com",
                       implementation="anthropic")
    result = asyncio.run(fetch_models(binding, api_key="sk-ant"))
    assert result == ["claude-3.5-sonnet"]
    # urllib 的 header 名大小写不确定，用 case-insensitive 查
    lowered = {k.lower(): v for k, v in captured_headers.items()}
    assert lowered.get("x-api-key") == "sk-ant"
    assert lowered.get("anthropic-version") == "2023-06-01"


def test_fetch_models_url_fallback(monkeypatch):
    """第一个 URL 404，回退第二个成功。"""
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/models") and not req.full_url.endswith("/v1/models"):
            # first candidate (no /v1) → 404
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, None)
        return _FakeResponse(json.dumps({"data": [{"id": "m1"}]}))

    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", fake_urlopen)
    binding = _binding(base_url="http://localhost:11434")  # no /v1 suffix
    result = asyncio.run(fetch_models(binding, api_key=""))
    assert result == ["m1"]
    assert len(calls) == 2  # tried both candidates


# ── 错误分类 ─────────────────────────────────────────────────────────────


def _raise_http(status: int):
    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, status, "Err", {}, None)
    return fake


def test_fetch_models_auth_error(monkeypatch):
    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", _raise_http(401))
    with pytest.raises(ModelDiscoveryError) as ei:
        asyncio.run(fetch_models(_binding(), api_key="bad"))
    assert ei.value.kind == "auth"


def test_fetch_models_forbidden_error(monkeypatch):
    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", _raise_http(403))
    with pytest.raises(ModelDiscoveryError) as ei:
        asyncio.run(fetch_models(_binding(), api_key="bad"))
    assert ei.value.kind == "auth"


def test_fetch_models_not_found(monkeypatch):
    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", _raise_http(404))
    with pytest.raises(ModelDiscoveryError) as ei:
        asyncio.run(fetch_models(_binding(), api_key="x"))
    assert ei.value.kind == "not_found"


def test_fetch_models_network_error(monkeypatch):
    def fake(req, timeout=None):
        raise urllib.error.URLError("Connection refused")
    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", fake)
    with pytest.raises(ModelDiscoveryError) as ei:
        asyncio.run(fetch_models(_binding(), api_key="x"))
    assert ei.value.kind == "network"


def test_fetch_models_timeout(monkeypatch):
    def fake(req, timeout=None):
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", fake)
    with pytest.raises(ModelDiscoveryError) as ei:
        asyncio.run(fetch_models(_binding(), api_key="x"))
    assert ei.value.kind == "timeout"


def test_fetch_models_parse_error(monkeypatch):
    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen",
        lambda req, timeout=None: _FakeResponse("not json"))
    with pytest.raises(ModelDiscoveryError) as ei:
        asyncio.run(fetch_models(_binding(), api_key="x"))
    assert ei.value.kind == "parse"


def test_fetch_models_no_base_url():
    binding = _binding(base_url=None)
    with pytest.raises(ModelDiscoveryError) as ei:
        asyncio.run(fetch_models(binding, api_key="x"))
    assert ei.value.kind == "not_found"


def test_timeout_constant():
    assert DISCOVERY_TIMEOUT_SECS == 10.0


# ── 空 id 过滤与残余 parse 分支 ──────────────────────────────────────────


def test_parse_skips_empty_and_blank_ids():
    """回归：空串 / 纯空白的 model id 不进结果列表。"""
    from agentcrew.models.discovery import _parse_models
    names = _parse_models(json.dumps({
        "data": [{"id": "gpt-4o"}, {"id": ""}, {"id": "   "}, {"id": "claude"}]
    }))
    assert names == ["gpt-4o", "claude"]  # 空/空白被滤掉，保输入序


def test_parse_body_not_a_dict():
    from agentcrew.models.discovery import _parse_models
    with pytest.raises(ModelDiscoveryError) as ei:
        _parse_models("[1, 2, 3]")
    assert ei.value.kind == "parse"


def test_parse_data_field_not_a_list():
    from agentcrew.models.discovery import _parse_models
    with pytest.raises(ModelDiscoveryError) as ei:
        _parse_models(json.dumps({"data": {"id": "nope"}}))
    assert ei.value.kind == "parse"


def test_fetch_models_other_http_status_is_network(monkeypatch):
    """5xx 等非 401/403/404 的 HTTP 错误归为 network。"""
    monkeypatch.setattr(
        "agentcrew.models.discovery.urllib.request.urlopen", _raise_http(500))
    with pytest.raises(ModelDiscoveryError) as ei:
        asyncio.run(fetch_models(_binding(), api_key="x"))
    assert ei.value.kind == "network"

