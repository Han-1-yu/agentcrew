"""定时任务（Automations）：一次性 / 周期日程的读写与到期触发。

对齐 WorkBuddy 的 Automations 能力：一条日程 = 「在某个时刻 / 每周某几天的
某个时刻，按某种执行模式跑一次 run」。日程持久化在
``<output_dir>/_schedules/schedules.json``，**不引入常驻 daemon** ——
``schedule tick`` 是幂等的「到期检查 + 触发」入口，由宿主（Studio 主进程的
定时器，或系统 cron）按需调用。

设计约束：

- ``tick`` 绝不阻塞：到期日程用 ``subprocess.Popen`` 起 ``main.py run`` 子进程，
  立即返回；本轮命中的日程先推进 ``next_run`` / 标记已触发，再落盘，避免同一
  时刻被并发 tick 重复触发（Popen 是「已派发」的持久事实，重复 tick 读到的
  已是推进后的状态）。
- 一次性日程触发后 ``enabled=False``（保留在列表里供审计，可手动删除）；
  周期日程推进到下一个命中点。
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

SCHEDULES_DIRNAME = "_schedules"
SCHEDULES_FILENAME = "schedules.json"

_WEEKDAYS = {
    "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6,
}
_VALID_MODES = ("agent", "plan", "ask")


class Schedule(BaseModel):
    id: str
    goal: str
    mock: bool = False
    mode: str = "agent"          # agent | plan | ask
    kind: str = "once"           # once | recurring
    at: str | None = None        # ISO8601，once 用
    weekdays: list[str] = Field(default_factory=list)  # recurring 用，如 ["MO","WE"]
    time: str | None = None      # "HH:MM"，recurring 用
    next_run: str | None = None  # ISO8601 下次触发
    enabled: bool = True
    last_run_at: str | None = None
    created_at: str = ""


def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _schedules_path(output_dir: Path) -> Path:
    return output_dir / SCHEDULES_DIRNAME / SCHEDULES_FILENAME


def load_schedules(output_dir: Path) -> list[Schedule]:
    path = _schedules_path(output_dir)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Schedule.model_validate(item) for item in raw]
    except (OSError, ValueError):
        # 日程文件损坏不该拖垮 tick / list：按空处理并让调用方感知到即可。
        return []


def save_schedules(output_dir: Path, schedules: Sequence[Schedule]) -> None:
    path = _schedules_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [s.model_dump(mode="json") for s in schedules]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _compute_once(schedule: Schedule) -> datetime | None:
    return _parse_iso(schedule.at)


def _compute_recurring(schedule: Schedule, now: datetime) -> datetime | None:
    """从 now 起，下一个落在 weekdays 且等于 time 的触发点（含今天已过则顺延）。"""
    if not schedule.weekdays or not schedule.time:
        return None
    try:
        hour, minute = (int(x) for x in schedule.time.split(":"))
    except (ValueError, AttributeError):
        return None
    wanted = {_WEEKDAYS[d] for d in schedule.weekdays if d in _WEEKDAYS}
    if not wanted:
        return None
    candidate = now.replace(second=0, microsecond=0, minute=minute, hour=hour)
    # 最多顺延 8 天，必然覆盖一周内至少一个命中日。
    for _ in range(8):
        if candidate.weekday() in wanted and candidate > now:
            return candidate
        candidate += timedelta(days=1)
    return None


def compute_next_run(schedule: Schedule, now: datetime) -> datetime | None:
    if schedule.kind == "recurring":
        return _compute_recurring(schedule, now)
    return _compute_once(schedule)


def _run_args(schedule: Schedule) -> list[str]:
    args = ["run", "--goal", schedule.goal]
    if schedule.mock:
        args.append("--mock")
    if schedule.mode == "ask":
        args.append("--plan-only")
    elif schedule.mode == "plan":
        args += ["--gate", "plan", "--approval-mode", "deferred"]
    else:
        args.append("--auto")
    # 来源进 ledger（`run_triggered source=schedule:<id>`）：到期的 run 与日程对得上号，
    # Studio 的「触发历史」就不必猜「这条 run 是不是定时任务叫起来的」。
    args += ["--trigger-source", f"schedule:{schedule.id}"]
    args += ["--no-live"]
    return args


def add_schedule(
    output_dir: Path,
    *,
    goal: str,
    mock: bool,
    mode: str,
    kind: str,
    at: str | None,
    weekdays: list[str],
    time: str | None,
    now: datetime | None = None,
) -> Schedule:
    if kind not in ("once", "recurring"):
        raise ValueError("kind 必须是 once 或 recurring")
    if mode not in _VALID_MODES:
        raise ValueError(f"mode 必须是 {'/'.join(_VALID_MODES)} 之一")
    now = now or _now()
    schedule = Schedule(
        id=f"sched_{uuid.uuid4().hex[:12]}",
        goal=goal,
        mock=mock,
        mode=mode,
        kind=kind,
        at=at,
        weekdays=weekdays,
        time=time,
        created_at=_iso(now),
    )
    nxt = compute_next_run(schedule, now)
    if nxt is None:
        raise ValueError("无法确定触发时间：一次性需 --at，周期需 --daily 与 --weekdays")
    schedule.next_run = _iso(nxt)
    schedules = load_schedules(output_dir)
    schedules.append(schedule)
    save_schedules(output_dir, schedules)
    return schedule


def set_enabled(
    output_dir: Path,
    schedule_id: str,
    enabled: bool,
    *,
    now: datetime | None = None,
) -> Schedule | None:
    """启用 / 停用一条日程，返回改后的对象（找不到返回 ``None``）。

    启用时**必须重算** ``next_run``：停用期间日历照常走，沿用旧值会让重新启用的
    日程立刻补跑一次（周期日程在停用一周后尤其明显）。停用只翻开关、不动
    ``next_run``——列表里保留「本来该什么时候跑」用于审计。

    一次性日程的 ``at`` 已经过去了就没法「重新启用」（没有下一个触发点可算），
    此时抛 ``ValueError`` 让调用方提示重建，而不是把它排成「立刻触发」。
    """
    now = now or _now()
    schedules = load_schedules(output_dir)
    for index, schedule in enumerate(schedules):
        if schedule.id != schedule_id:
            continue
        if enabled:
            nxt = compute_next_run(schedule, now)
            if nxt is not None and nxt <= now and schedule.kind == "once":
                raise ValueError(
                    f"一次性日程的触发时间已过（{schedule.at}），请删除后重建")
            schedule.next_run = _iso(nxt) if nxt else None
        schedule.enabled = enabled
        schedules[index] = schedule
        save_schedules(output_dir, schedules)
        return schedule
    return None


def remove_schedule(output_dir: Path, schedule_id: str) -> bool:
    schedules = load_schedules(output_dir)
    kept = [s for s in schedules if s.id != schedule_id]
    if len(kept) == len(schedules):
        return False
    save_schedules(output_dir, kept)
    return True


def _spawn(output_dir: Path, schedule: Schedule) -> None:
    """起一个 ``main.py run`` 子进程并立即返回（不阻塞 tick）。"""
    repo_root = Path(__file__).resolve().parents[2]
    entry = repo_root / "main.py"
    cmd = [sys.executable, str(entry), *_run_args(schedule), "--output-dir", str(output_dir)]
    subprocess.Popen(cmd, cwd=str(repo_root))


def tick(
    output_dir: Path,
    *,
    now: datetime | None = None,
    runner: Callable[[Path, Schedule], None] | None = None,
) -> list[dict[str, Any]]:
    """到期检查 + 触发。返回本轮触发（或推进）的日程摘要，幂等。"""
    now = now or _now()
    spawn = runner or _spawn
    schedules = load_schedules(output_dir)
    fired: list[dict[str, Any]] = []
    changed = False
    for s in schedules:
        if not s.enabled:
            continue
        due = _parse_iso(s.next_run)
        if due is None or due > now:
            continue
        spawn(output_dir, s)
        s.last_run_at = _iso(now)
        if s.kind == "once":
            s.enabled = False
            s.next_run = None
        else:
            nxt = compute_next_run(s, now)
            s.next_run = _iso(nxt) if nxt else None
        fired.append({"id": s.id, "goal": s.goal, "kind": s.kind,
                      "mode": s.mode, "next_run": s.next_run})
        changed = True
    if changed:
        save_schedules(output_dir, schedules)
    return fired
