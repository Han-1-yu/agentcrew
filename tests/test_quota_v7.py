"""V7-P2 多窗口配额管理测试。"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agentcrew.models.quota import (
    AliasQuota,
    QuotaExhaustedError,
    QuotaLimit,
    QuotaTracker,
    parse_quota_config,
)


class TestQuotaConfig:
    def test_quota_limit_empty(self):
        assert QuotaLimit().is_empty() is True

    def test_quota_limit_nonempty(self):
        assert not QuotaLimit(max_calls=100).is_empty()

    def test_alias_quota_empty(self):
        assert AliasQuota().is_empty() is True

    def test_parse_quota_config(self):
        raw = {
            "openai:gpt-4o-mini": {
                "daily": {"max_calls": 100, "max_tokens": 100000},
                "weekly": {"max_cost_usd": 5.0},
            },
            "anthropic:claude-3-5-sonnet": {"monthly": {"max_calls": 1000}},
            "empty:alias": {},
        }
        result = parse_quota_config(raw)
        assert "openai:gpt-4o-mini" in result
        assert "anthropic:claude-3-5-sonnet" in result
        assert "empty:alias" not in result
        assert result["openai:gpt-4o-mini"].daily.max_calls == 100
        assert result["openai:gpt-4o-mini"].weekly.max_cost_usd == 5.0


class TestQuotaCheckAndConsume:
    def _make(self, **kwargs) -> QuotaTracker:
        return QuotaTracker(quotas={"mock:test": AliasQuota(**kwargs)})

    def test_no_quota_passes(self):
        QuotaTracker().check("any:alias")

    def test_daily_calls_exhausted(self):
        t = self._make(daily=QuotaLimit(max_calls=2))
        t.consume("mock:test", 10, 5)
        t.consume("mock:test", 10, 5)
        with pytest.raises(QuotaExhaustedError) as ei:
            t.check("mock:test")
        assert ei.value.window == "daily"
        assert ei.value.metric == "calls"

    def test_daily_tokens_exhausted(self):
        t = self._make(daily=QuotaLimit(max_tokens=100))
        t.consume("mock:test", 60, 50)
        with pytest.raises(QuotaExhaustedError) as ei:
            t.check("mock:test")
        assert ei.value.metric == "tokens"

    def test_weekly_window_independent(self):
        t = self._make(
            daily=QuotaLimit(max_calls=100),
            weekly=QuotaLimit(max_calls=3),
        )
        for _ in range(3):
            t.consume("mock:test", 1, 1)
        with pytest.raises(QuotaExhaustedError) as ei:
            t.check("mock:test")
        assert ei.value.window == "weekly"

    def test_consume_accumulates_all_windows(self):
        t = self._make(
            daily=QuotaLimit(max_calls=100),
            weekly=QuotaLimit(max_calls=100),
            monthly=QuotaLimit(max_calls=100),
        )
        t.consume("mock:test", 10, 5, cost_usd=0.1)
        for w in ("daily", "weekly", "monthly"):
            u = t._get_usage("mock:test", w)
            assert u.calls == 1
            assert u.tokens == 15

    def test_remaining(self):
        t = self._make(daily=QuotaLimit(max_calls=10, max_tokens=1000))
        t.consume("mock:test", 100, 50)
        assert t.remaining("mock:test", "daily", "calls") == 9
        assert t.remaining("mock:test", "daily", "tokens") == 850
        assert t.remaining("no:quota", "daily", "calls") is None

    def test_reset(self):
        t = self._make(daily=QuotaLimit(max_calls=5))
        t.consume("mock:test", 1, 1)
        t.reset("mock:test")
        t.check("mock:test")
        t.consume("mock:test", 1, 1)
        assert t._get_usage("mock:test", "daily").calls == 1


class TestWindowRolling:
    def test_daily_reset_at_midnight(self):
        from agentcrew.models.quota import _WindowUsage
        t = QuotaTracker(quotas={"mock:test": AliasQuota(daily=QuotaLimit(max_calls=5))})
        yesterday = (datetime.now() - timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0)
        t._usage["mock:test"] = {
            "daily": _WindowUsage(tokens=100, cost_usd=1.0, calls=5,
                                  window_start=yesterday.timestamp())
        }
        t.check("mock:test")
        assert t._get_usage("mock:test", "daily").calls == 0

    def test_window_start_daily(self):
        from agentcrew.models.quota import _window_start
        now = datetime(2026, 10, 10, 14, 30, 0).timestamp()
        assert datetime.fromtimestamp(_window_start("daily", now)).hour == 0

    def test_window_start_weekly_monday(self):
        from agentcrew.models.quota import _window_start
        now = datetime(2026, 10, 10, 14, 30, 0).timestamp()
        assert datetime.fromtimestamp(_window_start("weekly", now)).weekday() == 0


class TestPersistence:
    def test_save_and_load(self, tmp_path: Path):
        f = tmp_path / "state.json"
        t1 = QuotaTracker(state_path=f,
                          quotas={"mock:test": AliasQuota(daily=QuotaLimit(max_calls=100))})
        t1.consume("mock:test", 50, 30, cost_usd=0.5)
        t2 = QuotaTracker(state_path=f,
                          quotas={"mock:test": AliasQuota(daily=QuotaLimit(max_calls=100))})
        u = t2._get_usage("mock:test", "daily")
        assert u.calls == 1
        assert u.tokens == 80

    def test_corrupted_state_falls_back(self, tmp_path: Path):
        f = tmp_path / "state.json"
        f.write_text("{broken", encoding="utf-8")
        t = QuotaTracker(state_path=f,
                         quotas={"mock:test": AliasQuota(daily=QuotaLimit(max_calls=100))})
        t.check("mock:test")


class TestFactoryIntegration:
    @pytest.mark.asyncio
    async def test_quota_exhausted_triggers_failover(self):
        from agentcrew.config import Settings
        from agentcrew.models.factory import ModelFactory
        settings = Settings(
            mock=True,
            quotas={"mock:primary": {"daily": {"max_calls": 0}},
                    "mock:backup": {"daily": {"max_calls": 100}}},
            model_failover={"mock:primary": ["mock:backup"]},
        )
        factory = ModelFactory(settings)
        result = await factory.call(
            "mock:primary",
            [{"role": "user", "content": "hi"}],  # type: ignore[arg-type]
            temperature=0.0, max_tokens=50, purpose="t", role="t",
        )
        assert result.text is not None

    @pytest.mark.asyncio
    async def test_quota_disabled_by_flag(self):
        from agentcrew.config import Settings
        from agentcrew.models.factory import ModelFactory
        settings = Settings(
            mock=True, quota_enabled=False,
            quotas={"mock:primary": {"daily": {"max_calls": 0}}},
        )
        factory = ModelFactory(settings)
        result = await factory.call(
            "mock:primary",
            [{"role": "user", "content": "hi"}],  # type: ignore[arg-type]
            temperature=0.0, max_tokens=50, purpose="t", role="t",
        )
        assert result.text is not None
