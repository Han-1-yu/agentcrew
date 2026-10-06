"""P3-1：复杂度动态校准的测试。

覆盖 :class:`ComplexityCalibrator` 的调级规则、``record`` 统计、工厂
``alias_for_task`` 对接、``complexity_calibrated`` 留痕，以及「校准器未注入
时行为不变」的向后兼容。
"""

from __future__ import annotations

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.config import Settings
from agentcrew.models.complexity import (
    ComplexityCalibrator,
    ComplexityLevel,
    evaluate,
    get_calibrator,
    set_calibrator,
)
from agentcrew.models.factory import ModelFactory
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task


def _task(**kwargs) -> Task:
    """一个静态评估为 MEDIUM 的任务（backend_dev 基础分 2 + 3 条验收标准 = 5）。"""
    defaults = dict(
        id="T-1", title="t", description="写代码", role="backend_dev",
        acceptance_criteria=["一", "二", "三"],
        deliverables=[ArtifactSpec(path="a.py", kind="python", description="d")],
    )
    defaults.update(kwargs)
    return Task.model_validate(defaults)


@pytest.fixture(autouse=True)
def _reset_global_calibrator():
    """每个测试前后清空全局校准器，避免模块间污染。"""
    set_calibrator(None)
    yield
    set_calibrator(None)


class TestCalibratorRules:
    def test_no_history_returns_static_level(self):
        cal = ComplexityCalibrator()
        task = _task()
        assert cal.calibrate(task) == evaluate(task) == ComplexityLevel.MEDIUM

    def test_repeated_failures_upgrade_level(self):
        cal = ComplexityCalibrator()
        task = _task(role="backend_dev")
        for _ in range(3):
            cal.record(task, success=False, attempts=3)
        # 成功率 0% < 50% 且平均 attempts=3 > 1.5 → 上调 MEDIUM → HIGH
        assert evaluate(task) == ComplexityLevel.MEDIUM
        assert cal.calibrate(task) == ComplexityLevel.HIGH

    def test_high_attempts_upgrade_even_with_partial_success(self):
        cal = ComplexityCalibrator()
        task = _task(role="backend_dev")
        # 2 成功 1 失败（成功率 67%，不触发 <50%），但平均 attempts=2 > 1.5
        cal.record(task, success=True, attempts=2)
        cal.record(task, success=True, attempts=2)
        cal.record(task, success=False, attempts=2)
        assert cal.calibrate(task) == ComplexityLevel.HIGH

    def test_repeated_success_downgrade_level(self):
        cal = ComplexityCalibrator()
        # 用不同角色隔离特征桶
        task = _task(role="technical_writer")
        for _ in range(3):
            cal.record(task, success=True, attempts=1)
        # 成功率 100% > 90% 且平均 attempts=1 → 下调一级（MEDIUM → LOW）。
        assert evaluate(task) == ComplexityLevel.MEDIUM
        assert cal.calibrate(task) == ComplexityLevel.LOW

    def test_downgrade_moves_medium_to_low(self):
        cal = ComplexityCalibrator()
        task = _task(role="backend_dev")
        for _ in range(3):
            cal.record(task, success=True, attempts=1)
        assert evaluate(task) == ComplexityLevel.MEDIUM
        assert cal.calibrate(task) == ComplexityLevel.LOW

    def test_mixed_outcome_keeps_static_level(self):
        cal = ComplexityCalibrator()
        task = _task(role="backend_dev")
        # 1 成功(attempts=1) + 1 失败(attempts=1)：成功率 50%（不 <50%）、
        # 平均 attempts=1（不 >1.5、也不满足「全部 attempts==1 且 >90%」）。
        cal.record(task, success=True, attempts=1)
        cal.record(task, success=False, attempts=1)
        assert cal.calibrate(task) == ComplexityLevel.MEDIUM

    def test_record_tracks_stats(self):
        cal = ComplexityCalibrator()
        task = _task(role="backend_dev")
        cal.record(task, success=True, attempts=1)
        cal.record(task, success=False, attempts=2)
        stats = cal.stats()
        key = next(iter(stats))
        assert stats[key]["count"] == 2
        assert stats[key]["successes"] == 1
        assert stats[key]["success_rate"] == 0.5
        assert stats[key]["avg_attempts"] == 1.5

    def test_different_roles_isolated(self):
        cal = ComplexityCalibrator()
        cheap = _task(role="technical_writer")
        strong = _task(role="architect")
        # 只给 strong 喂失败
        cal.record(strong, success=False, attempts=3)
        assert cal.calibrate(cheap) == evaluate(cheap)  # cheap 无历史
        assert cal.calibrate(strong) != evaluate(strong)


