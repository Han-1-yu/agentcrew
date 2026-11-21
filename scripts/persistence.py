#!/usr/bin/env python3
"""阶段 3：ledger 落盘策略的持久化测量。

对 ``each`` / ``batch`` / ``none`` 三种 fsync 策略各跑一轮固定数量的
``emit``，报告吞吐（events/s）与 P50/P95 单条延迟，输出成可直接贴进
文档的 Markdown 表。测量结果（2026-11-21，Windows 本机）见
``bench/persistence-2026-11-21.md``。

用法::

    python scripts/persistence.py            # 默认 2000 事件/策略
    python scripts/persistence.py --events 5000 --target bench/xxx.md

口径：事件体固定（含中文、嵌套 dict），同一临时目录，策略之间互不干扰；
吞吐不含进程启动与首条冷写。``each`` 的绝对值就是 spec §13.2 全承诺下的
真实成本——\"慢\"的是它，而不是写 JSONL 本身。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcrew.runtime.ledger import Ledger, resolve_sync_policy  # noqa: E402

#: 单条事件的固定形状（模拟真实 run 的事件体：中文字段 + 嵌套结构）。
_SAMPLE_DATA = {
    "task_id": "T-012",
    "role": "backend_dev",
    "tool_name": "write_file",
    "args": {"path": "src/api/handler.py", "note": "中文备注：处理器骨架"},
    "success": True,
    "duration_ms": 483,
}

_POLICIES = ("each", "batch", "none")


def _measure(policy: str, events: int) -> dict[str, float]:
    """跑一轮固定量 emit，返回吞吐与延迟分位数（毫秒）。"""
    with tempfile.TemporaryDirectory(prefix="ac-persist-") as tmp:
        path = Path(tmp) / "ledger.jsonl"
        ledger = Ledger(path, run_id="bench", sync=policy)
        # 预热一条：排除首次 open 的冷启动对首条延迟的污染。
        ledger.emit("warmup")
        durations: list[float] = []
        started = time.perf_counter()
        for i in range(events):
            t0 = time.perf_counter()
            ledger.emit("tool_call", seq=i, **_SAMPLE_DATA)
            durations.append((time.perf_counter() - t0) * 1000.0)
        elapsed = time.perf_counter() - started
        ledger.close()  # batch / none 在此统一收口 fsync
    durations.sort()

    def pct(p: float) -> float:
        idx = min(len(durations) - 1, max(0, round(p * (len(durations) - 1))))
        return durations[idx]

    return {
        "policy": policy,
        "throughput": events / elapsed,
        "p50": pct(0.50),
        "p95": pct(0.95),
        "max": durations[-1],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=2000,
                        help="每种策略的 emit 条数（默认 2000）")
    parser.add_argument("--target", type=str, default=None,
                        help="结果 Markdown 追加写入的文件（缺省只打印）")
    args = parser.parse_args()

    if args.events < 100:
        print("错误：--events 至少 100（样本太少分位数无意义）")
        return 3

    rows = [_measure(policy, args.events) for policy in _POLICIES]

    lines = [
        f"# ledger 持久化测量（{args.events} 事件/策略）",
        "",
        "| 策略 | 吞吐 (events/s) | P50 延迟 (ms) | P95 延迟 (ms) | 最大 (ms) |",
        "| --- | ---: | ---: | ---: | ---: |",
        *[
            f"| {r['policy']} | {r['throughput']:.0f} | "
            f"{r['p50']:.2f} | {r['p95']:.2f} | {r['max']:.2f} |"
            for r in rows
        ],
    ]
    lines += [
        "",
        f"> 环境：Windows 本机 / Python {sys.version.split()[0]} / "
        f"策略解析 {resolve_sync_policy()[0]}（默认）。"
        "batch=32；`none` 的崩溃窗口为整个 run（close 收口），"
        "`each` 为 0。绝对值随机器浮动，看相对差。",
    ]
    report = "\n".join(lines) + "\n"
    print(report)
    if args.target:
        Path(args.target).parent.mkdir(parents=True, exist_ok=True)
        with open(args.target, "a", encoding="utf-8") as file:
            file.write(report)
        print(f"已追加写入 {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
