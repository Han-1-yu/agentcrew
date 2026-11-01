"""别名路由策略：候选池构建、按策略选型、用量衰减、健康/冷却重排。

P0-2 有序故障转移、P3-10 权重路由、V7-P1 多模式路由（weighted / rotate /
usage / order）、P2-11 健康感知与失败冷却，全部收敛到本模块。

设计约束：**本模块不持有状态**。四份可变状态（健康记录、冷却表、轮询计数、
用量评分）由 :class:`~agentcrew.models.factory.ModelFactory` 持有，按参数传进来。
理由是生产上它们必须活到整次 run 结束、并被 API / 看板并发读取，而路由决策本身
只是「给定事实算结论」，抽出来之后可以脱离事件循环、脱离 Provider 单测。

``weighted`` 语义（保持 V3-10 原行为）：``unavailable`` 的别名**跳过**，
``degraded`` 的权重**减半**，全部不可用时权重清零并回退到第一个候选 —— 宁可
试一个已知不健康的，也不要在有候选的情况下直接失败。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentcrew.models.config import parse_alias
from agentcrew.models.health import HealthRecord, reorder_cooled_last, reorder_healthy_first

__all__ = [
    "WeightedAlias",
    "build_weighted_pool",
    "failover_candidates",
    "note_usage",
    "parse_entry",
    "preset_fallback_aliases",
    "select",
    "select_rotate",
    "select_usage",
    "usage_score",
]

#: usage 策略的衰减半衰期（秒）—— 借鉴 magpie ``usageHalfLife``。
USAGE_HALF_LIFE_SECS = 3600.0


class WeightedAlias(BaseModel):
    """带权重的别名（P3-10 权重优先级路由）。

    ``weight`` 是相对权重（无需归一化）；:func:`select` 内部归一化后用
    ``random.choices`` 按权重采样。字符串形式的 failover 项等价于 weight=1.0。
    """

    model_config = ConfigDict(extra="forbid")

    alias: str
    weight: float = 1.0


def parse_entry(entry: str | dict[str, Any]) -> WeightedAlias:
    """解析 failover 列表项为 :class:`WeightedAlias`。

    字符串项 → weight=1.0；dict 项 → 读取 ``alias`` 与 ``weight``。
    """
    if isinstance(entry, str):
        return WeightedAlias(alias=entry, weight=1.0)
    return WeightedAlias(
        alias=str(entry["alias"]),
        weight=float(entry.get("weight", 1.0)),
    )


def build_weighted_pool(
    failover_map: Mapping[str, Sequence[str | dict[str, Any]]],
    alias: str,
) -> list[WeightedAlias] | None:
    """构建权重路由池；纯字符串列表（旧格式）或未配置时返回 ``None``。

    池包含 primary（weight=1.0）+ 解析后的 backups，去重保序。返回 ``None``
    表示「走有序故障转移」，这是向后兼容的开关：只有出现 dict 项（显式权重）
    才启用权重路由。
    """
    backups = failover_map.get(alias, [])
    if not backups:
        return None
    if not any(isinstance(entry, dict) for entry in backups):
        return None
    pool: list[WeightedAlias] = [WeightedAlias(alias=alias, weight=1.0)]
    seen: set[str] = {alias}
    for entry in backups:
        weighted = parse_entry(entry)
        if weighted.alias not in seen:
            seen.add(weighted.alias)
            pool.append(weighted)
    return pool


def preset_fallback_aliases(
    alias: str,
    *,
    overrides: Mapping[str, Any],
    custom: Mapping[str, Any],
) -> list[str]:
    """从 preset 的 ``fallback_models`` 派生备用别名（``provider:model``）。"""
    try:
        binding = parse_alias(alias, overrides=overrides, custom=custom)
    except Exception:
        return []
    if not binding.fallback_models:
        return []
    return [f"{binding.provider}:{m}" for m in binding.fallback_models]


def failover_candidates(
    alias: str,
    *,
    failover_map: Mapping[str, Sequence[str | dict[str, Any]]],
    overrides: Mapping[str, Any],
    custom: Mapping[str, Any],
    health: Mapping[str, HealthRecord],
    health_enabled: bool,
    is_cooled: Callable[[str], bool],
) -> list[str]:
    """返回 ``[primary] + 备用别名`` 的尝试顺序（P0-2 故障转移）。

    去重保序：备用列表里若重复出现 primary 或自身重复，只留首次 —— 避免同一个
    不可用 provider 被反复尝试。未配置 failover 时从 preset 的
    ``fallback_models`` 派生，仍为空则只返回 ``[alias]``（行为与无故障转移一致）。

    两级重排，均为「把差的挪到末尾」而非删除：

    1. P2-11 健康感知：健康监控已启用且有探测数据时，``unavailable`` 的别名
       后置；``degraded`` 保持原位。
    2. P0 冷却感知：仍在冷却中的别名后置。

    两种重排都在「全部候选都不健康/冷却」时保持原序 —— 那是最后一搏，
    直接放弃等于把可控的降级变成失败。
    """
    explicit = failover_map.get(alias)
    if explicit:
        backups: Sequence[str | dict[str, Any]] = explicit
    else:
        backups = preset_fallback_aliases(
            alias, overrides=overrides, custom=custom)

    seen: set[str] = {alias}
    result: list[str] = [alias]
    for backup in backups:
        # failover 配置可能含 dict 项（权重格式）；有序路径只取别名部分。
        backup_str = str(backup.get("alias", "")) if isinstance(backup, dict) else str(backup)
        if backup_str and backup_str not in seen:
            seen.add(backup_str)
            result.append(backup_str)

    if health_enabled and health:
        result = reorder_healthy_first(result, health)
    result = reorder_cooled_last(result, is_cooled)
    return result


def usage_score(
    usage: Mapping[str, tuple[float, float]],
    alias: str,
    now: float,
    half_life: float,
) -> float:
    """计算别名的当前衰减使用量（指数衰减，半衰期 ``half_life`` 秒）。

    从未使用过的别名返回 0.0 —— 在 ``usage`` 策略里优先级最高。
    """
    record = usage.get(alias)
    if record is None:
        return 0.0
    tokens, at = record
    decay = 0.5 ** ((now - at) / half_life)
    return tokens * decay


def note_usage(
    usage: dict[str, tuple[float, float]],
    alias: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    now: float | None = None,
) -> None:
    """记录别名的使用量（V7-P1 usage 策略）。

    先把已有值衰减到当前时刻，再累加本次 token；衰减本身在读取时计算，
    因此这里只负责「折现 + 累加」，不需要后台任务。
    """
    total = prompt_tokens + completion_tokens
    if total <= 0:
        total = 1  # 至少记 1，避免「回答了但用量为 0」时不计数
    current = now if now is not None else time.time()
    existing = usage.get(alias)
    if existing is not None:
        decayed = usage_score(usage, alias, current, USAGE_HALF_LIFE_SECS)
        usage[alias] = (decayed + total, current)
    else:
        usage[alias] = (float(total), current)


def select(
    pool: Sequence[WeightedAlias],
    *,
    strategy: str,
    health: Mapping[str, HealthRecord],
    rotate_counters: dict[str, int],
    usage: dict[str, tuple[float, float]],
    rng: random.Random | None = None,
    now: float | None = None,
) -> tuple[str, str]:
    """按策略选择一个别名，返回 ``(alias, reason)``。

    支持 ``weighted`` / ``rotate`` / ``usage`` / ``order`` 四种策略。所有策略
    都先经过健康过滤（``unavailable`` 跳过，``degraded`` 降权），全部不可用时
    回退到第一个候选。

    - ``weighted``（默认）：按权重随机选择；
    - ``rotate``：轮询，每次选下一个别名，均匀分摊；
    - ``usage``：优先选指数衰减后累计 token 最少的别名；
    - ``order``：按列表顺序选第一个健康的别名。
    """
    healthy: list[WeightedAlias] = []
    degraded_aliases: set[str] = set()
    for weighted in pool:
        record = health.get(weighted.alias)
        if record is not None and record.status == "unavailable":
            continue
        if record is not None and record.status == "degraded":
            degraded_aliases.add(weighted.alias)
        healthy.append(weighted)
    candidates = healthy if healthy else list(pool)
    health_adjusted = len(healthy) != len(pool) or bool(degraded_aliases)

    if strategy == "rotate":
        return select_rotate(rotate_counters, candidates, health_adjusted)
    if strategy == "usage":
        return select_usage(usage, candidates, health_adjusted, now=now)
    if strategy == "order":
        suffix = "_health_adjusted" if health_adjusted else ""
        return candidates[0].alias, "order" + suffix

    actual_rng = rng if rng is not None else random.Random()
    weights: list[float] = []
    all_unavailable = len(healthy) == 0 and len(pool) > 0
    for weighted in candidates:
        weight = weighted.weight
        if all_unavailable:
            weight = 0.0  # 全部不可用时权重清零，走 fallback_first
        elif weighted.alias in degraded_aliases:
            weight = weight * 0.5
        weights.append(weight)
    if sum(weights) <= 0:
        return candidates[0].alias, "fallback_first"
    chosen = actual_rng.choices(candidates, weights=weights, k=1)[0]
    reason = "health_adjusted" if health_adjusted else "weighted"
    return chosen.alias, reason


def select_rotate(
    counters: dict[str, int],
    pool: Sequence[WeightedAlias],
    health_adjusted: bool,
) -> tuple[str, str]:
    """轮询策略：按池标识维护计数器，每次选下一个别名。"""
    pool_key = ",".join(weighted.alias for weighted in pool)
    index = counters.get(pool_key, 0) % len(pool)
    chosen = pool[index].alias
    counters[pool_key] = (index + 1) % len(pool)
    reason = "rotate" + ("_health_adjusted" if health_adjusted else "")
    return chosen, reason


def select_usage(
    usage: Mapping[str, tuple[float, float]],
    pool: Sequence[WeightedAlias],
    health_adjusted: bool,
    *,
    now: float | None = None,
) -> tuple[str, str]:
    """usage 策略：选指数衰减后累计 token 最少的别名。"""
    current = now if now is not None else time.time()
    best_alias = pool[0].alias
    best_score = float("inf")
    for weighted in pool:
        score = usage_score(usage, weighted.alias, current, USAGE_HALF_LIFE_SECS)
        if score < best_score:
            best_score = score
            best_alias = weighted.alias
    reason = "usage" + ("_health_adjusted" if health_adjusted else "")
    return best_alias, reason
