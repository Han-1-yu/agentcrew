按设计实现了两个模块：`core.analyze` 是唯一编排点，解析失败返回 None 并计入 skipped，分位用最近秩。

```python file=logstat/__init__.py
"""logstat：把访问日志压成几行统计的命令行工具。"""

__version__ = "0.1.0"

__all__ = ["__version__"]
```

```python file=logstat/core.py
"""解析与聚合：analyze 是唯一的编排点。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Record:
    """一条成功解析的访问记录。"""

    ts: datetime
    status: int
    duration_ms: int


def parse_line(line: str) -> Record | None:
    """把一行日志解析成 Record；不满足格式时返回 None（不抛异常）。

    设计文档 §3：日志是流式追加的，末行常是半行；启动横幅也会混进同一文件。
    解析失败一律交给调用方计入 skipped，绝不中断整次统计。
    """
    parts = line.split()
    if len(parts) < 3:
        return None
    try:
        ts = datetime.fromisoformat(parts[0])
        status = int(parts[1])
        duration = int(parts[2])
    except (ValueError, TypeError):
        return None
    if duration < 0:
        return None
    return Record(ts=ts, status=status, duration_ms=duration)


def percentile(values: list[int], ratio: float) -> int:
    """最近秩分位：取集合里真实存在的一个值，便于反查具体请求。"""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, -(-int(ratio * 1000) * len(ordered) // 1000))
    return ordered[min(rank, len(ordered)) - 1]


def analyze(lines: list[str], window_minutes: int = 1) -> dict:
    """解析 + 聚合，返回可直接渲染的统计结果。

    返回字典的键与 PRD §3 的统计口径逐项对应：total / skipped / status /
    p50 / p95 / window。
    """
    records: list[Record] = []
    skipped = 0
    for line in lines:
        if not line.strip():
            continue
        record = parse_line(line)
        if record is None:
            skipped += 1
        else:
            records.append(record)

    durations = [r.duration_ms for r in records]
    status: Counter[int] = Counter(r.status for r in records)

    step = timedelta(minutes=max(1, window_minutes))
    buckets: dict[datetime, dict[str, int]] = {}
    for record in records:
        bucket = record.ts.replace(second=0, microsecond=0)
        # 截断到窗口上界，避免同一分钟内的请求因对齐方式不同落到相邻桶
        bucket -= timedelta(minutes=(bucket.minute % max(1, window_minutes)))
        slot = buckets.setdefault(bucket, {"count": 0, "errors": 0})
        slot["count"] += 1
        if record.status >= 500:
            slot["errors"] += 1

    return {
        "total": len(records),
        "skipped": skipped,
        "status": {code: status[code] for code in sorted(status)},
        "p50": percentile(durations, 0.50),
        "p95": percentile(durations, 0.95),
        "window": [
            {"start": key.isoformat(sep=" "), "count": buckets[key]["count"],
             "errors": buckets[key]["errors"]}
            for key in sorted(buckets)
        ],
        "window_size": step.total_seconds(),
    }
```

两个模块都通过了语法检查，`analyze` 的返回键与 PRD 的统计口径逐项对齐。
