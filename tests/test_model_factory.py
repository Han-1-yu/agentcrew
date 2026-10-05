"""ModelFactory：别名解析、Provider 缓存、调用限流与记账（spec §7.3/§11.2/§12.2/§13.2/§16.1）。

brief 用例原样保留；在其之上补五件事：

- Task 1 的 carry-forward：``api_key_for`` 的逐 provider 分支与 ``secret_values``
  去重此前无测试覆盖（安全相关），折入本任务；
- ``settings.mock`` 是**全局**开关：必须在没有任何 Key 的环境里对每个别名成立，
  且用 ``mock`` 预设的能力位（不吃别名的 capability 覆盖）；
- ``DEFAULT_FIXTURES_DIR`` 的模块级常量身份：Task 34 的 Crew 与测试都靠
  monkeypatch 它注入临时 fixtures；
- 信号量只包住 ``complete()``：记账（ledger.emit）发生在释放之后；
- Task 18 的 carry-forward：``Retry-After`` 截断事实必须经 ``on_retry`` 落到
  ledger，否则操作者只能看到一次 300 秒的无解释等待；
- 控制器 FINDING：结构化输出（PM 拆解、G2 评审的实际路径）必须走工厂的受治理
  入口 —— 同一个信号量、同一段记账；降级/修复里失败的调用同样花了 token。
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agentcrew.config import Settings
from agentcrew.models.config import parse_alias
from agentcrew.models.factory import (
    DEFAULT_FIXTURES_DIR,
    ModelFactory,
    ModelFactoryError,
)
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.models.providers.openai_compat import (
    MAX_RETRY_AFTER_SECONDS,
    OpenAICompatProvider,
)
from agentcrew.models.structured import StructuredOutputError
from agentcrew.runtime.ledger import Ledger
from agentcrew.utils.retry import TransientError

#: spec §17.2 的六个 Key 环境变量（mock/ollama 之外的全部 provider）。
_KEY_ENV_VARS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "ARK_API_KEY", "DASHSCOPE_API_KEY", "QIANFAN_API_KEY",
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


@pytest.fixture
def no_sleep(monkeypatch):
    """把 ``asyncio.sleep`` 换成记录器（生产重试参数真睡一轮要 7 秒）。"""
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


def test_get_returns_cached_instance(settings):
    f = ModelFactory(settings)
    assert f.get("mock:m") is f.get("mock:m")


def test_mock_alias_returns_mock_provider(settings):
    assert isinstance(ModelFactory(settings).get("mock:m"), MockProvider)


def test_unknown_alias_raises(settings):
    with pytest.raises(Exception):
        ModelFactory(settings).get("wizard:merlin")


async def test_call_records_tokens_and_cost(settings, tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    p = f.get("mock:m")
    set_purpose(p, "execute", role="r", task_id="T-1", attempt=1)
    await f.call("mock:m", [{"role": "user", "content": "hello"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    assert f.llm_calls == 1
    assert f.total_prompt_tokens > 0
    assert lg.count("llm_call") == 1


async def test_call_emits_purpose_and_role(settings, tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    p = f.get("mock:m"); set_purpose(p, "review", role="architect", task_id="T-1", attempt=1)
    await f.call("mock:m", [{"role": "user", "content": "x"}],
                 temperature=0, max_tokens=10, purpose="review", role="architect")
    ev = list(lg.events("llm_call"))[-1]
    assert ev.data["purpose"] == "review" and ev.data["role"] == "architect"


async def test_semaphore_caps_concurrency(settings, tmp_path):
    settings = settings.model_copy(update={"max_concurrency": 2})
    f = ModelFactory(settings, ledger=None)
    p = f.get("mock:m")
    peak = 0
    live = 0
    orig = p.complete

    async def slow(*a, **kw):
        nonlocal peak, live
        live += 1; peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return await orig(*a, **kw)

    p.complete = slow
    set_purpose(p, "execute", role="r", task_id="T-1", attempt=1)
    await asyncio.gather(*[
        f.call("mock:m", [{"role": "user", "content": "x"}],
               temperature=0, max_tokens=10, purpose="execute", role="r")
        for _ in range(8)])
    assert peak <= 2


async def test_reset_stats_zeroes_counters(settings, tmp_path):
    f = ModelFactory(settings)
    p = f.get("mock:m"); set_purpose(p, "execute", role="r", task_id="T-1", attempt=1)
    await f.call("mock:m", [{"role": "user", "content": "x"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    f.reset_stats()
    assert f.llm_calls == 0 and f.total_cost_usd == 0.0


def test_missing_api_key_raises_at_construction(settings, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = settings.model_copy(update={"mock": False})
    with pytest.raises(ModelFactoryError) as ei:
        ModelFactory(s).get("openai:gpt-4o-mini")
    assert "OPENAI_API_KEY" in str(ei.value)


def test_ollama_needs_no_real_key(settings):
    f = ModelFactory(settings)
    assert f.get("ollama:llama3") is not None


# ── spec §11.2：预算与模型窗口的关系校验（退出码 3） ──
def test_context_budget_fits_large_window(settings):
    f = ModelFactory(settings)
    f.validate_context_budget({"default": "openai:gpt-4o-mini"})     # 不抛


def test_context_budget_exceeds_small_window_raises(settings, monkeypatch):
    from agentcrew.models.factory import ContextBudgetError
    s = settings.model_copy(update={"context_budget_tokens": 200_000})
    f = ModelFactory(s)
    with pytest.raises(ContextBudgetError) as ei:
        f.validate_context_budget({"default": "openai:gpt-4o-mini"})
    assert ei.value.alias == "openai:gpt-4o-mini"
    assert ei.value.suggested_budget > 0
    assert ei.value.suggested_budget < 200_000


def test_context_budget_checks_every_bound_alias(settings, monkeypatch):
    """同一 run 内不同角色可能绑不同模型，必须逐个校验。"""
    from agentcrew.models.config import ModelBinding, ProviderCapabilities
    from agentcrew.models.factory import ContextBudgetError
    s = settings.model_copy(update={"context_budget_tokens": 60_000})
    f = ModelFactory(s)
    small = ModelBinding(alias="tinylocal:q", provider="ollama", model="q",
                         base_url=None, api_key_env="X",
                         capabilities=ProviderCapabilities(max_context_tokens=8192))
    monkeypatch.setattr(f, "_resolve", lambda a: small if a == "tinylocal:q"
                        else ModelBinding(alias=a, provider="openai", model="m",
                                          base_url=None, api_key_env="OPENAI_API_KEY",
                                          capabilities=ProviderCapabilities(
                                              max_context_tokens=128_000)))
    with pytest.raises(ContextBudgetError):
        f.validate_context_budget({"default": "openai:m", "qa_engineer": "tinylocal:q"})


def test_fallback_event_emitted_for_missing_fixture(settings, tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    fx = tmp_path / "fx"; (fx / "responses").mkdir(parents=True)
    f = ModelFactory(settings, ledger=lg, fixtures_dir=fx)
    p = f.get("mock:m")
    set_purpose(p, "execute", role="r", task_id="MISSING", attempt=1)
    asyncio.run(f.call("mock:m", [{"role": "user", "content": "x"}],
                       temperature=0, max_tokens=10, purpose="execute", role="r"))
    assert lg.count("mock_fallback") == 1


# ── 以下为 brief 之外的补强用例 ─────────────────────────────────────────────


class _RecorderProvider(BaseProvider):
    """只记录 ``complete`` 入参的假 Provider（钉死 call() 的转发与记账）。"""

    def __init__(self) -> None:
        super().__init__(parse_alias("mock:m"))
        self.calls: list[dict[str, object]] = []
        self.result = CompletionResult(
            text="ok", provider="mock", model="m",
            prompt_tokens=3, completion_tokens=2, latency_ms=7)

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": response_format,
        })
        return self.result


async def test_call_forwards_kwargs_and_logs_exact_fields(
        settings, monkeypatch, tmp_path):
    """``llm_call`` 事件的字段集即 spec §13.2 的七项契约，多一个少一个都算漂移。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    recorder = _RecorderProvider()
    monkeypatch.setattr(f, "_resolve", lambda alias: recorder)

    result = await f.call(
        "mock:m", [{"role": "user", "content": "hi"}],
        temperature=0.25, max_tokens=64, purpose="execute", role="backend_dev",
        response_format={"type": "json_object"})

    assert result is recorder.result
    assert recorder.calls == [{
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.25,
        "max_tokens": 64,
        "response_format": {"type": "json_object"},
    }]
    ev = list(lg.events("llm_call"))[-1]
    assert ev.data == {
        "role": "backend_dev",
        "model": "m",
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "estimated_cost_usd": 0.0,
        "latency_ms": 7,
        "purpose": "execute",
    }


