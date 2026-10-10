"""P0 故障转移增强测试：402 故障转移、冷却矩阵、preset fallback、流式首块前切换。

覆盖 Magpie 风格的失败分类冷却：

- 402 余额不足 → 触发故障转移（Provider 内部不重试）；
- 5xx → 触发故障转移 + 冷却；
- 冷却中的别名在 _failover_aliases 里排到末尾；
- 冷却过期后别名恢复首位；
- preset.fallback_models 在无 settings.model_failover 时自动派生备用别名；
- 流式调用在第一个 chunk 到达前失败可切换；
- 无 fallback 配置时 402 原样抛出（向后兼容）。
"""

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.models.factory import (
    ModelFactory,
    ProviderUnavailableError,
)
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
    StreamChunk,
)
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.runtime.ledger import Ledger
from agentcrew.utils.retry import TransientError


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


def _failover_settings(settings, **mapping: list[str]):
    return settings.model_copy(update={"model_failover": dict(mapping)})


def _make_transient(status_code: int, msg: str = "") -> TransientError:
    """构造一个带 ``status_code`` 属性的 TransientError（模拟 provider 故障）。"""
    exc = TransientError(f"Error code: {status_code} - {msg}")
    exc.status_code = status_code  # type: ignore[attr-defined]
    return exc


# ── 1. 402 触发故障转移 ──────────────────────────────────────────────────

async def test_402_triggers_failover(settings, tmp_path):
    """primary 抛 402（余额不足）→ 自动切换到 backup 并成功。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = _failover_settings(settings, **{"mock:primary": ["mock:backup"]})
    f = ModelFactory(s, ledger=lg)

    primary = f.get("mock:primary")

    async def _raise_402(*a, **kw):
        raise _make_transient(402, "insufficient_quota")

    primary.complete = _raise_402  # type: ignore[method-assign]

    backup = f.get("mock:backup")
    set_purpose(backup, "execute", role="r", task_id="T-1", attempt=1)

    result = await f.call(
        "mock:primary", [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=10, purpose="execute", role="r")
    assert result is not None
    assert f.llm_calls == 1
    events = list(lg.events("llm_failover"))
    assert len(events) == 1
    assert events[0].data["from_alias"] == "mock:primary"
    assert events[0].data["to_alias"] == "mock:backup"
    assert "402" in events[0].data["error"]


# ── 2. 500 触发故障转移 ──────────────────────────────────────────────────

async def test_500_triggers_failover(settings, tmp_path):
    """primary 抛 500 → 切换到 backup 成功。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = _failover_settings(settings, **{"mock:a": ["mock:b"]})
    f = ModelFactory(s, ledger=lg)

    pa = f.get("mock:a")

    async def _raise_500(*a, **kw):
        raise _make_transient(500, "internal server error")

    pa.complete = _raise_500  # type: ignore[method-assign]

    pb = f.get("mock:b")
    set_purpose(pb, "execute", role="r", task_id="T-1", attempt=1)

    result = await f.call(
        "mock:a", [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=10, purpose="execute", role="r")
    assert result is not None
    assert f.llm_calls == 1
    assert lg.count("llm_failover") == 1


# ── 3. 冷却把别名排到末尾 ────────────────────────────────────────────────

async def test_cooldown_moves_alias_to_end(settings, tmp_path):
    """失败后 primary 被标记冷却；下次 _failover_aliases 把它排到末尾。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = _failover_settings(settings, **{"mock:primary": ["mock:backup"]})
    f = ModelFactory(s, ledger=lg)

    primary = f.get("mock:primary")

    async def _raise(*a, **kw):
        raise _make_transient(500, "boom")

    primary.complete = _raise  # type: ignore[method-assign]
    backup = f.get("mock:backup")
    set_purpose(backup, "execute", role="r", task_id="T-1", attempt=1)

    # 第一次调用：primary 失败 → backup 成功。primary 进入冷却。
    await f.call(
        "mock:primary", [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=10, purpose="execute", role="r")

    # primary 应在冷却中
    assert f._is_cooled_down("mock:primary") is True
    # 下次 _failover_aliases 把冷却中的 primary 排到末尾
    order = f._failover_aliases("mock:primary")
    assert order == ["mock:backup", "mock:primary"], order


# ── 4. 冷却过期后恢复 ───────────────────────────────────────────────────

async def test_cooldown_expiry(settings, tmp_path):
    """手动把冷却设为已过期 → _is_cooled_down 返回 False，别名回到首位。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = _failover_settings(settings, **{"mock:primary": ["mock:backup"]})
    f = ModelFactory(s, ledger=lg)

    # 手动设置冷却为 1 分钟前（已过期）
    f._cooldowns["mock:primary"] = datetime.now() - timedelta(minutes=1)

    # _is_cooled_down 应惰性清除并返回 False
    assert f._is_cooled_down("mock:primary") is False
    assert "mock:primary" not in f._cooldowns

    # _failover_aliases 保持原序
    order = f._failover_aliases("mock:primary")
    assert order == ["mock:primary", "mock:backup"], order


# ── 5. preset.fallback_models 自动生效 ─────────────────────────────────

async def test_preset_fallback_models_used(settings, tmp_path):
    """无 settings.model_failover 时，从 preset.fallback_models 派生备用别名。

    deepseek 预设的 fallback_models = ["deepseek-chat", "deepseek-v4-flash"]。
    """
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)

    # 不配置 model_failover；deepseek 预设自带 fallback_models。
    aliases = f._failover_aliases("deepseek:deepseek-v4-pro")
    assert aliases[0] == "deepseek:deepseek-v4-pro"
    # 派生的备用别名来自 preset.fallback_models
    assert "deepseek:deepseek-chat" in aliases
    assert "deepseek:deepseek-v4-flash" in aliases
    # 去重保序
    assert len(aliases) == len(set(aliases))


