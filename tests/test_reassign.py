"""R10：主管驳回后「换个人做」。

`Gatekeeper` 原来的驳回只有两条路：退回原执行者（REWORK）或额度用尽 FAILED。
主管对「这个人做不好」的判断没有出口。这一层加一条：主管驳回且还有重派额度时，
把 `task.role` 换成配置的备选角色，**照常走 REWORK**——不新增 `REWORK → READY`
跃迁（那会动 10 态契约），下次派发自然找新角色。

覆盖：候选筛选（未知角色 / 自己 / 主管本人 / 空映射）、上限、G2 驳回不换人、
换人后仍受返工额度约束、board 与事件留痕。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.gate.gatekeeper import Gatekeeper, NullPrompter
from agentcrew.gate.reassign import ReassignPolicy
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
BAD = {"judgements": [{"criterion_index": 0, "satisfied": False,
                       "evidence": "补充业务规则"}],
       "suggestions": [], "score": 0.2}


class _FakeReviewer:
    def __init__(self, *, verdict="approve", issues=None, fix_hints=None):
        self.verdict = verdict
        self.issues = list(issues or [])
        self.fix_hints = list(fix_hints or [])
        self.calls: list[tuple[str, list[str], str]] = []

    async def review(self, task, *, round_no, reviewer_keys=None, layer="llm"):
        keys = list(reviewer_keys or [])
        self.calls.append((task.id, keys, layer))
        return ReviewRecord(
            task_id=task.id, round=round_no, layer=layer,
            verdict=self.verdict, score=0.5, issues=self.issues,
            fix_hints=self.fix_hints, reviewer="+".join(keys) or "none",
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
        id="T-1", title="t", description="写 a.py", role="backend_dev",
        acceptance_criteria=["a"],
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


# ── 策略层：挑谁 ──────────────────────────────────────────────────────────


def _policy(env, fallback, *, max_reassignments=1):
    return ReassignPolicy(env["catalog"], fallback,
                          max_reassignments=max_reassignments)


def test_picks_first_available_candidate(env):
    policy = _policy(env, {"backend_dev": ["qa_engineer", "architect"]})

    assert policy.pick(env["task"], supervisor="project_manager") == "qa_engineer"


def test_skips_unknown_candidate(env):
    policy = _policy(env, {"backend_dev": ["ghost", "architect"]})

    assert policy.pick(env["task"], supervisor="project_manager") == "architect"


def test_skips_current_role(env):
    """映射里写回当前角色是常见手误：换给同一个人等于没换。"""
    policy = _policy(env, {"backend_dev": ["backend_dev", "qa_engineer"]})

    assert policy.pick(env["task"], supervisor="project_manager") == "qa_engineer"


def test_skips_supervisor(env):
    """不换给主管自己：派活的人转身去执行，下轮就没人验收了。"""
    policy = _policy(env, {"backend_dev": ["project_manager"]})

    assert policy.pick(env["task"], supervisor="project_manager") is None


def test_empty_fallback_never_reassigns(env):
    assert _policy(env, {}).pick(env["task"], supervisor="x") is None
    assert _policy(env, {"other": ["architect"]}).pick(
        env["task"], supervisor="x") is None


def test_respects_reassignment_limit(env):
    policy = _policy(env, {"backend_dev": ["qa_engineer"]}, max_reassignments=1)
    already = env["task"].model_copy(update={"reassign_count": 1})

    assert policy.pick(already, supervisor="project_manager") is None


def test_zero_limit_disables(env):
    policy = _policy(env, {"backend_dev": ["qa_engineer"]}, max_reassignments=0)

    assert policy.pick(env["task"], supervisor="project_manager") is None


# ── 门禁集成 ──────────────────────────────────────────────────────────────


def _gatekeeper(env, reviews: dict, tmp_path: Path, *, supervisor=None,
                reassign=None) -> Gatekeeper:
    fixtures = tmp_path / "fx"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"],
                          fixtures_dir=fixtures)
    reviewer = Reviewer(models, env["catalog"], env["store"], env["ledger"])
    return Gatekeeper(
        reviewer, env["store"], env["board"], gate="none",
        ledger=env["ledger"], prompter=NullPrompter(),
        supervisor=supervisor, reassign=reassign,
    )


def _commit(env) -> None:
    env["store"].commit("a.py", "def f():\n    return 1\n",
                        spec=env["task"].deliverables[0], task=env["task"])


def _supervisor(env, reviewer) -> SupervisorReviewer:
    return SupervisorReviewer(reviewer, env["catalog"], env["ledger"])


async def test_supervisor_reject_reassigns_role(env, tmp_path):
    """主管驳回 + 有备选 → 换角色，仍走 REWORK（状态不变）。"""
    _commit(env)
    fake = _FakeReviewer(verdict="reject", issues=["没按约定命名"],
                         fix_hints=["把 fetch 改成 get"])
    gk = _gatekeeper(env, {"T-1": GOOD}, tmp_path,
                     supervisor=_supervisor(env, fake),
                     reassign=_policy(env, {"backend_dev": ["qa_engineer"]}))

    decision = await gk.evaluate(env["task"], round_no=1)

    assert decision.verdict == "reject"
    assert decision.next_status is S.REWORK
    task = env["board"].get("T-1")
    assert task.role == "qa_engineer"
    assert task.reassign_count == 1
    assert task.status is S.REWORK
    # 返工额度照样被消耗：换人不是免费加次数
    assert task.rework_count == 1
    events = list(env["ledger"].events("supervisor_reassigned"))
    assert events[0].data["from_role"] == "backend_dev"
    assert events[0].data["to_role"] == "qa_engineer"
    assert events[0].data["supervisor"] == "project_manager"


async def test_g2_reject_does_not_reassign(env, tmp_path):
    """G2 驳回不换人：换人是主管的判断，不是机器评审的。"""
    _commit(env)
    gk = _gatekeeper(env, {"T-1": BAD}, tmp_path,
                     supervisor=_supervisor(env, _FakeReviewer()),
                     reassign=_policy(env, {"backend_dev": ["qa_engineer"]}))

    decision = await gk.evaluate(env["task"], round_no=1)

    assert decision.next_status is S.REWORK
    assert env["board"].get("T-1").role == "backend_dev"
    assert env["board"].get("T-1").reassign_count == 0
    assert env["ledger"].count("supervisor_reassigned") == 0


async def test_no_reassign_when_quota_exhausted(env, tmp_path):
    """返工额度已用尽：直接 FAILED，不该先换人再失败。"""
    _commit(env)
    env["board"].get("T-1").rework_count = 2       # == max_reworks
    fake = _FakeReviewer(verdict="reject", issues=["不行"], fix_hints=["重做"])
    gk = _gatekeeper(env, {"T-1": GOOD}, tmp_path,
                     supervisor=_supervisor(env, fake),
                     reassign=_policy(env, {"backend_dev": ["qa_engineer"]}))

    decision = await gk.evaluate(env["task"], round_no=3)

    assert decision.next_status is S.FAILED
    assert env["board"].get("T-1").role == "backend_dev"
    assert env["ledger"].count("supervisor_reassigned") == 0


async def test_without_policy_behaviour_unchanged(env, tmp_path):
    """未注入策略（默认）：主管驳回仍只退回原执行者。"""
    _commit(env)
    fake = _FakeReviewer(verdict="reject", issues=["不行"], fix_hints=["重做"])
    gk = _gatekeeper(env, {"T-1": GOOD}, tmp_path,
                     supervisor=_supervisor(env, fake))

    decision = await gk.evaluate(env["task"], round_no=1)

    assert decision.next_status is S.REWORK
    assert env["board"].get("T-1").role == "backend_dev"
    assert env["board"].get("T-1").reassign_count == 0


def test_board_reassign_bumps_counter(env):
    updated = env["board"].reassign("T-1", "architect")

    assert updated.role == "architect"
    assert updated.reassign_count == 1
    assert env["board"].get("T-1").role == "architect"


def test_resolved_config_defaults(tmp_path, monkeypatch):
    """默认不换人：空映射 + 上限 1。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()

    assert settings.role_fallback == {}
    assert settings.max_supervisor_reassignments == 1
