"""执行体级预算：调用前拦截、按协程隔离、释放后无残留。"""

from __future__ import annotations

import pytest

from agentcrew.contracts import AgentBudgetExceeded
from agentcrew.models.agent_budget import (
    bind_budget,
    current,
    guard,
    note_call,
    release_budget,
)
from agentcrew.models.agent_budget import (
    bind_failover as _bind_failover,
)
from agentcrew.models.agent_budget import (
    release_failover as _release_failover,
)


def test_guard_is_noop_without_budget():
    guard()          # 不抛
    note_call(1.0)   # 不抛
    assert current() is None


def test_call_limit_blocks_before_next_call():
    token = bind_budget("fast", None, 2)
    try:
        note_call(0.0)
        guard()
        note_call(0.0)
        with pytest.raises(AgentBudgetExceeded, match="调用次数"):
            guard()
    finally:
        release_budget(token)


def test_cost_ceiling_blocks_when_spent_reaches_limit():
    token = bind_budget("solid", 0.01, None)
    try:
        guard()
        note_call(0.004)
        guard()
        note_call(0.008)
        with pytest.raises(AgentBudgetExceeded, match="成本"):
            guard()
    finally:
        release_budget(token)


def test_release_restores_outer_context():
    outer = bind_budget("outer", 1.0, None)
    inner = bind_budget("inner", 1.0, None)
    assert current().profile == "inner"
    release_budget(inner)
    assert current().profile == "outer"
    release_budget(outer)


def test_negative_cost_is_ignored():
    token = bind_budget("x", 1.0, None)
    try:
        note_call(-5.0)
        assert current().spent_usd == 0.0
    finally:
        release_budget(token)


def test_factory_call_records_into_budget(tmp_path):
    """真实调用路径也要计数：``_account`` 是唯一记账点，预算从这里累加。"""
    from agentcrew.config import Settings
    from agentcrew.models.factory import ModelFactory
    from agentcrew.runtime.ledger import Ledger

    settings = Settings(mock=True, output_dir=tmp_path)
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=tmp_path)
    token = bind_budget("fast", 10.0, 1)
    try:
        asyncio_run(models.call(
            "mock:model", [{"role": "user", "content": "hi"}],
            temperature=0.0, max_tokens=16, purpose="execute", role="backend_dev"))
        assert current().calls == 1
        with pytest.raises(AgentBudgetExceeded):
            asyncio_run(models.call(
                "mock:model", [{"role": "user", "content": "again"}],
                temperature=0.0, max_tokens=16, purpose="execute", role="backend_dev"))
    finally:
        release_budget(token)


def asyncio_run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_agent_failover_overrides_global_map(tmp_path):
    """执行体自带 fallback 时优先于全局 model_failover。"""
    from agentcrew.config import Settings
    from agentcrew.models.factory import ModelFactory

    settings = Settings(mock=True, output_dir=tmp_path,
                        model_failover={"mock:model": ["global:one"]})
    models = ModelFactory(settings, ledger=None, fixtures_dir=tmp_path)
    token = _bind_failover(["local:two", "local:three"])
    try:
        assert models._failover_aliases("mock:model") == [
            "mock:model", "local:two", "local:three"]
    finally:
        _release_failover(token)
    assert models._failover_aliases("mock:model") == ["mock:model", "global:one"]


def test_agent_usage_is_recorded_per_profile(tmp_path):
    from agentcrew.config import Settings
    from agentcrew.models.factory import ModelFactory

    settings = Settings(mock=True, output_dir=tmp_path)
    models = ModelFactory(settings, ledger=None, fixtures_dir=tmp_path)
    token = bind_budget("fast", None, None)     # 只有 profile，没有上限
    try:
        asyncio_run(models.call(
            "mock:model", [{"role": "user", "content": "hi"}],
            temperature=0.0, max_tokens=16, purpose="execute", role="backend_dev"))
    finally:
        release_budget(token)
    usage = models.agent_usage["fast"]
    assert usage["calls"] == 1
    assert usage["prompt_tokens"] > 0


def test_report_prints_agent_cost_line():
    from agentcrew.crew.report import _agent_cost_line

    line = _agent_cost_line({"backend_fast": {"cost_usd": 0.021, "calls": 3}})
    assert "backend_fast" in line and "0.02" in line and "3次" in line



