"""多窗口配额管理（V7-P2，借鉴 magpie internal/budget）。

与现有全局预算（``max_cost_usd`` / ``max_llm_calls``）的区别：

- **时间窗口**：按日 / 周 / 月滚动重置，而不是 run 生命周期内累计；
- **按别名独立**：每个 provider/model 别名有自己的配额，适合多 key 池化时
  给每个 key 设独立限额（如免费额度 50 请求/天）；
- **调用前检查**：配额耗尽时直接拒绝（抛 ``QuotaExhaustedError``），而不是
  先调用再记账。

配额状态持久化到磁盘（JSON），跨 run 保留。窗口边界按本地自然日/周/月计算。

使用方式（工厂内部）::

    tracker = QuotaTracker(quota_dir, quotas)
    if not tracker.allow(alias):
        raise QuotaExhaustedError(...)
    result = await provider.complete(...)
    tracker.consume(alias, result.prompt_tokens, result.completion_tokens, cost)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class QuotaExhaustedError(Exception):
    """配额耗尽异常。

    Attributes:
        alias: 耗尽配额的别名。
        window: 耗尽的窗口（daily/weekly/monthly）。
        limit: 限额值。
        used: 已使用值。
        metric: 耗尽的指标（tokens/cost/calls）。
    """

    def __init__(
        self,
        alias: str,
        window: str,
        metric: str,
        limit: float,
        used: float,
    ) -> None:
        self.alias = alias
        self.window = window
        self.metric = metric
        self.limit = limit
        self.used = used
        super().__init__(
            f"配额耗尽：{alias} 在 {window} 窗口内 {metric} 已用 {used:.2f}/{limit:.2f}"
        )


@dataclass
class QuotaLimit:
    """单个时间窗口的配额限制。

    所有字段均为 ``None`` 表示该窗口不限制。
    """

    max_tokens: int | None = None
    """该窗口内最大 token 数（prompt + completion）。"""
    max_cost_usd: float | None = None
    """该窗口内最大成本（USD）。"""
    max_calls: int | None = None
    """该窗口内最大调用次数。"""

    def is_empty(self) -> bool:
        return all(v is None for v in (self.max_tokens, self.max_cost_usd, self.max_calls))


@dataclass
class AliasQuota:
    """单个别名的配额配置（三个时间窗口）。"""

    daily: QuotaLimit = field(default_factory=QuotaLimit)
    weekly: QuotaLimit = field(default_factory=QuotaLimit)
    monthly: QuotaLimit = field(default_factory=QuotaLimit)

    def is_empty(self) -> bool:
        return self.daily.is_empty() and self.weekly.is_empty() and self.monthly.is_empty()


@dataclass
class _WindowUsage:
    """单个窗口的使用量记录。"""

    tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    window_start: float = 0.0  # epoch seconds，窗口起始时间

    def reset_if_needed(self, window: str, now: float) -> None:
        """如果当前时间已超出窗口范围，重置使用量。"""
        start = _window_start(window, now)
        if self.window_start != start:
            self.tokens = 0
            self.cost_usd = 0.0
            self.calls = 0
            self.window_start = start


def _window_start(window: str, now: float) -> float:
    """计算当前时间所在窗口的起始时间（epoch seconds）。"""
    dt = datetime.fromtimestamp(now)
    if window == "daily":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window == "weekly":
        # 周一为一周开始
        start = (dt - timedelta(days=dt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
    elif window == "monthly":
        start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"未知窗口类型: {window}")
    return start.timestamp()


class QuotaTracker:
    """多窗口配额追踪器。

    维护每个别名在 daily/weekly/monthly 三个窗口的使用量，持久化到 JSON 文件。
    线程不安全，但在 asyncio 单线程模型下无需加锁。
    """

    def __init__(
        self,
        state_path: Path | None = None,
        quotas: dict[str, AliasQuota] | None = None,
    ) -> None:
        self._state_path = Path(state_path) if state_path else None
        self._quotas = quotas or {}
        # alias → {window → _WindowUsage}
        self._usage: dict[str, dict[str, _WindowUsage]] = {}
        # V7 质量修复：in-flight 预扣量（alias → {window → _WindowUsage}）
        # 借鉴 magpie budget 的 reservation 机制，防止并发请求同时通过检查
        self._reserved: dict[str, dict[str, _WindowUsage]] = {}
        self._loaded = False

    def _load(self) -> None:
        """从磁盘加载使用量状态。"""
        if self._loaded or self._state_path is None:
            self._loaded = True
            return
        if not self._state_path.exists():
            self._loaded = True
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            for alias, windows in data.get("usage", {}).items():
                self._usage[alias] = {
                    w: _WindowUsage(
                        tokens=v.get("tokens", 0),
                        cost_usd=v.get("cost_usd", 0.0),
                        calls=v.get("calls", 0),
                        window_start=v.get("window_start", 0.0),
                    )
                    for w, v in windows.items()
                }
        except (json.JSONDecodeError, OSError):
            # 损坏的状态文件不影响运行，从头开始计数
            pass
        self._loaded = True

    def _save(self) -> None:
        """持久化使用量状态到磁盘。"""
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data: dict[str, Any] = {
                "usage": {
                    alias: {
                        w: {
                            "tokens": u.tokens,
                            "cost_usd": u.cost_usd,
                            "calls": u.calls,
                            "window_start": u.window_start,
                        }
                        for w, u in windows.items()
                    }
                    for alias, windows in self._usage.items()
                },
                "saved_at": time.time(),
            }
            self._state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # 持久化失败不影响运行时配额检查
            pass

    def _get_usage(self, alias: str, window: str) -> _WindowUsage:
        """获取别名在指定窗口的使用量，必要时初始化和重置。"""
        self._load()
        if alias not in self._usage:
            self._usage[alias] = {}
        if window not in self._usage[alias]:
            self._usage[alias][window] = _WindowUsage(
                window_start=_window_start(window, time.time()))
        usage = self._usage[alias][window]
        usage.reset_if_needed(window, time.time())
        return usage

    def has_quota(self, alias: str) -> bool:
        """该别名是否配置了任何配额。"""
        return alias in self._quotas and not self._quotas[alias].is_empty()

    def check(self, alias: str) -> None:
        """检查别名是否还有配额。耗尽时抛 :class:`QuotaExhaustedError`。

        依次检查 daily → weekly → monthly，第一个耗尽的窗口抛出异常。
        未配置配额的别名直接通过。

        V7 质量修复：检查时将 in-flight 预扣量计入已使用量，防止并发请求
        同时通过检查导致超配额（借鉴 magpie budget reservation）。
        """
        if not self.has_quota(alias):
            return
        quota = self._quotas[alias]
        now = time.time()
        for window, limit in (
            ("daily", quota.daily),
            ("weekly", quota.weekly),
            ("monthly", quota.monthly),
        ):
            if limit.is_empty():
                continue
            usage = self._get_usage(alias, window)
            reserved = self._get_reserved(alias, window)
            total_tokens = usage.tokens + reserved.tokens
            total_cost = usage.cost_usd + reserved.cost_usd
            total_calls = usage.calls + reserved.calls
            if limit.max_tokens is not None and total_tokens >= limit.max_tokens:
                raise QuotaExhaustedError(
                    alias, window, "tokens", limit.max_tokens, total_tokens)
            if limit.max_cost_usd is not None and total_cost >= limit.max_cost_usd:
                raise QuotaExhaustedError(
                    alias, window, "cost_usd", limit.max_cost_usd, total_cost)
            if limit.max_calls is not None and total_calls >= limit.max_calls:
                raise QuotaExhaustedError(
                    alias, window, "calls", limit.max_calls, total_calls)

    def reserve(
        self,
        alias: str,
        estimated_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """预扣预计用量（请求发出前调用）。

        预扣量会在 check 时计入已使用量，防止并发请求同时通过检查。
        请求完成后应调用 :meth:`settle` 用实际用量结算，或调用
        :meth:`release` 释放预扣（请求失败时）。

        Args:
            alias: 别名。
            estimated_tokens: 预计消耗的 token 数（prompt + completion）。
            estimated_cost_usd: 预计成本（USD）。
        """
        if not self.has_quota(alias):
            return
        for window in ("daily", "weekly", "monthly"):
            reserved = self._get_reserved(alias, window)
            reserved.tokens += max(0, estimated_tokens)
            reserved.cost_usd += max(0.0, estimated_cost_usd)
            reserved.calls += 1

    def release(
        self,
        alias: str,
        estimated_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """释放预扣量（请求失败/未实际消耗时调用）。

        Args:
            alias: 别名。
            estimated_tokens: 之前预扣的 token 数。
            estimated_cost_usd: 之前预扣的成本。
        """
        if not self.has_quota(alias):
            return
        for window in ("daily", "weekly", "monthly"):
            reserved = self._get_reserved(alias, window)
            reserved.tokens = max(0, reserved.tokens - max(0, estimated_tokens))
            reserved.cost_usd = max(0.0, reserved.cost_usd - max(0.0, estimated_cost_usd))
            reserved.calls = max(0, reserved.calls - 1)

    def settle(
        self,
        alias: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float = 0.0,
        estimated_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """结算实际用量：先释放预扣，再记录实际使用量。

        等价于 ``release(estimated)`` + ``consume(actual)``，但原子性更好。

        Args:
            alias: 别名。
            prompt_tokens: 实际 prompt token 数。
            completion_tokens: 实际 completion token 数。
            cost_usd: 实际成本。
            estimated_tokens: 之前预扣的 token 数。
            estimated_cost_usd: 之前预扣的成本。
        """
        if not self.has_quota(alias):
            return
        self.release(alias, estimated_tokens, estimated_cost_usd)
        self.consume(alias, prompt_tokens, completion_tokens, cost_usd)

    def _get_reserved(self, alias: str, window: str) -> _WindowUsage:
        """获取别名在指定窗口的预扣量，必要时初始化。"""
        if alias not in self._reserved:
            self._reserved[alias] = {}
        if window not in self._reserved[alias]:
            self._reserved[alias][window] = _WindowUsage(
                window_start=_window_start(window, time.time()))
        reserved = self._reserved[alias][window]
        reserved.reset_if_needed(window, time.time())
        return reserved

    def consume(
        self,
        alias: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float = 0.0,
    ) -> None:
        """记录一次调用的使用量到所有窗口。"""
        if not self.has_quota(alias):
            return
        total_tokens = prompt_tokens + completion_tokens
        now = time.time()
        for window in ("daily", "weekly", "monthly"):
            usage = self._get_usage(alias, window)
            usage.tokens += total_tokens
            usage.cost_usd += cost_usd
            usage.calls += 1
        self._save()

    def remaining(self, alias: str, window: str, metric: str) -> float | None:
        """查询别名在指定窗口的剩余配额。无配额时返回 ``None``。"""
        if not self.has_quota(alias):
            return None
        quota = self._quotas[alias]
        limit = getattr(quota, window)
        limit_val = getattr(limit, f"max_{metric}")
        if limit_val is None:
            return None
        usage = self._get_usage(alias, window)
        used = getattr(usage, metric if metric != "cost_usd" else "cost_usd")
        return max(0.0, limit_val - used)

    def reset(self, alias: str | None = None) -> None:
        """重置使用量。``alias=None`` 时重置所有别名。"""
        if alias is None:
            self._usage.clear()
        elif alias in self._usage:
            del self._usage[alias]
        self._save()


def parse_quota_config(raw: dict[str, Any]) -> dict[str, AliasQuota]:
    """从配置字典解析别名配额。

    配置格式（YAML / dict）::

        quotas:
          openai:gpt-4o-mini:
            daily:
              max_calls: 100
              max_tokens: 100000
            weekly:
              max_cost_usd: 5.0
          anthropic:claude-3-5-sonnet:
            monthly:
              max_calls: 1000
    """
    result: dict[str, AliasQuota] = {}
    for alias, windows in raw.items():
        if not isinstance(windows, dict):
            continue
        quota = AliasQuota()
        for window_name in ("daily", "weekly", "monthly"):
            window_data = windows.get(window_name)
            if isinstance(window_data, dict):
                setattr(quota, window_name, QuotaLimit(
                    max_tokens=window_data.get("max_tokens"),
                    max_cost_usd=window_data.get("max_cost_usd"),
                    max_calls=window_data.get("max_calls"),
                ))
        if not quota.is_empty():
            result[alias] = quota
    return result
