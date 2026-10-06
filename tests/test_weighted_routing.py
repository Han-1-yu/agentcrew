"""P3-10：权重优先级路由测试。

覆盖：
- 权重格式解析正确（dict → WeightedAlias，字符串 → weight=1.0）
- _select_weighted 按权重分布选择
- 失败时按剩余权重重选
- unavailable 别名权重置 0
- degraded 别名权重减半
- 所有权重为 0 时回退到第一个
- 旧格式（纯字符串列表）行为不变
- ledger 记录 llm_routed 事件
- config 中 model_failover 权重格式正常加载
"""

import random
from collections.abc import Sequence

import pytest

from agentcrew.config import Settings
from agentcrew.models.config import parse_alias
from agentcrew.models.factory import (
    ModelFactory,
    WeightedAlias,
)
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.models.providers.mock import set_purpose
from agentcrew.runtime.ledger import Ledger
from agentcrew.utils.retry import TransientError


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


class _ScriptedProvider(BaseProvider):
    """可编程假 Provider：按脚本依次返回结果或抛异常。"""

    def __init__(self, alias: str, script: list[object]) -> None:
        """script 中每个元素是 CompletionResult 或 Exception。"""
        super().__init__(parse_alias(alias))
        self._script = list(script)
        self.calls = 0

    async def complete(self, messages: Sequence[Message], *,
                       temperature: float, max_tokens: int,
                       response_format: ResponseFormat | None = None) -> CompletionResult:
        idx = min(self.calls, len(self._script) - 1)
        self.calls += 1
        item = self._script[idx]
        if isinstance(item, Exception):
            raise item
        return item


def _ok_result(alias: str) -> CompletionResult:
    return CompletionResult(
        text="ok", provider=alias.split(":")[0], model=alias.split(":", 1)[1],
        prompt_tokens=1, completion_tokens=1, latency_ms=10)


# ── 解析 ─────────────────────────────────────────────────────────────────


def test_parse_string_entry_gives_weight_one(settings):
    """字符串项解析为 weight=1.0。"""
    wa = ModelFactory._parse_failover_entry("deepseek:v3")
    assert wa.alias == "deepseek:v3"
    assert wa.weight == 1.0


def test_parse_dict_entry_reads_alias_and_weight(settings):
    """dict 项解析 alias 与 weight。"""
    wa = ModelFactory._parse_failover_entry({"alias": "deepseek:v3", "weight": 0.6})
    assert wa.alias == "deepseek:v3"
    assert wa.weight == 0.6


def test_parse_dict_entry_default_weight(settings):
    """dict 项缺 weight 时默认 1.0。"""
    wa = ModelFactory._parse_failover_entry({"alias": "anthropic:claude"})
    assert wa.weight == 1.0


# ── _build_weighted_pool ─────────────────────────────────────────────────


def test_build_pool_returns_none_for_string_list(settings):
    """纯字符串列表 → None（走旧有序路径）。"""
    s = settings.model_copy(update={
        "model_failover": {"openai:gpt-4o": ["deepseek:v3", "anthropic:claude"]}})
    f = ModelFactory(s)
    assert f._build_weighted_pool("openai:gpt-4o") is None


def test_build_pool_returns_list_for_weighted(settings):
    """含 dict 项 → 返回权重池。"""
    s = settings.model_copy(update={
        "model_failover": {"openai:gpt-4o": [
            {"alias": "deepseek:v3", "weight": 0.6},
            {"alias": "anthropic:claude", "weight": 0.4},
        ]}})
    f = ModelFactory(s)
    pool = f._build_weighted_pool("openai:gpt-4o")
    assert pool is not None
    # primary + 2 backups
    assert len(pool) == 3
    assert pool[0].alias == "openai:gpt-4o"
    assert pool[0].weight == 1.0
    assert pool[1].alias == "deepseek:v3"
    assert pool[1].weight == 0.6
    assert pool[2].alias == "anthropic:claude"
    assert pool[2].weight == 0.4


def test_build_pool_dedupes(settings):
    """池中重复别名去重。"""
    s = settings.model_copy(update={
        "model_failover": {"openai:gpt-4o": [
            {"alias": "deepseek:v3", "weight": 0.6},
            {"alias": "deepseek:v3", "weight": 0.3},  # 重复
            {"alias": "anthropic:claude", "weight": 0.4},
        ]}})
    f = ModelFactory(s)
    pool = f._build_weighted_pool("openai:gpt-4o")
    assert pool is not None
    aliases = [wa.alias for wa in pool]
    assert aliases.count("deepseek:v3") == 1


