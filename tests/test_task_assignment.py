"""R59 规划期灵活指派执行体。

覆盖：

- 数据层：``TaskDraft.agent`` / ``Task.agent`` 默认空、映射、进 JSON Schema；
  ``AgentProfile.description`` / ``capabilities`` 默认与往返；
- 校验层：``validate_plan`` 传入执行体池时拒绝未知指派、接受已知指派；
  未传池（``None``）跳过、空指派永远合法（向后兼容）；
- 派发层：``agent_for_task`` 优先级「运行时人工覆盖 > 规划指派 > 角色默认」；
  规划指派失效丢指派、记 ``task_agent_assignment_dropped`` 并按角色默认继续；
- 提示词层：执行体池非空时 PM 系统提示词出现「可用执行体」段，为空时逐字不变；
- 委派层：动态子任务 spec 的 ``agent`` 透传到 ``Task.agent``。
"""

from __future__ import annotations

import pytest

from agentcrew.agents.project_manager import (
    _agent_menu,
    _plan_system_prompt,
)
from agentcrew.agents.registry import AgentFactory
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.crew.scheduler import Scheduler
from agentcrew.gate.gatekeeper import Gatekeeper, NullPrompter
from agentcrew.gate.reviewer import Reviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.binding import AgentProfile, BoundAgent, Duty, RoleAgents
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.planner import (
    PlanOutput,
    PlanValidationError,
    plan_output_json_schema,
    to_tasks,
    validate_plan,
)
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard

PM_SPEC = next(r for r in BUILTIN_ROLES if r.key == "project_manager")


def _task(agent: str = "", task_id: str = "T-1") -> Task:
    return Task(
        id=task_id, title="实现后端", description="写 API", role="backend_dev",
        agent=agent, acceptance_criteria=["可运行"],
        deliverables=[ArtifactSpec(path="a.py", kind="python", description="d")])


def _draft(agent: str | None = None) -> dict:
    base = dict(
        id="T-1", title="t", description="d", role="backend_dev",
        acceptance_criteria=["可运行"],
        deliverables=[ArtifactSpec(path="a.py", kind="python", description="d")])
    if agent is not None:
        base["agent"] = agent
    return base


# ── 数据层 ──────────────────────────────────────────────────────────────────

def test_task_agent_defaults_empty():
    assert _task().agent == ""


def test_task_draft_agent_defaults_empty_and_maps():
    plan = PlanOutput.model_validate({"tasks": [_draft()]})
    assert plan.tasks[0].agent == ""
    assert to_tasks(plan)[0].agent == ""


def test_to_tasks_maps_explicit_agent():
    plan = PlanOutput.model_validate({"tasks": [_draft("claude-code")]})
    assert to_tasks(plan)[0].agent == "claude-code"


def test_plan_schema_includes_agent():
    schema = plan_output_json_schema()
    draft = schema["$defs"]["TaskDraft"]["properties"]
    assert "agent" in draft


def test_profile_description_capabilities_roundtrip():
    prof = AgentProfile(
        key="doubao-image", description="豆包图文生成",
        capabilities=["text", "image"], model="doubao:seedream")
    dumped = prof.model_dump(mode="json")
    assert dumped["description"] == "豆包图文生成"
    assert dumped["capabilities"] == ["text", "image"]
    again = AgentProfile.model_validate(dumped)
    assert again.capabilities == ["text", "image"]
    # 默认值向后兼容
    bare = AgentProfile(key="x")
    assert bare.description == "" and bare.capabilities == []


# ── 校验层 ──────────────────────────────────────────────────────────────────

def test_validate_plan_rejects_unknown_agent():
    profiles = {"a": AgentProfile(key="a")}
    with pytest.raises(PlanValidationError, match="未定义的执行体"):
        validate_plan([_task("ghost")], RoleCatalog(BUILTIN_ROLES), profiles=profiles)


def test_validate_plan_accepts_known_agent():
    profiles = {"a": AgentProfile(key="a")}
    validate_plan([_task("a")], RoleCatalog(BUILTIN_ROLES), profiles=profiles)


