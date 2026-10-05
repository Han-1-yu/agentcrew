"""TaskBoard 唯一事实源的行为测试（spec §6/§7.3，Task 8）。"""

from datetime import datetime

import pytest
from agentcrew.task.task import Task, TaskStatus as S, ReviewRecord, TaskResult
from agentcrew.task.task_board import TaskBoard
from agentcrew.task.state_machine import IllegalTransitionError

def mk(tid, deps=(), status=S.PENDING, priority=5):
    return Task(id=tid, title=tid, description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=[], dependencies=list(deps),
                status=status, priority=priority)

def test_transition_updates_status_and_history():
    b = TaskBoard([mk("A")])
    b.transition("A", S.READY, reason="deps met")
    assert b.get("A").status is S.READY
    h = b.get("A").status_history
    assert len(h) == 1 and h[0].from_status is S.PENDING and h[0].to_status is S.READY

def test_illegal_transition_does_not_mutate():
    b = TaskBoard([mk("A", status=S.SUCCEEDED)])
    with pytest.raises(IllegalTransitionError):
        b.transition("A", S.RUNNING)
    assert b.get("A").status is S.SUCCEEDED

def test_on_change_callback_fires():
    seen = []
    b = TaskBoard([mk("A")], on_change=lambda t, c: seen.append((t.id, c.to_status)))
    b.transition("A", S.READY)
    assert seen == [("A", S.READY)]

def test_refresh_promotes_ready_and_blocks_failed_downstream():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"]), mk("C", status=S.SUCCEEDED),
                   mk("D", ["C"])])
    b.refresh()
    assert b.get("B").status is S.BLOCKED
    assert b.get("D").status is S.READY

def test_refresh_unblocks_when_upstream_recovers():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"])])
    b.refresh(); assert b.get("B").status is S.BLOCKED
    b.get("A").status = S.SUCCEEDED           # 模拟人工修复
    b.refresh(); assert b.get("B").status is S.READY

def test_take_ready_orders_by_priority_then_id_and_marks_running():
    b = TaskBoard([mk("B", priority=1), mk("A", priority=1), mk("C", priority=9)])
    b.refresh()
    taken = b.take_ready(limit=2)
    assert [t.id for t in taken] == ["A", "B"]
    assert b.get("A").status is S.RUNNING and b.get("B").status is S.RUNNING
    assert b.get("C").status is S.READY

def test_bump_attempt_increments():
    b = TaskBoard([mk("A")])
    assert b.bump_attempt("A").attempts == 1

def test_bump_rework_increments_without_changing_status():
    b = TaskBoard([mk("A", status=S.REVIEW)])
    t = b.bump_rework("A")
    assert t.rework_count == 1 and t.status is S.REVIEW

def test_is_terminal_true_only_when_all_settled():
    b = TaskBoard([mk("A", status=S.SUCCEEDED), mk("B")])
    assert not b.is_terminal()
    b.transition("B", S.READY); b.transition("B", S.RUNNING)
    b.transition("B", S.REVIEW); b.transition("B", S.SUCCEEDED)
    assert b.is_terminal()

def test_stats_counts_by_status():
    b = TaskBoard([mk("A", status=S.SUCCEEDED), mk("B", status=S.FAILED),
                   mk("C", status=S.BLOCKED)])
    s = b.stats()
    assert (s.total_tasks, s.succeeded, s.failed, s.blocked) == (3, 1, 1, 1)

def test_explain_stall_mentions_blocking_upstream():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"])])
    b.refresh()
    text = b.explain_stall()
    assert "B" in text and "A" in text

def test_snapshot_roundtrip_preserves_state():
    b = TaskBoard([mk("A"), mk("B", ["A"], priority=2)])
    b.transition("A", S.READY); b.transition("A", S.RUNNING); b.bump_attempt("A")
    b.record_review("A", ReviewRecord(task_id="A", round=1, layer="static",
                                      verdict="approve", issues=[], fix_hints=[],
                                      reviewer="static:contract",
                                      created_at=__import__("datetime").datetime.now()))
    b2 = TaskBoard.from_snapshot(b.snapshot())
    assert b2.get("A").status is S.RUNNING
    assert b2.get("A").attempts == 1
    assert len(b2.get("A").reviews) == 1
    assert b2.get("B").priority == 2

def test_from_snapshot_rejects_unknown_status():
    with pytest.raises(Exception):
        TaskBoard.from_snapshot([{"id": "A", "title": "t", "description": "d",
                                  "role": "r", "acceptance_criteria": [],
                                  "deliverables": [], "status": "bogus"}])

def test_reset_failed_moves_to_ready_and_logs_reason():
    b = TaskBoard([mk("A", status=S.FAILED)])
    b.reset_failed("A")
    assert b.get("A").status is S.READY
    assert b.get("A").status_history[-1].reason == "manual-retry"


# ---- brief 之外的补充行为（控制器决议 1–13 中未被上面覆盖的部分）----

