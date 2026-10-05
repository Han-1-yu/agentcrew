"""Anthropic 原生 Messages API Provider 的行为测试（spec §12.1 / §17.6）。

brief 用例原样保留；在其之上补三件事：

- ``slept``（autouse）把 ``asyncio.sleep`` 换成记录器（沿用 ``test_retry.py`` 的
  约定）：brief 的 ``test_retries_transient`` 每次耗尽 4 次尝试，若真睡要等约
  7 秒；真实等待时长由 ``test_retry.py`` 覆盖。
- 只拼 text 块的过滤（非 text 块没有 ``.text``，盲拼会 AttributeError/污染输出），
  brief 的 ``test_extracts_text_blocks_only`` 只喂了 text 块，钉不住这条。
- 生产重试参数（attempts=4）用次数钉死，brief 只断言 ``> 1``。
"""

import asyncio

import pytest

from agentcrew.models.config import parse_alias
from agentcrew.models.providers.anthropic import AnthropicProvider
from agentcrew.utils.retry import TransientError


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """记录 ``asyncio.sleep`` 的时长而不真睡。"""
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


class FakeResp:
    def __init__(self, text, i=10, o=5):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.usage = type("U", (), {"input_tokens": i, "output_tokens": o})()

class FakeClient:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error
        self.calls = []
        self.messages = self
    async def create(self, **kw):
        self.calls.append(kw)
        if self._error:
            raise self._error
        return self._result

def binding():
    return parse_alias("anthropic:claude-sonnet-5")

async def test_extracts_text_blocks_only():
    c = FakeClient(FakeResp("answer"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    r = await p.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=10)
    assert r.text == "answer" and r.provider == "anthropic"

async def test_maps_input_output_tokens():
    c = FakeClient(FakeResp("x", i=7, o=3))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    r = await p.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=10)
    assert r.prompt_tokens == 7 and r.completion_tokens == 3

async def test_system_message_extracted_to_top_level_param():
    c = FakeClient(FakeResp("x"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    await p.complete([{"role": "system", "content": "SYS"},
                      {"role": "user", "content": "USR"}], temperature=0, max_tokens=10)
    kw = c.calls[0]
    assert kw["system"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "USR"}]

async def test_no_system_key_when_absent():
    c = FakeClient(FakeResp("x"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    await p.complete([{"role": "user", "content": "USR"}], temperature=0, max_tokens=10)
    assert "system" not in c.calls[0]

async def test_response_format_is_ignored_not_forwarded():
    """Anthropic 不支持 response_format；必须忽略而不是传错参数。"""
    c = FakeClient(FakeResp("x"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    await p.complete([{"role": "user", "content": "U"}], temperature=0, max_tokens=10,
                     response_format={"type": "json_schema", "json_schema": {}})
    assert "response_format" not in c.calls[0]

async def test_retries_transient():
    c = FakeClient(error=Exception("Error code: 503 - overloaded"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    with pytest.raises(TransientError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) > 1


# ── 以下为 brief 之外的补强用例 ──────────────────────────────────────────────

async def test_skips_non_text_blocks():
    """非 text 块没有 ``.text``：盲拼会 AttributeError 或污染输出，必须过滤。"""
    thinking = type("B", (), {"type": "thinking", "thinking": "let me think"})()
    text_block = type("B", (), {"type": "text", "text": "answer"})()
    resp = FakeResp("placeholder")
    resp.content = [thinking, text_block]
    c = FakeClient(resp)
    p = AnthropicProvider(binding(), api_key="k", client=c)
    r = await p.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=10)
    assert r.text == "answer"


async def test_joins_multiple_text_blocks_in_order():
    first = type("B", (), {"type": "text", "text": "a"})()
    second = type("B", (), {"type": "text", "text": "b"})()
    resp = FakeResp("placeholder")
    resp.content = [first, second]
    c = FakeClient(resp)
    p = AnthropicProvider(binding(), api_key="k", client=c)
    r = await p.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=10)
    assert r.text == "ab"


async def test_system_parts_joined_and_model_forwarded():
    c = FakeClient(FakeResp("x"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    await p.complete([{"role": "system", "content": "S1"},
                      {"role": "system", "content": "S2"},
                      {"role": "user", "content": "U"}], temperature=0.4, max_tokens=9)
    kw = c.calls[0]
    assert kw["system"] == "S1\n\nS2"
    assert kw["messages"] == [{"role": "user", "content": "U"}]
    assert kw["model"] == "claude-sonnet-5"
    assert kw["temperature"] == 0.4 and kw["max_tokens"] == 9


async def test_production_retry_config_is_four_attempts(slept):
    """生产参数 attempts=4（brief 只断言 > 1）；退避时长由 test_retry.py 覆盖。"""
    c = FakeClient(error=Exception("Error code: 503 - overloaded"))
    seen: list[tuple[int, str, float]] = []
    p = AnthropicProvider(binding(), api_key="k", client=c,
                          on_retry=lambda n, e, d: seen.append((n, type(e).__name__, d)))
    with pytest.raises(TransientError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) == 4
    assert [n for n, _, _ in seen] == [1, 2, 3]
    assert {name for _, name, _ in seen} == {"TransientError"}
    assert 0.7 <= slept[0] <= 1.3
    assert 1.4 <= slept[1] <= 2.6
    assert 2.8 <= slept[2] <= 5.2