# ── 6. 流式调用首块前失败可切换 ─────────────────────────────────────────

class _FailingFirstChunkProvider(MockProvider):
    """stream_complete 在产出第一个 chunk 前抛 TransientError。"""

    async def stream_complete(self, messages, *, temperature, max_tokens,
                              response_format=None):
        raise _make_transient(503, "stream not available")
        yield  # pragma: no cover — 使本方法成为 async generator


async def test_streaming_failover_before_first_chunk(settings, tmp_path):
    """流式调用在第一个 chunk 前失败 → 切换到备用别名并成功产出。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = _failover_settings(settings, **{"mock:primary": ["mock:backup"]})
    f = ModelFactory(s, ledger=lg)

    # primary 的 stream_complete 在首块前抛错。
    failing = _FailingFirstChunkProvider(f.get("mock:primary").binding)
    f._cache["mock:primary"] = failing

    # backup 正常流式产出。
    backup = f.get("mock:backup")
    set_purpose(backup, "execute", role="r", task_id="T-1", attempt=1)

    chunks = [chunk async for chunk in f.stream_call(
        "mock:primary", [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=10, purpose="execute", role="r")]

    assert len(chunks) >= 1
    assert "".join(c.delta for c in chunks)  # 有内容
    assert lg.count("llm_failover") == 1


# ── 7. 无 fallback 配置时 402 原样抛出（向后兼容）────────────────────────

async def test_no_failover_without_config(settings, tmp_path):
    """无 fallback 配置时 402 应原样抛出（不包装成 ProviderUnavailableError）。

    ollama 预设的 fallback_models 为空，且 settings.model_failover 未配置，
    因此 _failover_aliases 只返回 [alias]；失败时直接抛原始 TransientError。
    """
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)

    provider = f.get("ollama:llama3.3")

    async def _raise_402(*a, **kw):
        raise _make_transient(402, "insufficient_quota")

    provider.complete = _raise_402  # type: ignore[method-assign]

    with pytest.raises(TransientError) as ei:
        await f.call(
            "ollama:llama3.3", [{"role": "user", "content": "hi"}],
            temperature=0, max_tokens=10, purpose="execute", role="r")
    # 必须是原始 TransientError，不是 ProviderUnavailableError 包装
    assert not isinstance(ei.value, ProviderUnavailableError)
    assert getattr(ei.value, "status_code", None) == 402
    assert lg.count("llm_failover") == 0
