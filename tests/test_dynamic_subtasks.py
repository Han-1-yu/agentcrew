"""P2-1 运行时动态子任务。

覆盖：

- ``TaskBoard.add_task`` 动态入板后状态为 READY 并能被 ``take_ready`` 拾取；
- ``TaskBoard.children_of`` 按 ``parent_id`` 查询；
- ``BaseAgent`` 检测回复里的 ``<subtask>`` 围栏并经回调创建子任务、记 ledger；
- 未注入 ``subtask_creator`` 时完全不检测（向后兼容，行为不变）；
- 子任务带 ``parent_id`` 与 ``dynamic=True``。
"""

from pathlib import Path

import pytest

from agentcrew.agents.generic import GenericAgent
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
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task import TaskStatus as S
from agentcrew.task.task_board import TaskBoard


def role():
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件", system_prompt="你是后端工程师。")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts", run_id="R", ledger=lg)
    spec = ArtifactSpec(path="a.py", kind="python", description="d",
                        must_contain=["def f"])
    task = Task(id="T-1", title="t", description="写 a.py", role="backend_dev",
                acceptance_criteria=["可运行"], deliverables=[spec])
    board = TaskBoard([task])
    bus = ContextBus("目标", board, store)
    return dict(settings=s, ledger=lg, store=store, spec=spec, task=task,
                board=board, bus=bus, tmp_path=tmp_path)


def write_fixture(fx: Path, text: str, *, attempt: int = 1, task_id: str = "T-1") -> None:
    directory = fx / "responses" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"attempt-{attempt}.md").write_text(text, encoding="utf-8")


GOOD_A = "```python file=a.py\ndef f():\n    return 1\n```\n"


# ── TaskBoard 层 ──

def test_add_task_is_picked_up_by_take_ready(env):
    board = env["board"]
    child = Task(id="T-DYN-001", title="子", description="d", role="backend_dev",
                 acceptance_criteria=[], deliverables=[], parent_id="T-1",
                 dynamic=True)
    board.add_task(child)
    added = board.get("T-DYN-001")
    assert added.status is S.READY            # 初始即 READY
    assert added.dynamic is True and added.parent_id == "T-1"
    # 历史里留下这次注入的显式记录（与 reset_failed 同类的可见绕过）
    assert added.status_history[-1].reason == "dynamic-created"
    # take_ready 拾取它
    taken = board.take_ready(limit=10)
    assert "T-DYN-001" in [t.id for t in taken]


def test_add_task_rejects_duplicate_id(env):
    board = env["board"]
    dup = Task(id="T-1", title="x", description="d", role="backend_dev",
               acceptance_criteria=[], deliverables=[])
    with pytest.raises(ValueError):
        board.add_task(dup)


def test_children_of_filters_by_parent(env):
    board = env["board"]
    board.add_task(Task(id="T-DYN-001", title="c1", description="d", role="x",
                        acceptance_criteria=[], deliverables=[], parent_id="T-1",
                        dynamic=True))
    board.add_task(Task(id="T-DYN-002", title="c2", description="d", role="x",
                        acceptance_criteria=[], deliverables=[], parent_id="T-1",
                        dynamic=True))
    board.add_task(Task(id="T-DYN-003", title="other", description="d", role="x",
                        acceptance_criteria=[], deliverables=[], parent_id="T-9",
                        dynamic=True))
    kids = board.children_of("T-1")
    assert [t.id for t in kids] == ["T-DYN-001", "T-DYN-002"]


def test_failed_dynamic_task_does_not_crash_refresh(env):
    """动态子任务 FAILED 后 refresh() 不得 KeyError（回归：真实 run 曾整跑崩掉）。

    动态任务不在建图节点集里，失败传播对它是空集——但 refresh 必须照常执行：
    图上任务照旧被判定，动态任务的失败不阻塞任何图上任务。
    """
    board = env["board"]
    board.add_task(Task(id="T-DYN-001", title="子", description="d", role="x",
                        acceptance_criteria=[], deliverables=[], parent_id="T-1",
                        dynamic=True))
    board.transition("T-DYN-001", S.RUNNING, reason="test-dispatch")
    board.transition("T-DYN-001", S.FAILED, reason="test-fail")
    # refresh 不抛 KeyError；图上任务（T-1）不因动态子任务失败而 BLOCKED，
    # 照常按依赖判定（无前置 → PENDING 转 READY）
    board.refresh()
    assert board.get("T-1").status is S.READY


# ── Agent 层 ──

async def test_agent_creates_subtask_from_fence(env, tmp_path):
    fx = tmp_path / "fx"
    subtask_block = (
        '<subtask>\n'
        '{"title": "写工具模块", "description": "实现 utils", '
        '"role": "backend_dev", "deliverables": ["utils.py"]}\n'
        '</subtask>\n'
    )
    write_fixture(fx, subtask_block + GOOD_A)

    created: list[dict] = []

    def creator(spec: dict) -> str:
        created.append(spec)
        return "T-DYN-001"

    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         subtask_creator=creator)
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    # 本任务自己的产物照常落盘
    assert env["store"].read("a.py").startswith("def f()")
    # 回调被调用，且 parent_id 由 Agent 注入
    assert len(created) == 1
    assert created[0]["title"] == "写工具模块"
    assert created[0]["parent_id"] == "T-1"
    # ledger 留痕
    events = list(env["ledger"].events("subtask_created"))
    assert len(events) == 1
    assert events[0].data["parent_id"] == "T-1"
    assert events[0].data["child_id"] == "T-DYN-001"
    assert events[0].data["title"] == "写工具模块"