async def test_semaphore_is_released_before_accounting(settings, tmp_path, monkeypatch):
    """信号量只包住 ``complete()``：本地记账不得占着全局 LLM 调用额度。"""
    s = settings.model_copy(update={"max_concurrency": 1})
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(s, ledger=lg)
    p = f.get("mock:m")
    set_purpose(p, "execute", role="r", task_id="T-1", attempt=1)
    inside_call: list[int] = []
    original_complete = p.complete

    async def spy_complete(*a, **kw):
        # asyncio.Semaphore 无公开只读接口，用 _value 白盒断言（容量 1）。
        inside_call.append(f.llm_semaphore._value)
        return await original_complete(*a, **kw)

    p.complete = spy_complete
    emitted: list[tuple[str, int]] = []
    original_emit = lg.emit

    def spy_emit(event, **data):
        emitted.append((event, f.llm_semaphore._value))
        return original_emit(event, **data)

    monkeypatch.setattr(lg, "emit", spy_emit)
    await f.call("mock:m", [{"role": "user", "content": "x"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    assert inside_call == [0]                       # 调用期间持有
    assert [value for name, value in emitted if name == "llm_call"] == [1]


async def test_mock_mode_still_records_cost_for_priced_model(settings, tmp_path):
    """mock 下保留原模型名，成本统计在看板里才是真实可演示的（spec §16.1）。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    p = f.get("mock:gpt-4o-mini")
    set_purpose(p, "execute", role="r", task_id="T-1", attempt=1)
    await f.call("mock:gpt-4o-mini", [{"role": "user", "content": "hello"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    ev = list(lg.events("llm_call"))[-1]
    assert ev.data["model"] == "gpt-4o-mini"
    assert ev.data["estimated_cost_usd"] > 0
    assert f.total_cost_usd == pytest.approx(ev.data["estimated_cost_usd"])


def test_mock_switch_overrides_every_alias_without_keys(settings, monkeypatch):
    """``--mock`` 是全局开关：无任何 Key 的环境里每个别名都必须可用。"""
    for name in _KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    f = ModelFactory(settings)
    aliases = ("openai:gpt-4o", "anthropic:claude-sonnet-5",
               "deepseek:deepseek-chat", "doubao:doubao-seed",
               "qwen:qwen-max", "ernie:ernie-4.0",
               "ollama:llama3", "mock:m")
    for alias in aliases:
        provider = f.get(alias)
        assert isinstance(provider, MockProvider), alias
        assert provider.binding.provider == "mock", alias
        assert provider.binding.model == alias.partition(":")[2], alias


def test_mock_switch_uses_mock_preset_capabilities(settings):
    """mock 覆盖用 ``mock`` 预设的能力位，而不是别名原 provider 的能力位。"""
    s = settings.model_copy(update={"provider_overrides": {
        "openai": {"capabilities": {"max_context_tokens": 8192,
                                    "max_output_tokens": 111}}}})
    provider = ModelFactory(s).get("openai:gpt-4o-mini")
    assert isinstance(provider, MockProvider)
    assert provider.capabilities.max_context_tokens == 128_000
    assert provider.capabilities.max_output_tokens == 8192


def test_settings_property_is_read_only(settings):
    f = ModelFactory(settings)
    assert f.settings is settings
    with pytest.raises(AttributeError):
        f.settings = settings.model_copy(update={"max_concurrency": 99})


def test_stats_are_per_instance(settings, tmp_path):
    """计数器属实例，不共享（两个 ModelFactory 各自记账）。"""
    f1 = ModelFactory(settings)
    f2 = ModelFactory(settings)
    p = f1.get("mock:m"); set_purpose(p, "execute", role="r", task_id="T-1", attempt=1)
    asyncio.run(f1.call("mock:m", [{"role": "user", "content": "x"}],
                        temperature=0, max_tokens=10, purpose="execute", role="r"))
    assert f1.llm_calls == 1
    assert f2.llm_calls == 0 and f2.total_prompt_tokens == 0


def test_implementation_override_dispatches_to_mock(settings):
    """``provider_overrides`` 可以把某个 provider 换成 mock 实现（离线替身）。

    生效的能力位同样换成 ``mock`` 预设的：能力位描述的是**实际在跑的实现**，
    不是别名原本声明的 provider。
    """
    s = settings.model_copy(update={
        "mock": False,
        "provider_overrides": {"openai": {
            "implementation": "mock",
            "capabilities": {"max_context_tokens": 8192}}}})
    provider = ModelFactory(s).get("openai:gpt-4o-mini")   # 无 Key 也构造成功
    assert isinstance(provider, MockProvider)
    assert provider.binding.provider == "mock"
    assert provider.binding.model == "gpt-4o-mini"
    assert provider.capabilities.max_context_tokens == 128_000


def test_invalid_implementation_override_fails_closed(settings):
    s = settings.model_copy(update={
        "mock": False,
        "provider_overrides": {"openai": {"implementation": "openai-compat"}}})
    with pytest.raises(ModelFactoryError) as ei:
        ModelFactory(s).get("openai:gpt-4o-mini")
    assert "implementation" in str(ei.value)


def test_default_fixtures_dir_is_module_level_and_read_at_construction(
        settings, tmp_path, monkeypatch):
    """Task 34 的 Crew 与测试都靠 monkeypatch 这个模块级常量注入 fixtures。"""
    expected = Path(__file__).resolve().parents[1] / "examples" / "fixtures"
    assert DEFAULT_FIXTURES_DIR == expected
    fx = tmp_path / "fx"
    (fx / "responses" / "T-9").mkdir(parents=True)
    (fx / "responses" / "T-9" / "attempt-1.md").write_text("MARKER", encoding="utf-8")
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", fx)
    f = ModelFactory(settings, ledger=None)          # fixtures_dir=None → 模块常量
    p = f.get("mock:m")
    set_purpose(p, "execute", role="r", task_id="T-9", attempt=1)
    result = asyncio.run(f.call("mock:m", [{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10,
                                purpose="execute", role="r"))
    assert result.text == "MARKER"


# ── 控制器 FINDING：结构化输出走工厂的受治理入口 ──

class _Answer(BaseModel):
    """测试用结构化输出 schema。"""

    value: int


class _StructuredProvider(BaseProvider):
    """可编程的假 Provider：脚本化文本 + 可注入延迟（钉结构化路径与并发）。"""

    def __init__(
        self,
        *,
        alias: str = "openai:m",
        texts: Sequence[str] = ('{"value": 1}',),
        delay: float = 0.0,
    ) -> None:
        super().__init__(parse_alias(alias))
        self._texts = list(texts)
        self.delay = delay
        self.calls = 0
        self.live = 0
        self.peak = 0

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        self.calls += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.live -= 1
        text = self._texts[min(self.calls - 1, len(self._texts) - 1)]
        return CompletionResult(
            text=text, provider=self.binding.provider, model=self.binding.model,
            prompt_tokens=5, completion_tokens=2, latency_ms=3)


async def test_structured_emits_one_llm_call_with_purpose(
        settings, monkeypatch, tmp_path):
    """第 1 级一次成功 → 恰好一条 ``llm_call``，字段与 ``call()`` 同形。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings.model_copy(update={"max_concurrency": 1}), ledger=lg)
    provider = _StructuredProvider()
    monkeypatch.setattr(f, "_resolve", lambda alias: provider)
    emitted: list[int] = []
    original_emit = lg.emit

    def spy_emit(event, **data):
        if event == "llm_call":
            emitted.append(f.llm_semaphore._value)
        return original_emit(event, **data)

    monkeypatch.setattr(lg, "emit", spy_emit)
    answer = await f.complete_structured(
        "openai:m", [{"role": "user", "content": "x"}], _Answer,
        temperature=0, max_tokens=10, purpose="review", role="architect")

    assert answer == _Answer(value=1)
    assert provider.calls == 1
    assert f.llm_calls == 1
    assert f.total_prompt_tokens == 5 and f.total_completion_tokens == 2
    assert emitted == [1]                       # 记账在信号量释放之后
    ev = list(lg.events("llm_call"))[-1]
    assert ev.data == {
        "role": "architect",
        "model": "m",
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "estimated_cost_usd": 0.0,
        "latency_ms": 3,
        "purpose": "review",
    }


async def test_structured_semaphore_caps_concurrency(settings, monkeypatch):
    """结构化调用与 ``call()`` 共享同一个全局信号量（spec §7.3：G2 也计入）。"""
    s = settings.model_copy(update={"max_concurrency": 2})
    f = ModelFactory(s, ledger=None)
    provider = _StructuredProvider(delay=0.02)
    monkeypatch.setattr(f, "_resolve", lambda alias: provider)
    await asyncio.gather(*[
        f.complete_structured("openai:m", [{"role": "user", "content": "x"}], _Answer,
                              temperature=0, max_tokens=10,
                              purpose="review", role="architect")
        for _ in range(8)])
    assert provider.peak <= 2
    assert f.llm_calls == 8


async def test_structured_accounts_every_ladder_attempt(
        settings, monkeypatch, tmp_path):
    """修复循环里失败的那次调用同样花了 token —— 两条都要进报告（§14.3）。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    provider = _StructuredProvider(alias="ernie:e", texts=["不是 JSON", '{"value": 7}'])
    monkeypatch.setattr(f, "_resolve", lambda alias: provider)
    answer = await f.complete_structured(
        "ernie:e", [{"role": "user", "content": "x"}], _Answer,
        temperature=0, max_tokens=10, purpose="review", role="architect")

    assert answer == _Answer(value=7)
    assert provider.calls == 2
    assert lg.count("llm_call") == 2
    assert f.total_prompt_tokens == 10          # 5 + 5，失败那次没被丢


async def test_structured_accounts_consumed_tokens_before_reraising(
        settings, monkeypatch, tmp_path):
    """阶梯耗尽抛错时，已消耗的 token 也必须先记账再抛（否则静默丢账）。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    provider = _StructuredProvider(alias="ernie:e", texts=["不是 JSON"])
    monkeypatch.setattr(f, "_resolve", lambda alias: provider)
    with pytest.raises(StructuredOutputError):
        await f.complete_structured(
            "ernie:e", [{"role": "user", "content": "x"}], _Answer,
            temperature=0, max_tokens=10, purpose="review", role="architect")
    assert provider.calls == 3                  # 1 次 + repair_attempts(2)
    assert lg.count("llm_call") == 3
    assert f.llm_calls == 3


# ── Task 1 carry-forward：Key 解析的逐 provider 行为 ──

@pytest.mark.parametrize("alias,env_name", [
    ("openai:gpt-4o-mini", "OPENAI_API_KEY"),
    ("anthropic:claude-sonnet-5", "ANTHROPIC_API_KEY"),
    ("deepseek:deepseek-chat", "DEEPSEEK_API_KEY"),
    ("doubao:doubao-seed", "ARK_API_KEY"),
    ("qwen:qwen-max", "DASHSCOPE_API_KEY"),
    ("ernie:ernie-4.0", "QIANFAN_API_KEY"),
])
def test_missing_key_error_names_the_env_var(settings, monkeypatch, alias, env_name):
    monkeypatch.delenv(env_name, raising=False)
    f = ModelFactory(settings.model_copy(update={"mock": False}))
    with pytest.raises(ModelFactoryError) as ei:
        f.get(alias)
    assert env_name in str(ei.value)


def test_key_resolution_is_passed_to_provider(settings, monkeypatch):
    """Key 从环境读取后原样交给 Provider；ollama 用占位串 "ollama"（无需真 Key）。

    替换掉两个 Provider 类而不是真构造它们：真实 SDK 客户端的构造在无网环境下
    也要约 6 秒，而本用例要钉的是「哪些构造参数」。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    built: list[tuple[str, str | None, str]] = []

    def fake_openai(binding, *, api_key, on_retry):
        built.append(("openai_compat", api_key, binding.model))
        return binding

    def fake_anthropic(binding, *, api_key, on_retry):
        built.append(("anthropic", api_key, binding.model))
        return binding

    monkeypatch.setattr("agentcrew.models.factory.OpenAICompatProvider", fake_openai)
    monkeypatch.setattr("agentcrew.models.factory.AnthropicProvider", fake_anthropic)
    f = ModelFactory(settings.model_copy(update={"mock": False}))
    f.get("openai:gpt-4o-mini")
    f.get("anthropic:claude-sonnet-5")
    f.get("ollama:llama3")
    assert built == [
        ("openai_compat", "sk-test-openai", "gpt-4o-mini"),
        ("anthropic", "sk-test-anthropic", "claude-sonnet-5"),
        ("openai_compat", "ollama", "llama3"),
    ]


def test_ollama_placeholder_used_even_with_base_url_set(settings, monkeypatch):
    """``OLLAMA_BASE_URL`` 已设时也不能把 base URL 当 key 传给 SDK（占位串先判）。"""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    built: list[str | None] = []

    def fake_openai(binding, *, api_key, on_retry):
        built.append(api_key)
        return binding

    monkeypatch.setattr("agentcrew.models.factory.OpenAICompatProvider", fake_openai)
    ModelFactory(settings.model_copy(update={"mock": False})).get("ollama:llama3")
    assert built == ["ollama"]


def test_api_key_for_mock_and_unknown_provider_is_none(settings):
    assert settings.api_key_for("mock") is None
    assert settings.api_key_for("wizard") is None


def test_secret_values_dedupes_and_strips(settings, monkeypatch):
    for name in _KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-shared  ")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-other")
    assert settings.secret_values() == ["sk-shared", "sk-other"]


# ── Task 18 carry-forward：Retry-After 截断经 on_retry 落 ledger ──

class _Always429Client:
    """注入用的假 SDK 客户端：每次调用都抛 429（可选带 Retry-After 头）。"""

    def __init__(self, retry_after: str | None = None) -> None:
        self._retry_after = retry_after
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: object) -> object:
        self.calls += 1
        exc = Exception("Error code: 429 - rate limited")
        if self._retry_after is not None:
            exc.response = SimpleNamespace(headers={"Retry-After": self._retry_after})
        raise exc


def _patch_openai_provider(monkeypatch, client: _Always429Client) -> None:
    """让 factory 构造的 openai_compat Provider 使用注入的假客户端。"""

    def build(binding, *, api_key, on_retry):
        return OpenAICompatProvider(binding, api_key=api_key, client=client,
                                    on_retry=on_retry)

    monkeypatch.setattr("agentcrew.models.factory.OpenAICompatProvider", build)


async def test_clamped_retry_after_reaches_ledger(
        settings, tmp_path, monkeypatch, no_sleep):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    client = _Always429Client(retry_after="86400")
    _patch_openai_provider(monkeypatch, client)
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings.model_copy(update={"mock": False}), ledger=lg)

    with pytest.raises(TransientError):
        await f.call("openai:gpt-4o-mini", [{"role": "user", "content": "x"}],
                     temperature=0, max_tokens=10, purpose="execute", role="r")

    assert client.calls == 4                        # attempts=4 → 重试 3 次
    retries = list(lg.events("llm_retry"))
    assert [ev.data["attempt"] for ev in retries] == [1, 2, 3]
    for ev in retries:
        assert ev.data["retry_after_clamped_from"] == 86400.0   # 服务端原始要求
        assert ev.data["delay_seconds"] == MAX_RETRY_AFTER_SECONDS
    assert no_sleep == [MAX_RETRY_AFTER_SECONDS] * 3
    assert lg.count("llm_call") == 0                # 失败的调用不记账
    assert f.llm_calls == 0 and f.total_cost_usd == 0.0


async def test_retry_without_clamp_has_no_clamp_field(
        settings, tmp_path, monkeypatch, no_sleep):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    _patch_openai_provider(monkeypatch, _Always429Client(retry_after=None))
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings.model_copy(update={"mock": False}), ledger=lg)

    with pytest.raises(TransientError):
        await f.call("openai:gpt-4o-mini", [{"role": "user", "content": "x"}],
                     temperature=0, max_tokens=10, purpose="execute", role="r")

    retries = list(lg.events("llm_retry"))
    assert len(retries) == 3
    assert all("retry_after_clamped_from" not in ev.data for ev in retries)


async def test_retry_events_are_skipped_without_ledger(
        settings, tmp_path, monkeypatch, no_sleep):
    """没有 ledger 时 on_retry 是无副作用的空操作（重试本身照常进行）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    client = _Always429Client(retry_after="86400")
    _patch_openai_provider(monkeypatch, client)
    f = ModelFactory(settings.model_copy(update={"mock": False}), ledger=None)
    with pytest.raises(TransientError):
        await f.call("openai:gpt-4o-mini", [{"role": "user", "content": "x"}],
                     temperature=0, max_tokens=10, purpose="execute", role="r")
    assert client.calls == 4
