"""P2-5：门禁配置化。

钉：

- ``gate_config`` 各键（g1_enabled / g2_enabled / g2_reviewers /
  convergence_threshold）真正改变 Gatekeeper 行为；
- ``gate_config=None`` 时行为与原来完全一致（向后兼容）；
- ``Settings.gate_config`` 字段能从 YAML 加载并进 ``config_snapshot``。
"""

import json
from pathlib import Path

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.gate.gatekeeper import Gatekeeper
from agentcrew.gate.reviewer import Reviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard
from agentcrew.task.task import TaskStatus as S

GOOD = {"judgements": [{"criterion_index": 0, "satisfied": True, "evidence": "ok"}],
        "suggestions": [], "score": 0.9}
BAD = {"judgements": [{"criterion_index": 0, "satisfied": False,
                       "evidence": "补充业务规则"}],
       "suggestions": [], "score": 0.2}


def _board(task):
    board = TaskBoard([task])
    board.transition(task.id, S.READY)
    board.transition(task.id, S.RUNNING)
    board.transition(task.id, S.REVIEW)
    return board


def _setup(tmp_path, monkeypatch, *, role="backend_dev", verdict=GOOD,
           content="def f():\n    return 1\n", must_contain=None):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=lg)
    task = Task(id="T-1", title="t", description="d", role=role,
                acceptance_criteria=["a"],
                deliverables=[ArtifactSpec(path="a.py", kind="python", description="d",
                                           must_contain=must_contain or [])])
    if content is not None:
        store.commit("a.py", content, spec=task.deliverables[0], task=task)
    board = _board(task)
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    (fx / "reviews.json").write_text(json.dumps({"T-1": verdict}), encoding="utf-8")
    models = ModelFactory(s, ledger=lg, fixtures_dir=fx)
    rev = Reviewer(models, RoleCatalog(BUILTIN_ROLES), store, lg)
    return dict(rev=rev, lg=lg, board=board, task=task, s=s, store=store)


def _gk(env, **cfg):
    return Gatekeeper(env["rev"], env["store"], env["board"], gate="none",
                      ledger=env["lg"], prompter=None, gate_config=cfg or None)


async def test_g1_disabled_skips_static_check(tmp_path, monkeypatch):
    """g1_enabled=False：缺产物也不被 G1 拦，直接放行到 G2。"""
    # 不提交任何产物：G1 开启时会因「缺少必需产物」reject；关闭后应放行
    env = _setup(tmp_path, monkeypatch, content=None)
    gk = _gk(env, g1_enabled=False)
    d = await gk.evaluate(env["task"], round_no=1)
    # 缺产物本应被 G1 驳回；既然 approve，说明 G1 被跳过了
    assert d.verdict == "approve" and d.next_status is S.SUCCEEDED
    # G1 被停用：没有 g1_extended_check 事件（run_static + 扩展检查整体没跑）
    assert env["lg"].count("g1_extended_check") == 0


async def test_g2_disabled_skips_llm_review(tmp_path, monkeypatch):
    """g2_enabled=False：G1 通过后不跑 LLM 评审，直接放行。"""
    env = _setup(tmp_path, monkeypatch)
    gk = _gk(env, g2_enabled=False)
    d = await gk.evaluate(env["task"], round_no=1)
    assert d.verdict == "approve" and d.next_status is S.SUCCEEDED
    # 没有任何 llm 层 gate_result
    llm_events = [e for e in env["lg"].events("gate_result")
                   if e.data.get("layer") == "llm"]
    assert llm_events == []


async def test_g2_reviewers_two_uses_both_reviewers(tmp_path, monkeypatch):
    """project_manager 有两位评审人；g2_reviewers=2 时聚合两人意见。"""
    env = _setup(tmp_path, monkeypatch, role="project_manager")
    gk = _gk(env, g2_reviewers=2)
    d = await gk.evaluate(env["task"], round_no=1)
    assert d.verdict == "approve"
    # 两人都跑 → reviewer 字段是两位的拼接
    assert "+" in d.record.reviewer
    assert set(d.record.reviewer.split("+")) == {"architect", "product_manager"}


async def test_g2_reviewers_one_limits_to_single(tmp_path, monkeypatch):
    """g2_reviewers=1：project_manager 只跑一位评审人。"""
    env = _setup(tmp_path, monkeypatch, role="project_manager")
    gk = _gk(env, g2_reviewers=1)
    d = await gk.evaluate(env["task"], round_no=1)
    assert d.verdict == "approve"
    assert d.record.reviewer == "architect"   # 目录顺序第一个


async def test_convergence_threshold_from_config(tmp_path, monkeypatch):
    """convergence_threshold=1：首轮 LLM 驳回即收敛放行（默认 2 首轮不收敛）。"""
    env = _setup(tmp_path, monkeypatch, verdict=BAD)
    gk = _gk(env, convergence_threshold=1)
    d = await gk.evaluate(env["task"], round_no=1)
    # 阈值=1：首轮即判收敛 → 放行
    assert d.verdict == "approve" and d.next_status is S.SUCCEEDED
    assert d.record.reviewer.endswith(":converged")


async def test_gate_config_none_keeps_legacy_behavior(tmp_path, monkeypatch):
    """gate_config=None：首轮 LLM 驳回正常 reject（收敛阈值回落默认 2）。"""
    env = _setup(tmp_path, monkeypatch, verdict=BAD)
    gk = _gk(env)   # gate_config=None
    d = await gk.evaluate(env["task"], round_no=1)
    assert d.verdict == "reject" and d.next_status is S.REWORK
    assert not d.record.reviewer.endswith(":converged")


# ── Settings.gate_config 字段 ──────────────────────────────────────────────


def test_gate_config_field_defaults_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    s = Settings.load()
    assert s.gate_config == {}


def test_gate_config_loads_from_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    yaml_path = tmp_path / "agentcrew.yaml"
    yaml_path.write_text(
        "gate_config:\n"
        "  g1_enabled: false\n"
        "  g2_reviewers: 2\n"
        "  convergence_threshold: 3\n",
        encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(yaml_path))
    s = Settings.load()
    assert s.gate_config["g1_enabled"] is False
    assert s.gate_config["g2_reviewers"] == 2
    assert s.gate_config["convergence_threshold"] == 3


def test_gate_config_in_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    s = Settings(gate_config={"g2_reviewers": 2, "g1_enabled": True})
    snap = s.config_snapshot()
    assert snap["gate_config"]["g2_reviewers"] == 2
    assert snap["gate_config"]["g1_enabled"] is True
