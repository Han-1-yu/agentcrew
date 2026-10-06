"""ModelFactory：别名 → Provider 实例（缓存）+ 全局 LLM 调用限流与记账。

spec §7.3 的两级并发限额里，任务槽位由 Scheduler 持有，本模块持有 **LLM 调用级
信号量**（容量 = ``settings.max_concurrency``）。两者刻意不嵌套：门禁在任务槽位
释放之后才执行，而调用级信号量只在 ``complete()`` 期间持有，所以不存在「持 A
等 B」的循环等待，不可能死锁。

设计要点：

- ``--mock``（``settings.mock``）是**全局**开关：所有别名一律解析为
  :class:`MockProvider`（用 ``mock`` 预设的能力位），无需任何 API Key。
- API Key 只从环境读取（spec §17.2），缺 Key 时在构造 Provider 的当场抛
  :class:`ModelFactoryError` 并点名环境变量，绝不静默降级成无鉴权调用；
  ``ollama`` 是本地服务，一律用占位串 ``"ollama"``。
- 记账：``call()`` 在信号量**释放之后**累加计数、用 ``estimate_cost`` 估算成本，
  再发一条 ``llm_call`` 事件（spec §13.2 的七个字段）。
- **结构化输出也走同一个受治理入口**：``complete_structured()`` 在工厂内解析别名、
  独占同一个信号量并复用同一段记账 —— PM 拆解与 G2 评审（spec §7.3 明确计入
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
"""

import asyncio
import random
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from agentcrew.config import Settings
from agentcrew.models import structured
from agentcrew.models.complexity import (
    evaluate as evaluate_complexity,
    get_calibrator,
)
from agentcrew.models.config import (
    PROVIDER_PRESETS,
    ModelBinding,
    UnknownProviderError,
    parse_alias,
)
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
from agentcrew.models.structured import DegradeEvent
from agentcrew.runtime.ledger import Ledger
from agentcrew.utils.retry import TransientError
from agentcrew.utils.tokens import estimate_tokens

if TYPE_CHECKING:
    from agentcrew.task.task import Task
    from agentcrew.models.complexity import ComplexityLevel


def _level_rank(level: "ComplexityLevel") -> int:
    """复杂度级别排序权重（P3-1：判断上调 / 下调）。LOW < MEDIUM < HIGH。"""
    return {"low": 0, "medium": 1, "high": 2}[level.value]

#: 默认 fixture 根目录（仓库根的 ``examples/fixtures``）。模块级常量是契约的
#: 一部分：测试与 Task 34 的 Crew 都靠 monkeypatch 它注入临时 fixtures。
DEFAULT_FIXTURES_DIR: Path = (
    Path(__file__).resolve().parents[2] / "examples" / "fixtures")

#: ollama 是本地服务（spec §17.2 无需密钥），但 SDK 要求 api_key 非空。
_OLLAMA_PLACEHOLDER = "ollama"

#: 支持的 Provider 实现（与 ``config.Implementation`` 的 Literal 对齐）。
_IMPLEMENTATIONS: tuple[str, ...] = ("openai_compat", "anthropic", "mock", "cli")

#: 建议预算相对窗口上限预留的头部空间（spec §11.2 的建议值公式）。
_SUGGESTED_BUDGET_HEADROOM = 1024

#: P2-11：健康探测中延迟超过此值（毫秒）视为 degraded。
_HEALTH_DEGRADED_THRESHOLD_MS = 2000.0

#: P2-11：健康探测单次超时（秒）。
_HEALTH_CHECK_TIMEOUT_SECS = 5.0

#: P0-2：触发故障转移的异常类型 —— Provider 内部重试耗尽后的瞬时故障。
#: openai_compat / anthropic 抛 TransientError；cli 抛 TimeoutError / ConnectionError。
_PROVIDER_UNAVAILABLE_TYPES: tuple[type[BaseException], ...] = (
    TransientError, TimeoutError, ConnectionError,
)


class ModelFactoryError(RuntimeError):
    """Provider 构造失败（缺 API Key、implementation 非法等）——属配置错误。"""


class ContextBudgetError(ModelFactoryError):
    """上下文预算 + 最大输出超出模型窗口（spec §11.2，退出码 3）。"""

    def __init__(
        self, alias: str, budget: int, window: int, suggested_budget: int
    ) -> None:
        self.alias = alias
        self.budget = budget
        self.window = window
        self.suggested_budget = suggested_budget
        super().__init__(
            f"上下文预算 {budget} tokens 对模型窗口过大：alias={alias!r} 的 "
            f"max_context_tokens={window}，建议把 AGENTCREW_CONTEXT_BUDGET 调低到 "
            f"{suggested_budget}（= 窗口 - max_output_tokens - "
            f"{_SUGGESTED_BUDGET_HEADROOM}）")


