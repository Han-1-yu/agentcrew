from datetime import datetime
from pathlib import Path

from rich.console import Console

from agentcrew.runtime.board_view import BoardView, STATUS_STYLE
from agentcrew.runtime.checkpoint import RunRecord, RunStatus
from agentcrew.runtime.ledger import LedgerEvent
from agentcrew.task.task import Task, TaskStatus as S, RunStats
from agentcrew.task.task_board import TaskBoard


def mk(tid, status=S.PENDING, role="backend_dev"):
    return Task(id=tid, title=f"任务{tid}", description="d", role=role,
                acceptance_criteria=["a"], deliverables=[], status=status)


def ev(name, **data):
    return LedgerEvent(ts=datetime.now(), run_id="R", event=name, data=data)


def render_text(renderable) -> str:
    """把任意 rich 可渲染对象转成纯文本，便于断言。"""
    console = Console(width=220, no_color=True)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def test_status_style_covers_all_ten_states():
    assert set(STATUS_STYLE) == {s.value for s in S}


def test_render_contains_task_ids():
    view = BoardView("R", "目标", enabled=False)
    board = TaskBoard([mk("T-001"), mk("T-002", S.SUCCEEDED)])
    printed = render_text(view.render(board))
    assert "T-001" in printed and "T-002" in printed


def test_render_shows_goal_and_run_id():
    view = BoardView("R-42", "做一个 Todo 应用", enabled=False)
    printed = render_text(view.render(TaskBoard([mk("T-1")])))
    assert "R-42" in printed and "做一个 Todo 应用" in printed


def test_render_shows_progress_count():
    board = TaskBoard([mk("A", S.SUCCEEDED), mk("B", S.PENDING)])
    printed = render_text(BoardView("R", "g", enabled=False).render(board))
    assert "1/2" in printed


def test_render_shows_role_and_status():
    board = TaskBoard([mk("A", S.REWORK, role="qa_engineer")])
    printed = render_text(BoardView("R", "g", enabled=False).render(board))
    assert "qa_engineer" in printed


def test_render_works_on_empty_board():
    assert render_text(BoardView("R", "g", enabled=False).render(TaskBoard([])))


def test_on_event_does_not_raise_for_unknown_event():
    BoardView("R", "g", enabled=False).on_event(ev("totally_unknown", x=1))


def test_on_event_tracks_last_event():
    v = BoardView("R", "g", enabled=False)
    v.on_event(ev("task_transition", task_id="T-1", **{"from": "ready", "to": "running"}))
    assert "T-1" in v.last_event_text


def test_disabled_view_is_silent(capsys):
    v = BoardView("R", "g", enabled=False)
    v.start()
    v.on_event(ev("run_started"))
    v.stop()
    assert capsys.readouterr().out == ""


def test_print_report_contains_counts(capsys):
    v = BoardView("R", "g", enabled=True)
    rec = RunRecord(run_id="R", project_goal="g", status=RunStatus.COMPLETED,
                    created_at=datetime.now(), updated_at=datetime.now(),
                    config_snapshot={},
                    stats=RunStats(total_tasks=3, succeeded=2, failed=1,
                                   llm_calls=7, estimated_cost_usd=0.42))
    board = TaskBoard([mk("A", S.SUCCEEDED), mk("B", S.FAILED)])
    v.print_report(rec, board, artifacts_dir=Path("/tmp/art"))
    out = capsys.readouterr().out
    assert "3" in out and "0.42" in out and "A" in out and "B" in out


def test_print_report_lists_failed_task_issues(capsys):
    v = BoardView("R", "g", enabled=True)
    rec = RunRecord(run_id="R", project_goal="g", status=RunStatus.FAILED,
                    created_at=datetime.now(), updated_at=datetime.now(),
                    config_snapshot={}, stats=RunStats(total_tasks=1, failed=1))
    t = mk("T-9", S.FAILED)
    from agentcrew.task.task import ReviewRecord
    t.reviews.append(ReviewRecord(task_id="T-9", round=3, layer="llm",
                                  verdict="reject", issues=["覆盖率不足"],
                                  fix_hints=[], reviewer="architect",
                                  created_at=datetime.now()))
    v.print_report(rec, TaskBoard([t]), artifacts_dir=Path("/tmp/art"))
    assert "覆盖率不足" in capsys.readouterr().out


# ---- 以下为分辨率（resolutions）钉死的边界行为 ----


def test_render_covers_every_status_without_key_error():
    """十个状态各一行：任何缺失的样式条目都会在渲染中段炸掉看板。"""
    statuses = list(S)
    board = TaskBoard([mk(f"T-{i:02d}", status) for i, status in enumerate(statuses)])
    printed = render_text(BoardView("R", "g", enabled=False).render(board))
    for i, status in enumerate(statuses):
        assert f"T-{i:02d}" in printed
        symbol, _ = STATUS_STYLE[status.value]
        assert symbol in printed


def test_unknown_event_keeps_previous_last_event_text():
    """向前兼容：llm_retry 等表外事件不改写最近事件，也绝不抛。"""
    v = BoardView("R", "g", enabled=False)
    v.on_event(
        ev("task_transition", task_id="T-1", **{"from": "ready", "to": "running"})
    )
    before = v.last_event_text
    v.on_event(ev("llm_retry", attempt=1, delay_seconds=2.0, error="boom"))
    assert v.last_event_text == before


def test_print_report_omits_failure_section_when_no_failures(capsys):
    rec = RunRecord(run_id="R", project_goal="g", status=RunStatus.COMPLETED,
                    created_at=datetime.now(), updated_at=datetime.now(),
                    config_snapshot={}, stats=RunStats(total_tasks=1, succeeded=1))
    board = TaskBoard([mk("A", S.SUCCEEDED)])
    BoardView("R", "g", enabled=True).print_report(
        rec, board, artifacts_dir=Path("/tmp/art"))
    assert "失败任务" not in capsys.readouterr().out


def test_start_degrades_to_line_logging_on_non_tty():
    """非 TTY（CI / 管道）不启动 Live，退化为逐行日志且不抛。"""
    import io

    stream = io.StringIO()
    console = Console(file=stream, width=80, no_color=True)
    v = BoardView("R", "g", console=console)
    v.start()
    v.on_event(ev("run_started", goal="g"))
    v.stop()
    assert "运行开始" in stream.getvalue()


def test_render_treats_task_data_as_literal_text():
    """目标 / 标题 / 角色里的富文本标记按字面渲染，绝不抛 MarkupError。"""
    view = BoardView("R[/x]", "目标[/red]", enabled=False)
    board = TaskBoard([mk("T-1[/x]", S.PENDING, role="role[/b]")])
    printed = render_text(view.render(board))
    assert "目标[/red]" in printed and "[/x]" in printed and "role[/b]" in printed


def test_print_report_still_prints_when_live_disabled(capsys):
    """--no-live 只关实时画面；运行报告照常打印。"""
    rec = RunRecord(run_id="R", project_goal="g", status=RunStatus.COMPLETED,
                    created_at=datetime.now(), updated_at=datetime.now(),
                    config_snapshot={}, stats=RunStats(total_tasks=1, succeeded=1))
    BoardView("R", "g", enabled=False).print_report(
        rec, TaskBoard([mk("A", S.SUCCEEDED)]), artifacts_dir=Path("/tmp/art"))
    assert "运行报告" in capsys.readouterr().out

