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
  ``ollama`` 是本地服务，用占位串 ``"ollama"``。
- 记账：``call()`` 在信号量**释放之后**累加计数、用 ``estimate_cost`` 估算成本，
  再发一条 ``llm_call`` 事件（spec §13.2 的七个字段）。
- fixture 缺失**绝不静默**（spec §16.1）：``MockProvider`` 的 ``on_fallback``
  接到 ledger 的 ``mock_fallback`` 事件。
- Task 18 的 carry-forward：``Retry-After`` 被截断到 300 秒时，截断事实经
  ``on_retry`` 落到 ledger 的 ``llm_retry`` 事件 —— 操作者看得到「远端要了多久、
  我们实际等多久」，而不是一次无解释的 300 秒等待。
"""

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

from agentcrew.config import Settings
from agentcrew.models.config import PROVIDER_PRESETS, ModelBinding, parse_alias
from agentcrew.models.pricing import estimate_cost
from agentcrew.models.providers.anthropic import AnthropicProvider
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.models.providers.mock import MockProvider
from agentcrew.models.providers.openai_compat import (
    RETRY_AFTER_CLAMPED_ATTR,
    OpenAICompatProvider,
)
from agentcrew.runtime.ledger import Ledger

#: 默认 fixture 根目录（仓库根的 ``examples/fixtures``）。模块级常量是契约的
#: 一部分：测试与 Task 34 的 Crew 都靠 monkeypatch 它注入临时 fixtures。
DEFAULT_FIXTURES_DIR: Path = (
    Path(__file__).resolve().parents[2] / "examples" / "fixtures")

#: ollama 是本地服务（spec §17.2 无需密钥），但 SDK 要求 api_key 非空。
_OLLAMA_PLACEHOLDER = "ollama"

#: 支持的 Provider 实现（与 ``config.Implementation`` 的 Literal 对齐）。
_IMPLEMENTATIONS: tuple[str, ...] = ("openai_compat", "anthropic", "mock")

#: 建议预算相对窗口上限预留的头部空间（spec §11.2 的建议值公式）。
_SUGGESTED_BUDGET_HEADROOM = 1024


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
        """
        provider = self.get(alias)
        async with self._llm_semaphore:
            result = await provider.complete(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format)
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
        return result

    # ── 内部 ──────────────────────────────────────────────────────────────

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
        """取 ``binding.provider`` 的 API Key；缺失时抛错并点名环境变量。"""
        api_key = self._settings.api_key_for(binding.provider)
        if api_key:
            return api_key
        if binding.provider == "ollama":
            return _OLLAMA_PLACEHOLDER
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