# ── _select_weighted ──────────────────────────────────────────────────────


def test_select_weighted_distribution(settings):
    """按权重比例选择（运行多次统计频率）。"""
    f = ModelFactory(settings)
    pool = [
        WeightedAlias(alias="a", weight=0.7),
        WeightedAlias(alias="b", weight=0.3),
    ]
    rng = random.Random(42)
    counts = {"a": 0, "b": 0}
    for _ in range(2000):
        chosen, reason = f._select_weighted(pool, rng=rng)
        counts[chosen] += 1
        assert reason == "weighted"
    # 允许 5% 误差
    assert counts["a"] > counts["b"]
    assert abs(counts["a"] / 2000 - 0.7) < 0.08


def test_select_weighted_unavailable_weight_zero(settings):
    """unavailable 的别名权重置 0（不被选中）。"""
    from datetime import datetime
    from agentcrew.models.factory import HealthRecord
    f = ModelFactory(settings)
    f._health["a"] = HealthRecord(
        alias="a", status="unavailable", last_check=datetime.now())
    pool = [
        WeightedAlias(alias="a", weight=0.7),
        WeightedAlias(alias="b", weight=0.3),
    ]
    rng = random.Random(42)
    for _ in range(100):
        chosen, reason = f._select_weighted(pool, rng=rng)
        assert chosen == "b"
        assert reason == "health_adjusted"


def test_select_weighted_degraded_weight_halved(settings):
    """degraded 的别名权重减半。"""
    from datetime import datetime
    from agentcrew.models.factory import HealthRecord
    f = ModelFactory(settings)
    # a degraded: weight 0.7 → 0.35；b healthy: weight 0.3
    f._health["a"] = HealthRecord(
        alias="a", status="degraded", latency_ms=3000.0,
        last_check=datetime.now())
    pool = [
        WeightedAlias(alias="a", weight=0.7),
        WeightedAlias(alias="b", weight=0.3),
    ]
    rng = random.Random(42)
    counts = {"a": 0, "b": 0}
    for _ in range(2000):
        chosen, reason = f._select_weighted(pool, rng=rng)
        counts[chosen] += 1
        assert reason == "health_adjusted"
    # a 的有效权重 0.35 / (0.35+0.3) ≈ 54%
    assert abs(counts["a"] / 2000 - 0.54) < 0.08


def test_select_weighted_all_zero_falls_back_to_first(settings):
    """所有权重为 0 时回退到第一个别名。"""
    from datetime import datetime
    from agentcrew.models.factory import HealthRecord
    f = ModelFactory(settings)
    for a in ("a", "b"):
        f._health[a] = HealthRecord(
            alias=a, status="unavailable", last_check=datetime.now())
    pool = [
        WeightedAlias(alias="a", weight=0.7),
        WeightedAlias(alias="b", weight=0.3),
    ]
    chosen, reason = f._select_weighted(pool)
    assert chosen == "a"
    assert reason == "fallback_first"


# ── call() 权重路由集成 ──────────────────────────────────────────────────


