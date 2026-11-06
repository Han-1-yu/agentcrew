"""R7：主管审核（谁派的活谁验收）。

R6 的四道闸只回答「谁能派」，没回答「派出去的活谁验收」——``manager_only`` 模式下
经理把活派给下属后，下属的产出仍只由目录里静态配置的 G2 评审人把关，经理本人
对结果没有发言权。这一层补那条回路。

覆盖：非委派任务不审、自派自审跳过、未知主管跳过、评审人/层名正确、主管驳回
走返工且带自己的 fix_hints、**主管驳回不受 score 旁路保护**、未注入时行为不变、
mock 通道明确跳过。

注：``Reviewer`` 的 mock 夹具按 ``task_id`` 索引，G2 与主管会拿到同一份——
所以主管层语义用可控的假评审器验，Gatekeeper 集成才用真 Reviewer。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.gate.gatekeeper import Gatekeeper, NullPrompter
from agentcrew.gate.reviewer import Reviewer
from agentcrew.gate.supervisor import SupervisorReviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import ReviewRecord, Task
from agentcrew.task.task import TaskStatus as S
from agentcrew.task.task_board import TaskBoard

GOOD = {"judgements": [{"criterion_index": 0, "satisfied": True,
                        "evidence": "a.py 已定义 def f"}],
        "suggestions": [], "score": 0.9}
HIGH_SCORE_BAD = {"judgements": [{"criterion_index": 0, "satisfied": False,
                                  "evidence": "补充业务规则"}],
                  "suggestions": [], "score": 0.85}


class _FakeReviewer:
    """可控的评审器替身：记录调用并按预设裁决返回。"""

    def __init__(
        self,
        *,
        verdict: str = "approve",
        issues: list[str] | None = None,
        fix_hints: list[str] | None = None,
        score: float | None = 0.9,
    ) -> None:
        self.verdict = verdict
        self.issues = list(issues or [])
        self.fix_hints = list(fix_hints or [])
        self.score = score
        self.calls: list[tuple[str, list[str], str]] = []

    async def review(
        self, task, *, round_no, reviewer_keys=None, layer="llm"
    ) -> ReviewRecord:
        keys = list(reviewer_keys or [])
        self.calls.append((task.id, keys, layer))
        return ReviewRecord(
            task_id=task.id,
            round=round_no,
            layer=layer,
            verdict=self.verdict,
            score=self.score,
            issues=self.issues,
            fix_hints=self.fix_hints,
            reviewer="+".join(keys) or "none",
            created_at=datetime.now(),
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=ledger)
    task = Task(
        id="T-DYN-001", title="子任务", description="写 utils.py",
        role="backend_dev", acceptance_criteria=["a"],
        deliverables=[ArtifactSpec(path="a.py", kind="python", description="d",
                                   must_contain=["def f"])],
        dynamic=True, delegated_by="project_manager",
    )
    board = TaskBoard([task])
    for status in (S.READY, S.RUNNING, S.REVIEW):
        board.transition(task.id, status)
    return dict(settings=settings, ledger=ledger, store=store, task=task,
                board=board, catalog=RoleCatalog(BUILTIN_ROLES),
                tmp_path=tmp_path)


def _supervisor(env, reviewer) -> SupervisorReviewer:
    return SupervisorReviewer(reviewer, env["catalog"], env["ledger"])


# ── SupervisorReviewer：谁审、什么时候不审 ────────────────────────────────


async def test_non_delegated_task_is_not_reviewed(env):
    """规划阶段的任务没有主管：返回 None，一次调用都不发。"""
    fake = _FakeReviewer()
    task = env["task"].model_copy(update={"delegated_by": ""})

    assert await _supervisor(env, fake).review(task, round_no=1) is None
    assert fake.calls == []


async def test_self_delegation_is_skipped(env):
    """自己派给自己不审：换个身份再看一遍只会白烧调用和返工额度。"""
    fake = _FakeReviewer()
    task = env["task"].model_copy(update={"delegated_by": "backend_dev"})

    assert await _supervisor(env, fake).review(task, round_no=1) is None
    assert fake.calls == []
    events = list(env["ledger"].events("supervisor_review_skipped"))
    assert events[0].data["reason"] == "self_delegated"


async def test_unknown_supervisor_is_skipped(env):
    """主管不在角色目录里（父任务角色是 generic 之类）：跳过并留痕。"""
    fake = _FakeReviewer()
    task = env["task"].model_copy(update={"delegated_by": "ghost_manager"})

    assert await _supervisor(env, fake).review(task, round_no=1) is None
    assert fake.calls == []
    events = list(env["ledger"].events("supervisor_review_skipped"))
    assert events[0].data["reason"] == "unknown_supervisor"


async def test_reviews_with_supervisor_key_and_layer(env):
    """评审人 = 派活的人，层名 = supervisor（与常规 G2 分开统计）。"""
    fake = _FakeReviewer()

    record = await _supervisor(env, fake).review(env["task"], round_no=2)

    assert fake.calls == [("T-DYN-001", ["project_manager"], "supervisor")]
    assert record is not None
    assert record.layer == "supervisor"
    assert record.round == 2


# ── Gatekeeper 集成：主管驳回走真返工通道 ────────────────────────────────


def _gatekeeper(env, reviews: dict, tmp_path: Path, *, supervisor=None,
                threshold: float | None = None) -> Gatekeeper:
    fixtures = tmp_path / "fx"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "reviews.json").write_text(
        json.dumps(reviews), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"],
                          fixtures_dir=fixtures)
    reviewer = Reviewer(models, env["catalog"], env["store"], env["ledger"])
    return Gatekeeper(
        reviewer, env["store"], env["board"], gate="none",
        ledger=env["ledger"], prompter=NullPrompter(),
        review_score_threshold=threshold, supervisor=supervisor,
    )


def _commit(env) -> None:
    env["store"].commit("a.py", "def f():\n    return 1\n",
                        spec=env["task"].deliverables[0], task=env["task"])


async def test_supervisor_approve_leads_to_succeeded(env, tmp_path):
    _commit(env)
    fake = _FakeReviewer()
    gk = _gatekeeper(env, {"T-DYN-001": GOOD}, tmp_path,
                     supervisor=_supervisor(env, fake))

    decision = await gk.evaluate(env["task"], round_no=1)

    assert decision.verdict == "approve"
    assert decision.next_status is S.SUCCEEDED
    assert fake.calls and fake.calls[0][2] == "supervisor"
    # 主管的裁决写进任务评审记录，层名与常规 G2 区分
    assert env["board"].get("T-DYN-001").reviews[-1].layer == "supervisor"


async def test_supervisor_reject_leads_to_rework_with_own_hints(env, tmp_path):
    """主管驳回：走 REWORK，且返工提示来自主管自己，不是 G2 的旧意见。"""
    _commit(env)
    fake = _FakeReviewer(verdict="reject", issues=["接口没按约定命名"],
                         fix_hints=["把 fetch_todo 改成 get_todo"], score=0.9)
    gk = _gatekeeper(env, {"T-DYN-001": GOOD}, tmp_path,
                     supervisor=_supervisor(env, fake))

    decision = await gk.evaluate(env["task"], round_no=1)

    assert decision.verdict == "reject"
    assert decision.next_status is S.REWORK
    assert "把 fetch_todo 改成 get_todo" in decision.fix_hints
    assert env["board"].get("T-DYN-001").rework_count == 1


async def test_supervisor_reject_is_not_saved_by_score_bypass(env, tmp_path):
    """G2 被分数旁路放行后，主管仍然能否决——旁路只对机器评审意见生效。"""
    _commit(env)
    fake = _FakeReviewer(verdict="reject", issues=["交付物缺测试"],
                         fix_hints=["补一个 test_utils.py"], score=0.95)
    gk = _gatekeeper(env, {"T-DYN-001": HIGH_SCORE_BAD}, tmp_path,
                     supervisor=_supervisor(env, fake), threshold=0.8)

    decision = await gk.evaluate(env["task"], round_no=1)

    # 阈值 0.8 < 0.85，G2 本会被旁路；主管说不行就是不行
    assert decision.verdict == "reject"
    assert decision.next_status is S.REWORK
    assert "补一个 test_utils.py" in decision.fix_hints


async def test_without_supervisor_behaviour_unchanged(env, tmp_path):
    """未注入主管（默认）：委派任务与普通任务走同一条路。"""
    _commit(env)
    gk = _gatekeeper(env, {"T-DYN-001": GOOD}, tmp_path)

    decision = await gk.evaluate(env["task"], round_no=1)

    assert decision.verdict == "approve"
    assert decision.next_status is S.SUCCEEDED
    assert env["ledger"].count("supervisor_review_skipped") == 0


async def test_g2_reject_skips_supervisor(env, tmp_path):
    """G2 已经驳回时主管不该再烧一次调用——它的裁决改变不了结果。"""
    _commit(env)
    fake = _FakeReviewer(verdict="approve")
    gk = _gatekeeper(env, {"T-DYN-001": HIGH_SCORE_BAD}, tmp_path,
                     supervisor=_supervisor(env, fake))
    # 不设阈值，G2 的 reject 生效

    decision = await gk.evaluate(env["task"], round_no=1)

    assert decision.verdict == "reject"
    assert fake.calls == []


# ── 装配：mock 通道明确跳过 ──────────────────────────────────────────────


def test_mock_channel_skips_supervisor(tmp_path, monkeypatch):
    """离线 mock 没有 supervisor 夹具：明确跳过并留痕，而不是装作审过。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_SUPERVISOR_REVIEW_ENABLED", "true")
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=ledger)
    board = TaskBoard([Task(id="T-1", title="t", description="d",
                            role="backend_dev", acceptance_criteria=[],
                            deliverables=[])])
    models = ModelFactory(settings, ledger=ledger)
    run = SimpleNamespace(models=models, store=store, ledger=ledger,
                          record=SimpleNamespace(run_id="R"))
    crew = Crew(settings)

    gk = crew._build_gatekeeper(run, board, bus=None)

    assert gk._supervisor is None
    events = list(ledger.events("supervisor_review_skipped"))
    assert events[0].data["reason"] == "mock_channel"