class ProviderUnavailableError(RuntimeError):
    """Provider 不可用错误标记类（P0-2 故障转移）。

    503/429/连接超时/读取超时等瞬时故障在 Provider 内部重试耗尽后，以
    :class:`~agentcrew.utils.retry.TransientError`（openai_compat / anthropic）
    或 :class:`TimeoutError` / :class:`ConnectionError`（cli）传播。工厂的
    故障转移循环捕获这些类型，依次尝试备用别名；全部备用耗尽时把最后一次
    错误包装成 ``ProviderUnavailableError`` 抛出，便于调用方区分「模型不可用」
    与「执行失败」。
    """


class BudgetExhaustedError(ModelFactoryError):
    """成本预算耗尽（P0-3）：累计成本或 LLM 调用次数超出硬上限。

    ``_account()`` 在每次记账后检查；超限即抛，Crew 捕获后优雅收尾
    （等在跑任务完成，不再派发新任务），记 ``budget_exhausted`` 事件，退出码 1。
    携带当前累计值与上限，便于日志定位。
    """

    def __init__(self, *, kind: str, current: float, limit: float) -> None:
        self.kind = kind
        self.current = current
        self.limit = limit
        super().__init__(
            f"预算耗尽：{kind} 累计 {current} 已达上限 {limit}")


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


class WeightedAlias(BaseModel):
    """带权重的别名（P3-10 权重优先级路由）。

    ``weight`` 是相对权重（无需归一化）；``_select_weighted`` 内部归一化后用
    ``random.choices`` 按权重采样。字符串形式的 failover 项等价于 weight=1.0。
    """

    model_config = ConfigDict(extra="forbid")

    alias: str
    weight: float = 1.0


