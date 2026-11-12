"""R44：成本感知路由（执行体剩余额度不足时提前改走便宜档）。

默认关闭；开启后只对**声明了 run 级额度**的执行体生效。这里全部离线、纯函数直测：
不建 provider、不发网络请求。
"""

from __future__ import annotations

import pytest

from agentcrew.config import Settings
from agentcrew.models.agent_budget import bind_budget, note_call, release_budget
from agentcrew.models.budget import under_headroom
from agentcrew.models.factory import ModelFactory


class _Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, name: str, **data) -> None:
        self.events.append((name, data))


# ── 纯判定 ────────────────────────────────────────────────────────────────


def test_under_headroom_thresholds():
    assert under_headroom(70.0, 100.0, 0.3) is True, "只剩 30% 即触发"
    assert under_headroom(69.9, 100.0, 0.3) is False
    assert under_headroom(999.0, None, 0.3) is False, "没有额度就没有剩余，不猜"
    assert under_headroom(999.0, 0.0, 0.3) is False


# ── 工厂接线 ──────────────────────────────────────────────────────────────


def _factory(**kw) -> tuple[ModelFactory, _Sink]:
    sink = _Sink()
    return ModelFactory(Settings(**kw), ledger=sink), sink


def test_headroom_shift_switches_alias_and_reports_once():
    factory, sink = _factory(cost_aware_routing=True, cost_aware_headroom=0.3)
    factory._find_cheaper_fallback = lambda alias, binding=None: "openai:gpt-4o-mini"

    token = bind_budget("backend_fast", None, None, run_ceiling_usd=1.0)
    try:
        note_call(0.8)  # 已花 80%，剩余 20% < 30%
        picked = factory._maybe_apply_downgrade("anthropic:claude-sonnet-5")
        again = factory._maybe_apply_downgrade("anthropic:claude-sonnet-5")
    finally:
        release_budget(token)

    assert picked == "openai:gpt-4o-mini"
    assert again == "openai:gpt-4o-mini"
    shifts = [e for e in sink.events if e[0] == "routing_budget_shift"]
    assert len(shifts) == 1, "同一执行体只上报一次，避免刷屏"
    assert shifts[0][1]["profile"] == "backend_fast"
    assert shifts[0][1]["to_alias"] == "openai:gpt-4o-mini"


def test_headroom_shift_keeps_alias_when_budget_is_healthy():
    factory, _ = _factory(cost_aware_routing=True)
    factory._find_cheaper_fallback = lambda alias, binding=None: "openai:gpt-4o-mini"
    token = bind_budget("backend_fast", None, None, run_ceiling_usd=1.0)
    try:
        note_call(0.1)
        assert factory._maybe_apply_downgrade("anthropic:claude-sonnet-5") == (
            "anthropic:claude-sonnet-5")
    finally:
        release_budget(token)


def test_headroom_shift_is_off_by_default():
    factory, _ = _factory()
    factory._find_cheaper_fallback = lambda alias, binding=None: "openai:gpt-4o-mini"
    token = bind_budget("backend_fast", None, None, run_ceiling_usd=1.0)
    try:
        note_call(0.99)
        assert factory._maybe_apply_downgrade("anthropic:claude-sonnet-5") == (
            "anthropic:claude-sonnet-5")
    finally:
        release_budget(token)


def test_headroom_shift_ignores_profile_without_run_ceiling():
    factory, _ = _factory(cost_aware_routing=True)
    factory._find_cheaper_fallback = lambda alias, binding=None: "openai:gpt-4o-mini"
    token = bind_budget("backend_fast", None, None)  # 没有 run_ceiling_usd
    try:
        note_call(99.0)
        assert factory._maybe_apply_downgrade("anthropic:claude-sonnet-5") == (
            "anthropic:claude-sonnet-5")
    finally:
        release_budget(token)


def test_global_downgrade_wins_over_headroom():
    """全局预算降级已触发时以它为准，不被执行体级判断覆盖。"""
    factory, _ = _factory(cost_aware_routing=True)
    factory._find_cheaper_fallback = lambda alias, binding=None: "openai:gpt-4o-mini"
    factory._budget_downgrade_to = "deepseek:deepseek-chat"
    token = bind_budget("backend_fast", None, None, run_ceiling_usd=1.0)
    try:
        note_call(0.99)
        assert factory._maybe_apply_downgrade("anthropic:claude-sonnet-5") == (
            "deepseek:deepseek-chat")
    finally:
        release_budget(token)


@pytest.mark.parametrize("headroom", [0.0, 0.5, 0.9])
def test_headroom_is_configurable(headroom):
    factory, _ = _factory(cost_aware_routing=True, cost_aware_headroom=headroom)
    factory._find_cheaper_fallback = lambda alias, binding=None: "cheap:model"
    token = bind_budget("p", None, None, run_ceiling_usd=1.0)
    try:
        note_call(0.25)
        expect_shift = 0.25 >= (1 - headroom)
        assert (factory._maybe_apply_downgrade("pricey:model") == "cheap:model") is expect_shift
    finally:
        release_budget(token)
