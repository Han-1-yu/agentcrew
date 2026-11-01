"""V7-P3 会话亲和性测试。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agentcrew.models.affinity import (
    AFFINITY_OFF,
    AFFINITY_SESSION,
    AFFINITY_TURN,
    AffinityTracker,
)


class TestAffinityModes:
    def test_off_mode_disabled(self):
        t = AffinityTracker(mode=AFFINITY_OFF)
        assert not t.enabled
        assert t.resolve("primary", "s1") == "primary"

    def test_session_mode_enabled(self):
        assert AffinityTracker(mode=AFFINITY_SESSION).enabled

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            AffinityTracker(mode="bad")

    def test_none_session_id_returns_primary(self):
        t = AffinityTracker(mode=AFFINITY_SESSION)
        assert t.resolve("primary", None) == "primary"


class TestResolveAndRemember:
    def test_session_mode_returns_bound_alias(self):
        t = AffinityTracker(mode=AFFINITY_SESSION)
        t.remember("s1", "backup")
        assert t.resolve("primary", "s1") == "backup"

    def test_no_record_returns_primary(self):
        t = AffinityTracker(mode=AFFINITY_SESSION)
        assert t.resolve("primary", "unknown") == "primary"

    def test_turn_mode_always_primary(self):
        t = AffinityTracker(mode=AFFINITY_TURN)
        t.remember("s1", "backup")
        assert t.resolve("primary", "s1") == "primary"

    def test_remember_force_switch(self):
        t = AffinityTracker(mode=AFFINITY_SESSION)
        t.remember("s1", "a")
        t.remember("s1", "b", force=True)
        assert t.resolve("primary", "s1") == "b"
        assert t.get("s1").switch_count == 1

    def test_candidates_filter(self):
        t = AffinityTracker(mode=AFFINITY_SESSION)
        t.remember("s1", "b")
        assert t.resolve("a", "s1", candidates=["a", "c"]) == "a"

    def test_availability_check(self):
        t = AffinityTracker(mode=AFFINITY_SESSION)
        t.remember("s1", "b")
        assert t.resolve("a", "s1", is_available=lambda x: x != "b") == "a"

    def test_availability_check_passes(self):
        t = AffinityTracker(mode=AFFINITY_SESSION)
        t.remember("s1", "b")
        assert t.resolve("a", "s1", is_available=lambda x: True) == "b"


class TestManagement:
    def test_forget(self):
        t = AffinityTracker(mode=AFFINITY_SESSION)
        t.remember("s1", "a")
        t.forget("s1")
        assert t.get("s1") is None

    def test_reset(self):
        t = AffinityTracker(mode=AFFINITY_SESSION)
        t.remember("s1", "a")
        t.remember("s2", "b")
        t.reset()
        assert t.get("s1") is None
        assert t.get("s2") is None


class TestPersistence:
    def test_save_and_load(self, tmp_path: Path):
        f = tmp_path / "affinity.json"
        t1 = AffinityTracker(mode=AFFINITY_SESSION, state_path=f)
        t1.remember("s1", "b")
        t2 = AffinityTracker(mode=AFFINITY_SESSION, state_path=f)
        assert t2.resolve("a", "s1") == "b"

    def test_corrupted_state(self, tmp_path: Path):
        f = tmp_path / "affinity.json"
        f.write_text("{broken", encoding="utf-8")
        t = AffinityTracker(mode=AFFINITY_SESSION, state_path=f)
        assert t.resolve("a", "s1") == "a"

    def test_expired_not_loaded(self, tmp_path: Path):
        f = tmp_path / "affinity.json"
        f.write_text(json.dumps({
            "records": {"old": {"alias": "x", "last_used": time.time() - 30*86400}}
        }), encoding="utf-8")
        t = AffinityTracker(mode=AFFINITY_SESSION, state_path=f, ttl_seconds=7*86400)
        assert t.get("old") is None


class TestFactoryIntegration:
    @pytest.mark.asyncio
    async def test_affinity_off_no_tracking(self):
        from agentcrew.config import Settings
        from agentcrew.models.factory import ModelFactory
        settings = Settings(mock=True, affinity_mode="off")
        factory = ModelFactory(settings)
        await factory.call(
            "mock:primary",
            [{"role": "user", "content": "hi"}],  # type: ignore[arg-type]
            temperature=0.0, max_tokens=50, purpose="t", role="t",
            session_id="s1",
        )
        assert factory._affinity_tracker.get("s1") is None

    @pytest.mark.asyncio
    async def test_affinity_session_binds_after_call(self):
        from agentcrew.config import Settings
        from agentcrew.models.factory import ModelFactory
        settings = Settings(
            mock=True, affinity_mode="session",
            model_failover={"mock:primary": [
                {"alias": "mock:primary", "weight": 1.0},
                {"alias": "mock:backup", "weight": 1.0},
            ]},
        )
        factory = ModelFactory(settings)
        await factory.call(
            "mock:primary",
            [{"role": "user", "content": "hi"}],  # type: ignore[arg-type]
            temperature=0.0, max_tokens=50, purpose="t", role="t",
            session_id="s1",
        )
        rec = factory._affinity_tracker.get("s1")
        assert rec is not None
        assert rec.alias in ("mock:primary", "mock:backup")
