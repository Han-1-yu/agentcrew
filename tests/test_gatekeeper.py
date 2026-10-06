"""G3 门禁编排：短路、状态跃迁与返工额度（spec §10、§6.2、§11.3）。

brief 用例原样保留；在其之上补五件事：

- **返工回路有界**：Task 8 的 carry-forward —— ``bump_rework`` 在本次之前全仓
  没有调用者。若 ``REVIEW → REWORK`` 不配对自增 ``rework_count``，``max_reworks``
  永不生效，而 ``REWORK → RUNNING`` 又不消耗 ``attempts``，返工就是无界循环。
  用一个任务被驳回 ``max_reworks + 1`` 次后必须 ``FAILED`` 把这条钉死。
- **短路可观测**：G1 驳回时 LLM 一次都没被调用（无 ``llm_call``、无
  ``layer="llm"`` 的 ``gate_result``）。
- **prompter 边界**：``gate="none"`` 绝不触碰 prompter（用一碰就抛的替身），
  而 ``plan`` / ``delivery`` / ``both`` 的开启矩阵与裁决透传各自钉一个用例。
- **fix_hints 存活**：G1 的 ``fix_hints`` 一路活到 ``GateDecision``，并在返工
  时沉淀为 ``ContextBus`` 的决策记录（spec §11.3）—— 该记录要求任务
  ``rework_count >= 1``，因此它同时钉死「先 bump 再记录」的顺序。
- **账目字段**：``gate_result`` 携带 spec §14.1 的 ``layer``/``verdict``/
  ``score``/``issues``；状态跃迁的 ``reason`` 用 ``TaskStatus.value``，
  不是枚举的 ``str()``（Python 3.12 会渲染成 ``TaskStatus.REWORK``）。
"""

import json

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.gate.gatekeeper import (
    DeliveryDecision,
    Gatekeeper,
    NullPrompter,
    PlanDecision,
)
from agentcrew.gate.reviewer import Reviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task import TaskStatus as S
from agentcrew.task.task_board import TaskBoard

#: G2 夹具：T-2 只有 1 条验收标准（"a"），judgement 必须恰好 1 条且序号为 0。
GOOD = {"judgements": [{"criterion_index": 0, "satisfied": True,
                        "evidence": "a.py 已定义 def f"}],
        "suggestions": [], "score": 0.9}
#: 唯一一条标准不满足；evidence 成为返工方看到的 fix_hints。
BAD = {"judgements": [{"criterion_index": 0, "satisfied": False,
                       "evidence": "补充业务规则"}],
       "suggestions": [], "score": 0.2}
#: 同 BAD 但 score 很高（0.85）：用于验证 score 阈值旁路 —— reject 但分数够高时放行。
HIGH_SCORE_BAD = {"judgements": [{"criterion_index": 0, "satisfied": False,
                                   "evidence": "补充业务规则"}],
                   "suggestions": [], "score": 0.85}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=lg)
    task = Task(id="T-2", title="t", description="d", role="backend_dev",
                acceptance_criteria=["a"],
                deliverables=[ArtifactSpec(path="a.py", kind="python",
                                           description="d", must_contain=["def f"])])
    board = TaskBoard([task])
    board.transition("T-2", S.READY)
    board.transition("T-2", S.RUNNING)
    board.transition("T-2", S.REVIEW)
    return dict(settings=s, ledger=lg, store=store, task=task, board=board,
                tmp_path=tmp_path)


def commit(env, content):
    env["store"].commit("a.py", content, spec=env["task"].deliverables[0],
                        task=env["task"])


def gk(env, reviews, tmp_path, gate="none"):
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    (fx / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    rev = Reviewer(models, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"])
    return Gatekeeper(rev, env["store"], env["board"], gate=gate,
                      ledger=env["ledger"], prompter=NullPrompter())


async def test_all_pass_leads_to_succeeded(env, tmp_path):
    commit(env, "def f():\n    return 1\n")
    d = await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=1)
    assert d.verdict == "approve" and d.next_status is S.SUCCEEDED


async def test_static_failure_short_circuits_before_llm(env, tmp_path):
    commit(env, "def g():\n    return 1\n")          # 缺 must_contain
    d = await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=1)
    assert d.verdict == "reject" and d.record.layer == "static"
    assert d.next_status is S.REWORK                 # 首次驳回仍有返工额度
    assert env["ledger"].count("gate_result") >= 1
    assert not env["task"].reviews or env["task"].reviews[-1].layer == "static"


