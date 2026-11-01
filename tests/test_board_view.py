from datetime import datetime
from pathlib import Path

from rich.console import Console

from agentcrew.runtime.board_view import STATUS_STYLE, BoardView
from agentcrew.runtime.checkpoint import RunRecord, RunStatus
from agentcrew.runtime.ledger import LedgerEvent
from agentcrew.task.task import RunStats, Task
from agentcrew.task.task import TaskStatus as S
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


def test_start_degrades_to_line_logging_on_dumb_terminal(monkeypatch):
    """TERM=dumb：is_terminal 为真但 rich 不重绘，必须降级为行日志。

    回归保护：rich 15 的 ``Live.refresh()`` 在 dumb 终端下既不重绘也不报错，
    若不降级，看班会停在首帧、事件全部丢失（CI 与精简 shell 上就是这种 TERM）。
    """
    import io

    monkeypatch.setenv("TERM", "dumb")
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, width=100, no_color=True)
    assert console.is_terminal and console.is_dumb_terminal, "前置条件：dumb 终端"

    v = BoardView("R", "g", console=console)
    v.start()
    try:
        assert v._live is None, "dumb 终端不得启动 Live（否则画面永不刷新）"
        v.on_event(ev("run_started", goal="g"))
    finally:
        v.stop()
    out = stream.getvalue()
    assert "降级为行日志" in out
    assert "运行开始" in out, "降级路径仍要逐行打印事件"


def test_live_is_refreshed_by_events_not_by_a_timer(monkeypatch):
    """spec §15.2：看板由 ledger 事件驱动刷新，不轮询。

    关掉 ``Live`` 的定时器（``auto_refresh=False``）之后，画面只在
    ``on_event`` 主动 ``refresh()`` 时更新 —— 这条同时钉住两件事：没有定时器，
    以及事件确实会让画面跟上（否则看板会停在启动占位图上，事件与画面脱钩）。

    ``TERM`` 必须显式钉住：``TERM=dumb`` 时 rich 的 ``Live.refresh()`` 不做重绘，
    而 ``BoardView`` 会把 dumb 终端当非交互终端降级（见
    :func:`test_start_degrades_to_line_logging_on_dumb_terminal`），
    那样本用例测的就不是「Live 刷新」这条路径了。
    """
    import io

    monkeypatch.setenv("TERM", "xterm-256color")
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, width=100, no_color=True)
    v = BoardView("R", "g", console=console)
    v.start()
    try:
        live = v._live
        assert live is not None, "强制 TTY 的 Console 必须启动 Live"
        assert live.auto_refresh is False, "Ledger 驱动的看板不得让 Live 自己轮询"
        v.render(TaskBoard([mk("T-001", S.RUNNING)]))   # Live 的取数回调
        assert "T-001" not in stream.getvalue()          # 还没刷新：画面上是占位图
        v.on_event(ev("task_transition", task_id="T-001",
                      **{"from": "ready", "to": "running"}))
        assert "T-001" in stream.getvalue(), "事件之后画面没有跟上（未主动 refresh）"
    finally:
        v.stop()


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