def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.delenv("AGENTCREW_SUPERVISOR_REVIEW_ENABLED", raising=False)
    assert Settings.load().supervisor_review_enabled is False


async def test_real_reviewer_emits_supervisor_gate_result(env, tmp_path):
    """真 Reviewer 路径：主管那次调用会落一条 ``layer="supervisor"`` 的审计事件。

    mock 夹具按 task_id 索引，主管与 G2 拿到同一份（都 approve）——这里验的是
    记账层级，不是裁决差异。
    """
    _commit(env)
    fixtures = tmp_path / "fx-real"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "reviews.json").write_text(
        json.dumps({"T-DYN-001": GOOD}), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"],
                          fixtures_dir=fixtures)
    reviewer = Reviewer(models, env["catalog"], env["store"], env["ledger"])
    gk = Gatekeeper(reviewer, env["store"], env["board"], gate="none",
                    ledger=env["ledger"], prompter=NullPrompter(),
                    supervisor=SupervisorReviewer(
                        reviewer, env["catalog"], env["ledger"]))

    decision = await gk.evaluate(env["task"], round_no=1)

    assert decision.next_status is S.SUCCEEDED
    layers = [e.data["layer"] for e in env["ledger"].events("gate_result")]
    assert layers.count("llm") == 1
    assert layers.count("supervisor") == 1


