"""R20：单步/断点调试——``debug_step`` 开启时每个任务派发前挂起。

R4 的「卡点挂起 → 跨进程恢复」只覆盖 plan/delivery 两个**阶段边界**；本轮把
同一套审批纪律搬到**任务粒度**：调度器派发前过 ``StepGate``，无未放行候选就
建 ``step`` 审批单挂起；``approve --decision step|run|abort`` + ``resume`` 逐个
放行、解除断点或取消。

覆盖四层：

- 审批存储层：step 卡点的裁决形状校验与 ``consumed_steps()`` 汇总；
- 面板层：``peek_ready`` 是 ``take_ready`` 的只读版（不改状态、不扣额度、
  同一条候选顺序）；
- 断点门：``StepGate`` 建单 / 复用 pending 单 / 放行已消费任务；
- Crew 端到端：挂起 → step 逐个放行 → run 解除断点跑到终局；abort 取消；
  resume 不重供开关也能从快照恢复断点（断点属于 run，不属于进程）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew, RunCancelled
from agentcrew.gate.stepper import StepGate
from agentcrew.runtime.approvals import (
    ApprovalError,
    ApprovalPending,
    ApprovalStore,
)
from agentcrew.runtime.checkpoint import RunStatus
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskStatus
from agentcrew.task.task_board import TaskBoard

# ── 夹具：与 test_crew.py 同一套 mock 骨架（两任务 + T-FINAL） ─────────────

PLAN = {
    "tasks": [
        {
            "id": "T-001", "title": "需求", "description": "写 PRD",
            "role": "product_manager",
            "acceptance_criteria": ["含功能清单"],
            "dependencies": [], "priority": 1, "brief": "PRD",
            "deliverables": [{"path": "docs/PRD.md", "kind": "markdown",
                              "description": "PRD", "must_contain": ["#"]}],
        },
        {
            "id": "T-002", "title": "后端", "description": "写后端",
            "role": "backend_dev", "acceptance_criteria": ["有 API"],
            "dependencies": ["T-001"], "priority": 3, "brief": "后端实现",
            "deliverables": [{"path": "backend/app/main.py", "kind": "python",
                              "description": "入口",
                              "must_contain": ["def create_app"]}],
        },
    ]
}

PRD_BODY = (
    "# PRD\n\n## 功能清单\n- 新增待办 (add)\n- 删除待办 (delete)\n\n"
    "## 验收标准\n- AC-01: 新增后列表包含该条目\n"
)
MAIN_BODY = "def create_app():\n    return None\n"
DELIVERY_BODY = (
    "# 交付总结\n\n## 完成\n"
    "- T-001 产品需求文档已交付并通过验收\n"
    "- T-002 后端入口已交付并通过验收\n"
    "- 全部任务无遗留阻塞\n\n## 运行\nuvicorn app.main:app --reload\n"
)


def _review(count: int) -> dict:
    return {
        "judgements": [
            {"criterion_index": i, "satisfied": True,
             "evidence": f"产物已满足第 {i + 1} 条验收标准"}
            for i in range(count)
        ],
        "suggestions": [], "score": 0.9,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "plan_todo.json").write_text(json.dumps(PLAN), encoding="utf-8")
    (fx / "reviews.json").write_text(json.dumps({
        "T-001": _review(1), "T-002": _review(1), "T-FINAL": _review(2),
    }), encoding="utf-8")
    for tid, path, body in [
        ("T-001", "docs/PRD.md", PRD_BODY),
        ("T-002", "backend/app/main.py", MAIN_BODY),
        ("T-FINAL", "DELIVERY.md", DELIVERY_BODY),
    ]:
        d = fx / "responses" / tid
        d.mkdir(parents=True, exist_ok=True)
        lang = path.rsplit(".", 1)[-1]
        (d / "attempt-1.md").write_text(
            f"```{lang} file={path}\n{body}```\n", encoding="utf-8")
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", fx)
    return {"settings": Settings.load(), "tmp_path": tmp_path}


def _step_requests(run_dir: Path) -> list:
    return [r for r in ApprovalStore(run_dir).all()
            if r.checkpoint == "step"]


# ── 审批存储层 ─────────────────────────────────────────────────────────────


def test_step_checkpoint_rejects_foreign_actions(tmp_path):
    """plan 的 approve / delivery 的 accept / rework 都不许写进 step 卡点。"""
    store = ApprovalStore(tmp_path / "run")
    request = store.create("step", context={"task_id": "T-1"})
    for bad in ("approve", "cancel", "save_exit", "accept", "rework"):
        with pytest.raises(ApprovalError):
            store.decide(request.id, {"action": bad}, decided_by="t")


@pytest.mark.parametrize("action", ["step", "run", "abort"])
def test_step_checkpoint_accepts_own_actions(tmp_path, action):
    store = ApprovalStore(tmp_path / "run")
    request = store.create("step", context={"task_id": "T-1"})
    store.decide(request.id, {"action": action}, decided_by="t")
    assert store.get(request.id).decision["action"] == action


def test_consumed_steps_aggregates_ids_and_final_action(tmp_path):
    store = ApprovalStore(tmp_path / "run")
    assert store.consumed_steps() == (frozenset(), None)

    first = store.create("step", context={"task_id": "T-1"})
    store.decide(first.id, {"action": "step"}, decided_by="t")
    store.consume(first.id)
    ids, final = store.consumed_steps()
    assert ids == frozenset({"T-1"}) and final is None

    second = store.create("step", context={"task_id": "T-2"})
    store.decide(second.id, {"action": "run"}, decided_by="t")
    store.consume(second.id)
    ids, final = store.consumed_steps()
    assert ids == frozenset({"T-1", "T-2"}) and final == "run"


def test_consumed_steps_ignores_unconsumed_decisions(tmp_path):
    """裁决必须先被引擎消费才生效——decided 未消费不算（与 plan/delivery 同纪律）。"""
    store = ApprovalStore(tmp_path / "run")
    request = store.create("step", context={"task_id": "T-1"})
    store.decide(request.id, {"action": "run"}, decided_by="t")
    assert store.consumed_steps() == (frozenset(), None)


# ── 面板层：peek_ready 只读 ────────────────────────────────────────────────


def _board_with(ids: list[str]) -> TaskBoard:
    tasks = [
        Task(id=tid, title=tid, description="d", role="backend_dev",
             acceptance_criteria=["a"], deliverables=[], priority=5)
        for tid in ids
    ]
    return TaskBoard(tasks)


def test_peek_ready_is_read_only_mirror_of_take_ready():
    board = _board_with(["T-2", "T-1"])
    peeked = board.peek_ready(limit=10)
    # 顺序与 take_ready 完全一致（(priority, id) 升序），且只读
    assert [task.id for task in peeked] == ["T-1", "T-2"]
    assert all(task.status is TaskStatus.READY for task in peeked)
    assert all(task.attempts == 0 for task in peeked)
    # take_ready 之后才是真正的跃迁
    taken = board.take_ready(limit=10)
    assert [task.id for task in taken] == ["T-1", "T-2"]
    assert all(task.status is TaskStatus.RUNNING for task in taken)
    assert board.get("T-1").attempts == 1


def test_peek_ready_respects_limit():
    board = _board_with(["T-1", "T-2", "T-3"])
    assert [task.id for task in board.peek_ready(limit=2)] == ["T-1", "T-2"]
    # 被 limit 截断的任务保持原状态
    assert board.get("T-3").status is TaskStatus.READY


# ── 断点门：StepGate ───────────────────────────────────────────────────────


def _task(tid: str) -> Task:
    return Task(id=tid, title=tid, description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=[], priority=5)


def test_step_gate_creates_request_for_first_unconsumed(tmp_path):
    store = ApprovalStore(tmp_path / "run")
    gate = StepGate(store, frozenset())
    with pytest.raises(ApprovalPending) as excinfo:
        gate([_task("T-1"), _task("T-2")])
    request = excinfo.value.request
    assert request.checkpoint == "step"
    assert request.context["task_id"] == "T-1"   # 派发顺序里的第一个


def test_step_gate_reuses_pending_request_without_duplicates(tmp_path):
    """用户没裁决就 resume 是普通操作：复用同一张单，不重复建单。"""
    store = ApprovalStore(tmp_path / "run")
    gate = StepGate(store, frozenset())
    with pytest.raises(ApprovalPending) as first:
        gate([_task("T-1")])
    with pytest.raises(ApprovalPending) as second:
        gate([_task("T-1")])
    assert first.value.request.id == second.value.request.id


def test_step_gate_passes_when_all_candidates_consumed(tmp_path):
    store = ApprovalStore(tmp_path / "run")
    gate = StepGate(store, frozenset({"T-1", "T-2"}))
    gate([_task("T-1"), _task("T-2")])   # 不抛即放行


def test_step_gate_pauses_next_unconsumed_after_approval(tmp_path):
    store = ApprovalStore(tmp_path / "run")
    gate = StepGate(store, frozenset({"T-1"}))
    with pytest.raises(ApprovalPending) as excinfo:
        gate([_task("T-1"), _task("T-2")])
    assert excinfo.value.request.context["task_id"] == "T-2"


# ── Crew 端到端 ────────────────────────────────────────────────────────────


async def test_debug_step_suspends_before_first_dispatch(env):
    settings = Settings.load(debug_step=True)
    crew = Crew(settings)
    record = await crew.run("做一个 Todo 应用")
    run_dir = crew.checkpoint.run_dir

    assert record.status is RunStatus.AWAITING_APPROVAL
    steps = _step_requests(run_dir)
    assert len(steps) == 1 and steps[0].status == "pending"
    # 断点落在**派发之前**：T-001 还是 READY，一次尝试都没扣
    board = crew.checkpoint.load_board()
    assert board.get("T-001").status is TaskStatus.READY
    assert board.get("T-001").attempts == 0
    # 审批上下文足够人做裁决
    assert steps[0].context["task_id"] == "T-001"
    assert steps[0].context["role"] == "product_manager"
    # run_paused 事件带着 step 卡点名（订阅方按它过滤）
    paused = list(Ledger(run_dir / "ledger.jsonl", run_id=record.run_id)
                  .events("run_paused"))
    assert paused[-1].data["checkpoint"] == "step"


async def test_debug_step_walks_one_task_per_approval(env):
    """step 放行 T-001 → resume → T-001 跑完 → 在 T-002 前再次挂起。"""
    settings = Settings.load(debug_step=True)
    crew = Crew(settings)
    record = await crew.run("做一个 Todo 应用")
    run_id = record.run_id
    run_dir = crew.checkpoint.run_dir
    store = ApprovalStore(run_dir)

    store.decide(_step_requests(run_dir)[0].id, {"action": "step"},
                 decided_by="tester")
    resumed = Crew.resume_from(Settings.load(), run_id)
    record = await resumed.resume()

    assert record.status is RunStatus.AWAITING_APPROVAL
    steps = _step_requests(run_dir)
    assert len(steps) == 2
    assert steps[1].context["task_id"] == "T-002"
    # T-001 已真实执行并通过门禁
    board = resumed.checkpoint.load_board()
    assert board.get("T-001").status is TaskStatus.SUCCEEDED


async def test_debug_step_run_decision_finishes_without_more_pauses(env):
    """run 裁决解除断点：resume 后一路跑到 COMPLETED，不再有任何新断点。"""
    settings = Settings.load(debug_step=True)
    crew = Crew(settings)
    record = await crew.run("做一个 Todo 应用")
    run_id = record.run_id
    run_dir = crew.checkpoint.run_dir
    store = ApprovalStore(run_dir)

    store.decide(_step_requests(run_dir)[0].id, {"action": "step"},
                 decided_by="tester")
    resumed = Crew.resume_from(Settings.load(), run_id)
    await resumed.resume()
    store.decide(_step_requests(run_dir)[1].id, {"action": "run"},
                 decided_by="tester")

    final = await Crew.resume_from(Settings.load(), run_id).resume()
    assert final.status is RunStatus.COMPLETED
    assert final.stats.succeeded == 3
    assert len(_step_requests(run_dir)) == 2   # run 之后没有新断点


async def test_debug_step_abort_cancels_run(env):
    settings = Settings.load(debug_step=True)
    crew = Crew(settings)
    record = await crew.run("做一个 Todo 应用")
    run_id = record.run_id
    run_dir = crew.checkpoint.run_dir

    ApprovalStore(run_dir).decide(
        _step_requests(run_dir)[0].id, {"action": "abort"}, decided_by="tester")
    resumed = Crew.resume_from(Settings.load(), run_id)
    with pytest.raises(RunCancelled):
        await resumed.resume()
    assert resumed.checkpoint.load_record().status is RunStatus.CANCELLED


async def test_debug_step_survives_resume_without_flag(env):
    """断点属于 run 不属于进程：resume 不重供 --debug-step 也继续逐任务停。"""
    settings = Settings.load(debug_step=True)
    crew = Crew(settings)
    record = await crew.run("做一个 Todo 应用")
    run_id = record.run_id
    run_dir = crew.checkpoint.run_dir

    ApprovalStore(run_dir).decide(
        _step_requests(run_dir)[0].id, {"action": "step"}, decided_by="tester")
    # Settings.load() 未带 debug_step（快照里有）——断点必须仍然生效
    plain = Settings.load()
    assert plain.debug_step is False
    resumed = Crew.resume_from(plain, run_id)
    assert resumed._settings.debug_step is True
    record = await resumed.resume()
    assert record.status is RunStatus.AWAITING_APPROVAL
    assert len(_step_requests(run_dir)) == 2
