"""ModelFactory：别名 → Provider 实例（缓存）+ 全局 LLM 调用限流与记账。

本模块是**模型层的组合点**：它持有一次 run 的全部可变状态（Provider 缓存、
用量计数、健康记录、冷却表、路由计数、配额与亲和性追踪），并把四类**无状态
策略**装配起来 —— 路由（``models/routing.py``）、健康与冷却（``models/health.py``）、
预算（``models/budget.py``）、异常层次（``models/errors.py``）。策略模块只接收
参数、不持有状态，因此能脱离事件循环与 Provider 单测；状态留在这里，因为它要
活到整次 run 结束，并被 API / 看板并发读取。

``call()`` / ``complete_structured()`` 共用一条「候选计划 → 单次尝试」的调用
管线（早先四个入口各自抄了一遍同段逻辑，任何一处修复都得改四遍）：

- **候选计划**（:class:`_CandidatePlan`）：有序故障转移（P0-2）或权重路由
  （P3-10 / V7-P1），决定「下一个该试谁」；
- **单次尝试**（:meth:`ModelFactory._attempt` / :meth:`._structured_attempt`）：
  配额预扣 → 响应缓存 → 信号量 → 脱敏 → 调用 → 记账，失败时释放预扣并上抛。

各入口的能力差异由计划形态与显式参数决定，而不是在各处复制条件分支：响应缓存
与会话亲和性**只作用于有序故障转移路径** —— 权重路由的语义是「按权重分摊流量」，
缓存命中会让某个别名永久承担全部请求，破坏分摊；``stream_call`` 保持独立循环，
因为「首个 chunk 之后不能回滚、因此不能故障转移」是生成器语义特有的约束。

spec §7.3 的两级并发限额里，任务槽位由 Scheduler 持有，本模块持有 **LLM 调用级
并发限额**（全局信号量 + per-provider 信号量 / RPM）。两者刻意不嵌套：门禁在任务
槽位释放之后才执行，而调用级信号量只在 ``complete()`` 期间持有，所以不存在
「持 A 等 B」的循环等待，不可能死锁。

其余设计要点：

- ``--mock``（``settings.mock``）是**全局**开关：所有别名一律解析为
  :class:`MockProvider`（用 ``mock`` 预设的能力位），无需任何 API Key。
- API Key 只从环境读取（spec §17.2），缺 Key 时在构造 Provider 的当场抛
  :class:`ModelFactoryError` 并点名环境变量，绝不静默降级成无鉴权调用；
  ``ollama`` 是本地服务，一律用占位串 ``"ollama"``。
- 记账：``call()`` 在信号量**释放之后**累加计数、用 ``estimate_cost`` 估算成本，
  再发一条 ``llm_call`` 事件（spec §13.2 的七个字段）。
- **结构化输出也走同一个受治理入口**：``complete_structured()`` 在工厂内解析别名、
  独占同一把信号量并复用同一段记账 —— PM 拆解与 G2 评审（spec §7.3 明确计入
  全局限额、§14.3 的报告要汇总 token/成本）经它调用，不会绕过限流与审计。
  ``models/structured.py`` 的 ``complete_structured`` 保持纯阶梯，仍可用任意
  Provider 独立测试。
- **降级默认落账**（spec §12.3）：``complete_structured()`` 的 ``on_degrade``
  缺省接本工厂的 ``llm_degraded`` 事件处理器 —— 调用方不接线也有诊断可查；
  显式传入回调则替换默认（调用方自行负责留痕）。
- fixture 缺失**绝不静默**（spec §16.1）：``MockProvider`` 的 ``on_fallback``
  接到 ledger 的 ``mock_fallback`` 事件。
- Task 18 的 carry-forward：``Retry-After`` 被截断到 300 秒时，截断事实经
  ``on_retry`` 落到 ledger 的 ``llm_retry`` 事件 —— 操作者看得到「远端要了多久、
  我们实际等多久」，而不是一次无解释的 300 秒等待。

跨层契约：``ledger`` 参数按 :class:`~agentcrew.contracts.EventSink` 协议接收，
任务对象按 :class:`~agentcrew.contracts.TaskLike` 协议接收，因此本模块**不再
import** 运行时编排层（``runtime.ledger``）与任务领域层（``task.task``）——
基础设施层不该反向依赖上层实现。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from agentcrew.config import Settings
from agentcrew.contracts import EventSink, TaskLike
from agentcrew.models import agent_budget, budget, routing, structured
from agentcrew.models.affinity import AffinityTracker
from agentcrew.models.cache import LLMCache
from agentcrew.models.complexity import evaluate as evaluate_complexity
from agentcrew.models.complexity import get_calibrator
from agentcrew.models.config import (
    PROVIDER_PRESETS,
    ModelBinding,
    UnknownProviderError,
    build_directory,
    parse_alias,
)
from agentcrew.models.errors import (
    SUGGESTED_BUDGET_HEADROOM,
    BudgetExhaustedError,
    ContextBudgetError,
    ModelFactoryError,
    ProviderUnavailableError,
)
from agentcrew.models.health import (
    HealthRecord,
    HealthStatus,
    cooldown_deadline,
    is_cooled_down,
)
from agentcrew.models.loop_guard import LoopDetectedError, LoopGuard, UnitRun
from agentcrew.models.pricing import estimate_cost
from agentcrew.models.providers.anthropic import AnthropicProvider
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
    StreamChunk,
)
from agentcrew.models.providers.cli import CliProvider
from agentcrew.models.providers.mock import MockProvider
from agentcrew.models.providers.openai_compat import (
    RETRY_AFTER_CLAMPED_ATTR,
    OpenAICompatProvider,
)
from agentcrew.models.quota import QuotaExhaustedError, QuotaTracker, parse_quota_config
from agentcrew.models.redact import RedactOptions, Redactor
from agentcrew.models.routing import WeightedAlias
from agentcrew.models.structured import DegradeEvent
from agentcrew.utils.retry import TransientError
from agentcrew.utils.tokens import estimate_tokens

if TYPE_CHECKING:
    from agentcrew.models.complexity import ComplexityLevel

__all__ = [
    "DEFAULT_FIXTURES_DIR",
    "BudgetExhaustedError",
    "ContextBudgetError",
    "HealthRecord",
    "HealthStatus",
    "ModelFactory",
    "ModelFactoryError",
    "ProviderUnavailableError",
    "WeightedAlias",
]

_T = TypeVar("_T")

#: 默认 fixture 根目录（仓库根的 ``examples/fixtures``）。模块级常量是契约的
#: 一部分：测试与 Task 34 的 Crew 都靠 monkeypatch 它注入临时 fixtures，
#: 因此构造函数**在实例化时**读取本常量，而不是把它绑成默认参数。
DEFAULT_FIXTURES_DIR: Path = (
    Path(__file__).resolve().parents[2] / "examples" / "fixtures")

#: ollama 是本地服务（spec §17.2 无需密钥），但 SDK 要求 api_key 非空。
_OLLAMA_PLACEHOLDER = "ollama"

#: 支持的 Provider 实现（与 ``config.Implementation`` 的 Literal 对齐）。
_IMPLEMENTATIONS: tuple[str, ...] = ("openai_compat", "anthropic", "mock", "cli")

#: P2-11：健康探测中延迟超过此值（毫秒）视为 degraded。
#: 模块级可 monkeypatch（``test_provider_health`` 用它把阈值压到 0.001ms）。
_HEALTH_DEGRADED_THRESHOLD_MS = 2000.0

#: P2-11：健康探测单次超时（秒）。
_HEALTH_CHECK_TIMEOUT_SECS = 5.0

#: P0-2：触发故障转移的异常类型 —— Provider 内部重试耗尽后的瞬时故障。
#: openai_compat / anthropic 抛 TransientError；cli 抛 TimeoutError / ConnectionError。
_PROVIDER_UNAVAILABLE_TYPES: tuple[type[BaseException], ...] = (
    TransientError, TimeoutError, ConnectionError,
    QuotaExhaustedError,
)


def _level_rank(level: ComplexityLevel) -> int:
    """复杂度级别排序权重（P3-1：判断上调 / 下调）。LOW < MEDIUM < HIGH。"""
    return {"low": 0, "medium": 1, "high": 2}[level.value]


def _estimate_request_tokens(
    messages: Sequence[Message],
    max_tokens: int | None,
) -> int:
    """估算请求的预计 token 用量（用于配额预扣）。

    估算口径：请求体序列化后的字节数 / 4（约 4 bytes/token）+ 预计输出
    （``max_tokens`` 或默认 500）。预扣量偏保守，实际结算时多退少补。

    借鉴 magpie budget 的 reservation 机制：请求体大小 + 平均输出。
    """
    import json as _json
    try:
        body = _json.dumps(
            [m.model_dump() if hasattr(m, "model_dump") else dict(m) for m in messages],
            ensure_ascii=False,
        )
        input_est = max(1, len(body) // 4)
    except Exception:
        input_est = 256
    output_est = max_tokens if max_tokens and max_tokens > 0 else 500
    return input_est + output_est


class _CandidatePlan:
    """候选别名的迭代计划：统一「有序故障转移」与「权重路由」两种来源。

    两种形态对外表现一致（``start()`` 取第一个、``after_failure()`` 取下一个
    或 ``None``），差别只在内部：有序计划按固定顺序前进，权重计划每次从**剩余**
    候选里按策略重选（``unavailable`` 跳过、``degraded`` 降权）。

    「选中」与「宣告」分成两步（:meth:`after_failure` 只选，:meth:`announce`
    才落 ``llm_routed``），好让调用方先发 ``llm_failover`` 再发 ``llm_routed``
    —— 这是历史事件顺序，看板按序回放。

    ``is_ordered`` 同时充当能力开关：只有有序计划启用响应缓存与会话亲和性
    （见模块 docstring）。
    """

    def __init__(
        self,
        *,
        ordered: list[str] | None = None,
        weighted: list[WeightedAlias] | None = None,
        select: Callable[[list[WeightedAlias]], tuple[str, str]] | None = None,
        emit: Callable[[str, list[WeightedAlias], str], None] | None = None,
    ) -> None:
        self._ordered = ordered or []
        self._weighted = weighted
        self._select = select
        self._emit = emit
        self._index = 0
        self._total = len(weighted) if weighted is not None else len(self._ordered)
        self._pending: tuple[str, list[WeightedAlias], str] | None = None

    @property
    def is_ordered(self) -> bool:
        """是否走有序故障转移（``False`` = 权重路由）。"""
        return self._weighted is None

    @property
    def total(self) -> int:
        """候选总数（用于「尝试了 N 个」的错误措辞）。"""
        return self._total

    def start(self) -> str | None:
        """第一个候选；无候选时返回 ``None``（权重路径同时落 ``llm_routed``）。"""
        if self._weighted is None:
            return self._ordered[0] if self._ordered else None
        chosen = self._choose(self._weighted)
        self.announce()
        return chosen

    def after_failure(self, failed: str) -> str | None:
        """``failed`` 之后的下一个候选；已无候选时返回 ``None``。

        只做选择，不落事件 —— 调用方要先发 ``llm_failover`` 再调用
        :meth:`announce`，以保持「先宣告切换、后宣告路由」的事件顺序。
        """
        if self._weighted is None:
            self._index += 1
            if self._index >= len(self._ordered):
                return None
            return self._ordered[self._index]
        remaining = [item for item in self._weighted if item.alias != failed]
        if not remaining:
            self._weighted = []
            return None
        self._weighted = remaining
        return self._choose(remaining)

    def announce(self) -> None:
        """把最近一次权重选择落成 ``llm_routed`` 事件（有序计划无事可做）。"""
        pending = self._pending
        if pending is None or self._emit is None:
            self._pending = None
            return
        chosen, available, reason = pending
        self._pending = None
        self._emit(chosen, available, reason)

    def _choose(self, available: list[WeightedAlias]) -> str:
        assert self._select is not None
        chosen, reason = self._select(list(available))
        self._pending = (chosen, list(available), reason)
        return chosen

    def exhausted_message(self, primary: str, exc: BaseException) -> str:
        """全部候选耗尽时的错误措辞（权重路由与有序故障转移各有一句）。"""
        kind = "故障转移" if self.is_ordered else "权重路由"
        return (
            f"全部{kind}别名均不可用（primary={primary!r}，"
            f"尝试了 {self._total} 个）：{exc}")


class ModelFactory:
    """按别名缓存 Provider，并独占全局 LLM 调用信号量与用量统计。"""

    def __init__(
        self,
        settings: Settings,
        *,
        ledger: EventSink | None = None,
        fixtures_dir: Path | None = None,
        cache: LLMCache | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        # P3-2：可选响应缓存。``None``（默认）时所有 LLM 调用都不缓存，行为与
        # 改造前逐字节一致；只有 ``temperature == 0`` 的确定性调用才走缓存。
        self._resp_cache = cache
        # 模块级常量在构造时读取（而不是绑成默认参数），monkeypatch 才有效。
        self._fixtures_dir = (
            Path(fixtures_dir) if fixtures_dir is not None else DEFAULT_FIXTURES_DIR)
        self._cache: dict[str, BaseProvider] = {}
        # Python 3.10+ 的 Semaphore 不再在构造时绑定事件循环，因此可以在这里
        # 创建；容量即全局 LLM 调用并发上限（spec §7.3）。
        # V6 P0.5：保留全局信号量作为只读属性（向后兼容），实际 call/stream_call/
        # complete_structured 路径改用 per-provider 信号量（_provider_semaphores）。
        self._llm_semaphore = asyncio.Semaphore(settings.max_concurrency)
        # V6 P0.5：per-provider 并发信号量缓存（provider 名 → Semaphore）。
        # 未配置 max_concurrency 的 provider 回退到 settings.max_concurrency。
        self._provider_semaphores: dict[str, asyncio.Semaphore] = {}
        # 多 agent：per-profile 并发信号量（仅当 profile 设了 max_concurrency）。
        self._agent_semaphores: dict[str, asyncio.Semaphore] = {}
        # V6 P0.5：per-provider RPM 滑动窗口时间戳（provider 名 → 单调递增时间戳列表）。
        self._rpm_timestamps: dict[str, list[float]] = {}
        self._llm_calls = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_cost_usd = 0.0
        self._agent_usage: dict[str, dict[str, float]] = {}
        # P2-11：主动健康监控 —— 各别名的健康状态缓存与后台探测任务。
        self._health: dict[str, HealthRecord] = {}
        self._health_task: asyncio.Task | None = None
        # P0 故障转移增强：失败冷却追踪 —— key = alias（或模型级键）。
        # value = 冷却到期时间；过期条目在 _is_cooled_down 里惰性清除。
        self._cooldowns: dict[str, datetime] = {}
        # 连续失败计数（指数退避用）；成功时清零。
        self._consecutive_failures: dict[str, int] = {}
        # P2 增强：预算警告 / 自动降级的一次性状态。``reset_stats`` 一并清零，
        # 让阈值在新 run 重新武装（否则一次 run 警告过后永久沉默）。
        self._budget_warning_emitted = False
        self._budget_downgrade_to: str | None = None
        # V7-P1：多模式路由状态
        # rotate 策略的轮询计数器（pool 标识 → 下一个索引）
        self._rotate_counters: dict[str, int] = {}
        # usage 策略的指数衰减使用量（alias → (tokens, timestamp)）
        self._usage_scores: dict[str, tuple[float, float]] = {}
        # V7-P2：多窗口配额追踪
        self._quota_tracker = QuotaTracker(
            state_path=settings.output_dir / "quota_state.json",
            quotas=parse_quota_config(settings.quotas) if settings.quota_enabled else {},
        )
        # V7-P3：会话亲和性追踪
        self._affinity_tracker = AffinityTracker(
            mode=settings.affinity_mode,
            state_path=settings.output_dir / "affinity_state.json",
            ttl_seconds=settings.affinity_ttl_days * 86400,
        )

    # ── 只读视图 ──────────────────────────────────────────────────────────

    @property
    def settings(self) -> Settings:
        """生效配置（BaseAgent 解析角色级模型绑定的三级优先级要用）。

        P1-6：``set_default_alias`` / ``set_role_binding`` 会就地替换它 ——
        frozen Settings 经 ``model_copy`` 出新版，引用持有者经本属性看到新值。
        """
        return self._settings

    @property
    def llm_semaphore(self) -> asyncio.Semaphore:
        """LLM 调用级信号量，容量 = ``settings.max_concurrency``。"""
        return self._llm_semaphore

    @property
    def llm_calls(self) -> int:
        """已成功完成的 LLM 调用数。"""
        return self._llm_calls

    @property
    def total_prompt_tokens(self) -> int:
        """累计输入 token（以 API/估算返回的 usage 为准）。"""
        return self._total_prompt_tokens

    @property
    def total_completion_tokens(self) -> int:
        """累计输出 token。"""
        return self._total_completion_tokens

    @property
    def agent_usage(self) -> dict[str, dict[str, float]]:
        """按执行体拆分的用量（profile → calls/cost/tokens）。单 agent 时为空。"""
        return {k: dict(v) for k, v in self._agent_usage.items()}

    @property
    def total_cost_usd(self) -> float:
        """累计估算成本（USD）；未知模型按 0 计但 token 照记。"""
        return self._total_cost_usd

    def reset_stats(self) -> None:
        """清零本实例的调用数、token 总数与成本。"""
        self._llm_calls = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_cost_usd = 0.0
        # P2 增强：预算警告 / 降级状态随计数器一起复位，新 run 重新武装阈值。
        self._budget_warning_emitted = False
        self._budget_downgrade_to = None

    # ── P1-6：运行时切换模型绑定 ───────────────────────────────────────────

    def set_default_alias(self, alias: str) -> None:
        """运行时切换默认模型别名（``settings.default_model``）。

        用 ``model_copy`` 出一份新 Settings 就地替换 ``self._settings``（frozen
        对象不可变，只能换引用）；清空 provider 缓存 —— 旧实例绑定了旧配置
        （api_key/base_url/capabilities），不清的话下次 ``get()`` 仍命中旧实例。
        切换前后记一条 ``llm_model_switched`` 事件（含 from/to/scope="default"）。
        别名本身过 ``parse_alias`` 校验，非法别名当场抛 ``ModelFactoryError``。
        """
        old = self._settings.default_model
        if alias == old:
            return
        self._validate_alias(alias)
        self._settings = self._settings.model_copy(update={"default_model": alias})
        self._cache.clear()
        self._provider_semaphores.clear()
        self._agent_semaphores.clear()
        self._rpm_timestamps.clear()
        self._emit_model_switched(old, alias, scope="default")

    def set_role_binding(self, role: str, alias: str) -> None:
        """运行时设置角色级模型绑定（``settings.role_bindings[role]``）。

        与 :meth:`set_default_alias` 同形：合并进新 ``role_bindings``、清缓存、
        记 ``llm_model_switched``（``scope=f"role:{role}"``）。
        """
        old_bindings = dict(self._settings.role_bindings)
        old = old_bindings.get(role)
        if old == alias:
            return
        self._validate_alias(alias)
        old_bindings[role] = alias
        self._settings = self._settings.model_copy(
            update={"role_bindings": old_bindings})
        self._cache.clear()
        self._provider_semaphores.clear()
        self._agent_semaphores.clear()
        self._rpm_timestamps.clear()
        self._emit_model_switched(old, alias, scope=f"role:{role}")

    def _validate_alias(self, alias: str) -> None:
        """``parse_alias`` 校验 + 把解析异常统一收敛成 ``ModelFactoryError``。"""
        try:
            parse_alias(
                alias,
                overrides=self._settings.provider_overrides,
                custom=self._settings.custom_providers,
            )
        except (ValueError, UnknownProviderError) as exc:
            raise ModelFactoryError(f"非法模型别名 {alias!r}: {exc}") from exc

    def current_bindings(self) -> dict[str, Any]:
        """当前生效的模型绑定（默认 + 角色级），API / CLI / Dashboard 共用。"""
        return {
            "default": self._settings.default_model,
            "role_bindings": dict(self._settings.role_bindings),
        }

    def _emit_model_switched(
        self, from_alias: str | None, to_alias: str, *, scope: str
    ) -> None:
        """记一条 ``llm_model_switched`` 事件（P1-6：诚实留痕）。"""
        if self._ledger is None:
            return
        self._ledger.emit(
            "llm_model_switched",
            from_alias=from_alias,
            to_alias=to_alias,
            scope=scope,
        )

    # ── P2-11：主动健康监控 ────────────────────────────────────────────────

    async def health_check(self, alias: str) -> HealthRecord:
        """对单个别名做一次健康探测，返回并缓存结果。

        探测方式：发一条 "ping" 消息（max_tokens=1），5 秒超时。
        成功且延迟 < 2000ms → healthy；成功但 >= 2000ms → degraded；
        失败（异常/超时）→ unavailable，consecutive_failures++。
        MockProvider 直接返回 healthy（不真实调用）。
        每次探测都记 ledger 事件 ``provider_health``。
        """
        provider = self.get(alias)

        # MockProvider 无需真实探测，直接标记 healthy。
        if isinstance(provider, MockProvider):
            record = HealthRecord(
                alias=alias,
                status="healthy",
                latency_ms=0.0,
                last_check=datetime.now(),
                consecutive_failures=0,
            )
            self._health[alias] = record
            self._emit_health(record)
            return record

        start = time.perf_counter()
        try:
            await asyncio.wait_for(
                provider.complete(
                    [{"role": "user", "content": "ping"}],
                    temperature=0,
                    max_tokens=1,
                ),
                timeout=_HEALTH_CHECK_TIMEOUT_SECS,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            status: HealthStatus = (
                "healthy" if elapsed_ms < _HEALTH_DEGRADED_THRESHOLD_MS else "degraded"
            )
            record = HealthRecord(
                alias=alias,
                status=status,
                latency_ms=elapsed_ms,
                last_check=datetime.now(),
                consecutive_failures=0,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            prev = self._health.get(alias)
            consecutive = (prev.consecutive_failures + 1) if prev is not None else 1
            record = HealthRecord(
                alias=alias,
                status="unavailable",
                latency_ms=elapsed_ms,
                last_check=datetime.now(),
                error=f"{type(exc).__name__}: {exc}",
                consecutive_failures=consecutive,
            )

        self._health[alias] = record
        self._emit_health(record)
        return record

    async def health_check_stream(self, alias: str) -> HealthRecord:
        """对单个别名做**流式探测**，返回并缓存结果（V5 P2）。

        与 :meth:`health_check` 的区别：

        - 走 :mod:`agentcrew.models.stream_check` 发一个 ``stream=true`` 的最小
          补全请求，只验证 SSE 第一个 ``data:`` 帧能否到达；
        - **只更新** ``HealthRecord.stream_ok``，不改变 ``status`` /
          ``latency_ms`` / ``consecutive_failures``（那些由普通 health_check
          维护）。若该别名还没有健康记录，则新建一条（status 按流式结果推断）。
        - mock/cli 实现由 stream_check 直接短路为 ok=True，无需 API Key。
        """
        from agentcrew.models import stream_check

        # 解析 binding + 取 API Key（与 health_check 同一套配置来源）。
        try:
            binding = parse_alias(
                alias,
                overrides=self._settings.provider_overrides,
                custom=self._settings.custom_providers,
            )
            api_key = self._require_api_key(binding)
        except Exception as exc:
            # 配置错误（缺 Key、非法别名等）：落成 unavailable + stream_ok=False，
            # 与普通 health_check 的失败路径同形，便于 /test 端点统一读取。
            record = HealthRecord(
                alias=alias,
                status="unavailable",
                latency_ms=None,
                last_check=datetime.now(),
                error=f"{type(exc).__name__}: {exc}",
                stream_ok=False,
            )
            self._health[alias] = record
            return record

        result = await stream_check.check_streaming(
            binding, api_key=api_key)

        existing = self._health.get(alias)
        if existing is not None:
            # 已有普通探测记录：只补丁 stream_ok，其余字段原样保留。
            record = existing.model_copy(update={"stream_ok": result["ok"]})
        else:
            record = HealthRecord(
                alias=alias,
                status="healthy" if result["ok"] else "unavailable",
                latency_ms=result["latency_ms"],
                last_check=datetime.now(),
                error=result["error"],
                stream_ok=result["ok"],
            )
        self._health[alias] = record
        return record

    async def _health_loop(self) -> None:
        """后台健康探测循环：每隔 interval 秒检查所有已缓存别名。"""
        while True:
            try:
                await asyncio.sleep(self._settings.health_check_interval_secs)
            except asyncio.CancelledError:
                return
            for alias in list(self._cache.keys()):
                try:
                    await self.health_check(alias)
                except Exception:
                    # health_check 内部已捕获并记录错误；这里再兜一层防止循环崩溃。
                    pass

    def start_health_monitoring(self) -> None:
        """启动后台健康探测任务（如果配置启用且尚未运行）。"""
        if not self._settings.health_check_enabled:
            return
        if self._health_task is not None and not self._health_task.done():
            return
        self._health_task = asyncio.create_task(self._health_loop())

    def stop_health_monitoring(self) -> None:
        """取消后台健康探测任务（可重复调用）。"""
        if self._health_task is not None:
            self._health_task.cancel()
            self._health_task = None

    def get_health(self, alias: str) -> HealthRecord | None:
        """查询某别名的最新健康状态；未探测过返回 None。"""
        return self._health.get(alias)

    def _emit_health(self, record: HealthRecord) -> None:
        """记一条 ``provider_health`` 事件（P2-11：诚实留痕）。"""
        if self._ledger is None:
            return
        self._ledger.emit(
            "provider_health",
            alias=record.alias,
            status=record.status,
            latency_ms=record.latency_ms,
            error=record.error,
            consecutive_failures=record.consecutive_failures,
        )

    # ── 解析与缓存 ────────────────────────────────────────────────────────

    def get(self, alias: str) -> BaseProvider:
        """按别名返回 Provider；同别名重复调用返回同一实例。"""
        provider = self._cache.get(alias)
        if provider is None:
            provider = self._resolve(alias)
            self._cache[alias] = provider
        return provider

    def alias_for_task(self, task: TaskLike) -> str:
        """按任务复杂度选模型别名（V2：按认知复杂度自动选模型）。

        优先级（高者胜）：
        1. 角色级 ``RoleSpec.model``（由调用方在 BaseAgent 里判断，本方法不感知）
        2. ``settings.role_bindings[role]``
        3. ``settings.model_tiers[<复杂度>]``（V2 新增，未配置则跳过）
        4. ``settings.default_model``

        本方法只负责 2–4 级；第 1 级由 BaseAgent 在调用前判断。
        mock 模式下仍返回有效别名（``_resolve`` 内部会统一替换为 MockProvider）。

        P3-1：若全局校准器已注入，第 3 级的复杂度用 ``calibrator.calibrate()``
        替代静态评估；级别被上调/下调时记 ``complexity_calibrated`` 事件（含
        task_id / original_level / calibrated_level / reason）。未注入时行为
        与改动前完全一致（向后兼容）。
        """
        role_binding = self._settings.role_bindings.get(task.role)
        if role_binding:
            return role_binding
        original_level = evaluate_complexity(task)
        calibrator = get_calibrator()
        if calibrator is not None:
            level = calibrator.calibrate(task)
            if level is not original_level:
                reason = (
                    "upgraded" if _level_rank(level) > _level_rank(original_level)
                    else "downgraded")
                self._emit_calibrated(task, original_level, level, reason)
        else:
            level = original_level
        tier = self._settings.model_tiers.get(level.value)
        if tier:
            return tier
        return self._settings.default_model

    def _emit_calibrated(
        self,
        task: TaskLike,
        original: ComplexityLevel,
        calibrated: ComplexityLevel,
        reason: str,
    ) -> None:
        """记一条 ``complexity_calibrated`` 事件（P3-1：诚实留痕）。"""
        if self._ledger is None:
            return
        self._ledger.emit(
            "complexity_calibrated",
            task_id=task.id,
            original_level=original.value,
            calibrated_level=calibrated.value,
            reason=reason,
        )

    def _resolve(self, alias: str) -> BaseProvider:
        """解析别名并构造 Provider（**不加缓存**；单独抽出便于测试 monkeypatch）。

        分派顺序：``parse_alias``（含 provider_overrides）→ ``--mock`` 全局覆盖
        → 按 implementation 构造。缺 Key 在这里当场报错，绝不静默。
        """
        try:
            binding = parse_alias(
                alias,
                overrides=self._settings.provider_overrides,
                custom=self._settings.custom_providers,
            )
        except UnknownProviderError:
            raise
        except ValueError as exc:
            # pydantic ValidationError 是 ValueError 子类；非法 implementation 等
            # schema 违例在这里统一收敛成 ModelFactoryError（配置错误）。
            raise ModelFactoryError(f"非法模型别名 {alias!r}: {exc}") from exc
        implementation = (
            "mock" if self._settings.mock
            else self._implementation_for(binding))
        if implementation == "mock":
            binding = self._as_mock_binding(binding)
            return MockProvider(
                binding,
                fixtures_dir=self._fixtures_dir,
                latency_ms=self._settings.mock_latency_ms,
                on_fallback=(
                    self._on_mock_fallback if self._ledger is not None else None),
            )
        if implementation == "cli":
            return CliProvider(binding)
        api_key = self._require_api_key(binding)
        if implementation == "anthropic":
            return AnthropicProvider(
                binding, api_key=api_key, on_retry=self._on_retry)
        return OpenAICompatProvider(
            binding, api_key=api_key, on_retry=self._on_retry)

    def validate_context_budget(self, bindings: Mapping[str, str]) -> None:
        """校验每个实际绑定的模型能否容下上下文预算（spec §11.2，退出码 3）。

        同一 run 内不同角色可能绑不同模型，因此逐个 alias 校验 —— 默认模型窗口
        再大，也拦得住某个小窗口的本地模型。
        """
        for alias in bindings.values():
            provider = self._resolve(alias)
            capabilities = provider.capabilities
            budget_tokens = self._settings.context_budget_tokens
            fits = (
                budget_tokens + capabilities.max_output_tokens
                <= capabilities.max_context_tokens)
            if fits:
                continue
            raise ContextBudgetError(
                alias=alias,
                budget=budget_tokens,
                window=capabilities.max_context_tokens,
                suggested_budget=(
                    capabilities.max_context_tokens
                    - capabilities.max_output_tokens
                    - SUGGESTED_BUDGET_HEADROOM),
            )

    # ── 调用入口 ──────────────────────────────────────────────────────────

    async def call(
        self,
        alias: str,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        purpose: str,
        role: str,
        response_format: ResponseFormat | None = None,
        session_id: str | None = None,
    ) -> CompletionResult:
        """取信号量 → 调用 Provider → 释放 → 记账并落 ledger。

        信号量只包住 ``complete()``：记账与落盘是本地小操作，占着全局调用额度
        只会无谓拉长排队。

        P0-2：Provider 不可用（503/429/超时/连接错误，重试耗尽后）时按
        ``model_failover`` 列表依次尝试备用别名，每次切换记 ``llm_failover`` 事件。
        故障转移不消耗任务 attempts（那是执行失败，不是模型不可用）。

        P3-10：如果 failover 配置使用权重格式（含 dict 项），按权重随机选择别名，
        失败时从剩余别名按剩余权重重选。

        P2 增强：若上一次记账已触发预算自动降级（``_budget_downgrade_to`` 已设），
        本次调用强制走更便宜的备别名，而不是原 alias。
        """
        agent_budget.guard()   # 执行体级预算：先查后调，越线不再花钱
        # P2 增强：预算降级一旦触发，后续调用统一走便宜模型（仅作用一次，
        # 调用方显式请求便宜模型本身时不重复切换）。
        alias = self._maybe_apply_downgrade(alias)
        plan = self._plan(alias, session_id=session_id)
        return await self._run_plan(
            plan,
            primary=alias,
            attempt=lambda current: self._attempt(
                current, messages,
                temperature=temperature,
                max_tokens=max_tokens,
                purpose=purpose,
                role=role,
                response_format=response_format,
                session_id=session_id,
                use_cache=plan.is_ordered,
            ),
            mark_cooldown=True,
        )

    async def stream_call(
        self,
        alias: str,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        purpose: str,
        role: str,
        response_format: ResponseFormat | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用入口（P3-3）：逐块 yield :class:`StreamChunk`，流结束后统一记账。

        与 :meth:`call` 的差异：

        - 走 ``provider.stream_complete``（默认实现退化为一次性 complete），边生成
         边把增量块吐给调用方，长输出可实时看到进度；
        - **首个块吐出后不再做故障转移**：流已吐出的块无法回滚，转移会产生两半
          不同来源的文本。首块之前失败仍按 ``model_failover`` 切换；
        - 记账在流**完整消费完**后进行：把全部 ``delta`` 拼成全文，按与 mock
          ``complete()`` 相同的口径估算 prompt/completion token，复用 :meth:`_account`
          落一条 ``llm_call`` 事件（token/成本/预算检查与非流式完全一致）。

        信号量同样只包住 ``stream_complete`` 的逐块消费；流结束后才释放并记账。

        P1.5：``settings.loop_guard_enabled`` 为真时，每个 chunk 喂入 text 通道的
        :class:`~agentcrew.models.loop_guard.LoopGuard`；窗口内 distinct unit 过少即
        抛 :class:`LoopDetectedError` 中断流（已 yield 的 chunk 不回滚），并落
        ``loop_detected`` 事件。``False``（默认）时行为与改造前逐字节一致。
        """
        aliases = self._failover_aliases(alias)
        last_error: BaseException | None = None
        for idx, current_alias in enumerate(aliases):
            agent_budget.guard()
            provider = self.get(current_alias)
            started = time.perf_counter()
            parts: list[str] = []
            finish_reason: str | None = None
            guard = LoopGuard(enabled=self._settings.loop_guard_enabled)
            # V7-P0：流式调用同样脱敏请求、逐块还原响应
            redactor = self._make_redactor()
            masked_messages = redactor.mask_messages(messages)
            provider_name = provider.binding.provider
            sem = self._get_provider_semaphore(
                provider_name, provider.binding.max_concurrency)
            await self._wait_for_rpm(provider_name, provider.binding.max_rpm)
            yielded_first = False
            success = False
            try:
                async with self._slots(sem):
                    async for chunk in provider.stream_complete(
                        masked_messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format,
                    ):
                        yielded_first = True
                        # V7-P0：逐块还原脱敏占位符（处理占位符被拆分到两个 chunk）
                        delta = redactor.unmask_stream_chunk(chunk.delta)
                        parts.append(delta)
                        if delta != chunk.delta:
                            chunk = chunk.model_copy(update={"delta": delta})
                        if guard.enabled:
                            samples = guard.feed_text(chunk.delta)
                            if samples:
                                self._emit_loop_detected(
                                    "text", samples, UnitRun.LOOP_WINDOW)
                                raise LoopDetectedError(
                                    "text", samples, UnitRun.LOOP_WINDOW)
                        if chunk.finish_reason is not None:
                            finish_reason = chunk.finish_reason
                        yield chunk
                # V7-P0：流结束后冲刷脱敏缓冲区（最后一个不完整占位符）
                flush_delta = redactor.unmask_stream_flush()
                if flush_delta:
                    parts.append(flush_delta)
                    yield StreamChunk(delta=flush_delta, finish_reason=None)
                # 流正常结束后冲刷缓冲（最后一个不完整 unit 也参与判定）。
                if guard.enabled:
                    for channel, samples in guard.flush().items():
                        self._emit_loop_detected(
                            channel, samples, UnitRun.LOOP_WINDOW)
                        raise LoopDetectedError(
                            channel, samples, UnitRun.LOOP_WINDOW)
                success = True
            except _PROVIDER_UNAVAILABLE_TYPES as exc:
                last_error = exc
                if yielded_first:
                    # 已吐出块给调用方，无法回滚 —— 直接上抛。
                    raise
                # 第一个 chunk 前失败：标记冷却并切换到下一个备用别名。
                self._mark_cooldown(
                    current_alias, getattr(exc, "status_code", None), exc)
                if idx < len(aliases) - 1:
                    self._emit_failover(current_alias, aliases[idx + 1], exc)
                    continue
                if len(aliases) > 1:
                    raise ProviderUnavailableError(
                        f"全部故障转移别名均不可用（primary={alias!r}，"
                        f"尝试了 {len(aliases)} 个）：{exc}") from exc
                raise
            if success:
                self._reset_cooldown(current_alias)
                latency_ms = int((time.perf_counter() - started) * 1000)
                text = "".join(parts)
                result = CompletionResult(
                    text=text,
                    provider=provider.binding.provider,
                    model=provider.binding.model,
                    prompt_tokens=sum(
                        estimate_tokens(m["content"]) for m in messages),
                    completion_tokens=estimate_tokens(text),
                    latency_ms=latency_ms,
                    finish_reason=finish_reason,
                )
                self._account(
                    result, purpose=purpose, role=role,
                    binding=provider.binding, alias=current_alias)
                return
        if last_error is not None:
            raise last_error

    async def complete_structured(
        self,
        alias: str,
        messages: Sequence[Message],
        schema: type[BaseModel],
        *,
        temperature: float,
        max_tokens: int,
        purpose: str,
        role: str,
        repair_attempts: int = 2,
        on_degrade: Callable[[DegradeEvent], None] | None = None,
    ) -> BaseModel:
        """结构化输出的受治理入口：同一个信号量 + 同一段记账（spec §7.3/§14.3）。

        ``models/structured.py`` 的
        :func:`~agentcrew.models.structured.complete_structured` 是纯阶梯、只认
        裸 Provider；直接拿它调用的路径（PM 拆解、G2 评审）会同时绕过全局调用
        限额与 ``llm_call`` 审计，报告里的 token/成本会缺掉整块。
        本方法把阶梯包进工厂：解析别名 → 取信号量 → 跑阶梯 → 记账。

        阶梯内部因能力降级/修复循环可能多次调用 ``provider.complete()``，这些
        调用同样消耗 token，因此**每一次返回结果的调用各记一条 ``llm_call``**
        （与 ``call()`` 同形）；阶梯最终失败时已消耗的部分也已记账再抛原异常。
        Provider 调用本身抛错（无 usage 可记）时不产生事件，与 ``call()`` 一致。

        ``on_degrade`` 缺省接 :meth:`_emit_degrade`：每次降级（能力驱动的初始
        降级、1/2 级失败下降）都落一条 ``llm_degraded`` 事件（spec §12.3），
        PM 拆解与 G2 评审不接线也查得到「哪家 provider 只能到第几级」。显式传入
        回调则**替换**默认处理器，由调用方自行决定如何留痕。

        P0-2：Provider 不可用时按 ``model_failover`` 依次尝试备用别名；每次切换
        记 ``llm_failover`` 事件。失败别名已消耗的 token 仍记账（真实 LLM 调用）。

        P3-10：权重格式 failover 时按权重随机选择，失败后从剩余权重重选。

        P2 增强：与 :meth:`call` 同形，预算降级触发后本入口也走便宜备别名。
        """
        agent_budget.guard()
        alias = self._maybe_apply_downgrade(alias)
        plan = self._plan(alias)
        return await self._run_plan(
            plan,
            primary=alias,
            attempt=lambda current: self._structured_attempt(
                current, messages, schema,
                temperature=temperature,
                max_tokens=max_tokens,
                purpose=purpose,
                role=role,
                repair_attempts=repair_attempts,
                on_degrade=on_degrade,
                use_cache=plan.is_ordered,
            ),
            # 结构化路径历史上不标记冷却（评审/拆解失败多为内容问题，不是
            # provider 不可用），保持原行为以免把一次坏输出冷却掉整家 provider。
            mark_cooldown=False,
        )

    # ── 调用管线：候选计划 + 单次尝试 ──────────────────────────────────────

    def _plan(self, alias: str, *, session_id: str | None = None) -> _CandidatePlan:
        """构建候选计划：权重路由优先，否则有序故障转移。

        P3-10：``model_failover`` 出现 dict 项（显式权重）即启用权重路由 ——
        权重池自带 primary，因此不再叠加会话亲和性（亲和性会把流量钉在一个
        别名上，与「按权重分摊」直接冲突）。

        V7-P3：有序路径下，若 ``session_id`` 命中亲和性绑定且该别名仍在候选里，
        把它提到首位 —— 同一会话连续调用落在同一上游，命中上游的 prompt 缓存。
        """
        pool = self._build_weighted_pool(alias)
        if pool is not None:
            return _CandidatePlan(
                weighted=pool,
                select=self._select_weighted,
                emit=self._emit_routed,
            )
        aliases = self._failover_aliases(alias)
        if session_id is not None and self._affinity_tracker.enabled:
            affinity_alias = self._affinity_tracker.resolve(
                alias, session_id,
                candidates=aliases,
                is_available=lambda a: not self._is_cooled_down(a),
            )
            if affinity_alias != alias and affinity_alias in aliases:
                aliases = [affinity_alias] + [a for a in aliases if a != affinity_alias]
        return _CandidatePlan(ordered=aliases)

    async def _run_plan(
        self,
        plan: _CandidatePlan,
        *,
        primary: str,
        attempt: Callable[[str], Awaitable[_T]],
        mark_cooldown: bool,
    ) -> _T:
        """沿候选计划逐个尝试，直至成功或候选耗尽。

        单次尝试失败（``_PROVIDER_UNAVAILABLE_TYPES``）时：取下一个候选 → 记
        ``llm_failover``（``mark_cooldown`` 为真时先标记冷却）。候选耗尽后，若原本
        有多个候选则包装成 :class:`ProviderUnavailableError`（调用方据此区分
        「模型不可用」与「执行失败」）；只有一个候选时原样上抛，保持无故障转移
        配置下的历史行为。

        冷却标记放在这里而不是单次尝试里，是因为它属于「这个别名这次不可用」
        这一**跨尝试**的事实；而结构化输出路径按设计不标冷却，于是它由入口显式
        传入 ``mark_cooldown=False``，而不是在单次尝试里再分一次叉。
        """
        current = plan.start()
        last_error: BaseException | None = None
        while current is not None:
            try:
                return await attempt(current)
            except _PROVIDER_UNAVAILABLE_TYPES as exc:
                last_error = exc
                if mark_cooldown:
                    self._mark_cooldown(
                        current, getattr(exc, "status_code", None), exc)
                nxt = plan.after_failure(current)
                if nxt is None:
                    if plan.total > 1:
                        raise ProviderUnavailableError(
                            plan.exhausted_message(primary, exc)) from exc
                    raise
                self._emit_failover(current, nxt, exc)
                plan.announce()
                current = nxt
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"call({primary!r}) 未产生结果也未抛出异常")

    async def _attempt(
        self,
        alias: str,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        purpose: str,
        role: str,
        response_format: ResponseFormat | None = None,
        session_id: str | None = None,
        use_cache: bool = True,
    ) -> CompletionResult:
        """对一个别名做一次完整尝试：配额 → 缓存 → 信号量 → 脱敏 → 记账。

        配额用「预扣 + 释放/结算」两段式：请求发出前 ``reserve`` 预估用量，
        失败时 ``release`` 退回，成功后交给 ``_account`` 按实际用量结算。预扣
        是为了让**并发**请求不能同时通过检查而一起超配额。

        V7-P0：请求前脱敏、响应后还原（未启用时零开销）。
        V7-P3：成功后把会话绑定到该别名（下次亲和性优先命中）。
        """
        estimated = _estimate_request_tokens(messages, max_tokens)
        try:
            # V7-P2：配额检查 —— 耗尽时抛 QuotaExhaustedError，
            # 被 _PROVIDER_UNAVAILABLE_TYPES 捕获后触发故障转移
            self._quota_tracker.check(alias)
            # V7 质量修复：预扣预计用量，防止并发请求同时通过检查超配额
            self._quota_tracker.reserve(alias, estimated)
            # P3-2：确定性调用先查响应缓存 —— 命中直接返回，不占信号量、
            # 不调 Provider、不计 token/成本。
            cache_key = (
                self._resp_cache_key(alias, messages, temperature, max_tokens)
                if use_cache else None)
            if cache_key is not None:
                cached = self._resp_cache.get(cache_key)
                if cached is not None:
                    self._emit_cache_hit(alias, cache_key)
                    self._account_cached(cached, purpose=purpose, role=role)
                    # 缓存命中：释放预扣，不记录实际用量
                    self._quota_tracker.release(alias, estimated)
                    return cached.model_copy(update={"from_cache": True})
            provider = self.get(alias)
            provider_name = provider.binding.provider
            sem = self._get_provider_semaphore(
                provider_name, provider.binding.max_concurrency)
            await self._wait_for_rpm(provider_name, provider.binding.max_rpm)
            async with self._slots(sem):
                redactor = self._make_redactor()
                masked_messages = redactor.mask_messages(messages)
                result = await provider.complete(
                    masked_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format)
                result = self._redact_result(result, redactor)
            # 释放预扣（release 与后续 consume 之间无 await，原子安全）
            self._quota_tracker.release(alias, estimated)
            self._account(
                result, purpose=purpose, role=role,
                binding=provider.binding, alias=alias)
            self._reset_cooldown(alias)
            # V7-P3：记录亲和性（成功调用后绑定会话到当前别名）
            if session_id is not None:
                self._affinity_tracker.remember(session_id, alias)
            if cache_key is not None:
                self._resp_cache.set(cache_key, result)
            return result
        except _PROVIDER_UNAVAILABLE_TYPES:
            # 调用失败：释放预扣后交给上层的候选计划处理
            self._quota_tracker.release(alias, estimated)
            raise

    async def _structured_attempt(
        self,
        alias: str,
        messages: Sequence[Message],
        schema: type[BaseModel],
        *,
        temperature: float,
        max_tokens: int,
        purpose: str,
        role: str,
        repair_attempts: int,
        on_degrade: Callable[[DegradeEvent], None] | None,
        use_cache: bool = True,
    ) -> BaseModel:
        """对一个别名跑一次结构化输出阶梯，并把阶梯内的每次调用全部记账。

        用 :class:`_CollectingProvider` 包住 Provider，是为了拿到阶梯内部
        （能力降级 + 修复循环）每一次 ``complete()`` 的真实用量 —— 阶梯只返回
        最终的 schema 对象，用量得靠收集器往外传。
        """
        provider = self.get(alias)
        # V7-P0：结构化输出阶梯内每次 complete 都走同一个 redactor
        redactor = self._make_redactor()
        collecting = _CollectingProvider(provider, redactor=redactor)
        degrade_cb = on_degrade if on_degrade is not None else self._emit_degrade
        # P3-2：结构化输出通常 temperature=0 → 可缓存。命中时直接把缓存的
        # 文本按当前 schema 解析返回；若 schema 漂移导致解析失败，则视为
        # 未命中，继续走正常阶梯。
        cache_key = (
            self._resp_cache_key(alias, messages, temperature, max_tokens)
            if use_cache else None)
        if cache_key is not None:
            cached = self._resp_cache.get(cache_key)
            if cached is not None:
                try:
                    parsed = schema.model_validate_json(cached.text)
                except ValidationError:
                    parsed = None
                if parsed is not None:
                    self._emit_cache_hit(alias, cache_key)
                    self._account_cached(cached, purpose=purpose, role=role)
                    return parsed
        try:
            provider_name = provider.binding.provider
            sem = self._get_provider_semaphore(
                provider_name, provider.binding.max_concurrency)
            async with self._slots(sem):
                model = await structured.complete_structured(
                    collecting,
                    messages,
                    schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    repair_attempts=repair_attempts,
                    on_degrade=degrade_cb)
        finally:
            # 信号量已释放（async with 先于本 finally 退出）；阶梯成功与失败
            # 走同一条记账路径，已消耗的 token 一条都不丢。故障转移时失败别名
            # 的消耗也照记（真实 LLM 调用）。
            for result in collecting.results:
                self._account(
                    result, purpose=purpose, role=role,
                    binding=provider.binding, alias=alias)
        # 阶梯成功：把最后一次（产出合法 schema 的）结果文本写入响应缓存。
        if cache_key is not None and collecting.results:
            self._resp_cache.set(cache_key, collecting.results[-1])
        return model

    # ── 路由与冷却（薄委托：策略在 models/routing.py，状态在本类）──────────

    @staticmethod
    def _parse_failover_entry(entry: str | dict[str, Any]) -> WeightedAlias:
        """解析 failover 列表项为 :class:`WeightedAlias`。"""
        return routing.parse_entry(entry)

    def _build_weighted_pool(self, alias: str) -> list[WeightedAlias] | None:
        """构建权重路由池；纯字符串列表（旧格式）返回 ``None``。"""
        return routing.build_weighted_pool(self._settings.model_failover, alias)

    def _select_weighted(
        self,
        pool: list[WeightedAlias],
        rng: random.Random | None = None,
    ) -> tuple[str, str]:
        """按配置的路由策略选择一个别名，返回 ``(alias, reason)``。

        V7-P1：``weighted`` / ``rotate`` / ``usage`` / ``order`` 四种策略，由
        ``settings.routing_strategy`` 决定；健康过滤与降权在
        :func:`agentcrew.models.routing.select` 里实现。
        """
        return routing.select(
            pool,
            strategy=self._settings.routing_strategy,
            health=self._health,
            rotate_counters=self._rotate_counters,
            usage=self._usage_scores,
            rng=rng,
        )

    def _note_usage(self, alias: str, prompt_tokens: int, completion_tokens: int) -> None:
        """记录别名的使用量（V7-P1 usage 策略）。"""
        routing.note_usage(
            self._usage_scores, alias, prompt_tokens, completion_tokens)

    def _current_usage_score(
        self, alias: str, now: float, half_life: float
    ) -> float:
        """计算别名的当前衰减使用量（指数衰减，半衰期 half_life 秒）。"""
        return routing.usage_score(self._usage_scores, alias, now, half_life)

    def _emit_routed(
        self,
        selected_alias: str,
        pool: list[WeightedAlias],
        reason: str,
    ) -> None:
        """记一条 ``llm_routed`` 事件（P3-10：诚实留痕）。"""
        if self._ledger is None:
            return
        self._ledger.emit(
            "llm_routed",
            selected_alias=selected_alias,
            reason=reason,
            candidates=[wa.alias for wa in pool],
            weights={wa.alias: wa.weight for wa in pool},
        )

    def _is_cooled_down(self, alias: str) -> bool:
        """检查 ``alias`` 是否在冷却中；冷却已过期则惰性清除并返回 ``False``。"""
        return is_cooled_down(self._cooldowns, alias)

    def _reset_cooldown(self, alias: str) -> None:
        """调用成功后清除该 alias 的冷却状态与连续失败计数。"""
        self._cooldowns.pop(alias, None)
        self._consecutive_failures.pop(alias, None)

    @staticmethod
    def _extract_retry_after(exc: BaseException) -> float | None:
        """沿异常链读 ``Retry-After``（秒）；复用 openai_compat 的解析逻辑。"""
        from agentcrew.models.providers.openai_compat import retry_after_seconds
        return retry_after_seconds(exc)

    def _mark_cooldown(
        self, alias: str, status_code: int | None, exc: BaseException,
    ) -> None:
        """按失败分类设置冷却到期时间（借鉴 Magpie ``routing.go restAfterMarked``）。

        分类规则见 :func:`agentcrew.models.health.cooldown_minutes`：402 长冷却、
        429 优先 ``Retry-After``、5xx / 其他指数退避、404 短冷却。连续失败计数在
        这里维护，成功时由 :meth:`_reset_cooldown` 清零。
        """
        failures = self._consecutive_failures.get(alias, 0) + 1
        self._consecutive_failures[alias] = failures
        self._cooldowns[alias] = cooldown_deadline(
            status_code,
            failures,
            retry_after=self._extract_retry_after(exc),
        )

    def _preset_fallback_aliases(self, alias: str) -> list[str]:
        """从 preset 的 ``fallback_models`` 派生备用别名（``provider:model``）。"""
        return routing.preset_fallback_aliases(
            alias,
            overrides=self._settings.provider_overrides,
            custom=self._settings.custom_providers,
        )

    def _failover_aliases(self, alias: str) -> list[str]:
        """返回 ``[primary] + 备用别名`` 的尝试列表（P0-2 故障转移）。

        去重保序，并按「健康 → 冷却」两级把差候选挪到末尾；全部候选都不健康
        时保持原序（最后一搏）。细节见
        :func:`agentcrew.models.routing.failover_candidates`。
        """
        override = agent_budget.failover_override()
        if override:
            # 执行体自带的备用别名优先：它们是给"这个 agent"准备的，不是全局的
            return [alias] + [a for a in override if a != alias]
        return routing.failover_candidates(
            alias,
            failover_map=self._settings.model_failover,
            overrides=self._settings.provider_overrides,
            custom=self._settings.custom_providers,
            health=self._health,
            health_enabled=self._settings.health_check_enabled,
            is_cooled=self._is_cooled_down,
        )

    def _emit_failover(self, from_alias: str, to_alias: str, exc: BaseException) -> None:
        """记一条 ``llm_failover`` 事件（P0-2：诚实留痕）。

        每次从一个别名切换到下一个备用别名时调用，含 from_alias / to_alias /
        error 三个字段。操作者事后可从 ledger 看出「哪个模型挂了、切到了谁、
        原始错误是什么」。没有 ledger 时静默跳过（纯逻辑测试）。

        P2-11：如果有健康数据，追加 from_status / to_status 字段。
        """
        if self._ledger is None:
            return
        data: dict[str, Any] = {
            "from_alias": from_alias,
            "to_alias": to_alias,
            "error": f"{type(exc).__name__}: {exc}",
        }
        from_health = self._health.get(from_alias)
        to_health = self._health.get(to_alias)
        if from_health is not None:
            data["from_status"] = from_health.status
        if to_health is not None:
            data["to_status"] = to_health.status
        self._ledger.emit("llm_failover", **data)

    # ── V7-P0 数据脱敏 ───────────────────────────────────────────────────

    def _make_redactor(self) -> Redactor:
        """根据当前 settings 构造 :class:`Redactor`。

        ``redact_enabled=False`` 时返回的 Redactor 的 ``mask_text`` /
        ``mask_messages`` 均为恒等操作，调用方无需分支判断。
        """
        return Redactor(RedactOptions(
            enabled=self._settings.redact_enabled,
            mask_api_keys=self._settings.redact_api_keys,
            mask_pii=self._settings.redact_pii,
            mask_credentials=self._settings.redact_credentials,
            custom_words=list(self._settings.redact_custom_words),
        ))

    def _redact_result(
        self, result: CompletionResult, redactor: Redactor
    ) -> CompletionResult:
        """对 Provider 返回结果做脱敏还原。

        仅还原 ``text`` 字段中的占位符；``usage`` / ``finish_reason`` 等元数据
        不受影响。脱敏未启用或无替换时逐字节不变。
        """
        if not redactor.enabled or redactor.mask_count() == 0:
            return result
        unmasked_text = redactor.unmask_text(result.text)
        if unmasked_text == result.text:
            return result
        return result.model_copy(update={"text": unmasked_text})

    def _resp_cache_key(
        self,
        alias: str,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
    ) -> str | None:
        """P3-2：返回本次调用的响应缓存键；不适用缓存时返回 ``None``。

        两条规则：(1) 工厂未注入 ``cache`` 时一律 ``None``（向后兼容）；
        (2) 只有 ``temperature == 0`` 的确定性调用才缓存 —— 温度 > 0 的结果
        本就非确定，缓存会掩盖采样随机性。
        """
        if self._resp_cache is None:
            return None
        if temperature != 0:
            return None
        return LLMCache._make_key(alias, messages, temperature, max_tokens)

    def _emit_cache_hit(self, alias: str, key: str) -> None:
        """记 ``llm_cache_hit`` 事件（含 alias 与键的前 12 位摘要）。"""
        if self._ledger is None:
            return
        self._ledger.emit(
            "llm_cache_hit",
            alias=alias,
            key=key[:12],
        )

    # ── V6 P0.5：per-provider 并发与 RPM 限制 ───────────────────────────────

    def _get_provider_semaphore(
        self, provider: str, max_concurrency: int | None
    ) -> asyncio.Semaphore:
        """返回某 provider 的专属信号量（按 provider 名缓存）。

        - ``max_concurrency`` 来自该 provider 的 preset（经 binding 拷贝）；
        - 为 ``None`` 时回退到全局 ``settings.max_concurrency``（向后兼容）；
        - 同一 provider 名复用同一信号量实例 —— 不同 alias 打同一上游时
          受同一把锁约束，避免触发上游 429。
        """
        sem = self._provider_semaphores.get(provider)
        if sem is None:
            cap = (
                max_concurrency if max_concurrency is not None
                else self._settings.max_concurrency)
            sem = asyncio.Semaphore(cap)
            self._provider_semaphores[provider] = sem
        return sem

    def _agent_semaphore(self) -> asyncio.Semaphore | None:
        """当前执行体的并发信号量；未绑定 profile 或没设上限时返回 ``None``。"""
        state = agent_budget.current()
        if state is None or state.max_concurrency is None:
            return None
        sem = self._agent_semaphores.get(state.profile)
        if sem is None:
            sem = asyncio.Semaphore(state.max_concurrency)
            self._agent_semaphores[state.profile] = sem
        return sem

    @asynccontextmanager
    async def _slots(self, provider_sem: asyncio.Semaphore) -> AsyncIterator[None]:
        """两级调用槽位：先占**执行体**名额，再占 provider 名额。

        顺序固定（agent → provider）是关键：所有调用点都按同一顺序取锁，
        就不会出现「A 持 agent 等 provider、B 持 provider 等 agent」的循环等待。
        两者都是纯 ``await`` 获取、内部不回调业务逻辑，因此与任务槽位不构成嵌套，
        不会重演 ``test_scheduler_deadlock`` 防的那类自锁。
        """
        async with AsyncExitStack() as stack:
            agent_sem = self._agent_semaphore()
            if agent_sem is not None:
                await stack.enter_async_context(agent_sem)
            await stack.enter_async_context(provider_sem)
            yield

    async def _wait_for_rpm(self, provider: str, max_rpm: int | None) -> None:
        """滑动窗口 RPM 限速（60 秒窗口）。

        - ``max_rpm`` 为 ``None`` 时直接返回（不限制，向后兼容）；
        - 每次调用清理 60 秒前的旧时间戳；
        - 若窗口内已达 ``max_rpm``，await 到最早时间戳滑出窗口再继续；
        - 调用结束把当前时间戳入列 —— 即本次请求占用一个 RPM 槽位。
        """
        if max_rpm is None:
            return
        timestamps = self._rpm_timestamps.setdefault(provider, [])
        now = time.monotonic()
        cutoff = now - 60.0
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)
        if len(timestamps) >= max_rpm:
            wait_time = timestamps[0] + 60.0 - now
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                # 睡醒后再清一次窗口（sleep 期间可能又有其他协程入列）。
                now = time.monotonic()
                cutoff = now - 60.0
                while timestamps and timestamps[0] < cutoff:
                    timestamps.pop(0)
        timestamps.append(time.monotonic())

    def _account_cached(
        self, result: CompletionResult, *, purpose: str, role: str
    ) -> None:
        """P3-2：缓存命中的留痕 —— 落一条 usage=0、``cached=True`` 的 ``llm_call``。

        命中**不**经过 :meth:`_account`：不递增 ``llm_calls``、不加 token / 成本、
        不触发预算硬上限（缓存命中应当免费）。仅落审计事件，便于运行报告区分
        「真调用」与「命中」。
        """
        if self._ledger is None:
            return
        self._ledger.emit(
            "llm_call",
            role=role,
            model=result.model,
            prompt_tokens=0,
            completion_tokens=0,
            estimated_cost_usd=0.0,
            latency_ms=0,
            purpose=purpose,
            cached=True,
        )

    # ── P1.5 / P2 增强：循环检测留痕与预算降级 ──────────────────────────────

    def _emit_loop_detected(
        self, channel: str, sample_units: list[str], window_size: int
    ) -> None:
        """记一条 ``loop_detected`` 事件（P1.5：诚实留痕）。"""
        if self._ledger is None:
            return
        self._ledger.emit(
            "loop_detected",
            channel=channel,
            sample_units=sample_units,
            window_size=window_size,
            distinct_units=len(sample_units),
        )

    def _maybe_apply_downgrade(self, alias: str) -> str:
        """P2 增强：预算降级触发后，后续调用改走便宜备别名。

        只在 ``_budget_downgrade_to`` 已设置且与请求 alias 不同时替换；调用方
        显式请求便宜模型本身时原样返回，避免无意义的二次切换。
        """
        target = self._budget_downgrade_to
        if target is not None and target != alias:
            return target
        return alias

    def _find_cheaper_fallback(
        self, current_model: str, binding: ModelBinding | None = None
    ) -> str | None:
        """找 input 单价最低且比当前模型便宜的备别名。

        候选来源合并两处：(1) ``settings.model_failover`` 显式配置的备用列表；
        (2) 当前 binding 的 ``fallback_models``（preset 自带的降级链，V6 P0）。
        价格用 :data:`~agentcrew.models.pricing.MODEL_PRICING` 最长前缀匹配；
        候选 binding 若自带 ``input_price`` 则优先用它（聚合平台 / 自定义模型
        不在公开价目表里）。

        当前模型价格未知（0.0）时不降级——无法判断谁更便宜，贸然切换可能更贵。
        无更便宜选项返回 ``None``，保持原行为（达 100% 时抛 BudgetExhaustedError）。
        """
        return budget.cheaper_fallback(
            current_model,
            failover_map=self._settings.model_failover,
            binding=binding,
        )

    def _maybe_emit_budget_warning(self) -> None:
        """P2 增强：累计成本首次达 ``budget_warning_threshold`` 时落 ``budget_warning``。

        只警告、不中断；一次性（``_budget_warning_emitted`` 防止每次记账都刷一条）。
        ``max_cost_usd`` 为 None 时整段跳过，行为完全不变。
        """
        limit = self._settings.max_cost_usd
        if limit is None or self._budget_warning_emitted:
            return
        threshold = self._settings.budget_warning_threshold
        # 浮点容差在 budget.threshold_reached 里：累计成本是多次加法累加的，
        # 直接与 limit*threshold 比会有 ~1e-17 误差，导致「刚好达到阈值」被判成未达到。
        if not budget.threshold_reached(self._total_cost_usd, limit, threshold):
            return
        self._budget_warning_emitted = True
        if self._ledger is None:
            return
        self._ledger.emit(
            "budget_warning",
            current_cost_usd=self._total_cost_usd,
            limit_usd=limit,
            threshold=threshold,
            pct=round(self._total_cost_usd / limit, 4),
        )

    def _maybe_downgrade_to_cheaper(
        self, result: CompletionResult, binding: ModelBinding | None = None
    ) -> None:
        """P2 增强：累计成本达 ``budget_downgrade_threshold`` 时切到便宜备模型。

        找到更便宜备别名后只设置 ``_budget_downgrade_to`` 标志位 + 落
        ``budget_downgrade`` 事件；实际模型切换由 :meth:`call` /
        :meth:`complete_structured` 下次调用时经 :meth:`_maybe_apply_downgrade`
        完成——已经开始的这次调用不半途切 provider（流已吐的 chunk 无法回滚）。
        """
        limit = self._settings.max_cost_usd
        if limit is None or self._budget_downgrade_to is not None:
            return
        threshold = self._settings.budget_downgrade_threshold
        if not budget.threshold_reached(self._total_cost_usd, limit, threshold):
            return
        cheaper = self._find_cheaper_fallback(result.model, binding)
        if cheaper is None:
            return
        self._budget_downgrade_to = cheaper
        if self._ledger is None:
            return
        self._ledger.emit(
            "budget_downgrade",
            from_model=result.model,
            to_alias=cheaper,
            reason="budget_threshold",
            current_cost_usd=self._total_cost_usd,
            limit_usd=limit,
            threshold=threshold,
        )

    def _account(
        self,
        result: CompletionResult,
        *,
        purpose: str,
        role: str,
        binding: ModelBinding | None = None,
        alias: str | None = None,
    ) -> None:
        """累加用量、估算成本并发一条 ``llm_call`` —— **所有调用路径共用**。

        单一记账路径是刻意的：``call()`` 与 ``complete_structured()`` 若各写一份，
        迟早漂移，而 spec §14.3 的运行报告会照抄其中一份。

        V6 P1：若 ``binding`` 同时给出 ``input_price`` / ``output_price``，优先
        用 preset 价格（USD/百万 token）计算成本；否则回退到 ``pricing.py`` 的
        ``MODEL_PRICING`` 最长前缀表 —— 聚合平台 / 未知模型仍按表估。

        P0-3：记账后检查 ``max_cost_usd`` / ``max_llm_calls`` 硬上限，超限抛
        ``BudgetExhaustedError``。ledger 事件先于异常发出（触发超限的那次调用
        也必须留痕），Crew 捕获异常后优雅收尾。

        V7-P1：``alias`` 非空时同步记录到 usage 路由策略的使用量追踪。
        """
        if (
            binding is not None
            and binding.input_price is not None
            and binding.output_price is not None
        ):
            cost = (
                result.prompt_tokens / 1e6 * binding.input_price
                + result.completion_tokens / 1e6 * binding.output_price
            )
        else:
            cost = estimate_cost(
                result.model, result.prompt_tokens, result.completion_tokens)
        self._llm_calls += 1
        self._total_prompt_tokens += result.prompt_tokens
        self._total_completion_tokens += result.completion_tokens
        self._total_cost_usd += cost
        agent_budget.note_call(cost)
        state = agent_budget.current()
        if state is not None:
            slot = self._agent_usage.setdefault(state.profile, {
                "calls": 0.0, "cost_usd": 0.0,
                "prompt_tokens": 0.0, "completion_tokens": 0.0})
            slot["calls"] += 1
            slot["cost_usd"] += cost
            slot["prompt_tokens"] += result.prompt_tokens
            slot["completion_tokens"] += result.completion_tokens   # 执行体级预算累加（无上下文时零开销）
        # V7-P1：usage 路由策略的使用量追踪
        if alias is not None:
            self._note_usage(alias, result.prompt_tokens, result.completion_tokens)
        # V7-P2：多窗口配额消耗记录
        if alias is not None:
            self._quota_tracker.consume(
                alias, result.prompt_tokens, result.completion_tokens, cost)
        if self._ledger is not None:
            self._ledger.emit(
                "llm_call",
                role=role,
                model=result.model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                estimated_cost_usd=cost,
                latency_ms=result.latency_ms,
                purpose=purpose)
        # P2 增强：先做软阈值（80% 警告 / 90% 降级），再做 100% 硬上限。
        # 软阈值不抛异常，只落事件 + 置标志位；硬超限仍按原逻辑抛
        # BudgetExhaustedError。max_cost_usd 为 None 时这两个方法整体空转。
        self._maybe_emit_budget_warning()
        self._maybe_downgrade_to_cheaper(result, binding)
        # P0-3：预算硬上限检查（ledger 事件已发出，超限调用可追溯）。
        exhausted = budget.exhaustion_error(
            total_cost_usd=self._total_cost_usd,
            llm_calls=self._llm_calls,
            max_cost_usd=self._settings.max_cost_usd,
            max_llm_calls=self._settings.max_llm_calls,
        )
        if exhausted is not None:
            raise exhausted

    def _emit_degrade(self, event: DegradeEvent) -> None:
        """把一次降级落成 ``llm_degraded`` 事件（spec §12.3）。

        工厂持有 ledger，因此这是默认留痕的正确归属：PM 拆解与 G2 评审都不传
        ``on_degrade``，没有这条默认处理器时降级事实只存在于内存里，操作者
        事后无从诊断「哪家 provider 不稳定」。``from_level`` / ``to_level`` /
        ``reason`` 原样取自 :class:`DegradeEvent`，字段与阶梯一一对应。

        没有 ledger（纯逻辑测试、只读工厂）时静默跳过：降级本身不影响调用
        结果，缺账不该把结构化调用打崩。
        """
        if self._ledger is None:
            return
        self._ledger.emit(
            "llm_degraded",
            provider=event.provider,
            from_level=event.from_level,
            to_level=event.to_level,
            reason=event.reason,
        )

    def _implementation_for(self, binding: ModelBinding) -> str:
        """binding 的生效实现方式（``provider_overrides`` 可整体替换它）。"""
        patch = self._settings.provider_overrides.get(binding.provider) or {}
        if "implementation" in patch:
            implementation = patch["implementation"]
        else:
            # A1：查合并目录（内置 + 自定义），而非直接 PROVIDER_PRESETS。
            directory = build_directory(
                custom=self._settings.custom_providers)
            implementation = directory[binding.provider].implementation
        if implementation not in _IMPLEMENTATIONS:
            raise ModelFactoryError(
                f"provider {binding.provider!r} 的 implementation 非法："
                f"{implementation!r}（可选：{', '.join(_IMPLEMENTATIONS)}）")
        return str(implementation)

    @staticmethod
    def _as_mock_binding(binding: ModelBinding) -> ModelBinding:
        """把任意绑定改写成 mock 预设的绑定（``--mock`` 与 implementation 覆盖用）。

        保留原 alias 与 model 名（ledger/成本统计里仍看得到「本来要用哪个
        模型」），能力位换成 ``mock`` 预设的 —— 能力位描述的是**实际在跑的
        实现**，离线回放的能力才是真实能力。
        """
        if binding.provider == "mock":
            return binding
        preset = PROVIDER_PRESETS["mock"]
        return binding.model_copy(update={
            "provider": "mock",
            "base_url": preset.base_url,
            "api_key_env": preset.api_key_env,
            "capabilities": preset.capabilities,
        })

    def _require_api_key(self, binding: ModelBinding) -> str:
        """取 ``binding.provider`` 的 API Key；缺失时抛错并点名环境变量。

        ``ollama`` **先于** :meth:`Settings.api_key_for` 判断：后者在
        ``OLLAMA_BASE_URL`` 已设时返回的是 base URL，把 URL 当 key 传进去虽无
        害（本地服务不鉴权），但会让占位串分支永远不可达。

        A1：自定义 provider 若 ``api_key_env`` 为空串（本地中转/网关无需鉴权），
        与 ollama 一样返回占位串，不报错。
        """
        if binding.provider == "ollama":
            return _OLLAMA_PLACEHOLDER
        if not binding.api_key_env:
            # 本地 / 无需鉴权的自定义 provider：返回占位串。
            return _OLLAMA_PLACEHOLDER
        api_key = self._settings.api_key_for(binding.provider)
        if api_key:
            return api_key
        raise ModelFactoryError(
            f"provider {binding.provider!r} 缺少 API Key（alias={binding.alias!r}）："
            f"请设置环境变量 {binding.api_key_env}（spec §17.2）")

    def _on_mock_fallback(self, message: str) -> None:
        """MockProvider 的 fixture 缺失上报 → ledger（spec §16.1：绝不静默）。"""
        if self._ledger is not None:
            self._ledger.emit("mock_fallback", message=message)

    def _on_retry(self, attempt: int, exc: Exception, delay: float) -> None:
        """Provider 每次即将重试时记账；Retry-After 截断事实随事件落 ledger。

        Task 18 的 carry-forward：``retry_after_seconds`` 把「服务端原始要求」
        留在异常属性 :data:`RETRY_AFTER_CLAMPED_ATTR` 上，而该异常正是
        ``retry_async`` 传给 ``on_retry`` 的同一个对象。
        """
        if self._ledger is None:
            return
        data: dict[str, object] = {
            "attempt": attempt,
            "delay_seconds": delay,
            "error": f"{type(exc).__name__}: {exc}",
        }
        clamped = getattr(exc, RETRY_AFTER_CLAMPED_ATTR, None)
        if clamped is not None:
            data["retry_after_clamped_from"] = clamped
        self._ledger.emit("llm_retry", **data)


class _CollectingProvider(BaseProvider):
    """包住真实 Provider，收集每次 ``complete()`` 的 ``CompletionResult``。

    :func:`~agentcrew.models.structured.complete_structured` 会因能力降级与修复
    循环多次调用 provider，而它只返回最终的 schema 对象、不吐出中间用量；包一层
    收集起来，工厂就能在阶梯结束后统一记账（成功与失败一视同仁）。

    ``capabilities`` 与 ``binding`` 都委托给被包的真实 Provider（继承自
    ``BaseProvider`` 的默认行为），阶梯据此选起始级别、报告 provider 名 ——
    这一层对阶梯完全透明。

    V7-P0：可选 ``redactor``，每次 complete 前脱敏请求、后还原响应。
    """

    def __init__(
        self,
        provider: BaseProvider,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        super().__init__(provider.binding)
        self._provider = provider
        self._redactor = redactor
        self.results: list[CompletionResult] = []

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        if self._redactor is not None and self._redactor.enabled:
            masked = self._redactor.mask_messages(messages)
            result = await self._provider.complete(
                masked,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format)
            result = result.model_copy(
                update={"text": self._redactor.unmask_text(result.text)})
        else:
            result = await self._provider.complete(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format)
        self.results.append(result)
        return result
