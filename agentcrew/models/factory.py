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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from agentcrew.config import Settings
from agentcrew.models import structured
from agentcrew.models.complexity import evaluate as evaluate_complexity
from agentcrew.models.config import PROVIDER_PRESETS, ModelBinding, parse_alias
from agentcrew.models.pricing import estimate_cost
from agentcrew.models.providers.anthropic import AnthropicProvider
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
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

if TYPE_CHECKING:
    from agentcrew.task.task import Task

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

    # ── 只读视图 ──────────────────────────────────────────────────────────

    @property
    def settings(self) -> Settings:
        """生效配置（BaseAgent 解析角色级模型绑定的三级优先级要用）。"""
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
        """
        role_binding = self._settings.role_bindings.get(task.role)
        if role_binding:
            return role_binding
        level = evaluate_complexity(task)
        tier = self._settings.model_tiers.get(level.value)
        if tier:
            return tier
        return self._settings.default_model

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
        """
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
        """
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

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _failover_aliases(self, alias: str) -> list[str]:
        """返回 ``[primary] + 备用别名`` 的尝试列表（P0-2 故障转移）。

        去重保序：备用列表里若重复出现 primary 或自身重复，只留首次 —— 避免
        同一个不可用 provider 被反复尝试。未配置 failover 时返回 ``[alias]``，
        行为与改动前完全一致（向后兼容）。
        """
        backups = self._settings.model_failover.get(alias, [])
        seen: set[str] = {alias}
        result = [alias]
        for backup in backups:
            if backup not in seen:
                seen.add(backup)
                result.append(backup)
        return result

    def _emit_failover(self, from_alias: str, to_alias: str, exc: Exception) -> None:
        """记一条 ``llm_failover`` 事件（P0-2：诚实留痕）。

        每次从一个别名切换到下一个备用别名时调用，含 from_alias / to_alias /
        error 三个字段。操作者事后可从 ledger 看出「哪个模型挂了、切到了谁、
        原始错误是什么」。没有 ledger 时静默跳过（纯逻辑测试）。
        """
        if self._ledger is None:
            return
        self._ledger.emit(
            "llm_failover",
            from_alias=from_alias,
            to_alias=to_alias,
            error=f"{type(exc).__name__}: {exc}",
        )

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