def test_validate_plan_skips_assignment_without_profiles():
    # 未提供池（G3 编辑器纯结构重检）：未知指派也不报错，行为与 R59 之前一致。
    validate_plan([_task("ghost")], RoleCatalog(BUILTIN_ROLES))


def test_validate_plan_empty_agent_always_ok():
    validate_plan([_task("")], RoleCatalog(BUILTIN_ROLES), profiles={})


def test_validate_plan_empty_pool_is_default_off():
    # 空池（用户没配执行体）= 本能力默认关闭：即便 PM 偶然吐了 agent 也不打回计划，
    # 派发层会记 task_agent_assignment_dropped 并回退角色默认（优雅降级）。
    validate_plan([_task("ghost")], RoleCatalog(BUILTIN_ROLES), profiles={})


# ── 派发层 ──────────────────────────────────────────────────────────────────

def _factory(tmp_path, ledger, *, profiles, binding):
    settings = Settings(default_model="mock:model", agent_profiles=profiles,
                        role_agents={"backend_dev": binding})
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=tmp_path)
    return AgentFactory(
        models, ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger),
        ledger, "目标", RoleCatalog(BUILTIN_ROLES), settings=settings, run_id="R")


def _single_binding(profiles):
    return RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)])


def test_planner_assignment_resolves_named_profile(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    profiles = {"a": AgentProfile(key="a"), "b": AgentProfile(key="b")}
    factory = _factory(tmp_path, ledger, profiles=profiles, binding=_single_binding(profiles))
    # 未指派 → 角色默认（单执行体路径 agent_key 为空）
    assert factory.agent_for_task("backend_dev", "T-1").agent_key == ""
    # 规划指派 b → 解析到 b
    assert factory.agent_for_task("backend_dev", "T-1", assigned="b").agent_key == "b"


def test_manual_override_beats_planner_assignment(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    profiles = {"a": AgentProfile(key="a"), "b": AgentProfile(key="b")}
    factory = _factory(tmp_path, ledger, profiles=profiles, binding=_single_binding(profiles))
    factory.set_task_override("backend_dev", "T-1", "a", reason="人工指定")
    assert factory.agent_for_task("backend_dev", "T-1", assigned="b").agent_key == "a"


def test_invalid_assignment_falls_back_and_logs(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    profiles = {"a": AgentProfile(key="a")}
    factory = _factory(tmp_path, ledger, profiles=profiles, binding=_single_binding(profiles))
    agent = factory.agent_for_task("backend_dev", "T-1", assigned="ghost")
    assert agent.agent_key == ""  # 回落角色默认
    events = list(ledger.events("task_agent_assignment_dropped"))
    assert len(events) == 1
    assert events[0].data["profile"] == "ghost"
    assert events[0].data["task_id"] == "T-1"


def test_assignment_only_affects_its_own_task(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    profiles = {"a": AgentProfile(key="a"), "b": AgentProfile(key="b")}
    factory = _factory(tmp_path, ledger, profiles=profiles, binding=_single_binding(profiles))
    assert factory.agent_for_task("backend_dev", "T-1", assigned="b").agent_key == "b"
    assert factory.agent_for_task("backend_dev", "T-2", assigned="").agent_key == ""


async def test_dispatch_trace_records_assigned_agent(tmp_path, monkeypatch):
    """R59：task_dispatched 留痕规划期指派的执行体（空指派记空串），供审计。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger)
    task = Task(id="T-1", title="t", description="d", role="backend_dev", agent="coder",
                acceptance_criteria=["可运行"],
                deliverables=[ArtifactSpec(path="a.py", kind="python", description="d")])
    board = TaskBoard([task])
    bus = ContextBus("目标", board, store)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=tmp_path)
    catalog = RoleCatalog(BUILTIN_ROLES)
    agents = AgentFactory(models, store, ledger, "目标", catalog)
    reviewer = Reviewer(models, catalog, store, ledger)
    gk = Gatekeeper(reviewer, store, board, gate="none", ledger=ledger, prompter=NullPrompter())
    seen: list[tuple[str, dict]] = []
    scheduler = Scheduler(board, agents, gk, bus, ledger=ledger,
                          on_event=lambda name, data: seen.append((name, data)))
    scheduler._spawn(task, discarded=0)
    scheduler._workers[task.id].cancel()  # 只验派发留痕，不真跑执行体
    dispatched = [data for name, data in seen if name == "task_dispatched"]
    assert dispatched and dispatched[0]["agent"] == "coder"
    assert dispatched[0]["task_id"] == "T-1"


# ── 提示词层 ────────────────────────────────────────────────────────────────

def test_prompt_unchanged_without_agent_menu():
    base = _plan_system_prompt(PM_SPEC, BUILTIN_ROLES)
    assert "可用执行体" not in base
    # 显式空菜单与缺省逐字一致
    assert _plan_system_prompt(PM_SPEC, BUILTIN_ROLES, "") == base


def test_prompt_includes_agent_menu_when_present():
    menu = _agent_menu({
        "claude-code": AgentProfile(
            key="claude-code", description="Claude Code 写代码",
            capabilities=["code"], model="cli:claude"),
        "doubao-image": AgentProfile(
            key="doubao-image", description="豆包图文", capabilities=["text", "image"]),
    })
    prompt = _plan_system_prompt(PM_SPEC, BUILTIN_ROLES, menu)
    assert "可用执行体" in prompt
    assert "claude-code" in prompt and "Claude Code 写代码" in prompt
    assert "code" in prompt and "text/image" in prompt
    assert "留空" in prompt  # 明确「不填 = 角色默认」


def test_pm_agent_menu_empty_without_profiles(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _factory(tmp_path, ledger, profiles={}, binding=RoleAgents())
    pm = factory.get("project_manager")
    assert pm._agents_menu() == ""


def test_pm_agent_menu_rendered_from_settings(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    profiles = {"codex": AgentProfile(
        key="codex", description="Codex 统筹", capabilities=["plan"])}
    factory = _factory(tmp_path, ledger, profiles=profiles, binding=RoleAgents())
    pm = factory.get("project_manager")
    menu = pm._agents_menu()
    assert "codex" in menu and "Codex 统筹" in menu and "plan" in menu


# ── 委派层：动态子任务透传 agent ────────────────────────────────────────────

@pytest.fixture
def sub_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts", run_id="R", ledger=ledger)
    task = Task(id="T-1", title="t", description="d", role="backend_dev",
                acceptance_criteria=["可运行"],
                deliverables=[ArtifactSpec(path="a.py", kind="python", description="d")])
    board = TaskBoard([task])
    bus = ContextBus("目标", board, store)
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=fx)
    catalog = RoleCatalog(BUILTIN_ROLES)
    agents = AgentFactory(models, store, ledger, "目标", catalog)
    reviewer = Reviewer(models, catalog, store, ledger)
    gk = Gatekeeper(reviewer, store, board, gate="none", ledger=ledger, prompter=NullPrompter())
    scheduler = Scheduler(board, agents, gk, bus, ledger=ledger)
    return dict(board=board, scheduler=scheduler)


def test_subtask_passes_agent_through(sub_env):
    scheduler = sub_env["scheduler"]
    child_id = scheduler._create_subtask({
        "title": "写工具", "role": "backend_dev", "agent": "claude-code",
        "deliverables": ["utils.py"], "parent_id": "T-1",
    })
    assert child_id is not None
    assert sub_env["board"].get(child_id).agent == "claude-code"


def test_subtask_without_agent_defaults_empty(sub_env):
    scheduler = sub_env["scheduler"]
    child_id = scheduler._create_subtask({
        "title": "写工具", "role": "backend_dev",
        "deliverables": ["utils.py"], "parent_id": "T-1",
    })
    assert sub_env["board"].get(child_id).agent == ""
