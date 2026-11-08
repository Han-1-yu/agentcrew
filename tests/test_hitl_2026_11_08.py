"""P2-7：HITL 审批的幂等与恢复语义（把「已经对」的行为钉住）。

R4 给了审批单持久化 + 跨进程恢复，R30 给了等待窗口。但「重复裁决会怎样」「过期后
裁决会怎样」「进程重启后未裁决的单还在不在」这三件事此前只在 CLI 往返里被间接
覆盖，没有单独钉住。本文件补的就是这三条——它们是「审批单可以被信任」的全部理由：

1. **重复裁决被拒绝**（不是静默覆盖）。否则两个人同时点「批准」和「驳回」，最终
   结果取决于谁后写文件，而这正是审批最不能出的事。
2. **过期后裁决被拒绝**，且提示可操作（调大窗口 / 重新触发），不是一句「已 expired」。
3. **未裁决的单在进程重启后仍是 pending**：续跑不该把它丢掉，也不该替人裁决。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agentcrew.runtime.approvals import ApprovalError, ApprovalStore

# ── 1. 幂等：重复裁决 ──────────────────────────────────────────────────


def test_second_decision_is_rejected_and_first_is_kept(tmp_path):
    store = ApprovalStore(tmp_path)
    request = store.create("plan", context={"run": "R"}, run_id="R")
    store.decide(request.id, {"action": "approve"}, decided_by="alice")

    with pytest.raises(ApprovalError) as exc:
        store.decide(request.id, {"action": "reject"}, decided_by="bob")
    assert "不能重复裁决" in str(exc.value)

    # 关键：文件里留下的是第一份裁决，后来者没有覆盖它
    current = store.get(request.id)
    assert current.status == "decided"
    assert current.decision == {"action": "approve"}
    assert current.decided_by == "alice"


def test_decided_request_is_not_listed_as_pending(tmp_path):
    store = ApprovalStore(tmp_path)
    # delivery 卡点的合法 action 是 accept / abort / rework（plan 才是 approve）
    request = store.create("delivery", run_id="R")
    assert [item.id for item in store.pending()] == [request.id]
    store.decide(request.id, {"action": "accept"})
    assert store.pending() == []


def test_consumed_request_cannot_be_decided_again(tmp_path):
    """resume 消费掉一张单之后，再裁决必须被拒（否则同一张单会被执行两次）。"""
    store = ApprovalStore(tmp_path)
    request = store.create("plan", run_id="R")
    store.decide(request.id, {"action": "approve"})
    store.consume(request.id)
    with pytest.raises(ApprovalError) as exc:
        store.decide(request.id, {"action": "approve"})
    assert "已 consumed" in str(exc.value)


# ── 2. 过期 ────────────────────────────────────────────────────────────


def test_decide_after_expiry_is_rejected_with_actionable_hint(tmp_path):
    store = ApprovalStore(tmp_path)
    request = store.create("plan", run_id="R", timeout_seconds=60)
    later = datetime.now() + timedelta(hours=2)
    expired = store.expire_overdue(now=later)
    assert [item.id for item in expired] == [request.id]

    with pytest.raises(ApprovalError) as exc:
        store.decide(request.id, {"action": "approve"})
    message = str(exc.value)
    assert "已过期" in message
    # 提示要能照着办，而不是只说「不行」
    assert "approval_timeout_seconds" in message


def test_expiry_only_touches_pending_requests(tmp_path):
    store = ApprovalStore(tmp_path)
    decided = store.create("plan", run_id="R", timeout_seconds=1)
    store.decide(decided.id, {"action": "approve"})
    other = store.create("delivery", run_id="R", timeout_seconds=1)
    expired = store.expire_overdue(now=datetime.now() + timedelta(hours=1))
    assert [item.id for item in expired] == [other.id]
    assert store.get(decided.id).status == "decided"


def test_no_timeout_means_never_expires(tmp_path):
    """``timeout_seconds=0``（默认）= 永不超时，行为与 R30 之前一致。"""
    store = ApprovalStore(tmp_path)
    request = store.create("plan", run_id="R")
    assert request.expires_at is None
    assert store.expire_overdue(now=datetime.now() + timedelta(days=30)) == []
    store.decide(request.id, {"action": "approve"})
    assert store.get(request.id).status == "decided"


# ── 3. 重启 ────────────────────────────────────────────────────────────


def test_pending_survives_process_restart(tmp_path):
    """新实例（模拟进程重启）读到的仍是同一张 pending 单。"""
    first = ApprovalStore(tmp_path)
    request = first.create("step", run_id="R", context={"task": "T-1"})

    second = ApprovalStore(tmp_path)
    pending = second.pending()
    assert [item.id for item in pending] == [request.id]
    assert pending[0].context == {"task": "T-1"}
    # 重启后裁决走同一条路径
    second.decide(request.id, {"action": "step"}, decided_by="carol")
    assert ApprovalStore(tmp_path).get(request.id).status == "decided"


def test_corrupt_tail_line_does_not_lose_history(tmp_path):
    """追加日志被强杀截断半行时，前面已落盘的审批单不能丢。"""
    store = ApprovalStore(tmp_path)
    request = store.create("plan", run_id="R")
    with open(store.path, "a", encoding="utf-8") as handle:
        handle.write('{"op": "decide", "id": "AP-trunc')   # 半行
    assert ApprovalStore(tmp_path).get(request.id) is not None
