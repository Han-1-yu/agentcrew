"""R6：动态委派治理（四道闸）。

覆盖：角色必须真实存在、``manager_only`` 下只有经理能派、层数上限、总数配额、
``delegated_by`` 留痕，以及「未注入 settings 时全部跳过」的向后兼容。

背景：R5 收尾时把「经理 Agent 动态委派 / 层级流程」列为遗留项。动手前先核实了
``Scheduler._create_subtask`` 的现状——它直接 ``role=str(spec.get("role") or
"generic")``，模型写什么就是什么：拼错角色名会静默落到通用兜底（任务跑完、产物
看着也在，质量却塌一档），委派链也没有任何深度或数量上限。这两条是 bug 级的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.agents.registry import AgentFactory
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.crew.scheduler import Scheduler
from agentcrew.gate.gatekeeper import Gatekeeper, NullPrompter
from agentcrew.gate.reviewer import Reviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts",
                          run_id="R", ledger=ledger)
    root = Task(
        id="T-1", title="根任务", description="写 a.py", role="backend_dev",
        acceptance_criteria=["可运行"],
        deliverables=[ArtifactSpec(path="a.py", kind="python", description="d",
                                   must_contain=["def f"])],
    )
    board = TaskBoard([root])
    bus = ContextBus("目标", board, store)
    return dict(settings=settings, ledger=ledger, store=store, board=board,
                bus=bus, tmp_path=tmp_path)


def _scheduler(env, *, settings=None, catalog=None) -> Scheduler:
    """构造带治理参数的调度器；``settings=None`` 复现 R6 之前的库层用法。"""
    fixtures = env["tmp_path"] / "fx"
    fixtures.mkdir(exist_ok=True)
    models = ModelFactory(env["settings"], ledger=env["ledger"],
                          fixtures_dir=fixtures)
    catalog = catalog if catalog is not None else RoleCatalog(BUILTIN_ROLES)
    agents = AgentFactory(models, env["store"], env["ledger"], "目标", catalog)
    reviewer = Reviewer(models, catalog, env["store"], env["ledger"])
    gatekeeper = Gatekeeper(reviewer, env["store"], env["board"], gate="none",
                            ledger=env["ledger"], prompter=NullPrompter())
    return Scheduler(env["board"], agents, gatekeeper, env["bus"],
                     ledger=env["ledger"], catalog=catalog, settings=settings)


def _spec(**overrides):
    base = {
        "title": "子任务", "description": "d", "role": "backend_dev",
        "deliverables": ["utils.py"], "parent_id": "T-1",
    }
    base.update(overrides)
    return base


# ── 闸 ①：角色必须真实存在 ────────────────────────────────────────────────


def test_unknown_role_is_rejected(env):
    """拼错的角色名不能静默落到通用兜底——那会让产物质量无声塌一档。"""
    scheduler = _scheduler(env, settings=env["settings"])

    child = scheduler._create_subtask(_spec(role="senior_backend"))

    assert child is None
    events = list(env["ledger"].events("subtask_role_unknown"))
    assert len(events) == 1
    assert events[0].data["role"] == "senior_backend"
    assert "architect" in events[0].data["available"]
    # 拒绝原因要能被人读懂，且明确指出可用角色
    rejected = list(env["ledger"].events("subtask_rejected_by_policy"))
    assert "没有 'senior_backend'" in rejected[0].data["reason"]


def test_known_role_is_accepted(env):
    scheduler = _scheduler(env, settings=env["settings"])

    child = scheduler._create_subtask(_spec(role="qa_engineer"))

    assert child is not None
    assert env["board"].get(child).role == "qa_engineer"
    assert env["ledger"].count("subtask_role_unknown") == 0


def test_generic_role_still_allowed(env):
    """``generic`` 是显式的通用执行者占位，不该被当成拼错的名字。"""
    scheduler = _scheduler(env, settings=env["settings"])

    child = scheduler._create_subtask(_spec(role="generic"))

    assert child is not None
    assert env["board"].get(child).role == "generic"


# ── 闸 ②：manager_only ───────────────────────────────────────────────────


def test_manager_only_blocks_worker_delegation(env):
    """执行角色擅自派活要被拒：它派出的子任务父任务是 backend_dev。"""
    settings = env["settings"].model_copy(update={"delegation_mode": "manager_only"})
    scheduler = _scheduler(env, settings=settings)

    child = scheduler._create_subtask(_spec())

    assert child is None
    events = list(env["ledger"].events("subtask_unauthorized"))
    assert events[0].data["role"] == "backend_dev"
    assert "manager_only" in events[0].data["reason"] if "reason" in events[0].data else True


def test_manager_only_allows_manager_delegation(env):
    """经理派活要放行：父任务角色是 project_manager。"""
    env["board"].add_task(Task(
        id="T-PM", title="经理任务", description="d", role="project_manager",
        acceptance_criteria=[], deliverables=[],
    ))
    settings = env["settings"].model_copy(update={"delegation_mode": "manager_only"})
    scheduler = _scheduler(env, settings=settings)

    child = scheduler._create_subtask(_spec(parent_id="T-PM"))

    assert child is not None
    assert env["ledger"].count("subtask_unauthorized") == 0


def test_default_mode_allows_anyone(env):
    """默认 ``any`` = P2-1 以来的现状，逐字不变。"""
    scheduler = _scheduler(env, settings=env["settings"])

    assert scheduler._create_subtask(_spec()) is not None


# ── 闸 ③：层数 ───────────────────────────────────────────────────────────


def test_delegation_depth_limit(env):
    """根(0) → 子(1) → 孙(2) 放行，曾孙(3) 被拒（默认上限 2）。"""
    scheduler = _scheduler(env, settings=env["settings"])

    child = scheduler._create_subtask(_spec())
    grandchild = scheduler._create_subtask(_spec(
        parent_id=child, deliverables=["b.py"]))
    great = scheduler._create_subtask(_spec(
        parent_id=grandchild, deliverables=["c.py"]))

    assert child is not None and grandchild is not None
    assert great is None
    events = list(env["ledger"].events("subtask_depth_exceeded"))
    assert events[0].data["depth"] == 3
    assert events[0].data["max_depth"] == 2


def test_depth_limit_configurable(env):
    settings = env["settings"].model_copy(update={"max_delegation_depth": 1})
    scheduler = _scheduler(env, settings=settings)

    child = scheduler._create_subtask(_spec())
    grandchild = scheduler._create_subtask(_spec(
        parent_id=child, deliverables=["b.py"]))

    assert child is not None
    assert grandchild is None


# ── 闸 ④：总数配额 ───────────────────────────────────────────────────────


def test_dynamic_task_quota(env):
    settings = env["settings"].model_copy(update={"max_dynamic_tasks": 2})
    scheduler = _scheduler(env, settings=settings)

    first = scheduler._create_subtask(_spec(deliverables=["u1.py"]))
    second = scheduler._create_subtask(_spec(deliverables=["u2.py"]))
    third = scheduler._create_subtask(_spec(deliverables=["u3.py"]))

    assert first is not None and second is not None
    assert third is None
    events = list(env["ledger"].events("subtask_quota_exceeded"))
    assert events[0].data["used"] == 2
    assert events[0].data["max_tasks"] == 2


# ── 留痕与兼容 ───────────────────────────────────────────────────────────


def test_delegated_by_records_parent_role(env):
    scheduler = _scheduler(env, settings=env["settings"])

    child = scheduler._create_subtask(_spec())

    assert env["board"].get(child).delegated_by == "backend_dev"


def test_no_settings_skips_all_gates(env):
    """库层直接构造（未注入 settings）：四道闸全跳过，行为与 R6 之前一致。"""
    scheduler = _scheduler(env, settings=None)

    # 拼错的角色、超深的链，这里都该放行
    child = scheduler._create_subtask(_spec(role="senior_backend"))

    assert child is not None
    assert env["ledger"].count("subtask_role_unknown") == 0


def test_rejection_does_not_break_board(env):
    """被拒的子任务不能留在板上，也不能影响已有任务。"""
    scheduler = _scheduler(env, settings=env["settings"])
    before = sorted(env["board"].ids())

    scheduler._create_subtask(_spec(role="ghost"))

    assert sorted(env["board"].ids()) == before


def test_depth_cycle_does_not_hang(env):
    """坏数据造出 parent 环时，深度计算必须收敛（不能把调度器转死）。"""
    env["board"].add_task(Task(
        id="T-A", title="a", description="d", role="backend_dev",
        acceptance_criteria=[], deliverables=[], parent_id="T-B",
    ))
    env["board"].add_task(Task(
        id="T-B", title="b", description="d", role="backend_dev",
        acceptance_criteria=[], deliverables=[], parent_id="T-A",
    ))
    scheduler = _scheduler(env, settings=env["settings"])

    assert scheduler._delegation_depth("T-A") == 2