def test_transition_returns_new_object_and_leaves_old_reference_untouched():
    b = TaskBoard([mk("A")])
    old = b.get("A")
    new = b.transition("A", S.READY)
    assert new is not old
    assert b.get("A") is new
    assert old.status is S.PENDING and old.status_history == []

def test_get_returns_live_reference_stored_on_board():
    b = TaskBoard([mk("A")])
    assert b.get("A") is b.all()[0] is b.map()["A"]

def test_get_unknown_id_raises_keyerror():
    with pytest.raises(KeyError):
        TaskBoard([]).get("nope")

def test_refresh_returns_change_count_and_second_call_is_noop():
    b = TaskBoard([mk("A", status=S.SUCCEEDED), mk("B", ["A"]),
                   mk("C", status=S.FAILED), mk("D", ["C"])])
    assert b.refresh() == 2
    assert b.refresh() == 0

def test_refresh_skips_tasks_with_skipped_upstream():
    b = TaskBoard([mk("A", status=S.SKIPPED), mk("B", ["A"])])
    assert b.refresh() == 1
    assert b.get("B").status is S.SKIPPED

def test_refresh_blocks_before_skipping_when_dep_is_both():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", status=S.SKIPPED),
                   mk("C", ["A", "B"])])
    b.refresh()
    assert b.get("C").status is S.BLOCKED   # (i) 先于 (ii)：BLOCKED 胜出

def test_take_ready_refreshes_pending_tasks_first():
    b = TaskBoard([mk("A", status=S.SUCCEEDED), mk("B", ["A"])])
    taken = b.take_ready(limit=5)
    assert [t.id for t in taken] == ["B"]
    assert b.get("B").status is S.RUNNING

def test_take_ready_consumes_attempt_at_dispatch_time():
    b = TaskBoard([mk("A")])
    (taken,) = b.take_ready(limit=1)
    assert taken.attempts == 1 and taken.status is S.RUNNING
    assert b.get("A").attempts == 1

def test_take_ready_non_positive_limit_takes_nothing():
    b = TaskBoard([mk("A")])
    assert b.take_ready(limit=0) == []
    assert b.take_ready(limit=-3) == []
    assert b.get("A").status is S.READY and b.get("A").attempts == 0

def test_on_change_reports_refresh_then_dispatch_with_bumped_attempts():
    seen = []
    b = TaskBoard([mk("A")],
                  on_change=lambda t, c: seen.append((t.status, t.attempts, c.reason)))
    b.take_ready(limit=1)
    # take_ready 内部先 refresh（PENDING→READY），再派发（扣额度后 RUNNING）；
    # 两次回调拿到的都是落板后的新 Task。
    assert seen == [(S.READY, 0, "dependencies-met"),
                    (S.RUNNING, 1, "dispatched")]

def test_set_result_stores_result_without_changing_status():
    b = TaskBoard([mk("A", status=S.RUNNING)])
    now = datetime.now()
    result = TaskResult(task_id="A", role="backend_dev", summary="done",
                        artifacts=[], started_at=now, finished_at=now, duration_ms=12)
    b.set_result("A", result)
    assert b.get("A").result == result
    assert b.get("A").status is S.RUNNING

def test_stats_counts_skipped_and_leaves_cost_fields_at_defaults():
    b = TaskBoard([mk("A", status=S.SUCCEEDED), mk("B", status=S.SKIPPED),
                   mk("C", status=S.RUNNING)])
    s = b.stats()
    assert (s.total_tasks, s.succeeded, s.failed, s.blocked, s.skipped) == (3, 1, 0, 0, 1)
    assert (s.llm_calls, s.prompt_tokens, s.completion_tokens) == (0, 0, 0)
    assert s.estimated_cost_usd == 0.0

def test_explain_stall_uses_status_values_not_enum_repr():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"])])
    b.refresh()
    text = b.explain_stall()
    assert "blocked" in text and "failed" in text
    assert "TaskStatus." not in text

def test_explain_stall_reports_dangling_dependency():
    b = TaskBoard([mk("B", ["GHOST"])])
    text = b.explain_stall()
    assert "B" in text and "GHOST" in text

def test_from_snapshot_board_can_keep_transitioning():
    b = TaskBoard([mk("A")])
    b.transition("A", S.READY); b.transition("A", S.RUNNING)
    b2 = TaskBoard.from_snapshot(b.snapshot())
    b2.transition("A", S.REVIEW); b2.transition("A", S.SUCCEEDED)
    assert b2.is_terminal()
    assert len(b2.get("A").status_history) == 4

def test_reset_failed_rejects_task_that_is_not_failed():
    b = TaskBoard([mk("A", status=S.RUNNING)])
    with pytest.raises(ValueError):
        b.reset_failed("A")
    assert b.get("A").status is S.RUNNING


# ---- 失败传播的传递性（控制器复核发现的一级传播缺口，修复轮）----

def test_refresh_blocks_transitive_descendants_of_failed_task():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"]), mk("C", ["B"])])
    assert b.refresh() == 2          # B、C 各一条跃迁
    assert b.get("B").status is S.BLOCKED
    assert b.get("C").status is S.BLOCKED