class ModelFactory:
    """按别名缓存 Provider，并独占全局 LLM 调用信号量与用量统计。"""

    def __init__(
        self,
        settings: Settings,
        *,
        ledger: Ledger | None = None,
        fixtures_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        # 模块级常量在构造时读取（而不是绑成默认参数），monkeypatch 才有效。
        self._fixtures_dir = (
            Path(fixtures_dir) if fixtures_dir is not None else DEFAULT_FIXTURES_DIR)
        self._cache: dict[str, BaseProvider] = {}
        # Python 3.10+ 的 Semaphore 不再在构造时绑定事件循环，因此可以在这里
        # 创建；容量即全局 LLM 调用并发上限（spec §7.3）。
        self._llm_semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._llm_calls = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_cost_usd = 0.0
        # P2-11：主动健康监控 —— 各别名的健康状态缓存与后台探测任务。
        self._health: dict[str, HealthRecord] = {}
        self._health_task: asyncio.Task | None = None

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
    def total_cost_usd(self) -> float:
        """累计估算成本（USD）；未知模型按 0 计但 token 照记。"""
        return self._total_cost_usd

    def reset_stats(self) -> None:
        """清零本实例的调用数、token 总数与成本。"""
        self._llm_calls = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_cost_usd = 0.0

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
        self._emit_model_switched(old, alias, scope=f"role:{role}")

    def _validate_alias(self, alias: str) -> None:
        """``parse_alias`` 校验 + 把解析异常统一收敛成 ``ModelFactoryError``。"""
        try:
            parse_alias(alias, overrides=self._settings.provider_overrides)
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

    def alias_for_task(self, task: "Task") -> str:
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
        task: "Task",
        original: "ComplexityLevel",
        calibrated: "ComplexityLevel",
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
        binding = parse_alias(alias, overrides=self._settings.provider_overrides)
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
            budget = self._settings.context_budget_tokens
            fits = (
                budget + capabilities.max_output_tokens
                <= capabilities.max_context_tokens)
            if fits:
                continue
            raise ContextBudgetError(
                alias=alias,
                budget=budget,
                window=capabilities.max_context_tokens,
                suggested_budget=(
                    capabilities.max_context_tokens
                    - capabilities.max_output_tokens
                    - _SUGGESTED_BUDGET_HEADROOM),
            )

    # ── 调用与记账 ────────────────────────────────────────────────────────

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
    ) -> CompletionResult:
        """取信号量 → 调用 Provider → 释放 → 记账并落 ledger。

        信号量只包住 ``complete()``：记账与落盘是本地小操作，占着全局调用额度
        只会无谓拉长排队。

        P0-2：Provider 不可用（503/429/超时/连接错误，重试耗尽后）时按
        ``model_failover`` 列表依次尝试备用别名，每次切换记 ``llm_failover`` 事件。
        故障转移不消耗任务 attempts（那是执行失败，不是模型不可用）。

        P3-10：如果 failover 配置使用权重格式（含 dict 项），按权重随机选择别名，
        失败时从剩余别名按剩余权重重选。
        """
        # P3-10：权重路由路径
        pool = self._build_weighted_pool(alias)
        if pool is not None:
            return await self._call_weighted(alias, pool, messages,
                                             temperature=temperature,
                                             max_tokens=max_tokens,
                                             purpose=purpose, role=role,
                                             response_format=response_format)

        aliases = self._failover_aliases(alias)
        last_error: Exception | None = None
        for idx, current_alias in enumerate(aliases):
            try:
                provider = self.get(current_alias)
                async with self._llm_semaphore:
                    result = await provider.complete(
                        messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format)
                self._account(result, purpose=purpose, role=role)
                return result
            except _PROVIDER_UNAVAILABLE_TYPES as exc:
                last_error = exc
                if idx < len(aliases) - 1:
                    self._emit_failover(current_alias, aliases[idx + 1], exc)
                    continue
                # 仅当实际尝试过备用别名（len>1）时才包装为 ProviderUnavailableError；
                # 未配置 failover 时原样抛出，保持向后兼容（现有测试期望 TransientError）。
                if len(aliases) > 1:
                    raise ProviderUnavailableError(
                        f"全部故障转移别名均不可用（primary={alias!r}，"
                        f"尝试了 {len(aliases)} 个）：{exc}") from exc
                raise
        # 理论不可达：aliases 至少含 primary，循环要么 return 要么 raise
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"call({alias!r}) 未产生结果也未抛出异常")

    async def _call_weighted(
        self,
        primary: str,
        pool: list[WeightedAlias],
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        purpose: str,
        role: str,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        """P3-10：权重路由的 ``call()`` 实现 —— 按权重选择，失败后从剩余权重重选。"""
        available = list(pool)
        last_error: Exception | None = None
        while available:
            chosen, reason = self._select_weighted(available)
            self._emit_routed(chosen, available, reason)
            try:
                provider = self.get(chosen)
                async with self._llm_semaphore:
                    result = await provider.complete(
                        messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format)
                self._account(result, purpose=purpose, role=role)
                return result
            except _PROVIDER_UNAVAILABLE_TYPES as exc:
                last_error = exc
                available = [wa for wa in available if wa.alias != chosen]
                if available:
                    self._emit_failover(chosen, available[0].alias, exc)
                    continue
                raise ProviderUnavailableError(
                    f"全部权重路由别名均不可用（primary={primary!r}，"
                    f"尝试了 {len(pool)} 个）：{exc}") from exc
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"_call_weighted({primary!r}) 未产生结果也未抛出异常")

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
        - **不做故障转移**（简化）：流式过程中 Provider 不可用直接上抛，不像
          :meth:`call` 那样切换备用别名 —— 流已吐出的块无法回滚，转移会产生两半
          不同来源的文本；
        - 记账在流**完整消费完**后进行：把全部 ``delta`` 拼成全文，按与 mock
          ``complete()`` 相同的口径估算 prompt/completion token，复用 :meth:`_account`
          落一条 ``llm_call`` 事件（token/成本/预算检查与非流式完全一致）。

        信号量同样只包住 ``stream_complete`` 的逐块消费；流结束后才释放并记账。
        """
        provider = self.get(alias)
        started = time.perf_counter()
        parts: list[str] = []
        finish_reason: str | None = None
        async with self._llm_semaphore:
            async for chunk in provider.stream_complete(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            ):
                parts.append(chunk.delta)
                if chunk.finish_reason is not None:
                    finish_reason = chunk.finish_reason
                yield chunk
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = "".join(parts)
        result = CompletionResult(
            text=text,
            provider=provider.binding.provider,
            model=provider.binding.model,
            prompt_tokens=sum(estimate_tokens(m["content"]) for m in messages),
            completion_tokens=estimate_tokens(text),
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )
        self._account(result, purpose=purpose, role=role)

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
        """
        # P3-10：权重路由路径
        pool = self._build_weighted_pool(alias)
        if pool is not None:
            return await self._complete_structured_weighted(
                alias, pool, messages, schema,
                temperature=temperature, max_tokens=max_tokens,
                purpose=purpose, role=role,
                repair_attempts=repair_attempts, on_degrade=on_degrade)

        aliases = self._failover_aliases(alias)
        last_error: Exception | None = None
        for idx, current_alias in enumerate(aliases):
            provider = self.get(current_alias)
            collecting = _CollectingProvider(provider)
            degrade_cb = on_degrade if on_degrade is not None else self._emit_degrade
            try:
                async with self._llm_semaphore:
                    return await structured.complete_structured(
                        collecting,
                        messages,
                        schema,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        repair_attempts=repair_attempts,
                        on_degrade=degrade_cb)
            except _PROVIDER_UNAVAILABLE_TYPES as exc:
                last_error = exc
                if idx < len(aliases) - 1:
                    self._emit_failover(current_alias, aliases[idx + 1], exc)
                    continue
                if len(aliases) > 1:
                    raise ProviderUnavailableError(
                        f"全部故障转移别名均不可用（primary={alias!r}，"
                        f"尝试了 {len(aliases)} 个）：{exc}") from exc
                raise
            finally:
                # 信号量已释放（async with 先于本 finally 退出）；阶梯成功与失败
                # 走同一条记账路径，已消耗的 token 一条都不丢。故障转移时失败别名
                # 的消耗也照记（真实 LLM 调用）。
                for result in collecting.results:
                    self._account(result, purpose=purpose, role=role)
        # 理论不可达
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"complete_structured({alias!r}) 未产生结果也未抛出异常")

    async def _complete_structured_weighted(
        self,
        primary: str,
        pool: list[WeightedAlias],
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
        """P3-10：权重路由的 ``complete_structured()`` 实现。"""
        available = list(pool)
        last_error: Exception | None = None
        while available:
            chosen, reason = self._select_weighted(available)
            self._emit_routed(chosen, available, reason)
            provider = self.get(chosen)
            collecting = _CollectingProvider(provider)
            degrade_cb = on_degrade if on_degrade is not None else self._emit_degrade
            try:
                async with self._llm_semaphore:
                    return await structured.complete_structured(
                        collecting,
                        messages,
                        schema,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        repair_attempts=repair_attempts,
                        on_degrade=degrade_cb)
            except _PROVIDER_UNAVAILABLE_TYPES as exc:
                last_error = exc
                available = [wa for wa in available if wa.alias != chosen]
                if available:
                    self._emit_failover(chosen, available[0].alias, exc)
                    continue
                raise ProviderUnavailableError(
                    f"全部权重路由别名均不可用（primary={primary!r}，"
                    f"尝试了 {len(pool)} 个）：{exc}") from exc
            finally:
                for result in collecting.results:
                    self._account(result, purpose=purpose, role=role)
        if last_error is not None:
            raise last_error
        raise RuntimeError(
            f"_complete_structured_weighted({primary!r}) 未产生结果也未抛出异常")

    # ── 内部 ──────────────────────────────────────────────────────────────

    # ── P3-10：权重路由 ───────────────────────────────────────────────────

    @staticmethod
    def _parse_failover_entry(entry: str | dict[str, Any]) -> WeightedAlias:
        """解析 failover 列表项为 :class:`WeightedAlias`。

        字符串项 → weight=1.0；dict 项 → 读取 ``alias`` 与 ``weight``。
        """
        if isinstance(entry, str):
            return WeightedAlias(alias=entry, weight=1.0)
        return WeightedAlias(
            alias=str(entry["alias"]),
            weight=float(entry.get("weight", 1.0)),
        )

    def _build_weighted_pool(self, alias: str) -> list[WeightedAlias] | None:
        """构建权重路由池；纯字符串列表（旧格式）返回 ``None``。

        池包含 primary（weight=1.0）+ 解析后的 backups。去重保序。
        """
        backups = self._settings.model_failover.get(alias, [])
        if not backups:
            return None
        # 纯字符串列表 → 旧格式，返回 None 走有序故障转移。
        if not any(isinstance(b, dict) for b in backups):
            return None
        pool: list[WeightedAlias] = [WeightedAlias(alias=alias, weight=1.0)]
        seen: set[str] = {alias}
        for entry in backups:
            wa = self._parse_failover_entry(entry)
            if wa.alias not in seen:
                seen.add(wa.alias)
                pool.append(wa)
        return pool

    def _select_weighted(
        self,
        pool: list[WeightedAlias],
        rng: random.Random | None = None,
    ) -> tuple[str, str]:
        """按权重随机选择一个别名，返回 ``(alias, reason)``。

        结合健康状态（P2-11）：``unavailable`` 的权重置 0；``degraded`` 的权重
        减半。所有权重为 0 时回退到第一个别名。
        reason 为 ``"weighted"``（纯权重）、``"health_adjusted"``（健康调整过权重）
        或 ``"fallback_first"``（全部权重为 0 的兜底）。
        """
        actual_rng = rng if rng is not None else random.Random()
        weights: list[float] = []
        health_adjusted = False
        for wa in pool:
            w = wa.weight
            rec = self._health.get(wa.alias)
            if rec is not None:
                if rec.status == "unavailable":
                    w = 0.0
                    health_adjusted = True
                elif rec.status == "degraded":
                    w = w * 0.5
                    health_adjusted = True
            weights.append(w)
        if sum(weights) <= 0:
            return pool[0].alias, "fallback_first"
        chosen = actual_rng.choices(pool, weights=weights, k=1)[0]
        reason = "health_adjusted" if health_adjusted else "weighted"
        return chosen.alias, reason

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

    # ── P0-2：有序故障转移 ────────────────────────────────────────────────

    def _failover_aliases(self, alias: str) -> list[str]:
        """返回 ``[primary] + 备用别名`` 的尝试列表（P0-2 故障转移）。

        去重保序：备用列表里若重复出现 primary 或自身重复，只留首次 —— 避免
        同一个不可用 provider 被反复尝试。未配置 failover 时返回 ``[alias]``，
        行为与改动前完全一致（向后兼容）。

        P2-11：如果健康监控已启用且有探测数据，将 ``unavailable`` 的别名移到
        列表末尾（优先尝试健康的）；``degraded`` 的别名保持原位。如果所有别名
        都 unavailable，仍返回原列表（最后一搏）。健康监控未启用或无数据时
        行为不变。
        """
        backups = self._settings.model_failover.get(alias, [])
        seen: set[str] = {alias}
        result = [alias]
        for backup in backups:
            if backup not in seen:
                seen.add(backup)
                result.append(backup)
        # P2-11：健康感知重排 —— unavailable 的别名移到末尾。
        if self._settings.health_check_enabled and self._health:
            healthy_part: list[str] = []
            unavailable_part: list[str] = []
            for a in result:
                rec = self._health.get(a)
                if rec is not None and rec.status == "unavailable":
                    unavailable_part.append(a)
                else:
                    healthy_part.append(a)
            if unavailable_part and healthy_part:
                result = healthy_part + unavailable_part
            # 全部 unavailable 时保持原序（最后一搏）。
        return result

    def _emit_failover(self, from_alias: str, to_alias: str, exc: Exception) -> None:
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

    def _account(self, result: CompletionResult, *, purpose: str, role: str) -> None:
        """累加用量、估算成本并发一条 ``llm_call`` —— **两条调用路径共用**。

        单一记账路径是刻意的：``call()`` 与 ``complete_structured()`` 若各写一份，
        迟早漂移，而 spec §14.3 的运行报告会照抄其中一份。

        P0-3：记账后检查 ``max_cost_usd`` / ``max_llm_calls`` 硬上限，超限抛
        ``BudgetExhaustedError``。ledger 事件先于异常发出（触发超限的那次调用
        也必须留痕），Crew 捕获异常后优雅收尾。
        """
        cost = estimate_cost(
            result.model, result.prompt_tokens, result.completion_tokens)
        self._llm_calls += 1
        self._total_prompt_tokens += result.prompt_tokens
        self._total_completion_tokens += result.completion_tokens
        self._total_cost_usd += cost
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
        # P0-3：预算硬上限检查（ledger 事件已发出，超限调用可追溯）。
        # 严格大于才抛：上限本身包含在内（max_llm_calls=1 允许 1 次成功调用）。
        if (self._settings.max_cost_usd is not None
                and self._total_cost_usd > self._settings.max_cost_usd):
            raise BudgetExhaustedError(
                kind="cost_usd", current=self._total_cost_usd,
                limit=self._settings.max_cost_usd)
        if (self._settings.max_llm_calls is not None
                and self._llm_calls > self._settings.max_llm_calls):
            raise BudgetExhaustedError(
                kind="llm_calls", current=float(self._llm_calls),
                limit=float(self._settings.max_llm_calls))

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
        implementation = patch.get(
            "implementation", PROVIDER_PRESETS[binding.provider].implementation)
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
        """
        if binding.provider == "ollama":
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
    """

    def __init__(self, provider: BaseProvider) -> None:
        super().__init__(provider.binding)
        self._provider = provider
        self.results: list[CompletionResult] = []

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        result = await self._provider.complete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format)
        self.results.append(result)
        return result
