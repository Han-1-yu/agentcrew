"""R30：审批单超时与告警——单张过期、批量结算、裁决拒绝、ledger 留痕。

核心契约：**过期不改 run 结局**（仍 interrupted），只是「这张单不能再用了」；
过期结算必须留下 ``approval_expired`` 事件，否则没人知道有人等了一周白等。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.runtime.approvals import ApprovalError, ApprovalStore
from agentcrew.runtime.checkpoint import CheckpointStore


def _store(tmp_path: Path) -> ApprovalStore:
    run_dir = tmp_path / "20260101-000000-aaaa"
    run_dir.mkdir(parents=True, exist_ok=True)
    return ApprovalStore(run_dir)


def test_create_without_timeout_has_no_expiry(tmp_path):
    store = _store(tmp_path)
    request = store.create("plan", context={"task_ids": ["T-001"]})
    assert request.expires_at is None
    assert store.is_expired(request) is False      # 永不超时


def test_create_with_timeout_sets_expiry(tmp_path):
    store = _store(tmp_path)
    request = store.create("plan", timeout_seconds=600)
    assert request.expires_at is not None
    delta = request.expires_at - request.created_at
    assert 590 <= delta.total_seconds() <= 610
    assert store.is_expired(request) is False
    assert store.is_expired(request, now=request.expires_at) is True


def test_expire_overdue_marks_pending_only_once(tmp_path):
    store = _store(tmp_path)
    request = store.create("delivery", timeout_seconds=60)
    later = request.expires_at + timedelta(seconds=1)

    expired = store.expire_overdue(now=later)
    assert [item.id for item in expired] == [request.id]
    assert expired[0].status == "expired"
    assert expired[0].expired_at is not None
    assert store.expire_overdue(now=later) == []   # 幂等：不再重复结算

    # 已裁决 / 已消费的单不会被过期逻辑改写
    decided = store.create("plan", timeout_seconds=60)
    store.decide(decided.id, {"action": "approve"}, decided_by="alice")
    assert store.expire_overdue(now=decided.expires_at + timedelta(days=1)) == []
    assert store.get(decided.id).status == "decided"


def test_decide_rejects_expired_request(tmp_path):
    store = _store(tmp_path)
    request = store.create("plan", timeout_seconds=60)
    store.expire_overdue(now=request.expires_at + timedelta(seconds=1))
    with pytest.raises(ApprovalError, match="已过期"):
        store.decide(request.id, {"action": "approve"})


def test_approval_timeout_config_channel(monkeypatch):
    monkeypatch.setenv("AGENTCREW_APPROVAL_TIMEOUT_SECONDS", "900")
    assert Settings.load().approval_timeout_seconds == 900


def test_startup_scan_emits_approval_expired(tmp_path, capsys):
    """`_expire_overdue_approvals` 把过期单标记 + 写 run 级 ledger 事件。"""
    import main as cli

    settings = Settings.load(output_dir=tmp_path,
                             approval_timeout_seconds=1)
    store = CheckpointStore.create(
        tmp_path, project_goal="审批超时", config_snapshot={})
    approvals = ApprovalStore(store.run_dir)
    request = approvals.create("plan", timeout_seconds=1)

    # 把 create 行的 expires_at 改成过去（等价于「这张单等了一天」）
    path = approvals.path
    lines = [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    past = (datetime.now() - timedelta(days=1)).isoformat()
    lines[0]["expires_at"] = past
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False)
                              for item in lines) + "\n", encoding="utf-8")

    count = cli._expire_overdue_approvals(settings)
    assert count == 1
    assert approvals.get(request.id).status == "expired"
    assert "已超时" in capsys.readouterr().err

    events = [json.loads(line) for line in
              (store.run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip()]
    expired_events = [item for item in events
                      if item.get("event") == "approval_expired"]
    assert expired_events and expired_events[0]["data"]["request_id"] == request.id
