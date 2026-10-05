"""状态机跃迁表的行为测试（spec §6.1）。"""

import pytest
from agentcrew.task.task import TaskStatus as S
from agentcrew.task.state_machine import (
    IllegalTransitionError, ALLOWED_TRANSITIONS, can_transition,
    assert_transition, is_terminal,
)

def test_every_status_has_an_entry():
    assert set(ALLOWED_TRANSITIONS) == set(S)

@pytest.mark.parametrize("src,dst", [
    (S.PENDING, S.READY), (S.PENDING, S.BLOCKED), (S.PENDING, S.SKIPPED),
    (S.PENDING, S.CANCELLED),
    (S.READY, S.RUNNING), (S.READY, S.BLOCKED), (S.READY, S.CANCELLED),
    (S.RUNNING, S.REVIEW), (S.RUNNING, S.READY), (S.RUNNING, S.FAILED),
    (S.RUNNING, S.CANCELLED),
    (S.REVIEW, S.SUCCEEDED), (S.REVIEW, S.REWORK), (S.REVIEW, S.FAILED),
    (S.REVIEW, S.CANCELLED),
    (S.REWORK, S.RUNNING), (S.REWORK, S.CANCELLED),
    (S.BLOCKED, S.READY),
])
def test_legal_transitions_allowed(src, dst):
    assert can_transition(src, dst)
    assert_transition(src, dst, task_id="T-1")   # 不抛

@pytest.mark.parametrize("src,dst", [
    (S.SUCCEEDED, S.RUNNING), (S.FAILED, S.RUNNING), (S.CANCELLED, S.READY),
    (S.SKIPPED, S.READY), (S.PENDING, S.RUNNING), (S.PENDING, S.REVIEW),
    (S.READY, S.REVIEW), (S.REVIEW, S.READY), (S.RUNNING, S.SUCCEEDED),
    (S.REWORK, S.REVIEW), (S.BLOCKED, S.RUNNING),
])
def test_illegal_transitions_rejected(src, dst):
    assert not can_transition(src, dst)
    with pytest.raises(IllegalTransitionError) as ei:
        assert_transition(src, dst, task_id="T-9")
    assert ei.value.task_id == "T-9"
    assert ei.value.from_status is src and ei.value.to_status is dst

def test_terminal_states_have_no_outgoing():
    for s in (S.SUCCEEDED, S.CANCELLED, S.SKIPPED):
        assert ALLOWED_TRANSITIONS[s] == frozenset()
        assert is_terminal(s)

def test_failed_has_only_manual_retry_edge():
    # brief 原文的集合推导式断言「FAILED 不是任何非 FAILED 状态的终点」，
    # 与同一 brief 的合法清单 (RUNNING, FAILED)、(REVIEW, FAILED) 直接冲突，
    # 任何满足合法清单的跃迁表都无法让它通过。改按设计意图断言：
    # FAILED 的出边为空（人工重试走 TaskBoard.reset_failed()，不经状态机），
    # 只作为 RUNNING / REVIEW 的终点。
    assert ALLOWED_TRANSITIONS[S.FAILED] == frozenset()
    assert {s for s, ds in ALLOWED_TRANSITIONS.items() if S.FAILED in ds} == {
        S.RUNNING, S.REVIEW,
    }
    assert is_terminal(S.FAILED)
