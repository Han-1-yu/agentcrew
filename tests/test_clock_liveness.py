"""A-7 / D-14：时钟异常容错与时间解析的回归。

核心断言：**时钟不可信时宁可留下僵尸 run，也绝不改写/接管一个可能仍在跑的
run**（fail-closed），同时旧版 naive 本地时间数据不需要迁移。

三个真实事故场景：
1. 时钟回拨（NTP 纠正 / 手动改时间）→ 差值为负 → 此前孤儿永不收敛、锁永不接管；
2. 时钟前跳 → 差值虚大 → 此前把活 run 判成孤儿并改写状态；
3. 旧数据（naive 本地时间）与新数据（aware UTC）混存。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentcrew.runtime.checkpoint import is_stale_running
from agentcrew.runtime.lifecycle import (
    HEARTBEAT_FILE,
    LIVENESS_STALE_AFTER,
    LOCK_STALE_AFTER,
    ResumeLock,
    is_orphaned_running,
    read_heartbeat,
    write_heartbeat,
)
from agentcrew.utils.clock import (
    CLOCK_SKEW_TOLERANCE,
    elapsed_since,
    is_clock_skew,
    parse_stamp,
    utc_now,
)


def _running_run(tmp_path: Path, *, updated_at: str, heartbeat: str | None) -> Path:
    """造一个停在 RUNNING 的 run 目录（可选带心跳文件）。"""
    run_dir = tmp_path / "R-1"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps({"schema_version": 1, "run_id": "R-1", "status": "running",
                    "updated_at": updated_at}),
        encoding="utf-8",
    )
    if heartbeat is not None:
        (run_dir / HEARTBEAT_FILE).write_text(
            json.dumps({"pid": 1, "updated_at": heartbeat}), encoding="utf-8")
    return run_dir


class TestParseStamp:
    def test_naive_is_read_as_local_time(self):
        """旧数据是 naive 本地时间——必须按本机时区补齐，而不是当成 UTC。"""
        stamp = parse_stamp("2026-11-11T10:00:00")
        assert stamp is not None and stamp.tzinfo is not None
        assert stamp.astimezone().hour == 10   # 语义（本地 10 点）不变

    def test_aware_is_preserved(self):
        raw = "2026-11-11T02:00:00+00:00"
        stamp = parse_stamp(raw)
        assert stamp == datetime(2026, 11, 11, 2, tzinfo=timezone.utc)

    def test_invalid_returns_none(self):
        for bad in ("", "not-a-time", None, 123, "2026-13-45T99:99:99"):
            assert parse_stamp(bad) is None

    def test_naive_and_utc_of_same_instant_compare_equal(self):
        """同一时刻的 naive 本地串与 aware UTC 串归一后应相等。"""
        local = datetime.now()
        assert parse_stamp(local.isoformat()) == parse_stamp(
            local.astimezone(timezone.utc).isoformat())


class TestElapsedSince:
    def test_normal_elapsed(self):
        stamp = utc_now() - timedelta(minutes=30)
        assert elapsed_since(stamp) == pytest.approx(timedelta(minutes=30), abs=timedelta(seconds=5))

    def test_small_backward_skew_counts_as_zero(self):
        """NTP 微调造成的几秒倒挂 → 当作刚写过，不产生负值。"""
        stamp = utc_now() + timedelta(seconds=3)
        assert elapsed_since(stamp) == timedelta(0)

    def test_large_backward_skew_returns_none(self):
        """倒挂超过容忍窗口 → None（判定作废）。"""
        stamp = utc_now() + CLOCK_SKEW_TOLERANCE + timedelta(minutes=1)
        assert elapsed_since(stamp) is None
        assert is_clock_skew(stamp) is True

    def test_missing_stamp_returns_none(self):
        assert elapsed_since(None) is None
        assert is_clock_skew(None) is False


class TestFailClosedOnClockSkew:
    def test_orphan_detection_skipped_when_heartbeat_in_future(self, tmp_path):
        """回拨：心跳时间在「未来」→ 不判孤儿（不能改写一个可能活着的 run）。"""
        run_dir = _running_run(
            tmp_path,
            updated_at=utc_now().isoformat(),
            heartbeat=(utc_now() + timedelta(hours=2)).isoformat(),
        )
        assert is_orphaned_running(run_dir) is False

    def test_orphan_detection_still_works_for_genuinely_old_heartbeat(self, tmp_path):
        run_dir = _running_run(
            tmp_path,
            updated_at=(utc_now() - timedelta(days=2)).isoformat(),
            heartbeat=(utc_now() - LIVENESS_STALE_AFTER - timedelta(minutes=5)).isoformat(),
        )
        assert is_orphaned_running(run_dir) is True

    def test_fresh_heartbeat_is_not_orphan(self, tmp_path):
        run_dir = _running_run(
            tmp_path,
            updated_at=(utc_now() - timedelta(days=3)).isoformat(),
            heartbeat=utc_now().isoformat(),
        )
        assert is_orphaned_running(run_dir) is False

    def test_stale_running_skipped_when_updated_at_in_future(self, tmp_path):
        run_dir = _running_run(
            tmp_path,
            updated_at=(utc_now() + timedelta(hours=3)).isoformat(),
            heartbeat=None,
        )
        assert is_stale_running(run_dir) is False

    def test_stale_running_still_flags_real_zombie(self, tmp_path):
        run_dir = _running_run(
            tmp_path,
            updated_at=(utc_now() - timedelta(days=3)).isoformat(),
            heartbeat=None,
        )
        assert is_stale_running(run_dir) is True


class TestLockFailClosed:
    def test_lock_not_taken_over_when_created_in_future(self, tmp_path):
        """锁的 created_at 在未来（时钟回拨）→ 不接管，避免双进程同写。"""
        run_dir = tmp_path / "R-lock"
        run_dir.mkdir()
        lock = ResumeLock(run_dir)
        future = utc_now() + timedelta(hours=1)
        lock._path.write_text(
            json.dumps({"owner": "other", "created_at": future.isoformat()}),
            encoding="utf-8",
        )
        assert lock._lock_is_stale(now=utc_now()) is False

    def test_lock_taken_over_when_old_and_no_heartbeat(self, tmp_path):
        run_dir = tmp_path / "R-lock2"
        run_dir.mkdir()
        lock = ResumeLock(run_dir)
        old = utc_now() - LOCK_STALE_AFTER - timedelta(hours=1)
        lock._path.write_text(
            json.dumps({"owner": "other", "created_at": old.isoformat()}),
            encoding="utf-8",
        )
        assert lock._lock_is_stale(now=utc_now()) is True


class TestLegacyDataCompatibility:
    def test_legacy_naive_heartbeat_still_expires(self, tmp_path):
        """升级前写的 naive 本地时间心跳，仍要被正确判定为过期。"""
        run_dir = _running_run(
            tmp_path,
            updated_at=datetime.now().isoformat(),
            heartbeat=(datetime.now() - LIVENESS_STALE_AFTER - timedelta(minutes=1)).isoformat(),
        )
        assert is_orphaned_running(run_dir) is True

    def test_new_utc_heartbeat_roundtrip(self, tmp_path):
        """新写入的心跳是 aware UTC，读回来语义不变。"""
        run_dir = tmp_path / "R-utc"
        run_dir.mkdir()
        write_heartbeat(run_dir, now=utc_now())
        data = json.loads((run_dir / HEARTBEAT_FILE).read_text(encoding="utf-8"))
        assert data["updated_at"].endswith("+00:00")
        parsed = read_heartbeat(run_dir)
        assert parsed is not None and parsed.tzinfo is not None