# ── 报告口径：主管驳回复不能从统计里消失 ─────────────────────────────────


def _record(layer: str, verdict: str) -> ReviewRecord:
    return ReviewRecord(
        task_id="T-1", round=1, layer=layer, verdict=verdict, score=None,
        issues=[], fix_hints=[], reviewer=layer, created_at=datetime.now(),
    )


def test_review_line_counts_supervisor_rejection():
    """R7 之前三处统计只认 static/llm/human：主管驳回会让报告显示「驳回 0 次」。"""
    from agentcrew.crew.report import review_line

    task = Task(id="T-1", title="t", description="d", role="backend_dev",
                acceptance_criteria=[], deliverables=[])
    task.reviews = [_record("llm", "approve"), _record("supervisor", "reject")]

    line = review_line([task])

    assert "主管驳回 1" in line
    assert "50%" in line


def test_review_line_unchanged_without_supervisor():
    """没有主管记录时输出逐字不变（默认关闭的兼容性）。"""
    from agentcrew.crew.report import review_line

    task = Task(id="T-1", title="t", description="d", role="backend_dev",
                acceptance_criteria=[], deliverables=[])
    task.reviews = [_record("static", "approve"), _record("llm", "approve")]

    line = review_line([task])

    assert line == "100%（G1 拦截 0 / G2 驳回 0 / 人工驳回 0）"


