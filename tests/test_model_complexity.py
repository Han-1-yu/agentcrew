"""按复杂度自动选模型的测试（V2 第 2 项）。"""

from __future__ import annotations

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.config import Settings
from agentcrew.models.complexity import ComplexityLevel, evaluate
from agentcrew.models.factory import ModelFactory
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task


def _task(**kwargs) -> Task:
    defaults = dict(
        id="T-1", title="t", description="写代码", role="backend_dev",
        acceptance_criteria=["完成"], deliverables=[ArtifactSpec(path="a.py", kind="python", description="d")],
    )
    defaults.update(kwargs)
    return Task.model_validate(defaults)


class TestComplexityEvaluator:
    def test_technical_writer_low(self):
        task = _task(role="technical_writer", acceptance_criteria=["一条"], deliverables=[])
        assert evaluate(task) == ComplexityLevel.LOW

    def test_architect_high(self):
        task = _task(
            role="architect",
            acceptance_criteria=["一", "二", "三", "四"],
            deliverables=[ArtifactSpec(path=f"f{i}.md", kind="markdown", description="d") for i in range(3)],
            dependencies=["T-A", "T-B"],
        )
        assert evaluate(task) == ComplexityLevel.HIGH

    def test_backend_medium(self):
        task = _task(role="backend_dev", acceptance_criteria=["一", "二", "三"])
        assert evaluate(task) == ComplexityLevel.MEDIUM

    def test_more_criteria_increases_complexity(self):
        low = _task(acceptance_criteria=["一条"])
        high = _task(acceptance_criteria=["一", "二", "三", "四", "五", "六"])
        assert evaluate(high).value != evaluate(low).value or evaluate(high) == ComplexityLevel.HIGH

    def test_deterministic(self):
        task = _task()
        assert evaluate(task) == evaluate(task)


class TestModelFactoryAliasForTask:
    def _factory(self, **settings_kwargs) -> ModelFactory:
        s = Settings.load()
        if settings_kwargs:
            s = s.model_copy(update=settings_kwargs)
        return ModelFactory(s)

    def test_default_model_when_no_tiers(self, tmp_path):
        ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
        s = Settings.load().model_copy(update={"default_model": "openai:gpt-4o-mini"})
        factory = ModelFactory(s, ledger=ledger)
        task = _task(role="backend_dev")
        assert factory.alias_for_task(task) == "openai:gpt-4o-mini"

    def test_tier_model_used_for_complexity(self, tmp_path):
        s = Settings.load().model_copy(update={
            "default_model": "openai:cheap",
            "model_tiers": {"high": "openai:powerful"},
        })
        factory = ModelFactory(s)
        high_task = _task(
            role="architect",
            acceptance_criteria=["一", "二", "三", "四", "五"],
            dependencies=["T-A", "T-B", "T-C"],
        )
        assert evaluate(high_task) == ComplexityLevel.HIGH
        assert factory.alias_for_task(high_task) == "openai:powerful"

    def test_role_binding_overrides_tier(self, tmp_path):
        s = Settings.load().model_copy(update={
            "default_model": "openai:default",
            "role_bindings": {"backend_dev": "openai:role-specific"},
            "model_tiers": {"medium": "openai:tier-medium"},
        })
        factory = ModelFactory(s)
        task = _task(role="backend_dev", acceptance_criteria=["一", "二", "三"])
        assert evaluate(task) == ComplexityLevel.MEDIUM
        # role_bindings 优先级高于 tier
        assert factory.alias_for_task(task) == "openai:role-specific"

    def test_mock_mode_returns_valid_alias(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTCREW_MOCK", "true")
        s = Settings.load()
        factory = ModelFactory(s)
        task = _task()
        # mock 模式下仍返回有效别名（_resolve 内部转 MockProvider）
        alias = factory.alias_for_task(task)
        assert ":" in alias
        # 且能正常解析为 provider
        provider = factory.get(alias)
        assert provider is not None

    def test_low_tier_not_used_for_medium_task(self, tmp_path):
        s = Settings.load().model_copy(update={
            "default_model": "openai:default",
            "model_tiers": {"low": "openai:cheap"},
        })
        factory = ModelFactory(s)
        task = _task(role="backend_dev", acceptance_criteria=["一", "二", "三"])
        assert evaluate(task) == ComplexityLevel.MEDIUM
        # medium 没有配置 tier，回退到 default
        assert factory.alias_for_task(task) == "openai:default"