async def test_llm_rejection_leads_to_rework_with_hints(env, tmp_path):
    commit(env, "def f():\n    return 1\n")
    d = await gk(env, {"T-2": BAD}, tmp_path).evaluate(env["task"], round_no=1)
    assert d.verdict == "reject" and d.next_status is S.REWORK
    # issues 引用任务里那条标准的原文，fix_hints 是模型给的返工依据
    assert d.record.issues == ["未满足验收标准：a —— 补充业务规则"]
    assert d.fix_hints == ["补充业务规则"]


async def test_rework_is_transitioned_on_board(env, tmp_path):
    commit(env, "def g():\n")
    await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=1)
    assert env["board"].get("T-2").status is S.REWORK
    assert env["board"].get("T-2").rework_count == 1


async def test_failing_after_max_reworks_leads_to_failed(env, tmp_path):
    env["board"].get("T-2").rework_count = 2         # == max_reworks
    commit(env, "def g():\n")
    d = await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=3)
    assert d.verdict == "reject" and d.next_status is S.FAILED


async def test_approve_transitions_board_to_succeeded(env, tmp_path):
    commit(env, "def f():\n    return 1\n")
    await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=1)
    assert env["board"].get("T-2").status is S.SUCCEEDED


async def test_reviews_are_appended_to_task(env, tmp_path):
    commit(env, "def f():\n    return 1\n")
    await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=1)
    assert len(env["board"].get("T-2").reviews) >= 1


async def test_gate_plan_approve_returns_same_tasks(env, tmp_path):
    d = await gk(env, {}, tmp_path, gate="plan").human_plan_gate([env["task"]])
    assert isinstance(d, PlanDecision) and d.action == "approve"


async def test_gate_none_skips_human_prompts(env, tmp_path):
    d = await gk(env, {}, tmp_path, gate="none").human_delivery_gate(None)
    assert d.action == "accept"


async def test_delivery_gate_accept(env, tmp_path):
    d = await gk(env, {}, tmp_path, gate="delivery").human_delivery_gate(None)
    assert isinstance(d, DeliveryDecision)


async def test_null_prompter_is_fully_automatic():
    p = NullPrompter()
    assert (await p.ask_plan([])).action == "approve"
    assert (await p.ask_delivery(None)).action == "accept"


# ── 以下是 brief 之外的补充用例 ──────────────────────────────────────────


class _ExplodingPrompter:
    """一碰就抛：钉死「gate 未开启时绝不询问」——prompter 被调用本身即失败。"""

    async def ask_plan(self, tasks):
        raise AssertionError("gate 未开启时不得询问计划门")

    async def ask_delivery(self, record):
        raise AssertionError("gate 未开启时不得询问交付门")


class _RecordingPrompter:
    """记录被问过的卡点并回一条可区分的裁决，验证开启矩阵与裁决透传。"""

    def __init__(self):
        self.plan_calls = 0
        self.delivery_calls = 0

    async def ask_plan(self, tasks):
        self.plan_calls += 1
        return PlanDecision(action="cancel")

    async def ask_delivery(self, record):
        self.delivery_calls += 1
        return DeliveryDecision(action="abort", task_ids=["T-2"])


