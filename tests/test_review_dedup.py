"""P2-3：G2 评审意见去重 + 收敛检测。

钉三件事：

- **去重**：完全相同、仅大小写 / 标点差异、token 高度重叠（Jaccard > 0.8）的
  意见被合并；互不相关的意见保留；去重发生在 ``review()`` 返回前，并记
  ``review_dedup`` 事件（original_count / deduped_count / removed_issues）。
- **收敛检测放行**：同一任务连续 N 轮 LLM 驳回意见不变时判收敛，Gatekeeper 改为
  放行（reviewer 追加 ``:converged``），记 ``review_converged`` 事件；未收敛时
  正常驳回走返工通道。
- **向后兼容**：``convergence_threshold=0`` / ``None`` 关闭收敛检测，G1 结构性
  驳回不参与收敛判定。
"""

import json

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.gate.gatekeeper import Gatekeeper
from agentcrew.gate.reviewer import (
    ReviewVerdict,
    Reviewer,
    deduplicate_issues,
)
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard
from agentcrew.task.task import TaskStatus as S

#: 唯一一条标准不满足；evidence 固定，因此每轮驳回 issues 完全相同（收敛用）。
BAD = {"judgements": [{"criterion_index": 0, "satisfied": False,
                       "evidence": "补充业务规则"}],
       "suggestions": [], "score": 0.2}


# ── 去重纯函数 ──────────────────────────────────────────────────────────────


def test_identical_issues_are_deduplicated():
    issues = ["缺空值校验", "缺空值校验", "缺空值校验"]
    assert deduplicate_issues(issues) == ["缺空值校验"]


def test_case_and_punctuation_variants_are_merged():
    """大小写 / 句尾标点 / 首尾空白差异规范化后相等 → 合并。"""
    issues = ["Missing import", "missing import.", "  MISSING import  "]
    assert deduplicate_issues(issues) == ["Missing import"]


def test_unrelated_issues_are_kept():
    issues = ["a.py 未做空值校验", "Dockerfile 缺少健康检查"]
    assert deduplicate_issues(issues) == issues


def test_keeps_first_occurrence_order():
    issues = ["第二个问题", "第一个问题", "第二个问题"]
    assert deduplicate_issues(issues) == ["第二个问题", "第一个问题"]


def test_near_duplicate_by_jaccard_is_merged():
    """token 集合 Jaccard > 0.8 的近重复意见被合并（保留先出现者）。

    第一条 7 个 token，第二条仅多一个 ``correctly`` → Jaccard 7/8 = 0.875。
    """
    issues = [
        "the endpoint does not validate empty input",
        "the endpoint does not validate empty input correctly",
    ]
    result = deduplicate_issues(issues)
    assert result == ["the endpoint does not validate empty input"]


def test_empty_input():
    assert deduplicate_issues([]) == []


# ── review_dedup ledger 事件 ────────────────────────────────────────────────


class _FakeModels:
    """``ModelFactory`` 替身：按 role 返回预设裁决（与 test_reviewer 同款）。"""

    def __init__(self, by_role: dict[str, dict], settings: Settings) -> None:
        self._by_role = by_role
        self.settings = settings

    def get(self, alias: str) -> object:
        return object()

    async def complete_structured(self, alias, messages, schema, *,
                                  temperature, max_tokens, purpose, role):
        return ReviewVerdict(**self._by_role[role])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=lg)
    task = Task(id="T-2", title="t", description="d", role="project_manager",
                acceptance_criteria=["c0", "c1"],
                deliverables=[ArtifactSpec(path="a.py", kind="python",
                                           description="d")])
    store.commit("a.py", "def f():\n    return 1\n",
                 spec=task.deliverables[0], task=task)
    return dict(settings=s, ledger=lg, store=store, task=task, tmp_path=tmp_path)


