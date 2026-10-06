"""P2-6：计划质量评分（多维度）与 Crew 留痕。

评分是**观测**不是校验：``PlanValidationError`` 仍然是放行与否的唯一闸门，
本套测试钉死两件事 —— 四个维度在构造合理/劣质计划时的单调性，以及 Crew
在规划成功后落 ``plan_scored``、低分落 ``plan_low_quality``（只警告不阻断）。
"""

import json
from pathlib import Path

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.planner import (
    MIN_PLAN_SCORE,
    PlanScore,
    score_plan,
)
from agentcrew.task.task import Task


def _spec(path: str) -> ArtifactSpec:
    return ArtifactSpec(path=path, kind="python", description="d")


def _task(
    tid: str,
    *,
    description: str = "d",
    criteria: list[str] | None = None,
    deliverables: list[ArtifactSpec] | None = None,
    deps: list[str] | None = None,
) -> Task:
    return Task(
        id=tid,
        title=f"任务{tid}",
        description=description,
        role="backend_dev",
        acceptance_criteria=criteria or ["验收"],
        deliverables=deliverables or [],
        dependencies=deps or [],
    )


# ── 单元：四维评分 ─────────────────────────────────────────────────────────


def test_high_quality_plan_scores_above_0_7():
    long_desc = "实现待办的增删改查接口，并给出单元测试与错误处理" * 3
    tasks = [
        _task("T-1", description=long_desc, criteria=["接口返回 200", "覆盖异常分支"],
              deliverables=[_spec("backend/app/main.py"), _spec("backend/tests/test_api.py")],
              deps=[]),
        _task("T-2", description=long_desc, criteria=["页面渲染列表", "表单校验"],
              deliverables=[_spec("frontend/index.html")], deps=["T-1"]),
    ]
    score = score_plan(tasks, "实现 Todo 应用 增删改查 接口 前端页面")
    assert score.total > 0.7
    for dim in (score.coverage, score.specificity,
                score.dependency_quality, score.deliverable_clarity):
        assert 0.0 <= dim <= 1.0


def test_empty_descriptions_and_few_criteria_make_specificity_low():
    tasks = [
        _task("T-1", description="", criteria=["x"], deliverables=[_spec("a.py")]),
        _task("T-2", description="", criteria=["y"], deliverables=[_spec("b.py")],
              deps=["T-1"]),
    ]
    score = score_plan(tasks, "随便")
    # 平均描述长度 0、平均验收标准 1 条：specificity 远低于「具体」计划。
    assert score.specificity < 0.5


def test_cycle_makes_dependency_quality_zero():
    tasks = [
        _task("T-1", criteria=["x"], deliverables=[_spec("a.py")], deps=["T-2"]),
        _task("T-2", criteria=["y"], deliverables=[_spec("b.py")], deps=["T-1"]),
    ]
    score = score_plan(tasks, "随便")
    assert score.dependency_quality == 0.0
    assert score.details["cycles"]  # 环必须在 details 里留痕


def test_tasks_without_deliverables_make_clarity_low():
    tasks = [
        _task("T-1", description="x" * 200, criteria=["a", "b"]),
        _task("T-2", description="y" * 200, criteria=["c", "d"], deps=["T-1"]),
    ]
    score = score_plan(tasks, "随便")
    assert score.deliverable_clarity < 0.5


def test_score_plan_returns_valid_score_in_range():
    tasks = [_task("T-1")]
    score = score_plan(tasks, "做一个 Todo 应用")
    assert isinstance(score, PlanScore)
    assert 0.0 <= score.total <= 1.0
    for dim in (score.coverage, score.specificity,
                score.dependency_quality, score.deliverable_clarity):
        assert 0.0 <= dim <= 1.0
    assert "draft_task_count" in score.details


def test_goal_without_keywords_defaults_coverage_to_one():
    score = score_plan([_task("T-1")], "")
    assert score.coverage == 1.0


def test_min_plan_score_constant_is_half():
    assert MIN_PLAN_SCORE == 0.5


# ── Crew 留痕 ────────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    """与 tests/test_crew.py 同款的最小 mock 环境（复用其 plan/reviews 夹具）。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    import tests.test_crew as crew_tests

    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "plan_todo.json").write_text(json.dumps(crew_tests.PLAN), encoding="utf-8")
    (fx / "reviews.json").write_text(json.dumps(crew_tests.REVIEWS), encoding="utf-8")
    for tid, path, body in [
        ("T-001", "docs/PRD.md", crew_tests.PRD_BODY),
        ("T-002", "backend/app/main.py", crew_tests.MAIN_BODY),
        ("T-FINAL", "DELIVERY.md", crew_tests.DELIVERY_BODY),
    ]:
        d = fx / "responses" / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "attempt-1.md").write_text(
            f"```{path.split('.')[-1]} file={path}\n{body}```\n", encoding="utf-8")
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", fx)
    return {"settings": Settings.load(), "tmp_path": tmp_path}


def _ledger(settings, run_id) -> Ledger:
    return Ledger(
        settings.output_dir / run_id / "ledger.jsonl",
        run_id=run_id,
        secrets=settings.secret_values(),
    )


async def test_crew_emits_plan_scored_event(env):
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    events = list(_ledger(env["settings"], rec.run_id).events("plan_scored"))
    assert events, "规划成功后必须记 plan_scored"
    data = events[-1].data
    for field in ("total", "coverage", "specificity",
                  "dependency_quality", "deliverable_clarity"):
        assert field in data
        assert 0.0 <= data[field] <= 1.0


async def test_low_score_plan_emits_plan_low_quality_warning(env, monkeypatch):
    """总分低于 MIN_PLAN_SCORE → 记 plan_low_quality，但 run 仍正常完成（不阻断）。"""
    low = PlanScore(
        total=0.1,
        coverage=0.0,
        specificity=0.1,
        dependency_quality=0.0,
        deliverable_clarity=0.0,
        details={"reason": "stub"},
    )
    monkeypatch.setattr("agentcrew.crew.crew.score_plan", lambda tasks, goal: low)
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    assert rec.status.value == "completed"  # 警告不阻断执行
    ledger = _ledger(env["settings"], rec.run_id)
    warns = list(ledger.events("plan_low_quality"))
    assert warns, "低分计划必须记 plan_low_quality"
    assert warns[-1].data["total"] == 0.1
    assert warns[-1].data["min_score"] == MIN_PLAN_SCORE