def test_refresh_blocks_deep_chain_transitively():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"]), mk("C", ["B"]),
                   mk("D", ["C"])])
    b.refresh()
    assert all(b.get(t).status is S.BLOCKED for t in "BCD")

def test_refresh_does_not_resurrect_succeeded_descendants():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"]), mk("C", ["B"]),
                   mk("D", ["A"], status=S.SUCCEEDED)])
    b.refresh()
    assert b.get("C").status is S.BLOCKED        # 传递阻塞生效
    assert b.get("D").status is S.SUCCEEDED      # 已是终态的下游原样保留

def test_refresh_blocks_transitive_descendants_of_cancelled_task():
    b = TaskBoard([mk("A", status=S.CANCELLED), mk("B", ["A"]), mk("C", ["B"])])
    b.refresh()
    assert b.get("B").status is S.BLOCKED
    assert b.get("C").status is S.BLOCKED

def test_refresh_blocks_ready_task_when_upstream_is_failed():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"], status=S.READY)])
    b.refresh()
    assert b.get("B").status is S.BLOCKED     # READY → BLOCKED 是合法边

def test_transitively_blocked_chain_recovers_step_by_step():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"]), mk("C", ["B"])])
    b.refresh()
    assert b.get("C").status is S.BLOCKED
    b.reset_failed("A")
    for status in (S.RUNNING, S.REVIEW, S.SUCCEEDED):
        b.transition("A", status)
    b.refresh()
    assert b.get("B").status is S.READY         # 直接下游解除阻塞
    assert b.get("C").status is S.BLOCKED       # C 的依赖 B 只是 READY，尚不放行
    for status in (S.RUNNING, S.REVIEW, S.SUCCEEDED):
        b.transition("B", status)
    b.refresh()
    assert b.get("C").status is S.READY


# ---- REWORK 任务的重新派发（控制器复核 FINDING 2）----

def test_rework_task_is_redispatched_by_take_ready():
    b = TaskBoard([mk("A")])
    b.transition("A", S.READY); b.transition("A", S.RUNNING)
    b.transition("A", S.REVIEW); b.transition("A", S.REWORK)
    b.bump_rework("A")
    assert b.get("A").rework_count == 1
    taken = b.take_ready(limit=1)
    assert [t.id for t in taken] == ["A"]
    assert b.get("A").status is S.RUNNING
    assert b.get("A").status_history[-1].reason == "rework-dispatched"

def test_take_ready_mixes_ready_and_rework_by_priority_then_id():
    b = TaskBoard([mk("A", priority=5), mk("Z", status=S.REWORK, priority=1)])
    taken = b.take_ready(limit=1)
    assert [t.id for t in taken] == ["Z"]        # priority 1 先于 5
    assert b.get("Z").status is S.RUNNING
    assert b.get("A").status is S.READY          # 被 limit 截断者保持 READY

    b2 = TaskBoard([mk("A", priority=1), mk("Z", status=S.REWORK, priority=9)])
    (first,) = b2.take_ready(limit=1)
    assert first.id == "A"                       # priority 1 先于 9
    assert b2.get("A").status is S.RUNNING
    assert b2.get("Z").status is S.REWORK        # 被截断的 REWORK 保持 REWORK

def test_rework_dispatch_does_not_consume_attempts():
    b = TaskBoard([mk("A", status=S.REWORK), mk("B")])
    taken = b.take_ready(limit=5)
    assert [t.id for t in taken] == ["A", "B"]   # 同优先级按 id
    assert b.get("A").status is S.RUNNING and b.get("A").attempts == 0
    assert b.get("B").status is S.RUNNING and b.get("B").attempts == 1

def test_rework_loop_redispatches_then_reaches_succeeded():
    b = TaskBoard([mk("A")])
    b.transition("A", S.READY); b.transition("A", S.RUNNING)
    b.transition("A", S.REVIEW); b.transition("A", S.REWORK)
    b.bump_rework("A")
    (redispatched,) = b.take_ready(limit=1)
    assert redispatched.status is S.RUNNING
    b.transition("A", S.REVIEW)
    b.transition("A", S.SUCCEEDED)
    assert b.get("A").rework_count == 1 and b.get("A").attempts == 0
    assert b.is_terminal()
    assert b.take_ready(limit=5) == []

def test_refresh_leaves_rework_task_alone_until_dispatched():
    b = TaskBoard([mk("A", status=S.REWORK)])
    b.refresh()
    assert b.get("A").status is S.REWORK         # refresh 不移动 REWORK
    (taken,) = b.take_ready(limit=1)             # 只有派发才离开 REWORK
    assert taken.status is S.RUNNING

    # 上游事后 FAILED 的 REWORK 任务（能进入 REWORK 说明其上游曾 SUCCEEDED，
    # 合法历史不可达）：状态表没有 REWORK → BLOCKED 边，故 refresh 保持
    # REWORK，等 take_ready 重新派发。
    weird = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"], status=S.REWORK)])
    weird.refresh()
    assert weird.get("B").status is S.REWORK
