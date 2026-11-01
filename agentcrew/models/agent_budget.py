"""按「执行体 × 任务」计量的预算闸门。

为什么不用全局计数：``max_cost_usd`` 是 run 级硬上限（已有），而
``AgentProfile.cost_ceiling_usd`` 是**单个 agent 在单个任务上**的软上限——
同一个角色可能有"便宜的主笔 + 不设限的复核"，两者的额度不能混在一起算。

实现用 ``contextvars``：每次 ``BaseAgent.execute`` 绑定一份预算上下文，
``ModelFactory`` 在调用前查额度、记账后累加。并发执行多个 agent 时，
每个协程各自持有自己的上下文，互不影响。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

from agentcrew.contracts import AgentBudgetExceeded


@dataclass
class BudgetState:
    profile: str
    cost_ceiling_usd: float | None
    max_calls: int | None
    spent_usd: float = 0.0
    calls: int = 0


_current: ContextVar[BudgetState | None] = ContextVar("agent_budget", default=None)

#: 执行体自带的备用别名（来自 ``AgentProfile.fallback``）。与全局
#: ``model_failover`` 并存：执行体级优先，缺省回落到全局配置。
_failover: ContextVar[tuple[str, ...] | None] = ContextVar("agent_failover", default=None)


def bind_budget(
    profile: str,
    cost_ceiling_usd: float | None,
    max_calls: int | None,
) -> Token[BudgetState | None] | None:
    """给当前协程绑定一份预算上下文。

    只要给了 ``profile`` 就绑定（即使没有上限）：按执行体记账要用到它。
    没有 profile 时返回 ``None``，单 agent 老路径零开销。
    """
    if not profile:
        return None
    return _current.set(BudgetState(
        profile=profile, cost_ceiling_usd=cost_ceiling_usd, max_calls=max_calls))


def release_budget(token: Token[BudgetState | None] | None) -> None:
    if token is not None:
        _current.reset(token)


def note_call(cost_usd: float) -> None:
    """记一次调用；上限只在 ``guard()`` 里判定（记账失败不该吞掉已完成调用）。"""
    state = _current.get()
    if state is None:
        return
    state.spent_usd += max(0.0, cost_usd)
    state.calls += 1


def guard() -> None:
    """调用**前**的闸门：已花超或已调超就拒绝，避免"越线那一次"重复烧钱。"""
    state = _current.get()
    if state is None:
        return
    if state.max_calls is not None and state.calls >= state.max_calls:
        raise AgentBudgetExceeded(
            state.profile, kind="calls", limit=state.max_calls, used=state.calls)
    if (state.cost_ceiling_usd is not None
            and state.spent_usd >= state.cost_ceiling_usd):
        raise AgentBudgetExceeded(
            state.profile, kind="cost",
            limit=state.cost_ceiling_usd, used=state.spent_usd)


def current() -> BudgetState | None:
    return _current.get()


def bind_failover(aliases: list[str] | None) -> Token[tuple[str, ...] | None] | None:
    """绑定执行体级备用别名；空列表返回 ``None``（零开销）。"""
    if not aliases:
        return None
    return _failover.set(tuple(aliases))


def release_failover(token: Token[tuple[str, ...] | None] | None) -> None:
    if token is not None:
        _failover.reset(token)


def failover_override() -> tuple[str, ...]:
    return _failover.get() or ()
