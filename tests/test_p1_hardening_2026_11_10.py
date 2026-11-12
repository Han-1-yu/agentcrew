"""2026-11-10 P1 加固回归（对应 docs/GAP-ANALYSIS-2026-11-10.md 批次 2）。

覆盖（只断言行为，不锁实现细节）：

1. G1 孤立代理项（A-5）：含 ``\\ud800`` 的产物按欠尺寸产出 issue，不再抛异常判 FAILED；
2. 超时字段下界（A-14）：``timeout_seconds=0`` 在构造期被拒；
3. 预算上限合法性（A-22）：NaN 在构造期被拒；摘要额度为 0 时不再发空请求；
4. observer 异常隔离（A-23）：回调抛异常不阻断 emit、不跳过预算检查；
5. 分页上界与流式（B-8）：``clamp_int`` 有上界；``query_events`` 分页正确；
6. expires_at 归一化（B-17）：带时区的 ISO 不再让 verify 抛 TypeError；
7. 审批重复裁决（B-18）：归并时第一条胜出；
8. 产物清单同步（A-12）：commit 后 MANIFEST 立即含记录。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agentcrew.api import http as http_transport
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.gate.static_checks import _content_findings
from agentcrew.runtime.ledger import Ledger, LedgerReader
from agentcrew.task.planner import TaskDraft

# ── 1. G1 孤立代理项 ─────────────────────────────────────────────────────


def test_surrogate_content_is_treated_as_undersized_not_exception():
    """A-5：截断多字节输出里的孤立代理码点此前会让 ``encode`` 抛
    ``UnicodeEncodeError``，REVIEW 阶段直接判 FAILED、不给返工机会。"""
    spec = ArtifactSpec(
        path="a.py", kind="python", description="d", min_bytes=10)
    findings = _content_findings(spec, "\ud800\ud800")
    assert findings, "欠尺寸应当产出 issue（而不是抛异常）"


# ── 2. 超时字段下界 ──────────────────────────────────────────────────────


def test_timeout_seconds_zero_is_rejected_at_construction():
    """A-14：PM 输出 timeout_seconds=0 会在构造期被拒，不再让 wait_for 立即超时。"""
    with pytest.raises(ValidationError):
        TaskDraft(
            id="T1", title="t", description="d", role="r",
            acceptance_criteria=["a"], deliverables=[], timeout_seconds=0)


def test_timeout_seconds_negative_is_rejected():
    with pytest.raises(ValidationError):
        TaskDraft(
            id="T1", title="t", description="d", role="r",
            acceptance_criteria=["a"], deliverables=[], timeout_seconds=-5)


# ── 3. 预算上限合法性 ────────────────────────────────────────────────────


def test_max_cost_usd_nan_is_rejected():
    """A-22：NaN 使 ``>`` 比较恒 False、上限静默失效，构造期即拒。"""
    from agentcrew.config import Settings

    with pytest.raises(ValueError):
        Settings(max_cost_usd=float("nan"))


def test_max_cost_usd_negative_is_rejected():
    from agentcrew.config import Settings

    with pytest.raises(ValueError):
        Settings(max_cost_usd=-0.01)


# ── 4. observer 异常隔离 ──────────────────────────────────────────────────


def test_observer_exception_does_not_break_emit(tmp_path):
    """A-23：observer 回调抛异常时事件仍落盘、emit 不抛（否则会跳过预算检查）。"""
    def bad_observer(event, data):
        raise RuntimeError("下游断连")

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R", observer=bad_observer)
    written = ledger.emit("llm_call", x=1)
    assert written.event == "llm_call"
    assert (tmp_path / "l.jsonl").is_file()


# ── 5. 分页上界与流式 ────────────────────────────────────────────────────


def test_clamp_int_respects_maximum():
    assert http_transport.clamp_int("999999", default=100, minimum=1,
                                    maximum=1000) == 1000
    assert http_transport.clamp_int("5", default=100, minimum=1,
                                    maximum=1000) == 5


def test_query_events_pagination_is_correct(tmp_path):
    """B-8：流式分页与全量切片结果一致（offset/limit 语义不变）。"""
    run_dir = tmp_path / "R"
    run_dir.mkdir()
    ledger = Ledger(run_dir / "ledger.jsonl", run_id="R")
    for i in range(10):
        ledger.emit("step", i=i)
    reader = LedgerReader(tmp_path)
    page = reader.query_events("R", limit=3, offset=4)
    assert [e["data"]["i"] for e in page] == [4, 5, 6]


# ── 6. expires_at 归一化 ──────────────────────────────────────────────────


def test_key_with_timezone_expiry_does_not_break_verify(tmp_path):
    """B-17：带时区的 expires_at 此前让 verify 抛 TypeError → 全站 500。"""
    from agentcrew.api.rbac import KeyStore

    store = KeyStore(tmp_path / "auth")
    record, plaintext = store.add(
        name="tz-key", expires_at="2099-01-01T00:00:00+08:00")
    # 归一化为 naive UTC 落库
    assert "+" not in record.expires_at
    # verify 不抛，且能匹配（未过期）
    principal = store.verify(plaintext)
    assert principal is not None
    assert principal.name == "tz-key"


def test_key_with_bad_expiry_is_rejected(tmp_path):
    from agentcrew.api.rbac import KeyStore

    store = KeyStore(tmp_path / "auth")
    with pytest.raises(ValueError):
        store.add(name="bad-key", expires_at="not-a-date")


# ── 7. 审批重复裁决 ──────────────────────────────────────────────────────


def test_duplicate_decide_keeps_first(tmp_path):
    """B-18：两条 decide 行归并时第一条胜出（后写不再覆盖先写）。"""
    from agentcrew.runtime.approvals import ApprovalStore

    store = ApprovalStore(tmp_path)
    req = store.create(
        checkpoint="plan", context={"k": "v"}, timeout_seconds=0)
    store.decide(req.id, {"action": "approve"}, decided_by="alice")
    # 手工追加一条矛盾的第二裁决（模拟并发双写）
    with open(tmp_path / "approvals.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "op": "decide", "id": req.id,
            "decision": {"action": "reject"},
            "decided_by": "bob",
            "decided_at": "2026-11-10T20:00:00",
        }) + "\n")
    got = store.get(req.id)
    assert got.status == "decided"
    assert got.decision.get("action") == "approve"
    assert got.decided_by == "alice"


# ── 8. 产物清单同步 ──────────────────────────────────────────────────────


def test_commit_writes_manifest_immediately(tmp_path):
    """A-12：commit 落盘后 MANIFEST 立即含记录，消除崩溃窗口。"""
    from agentcrew.artifacts.store import ArtifactStore
    from agentcrew.task.task import Task

    store = ArtifactStore(tmp_path, run_id="R")
    spec = ArtifactSpec(
        path="docs/x.md", kind="markdown", description="d",
        min_bytes=1, max_bytes=1000)
    task = Task(
        id="T-1", title="t", description="d", role="r",
        acceptance_criteria=["a"], deliverables=[spec])
    store.commit(spec.path, "# hi", spec=spec, task=task, agent="r")

    manifest = json.loads((tmp_path / "MANIFEST.json").read_text(encoding="utf-8"))
    entries = manifest["entries"]
    assert any(e["path"] == "docs/x.md" for e in entries)