async def test_review_emits_review_dedup_event(env):
    """两位评审人给出重复 issue → 去重并记 review_dedup 事件。"""
    fake = _FakeModels({
        "architect": {
            "judgements": [
                {"criterion_index": 0, "satisfied": False, "evidence": "接口缺少错误语义"},
                {"criterion_index": 1, "satisfied": True, "evidence": "已覆盖空值"},
            ],
            "suggestions": [], "score": 0.5},
        "product_manager": {
            "judgements": [
                {"criterion_index": 0, "satisfied": False, "evidence": "接口缺少错误语义"},
                {"criterion_index": 1, "satisfied": False, "evidence": "校验未覆盖空字符串"},
            ],
            "suggestions": [], "score": 0.2},
    }, env["settings"])
    rev = Reviewer(fake, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"])
    r = await rev.review(env["task"], round_no=1)
    # 两人对 index 0 的同一判定只留一份
    assert r.issues == [
        "未满足验收标准：c0 —— 接口缺少错误语义",
        "未满足验收标准：c1 —— 校验未覆盖空字符串",
    ]
    events = list(env["ledger"].events("review_dedup"))
    assert len(events) == 1
    data = events[0].data
    assert data["task_id"] == "T-2" and data["round"] == 1
    assert data["original_count"] == 3 and data["deduped_count"] == 2
    assert data["removed_issues"] == ["未满足验收标准：c0 —— 接口缺少错误语义"]


async def test_review_without_duplicates_emits_no_dedup_event(env):
    """没有重复意见时不记 review_dedup（不制造噪声）。"""
    fake = _FakeModels({
        "architect": {
            "judgements": [
                {"criterion_index": 0, "satisfied": True, "evidence": "ok"},
                {"criterion_index": 1, "satisfied": False, "evidence": "唯一的问题"},
            ],
            "suggestions": [], "score": 0.5},
        "product_manager": {
            "judgements": [
                {"criterion_index": 0, "satisfied": True, "evidence": "ok"},
                {"criterion_index": 1, "satisfied": True, "evidence": "ok"},
            ],
            "suggestions": [], "score": 0.9},
    }, env["settings"])
    rev = Reviewer(fake, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"])
    await rev.review(env["task"], round_no=1)
    assert env["ledger"].count("review_dedup") == 0


# ── 收敛检测（Gatekeeper 端到端）────────────────────────────────────────────


@pytest.fixture
def gk_env(env, tmp_path):
    """单评审人（backend_dev→architect）任务，G1 通过、G2 恒定驳回。

    用独立产物路径 ``svc.py``：与 ``env`` 夹具提交的 ``a.py`` 同 store，
    同路径跨任务会触发 ``ArtifactConflictError``。
    """
    task = Task(id="T-9", title="t", description="d", role="backend_dev",
                acceptance_criteria=["a"],
                deliverables=[ArtifactSpec(path="svc.py", kind="python",
                                           description="d", must_contain=["def f"])])
    board = TaskBoard([task])
    board.transition("T-9", S.READY)
    board.transition("T-9", S.RUNNING)
    board.transition("T-9", S.REVIEW)
    env["store"].commit("svc.py", "def f():\n    return 1\n",
                        spec=task.deliverables[0], task=task)
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    (fx / "reviews.json").write_text(json.dumps({"T-9": BAD}), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    rev = Reviewer(models, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"])
    return dict(task=task, board=board, reviewer=rev, env=env, tmp_path=tmp_path)


async def _run_rounds(gk, board, task_id, n):
    """顺序跑 n 轮 evaluate，模拟调度器把 REWORK 重新派回 REVIEW。"""
    decisions = []
    for round_no in range(1, n + 1):
        d = await gk.evaluate(board.get(task_id), round_no=round_no)
        decisions.append(d)
        if board.get(task_id).status is S.REWORK:
            board.transition(task_id, S.RUNNING, reason="rework-dispatched")
            board.transition(task_id, S.REVIEW, reason="submitted")
    return decisions


async def test_converged_after_two_identical_rounds_approves(gk_env):
    """threshold=2：第 1 轮正常驳回，第 2 轮意见相同 → 收敛放行。"""
    gk = Gatekeeper(gk_env["reviewer"], gk_env["env"]["store"], gk_env["board"],
                    gate="none", ledger=gk_env["env"]["ledger"], prompter=None,
                    convergence_threshold=2)
    decisions = await _run_rounds(gk, gk_env["board"], "T-9", 2)
    assert decisions[0].verdict == "reject" and decisions[0].next_status is S.REWORK
    assert decisions[1].verdict == "approve" and decisions[1].next_status is S.SUCCEEDED
    assert decisions[1].record.reviewer.endswith(":converged")
    assert gk_env["board"].get("T-9").status is S.SUCCEEDED


async def test_single_rejection_not_converged(gk_env):
    """threshold=2：只驳回一轮（连续=1）→ 不收敛，正常驳回走返工。"""
    gk = Gatekeeper(gk_env["reviewer"], gk_env["env"]["store"], gk_env["board"],
                    gate="none", ledger=gk_env["env"]["ledger"], prompter=None,
                    convergence_threshold=2)
    d = await gk.evaluate(gk_env["board"].get("T-9"), round_no=1)
    assert d.verdict == "reject" and d.next_status is S.REWORK
    assert not d.record.reviewer.endswith(":converged")
    assert gk_env["env"]["ledger"].count("review_converged") == 0


async def test_convergence_disabled_with_zero_threshold(gk_env):
    """threshold=0：连续多轮相同意见也不收敛，正常烧返工额度。"""
    gk = Gatekeeper(gk_env["reviewer"], gk_env["env"]["store"], gk_env["board"],
                    gate="none", ledger=gk_env["env"]["ledger"], prompter=None,
                    convergence_threshold=0)
    decisions = await _run_rounds(gk, gk_env["board"], "T-9", 2)
    assert decisions[0].next_status is S.REWORK
    assert decisions[1].next_status is S.REWORK       # 第 2 轮仍驳回
    assert gk_env["env"]["ledger"].count("review_converged") == 0


async def test_convergence_disabled_with_none_threshold(gk_env):
    """threshold=None：关闭收敛检测（向后兼容）。"""
    gk = Gatekeeper(gk_env["reviewer"], gk_env["env"]["store"], gk_env["board"],
                    gate="none", ledger=gk_env["env"]["ledger"], prompter=None,
                    convergence_threshold=None)
    decisions = await _run_rounds(gk, gk_env["board"], "T-9", 2)
    assert decisions[0].next_status is S.REWORK
    assert decisions[1].next_status is S.REWORK


async def test_static_rejection_not_converged(gk_env):
    """G1 结构性驳回不参与收敛判定（layer != llm）。"""
    board = gk_env["board"]
    task = gk_env["task"]
    # 重提交不含 def f 的内容：G1 因 must_contain 缺失而恒驳回
    gk_env["env"]["store"].commit(
        "svc.py", "x = 1\n", spec=task.deliverables[0], task=task)
    gk = Gatekeeper(gk_env["reviewer"], gk_env["env"]["store"], board,
                    gate="none", ledger=gk_env["env"]["ledger"], prompter=None,
                    convergence_threshold=2)
    d = await gk.evaluate(board.get("T-9"), round_no=1)
    assert d.record.layer == "static" and d.verdict == "reject"
    assert gk_env["env"]["ledger"].count("review_converged") == 0


async def test_review_converged_event_in_ledger(gk_env):
    """收敛放行时记 review_converged 事件（含 task_id / rounds / issues）。"""
    gk = Gatekeeper(gk_env["reviewer"], gk_env["env"]["store"], gk_env["board"],
                    gate="none", ledger=gk_env["env"]["ledger"], prompter=None,
                    convergence_threshold=2)
    await _run_rounds(gk, gk_env["board"], "T-9", 2)
    events = list(gk_env["env"]["ledger"].events("review_converged"))
    assert len(events) == 1
    data = events[0].data
    assert data["task_id"] == "T-9" and data["rounds"] == 2
    assert data["issues"] == ["未满足验收标准：a —— 补充业务规则"]
    # 旁路 gate_result：verdict=approve、issues 保留、reviewer 追加 :converged
    bypass = [e for e in gk_env["env"]["ledger"].events("gate_result")
              if e.data.get("reviewer", "").endswith(":converged")]
    assert len(bypass) == 1
    assert bypass[0].data["verdict"] == "approve"


async def test_check_convergence_public_method(gk_env):
    """公开方法：连续相同意见第 N 次返回 True，首次与不连续返回 False。"""
    gk = Gatekeeper(gk_env["reviewer"], gk_env["env"]["store"], gk_env["board"],
                    gate="none", ledger=gk_env["env"]["ledger"], prompter=None,
                    convergence_threshold=2)
    issues = ["缺空值校验"]
    assert gk.check_convergence("T-X", issues) is False       # 第 1 轮
    assert gk.check_convergence("T-X", issues) is True        # 第 2 轮相同 → 收敛
    assert gk.check_convergence("T-X", ["另一个完全不同的问题"]) is False
