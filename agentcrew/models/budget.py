"""预算策略：软阈值告警、自动降级选型、硬上限判定（P0-3 / P2 增强）。

三档预算语义在这里定义，与「谁负责抛异常」解耦：

- **软阈值告警**（默认 80%）：只落 ``budget_warning`` 事件，不中断；
- **自动降级**（默认 90%）：把后续调用切到更便宜的备别名，需能找到一个
  「单价更低且价格已知」的候选，否则不降级；
- **硬上限**（100%）：超出即抛 :class:`~agentcrew.models.errors.BudgetExhaustedError`。

本模块全是纯函数：输入是当前累计值与配置，输出是「该不该做」与「换成谁」。
累计值本身由 :class:`~agentcrew.models.factory.ModelFactory` 持有。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agentcrew.models.config import ModelBinding
from agentcrew.models.errors import BudgetExhaustedError
from agentcrew.models.pricing import MODEL_PRICING

__all__ = [
    "BUDGET_EPSILON",
    "cheaper_fallback",
    "exhaustion_error",
    "input_price_of",
    "threshold_reached",
    "under_headroom",
]

#: 预算软阈值比较的浮点容差（USD）。累计成本是多次加法累加的，
#: 与 ``limit * threshold`` 直接比会有 ~1e-17 误差，导致「刚好达到阈值」漏触发。
BUDGET_EPSILON = 1e-9


def threshold_reached(current: float, limit: float | None, threshold: float) -> bool:
    """累计成本是否已达 ``limit * threshold``（带浮点容差）。``limit`` 为 None 时恒 False。"""
    if limit is None:
        return False
    return current >= limit * threshold - BUDGET_EPSILON


def under_headroom(spent: float, ceiling: float | None, headroom: float) -> bool:
    """剩余额度是否已低于 ``headroom`` 比例（成本感知路由的触发条件）。

    ``ceiling`` 为 None 表示该执行体没有声明 run 级额度——没有"剩余"可言，
    一律返回 False（不猜、不提前降级）。

    ``headroom=0.3`` 的含义是"剩下不到 30% 了"：此时提前换便宜模型，比等到
    ``budget_downgrade_threshold``（默认 90% 已花完）才刹车更有意义——后者往往
    只剩最后几次调用，省不下什么。
    """
    if ceiling is None or ceiling <= 0:
        return False
    return spent >= ceiling * (1.0 - headroom) - BUDGET_EPSILON


def input_price_of(model: str) -> float:
    """取模型 input 单价（USD / 1M tokens）；未知模型返回 0.0。

    与 :func:`~agentcrew.models.pricing.estimate_cost` 同款最长前缀匹配，
    仅取 input 价：比较「哪个备用模型更便宜」用 input 价即可，无需完整估算。
    """
    prefix = max(
        (p for p in MODEL_PRICING if model.startswith(p)), key=len, default=None)
    if prefix is None:
        return 0.0
    return MODEL_PRICING[prefix][0]


def cheaper_fallback(
    current_model: str,
    *,
    failover_map: Mapping[str, Sequence[str | dict[str, Any]]],
    binding: ModelBinding | None = None,
) -> str | None:
    """找 input 单价最低且比当前模型便宜的备别名；没有则返回 ``None``。

    候选来源合并两处：(1) ``settings.model_failover`` 显式配置的备用列表；
    (2) 当前 binding 的 ``fallback_models``（preset 自带的降级链）。

    当前模型价格未知（0.0）时**不降级** —— 无法判断谁更便宜，贸然切换可能更贵。
    无更便宜选项同样返回 ``None``，让调用方保持原行为（达 100% 时照常抛硬上限）。
    """
    current_input = input_price_of(current_model)
    if current_input <= 0:
        return None
    best: tuple[float, str] | None = None

    def consider(alias: str, price: float) -> None:
        nonlocal best
        if 0 < price < current_input and (best is None or price < best[0]):
            best = (price, alias)

    for backups in failover_map.values():
        for entry in backups:
            alias = entry if isinstance(entry, str) else entry.get("alias")
            if not alias:
                continue
            model_name = alias.split(":", 1)[1] if ":" in alias else alias
            consider(alias, input_price_of(model_name))
    if binding is not None and binding.fallback_models:
        for model_name in binding.fallback_models:
            consider(f"{binding.provider}:{model_name}", input_price_of(model_name))
    return best[1] if best is not None else None


def exhaustion_error(
    *,
    total_cost_usd: float,
    llm_calls: int,
    max_cost_usd: float | None,
    max_llm_calls: int | None,
) -> BudgetExhaustedError | None:
    """硬上限检查：超限返回待抛的异常，未超限返回 ``None``。

    严格大于才判超限：上限本身包含在内（``max_llm_calls=1`` 允许 1 次成功调用）。
    先判成本再判调用次数 —— 两者同时超限时，成本是更能解释「为什么停下」的那个。
    """
    if max_cost_usd is not None and total_cost_usd > max_cost_usd:
        return BudgetExhaustedError(
            kind="cost_usd", current=total_cost_usd, limit=max_cost_usd)
    if max_llm_calls is not None and llm_calls > max_llm_calls:
        return BudgetExhaustedError(
            kind="llm_calls", current=float(llm_calls), limit=float(max_llm_calls))
    return None
