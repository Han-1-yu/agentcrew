"""R4 Flow Runtime：持久化审批 + 条件分支的核心链路测试。

覆盖：审批单三态机（create/decide/consume、幂等、坏行宽容、跨进程读写）、
DeferredPrompter 的挂起/消费语义、结果标签解析与条件边判定、TaskBoard 的
分支传播与汇合、Crew 计划卡点/交付卡点挂起后 resume 从原卡点继续、以及
「只有命中分支执行、未命中分支预期跳过、run 仍 COMPLETED」的端到端样例。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.gate.deferred import DeferredPrompter
from agentcrew.runtime.approvals import (
    ApprovalError,
    ApprovalPending,
    ApprovalStore,
)
from agentcrew.runtime.checkpoint import RunStatus
from agentcrew.task.conditions import (
    CONDITION_SKIP_PROPAGATED,
    CONDITION_SKIP_REASON,
    condition_matches,
    is_conditional_skip,
    normalize_outcome,
    parse_outcome,
)
from agentcrew.task.task import Task, TaskResult, TaskStatus
from agentcrew.task.task_board import TaskBoard


def _task(
    task_id: str,
    *,
    deps: list[str] | None = None,
    when: list[str] | None = None,
    status: TaskStatus = TaskStatus.PENDING,
    outcome: str | None = None,
) -> Task:
    result = None
    if outcome is not None:
        result = TaskResult(
            task_id=task_id, role="backend_dev", summary="s",
            artifacts=[], outcome=outcome,
            started_at=datetime.now(),
            finished_at=datetime.now(),
            duration_ms=1,
        )
    return Task(
        id=task_id, title=task_id, description="d", role="backend_dev",
        acceptance_criteria=["a"], deliverables=[],
        dependencies=deps or [], when_outcomes=when or [],
        status=status, result=result,
    )


# ── 审批单存储 ─────────────────────────────────────────────────────────

def test_approval_store_three_state_machine(tmp_path: Path):
    store = ApprovalStore(tmp_path / "R-1")
    assert store.all() == []
    req = store.create("plan", context={"task_ids": ["T-001"]})
    assert req.status == "pending" and req.checkpoint == "plan"
    assert store.pending() == [req]
    assert store.awaiting_checkpoint() == "plan"

    # 跨进程：用另一个实例写裁决
    other = ApprovalStore(tmp_path / "R-1")
    decided = other.decide(req.id, {"action": "approve"}, decided_by="cli")
    assert decided.status == "decided" and decided.decision["action"] == "approve"
    assert store.get(req.id).status == "decided"
    with pytest.raises(ApprovalError):
        other.decide(req.id, {"action": "approve"})  # 幂等：不能重复裁决

    consumed = store.consume(req.id)
    assert consumed.status == "consumed"
    assert store.consume(req.id).status == "consumed"  # 消费也幂等
    assert store.awaiting_checkpoint() is None
    assert store.pending() == []


def test_approval_store_rejects_bad_decisions(tmp_path: Path):
    store = ApprovalStore(tmp_path / "R-2")
    plan = store.create("plan")
    delivery = store.create("delivery")
    with pytest.raises(ApprovalError):
        store.decide(plan.id, {"action": "accept"})  # 交付侧动作不能用在计划侧
    with pytest.raises(ApprovalError):
        store.decide("AP-missing", {"action": "approve"})
    with pytest.raises(ApprovalError):
        store.decide(delivery.id, {"action": "rework"})  # 缺 task_ids
    with pytest.raises(ApprovalError):
        store.decide(delivery.id, {"action": "rework", "task_ids": []})
    assert store.get(delivery.id).status == "pending"


def test_approval_store_tolerates_broken_lines(tmp_path: Path):
    store = ApprovalStore(tmp_path / "R-3")
    req = store.create("delivery")
    with open(store.path, "a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")
        handle.write('{"op": "unknown", "id": "AP-x"}\n')
    assert [item.id for item in store.all()] == [req.id]


# ── DeferredPrompter ───────────────────────────────────────────────────

async def test_deferred_prompter_creates_request_then_consumes_decision(
    tmp_path: Path,
):
    prompter = DeferredPrompter(tmp_path / "R-4")
    with pytest.raises(ApprovalPending) as first:
        await prompter.ask_plan([])
    request = first.value.request
    assert request.checkpoint == "plan"

    # 仍 pending：复用同一张单，不重复创建
    with pytest.raises(ApprovalPending) as again:
        await prompter.ask_plan([])
    assert again.value.request.id == request.id
    assert len(prompter.store.all()) == 1

    prompter.store.decide(request.id, {"action": "approve"}, decided_by="web")
    decision = await prompter.ask_plan([])
    assert decision.action == "approve"
    assert prompter.store.get(request.id).status == "consumed"


async def test_deferred_prompter_delivery_rework_payload(tmp_path: Path):
    prompter = DeferredPrompter(tmp_path / "R-5")
    with pytest.raises(ApprovalPending) as caught:
        await prompter.ask_delivery(None)
    prompter.store.decide(
        caught.value.request.id,
        {"action": "rework", "task_ids": ["T-001", "T-002"]},
    )
    decision = await prompter.ask_delivery(None)
    assert decision.action == "rework"
    assert decision.task_ids == ["T-001", "T-002"]


# ── 结果标签与条件边纯语义 ─────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("approved", "approved"), ("  AUTH-REQUIRED ", "auth-required"),
    ("", None), ("a b", None), ("<script>", None), ("x" * 100, None), (7, None),
])
def test_normalize_outcome(raw, expected):
    assert normalize_outcome(raw) == expected


def test_parse_outcome_takes_last_valid_tag():
    assert parse_outcome("正文 <outcome>no-auth</outcome> 后文") == "no-auth"
    assert parse_outcome(
        "<outcome>first</outcome>\n<outcome>AUTH_REQUIRED</outcome>"
    ) == "auth_required"
    assert parse_outcome("没有标签") is None
    assert parse_outcome("<outcome>not valid!</outcome>") is None


def test_condition_matches():
    hit = _task("B", deps=["A"], when=["auth-required"])
    assert condition_matches(hit, ["auth-required"]) is True
    assert condition_matches(hit, ["no-auth"]) is False
    assert condition_matches(hit, [None]) is False
    assert condition_matches(_task("C"), [None]) is True  # 无条件任务恒通过


# ── R31：否定边与兜底边 ────────────────────────────────────────────────

def test_negated_condition_hits_when_label_absent():
    negated = _task("B", deps=["A"], when=["!auth-required"])
    assert condition_matches(negated, ["no-auth"]) is True
    assert condition_matches(negated, ["auth-required"]) is False
    assert condition_matches(negated, [None]) is True   # 上游没声明 → 不等于该标签


def test_multiple_negations_are_all_required():
    """``!a`` + ``!b`` = 既不是 a 也不是 b（否定项之间是 AND）。"""
    task = _task("B", deps=["A"], when=["!a", "!b"])
    assert condition_matches(task, ["c"]) is True
    assert condition_matches(task, ["a"]) is False
    assert condition_matches(task, ["b"]) is False


def test_positive_and_negated_are_or_combined():
    task = _task("B", deps=["A"], when=["ok", "!failed"])
    assert condition_matches(task, ["ok"]) is True
    assert condition_matches(task, ["other"]) is True    # 命中否定项
    assert condition_matches(task, ["failed"]) is False  # 两条件都不成立


def test_fallback_label_matches_any_upstream():
    task = _task("B", deps=["A"], when=["*"])
    assert condition_matches(task, ["anything"]) is True
    assert condition_matches(task, [None]) is True
    assert condition_matches(task, []) is False          # 没有上游 = 兜底也不成立


def test_condition_validation_accepts_negation_and_rejects_bare_prefix():
    from agentcrew.crew.crew import build_catalog
    from agentcrew.task.planner import PlanValidationError, validate_plan

    catalog = build_catalog(Settings.load())
    upstream = _task("A")
    ok = _task("B", deps=["A"], when=["!failed"])
    validate_plan([upstream, ok], catalog)               # 不抛 = 接受

    bad = _task("C", deps=["A"], when=["!"])
    with pytest.raises(PlanValidationError, match="非法条件"):
        validate_plan([upstream, bad], catalog)


# ── TaskBoard：分支传播与汇合 ──────────────────────────────────────────

def test_condition_branch_only_runs_matching_tasks():
    """A→(B|auth-required, C|no-auth)→J 汇合；只有 B 应就绪，C 预期跳过。"""
    board = TaskBoard([
        _task("A", status=TaskStatus.SUCCEEDED, outcome="auth-required"),
        _task("B", deps=["A"], when=["auth-required"]),
        _task("C", deps=["A"], when=["no-auth"]),
        _task("J", deps=["B", "C"]),
    ])
    board.refresh()
    assert board.get("B").status is TaskStatus.READY
    assert board.get("C").status is TaskStatus.SKIPPED
    assert is_conditional_skip(board.get("C")) is True
    assert board.get("C").status_history[-1].reason == CONDITION_SKIP_REASON
    # J 还在等 B 完成（此时 C 已条件性跳过但 B 未 SUCCEEDED）
    assert board.get("J").status is TaskStatus.PENDING

    # 走状态机合法链路 READY → RUNNING → REVIEW → SUCCEEDED
    for state in (TaskStatus.RUNNING, TaskStatus.REVIEW, TaskStatus.SUCCEEDED):
        board.transition("B", state, reason="test")
    board.refresh()
    assert board.get("J").status is TaskStatus.READY, (
        "汇合节点只要有一条分支成功就应继续"
    )


def test_unmatched_branch_propagates_conditional_skip():
    """未命中分支的下游整条传播，且 reason 保持「预期跳过」。"""
    board = TaskBoard([
        _task("A", status=TaskStatus.SUCCEEDED, outcome="no-auth"),
        _task("B", deps=["A"], when=["auth-required"]),
        _task("B2", deps=["B"]),
    ])
    board.refresh()
    assert board.get("B").status_history[-1].reason == CONDITION_SKIP_REASON
    board.refresh()  # 多级传播由下一轮收敛（与既有语义一致）
    assert board.get("B2").status is TaskStatus.SKIPPED
    assert board.get("B2").status_history[-1].reason == CONDITION_SKIP_PROPAGATED
    assert is_conditional_skip(board.get("B2")) is True


def test_legacy_failed_upstream_skip_is_not_conditional():
    board = TaskBoard([
        _task("A", status=TaskStatus.SKIPPED),
        _task("B", deps=["A"]),
    ])
    board.refresh()
    assert board.get("B").status is TaskStatus.SKIPPED
    assert is_conditional_skip(board.get("B")) is False


def test_condition_not_decided_while_upstream_running():
    board = TaskBoard([
        _task("A", status=TaskStatus.RUNNING),
        _task("B", deps=["A"], when=["auth-required"]),
    ])
    board.refresh()
    assert board.get("B").status is TaskStatus.PENDING, "上游在跑时不能提前判死分支"


# ── Crew 端到端：审批挂起 → 跨进程决策 → resume ─────────────────────────

_PLAN = {
    "tasks": [
        {
            "id": "T-001", "title": "需求", "description": "写 PRD",
            "role": "product_manager", "acceptance_criteria": ["含功能清单"],
            "dependencies": [], "priority": 1, "brief": "PRD",
            "deliverables": [{
                "path": "docs/PRD.md", "kind": "markdown", "description": "PRD",
                "must_contain": ["#"],
            }],
        },
        {
            "id": "T-002", "title": "后端", "description": "写后端",
            "role": "backend_dev", "acceptance_criteria": ["有 API"],
            "dependencies": ["T-001"], "priority": 3, "brief": "后端实现",
            "deliverables": [{
                "path": "backend/app/main.py", "kind": "python",
                "description": "入口", "must_contain": ["def create_app"],
            }],
        },
    ]
}
_PRD = "# PRD\n\n## 功能清单\n- 新增待办 (add)\n- 删除待办 (delete)\n\n## 验收标准\n- AC-01: 新增后列表包含该条目\n"
_MAIN = "def create_app():\n    return None\n"
_DELIVERY = (
    "# 交付总结\n\n## 完成\n- T-001 产品需求文档已交付并通过两层门禁验收\n"
    "- T-002 后端入口已交付并通过两层门禁验收\n- 全部 3 个任务均无遗留阻塞\n\n"
    "## 运行\nuvicorn app.main:app --reload\n"
)


def _review(count: int) -> dict:
    return {
        "judgements": [
            {"criterion_index": i, "satisfied": True, "evidence": "已满足"}
            for i in range(count)
        ],
        "suggestions": [], "score": 0.9,
    }


@pytest.fixture
def flow_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "plan_todo.json").write_text(json.dumps(_PLAN), encoding="utf-8")
    (fx / "reviews.json").write_text(json.dumps({
        "T-001": _review(1), "T-002": _review(1), "T-FINAL": _review(2),
    }), encoding="utf-8")
    for tid, path, body in [
        ("T-001", "docs/PRD.md", _PRD),
        ("T-002", "backend/app/main.py", _MAIN),
        ("T-FINAL", "DELIVERY.md", _DELIVERY),
    ]:
        folder = fx / "responses" / tid
        folder.mkdir(parents=True, exist_ok=True)
        lang = path.rsplit(".", 1)[-1]
        (folder / "attempt-1.md").write_text(
            f"```{lang} file={path}\n{body}```\n", encoding="utf-8"
        )
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", fx)
    return {"settings": Settings.load(), "tmp_path": tmp_path, "fx": fx}


def _run_dir(env) -> Path:
    """输出目录下唯一的 run 目录（名字形如 ``20261107-093012-xxxx``）。

    不能用 ``next(iterdir())``：经验库 / 实体记忆会在 output 下建
    ``knowledge/`` 子目录（R3），Linux 上目录枚举顺序不定，拿到它就会
    FileNotFoundError（CI 3.10 首跑的真实失败）。按 run-id 模式过滤 + 排序，
    跨平台跨版本确定性。
    """
    candidates = sorted(
        path for path in env["settings"].output_dir.iterdir()
        if path.is_dir() and re.match(r"^\d{8}-\d{6}-", path.name)
    )
    assert candidates, "输出目录下没有 run 目录（run 未落盘？）"
    return candidates[0]


async def test_plan_gate_suspends_and_resumes_from_same_checkpoint(flow_env):
    settings = flow_env["settings"].model_copy(update={"gate": "plan"})
    record = await Crew(settings, prompter=DeferredPrompter()).run("做一个 Todo 应用")
    assert record.status is RunStatus.AWAITING_APPROVAL
    run_dir = _run_dir(flow_env)
    assert (run_dir / "tasks.json").is_file(), "挂起时面板必须落盘，否则 resume 无板可续"
    assert (run_dir / "approvals.jsonl").is_file()

    store = ApprovalStore(run_dir)
    requests = store.all()
    assert len(requests) == 1 and requests[0].checkpoint == "plan"
    assert requests[0].status == "pending"
    ledger = (run_dir / "ledger.jsonl").read_text(encoding="utf-8")
    assert "run_paused" in ledger and "approval_requested" in ledger
    assert "run_finished" not in ledger, "挂起不是终局，不能发 run_finished"

    # 另一个「进程」：新 store 实例写裁决
    ApprovalStore(run_dir).decide(
        requests[0].id, {"action": "approve"}, decided_by="test"
    )
    resumed = Crew.resume_from(
        settings, record.run_id, prompter=DeferredPrompter()
    )
    final = await resumed.resume()
    assert final.status is RunStatus.COMPLETED
    assert ApprovalStore(run_dir).get(requests[0].id).status == "consumed"
    assert "run_finished" in (run_dir / "ledger.jsonl").read_text(encoding="utf-8")


async def test_delivery_gate_suspends_and_resumes(flow_env):
    settings = flow_env["settings"].model_copy(update={"gate": "delivery"})
    record = await Crew(settings, prompter=DeferredPrompter()).run("做一个 Todo 应用")
    assert record.status is RunStatus.AWAITING_APPROVAL
    run_dir = _run_dir(flow_env)
    store = ApprovalStore(run_dir)
    request = store.all()[-1]
    assert request.checkpoint == "delivery"
    store.decide(request.id, {"action": "accept"}, decided_by="test")

    final = await Crew.resume_from(
        settings, record.run_id, prompter=DeferredPrompter()
    ).resume()
    assert final.status is RunStatus.COMPLETED


async def test_resume_without_decision_hangs_again_without_new_request(flow_env):
    settings = flow_env["settings"].model_copy(update={"gate": "plan"})
    record = await Crew(settings, prompter=DeferredPrompter()).run("做一个 Todo 应用")
    run_dir = _run_dir(flow_env)
    again = await Crew.resume_from(
        settings, record.run_id, prompter=DeferredPrompter()
    ).resume()
    assert again.status is RunStatus.AWAITING_APPROVAL
    assert len(ApprovalStore(run_dir).all()) == 1, "未决时不得重复建单"


async def test_interrupted_resume_keeps_legacy_semantics(flow_env):
    """R4 不能改变 §13.3：INTERRUPTED 的 resume 依旧跳过规划与计划卡点。"""
    settings = flow_env["settings"].model_copy(update={"gate": "plan"})
    # 用 save_exit 造一个 INTERRUPTED，再 resume；不得产生任何审批单。
    from agentcrew.gate.gatekeeper import PlanDecision

    class _SaveExit:
        async def ask_plan(self, tasks):
            return PlanDecision(action="save_exit")

        async def ask_delivery(self, record):
            from agentcrew.gate.gatekeeper import DeliveryDecision
            return DeliveryDecision(action="accept")

    from agentcrew.crew.crew import RunCancelled
    with pytest.raises(RunCancelled):
        await Crew(settings, prompter=_SaveExit()).run("做一个 Todo 应用")
    run_dir = _run_dir(flow_env)
    assert not (run_dir / "approvals.jsonl").exists()
    final = await Crew.resume_from(
        settings, next(flow_env["settings"].output_dir.iterdir()).name,
        prompter=DeferredPrompter(),
    ).resume()
    assert final.status is RunStatus.COMPLETED


async def test_conditional_branch_end_to_end(flow_env):
    """T-001 声明 auth-required → 只跑 T-002 分支，T-003 预期跳过，run 成功。"""
    plan = json.loads((flow_env["fx"] / "plan_todo.json").read_text(encoding="utf-8"))
    plan["tasks"][1]["when_outcomes"] = ["auth-required"]
    plan["tasks"][1]["deliverables"] = [{
        "path": "backend/auth.py", "kind": "python", "description": "鉴权",
        "must_contain": ["def auth"],
    }]
    plan["tasks"].append({
        "id": "T-003", "title": "无鉴权分支", "description": "不需要鉴权时实现",
        "role": "backend_dev", "acceptance_criteria": ["有实现"],
        "dependencies": ["T-001"], "priority": 6, "brief": "无鉴权实现",
        "when_outcomes": ["no-auth"],
        "deliverables": [{
            "path": "backend/noauth.py", "kind": "python", "description": "无鉴权",
            "must_contain": ["def noauth"],
        }],
    })
    (flow_env["fx"] / "plan_todo.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )
    # T-001 的回复带上结果标签；T-002 换一条能过 G1 的产物
    (flow_env["fx"] / "responses" / "T-001" / "attempt-1.md").write_text(
        f"```markdown file=docs/PRD.md\n{_PRD}```\n"
        "<outcome>auth-required</outcome>\n",
        encoding="utf-8",
    )
    (flow_env["fx"] / "responses" / "T-002" / "attempt-1.md").write_text(
        "```python file=backend/auth.py\ndef auth():\n    return True\n```\n",
        encoding="utf-8",
    )
    (flow_env["fx"] / "reviews.json").write_text(json.dumps({
        "T-001": _review(1), "T-002": _review(1),
        "T-003": _review(1), "T-FINAL": _review(2),
    }), encoding="utf-8")

    record = await Crew(flow_env["settings"]).run("做一个 Todo 应用")
    assert record.status is RunStatus.COMPLETED, record.status
    run_dir = _run_dir(flow_env)
    tasks = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    by_id = {item["id"]: item for item in tasks}
    assert by_id["T-001"]["result"]["outcome"] == "auth-required"
    assert by_id["T-002"]["status"] == "succeeded"
    assert by_id["T-003"]["status"] == "skipped"
    assert by_id["T-003"]["status_history"][-1]["reason"] == CONDITION_SKIP_REASON
    assert by_id["T-FINAL"]["status"] == "succeeded", "汇合任务必须正常完成"
    assert not (run_dir / "artifacts" / "backend" / "noauth.py").exists()
