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
        #: 每次 execute 收到的 fix_hints —— 用来断言复核意见真的回灌给了主笔。
        self.hints: list[list[str]] = []
        #: 每次 execute 收到的上下文包 —— 用来断言分工依赖真的注入了上游摘要。
        self.packs: list = []

    async def execute(self, task, pack, *, fix_hints=()):
        self.hints.append(list(fix_hints))
        self.packs.append(pack)
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

def _team(store, binding, members, agents, *, mode, settings=None):
    return TeamAgent(
        BACKEND, binding, members,
        get_agent=lambda p: agents[p],
        models=None, store=store, ledger=_NullLedger(), settings=settings or Settings(),
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


def _outcome(profile, *, approved=True, issues=(), hints=(), suggestions=()):
    """构造一份复核结论（替身用）。裁决结构由 ``gate.verdict`` 统一，这里只填字段。"""
    from agentcrew.agents.team import _ReviewOutcome

    return _ReviewOutcome(
        profile=profile, approved=approved, issues=list(issues),
        hints=list(hints), suggestions=list(suggestions))


def test_review_memo_is_merged_into_notes(store):
    task = _task("backend/app.py")
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    agents = {"be": StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")}
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review"))
    team = _team(store, binding, members, agents, mode="review")

    async def fake_review(reviewer, task, result, round_no=1):
        return _outcome(reviewer.profile, approved=False,
                        issues=["缺少输入校验，请补 422 用例"],
                        hints=["补 422 用例"])

    team._review_once = fake_review
    result = asyncio.run(team.execute(task, None))
    assert "缺少输入校验" in result.notes
    assert [r.path for r in result.artifacts] == ["backend/app.py"]
    assert "已按复核意见修订" not in result.notes, "默认轮数为 0，不该修订"


def test_review_failure_is_visible_not_silent(store):
    task = _task("backend/app.py")
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    agents = {"be": StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")}
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review"))
    team = _team(store, binding, members, agents, mode="review")

    async def boom(reviewer, task, result, round_no=1):
        raise RuntimeError("复核模型不可用")

    team._review_once = boom
    result = asyncio.run(team.execute(task, None))
    assert "复核不可用" in result.notes
    assert any(name == "agent_degraded" for name, _ in team._ledger.events)


def test_review_rounds_feeds_issues_back_to_primary(store):
    """``review_rounds=1``：复核出问题 → 意见进 fix_hints 重跑主笔 → 再复核。"""
    task = _task("backend/app.py")
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    primary = StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")
    agents = {"be": primary}
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review", review_rounds=1))
    team = _team(store, binding, members, agents, mode="review")

    outcomes = iter([
        _outcome("rv", approved=False,
                 issues=["未满足验收标准：缺少输入校验 —— 没有 422 分支"],
                 hints=["补 422 用例"]),
        _outcome("rv", approved=True),
    ])

    async def fake_review(reviewer, task, result, round_no=1):
        return next(outcomes)

    team._review_once = fake_review
    result = asyncio.run(team.execute(task, None))

    assert len(primary.hints) == 2, "复核报问题后主笔必须被重跑一次"
    assert any("422" in hint for hint in primary.hints[1]), "复核意见要真的进 fix_hints"
    assert "已按复核意见修订 1 轮" in result.notes
    assert [name for name, _ in team._ledger.events].count("agent_revise") == 1


def test_review_rounds_default_keeps_notes_only(store):
    """默认 ``review_rounds=0``：只出意见、不重跑主笔（向后兼容）。"""
    task = _task("backend/app.py")
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    primary = StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review"))
    team = _team(store, binding, members, {"be": primary}, mode="review")

    async def fake_review(reviewer, task, result, round_no=1):
        return _outcome(reviewer.profile, approved=False,
                        issues=["缺少输入校验，请补 422 用例"],
                        hints=["补 422 用例"])

    team._review_once = fake_review
    result = asyncio.run(team.execute(task, None))

    assert len(primary.hints) == 1, "默认不修订，主笔只跑一次"
    assert "缺少输入校验" in result.notes
    assert "修订" not in result.notes


def test_review_rounds_stops_when_no_issue(store):
    """复核说「未发现问题」时，即使开了多轮也不该白跑主笔。"""
    task = _task("backend/app.py")
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    primary = StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review", review_rounds=2))
    team = _team(store, binding, members, {"be": primary}, mode="review")

    async def fake_review(reviewer, task, result, round_no=1):
        return _outcome(reviewer.profile, approved=True)

    team._review_once = fake_review
    asyncio.run(team.execute(task, None))

    assert len(primary.hints) == 1
    assert not [n for n, _ in team._ledger.events if n == "agent_revise"]


def test_review_uses_structured_criteria_verdict(store):
    """复核裁决由「逐条判定」派生，评审措辞不参与判定。

    这是与 G2 门禁对齐的关键：过去复核返回自由文本、靠 ``"未发现问题"`` 子串判断，
    模型换个说法就会触发无意义修订。现在只认 ``ReviewVerdict``，且
    **标准原文由 ``derive_review`` 按序号回填**，不采信模型复述。
    """
    from agentcrew.gate.verdict import ReviewVerdict

    task = _task("backend/app.py")  # 验收标准：["可运行"]
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    primary = StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review", review_rounds=1))
    team = _team(store, binding, members, {"be": primary}, mode="review")

    calls = []

    async def fake_structured(member, task, messages, schema, *, temperature, max_tokens, review_round=1):
        calls.append(schema)
        assert schema is ReviewVerdict
        if len(calls) == 1:
            return ReviewVerdict(judgements=[{
                "criterion_index": 0, "satisfied": False,
                "evidence": "没有启动入口"}])
        return ReviewVerdict(judgements=[{
            "criterion_index": 0, "satisfied": True,
            "evidence": "产物里有 main 入口"}])

    team._call_structured = fake_structured
    result = asyncio.run(team.execute(task, None))

    assert calls and all(c is ReviewVerdict for c in calls)
    # 标准原文来自任务契约（"可运行"），不是模型写的话
    assert "未满足验收标准：可运行 —— 没有启动入口" in result.notes
    # 依据作为修订指令回灌主笔
    assert any("没有启动入口" in h for h in primary.hints[1])
    assert "已按复核意见修订 1 轮" in result.notes


def test_malformed_review_is_degraded_not_a_rejection(store):
    """裁决覆盖率不足 = 复核器故障，必须记为降级，**不得**触发返工。

    拿不到可用裁决就当作"有问题"，会让主笔照着空气改——这正是旧版
    「reject 无 issues」踩过的坑。
    """
    from agentcrew.gate.verdict import ReviewVerdict

    task = _task("backend/app.py")  # 1 条标准
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    primary = StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review", review_rounds=2))
    team = _team(store, binding, members, {"be": primary}, mode="review")

    async def empty_verdict(member, task, messages, schema, *, temperature, max_tokens, review_round=1):
        return ReviewVerdict(judgements=[], suggestions=["再打磨一下"])

    team._call_structured = empty_verdict
    result = asyncio.run(team.execute(task, None))

    assert "复核不可用" in result.notes
    assert len(primary.hints) == 1, "格式错误不该引发返工"
    names = [n for n, _ in team._ledger.events]
    assert "agent_degraded" in names
    assert "agent_revise" not in names


def test_review_suggestions_do_not_trigger_revision(store):
    """「还能更好」进 suggestions，不构成不满足，不引发修订。"""
    from agentcrew.gate.verdict import ReviewVerdict

    task = _task("backend/app.py")
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    primary = StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review", review_rounds=2))
    team = _team(store, binding, members, {"be": primary}, mode="review")

    async def suggestion_only(member, task, messages, schema, *, temperature, max_tokens, review_round=1):
        return ReviewVerdict(
            judgements=[{"criterion_index": 0, "satisfied": True,
                         "evidence": "有 main 入口"}],
            suggestions=["可以补一个 --help"])

    team._call_structured = suggestion_only
    result = asyncio.run(team.execute(task, None))

    assert len(primary.hints) == 1
    assert "可以补一个 --help" in result.notes
    assert "修订" not in result.notes


def test_circuit_breaker_counts_consecutive_failures_only():
    """熔断按"连续"失败计：成功一次即清零，不是"曾经失败过就判死"。"""
    from agentcrew.agents.team import _Circuit

    circuit = _Circuit(threshold=2)
    assert not circuit.failure("a")
    assert circuit.ok("a")
    assert circuit.failure("a") is True, "第 2 次连续失败应熔断"
    assert not circuit.ok("a")
    assert circuit.failure("a") is False, "已熔断不重复上报"
    circuit.success("a")
    assert circuit.ok("a"), "成功一次即恢复"
    assert circuit.threshold == 2


def test_circuit_breaker_skips_failing_primary(store):
    """连续失败的执行体被跳过，任务改由同角色另一个 primary 承担。"""
    settings = Settings(
        agent_circuit_breaker_enabled=True, agent_circuit_breaker_threshold=2)
    members = [_member("bad", Duty.PRIMARY), _member("good", Duty.PRIMARY)]
    # 权重悬殊：熔断前几乎一定选中 bad，便于观察"什么时候不再选它"。
    members[0] = members[0].model_copy(update={"weight": 1000.0})
    members[1] = members[1].model_copy(update={"weight": 1.0})
    calls = {"bad": 0, "good": 0}

    class Boom:
        async def execute(self, task, pack, *, fix_hints=()):
            calls["bad"] += 1
            raise RuntimeError("这个执行体就是不行")

    class Fine(StubAgent):
        async def execute(self, task, pack, *, fix_hints=()):
            calls["good"] += 1
            return await super().execute(task, pack, fix_hints=fix_hints)

    agents = {"bad": Boom(),
              "good": Fine(store, {"backend/app.py": "app = 1\n"}, key="good")}
    binding = RoleAgents(
        agents=[BoundAgent(ref="bad", duty=Duty.PRIMARY),
                BoundAgent(ref="good", duty=Duty.PRIMARY)],
        aggregation=Aggregation(mode="primary_with_review", quorum=1, review_rounds=0))
    team = _team(store, binding, members, agents, mode="single", settings=settings)

    for i in range(2):
        with pytest.raises(RuntimeError):
            asyncio.run(team.execute(_task("backend/app.py", task_id=f"T-{i}"), None))

    # 阈值 2 已到：第三次必须换人，且任务应当成功
    result = asyncio.run(team.execute(_task("backend/app.py", task_id="T-9"), None))
    assert result.artifacts, "熔断后应由 healthy 的 primary 完成"
    assert calls["bad"] == 2, "熔断后不得再调用坏执行体"
    assert calls["good"] == 1
    assert [n for n, _ in team._ledger.events].count("agent_circuit_open") == 1


def test_circuit_breaker_disabled_by_default(store):
    """默认关闭：失败多少次都照常重试（与引入熔断之前逐字一致）。"""
    members = [_member("bad", Duty.PRIMARY), _member("good", Duty.PRIMARY)]
    members[0] = members[0].model_copy(update={"weight": 1000.0})
    members[1] = members[1].model_copy(update={"weight": 1.0})
    calls = {"bad": 0}

    class Boom:
        async def execute(self, task, pack, *, fix_hints=()):
            calls["bad"] += 1
            raise RuntimeError("还是不行")

    agents = {"bad": Boom(),
              "good": StubAgent(store, {"backend/app.py": "app = 1\n"}, key="good")}
    binding = RoleAgents(
        agents=[BoundAgent(ref="bad", duty=Duty.PRIMARY),
                BoundAgent(ref="good", duty=Duty.PRIMARY)],
        aggregation=Aggregation(mode="primary_with_review", quorum=1))
    team = _team(store, binding, members, agents, mode="single")

    for i in range(4):
        with pytest.raises(RuntimeError):
            asyncio.run(team.execute(_task("backend/app.py", task_id=f"T-{i}"), None))

    assert calls["bad"] == 4, "开关默认关，不该自动跳过"
    assert not [n for n, _ in team._ledger.events if n == "agent_circuit_open"]


def test_review_rounds_is_bounded():
    """修订轮数上界 3：再多说明验收标准本身没写清。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Aggregation(mode="primary_with_review", review_rounds=4)
    with pytest.raises(ValidationError):
        Aggregation(mode="primary_with_review", review_rounds=-1)


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


def test_revision_loop_converges_through_mock_fixtures(store, tmp_path):
    """端到端（真工厂 + mock 回放）：第 1 轮不满足 → 修订 → 第 2 轮满足。

    这条用例钉的是"修订回路真的能收敛"：不用替身桩住 ``_call_structured``，
    而是走 ``ModelFactory.complete_structured`` → ``MockProvider`` → ``reviews.json``。
    夹具用 ``"T-1@1"`` / ``"T-1@2"`` 区分轮次（R47 新增的回放能力）。
    """
    import json

    from agentcrew.models.factory import ModelFactory

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "reviews.json").write_text(json.dumps({
        "T-1@1": {
            "judgements": [{"criterion_index": 0, "satisfied": False,
                            "evidence": "没有启动入口"}],
            "suggestions": ["可以顺手补个 --help"], "score": 0.3,
        },
        "T-1@2": {
            "judgements": [{"criterion_index": 0, "satisfied": True,
                            "evidence": "已有 main 入口"}],
            "suggestions": [], "score": 0.9,
        },
    }, ensure_ascii=False), encoding="utf-8")

    task = _task("backend/app.py")  # 验收标准：["可运行"]
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    primary = StubAgent(store, {"backend/app.py": "app = 1\n"}, key="be")
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review", review_rounds=2))
    settings = Settings(default_model="mock:model")
    ledger = _NullLedger()
    team = TeamAgent(
        BACKEND, binding, members,
        get_agent=lambda p: primary,
        models=ModelFactory(settings, ledger=ledger, fixtures_dir=fixtures),
        store=store, ledger=ledger, settings=settings, run_id="R")

    result = asyncio.run(team.execute(task, None))

    assert len(primary.hints) == 2, "第 1 轮不满足 → 主笔应被重跑一次"
    assert any("没有启动入口" in hint for hint in primary.hints[1])
    assert "已按复核意见修订 1 轮" in result.notes
    # 两轮都留在报告里（第 3 轮因已通过而不再发生）
    assert "第 1 轮" in result.notes and "第 2 轮" in result.notes
    names = [n for n, _ in ledger.events]
    assert names.count("agent_revise") == 1, "收敛后不得继续修订"
    approved = [d.get("approved") for n, d in ledger.events if n == "agent_review"]
    assert approved == [False, True], "第 1 轮驳回、第 2 轮通过"
    # 驳回必须留原文：只有条数的话，事后无法复盘"到底哪条不满足"
    details = [d.get("detail", "") for n, d in ledger.events if n == "agent_review"]
    assert "未满足验收标准：可运行" in details[0]
    assert details[1] == "未发现问题"




def test_excerpt_shows_every_file_even_behind_a_huge_one(store):
    """摘录预算必须均分：大文件吃掉全部额度会让后面的文件"看起来不存在"。

    实测事故：README 已写入（ledger 里 4 条 artifact_written），复核却判
    "未提供 README"，并且这条意见靠修订永远修不掉——文件一直在，只是没被展示。
    """
    task = _task("backend/app.py", "README.md", "tests/test_app.py")
    members = [_member("be", Duty.PRIMARY), _member("rv", Duty.REVIEW)]
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.PRIMARY),
                BoundAgent(ref="rv", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review"))
    team = _team(store, binding, members, {"be": StubAgent(store, {}, key="be")},
                 mode="review")

    huge = "x = 1\n" * 6000          # 远超 1800 token 的总预算
    result = asyncio.run(StubAgent(
        store,
        {"backend/app.py": huge,
         "README.md": "# 安装\npip install -e .\n",
         "tests/test_app.py": "def test_ok(): assert True\n"},
        key="be").execute(task, None))

    excerpt = team._artifact_excerpt(result)
    for path in ("backend/app.py", "README.md", "tests/test_app.py"):
        assert path in excerpt, f"{path} 必须出现（哪怕只给文件名与开头）"
    assert "已截断" in excerpt, "大文件应被截断并标注"


# ── R49：分工依赖（intra_deps）─────────────────────────────────────────────


def _scope_profiles(*keys: str) -> dict:
    return {k: AgentProfile(key=k) for k in keys}


def test_intra_deps_only_valid_for_split_mode():
    profiles = _scope_profiles("a", "b")
    binding = RoleAgents(
        agents=[BoundAgent(ref="a", duty=Duty.PRIMARY),
                BoundAgent(ref="b", duty=Duty.REVIEW)],
        aggregation=Aggregation(mode="primary_with_review",
                                intra_deps={"a": ["b"]}))
    with pytest.raises(BindingError, match="只在 split_by_path"):
        validate_bindings("backend_dev", binding, profiles, ["*"])


def test_intra_deps_rejects_self_unknown_and_cycle():
    profiles = {
        "a": AgentProfile(key="a", path_scope=["x/"]),
        "b": AgentProfile(key="b", path_scope=["y/"]),
    }
    agents = [BoundAgent(ref="a", duty=Duty.SECTION),
              BoundAgent(ref="b", duty=Duty.SECTION)]

    def check(deps, match):
        binding = RoleAgents(
            agents=agents,
            aggregation=Aggregation(mode="split_by_path", quorum=2, intra_deps=deps))
        with pytest.raises(BindingError, match=match):
            validate_bindings("backend_dev", binding, profiles, ["*"])

    check({"a": ["a"]}, "不能依赖自己")
    check({"a": ["zzz"]}, "非 section")
    check({"a": ["b"], "b": ["a"]}, "成环")


def test_intra_deps_runs_in_order_and_injects_upstream(store):
    """依赖方在依赖项之后执行，并在上下文里拿到对方的摘要与产物路径。"""
    from agentcrew.context.context_bus import ContextPack

    task = _task("backend/app.py", "tests/test_app.py")
    members = [_member("be", Duty.SECTION, ["backend/"]),
               _member("qa", Duty.SECTION, ["tests/"])]
    order: list[str] = []

    class Recorder(StubAgent):
        async def execute(self, task, pack, *, fix_hints=()):
            order.append(self.key)
            return await super().execute(task, pack, fix_hints=fix_hints)

    be = Recorder(store, {"backend/app.py": "app = 1\n"}, scope=["backend/"], key="be")
    qa = Recorder(store, {"tests/test_app.py": "def test(): pass\n"},
                  scope=["tests/"], key="qa")
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.SECTION),
                BoundAgent(ref="qa", duty=Duty.SECTION)],
        aggregation=Aggregation(mode="split_by_path", quorum=2,
                                intra_deps={"qa": ["be"]}))
    team = _team(store, binding, members, {"be": be, "qa": qa}, mode="split")
    pack = ContextPack(project_goal="做后端", task=task)

    result = asyncio.run(team.execute(task, pack))

    assert order == ["be", "qa"], "依赖方必须后执行"
    assert {r.path for r in result.artifacts} == {"backend/app.py", "tests/test_app.py"}
    # be 拿到的是原上下文；qa 多了一份上游摘要
    assert not be.packs[0].upstream
    upstream = qa.packs[0].upstream
    assert len(upstream) == 1
    assert upstream[0].files == ["backend/app.py"]
    assert "be" in upstream[0].title


def test_intra_deps_failure_does_not_block_dependents(store):
    """依赖项失败不阻塞下游：仍按序跑完，由 quorum 判定整体成败。

    否则一个 section 失败会让整条链静默停摆，连"哪几份产物缺失"都报不出来。
    """
    task = _task("backend/app.py", "tests/test_app.py")
    members = [_member("be", Duty.SECTION, ["backend/"]),
               _member("qa", Duty.SECTION, ["tests/"])]
    order: list[str] = []

    class Boom:
        async def execute(self, task, pack, *, fix_hints=()):
            order.append("be")
            raise RuntimeError("后端挂了")

    class Recorder(StubAgent):
        async def execute(self, task, pack, *, fix_hints=()):
            order.append(self.key)
            return await super().execute(task, pack, fix_hints=fix_hints)

    qa = Recorder(store, {"tests/test_app.py": "def test(): pass\n"},
                  scope=["tests/"], key="qa")
    binding = RoleAgents(
        agents=[BoundAgent(ref="be", duty=Duty.SECTION),
                BoundAgent(ref="qa", duty=Duty.SECTION)],
        aggregation=Aggregation(mode="split_by_path", quorum=1,
                                intra_deps={"qa": ["be"]}))
    team = _team(store, binding, members, {"be": Boom(), "qa": qa}, mode="split")

    result = asyncio.run(team.execute(task, _task("backend/app.py")))

    assert order == ["be", "qa"], "依赖项失败也要继续跑下游"
    assert [r.path for r in result.artifacts] == ["tests/test_app.py"]

