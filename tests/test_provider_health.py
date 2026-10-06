"""P2-11：Provider 主动健康监控测试。

覆盖：
- health_check 对 MockProvider 返回 healthy
- health_check 对抛异常的 provider 返回 unavailable
- health_check 对慢响应返回 degraded
- _failover_aliases 将 unavailable 别名移到末尾
- 健康别名保持在前列
- health_check_enabled=False 时不启动监控
- ledger 记录 provider_health 事件
- get_health 返回正确状态
"""

import asyncio
from collections.abc import Sequence

import pytest

from agentcrew.config import Settings
from agentcrew.models.config import parse_alias
from agentcrew.models.factory import (
    HealthRecord,
    ModelFactory,
    _HEALTH_DEGRADED_THRESHOLD_MS,
)
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.runtime.ledger import Ledger
from agentcrew.utils.retry import TransientError


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


class _FailingProvider(BaseProvider):
    """complete 恒抛异常的假 Provider（模拟不可用）。"""

    def __init__(self, alias: str = "openai:down") -> None:
        super().__init__(parse_alias(alias))

    async def complete(self, messages: Sequence[Message], *,
                       temperature: float, max_tokens: int,
                       response_format: ResponseFormat | None = None) -> CompletionResult:
        raise TransientError("503 service unavailable")


class _SlowProvider(BaseProvider):
    """complete 带延迟的假 Provider（模拟慢响应）。"""

    def __init__(self, alias: str = "openai:slow", delay: float = 0.05) -> None:
        super().__init__(parse_alias(alias))
        self._delay = delay

    async def complete(self, messages: Sequence[Message], *,
                       temperature: float, max_tokens: int,
                       response_format: ResponseFormat | None = None) -> CompletionResult:
        await asyncio.sleep(self._delay)
        return CompletionResult(
            text="ok", provider=self.binding.provider, model=self.binding.model,
            prompt_tokens=1, completion_tokens=1, latency_ms=10)


# ── health_check 基本行为 ──────────────────────────────────────────────────


async def test_health_check_mock_provider_returns_healthy(settings, tmp_path):
    """MockProvider 直接返回 healthy，不真实调用。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    record = await f.health_check("mock:m")
    assert record.status == "healthy"
    assert record.alias == "mock:m"
    assert record.consecutive_failures == 0


async def test_health_check_failing_provider_returns_unavailable(settings, tmp_path, monkeypatch):
    """抛异常的 provider → unavailable，consecutive_failures 递增。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    failing = _FailingProvider("openai:down")
    monkeypatch.setattr(f, "_resolve", lambda alias: failing)

    record = await f.health_check("openai:down")
    assert record.status == "unavailable"
    assert record.consecutive_failures == 1
    assert record.error is not None
    assert "503" in record.error

    # 连续第二次探测 → consecutive_failures=2
    record2 = await f.health_check("openai:down")
    assert record2.consecutive_failures == 2


async def test_health_check_slow_provider_returns_degraded(settings, tmp_path, monkeypatch):
    """慢响应（延迟超过阈值）→ degraded。

    monkeypatch 阈值为极小值，让 50ms 的延迟必然超过它。
    """
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    slow = _SlowProvider("openai:slow", delay=0.05)
    monkeypatch.setattr(f, "_resolve", lambda alias: slow)
    # 阈值设为 0.001ms（远小于实际延迟），确保必然 degraded。
    monkeypatch.setattr(
        "agentcrew.models.factory._HEALTH_DEGRADED_THRESHOLD_MS", 0.001)

    record = await f.health_check("openai:slow")
    assert record.status == "degraded"
    assert record.latency_ms is not None
    assert record.latency_ms > 0


async def test_health_check_fast_provider_returns_healthy(settings, tmp_path, monkeypatch):
    """快速响应 → healthy（恢复默认阈值验证）。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    fast = _SlowProvider("openai:fast", delay=0.001)
    monkeypatch.setattr(f, "_resolve", lambda alias: fast)

    record = await f.health_check("openai:fast")
    assert record.status == "healthy"


# ── ledger 事件 ────────────────────────────────────────────────────────────


async def test_health_check_emits_provider_health_event(settings, tmp_path):
    """每次 health_check 都记一条 provider_health 事件。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    await f.health_check("mock:m")
    events = list(lg.events("provider_health"))
    assert len(events) == 1
    assert events[0].data["alias"] == "mock:m"
    assert events[0].data["status"] == "healthy"


