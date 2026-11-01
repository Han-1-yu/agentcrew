"""P2 预算控制增强测试。

覆盖 :class:`~agentcrew.models.factory.ModelFactory` 的软阈值行为：

- 累计成本首次达 ``budget_warning_threshold``（默认 80%）时落 ``budget_warning``；
- 达 ``budget_downgrade_threshold``（默认 90%）且存在更便宜备模型时切便宜模型；
- 无更便宜备模型时不降级，保持原行为（达 100% 抛 BudgetExhaustedError）。

用可编程的假 Provider（binding 自带 input/output_price）精确控制每次调用成本，
避免依赖真实网络与价目表漂移。
"""

from collections.abc import Sequence

import pytest

from agentcrew.config import Settings
from agentcrew.models.config import ModelBinding, ProviderCapabilities
from agentcrew.models.factory import BudgetExhaustedError, ModelFactory
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.runtime.ledger import Ledger


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


class _PricedProvider(BaseProvider):
    """按 binding 自带单价记账的假 Provider；每次返回固定 completion token 数。"""

    def __init__(
        self,
        alias: str,
        model: str,
        *,
        input_price: float,
        output_price: float,
        completion_tokens: int = 1000,
    ) -> None:
        binding = ModelBinding(
            alias=alias,
            provider=alias.split(":", 1)[0],
            model=model,
            implementation="openai_compat",
            base_url=None,
            api_key_env="OPENAI_API_KEY",
            capabilities=ProviderCapabilities(max_context_tokens=128_000),
            input_price=input_price,
            output_price=output_price,
        )
        super().__init__(binding)
        self.calls = 0
        self._ct = completion_tokens

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        self.calls += 1
        return CompletionResult(
            text="x",
            provider=self.binding.provider,
            model=self.binding.model,
            prompt_tokens=0,
            completion_tokens=self._ct,
            latency_ms=1,
        )


def _wire(f: ModelFactory, providers: dict[str, BaseProvider], monkeypatch) -> None:
    """把工厂的 ``_resolve`` 接到可编程假 Provider 上。"""
    monkeypatch.setattr(f, "_resolve", lambda alias: providers[alias])


# gpt-4o：input 2.50 / output 10.00 → 每次 1000 completion tokens = $0.01。
_EXPENSIVE_PRICE = dict(input_price=2.50, output_price=10.00)
# gpt-4o-mini：input 0.15 / output 0.60 → 每次 = $0.0006。
_CHEAP_PRICE = dict(input_price=0.15, output_price=0.60)
# claude-opus：input 15.00（比 gpt-4o 更贵，用于「无更便宜选项」用例）。
_PRICIER_PRICE = dict(input_price=15.00, output_price=75.00)