def _gk(env, tmp_path, *, gate="none", prompter=None, context_bus=None,
        reviews=None, review_score_threshold=None) -> Gatekeeper:
    """可指定 gate / prompter / context_bus / review_score_threshold 的构造器。"""
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    reviews = {"T-2": GOOD} if reviews is None else reviews
    (fx / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    rev = Reviewer(models, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"])
    return Gatekeeper(rev, env["store"], env["board"], gate=gate,
                      ledger=env["ledger"], prompter=prompter,
                      context_bus=context_bus,
                      review_score_threshold=review_score_threshold)


async def test_repeated_rejections_exhaust_budget_instead_of_looping(env, tmp_path):
    """``bump_rework`` 必须配对：驳回 ``max_reworks + 1`` 次后终态是 FAILED。"""
    commit(env, "def g():\n    return 1\n")          # 缺 must_contain：G1 恒驳回
    gatekeeper = _gk(env, tmp_path)
    decisions = []
    for round_no in range(1, env["task"].max_reworks + 2):      # 3 轮
        decision = await gatekeeper.evaluate(
            env["board"].get("T-2"), round_no=round_no)
        decisions.append(decision.next_status)
        if env["board"].get("T-2").status is S.REWORK:          # 模拟调度器再派发
            env["board"].transition("T-2", S.RUNNING, reason="rework-dispatched")
            env["board"].transition("T-2", S.REVIEW, reason="submitted")
    task = env["board"].get("T-2")
    assert decisions == [S.REWORK, S.REWORK, S.FAILED]
    assert task.status is S.FAILED                  # 不是无界循环
    assert task.rework_count == task.max_reworks == 2
    assert len(task.reviews) == 3                   # 每轮一条 G1 记录


async def test_g1_reject_never_reaches_llm(env, tmp_path):
    commit(env, "def g():\n    return 1\n")
    d = await _gk(env, tmp_path).evaluate(env["task"], round_no=1)
    assert d.record.layer == "static"
    assert env["ledger"].count("llm_call") == 0
    assert not [e for e in env["ledger"].events("gate_result")
                if e.data.get("layer") == "llm"]


async def test_g1_fix_hints_survive_to_decision(env, tmp_path):
    commit(env, "def g():\n    return 1\n")
    d = await _gk(env, tmp_path).evaluate(env["task"], round_no=1)
    assert d.verdict == "reject" and d.record.layer == "static"
    assert d.record.fix_hints == ["在 a.py 中定义 def f"]        # 配对的 fix_hint
    assert d.fix_hints == d.record.fix_hints                     # 存活到 GateDecision


async def test_gate_none_never_touches_prompter(env, tmp_path):
    gatekeeper = _gk(env, tmp_path, gate="none", prompter=_ExplodingPrompter())
    plan = await gatekeeper.human_plan_gate([env["task"]])
    delivery = await gatekeeper.human_delivery_gate(None)
    assert plan.action == "approve" and plan.tasks == [env["task"]]
    assert delivery.action == "accept" and delivery.task_ids == []


@pytest.mark.parametrize("gate", ["plan", "delivery", "both"])
def test_human_gate_without_prompter_raises_at_construction(env, tmp_path, gate):
    """开了人工卡点却没有 prompter：构造期炸响，不得静默放行。

    spec §10.3 的 CLI 卡点是用户明示要的三层门禁之一。静默降级成自动放行比
    没有闸门更危险 —— 操作者会照着「有人工卡点」去规划，而实际上一次都没问过。
    """
    with pytest.raises(ValueError, match="prompter"):
        _gk(env, tmp_path, gate=gate, prompter=None)


async def test_gate_none_without_prompter_stays_fully_automatic(env, tmp_path):
    """全自动路径（``gate="none"``）不要求 prompter：构造成功且只回放行裁决。"""
    gatekeeper = _gk(env, tmp_path, gate="none", prompter=None)
    assert (await gatekeeper.human_plan_gate([env["task"]])).action == "approve"
    assert (await gatekeeper.human_delivery_gate(None)).action == "accept"


async def test_null_prompter_stays_legal_under_human_gate(env, tmp_path):
    """被拒的只有「忘了传」；显式传入 ``NullPrompter`` 是明示的全自动选择
    （brief 的 ``test_gate_plan_approve_returns_same_tasks`` 也这么用）。"""
    gatekeeper = _gk(env, tmp_path, gate="both", prompter=NullPrompter())
    assert (await gatekeeper.human_plan_gate([env["task"]])).action == "approve"
    assert (await gatekeeper.human_delivery_gate(None)).action == "accept"


async def test_gate_mode_matrix_and_decision_passthrough(env, tmp_path):
    plan_only = _RecordingPrompter()
    gk_plan = _gk(env, tmp_path, gate="plan", prompter=plan_only)
    assert (await gk_plan.human_plan_gate([env["task"]])).action == "cancel"
    assert (await gk_plan.human_delivery_gate(None)).action == "accept"
    assert (plan_only.plan_calls, plan_only.delivery_calls) == (1, 0)

    delivery_only = _RecordingPrompter()
    gk_delivery = _gk(env, tmp_path, gate="delivery", prompter=delivery_only)
    assert (await gk_delivery.human_plan_gate([env["task"]])).action == "approve"
    d = await gk_delivery.human_delivery_gate(None)
    assert d.action == "abort" and d.task_ids == ["T-2"]         # 裁决透传
    assert (delivery_only.plan_calls, delivery_only.delivery_calls) == (0, 1)

    both = _RecordingPrompter()
    gk_both = _gk(env, tmp_path, gate="both", prompter=both)
    assert (await gk_both.human_plan_gate([env["task"]])).action == "cancel"
    assert (await gk_both.human_delivery_gate(None)).action == "abort"
    assert (both.plan_calls, both.delivery_calls) == (1, 1)


async def test_rework_decision_reaches_context_bus(env, tmp_path):
    """spec §11.3：返工意见沉淀为全局决策；它要求 ``rework_count >= 1``，
    因此本用例同时钉死「先 bump 再记录」的调用顺序。"""
    commit(env, "def g():\n    return 1\n")
    bus = ContextBus("目标", env["board"], env["store"])
    d = await _gk(env, tmp_path, context_bus=bus).evaluate(env["task"], round_no=1)
    decisions = bus.decisions()
    assert d.next_status is S.REWORK and len(decisions) == 1
    assert decisions[0].task_id == "T-2" and decisions[0].role == "backend_dev"
    assert decisions[0].decision == "在 a.py 中定义 def f"


async def test_approve_records_both_layers_without_rework_side_effects(env, tmp_path):
    commit(env, "def f():\n    return 1\n")
    bus = ContextBus("目标", env["board"], env["store"])
    d = await _gk(env, tmp_path, context_bus=bus).evaluate(env["task"], round_no=1)
    task = env["board"].get("T-2")
    assert [r.layer for r in task.reviews] == ["static", "llm"]
    assert d.record.layer == "llm" and d.fix_hints == []
    assert task.rework_count == 0 and bus.decisions() == []


async def test_gate_result_events_carry_spec_fields(env, tmp_path):
    commit(env, "def g():\n    return 1\n")
    await _gk(env, tmp_path).evaluate(env["task"], round_no=2)
    events = list(env["ledger"].events("gate_result"))
    assert len(events) == 1                          # 短路：只有 G1 一条
    data = events[0].data
    assert data["task_id"] == "T-2" and data["round"] == 2
    assert data["layer"] == "static"
    assert data["verdict"] == "reject" and data["score"] is None
    assert data["issues"] == env["board"].get("T-2").reviews[-1].issues


async def test_full_pass_emits_exactly_one_gate_result_per_layer(env, tmp_path):
    """两层各一条，双向钉死计数：G2 的事件归 ``Reviewer``，本模块只发 G1。

    Task 30 的运行报告按 ``gate_result`` 统计各层驳回数（G1 拦截 N / G2 驳回 M），
    任何一层被记两次都会让报告显示错的数字 —— 多一条或少一条都必须让本用例失败。
    """
    commit(env, "def f():\n    return 1\n")
    await _gk(env, tmp_path).evaluate(env["task"], round_no=1)
    events = list(env["ledger"].events("gate_result"))
    assert [e.data["layer"] for e in events] == ["static", "llm"]
    assert [e.data["task_id"] for e in events] == ["T-2", "T-2"]
    assert [e.data["verdict"] for e in events] == ["approve", "approve"]
    # 板上两层的评审记录不受「谁发事件」影响：每层都必须 record_review。
    assert [r.layer for r in env["board"].get("T-2").reviews] == ["static", "llm"]


async def test_transition_reason_uses_status_value_not_enum(env, tmp_path):
    commit(env, "def g():\n    return 1\n")
    await _gk(env, tmp_path).evaluate(env["task"], round_no=1)
    change = env["board"].get("T-2").status_history[-1]
    assert change.from_status is S.REVIEW and change.to_status is S.REWORK
    assert change.reason == "gate-rework"            # 不是 "gate-TaskStatus.REWORK"


# ── P0-1: score 阈值旁路 ────────────────────────────────────────────────────


async def test_score_bypass_reject_with_high_score_approves(env, tmp_path):
    """reject 但 score >= threshold：改为放行，状态 SUCCEEDED，verdict 变 approve。"""
    commit(env, "def f():\n    return 1\n")
    d = await _gk(env, tmp_path, reviews={"T-2": HIGH_SCORE_BAD},
                  review_score_threshold=0.5).evaluate(env["task"], round_no=1)
    assert d.verdict == "approve"                     # 旁路后裁决变为 approve
    assert d.next_status is S.SUCCEEDED               # 直接成功，不返工
    assert d.record.reviewer.endswith(":score-bypass")
    assert env["board"].get("T-2").status is S.SUCCEEDED


async def test_score_bypass_preserves_issues_in_ledger(env, tmp_path):
    """旁路的 gate_result 事件：verdict=approve 但 issues 保留，reviewer 追加后缀。"""
    commit(env, "def f():\n    return 1\n")
    await _gk(env, tmp_path, reviews={"T-2": HIGH_SCORE_BAD},
              review_score_threshold=0.5).evaluate(env["task"], round_no=1)
    events = list(env["ledger"].events("gate_result"))
    # 原始 LLM 事件（reject）+ 旁路事件（approve）
    bypass_events = [e for e in events if ":score-bypass" in e.data.get("reviewer", "")]
    assert len(bypass_events) == 1
    data = bypass_events[0].data
    assert data["verdict"] == "approve"
    assert data["score"] == 0.85
    assert data["issues"] == ["未满足验收标准：a —— 补充业务规则"]  # issues 保留
    assert data["reviewer"] == "architect:score-bypass"


async def test_no_bypass_when_score_below_threshold(env, tmp_path):
    """reject 且 score < threshold：正常驳回，走返工通道。"""
    commit(env, "def f():\n    return 1\n")
    d = await _gk(env, tmp_path, reviews={"T-2": BAD},
                  review_score_threshold=0.5).evaluate(env["task"], round_no=1)
    assert d.verdict == "reject"
    assert d.next_status is S.REWORK
    assert not d.record.reviewer.endswith(":score-bypass")
    assert env["board"].get("T-2").status is S.REWORK


async def test_no_bypass_when_threshold_is_none(env, tmp_path):
    """threshold=None（默认）：完全保持现有行为，reject 即驳回。"""
    commit(env, "def f():\n    return 1\n")
    d = await _gk(env, tmp_path, reviews={"T-2": HIGH_SCORE_BAD},
                  review_score_threshold=None).evaluate(env["task"], round_no=1)
    assert d.verdict == "reject" and d.next_status is S.REWORK
    bypass_events = [e for e in env["ledger"].events("gate_result")
                     if ":score-bypass" in e.data.get("reviewer", "")]
    assert bypass_events == []                          # 没有旁路事件


async def test_no_bypass_for_static_rejection(env, tmp_path):
    """G1 静态校验驳回（score=None）：即使设了 threshold 也不旁路。"""
    commit(env, "def g():\n    return 1\n")           # 缺 must_contain → G1 恒驳回
    d = await _gk(env, tmp_path, reviews={"T-2": HIGH_SCORE_BAD},
                  review_score_threshold=0.5).evaluate(env["task"], round_no=1)
    assert d.verdict == "reject" and d.next_status is S.REWORK
    assert d.record.layer == "static"                  # 短路，没到 LLM
    assert env["ledger"].count("llm_call") == 0


async def test_score_bypass_does_not_consume_rework_quota(env, tmp_path):
    """旁路放行不消耗返工额度：rework_count 保持 0。"""
    commit(env, "def f():\n    return 1\n")
    await _gk(env, tmp_path, reviews={"T-2": HIGH_SCORE_BAD},
              review_score_threshold=0.5).evaluate(env["task"], round_no=1)
    assert env["board"].get("T-2").rework_count == 0
