"""P2-2 运行时超时停滞检测。

与既有的「无就绪、无在跑」结构性死锁互补：这里测的是**有 worker 在跑但长时间
没有任何任务状态跃迁**的运行时停滞（worker 僵在某个永不返回的 await 上）。

覆盖：

- worker 僵死、超过 ``stall_timeout_seconds`` 无进展 → ``scheduler.stalled=True``；
- 任务正常推进（持续有完成事件）时即使阈值很小也不触发；
- ledger 记录 ``run_stalled``（含 stalled_for_seconds / active_tasks）；
- Crew 检测到 ``scheduler.stalled`` 后以退出码 4 收尾。
"""

import asyncio

import pytest

from agentcrew.agents.base_agent import BaseAgent
from agentcrew.agents.registry import AGENT_CLASSES, AgentFactory
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.crew.crew import EXIT_CODES, Crew
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

# 复用 test_crew 的端到端夹具（计划/评审/产物正文），避免重复一大段样板。
from tests.test_crew import (  # noqa: E402
    DELIVERY_BODY,
    MAIN_BODY,
    PLAN,
    PRD_BODY,
    REVIEWS,
    _ledger,
    _response,
)


class _HangingAgent(BaseAgent):
    """``execute`` 永远不返回 —— 模拟 worker 僵死（不抛错、不完成）。

    它会在 hard-cancel 时被 ``CancelledError`` 打断（``asyncio.Event.wait`` 是可
    取消的 await 点），与真实僵在 provider 调用里的 worker 同形。
    """

    async def execute(self, task, pack, *, fix_hints=()):  # noqa: D401
        await asyncio.Event().wait()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R", secrets=s.secret_values())
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=lg)
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "reviews.json").write_text(
        __import__("json").dumps({tid: _review_good() for tid in "AB"}),
        encoding="utf-8",
    )
    models = ModelFactory(s, ledger=lg, fixtures_dir=fx)
    catalog = RoleCatalog(BUILTIN_ROLES)
    agents = AgentFactory(models, store, lg, "目标", catalog)
    rev = Reviewer(models, catalog, store, lg)
    return dict(settings=s, ledger=lg, store=store, fx=fx, models=models,
                catalog=catalog, agents=agents, reviewer=rev, tmp_path=tmp_path)


def _review_good():
    return {"judgements": [{"criterion_index": 0, "satisfied": True,
                            "evidence": "ok"}],
            "suggestions": [], "score": 0.95}


def _mk(tid, deps=()):
    return Task(id=tid, title=tid, description=f"写 {tid}", role="backend_dev",
                acceptance_criteria=["ok"], dependencies=list(deps),
                deliverables=[ArtifactSpec(path=f"{tid}.py", kind="python",
                                           description="d")])


def _write_fx(fx, tid):
    d = fx / "responses" / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / "attempt-1.md").write_text(
        f"```python file={tid}.py\n# {tid}\nX = 1\n```\n", encoding="utf-8")


# ── Scheduler 级 ──

async def test_stall_triggers_when_worker_hangs(env):
    catalog = env["catalog"]
    catalog.register(RoleSpec(
        key="hang", title="hang", goal="g", backstory="b",
        responsibilities=["r"], boundaries=["b"],
        output_contract="c", system_prompt="s"))
    AGENT_CLASSES["hang"] = _HangingAgent
    try:
        # timeout_seconds 必须 > stall_timeout，否则 P1-4 任务级超时会先把它拦下来。
        task = Task(id="H1", title="h", description="d", role="hang",
                    acceptance_criteria=["ok"], deliverables=[],
                    timeout_seconds=60)
        board = TaskBoard([task])
        bus = ContextBus("g", board, env["store"])
        gk = Gatekeeper(env["reviewer"], env["store"], board, gate="none",
                        ledger=env["ledger"], prompter=NullPrompter())
        sch = Scheduler(board, env["agents"], gk, bus, ledger=env["ledger"],
                        stall_timeout_seconds=1)
        await asyncio.wait_for(sch.run(), timeout=15)
        assert sch.stalled is True
        events = list(env["ledger"].events("run_stalled"))
        assert events, "应记录 run_stalled 事件"
        assert events[-1].data["active_tasks"] == ["H1"]
        assert events[-1].data["stalled_for_seconds"] >= 1
    finally:
        AGENT_CLASSES.pop("hang", None)


async def test_no_stall_when_tasks_progress(env):
    """正常推进的任务即使 stall_timeout=1 也不触发停滞。"""
    _write_fx(env["fx"], "A")
    _write_fx(env["fx"], "B")
    board = TaskBoard([_mk("A"), _mk("B", ["A"])])
    bus = ContextBus("g", board, env["store"])
    gk = Gatekeeper(env["reviewer"], env["store"], board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    sch = Scheduler(board, env["agents"], gk, bus, ledger=env["ledger"],
                    stall_timeout_seconds=1)
    await asyncio.wait_for(sch.run(), timeout=15)
    assert sch.stalled is False
    assert board.get("A").status is S.SUCCEEDED
    assert board.get("B").status is S.SUCCEEDED


# ── Crew 级 ──

async def test_crew_maps_stalled_to_exit_code_4(tmp_path, monkeypatch):
    from agentcrew.crew import scheduler as scheduler_module

    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    fx = tmp_path / "fx"
    fx.mkdir()
    fx.joinpath("plan_todo.json").write_text(__import__("json").dumps(PLAN), encoding="utf-8")
    fx.joinpath("reviews.json").write_text(__import__("json").dumps(REVIEWS), encoding="utf-8")
    for tid, path, body in [
        ("T-001", "docs/PRD.md", PRD_BODY),
        ("T-002", "backend/app/main.py", MAIN_BODY),
        ("T-FINAL", "DELIVERY.md", DELIVERY_BODY),
    ]:
        d = fx / "responses" / tid
        d.mkdir(parents=True, exist_ok=True)
        d.joinpath("attempt-1.md").write_text(
            _response(path.split(".")[-1], path, body), encoding="utf-8")
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", fx)

    # 让调度器一跑就置停滞（模拟 P2-2 超时判定结果）。
    async def stalled_run(self):
        self._stalled = True

    monkeypatch.setattr(scheduler_module.Scheduler, "run", stalled_run)

    crew = Crew(Settings.load())
    rec = await crew.run("做一个 Todo 应用")
    assert crew.stalled is True
    finished = list(_ledger(Settings.load(), rec.run_id).events("run_finished"))
    assert finished[-1].data["exit_code"] == EXIT_CODES["stalled"] == 4
    # run_stalled 留痕（调度器或 run 级各一条均可）
    stalled_events = list(_ledger(Settings.load(), rec.run_id).events("run_stalled"))
    assert stalled_events
