"""槽位自锁守门测试（spec §7.3）。

背景：若把 G2 评审放进任务槽位内部，而评审又要走 LLM 调用级信号量，
在 max_concurrency=1 时调度器会自己等自己。这里用最小并发跑一个
【静态通过 → 必须走 G2】的任务来钉死这个回归。
"""
import asyncio
import json

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
from agentcrew.task.task import TaskStatus as S
from agentcrew.task.task_board import TaskBoard

APPROVE = {"judgements": [{"criterion_index": 0, "satisfied": True,
                           "evidence": "app.py 已定义 create_app"}],
           "suggestions": [], "score": 0.95}

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load().model_copy(update={"max_concurrency": 1})
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger)

    fx = tmp_path / "fx"
    (fx / "responses" / "T-1").mkdir(parents=True)
    # 产物必须通过 G1（含 must_contain），才会走到 G2
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=app.py\ndef create_app():\n    return None\n```\n",
        encoding="utf-8")
    # 评审夹具按 task_id 查找：判定条数按任务而定，兜底键 _default 不再可用
    (fx / "reviews.json").write_text(
        json.dumps({"T-1": APPROVE}), encoding="utf-8")

    models = ModelFactory(settings, ledger=ledger, fixtures_dir=fx)
    catalog = RoleCatalog(BUILTIN_ROLES)
    task = Task(id="T-1", title="t", description="d", role="backend_dev",
                acceptance_criteria=["可运行"],
                deliverables=[ArtifactSpec(path="app.py", kind="python",
                                           description="d",
                                           must_contain=["def create_app"])])
    board = TaskBoard([task])
    gatekeeper = Gatekeeper(Reviewer(models, catalog, store, ledger), store, board,
                            gate="none", ledger=ledger, prompter=NullPrompter())
    scheduler = Scheduler(board, AgentFactory(models, store, ledger, "g", catalog),
                          gatekeeper, ContextBus("g", board, store),
                          ledger=ledger, max_concurrency=1)
    return dict(settings=settings, ledger=ledger, board=board, scheduler=scheduler)


async def test_single_slot_completes_without_deadlock(env):
    """max_concurrency=1 且任务必须过 G2 —— 不得死锁。"""
    await asyncio.wait_for(env["scheduler"].run(), timeout=5)
    assert env["board"].get("T-1").status is S.SUCCEEDED


async def test_g2_actually_ran(env):
    """确认 G2 确实被调用过，否则上面的用例只是空跑。"""
    await asyncio.wait_for(env["scheduler"].run(), timeout=5)
    assert any(e.data.get("layer") == "llm" for e in env["ledger"].events("gate_result"))


async def test_peak_concurrency_never_exceeds_one(env):
    await asyncio.wait_for(env["scheduler"].run(), timeout=5)
    assert env["scheduler"].peak_concurrency <= 1