async def test_weighted_call_selects_by_weight(settings, tmp_path, monkeypatch):
    """权重格式 call() 按权重选择，记 llm_routed 事件。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = settings.model_copy(update={
        "model_failover": {"openai:primary": [
            {"alias": "openai:a", "weight": 1.0},
            {"alias": "openai:b", "weight": 0.0},
        ]}})
    f = ModelFactory(s, ledger=lg)
    # primary 权重设为极高（通过 _health 或直接改 pool），确保先选 primary 失败
    provider_primary = _ScriptedProvider("openai:primary", [TransientError("down")])
    provider_a = _ScriptedProvider("openai:a", [_ok_result("openai:a")])
    provider_b = _ScriptedProvider("openai:b", [_ok_result("openai:b")])

    def _resolve(alias):
        return {"openai:primary": provider_primary,
                "openai:a": provider_a,
                "openai:b": provider_b}[alias]

    monkeypatch.setattr(f, "_resolve", _resolve)
    result = await f.call("openai:primary", [{"role": "user", "content": "x"}],
                           temperature=0, max_tokens=10,
                           purpose="execute", role="r")
    # b 权重为 0，永远不应被选中
    assert provider_b.calls == 0
    # 最终成功的是 a（primary 失败后重选到 a，或 a 直接被选中）
    assert provider_a.calls == 1
    routed = list(lg.events("llm_routed"))
    assert len(routed) >= 1
    # 最后一次路由选中的是 a（成功的那个）
    assert routed[-1].data["selected_alias"] == "openai:a"


async def test_weighted_call_rerolls_after_failure(settings, tmp_path, monkeypatch):
    """失败后从剩余别名按权重重选。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = settings.model_copy(update={
        "model_failover": {"openai:primary": [
            {"alias": "openai:a", "weight": 1.0},
            {"alias": "openai:b", "weight": 0.0},  # 权重 0 → 最后兜底
        ]}})
    f = ModelFactory(s, ledger=lg)
    # primary 和 a 都失败，b 成功。
    provider_primary = _ScriptedProvider(
        "openai:primary", [TransientError("down")])
    provider_a = _ScriptedProvider("openai:a", [TransientError("down")])
    provider_b = _ScriptedProvider("openai:b", [_ok_result("openai:b")])

    def _resolve(alias):
        return {"openai:primary": provider_primary,
                "openai:a": provider_a,
                "openai:b": provider_b}[alias]

    monkeypatch.setattr(f, "_resolve", _resolve)
    result = await f.call("openai:primary", [{"role": "user", "content": "x"}],
                           temperature=0, max_tokens=10,
                           purpose="execute", role="r")
    assert provider_b.calls == 1
    routed_events = list(lg.events("llm_routed"))
    # primary 和 a 先后失败，最后兜底到 b → 至少 2 次路由
    assert len(routed_events) >= 2


async def test_weighted_call_all_fail_raises(settings, tmp_path, monkeypatch):
    """全部别名都失败 → ProviderUnavailableError。"""
    from agentcrew.models.factory import ProviderUnavailableError
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = settings.model_copy(update={
        "model_failover": {"openai:primary": [
            {"alias": "openai:a", "weight": 1.0},
        ]}})
    f = ModelFactory(s, ledger=lg)
    for alias in ("openai:primary", "openai:a"):
        p = _ScriptedProvider(alias, [TransientError("down")])
        monkeypatch.setattr(f, "_resolve", lambda a, p=p: p)

    with pytest.raises(ProviderUnavailableError):
        await f.call("openai:primary", [{"role": "user", "content": "x"}],
                     temperature=0, max_tokens=10,
                     purpose="execute", role="r")


# ── 旧格式向后兼容 ──────────────────────────────────────────────────────


async def test_string_list_format_unchanged(settings, tmp_path):
    """纯字符串列表（旧格式）行为完全不变。"""
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    s = settings.model_copy(update={
        "model_failover": {"mock:primary": ["mock:backup"]}})
    f = ModelFactory(s, ledger=lg)
    # 确认不走向量路由路径
    assert f._build_weighted_pool("mock:primary") is None

    primary = f.get("mock:primary")

    async def _raise(*a, **kw):
        raise TransientError("503")

    primary.complete = _raise  # type: ignore[method-assign]
    backup = f.get("mock:backup")
    set_purpose(backup, "execute", role="r", task_id="T-1", attempt=1)
    await f.call("mock:primary", [{"role": "user", "content": "x"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    assert lg.count("llm_routed") == 0  # 旧格式不记 llm_routed
    assert lg.count("llm_failover") == 1


# ── config 加载 ─────────────────────────────────────────────────────────


def test_config_accepts_weighted_failover_format():
    """Settings 接受权重格式的 model_failover。"""
    s = Settings(
        model_failover={
            "openai:gpt-4o": [
                {"alias": "deepseek:v3", "weight": 0.6},
                {"alias": "anthropic:claude", "weight": 0.4},
            ],
        })
    entry = s.model_failover["openai:gpt-4o"][0]
    assert isinstance(entry, dict)
    assert entry["alias"] == "deepseek:v3"
    assert entry["weight"] == 0.6


def test_config_snapshot_includes_weighted_failover():
    """config_snapshot 正常序列化权重格式。"""
    s = Settings(
        model_failover={
            "openai:gpt-4o": [
                {"alias": "deepseek:v3", "weight": 0.6},
            ],
        })
    snapshot = s.config_snapshot()
    failover = snapshot["model_failover"]["openai:gpt-4o"]
    assert failover[0]["alias"] == "deepseek:v3"
    assert failover[0]["weight"] == 0.6