async def test_health_unavailable_emits_event_with_error(settings, tmp_path, monkeypatch):
    """unavailable 事件含 error 字段。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    failing = _FailingProvider()
    monkeypatch.setattr(f, "_resolve", lambda alias: failing)
    await f.health_check("openai:down")
    events = list(lg.events("provider_health"))
    assert len(events) == 1
    assert events[0].data["status"] == "unavailable"
    assert events[0].data["error"] is not None


# ── get_health ────────────────────────────────────────────────────────────


async def test_get_health_returns_correct_state(settings, tmp_path):
    """get_health 返回缓存的健康记录；未探测返回 None。"""
    f = ModelFactory(settings, ledger=None)
    assert f.get_health("mock:m") is None
    await f.health_check("mock:m")
    rec = f.get_health("mock:m")
    assert rec is not None
    assert rec.status == "healthy"


# ── _failover_aliases 健康感知重排 ────────────────────────────────────────


def _failover_settings(settings, **mapping):
    return settings.model_copy(update={"model_failover": dict(mapping)})


async def test_failover_aliases_moves_unavailable_to_end(settings, tmp_path, monkeypatch):
    """unavailable 的别名被移到末尾，健康的在前面。"""
    s = _failover_settings(settings, **{"mock:a": ["mock:b", "mock:c"]})
    f = ModelFactory(s, ledger=None)
    # 预先缓存探测结果：b 不可用，c 正常。
    from datetime import datetime
    f._health["mock:b"] = HealthRecord(
        alias="mock:b", status="unavailable",
        last_check=datetime.now(), error="down", consecutive_failures=3)
    f._health["mock:c"] = HealthRecord(
        alias="mock:c", status="healthy",
        last_check=datetime.now(), consecutive_failures=0)
    result = f._failover_aliases("mock:a")
    # a 无健康记录（视为健康），c 健康，b 不可用 → a, c, b
    assert result == ["mock:a", "mock:c", "mock:b"]


async def test_failover_aliases_all_unavailable_keeps_original_order(settings, tmp_path):
    """全部别名都 unavailable 时保持原序（最后一搏）。"""
    from datetime import datetime
    s = _failover_settings(settings, **{"mock:a": ["mock:b", "mock:c"]})
    f = ModelFactory(s, ledger=None)
    for a in ("mock:a", "mock:b", "mock:c"):
        f._health[a] = HealthRecord(
            alias=a, status="unavailable",
            last_check=datetime.now(), consecutive_failures=1)
    result = f._failover_aliases("mock:a")
    assert result == ["mock:a", "mock:b", "mock:c"]


async def test_failover_aliases_no_health_data_unchanged(settings, tmp_path):
    """无健康数据时行为不变（向后兼容）。"""
    s = _failover_settings(settings, **{"mock:a": ["mock:b", "mock:c"]})
    f = ModelFactory(s, ledger=None)
    result = f._failover_aliases("mock:a")
    assert result == ["mock:a", "mock:b", "mock:c"]


async def test_failover_aliases_degraded_stays_in_place(settings, tmp_path):
    """degraded 的别名保持原位（不移到末尾）。"""
    from datetime import datetime
    s = _failover_settings(settings, **{"mock:a": ["mock:b", "mock:c"]})
    f = ModelFactory(s, ledger=None)
    f._health["mock:b"] = HealthRecord(
        alias="mock:b", status="degraded", latency_ms=3000.0,
        last_check=datetime.now(), consecutive_failures=0)
    result = f._failover_aliases("mock:a")
    # degraded 不移位，只有 unavailable 才移到末尾。
    assert result == ["mock:a", "mock:b", "mock:c"]


# ── health_check_enabled 开关 ──────────────────────────────────────────────


def test_health_check_disabled_no_monitoring(settings):
    """health_check_enabled=False 时 start_health_monitoring 不创建任务。"""
    s = settings.model_copy(update={"health_check_enabled": False})
    f = ModelFactory(s, ledger=None)
    f.start_health_monitoring()
    assert f._health_task is None


def test_health_check_enabled_default_true(settings):
    """默认启用健康监控。"""
    assert settings.health_check_enabled is True


def test_health_check_interval_default_30(settings):
    """默认探测间隔 30 秒。"""
    assert settings.health_check_interval_secs == 30


# ── start/stop 监控 ───────────────────────────────────────────────────────


async def test_start_stop_health_monitoring(settings):
    """start 创建任务，stop 取消任务。"""
    f = ModelFactory(settings, ledger=None)
    f.start_health_monitoring()
    assert f._health_task is not None
    assert not f._health_task.done()
    f.stop_health_monitoring()
    assert f._health_task is None
    # 确认任务被取消（不抛）。
    f.stop_health_monitoring()


# ── failover 事件含健康信息 ───────────────────────────────────────────────


async def test_failover_event_includes_health_status(settings, tmp_path, monkeypatch):
    """llm_failover 事件追加 from_status / to_status。

    primary 标记为 degraded（保持原位），调用时抛错 → failover 到 backup。
    """
    from datetime import datetime
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = _failover_settings(settings, **{"mock:primary": ["mock:backup"]})
    f = ModelFactory(s, ledger=lg)
    # primary degraded（不移位），backup 健康。
    f._health["mock:primary"] = HealthRecord(
        alias="mock:primary", status="degraded", latency_ms=3500.0,
        last_check=datetime.now(), consecutive_failures=0)
    f._health["mock:backup"] = HealthRecord(
        alias="mock:backup", status="healthy",
        last_check=datetime.now(), consecutive_failures=0)

    primary = f.get("mock:primary")

    async def _raise(*a, **kw):
        raise TransientError("503")

    primary.complete = _raise  # type: ignore[method-assign]
    backup = f.get("mock:backup")
    set_purpose(backup, "execute", role="r", task_id="T-1", attempt=1)

    await f.call("mock:primary", [{"role": "user", "content": "x"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    events = list(lg.events("llm_failover"))
    assert len(events) == 1
    assert events[0].data["from_status"] == "degraded"
    assert events[0].data["to_status"] == "healthy"