class TestFactoryIntegration:
    def _factory(self, tmp_path, **settings_kwargs) -> ModelFactory:
        s = Settings.load().model_copy(update=settings_kwargs)
        ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
        return ModelFactory(s, ledger=ledger)

    def test_alias_uses_calibrated_level(self, tmp_path):
        factory = self._factory(
            tmp_path,
            default_model="openai:default",
            model_tiers={
                "low": "openai:cheap",
                "medium": "openai:mid",
                "high": "openai:powerful",
            },
        )
        task = _task(role="backend_dev")
        assert evaluate(task) == ComplexityLevel.MEDIUM

        # 无校准器：走静态 medium
        set_calibrator(None)
        assert factory.alias_for_task(task) == "openai:mid"

        # 注入校准器并喂失败：上调到 high
        cal = ComplexityCalibrator()
        set_calibrator(cal)
        cal.record(task, success=False, attempts=3)
        assert factory.alias_for_task(task) == "openai:powerful"

    def test_alias_downgraded_uses_cheap_tier(self, tmp_path):
        factory = self._factory(
            tmp_path,
            default_model="openai:default",
            model_tiers={
                "low": "openai:cheap",
                "medium": "openai:mid",
            },
        )
        task = _task(role="backend_dev")
        cal = ComplexityCalibrator()
        set_calibrator(cal)
        cal.record(task, success=True, attempts=1)
        cal.record(task, success=True, attempts=1)
        assert evaluate(task) == ComplexityLevel.MEDIUM
        assert cal.calibrate(task) == ComplexityLevel.LOW
        assert factory.alias_for_task(task) == "openai:cheap"

    def test_ledger_records_calibrated_event(self, tmp_path):
        factory = self._factory(
            tmp_path,
            default_model="openai:default",
            model_tiers={"high": "openai:powerful", "medium": "openai:mid"},
        )
        task = _task(role="backend_dev", id="T-CAL")
        cal = ComplexityCalibrator()
        set_calibrator(cal)
        cal.record(task, success=False, attempts=3)
        factory.alias_for_task(task)

        events = list(factory._ledger.events("complexity_calibrated"))
        assert len(events) == 1
        data = events[0].data
        assert data["task_id"] == "T-CAL"
        assert data["original_level"] == "medium"
        assert data["calibrated_level"] == "high"
        assert data["reason"] == "upgraded"

    def test_no_event_when_level_unchanged(self, tmp_path):
        factory = self._factory(
            tmp_path,
            default_model="openai:default",
            model_tiers={"medium": "openai:mid"},
        )
        task = _task(role="backend_dev")
        cal = ComplexityCalibrator()
        set_calibrator(cal)
        # 混合结局 → 级别不变，不发事件
        cal.record(task, success=True, attempts=1)
        cal.record(task, success=False, attempts=1)
        factory.alias_for_task(task)
        assert factory._ledger.count("complexity_calibrated") == 0

    def test_backward_compat_without_calibrator(self, tmp_path):
        """校准器未设置时 alias_for_task 行为与改动前完全一致。"""
        factory = self._factory(
            tmp_path,
            default_model="openai:default",
            model_tiers={"medium": "openai:mid"},
        )
        set_calibrator(None)
        task = _task(role="backend_dev")
        assert get_calibrator() is None
        assert factory.alias_for_task(task) == "openai:mid"
        assert factory._ledger.count("complexity_calibrated") == 0