async def test_budget_warning_emitted(settings, tmp_path, monkeypatch):
    """累计成本达 80% 时落一条 ``budget_warning``，只警告不中断。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = settings.model_copy(update={"max_cost_usd": 0.05})
    f = ModelFactory(s, ledger=lg)
    expensive = _PricedProvider("openai:gpt-4o", "gpt-4o", **_EXPENSIVE_PRICE)
    _wire(f, {"openai:gpt-4o": expensive}, monkeypatch)

    # 前 3 次：$0.01 / $0.02 / $0.03，均 < 80% ($0.04)，不警告。
    for _i in range(3):
        await f.call("openai:gpt-4o", [{"role": "user", "content": "x"}],
                     temperature=0, max_tokens=10,
                     purpose="execute", role="r")
    assert lg.count("budget_warning") == 0

    # 第 4 次：$0.04 = 80%，触发一次警告。
    await f.call("openai:gpt-4o", [{"role": "user", "content": "x"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    warnings = list(lg.events("budget_warning"))
    assert len(warnings) == 1
    assert warnings[0].data["current_cost_usd"] == pytest.approx(0.04)
    assert warnings[0].data["limit_usd"] == 0.05
    assert warnings[0].data["pct"] == pytest.approx(0.8)

    # 第 5 次：$0.05 = 100%，未超（严格大于才抛），警告不重复刷。
    await f.call("openai:gpt-4o", [{"role": "user", "content": "x"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    assert lg.count("budget_warning") == 1


async def test_budget_downgrade_to_cheaper_model(settings, tmp_path, monkeypatch):
    """达 90% 时切到更便宜备模型；后续调用走便宜模型。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = settings.model_copy(update={
        "max_cost_usd": 0.10,
        "model_failover": {"openai:gpt-4o": ["openai:gpt-4o-mini"]},
    })
    f = ModelFactory(s, ledger=lg)
    expensive = _PricedProvider("openai:gpt-4o", "gpt-4o", **_EXPENSIVE_PRICE)
    cheap = _PricedProvider("openai:gpt-4o-mini", "gpt-4o-mini", **_CHEAP_PRICE)
    _wire(f, {"openai:gpt-4o": expensive, "openai:gpt-4o-mini": cheap}, monkeypatch)

    # 前 9 次都走 gpt-4o：第 9 次后 $0.09 = 90%，触发降级。
    for _i in range(9):
        await f.call("openai:gpt-4o", [{"role": "user", "content": "x"}],
                     temperature=0, max_tokens=10,
                     purpose="execute", role="r")
    assert expensive.calls == 9
    assert cheap.calls == 0
    assert f._budget_downgrade_to == "openai:gpt-4o-mini"
    downgrades = list(lg.events("budget_downgrade"))
    assert len(downgrades) == 1
    assert downgrades[0].data["from_model"] == "gpt-4o"
    assert downgrades[0].data["to_alias"] == "openai:gpt-4o-mini"

    # 第 10 次调用仍请求 gpt-4o，但工厂自动切到便宜模型。
    await f.call("openai:gpt-4o", [{"role": "user", "content": "x"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    assert cheap.calls == 1
    assert expensive.calls == 9          # 贵模型不再被调用


async def test_budget_no_downgrade_without_cheaper_option(settings, tmp_path, monkeypatch):
    """备模型更贵时不降级；达 100% 后仍抛 BudgetExhaustedError（原行为）。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = settings.model_copy(update={
        "max_cost_usd": 0.10,
        "model_failover": {"openai:gpt-4o": ["anthropic:claude-opus"]},
    })
    f = ModelFactory(s, ledger=lg)
    expensive = _PricedProvider("openai:gpt-4o", "gpt-4o", **_EXPENSIVE_PRICE)
    pricier = _PricedProvider("anthropic:claude-opus", "claude-opus", **_PRICIER_PRICE)
    _wire(f, {"openai:gpt-4o": expensive, "anthropic:claude-opus": pricier},
          monkeypatch)

    # 前 10 次：$0.10 = 100%（未严格超过），不抛；90% 时因无更便宜选项不降级。
    for _i in range(10):
        await f.call("openai:gpt-4o", [{"role": "user", "content": "x"}],
                     temperature=0, max_tokens=10,
                     purpose="execute", role="r")
    assert f._budget_downgrade_to is None
    assert lg.count("budget_downgrade") == 0

    # 第 11 次：$0.11 > $0.10，抛 BudgetExhaustedError（原硬上限行为不变）。
    with pytest.raises(BudgetExhaustedError) as ei:
        await f.call("openai:gpt-4o", [{"role": "user", "content": "x"}],
                     temperature=0, max_tokens=10,
                     purpose="execute", role="r")
    assert ei.value.kind == "cost_usd"


async def test_no_budget_limit_means_no_warning_no_downgrade(settings, tmp_path, monkeypatch):
    """max_cost_usd=None 时软阈值完全空转（向后兼容）。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    expensive = _PricedProvider("openai:gpt-4o", "gpt-4o", **_EXPENSIVE_PRICE)
    _wire(f, {"openai:gpt-4o": expensive}, monkeypatch)
    for _i in range(20):
        await f.call("openai:gpt-4o", [{"role": "user", "content": "x"}],
                     temperature=0, max_tokens=10,
                     purpose="execute", role="r")
    assert lg.count("budget_warning") == 0
    assert lg.count("budget_downgrade") == 0
    assert f._budget_downgrade_to is None
