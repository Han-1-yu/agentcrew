"""时间统一与时钟异常容错（A-7 / D-14）。

放在 ``utils`` 而不是 ``runtime``：``models`` 层被禁止依赖 ``runtime``
（见 ``tests/test_architecture.py`` 的 FORBIDDEN），而 provider 的 latency、
任务时长都要算差值。时间工具是跨层基础设施，归 ``utils`` 才是正确的位置。

**问题**：``run.json.updated_at`` 与心跳都是 ``datetime.now()`` 写的 naive 本地
时间，多处「距上次心跳多久」直接相减，对系统时钟的变化没有任何防护：

- **时钟回拨**（NTP 纠正、用户改时间、跨时区迁移）：差值为负 → 永远小于阈值
  → 孤儿 run 永不收敛，锁也不可接管（死锁）。
- **时钟前跳** >15 分钟：差值虚大 → 把仍在跑的 run 判成孤儿，改写状态甚至删目录。

**策略**（与 GAP-ANALYSIS A-7 / D-14 一致）：

1. 新写入统一 UTC（``utc_now``，带 ``+00:00`` 偏移），跨时区、夏令时不再错位；
2. 旧数据仍是 naive 本地时间——``parse_stamp`` 按本机时区补齐，读取端零迁移；
3. 差值判定集中在 ``elapsed_since``：倒挂超过容忍窗口一律返回 ``None``，
   调用方据此 **fail-closed**（不判孤儿、不接管锁），宁可留僵尸等人工处理，
   也不误杀活进程。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: 允许的时钟倒挂窗口。小于等于它的倒挂按「刚写过」处理（正常 NTP 微调、
#: 两次采样间隔内的抖动）；超过它说明时钟严重不同步，本次判定作废。
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)

_ZERO = timedelta(0)


def utc_now() -> datetime:
    """当前时刻（timezone-aware UTC）。所有新落盘时间都应用它。"""
    return datetime.now(timezone.utc)


def parse_stamp(raw: object) -> datetime | None:
    """把 ISO 时间串解析成 aware ``datetime``；非法/缺失返回 ``None``。

    naive 串（旧数据，``datetime.now()`` 写的本机时间）按本机时区补齐——
    不这么做的话，拿 UTC 的 now 去减 naive 的本地时间会平白差出一个时区偏移，
    在东八区就是 8 小时，足以让所有判据失真。
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp


def elapsed_since(stamp: datetime | None, *, now: datetime | None = None) -> timedelta | None:
    """``now - stamp``；返回 ``None`` 表示时间数据不可信（时钟倒挂超容忍）。

    * ``stamp`` 为 ``None`` → ``None``（调用方按自己的缺失语义处理）；
    * 倒挂超过 :data:`CLOCK_SKEW_TOLERANCE` → ``None``（判定作废，调用方 fail-closed）；
    * 小倒挂 → ``0``（当作刚写入，避免 NTP 微调导致「未来时间」判据异常）；
    * 其余返回真实差值。
    """
    if stamp is None:
        return None
    moment = now or utc_now()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    delta = moment - stamp
    if delta < -CLOCK_SKEW_TOLERANCE:
        return None
    if delta < _ZERO:
        return _ZERO
    return delta


def is_clock_skew(stamp: datetime | None, *, now: datetime | None = None) -> bool:
    """``stamp`` 是否明显晚于 ``now``（时钟倒挂超容忍）——供调用方留痕用。"""
    if stamp is None:
        return False
    moment = now or utc_now()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return (moment - stamp) < -CLOCK_SKEW_TOLERANCE


def seconds_between(start: datetime | None, end: datetime | None = None) -> float:
    """``start → end`` 之间经过的秒数；不可判定时返回 ``0.0``。

    D-14 迁移用：落盘时间改成 aware UTC 后，``(end - start)`` 在遇到旧数据
    （naive）时不再是「差 8 小时」而是直接 ``TypeError``。所有时长计算都走这里，
    两种格式混存也能算对；时钟倒挂或缺失时退化为 0——时长展示为 0 远好过整个
    页面/报告 500。
    """
    if start is None:
        return 0.0
    finish = end or utc_now()
    # 统一成 aware 再相减：naive 旧数据按本机时区补齐（与 parse_stamp 同口径）。
    if start.tzinfo is None:
        start = start.astimezone()
    if finish.tzinfo is None:
        finish = finish.astimezone()
    seconds = (finish - start).total_seconds()
    return seconds if seconds > 0 else 0.0
