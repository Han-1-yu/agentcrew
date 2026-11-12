"""批次 7 回归：B-6 / B-7 / B-9 / B-21 / B-22 / A-16。

每条都对应一个「看起来能用、实际有洞」的边界，所以断言写成**负向**的：
错误输入必须被拒绝，而不是正确输入能通过。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentcrew.api.auth import APIKeyAuth
from agentcrew.api.rbac import (
    SCOPE_RUNS_READ,
    required_scope,
)
from agentcrew.api.server import ApiServer
from agentcrew.config import Settings
from agentcrew.runtime.approvals import ApprovalError, _validate_decision
from agentcrew.utils.files import tree


class TestB6ConstantTimeCompare:
    def _headers(self, key: str) -> dict[str, str]:
        return {"authorization": f"Bearer {key}"}

    def test_correct_key_passes(self):
        auth = APIKeyAuth(["k-aaa", "k-bbb"])
        assert auth.authenticate(self._headers("k-bbb")) is True

    def test_wrong_key_rejected(self):
        auth = APIKeyAuth(["k-aaa"])
        assert auth.authenticate(self._headers("k-aa")) is False
        assert auth.authenticate(self._headers("k-aaa-x")) is False

    def test_missing_key_rejected(self):
        auth = APIKeyAuth(["k-aaa"])
        assert auth.authenticate({}) is False

    def test_empty_key_list_disables_auth(self):
        auth = APIKeyAuth([])
        assert auth.authenticate({}) is True

    def test_non_ascii_key_does_not_raise(self):
        """compare_digest 对 str 只支持 ASCII；走 bytes 后中文 key 也能比。"""
        auth = APIKeyAuth(["密钥-中文"])
        assert auth.authenticate(self._headers("密钥-中文")) is True
        assert auth.authenticate(self._headers("密钥-中文x")) is False

    def test_x_api_key_header_still_works(self):
        auth = APIKeyAuth(["k-aaa"])
        assert auth.authenticate({"x-api-key": "k-aaa"}) is True


class TestB7WebhookBounds:
    def test_retries_upper_bound(self):
        with pytest.raises(ValidationError):
            Settings(webhook_max_retries=1_000_000)

    def test_retries_lower_bound(self):
        with pytest.raises(ValidationError):
            Settings(webhook_max_retries=-1)

    def test_timeout_upper_bound(self):
        with pytest.raises(ValidationError):
            Settings(webhook_timeout_seconds=3600)

    def test_timeout_must_be_positive(self):
        with pytest.raises(ValidationError):
            Settings(webhook_timeout_seconds=0)

    def test_defaults_still_valid(self):
        s = Settings()
        assert s.webhook_max_retries == 2
        assert s.webhook_timeout_seconds == 5.0


class TestB9RunRegistry:
    def test_empty_run_id_does_not_raise(self):
        """B-9：run_id 为空 + task_transition 此前 KeyError → 500。"""
        server = ApiServer(port=0)
        server.on_event("task_transition", {"run_id": "", "task_id": "T-1"})
        assert "" not in server.runs

    def test_missing_run_id_does_not_raise(self):
        server = ApiServer(port=0)
        server.on_event("run_started", {"goal": "x"})
        assert server.runs == {}

    def test_registry_has_upper_bound(self, monkeypatch):
        server = ApiServer(port=0)
        monkeypatch.setattr(ApiServer, "MAX_TRACKED_RUNS", 2, raising=False)
        for i in range(5):
            server.on_event("run_started", {"run_id": f"R-{i}"})
        assert len(server.runs) == 2

    def test_existing_run_still_updates_after_cap(self, monkeypatch):
        """到达上限后，**已注册**的 run 仍要能更新状态（只拒绝新 run）。"""
        server = ApiServer(port=0)
        monkeypatch.setattr(ApiServer, "MAX_TRACKED_RUNS", 1, raising=False)
        server.on_event("run_started", {"run_id": "R-1"})
        server.on_event("run_finished", {"run_id": "R-1", "status": "completed"})
        assert server.runs["R-1"].status == "completed"

    def test_task_transition_recorded(self):
        server = ApiServer(port=0)
        server.on_event("task_transition",
                        {"run_id": "R-1", "task_id": "T-1", "to_status": "running"})
        assert server.runs["R-1"].tasks["T-1"]["status"] == "running"


class TestB21AgentsScope:
    def test_get_agents_requires_read_scope(self):
        """B-21：此前返回 None → Principal.allows(None) 恒 True。"""
        assert required_scope("GET", "/runs/R-1/agents") == SCOPE_RUNS_READ

    def test_post_agents_still_admin(self):
        scope = required_scope("POST", "/runs/R-1/agents")
        assert scope is not None and scope != SCOPE_RUNS_READ

    def test_unaffected_route_unchanged(self):
        assert required_scope("GET", "/runs/R-1/status") == SCOPE_RUNS_READ


class TestB22DecisionWhitelist:
    def test_unknown_fields_are_dropped(self):
        """B-22：额外字段不透传落盘（防污染审计记录）。"""
        out = _validate_decision("plan", {"action": "approve", "injected": "<script>"})
        assert out == {"action": "approve"}

    def test_rework_keeps_task_ids(self):
        out = _validate_decision("delivery", {"action": "rework", "task_ids": ["T-1"]})
        assert out == {"action": "rework", "task_ids": ["T-1"]}

    def test_extra_fields_dropped_even_with_task_ids(self):
        out = _validate_decision(
            "delivery", {"action": "rework", "task_ids": ["T-1"], "x": 1})
        assert "x" not in out

    def test_invalid_action_rejected(self):
        with pytest.raises(ApprovalError):
            _validate_decision("plan", {"action": "nope"})

    def test_rework_without_task_ids_rejected(self):
        with pytest.raises(ApprovalError):
            _validate_decision("delivery", {"action": "rework"})

    def test_non_dict_rejected(self):
        with pytest.raises(ApprovalError):
            _validate_decision("plan", ["approve"])


class TestA16TreeResilience:
    def test_vanished_child_is_skipped(self, tmp_path: Path, monkeypatch):
        """A-16：并发删除导致 is_dir/is_file 抛 OSError 时，只跳过该条目。"""
        (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
        (tmp_path / "gone.txt").write_text("x", encoding="utf-8")

        real_is_file = Path.is_file

        def flaky_is_file(self):
            if self.name == "gone.txt":
                raise OSError("vanished")
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", flaky_is_file)
        entries = tree(tmp_path)
        assert entries == ["keep.txt"]

    def test_normal_tree_unaffected(self, tmp_path: Path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b.txt").write_text("x", encoding="utf-8")
        assert tree(tmp_path) == ["a/b.txt"]
