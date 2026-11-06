"""R8：子任务被拒时把原因回灌给模型。

P2-1 起 Agent 只拿到「建成 / 没建成」，模型不知道自己错在哪，下一轮往往换个
同样错的名字继续试，或者干脆把这件活丢掉。R6 的委派四道闸会把原因写进 ledger，
但那是给人看的，模型看不见。

覆盖：裸闭包（无原因）不回灌、带原因的 dispatcher 触发一次纠正、提示词里含原因、
只纠正一轮（不会套娃）、配置 ``0`` 可关闭、Scheduler 侧的「设原因 / 成功清空」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentcrew.agents.generic import GenericAgent
from agentcrew.agents.registry import AgentFactory
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.crew.scheduler import Scheduler, SubtaskDispatcher
from agentcrew.gate.gatekeeper import Gatekeeper, NullPrompter
from agentcrew.gate.reviewer import Reviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import CompletionResult
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard

GOOD = "```python file=a.py\ndef f():\n    return 1\n```\n"
GOOD_V2 = "```python file=a.py\ndef f():\n    return 2\n```\n"
SUBTASK = (
    '<subtask>\n'
    '{"title": "子任务", "description": "d", "role": "ghost_dev", '
    '"deliverables": ["b.py"]}\n'
    '</subtask>\n'
)


def _role() -> RoleSpec:
    return RoleSpec(
        key="backend_dev", title="后端", goal="g", backstory="b",
        responsibilities=["写代码"], boundaries=["不写前端"],
        output_contract="输出 python 文件", system_prompt="你是后端工程师。",
    )


class _ScriptedProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.seen: list[list[dict]] = []
        self.binding = SimpleNamespace(
            provider="scripted", max_rpm=None, max_concurrency=None,
            input_price=None, output_price=None)

    async def complete(self, messages, *, temperature, max_tokens,
                       response_format=None):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        return CompletionResult(
            text=self._responses.pop(0), provider="scripted", model="x",
            prompt_tokens=10, completion_tokens=10, latency_ms=1,
        )


class _RejectingDispatcher:
    """模拟 Crew 装配的 ``SubtaskDispatcher``：总是拒绝并给出原因。"""

    def __init__(self, reason: str = "团队里没有 'ghost_dev' 这个角色") -> None:
        self.calls = 0
        self.last_rejection = reason

    def __call__(self, spec: dict) -> str | None:
        self.calls += 1
        return None


class _AcceptingDispatcher(_RejectingDispatcher):
    def __call__(self, spec: dict) -> str | None:
        self.calls += 1
        self.last_rejection = None
        return "T-DYN-001"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "false")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts",
                          run_id="R", ledger=ledger)
    task = Task(
        id="T-1", title="t", description="写 a.py", role="backend_dev",
        acceptance_criteria=["可运行"],
        deliverables=[ArtifactSpec(path="a.py", kind="python", description="d",
                                   must_contain=["def f"])],
    )
    bus = ContextBus("目标", TaskBoard([task]), store)
    return dict(settings=settings, ledger=ledger, store=store, task=task,
                bus=bus, models=ModelFactory(settings, ledger=ledger),
                tmp_path=tmp_path)


def _agent(env, dispatcher, *, corrections: int = 1) -> GenericAgent:
    provider = _ScriptedProvider([SUBTASK + GOOD, GOOD_V2])
    env["models"]._cache[env["settings"].default_model] = provider
    env["provider"] = provider
    return GenericAgent(
        _role(), models=env["models"], store=env["store"],
        ledger=env["ledger"], project_goal="目标",
        subtask_creator=dispatcher,
        max_subtask_corrections=corrections,
    )


# ── 回灌生效 ──────────────────────────────────────────────────────────────


async def test_dispatcher_rejection_feeds_back(env):
    """有原因 → 追加一次模型调用，且用调整后的回复作为最终产物。"""
    dispatcher = _RejectingDispatcher()
    agent = _agent(env, dispatcher)

    result = await agent.execute(env["task"], env["bus"].build(env["task"]))

    assert env["provider"].calls == 2
    assert result.task_id == "T-1"
    # 用的是第二次回复（return 2），不是第一次的
    assert "return 2" in env["store"].read("a.py")
    assert env["ledger"].count("subtask_rejection_fed_back") == 1


async def test_feedback_prompt_carries_reason(env):
    """回灌内容必须包含具体原因，否则模型只能盲猜。"""
    dispatcher = _RejectingDispatcher("团队里没有 'ghost_dev' 这个角色")
    agent = _agent(env, dispatcher)

    await agent.execute(env["task"], env["bus"].build(env["task"]))

    second = env["provider"].seen[1]
    feedback = second[-1]["content"]
    assert "ghost_dev" in feedback
    assert "没有被接受" in feedback


async def test_feedback_happens_only_once(env):
    """再被拒也不追问：只纠正一轮，不套娃。"""
    dispatcher = _RejectingDispatcher()
    # 两次回复都带子任务请求：第二次仍被拒，但不该触发第三轮
    provider = _ScriptedProvider([SUBTASK + GOOD, SUBTASK + GOOD_V2])
    env["models"]._cache[env["settings"].default_model] = provider
    agent = GenericAgent(
        _role(), models=env["models"], store=env["store"],
        ledger=env["ledger"], project_goal="目标",
        subtask_creator=dispatcher, max_subtask_corrections=1,
    )

    await agent.execute(env["task"], env["bus"].build(env["task"]))

    assert provider.calls == 2
    # 两次请求都被拒 → dispatcher 被调用两次（原始 + 重试后），但只回灌一次
    assert dispatcher.calls == 2
    assert env["ledger"].count("subtask_rejection_fed_back") == 1


# ── 兼容：没有原因就不回灌 ────────────────────────────────────────────────


async def test_bare_lambda_does_not_feed_back(env):
    """裸闭包没有 last_rejection：不回灌、不多花一次调用（既有测试的形态）。"""
    provider = _ScriptedProvider([SUBTASK + GOOD])
    env["models"]._cache[env["settings"].default_model] = provider
    agent = GenericAgent(
        _role(), models=env["models"], store=env["store"],
        ledger=env["ledger"], project_goal="目标",
        subtask_creator=lambda spec: None,
    )

    result = await agent.execute(env["task"], env["bus"].build(env["task"]))

    assert provider.calls == 1
    assert result.task_id == "T-1"
    assert env["ledger"].count("subtask_rejection_fed_back") == 0
    # 单纯的拒绝仍然留痕（reason 为空串）
    rejected = list(env["ledger"].events("subtask_rejected"))
    assert rejected[0].data["reason"] == ""


async def test_corrections_zero_disables_feedback(env):
    """``max_subtask_corrections=0``：行为回到 R8 之前。"""
    dispatcher = _RejectingDispatcher()
    agent = _agent(env, dispatcher, corrections=0)

    await agent.execute(env["task"], env["bus"].build(env["task"]))

    assert env["provider"].calls == 1
    assert env["ledger"].count("subtask_rejection_fed_back") == 0


async def test_accepted_subtask_is_silent(env):
    """建成功时既不回灌也不记拒绝。"""
    provider = _ScriptedProvider([SUBTASK + GOOD])
    env["models"]._cache[env["settings"].default_model] = provider
    agent = GenericAgent(
        _role(), models=env["models"], store=env["store"],
        ledger=env["ledger"], project_goal="目标",
        subtask_creator=_AcceptingDispatcher(),
    )

    await agent.execute(env["task"], env["bus"].build(env["task"]))

    assert provider.calls == 1
    assert env["ledger"].count("subtask_created") == 1
    assert env["ledger"].count("subtask_rejected") == 0


# ── Scheduler 侧：原因的产生与清空 ────────────────────────────────────────


@pytest.fixture
def scheduler_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "s.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts",
                          run_id="R", ledger=ledger)
    root = Task(id="T-1", title="r", description="d", role="backend_dev",
                acceptance_criteria=[], deliverables=[])
    board = TaskBoard([root])
    bus = ContextBus("目标", board, store)
    catalog = RoleCatalog(BUILTIN_ROLES)
    fixture_dir = tmp_path / "fx"
    fixture_dir.mkdir(exist_ok=True)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=fixture_dir)
    agents = AgentFactory(models, store, ledger, "目标", catalog)
    reviewer = Reviewer(models, catalog, store, ledger)
    gatekeeper = Gatekeeper(reviewer, store, board, gate="none",
                            ledger=ledger, prompter=NullPrompter())
    scheduler = Scheduler(board, agents, gatekeeper, bus, ledger=ledger,
                          catalog=catalog, settings=settings)
    return dict(scheduler=scheduler, ledger=ledger, board=board, settings=settings)


def test_dispatcher_sets_reason_on_rejection(scheduler_env):
    """拒绝时把原因挂到可调用对象上（Agent 从那里取）。"""
    scheduler = scheduler_env["scheduler"]
    dispatcher = SubtaskDispatcher(scheduler)

    child = dispatcher({"title": "t", "role": "ghost_dev",
                        "deliverables": ["x.py"], "parent_id": "T-1"})

    assert child is None
    assert "ghost_dev" in dispatcher.last_rejection


def test_dispatcher_clears_reason_on_success(scheduler_env):
    """成功后必须清空：否则下一次读到的会是上一次的陈旧原因。"""
    scheduler = scheduler_env["scheduler"]
    dispatcher = SubtaskDispatcher(scheduler)

    dispatcher({"title": "t", "role": "ghost_dev",
                "deliverables": ["x.py"], "parent_id": "T-1"})
    assert dispatcher.last_rejection is not None

    child = dispatcher({"title": "ok", "role": "qa_engineer",
                        "deliverables": ["y.py"], "parent_id": "T-1"})

    assert child is not None
    assert dispatcher.last_rejection is None


def test_scheduler_injects_dispatcher_not_bound_method(scheduler_env):
    """Scheduler 必须注入 dispatcher 对象，否则 Agent 拿不到 last_rejection。"""
    creator = scheduler_env["scheduler"]._agents.subtask_creator

    assert isinstance(creator, SubtaskDispatcher)
    assert hasattr(creator, "last_rejection")
