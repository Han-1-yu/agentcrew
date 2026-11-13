"""V6 P1：模型元数据暴露与成本估算增强测试。

覆盖：
- GET /providers 响应包含价格/窗口/并发/RPM 字段；
- 29 个内置预设全部能加载，且新字段有合理值；
- _account 在 binding 给出 preset 价格时优先用 preset 价格。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentcrew.api.server import ApiServer
from agentcrew.config import Settings
from agentcrew.models.config import (
    PROVIDER_PRESETS,
    ModelBinding,
    ProviderCapabilities,
    parse_alias,
)
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import CompletionResult


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


# ── 1. 全部 29 个预设正常加载 ────────────────────────────────────────────

def test_all_presets_load():
    """29 个内置预设全部能被 pydantic 校验通过，新字段有默认值。"""
    assert len(PROVIDER_PRESETS) == 29
    for name, preset in PROVIDER_PRESETS.items():
        # 新字段必须存在（即使是 None / 空列表）
        assert hasattr(preset, "input_price"), f"{name} 缺 input_price"
        assert hasattr(preset, "output_price"), f"{name} 缺 output_price"
        assert hasattr(preset, "context_window"), f"{name} 缺 context_window"
        assert hasattr(preset, "fallback_models"), f"{name} 缺 fallback_models"
        assert hasattr(preset, "max_concurrency"), f"{name} 缺 max_concurrency"
        assert hasattr(preset, "max_rpm"), f"{name} 缺 max_rpm"
        assert isinstance(preset.fallback_models, list)


def test_preset_metadata_known_values():
    """抽查几个关键预设的价格/窗口与任务书一致。"""
    ds = PROVIDER_PRESETS["deepseek"]
    assert ds.input_price == 0.27
    assert ds.output_price == 1.10
    assert ds.context_window == 64_000

    m = PROVIDER_PRESETS["mock"]
    assert m.input_price == 0.0
    assert m.output_price == 0.0
    assert m.context_window == 128_000
    assert m.max_output_tokens == 8_192

    g = PROVIDER_PRESETS["gemini"]
    assert g.context_window == 1_000_000
    assert g.input_price == 1.25

    sf = PROVIDER_PRESETS["siliconflow"]
    assert sf.fallback_models == ["deepseek-ai/DeepSeek-V4"]
    assert sf.context_window == 32_768


def test_parse_alias_copies_new_fields():
    """parse_alias 应把 preset 的新字段拷进 ModelBinding。"""
    b = parse_alias("deepseek:deepseek-chat")
    assert b.input_price == 0.27
    assert b.output_price == 1.10
    assert b.context_window == 64_000
    assert b.fallback_models == ["deepseek-chat", "deepseek-v4-flash"]


# ── 2. GET /providers API 暴露元数据 ────────────────────────────────────

@pytest.mark.asyncio
async def test_providers_api_includes_metadata(settings):
    """GET /providers 的 entries 应包含价格/窗口/并发/RPM 字段。"""
    server = ApiServer(port=0, settings=settings)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /providers HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await writer.drain()
        chunks = []
        while True:
            line = await reader.read(8192)
            if not line:
                break
            chunks.append(line)
        writer.close()
        await writer.wait_closed()
        raw = b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        await server.stop()

    # 取 HTTP body（\r\n\r\n 之后）
    body = raw.split("\r\n\r\n", 1)[1]
    data = json.loads(body)
    providers = {p["name"]: p for p in data["providers"]}
    assert "deepseek" in providers
    ds = providers["deepseek"]
    # 新字段必须出现
    for field in (
        "input_price", "output_price", "context_window",
        "max_output_tokens", "fallback_models",
        "max_concurrency", "max_rpm",
    ):
        assert field in ds, f"GET /providers 缺字段 {field}"
    assert ds["input_price"] == 0.27
    assert ds["output_price"] == 1.10
    assert ds["context_window"] == 64_000
    assert ds["fallback_models"] == ["deepseek-chat", "deepseek-v4-flash"]


# ── 3. 成本估算优先用 preset 价格 ─────────────────────────────────────────

def _make_binding(provider: str, model: str,
                   input_price: float | None,
                   output_price: float | None) -> ModelBinding:
    return ModelBinding(
        alias=f"{provider}:{model}",
        provider=provider,
        model=model,
        implementation="openai_compat",
        base_url=None,
        api_key_env="X",
        capabilities=ProviderCapabilities(),
        input_price=input_price,
        output_price=output_price,
    )


def test_cost_uses_preset_pricing(settings):
    """binding 同时给出 input/output_price 时，成本应按 preset 价计算。"""
    f = ModelFactory(settings)
    binding = _make_binding("myprov", "my-model", 2.00, 8.00)
    result = CompletionResult(
        text="x", provider="myprov", model="my-model",
        prompt_tokens=1_000_000, completion_tokens=1_000_000, latency_ms=1)
    f._account(result, purpose="execute", role="r", binding=binding)
    # 1M in * 2.00 + 1M out * 8.00 = 10.0 USD
    assert f.total_cost_usd == pytest.approx(10.0)


def test_cost_falls_back_to_pricing_table_when_binding_missing(settings):
    """binding 为 None（或价格为 None）时回退到 MODEL_PRICING 表。"""
    f = ModelFactory(settings)
    # gpt-4o-mini 在 MODEL_PRICING 里 = (0.15, 0.60)
    result = CompletionResult(
        text="x", provider="openai", model="gpt-4o-mini",
        prompt_tokens=1_000_000, completion_tokens=1_000_000, latency_ms=1)
    f._account(result, purpose="execute", role="r", binding=None)
    # 1M * 0.15 + 1M * 0.60 = 0.75 USD
    assert f.total_cost_usd == pytest.approx(0.75)


def test_cost_falls_back_when_binding_price_partial(settings):
    """binding 只给一个价格（None）时仍回退到表（不partial 使用）。"""
    f = ModelFactory(settings)
    binding = _make_binding("myprov", "gpt-4o-mini", None, 0.60)
    result = CompletionResult(
        text="x", provider="myprov", model="gpt-4o-mini",
        prompt_tokens=1_000_000, completion_tokens=1_000_000, latency_ms=1)
    f._account(result, purpose="execute", role="r", binding=binding)
    # 两个价格必须同时非 None 才用 preset；这里 input_price=None → 回退表
    # gpt-4o-mini = (0.15, 0.60) → 0.75
    assert f.total_cost_usd == pytest.approx(0.75)
