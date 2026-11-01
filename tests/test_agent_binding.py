"""角色绑定多 Agent：配置校验、计划解析、路径域与团队执行。

覆盖四层：
1. 配置层：profile/绑定校验（未知 ref、扩权、缺 aggregation、judge 参赛）；
2. 计划层：无绑定 → 默认单执行体；有绑定 → 展开成 ResolvedAgent；
3. 权限层：path_scope 越界在 store 落盘前就被拒；
4. 执行层：split 分工合并、review 意见进 notes、best_of 中标草稿转正。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from agentcrew.agents.plan import alias_for, pick_primary, resolve_plan
from agentcrew.agents.team import TeamAgent
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactScopeError, ArtifactStore
from agentcrew.config import Settings
from agentcrew.roles.binding import (
    AgentProfile,
    Aggregation,
    BindingError,
    BoundAgent,
    Duty,
    RoleAgents,
    validate_bindings,
)
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.task.task import Task, TaskResult

BACKEND = next(r for r in BUILTIN_ROLES if r.key == "backend_dev")


def _task(*paths: str, task_id: str = "T-1") -> Task:
    return Task(
        id=task_id, title="实现后端", description="写 API", role="backend_dev",
        acceptance_criteria=["可运行"],
        deliverables=[ArtifactSpec(path=p, kind="python", description=p) for p in paths])


class StubAgent:
    """只做一件事：把预设内容按契约落盘，用来观察分流与汇总。"""

    def __init__(self, store, files, *, scope=None, key=""):
        self.store, self.files, self.scope, self.key = store, files, scope, key

    async def execute(self, task, pack, *, fix_hints=()):
        now = datetime.now()
        records = []
        for spec in task.deliverables:
            if spec.path in self.files:
                records.append(self.store.commit(
                    spec.path, self.files[spec.path], spec=spec, task=task,
                    path_scope=self.scope, agent=self.key))
        return TaskResult(
            task_id=task.id, role=task.role, summary=f"{self.key} 完成",
            artifacts=records, started_at=now, finished_at=now, duration_ms=1)


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "art", run_id="R")


# ── 1. 配置校验 ────────────────────────────────────────────────────────────

def test_unknown_profile_is_rejected():
    binding = RoleAgents(agents=[BoundAgent(ref="ghost")])
    with pytest.raises(BindingError, match="未定义的 agent"):
        validate_bindings("backend_dev", binding, {}, ["*"])


def test_agent_cannot_widen_tool_permissions():
    profiles = {"wide": AgentProfile(key="wide", tools=["run_python"])}
    binding = RoleAgents(agents=[BoundAgent(ref="wide")])
    with pytest.raises(BindingError, match="只能收窄"):
        validate_bindings("backend_dev", binding, profiles, ["read_file"])


def test_multi_agent_requires_aggregation():
    profiles = {
        "a": AgentProfile(key="a"),
        "b": AgentProfile(key="b"),
    }
    binding = RoleAgents(agents=[
        BoundAgent(ref="a", duty=Duty.PRIMARY),
        BoundAgent(ref="b", duty=Duty.REVIEW),
    ])
    with pytest.raises(BindingError, match="必须声明 aggregation"):
        validate_bindings("backend_dev", binding, profiles, ["*"])


def test_judge_cannot_be_a_contestant():
    profiles = {k: AgentProfile(key=k) for k in "abc"}
    binding = RoleAgents(
        agents=[BoundAgent(ref="a", duty=Duty.PRIMARY),
                BoundAgent(ref="b", duty=Duty.PRIMARY),
                BoundAgent(ref="c", duty=Duty.JUDGE)],
        aggregation=Aggregation(mode="best_of", judge="a"))
    with pytest.raises(BindingError, match="judge 不能同时参赛"):
        validate_bindings("backend_dev", binding, profiles, ["*"])


def test_split_requires_exclusive_path_scopes():
    profiles = {
        "a": AgentProfile(key="a", path_scope=["backend/"]),
        "b": AgentProfile(key="b", path_scope=["backend/"]),
    }
    binding = RoleAgents(
        agents=[BoundAgent(ref="a", duty=Duty.SECTION),
                BoundAgent(ref="b", duty=Duty.SECTION)],
        aggregation=Aggregation(mode="split_by_path"))
    with pytest.raises(BindingError, match="路径域重叠"):
        validate_bindings("backend_dev", binding, profiles, ["*"])


# ── 2. 计划解析 ────────────────────────────────────────────────────────────

def test_plan_defaults_to_single_agent_when_unbound():
    settings = Settings(default_model="mock:model")
    plan = resolve_plan(settings, RoleCatalog(BUILTIN_ROLES), "R1")
    backend = plan.for_role("backend_dev")
    assert len(backend) == 1
    assert backend[0].profile == "default"
    assert backend[0].duty is Duty.PRIMARY


def test_plan_expands_bindings_and_narrows_tools():
    settings = Settings(
        default_model="mock:model",
        agent_profiles={
            "fast": AgentProfile(key="fast", model="deepseek:chat", tools=["read_file"]),
            "reviewer": AgentProfile(key="reviewer", model="openai:mini"),
        },
        role_agents={"backend_dev": RoleAgents(
            agents=[BoundAgent(ref="fast", duty=Duty.PRIMARY),
                    BoundAgent(ref="reviewer", duty=Duty.REVIEW)],
            aggregation=Aggregation(mode="primary_with_review"))},
    )
    plan = resolve_plan(settings, RoleCatalog(BUILTIN_ROLES), "R1")
    backend = {a.profile: a for a in plan.for_role("backend_dev")}
    assert set(backend) == {"fast", "reviewer"}
    assert backend["fast"].alias == "deepseek:chat"
    assert backend["fast"].tools == ["read_file"]      # 角色 ["*"] ∩ agent 列表
    assert plan.reviewers("backend_dev")[0].profile == "reviewer"


def test_alias_precedence_prefers_profile_over_role_model():
    settings = Settings(default_model="mock:model",
                        role_bindings={"backend_dev": "bind:model"})
    assert alias_for(BACKEND, AgentProfile(key="x", model="profile:model"), settings) \
        == "profile:model"
    assert alias_for(BACKEND, AgentProfile(key="x"), settings) == "bind:model"
    assert alias_for(BACKEND, None, Settings(default_model="mock:model")) == "mock:model"


def test_pick_primary_is_deterministic_and_weighted():
    from agentcrew.agents.plan import ResolvedAgent
    pool = [
        ResolvedAgent(role="r", profile="a", alias="m", duty=Duty.PRIMARY, weight=1),
        ResolvedAgent(role="r", profile="b", alias="m", duty=Duty.PRIMARY, weight=1),
    ]
    first = pick_primary(pool, run_id="R", task_id="T", attempt=1, role="r").profile
    again = pick_primary(pool, run_id="R", task_id="T", attempt=1, role="r").profile
    assert first == again
    picks = {pick_primary(pool, run_id="R", task_id=f"T{i}", attempt=1, role="r").profile
             for i in range(30)}
    assert picks == {"a", "b"}          # 两个候选都能被选中（不是恒定取第一个）


# ── 3. 路径域 ──────────────────────────────────────────────────────────────

def test_store_rejects_path_outside_scope(store):
    task = _task("backend/app.py")
    with pytest.raises(ArtifactScopeError):
        store.commit("backend/app.py", "x = 1\n", spec=task.deliverables[0],
                     task=task, path_scope=["frontend/"], agent="fe")


def test_store_allows_path_inside_scope(store):
    task = _task("backend/app.py")
    record = store.commit("backend/app.py", "x = 1\n", spec=task.deliverables[0],
                          task=task, path_scope=["backend/"], agent="be")
    assert record.produced_by_agent == "be"


# ── 4. 团队执行 ────────────────────────────────────────────────────────────

def _team(store, binding, members, agents, *, mode):
    return TeamAgent(
        BACKEND, binding, members,
        get_agent=lambda p: agents[p],
        models=None, store=store, ledger=_NullLedger(), settings=Settings(),
        run_id="R")


class _NullLedger:
    def __init__(self):
        self.events = []

    def emit(self, name, **data):
        self.events.append((name, data))


def _member(profile, duty, scope=()):
    from agentcrew.agents.plan import ResolvedAgent
    return ResolvedAgent(role="backend_dev", profile=profile, alias="mock:model",
                         duty=duty, tools=["*"], path_scope=list(scope))


def test_split_runs_sections_in_parallel_and_merges(store):
    task = _task("backend/app.py", "tests/test_app.py")
    members = [_member("be", Duty.SECTION, ["backend/"]),
               _member("qa", Duty.SECTION, ["tests/"])]
    agents = {
        "be": StubAgent(store, {"backend/app.py": "app = 1\n"}, scope=["backend/"], key="be"),
        "qa": StubAgent(store, {"tests/test_app.py": "def test(): pass\n"},
                        scope=["tests/"], key="qa"),
    }
    binding = RoleAgents(agents=[
        BoundAgent(ref="be", duty=Duty.SECTION), BoundAgent(ref="qa", duty=Duty.SECTION)],
        aggregation=Aggregation(mode="split_by_path", quorum=2))
    result = asyncio.run(_team(store, binding, members, agents, mode="split")
                         .execute(task, None))
    assert {r.path for r in result.artifacts} == {"backend/app.py", "tests/test_app.py"}
    assert {r.produced_by_agent for r in result.artifacts} == {"be", "qa"}


def test_split_below_quorum_fails(store):
    from agentcrew.agents.base_agent import AgentExecutionError
    task = _task("backend/app.py", "tests/test_app.py")
    members = [_member("be", Duty.SECTION, ["backend/"]),
               _member("qa", Duty.SECTION, ["tests/"])]

    class Boom:
        async def execute(self, task, pack, *, fix_hints=()):
            raise RuntimeError("模型挂了")

    agents = {"be": StubAgent(store, {"backend/app.py": "x=1\n"}, scope=["backend/"], key="be"),
              "qa": Boom()}
    binding = RoleAgents(agents=[
        BoundAgent(ref="be", duty=Duty.SECTION), BoundAgent(ref="qa", duty=Duty.SECTION)],
        aggregation=Aggregation(mode="split_by_path", quorum=2))
    with pytest.raises(AgentExecutionError, match="quorum"):
        asyncio.run(_team(store, binding, members, agents, mode="split").execute(task, None))


def test_review_memo_is_merged_into_notes(store):
    task = _task("backend/app.py")
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    agents = {"be": StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")}
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review"))
    team = _team(store, binding, members, agents, mode="review")

    async def fake_review(reviewer, task, result):
        return "缺少输入校验，请补 422 用例"

    team._review_once = fake_review
    result = asyncio.run(team.execute(task, None))
    assert "缺少输入校验" in result.notes
    assert [r.path for r in result.artifacts] == ["backend/app.py"]


def test_review_failure_is_visible_not_silent(store):
    task = _task("backend/app.py")
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    agents = {"be": StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")}
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review"))
    team = _team(store, binding, members, agents, mode="review")

    async def boom(reviewer, task, result):
        raise RuntimeError("复核模型不可用")

    team._review_once = boom
    result = asyncio.run(team.execute(task, None))
    assert "复核不可用" in result.notes
    assert any(name == "agent_degraded" for name, _ in team._ledger.events)


def test_best_of_promotes_winner_draft(store):
    task = _task("backend/app.py")
    members = [_member("a", Duty.PRIMARY), _member("b", Duty.PRIMARY),
               _member("j", Duty.JUDGE)]
    agents = {
        "a": StubAgent(store, {"drafts/a/backend/app.py": "better = 1\n"}, key="a"),
        "b": StubAgent(store, {"drafts/b/backend/app.py": "worse = 1\n"}, key="b"),
    }
    binding = RoleAgents(
        agents=[BoundAgent(ref="a", duty=Duty.PRIMARY),
                BoundAgent(ref="b", duty=Duty.PRIMARY),
                BoundAgent(ref="j", duty=Duty.JUDGE)],
        aggregation=Aggregation(mode="best_of", judge="j"))
    team = _team(store, binding, members, agents, mode="best_of")

    async def fake_judge(task, pairs, judge_ref):
        return "a"

    team._judge = fake_judge
    result = asyncio.run(team.execute(task, None))
    assert store.read("backend/app.py") == "better = 1\n"
    assert [r.path for r in result.artifacts] == ["backend/app.py"]


# ── 5. 运行中切换 ──────────────────────────────────────────────────────────

def _factory(tmp_path, ledger, *, profiles, binding):
    from agentcrew.agents.registry import AgentFactory
    from agentcrew.models.factory import ModelFactory
    settings = Settings(default_model="mock:model", agent_profiles=profiles,
                        role_agents={"backend_dev": binding})
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=tmp_path)
    return AgentFactory(models, ArtifactStore(tmp_path / "art2", run_id="R", ledger=ledger),
                        ledger, "目标", RoleCatalog(BUILTIN_ROLES),
                        settings=settings, run_id="R")


def test_switch_agent_replaces_primary_and_emits_event(tmp_path):
    from agentcrew.agents.team import TeamAgent
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _factory(
        tmp_path, ledger,
        profiles={"a": AgentProfile(key="a"), "b": AgentProfile(key="b")},
        binding=RoleAgents(
            agents=[BoundAgent(ref="a", duty=Duty.PRIMARY),
                    BoundAgent(ref="b", duty=Duty.REVIEW)],
            aggregation=Aggregation(mode="primary_with_review")))
    assert isinstance(factory.get("backend_dev"), TeamAgent)     # 多 agent → 团队
    members = factory.switch_agent("backend_dev", "b")
    assert [m.profile for m in members] == ["b"]
    assert members[0].duty is Duty.PRIMARY
    assert not isinstance(factory.get("backend_dev"), TeamAgent)  # 切完只剩单执行体
    switched_event = next(iter(ledger.events("agent_switched")))
    assert switched_event.data["from_profile"] == "a"   # 审计要能看出从谁换到谁
    assert switched_event.data["to"] == "b"


def test_switch_to_unresolved_profile_is_rejected(tmp_path):
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _factory(
        tmp_path, ledger,
        profiles={"a": AgentProfile(key="a")},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))
    with pytest.raises(ValueError, match="没有已解析的 agent"):
        factory.switch_agent("backend_dev", "ghost")


def test_binding_overrides_roundtrip(tmp_path):
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _factory(
        tmp_path, ledger,
        profiles={"a": AgentProfile(key="a"), "b": AgentProfile(key="b")},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))
    factory.switch_agent("backend_dev", "b")
    exported = factory.export_bindings()
    assert exported["backend_dev"]["agents"][0]["ref"] == "b"

    other = _factory(
        tmp_path, ledger,
        profiles={"a": AgentProfile(key="a"), "b": AgentProfile(key="b")},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))
    assert other.import_bindings(exported) == 1
    assert [m.profile for m in other.plan().for_role("backend_dev")] == ["b"]


# ── 6. 执行体治理参数一路带到 Agent ────────────────────────────────────────

def test_profile_carries_budget_fallback_and_concurrency(tmp_path):
    """配置里的预算/备用模型/并发上限必须落到 Agent 实例上，不能只停留在解析层。"""
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _factory(
        tmp_path, ledger,
        profiles={"a": AgentProfile(
            key="a", cost_ceiling_usd=0.05, max_calls_per_task=7,
            max_concurrency=2, fallback=["mock:backup"])},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))
    agent = factory.get("backend_dev", "a")
    assert agent.cost_ceiling_usd == 0.05
    assert agent.max_calls_per_task == 7
    assert agent.max_concurrency == 2
    assert agent.fallback_models == ["mock:backup"]


def test_reset_agent_restores_configured_binding(tmp_path):
    """切换 → 恢复默认：回到配置里的执行体，且第二次恢复是幂等的。"""
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _factory(
        tmp_path, ledger,
        profiles={"a": AgentProfile(key="a"), "b": AgentProfile(key="b")},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))
    factory.switch_agent("backend_dev", "b")
    assert [m.profile for m in factory.plan().for_role("backend_dev")] == ["b"]

    members, changed = factory.reset_agent("backend_dev")
    assert changed is True
    assert [m.profile for m in members] == ["a"]
    assert ledger.events("agent_binding_reset")

    members, changed = factory.reset_agent("backend_dev")
    assert changed is False                      # 幂等：没覆盖就不算改动
    assert [m.profile for m in members] == ["a"]


# ── 7. 任务级覆盖（只影响这一次任务）──────────────────────────────────────

def _task_factory(tmp_path, ledger, profiles, binding):
    return _factory(tmp_path, ledger, profiles=profiles, binding=binding)


def test_task_override_wins_only_for_that_task(tmp_path):
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _task_factory(
        tmp_path, ledger,
        profiles={"a": AgentProfile(key="a"), "b": AgentProfile(key="b")},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))

    factory.set_task_override("backend_dev", "T-1", "b", reason="省钱")
    assert factory.agent_for_task("backend_dev", "T-1").agent_key == "b"
    # 同角色的另一个任务不受影响：仍走角色绑定（单执行体 → 无 agent_key）
    assert factory.agent_for_task("backend_dev", "T-2").agent_key == ""
    event = next(iter(ledger.events("task_agent_switched")))
    assert event.data["task_id"] == "T-1" and event.data["to"] == "b"


def test_task_override_rejects_unknown_profile(tmp_path):
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _task_factory(
        tmp_path, ledger, profiles={"a": AgentProfile(key="a")},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))
    with pytest.raises(ValueError, match="没有可用的 agent"):
        factory.set_task_override("backend_dev", "T-1", "ghost")


def test_task_override_reset_and_roundtrip(tmp_path):
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _task_factory(
        tmp_path, ledger,
        profiles={"a": AgentProfile(key="a"), "b": AgentProfile(key="b")},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))
    factory.set_task_override("backend_dev", "T-1", "b")
    exported = factory.export_task_overrides()
    assert exported["T-1"]["profile"] == "b" and exported["T-1"]["role"] == "backend_dev"

    other = _task_factory(
        tmp_path, ledger,
        profiles={"a": AgentProfile(key="a"), "b": AgentProfile(key="b")},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))
    assert other.import_task_overrides(exported) == 1
    assert other.agent_for_task("backend_dev", "T-1").agent_key == "b"

    previous, changed = other.reset_task_override("T-1")
    assert (previous, changed) == ("b", True)
    assert other.reset_task_override("T-1") == ("", False)     # 幂等


def test_stale_task_override_falls_back_instead_of_crashing(tmp_path):
    """落盘的覆盖指向了已删除的 profile：丢覆盖、记事件、按默认继续。"""
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _task_factory(
        tmp_path, ledger, profiles={"a": AgentProfile(key="a")},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))
    factory.import_task_overrides({"T-9": {"role": "backend_dev", "profile": "gone"}})
    assert factory.agent_for_task("backend_dev", "T-9").agent_key == ""
    assert ledger.events("task_agent_override_dropped")
    assert factory.export_task_overrides() == {}


def test_run_ceiling_carried_to_agent(tmp_path):
    """配置里的整场上限必须落到 Agent 实例（与并发/预算同一通道）。"""
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    factory = _factory(
        tmp_path, ledger,
        profiles={"a": AgentProfile(key="a", run_cost_ceiling_usd=0.5)},
        binding=RoleAgents(agents=[BoundAgent(ref="a", duty=Duty.PRIMARY)]))
    assert factory.get("backend_dev", "a").run_cost_ceiling_usd == 0.5
