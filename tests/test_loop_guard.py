"""P1.5 流式循环检测测试。

覆盖 :mod:`agentcrew.models.loop_guard` 的核心算法：

- 重复文本在窗口填满后触发循环（distinct ≤ 24）；
- 正常多样化文本不触发；
- 纯数字 / 符号 unit 被跳过，不计入窗口；
- 滑动窗口：旧 unit 被顶出后从 counts 中移除。

以及工厂层集成：``settings.loop_guard_enabled=True`` 时 stream_call 抛
``LoopDetectedError`` 并落 ``loop_detected`` 事件；默认关闭时行为不变。
"""

from collections.abc import AsyncIterator

import pytest

from agentcrew.config import Settings
from agentcrew.models.config import ModelBinding, ProviderCapabilities
from agentcrew.models.factory import ModelFactory
from agentcrew.models.loop_guard import LoopDetectedError, LoopGuard, UnitRun
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    StreamChunk,
)
from agentcrew.runtime.ledger import Ledger


def test_loop_guard_detects_repetition():
    """256+ 个相同 unit：窗口填满后 distinct=1 ≤ 24，必须触发。"""
    run = UnitRun()
    # 每个 "hello world. " 切出一个 unit；喂 300 个（> LOOP_WINDOW=256）。
    samples = run.feed("hello world. " * 300)
    assert len(samples) > 0
    assert run.n >= UnitRun.LOOP_WINDOW
    # 样本就是那唯一一个重复 unit。
    assert any("hello world" in s for s in samples)


def test_loop_guard_ignores_normal_text():
    """300 个各不相同的 unit：distinct 始终 > 24，不触发。"""
    run = UnitRun()
    text = "".join(
        f"this is sentence number {i} about topic {i}. " for i in range(300))
    samples = run.feed(text)
    assert samples == []
    # 窗口满后 counts 仍有远多于 24 个不同 hash。
    assert run.n >= UnitRun.LOOP_WINDOW
    assert len(run.counts) > UnitRun.LOOP_DISTINCT


def test_loop_guard_skips_non_alphabetic():
    """纯数字 / 符号 unit 归一化后无字母，全部跳过，n 不增长。"""
    run = UnitRun()
    samples = run.feed("012. 345. 678. ##. $$. @@. " * 100)
    assert samples == []
    assert run.n == 0
    assert run.counts == {}


def test_loop_guard_window_slides():
    """滑动窗口：第 257 个 unit 写入 slot 0，顶掉最老的 unit，其 hash 从 counts 删除。"""
    run = UnitRun()
    run.feed("the very first unique sentence. ")
    first_hash = next(iter(run.counts))
    # 再喂 256 个互不相同的 unit：第 256 个 filler 写入时 n=256，slot=0，
    # 触发首次淘汰——顶掉最早的 "first unique"。
    for i in range(1, UnitRun.LOOP_WINDOW + 1):
        run.feed(f"filler sentence number {i} about different topics. ")
    assert run.n == UnitRun.LOOP_WINDOW + 1
    # 最早的 unit 已被顶出窗口，从 counts 删除。
    assert first_hash not in run.counts
    # 窗口内仍是 256 个不同 filler（255 个原始 + 新顶入的 1 个），不触发循环。
    assert len(run.counts) == UnitRun.LOOP_WINDOW


def test_loop_detected_error_carries_channel_and_samples():
    """异常携带 channel / sample_units / window_size 三个字段。"""
    err = LoopDetectedError("text", ["a.", "b."], 256)
    assert err.channel == "text"
    assert err.sample_units == ["a.", "b."]
    assert err.window_size == 256
    assert "text" in str(err)


def test_loop_guard_disabled_is_noop():
    """enabled=False 时 feed/flush 全部空转（向后兼容）。"""
    guard = LoopGuard(enabled=False)
    assert guard.feed_text("hello world. " * 300) == []
    assert guard.flush() == {}


# ── 工厂层集成：stream_call 接线 ──────────────────────────────────────────


class _RepeatingStreamProvider(BaseProvider):
    """流式吐出重复句子的假 Provider（模拟推理模型死循环）。"""

    def __init__(self, text: str) -> None:
        binding = ModelBinding(
            alias="mock:loop", provider="mock", model="loop",
            implementation="mock", base_url=None, api_key_env="",
            capabilities=ProviderCapabilities(max_context_tokens=128_000,
                                             max_output_tokens=8192),
        )
        super().__init__(binding)
        self._text = text

    async def complete(self, messages, *, temperature, max_tokens,
                       response_format=None):
        return CompletionResult(
            text=self._text, provider="mock", model="loop",
            prompt_tokens=1, completion_tokens=len(self._text), latency_ms=1)

    async def stream_complete(self, messages, *, temperature, max_tokens,
                              response_format=None) -> AsyncIterator[StreamChunk]:
        # 按 8 字符切片吐 chunk，模拟真实 SSE 流。
        for i in range(0, len(self._text), 8):
            yield StreamChunk(delta=self._text[i:i + 8],
                              finish_reason="stop" if i + 8 >= len(self._text) else None)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


async def test_stream_call_raises_loop_detected_when_enabled(
        settings, tmp_path, monkeypatch):
    """loop_guard_enabled=True 时，重复文本流中途抛 LoopDetectedError。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = settings.model_copy(update={"loop_guard_enabled": True})
    f = ModelFactory(s, ledger=lg)
    repeating = "i am stuck in a loop and i repeat myself. " * 400
    monkeypatch.setattr(f, "_resolve", lambda alias: _RepeatingStreamProvider(repeating))

    chunks: list[str] = []
    with pytest.raises(LoopDetectedError) as ei:
        async for chunk in f.stream_call(
            "mock:loop", [{"role": "user", "content": "hi"}],
            temperature=0, max_tokens=100, purpose="execute", role="r"):
            chunks.append(chunk.delta)

    assert ei.value.channel == "text"
    assert ei.value.window_size == UnitRun.LOOP_WINDOW
    assert len(ei.value.sample_units) <= UnitRun.LOOP_DISTINCT
    # 部分 chunk 已吐出（流中断不可回滚）。
    assert len(chunks) > 0
    # ledger 留痕。
    events = list(lg.events("loop_detected"))
    assert len(events) == 1
    assert events[0].data["channel"] == "text"
    assert events[0].data["window_size"] == UnitRun.LOOP_WINDOW


async def test_stream_call_no_loop_guard_by_default(settings, tmp_path, monkeypatch):
    """loop_guard_enabled=False（默认）时重复文本也不抛 LoopDetectedError。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)  # 默认关闭
    repeating = "i am stuck in a loop and i repeat myself. " * 400
    monkeypatch.setattr(f, "_resolve", lambda alias: _RepeatingStreamProvider(repeating))

    chunks: list[str] = []
    async for chunk in f.stream_call(
        "mock:loop", [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=100, purpose="execute", role="r"):
        chunks.append(chunk.delta)
    assert "".join(chunks) == repeating
    assert lg.count("loop_detected") == 0