# ── R9：覆盖规划期任务（跨任务层级） ─────────────────────────────────────


def _supervisor_all(env, reviewer, *, role: str = "project_manager"):
    return SupervisorReviewer(reviewer, env["catalog"], env["ledger"],
                              scope="all", fallback_role=role)


async def test_scope_all_reviews_planned_task(env):
    """没有 delegated_by 的规划期任务：scope=all 时由配置的主管角色验收。"""
    fake = _FakeReviewer()
    task = env["task"].model_copy(update={"delegated_by": ""})

    record = await _supervisor_all(env, fake).review(task, round_no=1)

    assert record is not None
    assert fake.calls == [("T-DYN-001", ["project_manager"], "supervisor")]


async def test_scope_delegated_ignores_planned_task(env):
    """默认 scope=delegated：规划期任务完全不受影响（R7 行为）。"""
    fake = _FakeReviewer()
    task = env["task"].model_copy(update={"delegated_by": ""})

    assert await _supervisor(env, fake).review(task, round_no=1) is None
    assert fake.calls == []


async def test_scope_all_skips_when_supervisor_already_in_g2(env):
    """architect 的任务 G2 本来就是 PM 在审：不再以主管身份重复烧一次调用。"""
    fake = _FakeReviewer()
    task = env["task"].model_copy(update={
        "role": "architect", "delegated_by": "",
    })

    assert await _supervisor_all(env, fake).review(task, round_no=1) is None
    assert fake.calls == []
    events = list(env["ledger"].events("supervisor_review_skipped"))
    assert events[0].data["reason"] == "already_in_g2"


async def test_scope_all_unknown_role_is_skipped(env):
    fake = _FakeReviewer()
    task = env["task"].model_copy(update={"delegated_by": ""})

    assert await _supervisor_all(
        env, fake, role="ghost_manager").review(task, round_no=1) is None
    assert fake.calls == []
    events = list(env["ledger"].events("supervisor_review_skipped"))
    assert events[0].data["reason"] == "unknown_supervisor"


async def test_scope_all_empty_role_is_skipped(env):
    """配了空串：跳过并说明「没配主管」，不静默改用别人。"""
    fake = _FakeReviewer()
    task = env["task"].model_copy(update={"delegated_by": ""})

    assert await _supervisor_all(env, fake, role="  ").review(
        task, round_no=1) is None
    assert fake.calls == []
    events = list(env["ledger"].events("supervisor_review_skipped"))
    assert events[0].data["reason"] == "no_supervisor_configured"


async def test_delegated_task_still_wins_over_fallback(env):
    """两者都成立时以 delegated_by 为准：派活的人优先于配置的主管。"""
    fake = _FakeReviewer()

    record = await _supervisor_all(env, fake).review(env["task"], round_no=1)

    assert record is not None
    assert fake.calls[0][1] == ["project_manager"]
