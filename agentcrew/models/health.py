"""Provider 健康与失败冷却：状态记录 + 纯判定函数（P0 增强 / P2-11 / V5 P2）。

本模块只做两件事：

1. 定义健康状态的**数据形状**（:class:`HealthRecord`）与冷却矩阵常量；
2. 提供**不持有状态**的判定函数 —— 冷却时长计算、冷却是否过期、按健康 /
   冷却重排候选别名。

状态本身（``ModelFactory._health`` / ``._cooldowns`` / ``._consecutive_failures``）
仍然由 :class:`~agentcrew.models.factory.ModelFactory` 持有并注入。这是刻意的：
探测动作要调用 Provider，属工厂的职责；本模块只回答「给定这些事实，结论是什么」，
因此可以脱离事件循环、脱离 Provider 单独测。所有函数都接受显式的状态字典或
标量参数，不读全局、不写回。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "COOLDOWN_402_MIN",
    "COOLDOWN_429_BASE_MIN",
    "COOLDOWN_429_MAX_MIN",
    "COOLDOWN_404_MIN",
    "COOLDOWN_5XX_BASE_MIN",
    "COOLDOWN_5XX_MAX_MIN",
    "COOLDOWN_DEFAULT_MIN",
    "HealthRecord",
    "HealthStatus",
    "cooldown_deadline",
    "cooldown_minutes",
    "is_cooled_down",
    "reorder_cooled_last",
    "reorder_healthy_first",
]

#: Provider 健康状态：healthy = 正常快速；degraded = 正常但慢（>=2s）；
#: unavailable = 不可用（异常/超时）。
HealthStatus = Literal["healthy", "degraded", "unavailable"]


class HealthRecord(BaseModel):
    """单个 Provider 别名的健康状态记录（P2-11 主动健康监控）。

    由后台探测任务定期更新，failover 路由据此跳过不健康的别名。
    ``consecutive_failures`` 记录连续失败次数，便于诊断抖动 Provider。
    """

    model_config = ConfigDict(extra="forbid")

    alias: str
    status: HealthStatus
    latency_ms: float | None = None
    last_check: datetime
    error: str | None = None
    consecutive_failures: int = 0
    #: V5 P2：流式探测结果（None = 未做过流式检测）。
    #: 由 ``ModelFactory.health_check_stream`` 按需更新；普通 ``health_check``
    #: 不触碰此字段，两者独立维护。
    stream_ok: bool | None = None


# ── 冷却矩阵（借鉴 Magpie routing.go restAfterMarked）────────────────────────
#: 402 余额不足冷却时长（分钟）—— creditRest。
COOLDOWN_402_MIN = 30
#: 429 限流基础冷却（分钟）—— 无 Retry-After 时的起步值。
COOLDOWN_429_BASE_MIN = 1
#: 429 限流冷却上限（分钟）—— 指数退避封顶。
COOLDOWN_429_MAX_MIN = 30
#: 5xx 瞬时故障基础冷却（分钟）。
COOLDOWN_5XX_BASE_MIN = 1
#: 5xx 瞬时故障冷却上限（分钟）。
COOLDOWN_5XX_MAX_MIN = 10
#: 404 模型不存在冷却（分钟）—— 只冷却该模型键，不冷却整个 provider。
COOLDOWN_404_MIN = 1
#: 其他未分类瞬时故障冷却（分钟）。
COOLDOWN_DEFAULT_MIN = 1


def cooldown_minutes(
    status_code: int | None,
    consecutive_failures: int,
    *,
    retry_after: float | None = None,
) -> float:
    """按失败分类算出本次冷却时长（分钟）。

    - 402 余额不足 → 固定 30 分钟（``creditRest``）；
    - 429 限流 → 优先 ``Retry-After``，否则 1 分钟起步指数退避（封顶 30 分钟）；
    - 5xx → 1 分钟起步指数退避（封顶 10 分钟）；
    - 404 模型不存在 → 冷却模型级键（alias 本身即 ``provider:model``），1 分钟；
    - 其他瞬时故障 → 1 分钟。

    ``consecutive_failures`` 是**本次失败后的连续失败次数**（首次为 1），指数
    退避以它为指数底数；调用方负责维护计数。
    """
    if status_code == 402:
        return float(COOLDOWN_402_MIN)
    if status_code == 429:
        if retry_after is not None:
            return retry_after / 60.0
        return float(min(
            COOLDOWN_429_BASE_MIN * (2 ** (consecutive_failures - 1)),
            COOLDOWN_429_MAX_MIN,
        ))
    if status_code is not None and 500 <= status_code < 600:
        return float(min(
            COOLDOWN_5XX_BASE_MIN * (2 ** (consecutive_failures - 1)),
            COOLDOWN_5XX_MAX_MIN,
        ))
    if status_code == 404:
        return float(COOLDOWN_404_MIN)
    return float(COOLDOWN_DEFAULT_MIN)


def cooldown_deadline(
    status_code: int | None,
    consecutive_failures: int,
    *,
    retry_after: float | None = None,
    now: datetime | None = None,
) -> datetime:
    """冷却到期时刻（``now + cooldown_minutes(...)``）。"""
    start = now if now is not None else datetime.now()
    return start + timedelta(
        minutes=cooldown_minutes(
            status_code, consecutive_failures, retry_after=retry_after))


def is_cooled_down(
    cooldowns: dict[str, datetime],
    alias: str,
    *,
    now: datetime | None = None,
) -> bool:
    """``alias`` 是否仍在冷却中；冷却已过期则**惰性删除**该条目并返回 ``False``。"""
    until = cooldowns.get(alias)
    if until is None:
        return False
    current = now if now is not None else datetime.now()
    if current >= until:
        cooldowns.pop(alias, None)
        return False
    return True


def reorder_healthy_first(
    aliases: Sequence[str],
    health: Mapping[str, HealthRecord],
) -> list[str]:
    """把 ``unavailable`` 的别名移到列表末尾（``healthy`` / ``degraded`` 保持原位）。

    全部不可用时**保持原序** —— 那是最后一搏，重排没有意义。
    """
    healthy: list[str] = []
    unavailable: list[str] = []
    for alias in aliases:
        record = health.get(alias)
        if record is not None and record.status == "unavailable":
            unavailable.append(alias)
        else:
            healthy.append(alias)
    if unavailable and healthy:
        return healthy + unavailable
    return list(aliases)


def reorder_cooled_last(
    aliases: Iterable[str],
    is_cooled: Callable[[str], bool],
) -> list[str]:
    """把仍在冷却中的别名移到列表末尾；全部冷却中时保持原序（最后一搏）。"""
    active: list[str] = []
    cooled: list[str] = []
    for alias in aliases:
        if is_cooled(alias):
            cooled.append(alias)
        else:
            active.append(alias)
    if cooled and active:
        return active + cooled
    return list(aliases)
