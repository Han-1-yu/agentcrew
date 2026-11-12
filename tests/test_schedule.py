"""定时任务（Automations）单元测试：add / list / remove / tick 与 next_run 计算。"""

from datetime import datetime

from agentcrew.cli.schedule import (
    Schedule,
    add_schedule,
    compute_next_run,
    load_schedules,
    remove_schedule,
    tick,
)


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_add_once_sets_next_run_to_at(tmp_path):
    s = add_schedule(tmp_path, goal="g", mock=True, mode="ask", kind="once",
                     at="2030-01-01T09:00:00", weekdays=[], time=None)
    assert s.id.startswith("sched_")
    assert s.next_run == "2030-01-01T09:00:00"
    assert [x.id for x in load_schedules(tmp_path)] == [s.id]


def test_add_recurring_computes_next_weekday(tmp_path):
    # 2026-11-09 是周一；daily 09:00 命中工作日 → 最近的是今天 09:00（若已过则明天）
    now = _dt("2026-11-09T12:00:00")
    s = add_schedule(tmp_path, goal="g", mock=False, mode="agent", kind="recurring",
                     at=None, weekdays=["MO", "TU", "WE", "TH", "FR"], time="09:00",
                     now=now)
    nxt = _dt(s.next_run)
    assert nxt > now
    assert nxt.hour == 9 and nxt.minute == 0
    assert nxt.weekday() in (0, 1, 2, 3, 4)


def test_add_recurring_skips_weekend(tmp_path):
    # 周五 12:00 触发后，下一个工作日应是下周一 09:00
    friday = _dt("2026-11-13T12:00:00")  # 2026-11-13 是周五
    s = add_schedule(tmp_path, goal="g", mock=False, mode="agent", kind="recurring",
                     at=None, weekdays=["MO", "TU", "WE", "TH", "FR"], time="09:00",
                     now=friday)
    nxt = _dt(s.next_run)
    assert nxt.isoweekday() == 1  # 周一
    assert nxt > friday


def test_compute_next_run_once_past_is_due(tmp_path):
    s = Schedule(id="x", goal="g", kind="once", at="2020-01-01T00:00:00")
    assert compute_next_run(s, _dt("2026-11-09T12:00:00")) == _dt("2020-01-01T00:00:00")


def test_tick_fires_due_once_and_disables(tmp_path):
    s = add_schedule(tmp_path, goal="g", mock=True, mode="ask", kind="once",
                     at="2020-01-01T00:00:00", weekdays=[], time=None)
    calls: list[tuple] = []

    def fake_runner(output_dir, sched):
        calls.append((str(output_dir), sched.id))

    fired = tick(tmp_path, now=_dt("2026-11-09T12:00:00"), runner=fake_runner)
    assert [f["id"] for f in fired] == [s.id]
    assert len(calls) == 1
    after = load_schedules(tmp_path)
    assert after[0].enabled is False
    assert after[0].last_run_at is not None


def test_tick_advances_recurring_and_is_idempotent(tmp_path):
    now = _dt("2026-11-09T09:00:01")  # 周一刚过 09:00，应触发并顺延
    s = add_schedule(tmp_path, goal="g", mock=True, mode="agent", kind="recurring",
                     at=None, weekdays=["MO", "TU", "WE", "TH", "FR"], time="09:00",
                     now=_dt("2026-11-09T00:00:00"))
    fired = tick(tmp_path, now=now, runner=lambda od, sc: None)
    assert [f["id"] for f in fired] == [s.id]
    after = load_schedules(tmp_path)[0]
    assert after.enabled is True
    # 顺延后不再到期：再次 tick 不应重复触发
    fired2 = tick(tmp_path, now=now, runner=lambda od, sc: None)
    assert fired2 == []


def test_remove_schedule(tmp_path):
    s = add_schedule(tmp_path, goal="g", mock=True, mode="agent", kind="once",
                     at="2030-01-01T09:00:00", weekdays=[], time=None)
    assert remove_schedule(tmp_path, s.id) is True
    assert remove_schedule(tmp_path, s.id) is False
    assert load_schedules(tmp_path) == []