async def test_no_creator_means_no_subtask_detection(env, tmp_path):
    """向后兼容：未注入 creator 时 <subtask> 围栏被忽略，本任务照常成功。"""
    fx = tmp_path / "fx"
    subtask_block = (
        '<subtask>\n{"title": "x", "description": "y", "role": "backend_dev"}\n</subtask>\n'
    )
    write_fixture(fx, subtask_block + GOOD_A)
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标")
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["store"].read("a.py").startswith("def f()")
    assert env["ledger"].count("subtask_created") == 0


def test_subtask_section_only_in_system_prompt_when_creator_set(env):
    fx = env["tmp_path"] / "fx"
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    with_creator = GenericAgent(
        role(), models=models, store=env["store"], ledger=env["ledger"],
        project_goal="目标", subtask_creator=lambda s: "T-DYN-001")
    without_creator = GenericAgent(
        role(), models=models, store=env["store"], ledger=env["ledger"],
        project_goal="目标")
    sys_with = with_creator.build_messages(env["task"], env["bus"].build(env["task"]), ())[0]["content"]
    sys_without = without_creator.build_messages(env["task"], env["bus"].build(env["task"]), ())[0]["content"]
    assert "<subtask>" in sys_with and "动态子任务" in sys_with
    assert "<subtask>" not in sys_without


async def test_malformed_subtask_block_does_not_fail_task(env, tmp_path):
    """坏 JSON 的子任务请求只留痕、跳过，不拖垮本任务。"""
    fx = tmp_path / "fx"
    write_fixture(fx, "<subtask>\n{not json\n</subtask>\n" + GOOD_A)
    created: list[dict] = []
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         subtask_creator=lambda s: created.append(s) or "T-DYN-001")
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["store"].read("a.py").startswith("def f()")
    assert created == []                       # 坏块没被当成合法 spec
    assert env["ledger"].count("subtask_created") == 0
    assert env["ledger"].count("subtask_malformed") == 1


# ── 契约路径先占先得（realrun10 实测）──
#
# T-IMPL 声明交付 ``logstat/__init__.py``，它拆出的动态子任务又声明同一路径：
# 子任务先写完，父任务落盘时被跨任务冲突检测拒掉，三次 attempt 烧光后 FAILED、
# 下游全 BLOCKED。冲突检测本身是对的（防覆盖），错的是让子任务去占既有契约。


def _scheduler(env) -> Scheduler:
    fx = env["tmp_path"] / "fx-sched"
    fx.mkdir(exist_ok=True)
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    catalog = RoleCatalog(BUILTIN_ROLES)
    agents = AgentFactory(models, env["store"], env["ledger"], "目标", catalog)
    reviewer = Reviewer(models, catalog, env["store"], env["ledger"])
    gk = Gatekeeper(reviewer, env["store"], env["board"], gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    return Scheduler(env["board"], agents, gk, env["bus"], ledger=env["ledger"])


def test_subtask_rejects_fully_conflicting_deliverables(env):
    """请求的交付路径全被既有契约占用时：不建任务，留一条 subtask_conflict。"""
    scheduler = _scheduler(env)
    child_id = scheduler._create_subtask({
        "title": "重复写 a.py", "role": "backend_dev",
        "deliverables": ["a.py"], "parent_id": "T-1",
    })
    assert child_id is None
    assert env["ledger"].count("subtask_conflict") == 1
    assert [t.id for t in env["board"].all() if t.dynamic] == []


def test_subtask_prunes_only_conflicting_deliverable(env):
    """部分冲突只剔除冲突项：子任务照样建，剩下的产物仍受契约保护。"""
    scheduler = _scheduler(env)
    child_id = scheduler._create_subtask({
        "title": "补工具模块", "role": "backend_dev",
        "deliverables": ["a.py", "utils.py"], "parent_id": "T-1",
    })
    assert child_id is not None
    assert [d.path for d in env["board"].get(child_id).deliverables] == ["utils.py"]
    assert env["ledger"].count("subtask_deliverable_pruned") == 1


def test_subtask_without_conflict_keeps_all_deliverables(env):
    """无冲突时行为不变（回归：动态子任务仍能正常入板）。"""
    scheduler = _scheduler(env)
    child_id = scheduler._create_subtask({
        "title": "写工具模块", "role": "backend_dev",
        "deliverables": ["utils.py", "helpers.py"], "parent_id": "T-1",
    })
    assert child_id is not None
    assert [d.path for d in env["board"].get(child_id).deliverables] == [
        "utils.py", "helpers.py"]
    assert env["board"].get(child_id).dynamic is True


async def test_rejected_subtask_leaves_ledger_trace(env, tmp_path):
    """回调返回 None 时 Agent 记 subtask_rejected，不记 subtask_created。"""
    fx = tmp_path / "fx-reject"
    subtask_block = (
        '<subtask>\n'
        '{"title": "重复交付", "description": "d", "role": "backend_dev", '
        '"deliverables": ["a.py"]}\n'
        '</subtask>\n'
    )
    write_fixture(fx, subtask_block + GOOD_A)
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         subtask_creator=lambda s: None)
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["ledger"].count("subtask_rejected") == 1
    assert env["ledger"].count("subtask_created") == 0
