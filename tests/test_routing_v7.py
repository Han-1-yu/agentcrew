"""V7-P1 多模式路由策略测试。

覆盖：
- routing_strategy 配置的四种模式（weighted/rotate/usage/order）
- rotate 策略的轮询行为
- usage 策略的最少使用优先
- order 策略的顺序选择
- 健康状态对各策略的影响
- 与 ModelFactory 的集成（mock provider + 权重池配置）
"""

from __future__ import annotations

import pytest

from agentcrew.config import Settings
from agentcrew.models.factory import ModelFactory, WeightedAlias


def _make_factory(strategy: str, **kwargs) -> ModelFactory:
    settings = Settings(
        mock=True,
        routing_strategy=strategy,
        model_failover={
            "mock:primary": [
                {"alias": "mock:primary", "weight": 1.0},
                {"alias": "mock:backup1", "weight": 1.0},
                {"alias": "mock:backup2", "weight": 1.0},
            ],
        },
        **kwargs,
    )
    return ModelFactory(settings)


def _pool() -> list[WeightedAlias]:
    return [
        WeightedAlias(alias="mock:a", weight=1.0),
        WeightedAlias(alias="mock:b", weight=1.0),
        WeightedAlias(alias="mock:c", weight=2.0),
    ]


class TestRoutingStrategyConfig:
    def test_default_strategy_is_weighted(self):
        s = Settings(mock=True)
        assert s.routing_strategy == "weighted"

    def test_custom_strategy(self):
        s = Settings(mock=True, routing_strategy="rotate")
        assert s.routing_strategy == "rotate"


class TestRotateStrategy:
    def test_rotate_cycles_through_pool(self):
        factory = _make_factory("rotate")
        pool = _pool()
        chosen = [factory._select_weighted(pool)[0] for _ in range(6)]
        # 轮询应按顺序循环 a, b, c, a, b, c
        assert chosen == ["mock:a", "mock:b", "mock:c", "mock:a", "mock:b", "mock:c"]

    def test_rotate_independent_per_pool(self):
        factory = _make_factory("rotate")
        pool1 = [WeightedAlias(alias="mock:x", weight=1.0)]
        pool2 = [WeightedAlias(alias="mock:y", weight=1.0)]
        # 不同 pool 的计数器独立
        assert factory._select_weighted(pool1)[0] == "mock:x"
        assert factory._select_weighted(pool2)[0] == "mock:y"
        assert factory._select_weighted(pool1)[0] == "mock:x"  # pool1 继续轮询（只有一个元素）

    def test_rotate_reason_includes_strategy(self):
        factory = _make_factory("rotate")
        _, reason = factory._select_weighted(_pool())
        assert reason.startswith("rotate")


class TestUsageStrategy:
    def test_usage_prefers_unused_alias(self):
        factory = _make_factory("usage")
        pool = _pool()
        # 第一次：所有别名未使用，选第一个
        first, _ = factory._select_weighted(pool)
        assert first == "mock:a"
        # 记录 a 使用了 100 tokens
        factory._note_usage("mock:a", 80, 20)
        # 第二次：a 已使用，应选 b（未使用）
        second, _ = factory._select_weighted(pool)
        assert second == "mock:b"

    def test_usage_prefers_less_used(self):
        factory = _make_factory("usage")
        pool = _pool()
        factory._note_usage("mock:a", 100, 0)
        factory._note_usage("mock:b", 50, 0)
        # c 未使用（0），应优先选 c
        chosen, _ = factory._select_weighted(pool)
        assert chosen == "mock:c"

    def test_usage_decay_over_time(self):
        factory = _make_factory("usage")
        import time
        # 模拟 a 在很久以前使用了大量 token
        factory._usage_scores["mock:a"] = (1000.0, time.time() - 7200)  # 2小时前
        factory._usage_scores["mock:b"] = (100.0, time.time())  # 刚使用
        pool = [
            WeightedAlias(alias="mock:a", weight=1.0),
            WeightedAlias(alias="mock:b", weight=1.0),
        ]
        # a 的使用量经 2 小时衰减（半衰期1小时，衰减4倍→250），仍大于 b 的 100
        # 所以应选 b
        chosen, _ = factory._select_weighted(pool)
        assert chosen == "mock:b"

    def test_note_usage_accumulates(self):
        factory = _make_factory("usage")
        factory._note_usage("mock:a", 10, 5)
        score = factory._current_usage_score("mock:a", __import__("time").time(), 3600.0)
        assert abs(score - 15.0) < 0.01


class TestOrderStrategy:
    def test_order_picks_first(self):
        factory = _make_factory("order")
        pool = _pool()
        chosen, reason = factory._select_weighted(pool)
        assert chosen == "mock:a"
        assert reason.startswith("order")

    def test_order_always_first_regardless_of_calls(self):
        factory = _make_factory("order")
        pool = _pool()
        for _ in range(5):
            assert factory._select_weighted(pool)[0] == "mock:a"


class TestWeightedStrategyBackwardCompat:
    def test_weighted_default_behavior(self):
        factory = _make_factory("weighted")
        pool = _pool()
        # 多次调用，c 权重 2.0 应被选中更多次
        counts = {"mock:a": 0, "mock:b": 0, "mock:c": 0}
        for _ in range(1000):
            chosen, _ = factory._select_weighted(pool)
            counts[chosen] += 1
        # c 权重是 a/b 的两倍，选中次数应明显更多
        assert counts["mock:c"] > counts["mock:a"]
        assert counts["mock:c"] > counts["mock:b"]


class TestHealthInteraction:
    def test_unavailable_skipped_in_rotate(self):
        from datetime import datetime
        factory = _make_factory("rotate")
        from agentcrew.models.factory import HealthRecord
        factory._health["mock:a"] = HealthRecord(
            alias="mock:a", status="unavailable", last_check=datetime.now())
        pool = _pool()
        # a 不可用，轮询应从 b 开始
        chosen, _ = factory._select_weighted(pool)
        assert chosen in ("mock:b", "mock:c")

    def test_all_unavailable_falls_back(self):
        from datetime import datetime
        factory = _make_factory("rotate")
        from agentcrew.models.factory import HealthRecord
        for alias in ("mock:a", "mock:b", "mock:c"):
            factory._health[alias] = HealthRecord(
                alias=alias, status="unavailable", last_check=datetime.now())
        pool = _pool()
        # 全部不可用时回退到第一个
        chosen, _ = factory._select_weighted(pool)
        assert chosen == "mock:a"


class TestFactoryIntegration:
    @pytest.mark.asyncio
    async def test_rotate_strategy_with_real_call(self):
        factory = _make_factory("rotate")
        messages = [{"role": "user", "content": "hello"}]
        # 连续调用 3 次，应轮询到不同别名
        results = []
        for _ in range(3):
            result = await factory.call(
                "mock:primary",
                messages,  # type: ignore[arg-type]
                temperature=0.0,
                max_tokens=50,
                purpose="test",
                role="test",
            )
            results.append(result)
        assert all(r.text is not None for r in results)

    @pytest.mark.asyncio
    async def test_usage_strategy_tracks_after_call(self):
        factory = _make_factory("usage")
        messages = [{"role": "user", "content": "hello"}]
        await factory.call(
            "mock:primary",
            messages,  # type: ignore[arg-type]
            temperature=0.0,
            max_tokens=50,
            purpose="test",
            role="test",
        )
        # 调用后 usage_scores 应有记录
        assert len(factory._usage_scores) > 0
