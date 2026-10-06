"""LLM 摘要式上下文压缩的测试（V2 第 1 项）。"""

from __future__ import annotations

import pytest

from agentcrew.context.summarizer import LLMSummarizer, NullSummarizer


@pytest.mark.asyncio
async def test_null_summarizer_passes_through_short_text():
    summ = NullSummarizer()
    result = await summ.summarize("hello world", max_tokens=100)
    assert result == "hello world"


@pytest.mark.asyncio
async def test_null_summarizer_truncates_long_text():
    summ = NullSummarizer()
    long_text = "\n".join(f"line {i}" for i in range(1000))
    result = await summ.summarize(long_text, max_tokens=50)
    assert len(result) < len(long_text)
    assert "截断" in result


@pytest.mark.asyncio
async def test_llm_summarizer_calls_fn():
    calls: list[tuple[str, int]] = []

    async def fake_fn(text: str, max_tokens: int) -> str:
        calls.append((text, max_tokens))
        return "compressed"

    summ = LLMSummarizer(fake_fn)
    long_text = "x" * 5000
    result = await summ.summarize(long_text, max_tokens=100)
    assert result == "compressed"
    assert len(calls) == 1
    assert calls[0][1] == 100
    assert long_text in calls[0][0]  # 原文传入


@pytest.mark.asyncio
async def test_llm_summarizer_skips_short_text():
    calls: list[tuple[str, int]] = []

    async def fake_fn(text: str, max_tokens: int) -> str:
        calls.append((text, max_tokens))
        return "should not be called"

    summ = LLMSummarizer(fake_fn)
    result = await summ.summarize("short", max_tokens=100)
    assert result == "short"
    assert calls == []


@pytest.mark.asyncio
async def test_llm_summarizer_guards_oversized_output():
    async def fake_fn(text: str, max_tokens: int) -> str:
        # 模拟 LLM 返回超过上限的摘要
        return "y" * 5000

    summ = LLMSummarizer(fake_fn)
    result = await summ.summarize("x" * 5000, max_tokens=50)
    from agentcrew.utils.tokens import estimate_tokens
    assert estimate_tokens(result) <= 50
