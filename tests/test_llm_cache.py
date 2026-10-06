"""P3-2：LLM 响应缓存的测试。

用一个计数假 Provider 钉住「第二次相同输入是否还真的打了 Provider」，并断言
缓存命中的留痕与用量口径（不消耗 token / 成本）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from agentcrew.config import Settings
from agentcrew.models.cache import LLMCache
from agentcrew.models.config import ModelBinding, ProviderCapabilities
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.runtime.ledger import Ledger


class _CountingProvider(BaseProvider):
    """每次 ``complete`` 计数并返回固定结果的假 Provider。"""

    def __init__(self, text: str = "固定回复") -> None:
        super().__init__(ModelBinding(
            alias="mock:m", provider="mock", model="m",
            base_url=None, api_key_env="X",
            capabilities=ProviderCapabilities(max_context_tokens=128_000)))
        self.complete_calls = 0
        self._text = text

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        self.complete_calls += 1
        return CompletionResult(
            text=self._text, provider="mock", model="m",
            prompt_tokens=10, completion_tokens=5, latency_ms=1,
        )


@pytest.fixture
def factory(tmp_path, monkeypatch) -> ModelFactory:
    s = Settings.load().model_copy(update={"mock": True})
    f = ModelFactory(s, ledger=Ledger(tmp_path / "l.jsonl", run_id="R"))
    return f


async def _call(f: ModelFactory, content: str, temperature: float = 0.0):
    return await f.call(
        "mock:m", [{"role": "user", "content": content}],
        temperature=temperature, max_tokens=16, purpose="execute", role="r")


class TestLLMCacheUnit:
    def test_make_key_stable_and_sensitive(self):
        msgs = [{"role": "user", "content": "hi"}]
        k1 = LLMCache._make_key("a", msgs, 0.0, 16)
        k2 = LLMCache._make_key("a", msgs, 0.0, 16)
        assert k1 == k2
        assert LLMCache._make_key("b", msgs, 0.0, 16) != k1
        assert LLMCache._make_key("a", [{"role": "user", "content": "yo"}],
                                  0.0, 16) != k1
        assert LLMCache._make_key("a", msgs, 0.5, 16) != k1

    def test_lru_eviction(self):
        cache = LLMCache(max_entries=2)
        def result(text):
            return CompletionResult(text=text, provider="mock", model="m",
                                    prompt_tokens=1, completion_tokens=1,
                                    latency_ms=0)
        cache.set("a", result("A"))
        cache.set("b", result("B"))
        # 命中 a（把 a 挪到尾部），再写入 c → 淘汰最旧的 b
        assert cache.get("a") is not None
        cache.set("c", result("C"))
        assert cache.get("b") is None          # b 被 LRU 淘汰
        assert cache.get("a") is not None
        assert cache.get("c") is not None
        assert cache.miss_count == 1            # 只有 b 未命中

    def test_clear_resets_stats(self):
        cache = LLMCache()
        cache.set("x", CompletionResult(text="t", provider="p", model="m",
                                        prompt_tokens=1, completion_tokens=1,
                                        latency_ms=0))
        cache.get("x")
        cache.get("y")
        cache.clear()
        assert cache.hit_count == 0 and cache.miss_count == 0
        assert len(cache) == 0


class TestFactoryCacheIntegration:
    async def test_second_identical_call_hits_cache(self, factory, tmp_path):
        provider = _CountingProvider()
        factory._resolve = lambda alias: provider  # type: ignore[method-assign]
        cache = LLMCache()
        factory._resp_cache = cache

        r1 = await _call(factory, "同样的输入")
        r2 = await _call(factory, "同样的输入")

        assert provider.complete_calls == 1          # Provider 只被打了一次
        assert r1.text == r2.text
        assert r1.from_cache is False
        assert r2.from_cache is True
        assert cache.hit_count == 1

    async def test_different_input_misses(self, factory):
        provider = _CountingProvider()
        factory._resolve = lambda alias: provider  # type: ignore[method-assign]
        factory._resp_cache = LLMCache()

        await _call(factory, "输入甲")
        await _call(factory, "输入乙")
        assert provider.complete_calls == 2

    async def test_temperature_above_zero_not_cached(self, factory):
        provider = _CountingProvider()
        factory._resolve = lambda alias: provider  # type: ignore[method-assign]
        factory._resp_cache = LLMCache()

        await _call(factory, "随机输入", temperature=0.7)
        await _call(factory, "随机输入", temperature=0.7)
        assert provider.complete_calls == 2
        assert len(factory._resp_cache) == 0

    async def test_cache_hit_does_not_consume_token(self, factory):
        provider = _CountingProvider()
        factory._resolve = lambda alias: provider  # type: ignore[method-assign]
        factory._resp_cache = LLMCache()

        await _call(factory, "x")
        calls_before = factory.llm_calls
        tokens_before = factory.total_prompt_tokens
        await _call(factory, "x")  # 命中

        # 命中不递增 llm_calls、不加 token
        assert factory.llm_calls == calls_before
        assert factory.total_prompt_tokens == tokens_before

    async def test_ledger_records_cache_hit(self, factory):
        provider = _CountingProvider()
        factory._resolve = lambda alias: provider  # type: ignore[method-assign]
        factory._resp_cache = LLMCache()
        lg = factory._ledger

        await _call(factory, "y")
        await _call(factory, "y")

        assert lg.count("llm_cache_hit") == 1
        hit = list(lg.events("llm_cache_hit"))[-1]
        assert hit.data["alias"] == "mock:m"
        assert len(hit.data["key"]) == 12
        # 命中落了一条 usage=0、cached=True 的 llm_call
        llm_calls = list(lg.events("llm_call"))
        cached_ev = [e for e in llm_calls if e.data.get("cached") is True]
        assert len(cached_ev) == 1
        assert cached_ev[0].data["prompt_tokens"] == 0
        assert cached_ev[0].data["completion_tokens"] == 0

    async def test_cache_none_is_backward_compatible(self, factory):
        """cache=None（默认）：相同输入重复调用，Provider 被打两次，无缓存事件。"""
        provider = _CountingProvider()
        factory._resolve = lambda alias: provider  # type: ignore[method-assign]
        # 不显式设置缓存 → __init__ 默认 None
        await _call(factory, "z")
        await _call(factory, "z")
        assert provider.complete_calls == 2
        assert factory._ledger.count("llm_cache_hit") == 0
