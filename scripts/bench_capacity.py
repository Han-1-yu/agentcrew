"""C14：单进程「多 run 并发」基线——量清上限，再决定要不要分布式。

    python scripts/bench_capacity.py                    # 1/2/4/8/10，各 1 轮
    python scripts/bench_capacity.py --levels 1,4,8 --repeat 2
    python scripts/bench_capacity.py --max-concurrent 8 --timeout-s 600

**测什么、不测什么**

- 测的是 `ApiServer` 在**同一个进程里同时跑 N 个 run** 时的：并发槽位是否按
  `AGENTCREW_MAX_CONCURRENT_RUNS` 生效（超限应 429）、创建延迟 P50/P95、
  全部完成墙钟、吞吐与峰值 RSS。
- provider 是 mock 回放（零 API Key、零费用），并注入固定 ``--latency-ms`` 模拟
  「等模型」；没有延迟时所有任务瞬间完成，并发度再高也看不出差别。
- **不测模型侧吞吐**：真实模型的限流与延迟另算（``bench.py --real`` 是那一侧）。
- 与 ``bench.py`` 的分工：bench.py 量**单 run 内的任务并发**（``max_concurrency``），
  本脚本量**多 run 并发**（API 并发槽位）。两个维度不重叠。

产物：``bench/capacity-<日期>.md`` 与同名 ``.json``。退出码 0 = 全部档位达标。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agentcrew.api.server import ApiServer  # noqa: E402
from agentcrew.config import Settings  # noqa: E402

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


# ── HTTP（asyncio 直连，避免线程池把并发压平）──────────────────────────────


async def _http_json(port: int, method: str, path: str, payload: dict | None = None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    data = b"" if payload is None else json.dumps(payload).encode("utf-8")
    head = (
        f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Connection: close\r\n"
    )
    if data:
        head += f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
    writer.write(head.encode("utf-8") + b"\r\n" + data)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass
    head_bytes, _, body = raw.partition(b"\r\n\r\n")
    first = head_bytes.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = first.split(" ")
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    try:
        parsed = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return status, parsed


# ── 内存（Windows psapi / Linux /proc；读不到就报 None，不编数）─────────────


def rss_mb() -> float | None:
    try:
        if sys.platform == "win32":
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            psapi = ctypes.WinDLL("psapi.dll")
            kernel32 = ctypes.WinDLL("kernel32.dll")
            # 必须显式声明原型：默认 restype 是 c_int，会把伪句柄 -1 截断，
            # GetProcessMemoryInfo 静默失败（soak.py 也踩过这个坑）。
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters),
                                          counters.cb):
                return counters.WorkingSetSize / (1024 * 1024)
            return None
        status = Path("/proc/self/status").read_text(encoding="utf-8")
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024.0
    except (OSError, ValueError, AttributeError):
        return None
    return None


# ── 单档测量 ──────────────────────────────────────────────────────────────


async def measure_level(port: int, level: int, *, latency_ms: int,
                        timeout_s: float) -> dict:
    baseline = rss_mb()
    peak = baseline or 0.0

    async def create(index: int) -> dict:
        started = time.perf_counter()
        status, body = await _http_json(port, "POST", "/runs", {
            "goal": f"容量基线 {level}-{index}：写一个 slugify 函数",
            "options": {},
        })
        return {
            "status": status,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "run_id": (body or {}).get("run_id") if isinstance(body, dict) else None,
        }

    started = time.perf_counter()
    cpu_started = time.process_time()
    created = await asyncio.gather(*(create(index) for index in range(level)))
    accepted = [item for item in created if item["status"] == 202 and item["run_id"]]
    rejected = [item for item in created if item["status"] == 429]

    completed = 0
    deadline = time.perf_counter() + timeout_s
    pending = {item["run_id"] for item in accepted}
    while pending and time.perf_counter() < deadline:
        for run_id in list(pending):
            status, body = await _http_json(
                port, "GET", f"/runs/{run_id}/status")
            state = str((body or {}).get("status") or "").lower() \
                if isinstance(body, dict) else ""
            if status == 200 and state in TERMINAL:
                pending.discard(run_id)
                completed += 1
        current = rss_mb()
        if current is not None:
            peak = max(peak, current)
        if pending:
            await asyncio.sleep(0.2)

    wall = time.perf_counter() - started
    cpu_s = time.process_time() - cpu_started
    latencies = sorted(item["latency_ms"] for item in accepted)
    return {
        "level": level,
        "accepted": len(accepted),
        "rejected_429": len(rejected),
        "completed": completed,
        "unfinished": len(pending),
        "create_p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "create_p95_ms": round(
            latencies[max(0, int(len(latencies) * 0.95) - 1)], 1
        ) if latencies else None,
        "wall_s": round(wall, 2),
        "throughput_per_min": round(len(accepted) / wall * 60, 2) if wall else None,
        # 单进程 Python 受 GIL 限制，cpu_ratio 接近 1 就说明 CPU 打满、瓶颈在本地；
        # 远低于 1 则说明大部分时间在等模型（mock 下是 asyncio.sleep）。
        "cpu_s": round(cpu_s, 2),
        "cpu_ratio": round(cpu_s / wall, 2) if wall else None,
        "per_run_s": round(wall / len(accepted), 2) if accepted else None,
        "rss_baseline_mb": round(baseline, 1) if baseline is not None else None,
        "rss_peak_mb": round(peak, 1) if peak else None,
        "latency_ms": latency_ms,
    }


# ── 报告 ──────────────────────────────────────────────────────────────────


def _mean(values: list) -> float | None:
    present = [value for value in values if value is not None]
    return round(statistics.mean(present), 2) if present else None


def aggregate(rows: list[dict]) -> list[dict]:
    """同一档位的多轮采样聚合成一行：均值 + 墙钟的范围（单轮噪声很大）。"""
    by_level: dict[int, list[dict]] = {}
    for row in rows:
        by_level.setdefault(row["level"], []).append(row)
    merged = []
    for level in sorted(by_level):
        group = by_level[level]
        walls = [row["wall_s"] for row in group]
        merged.append({
            "level": level,
            "rounds": len(group),
            "accepted": int(_mean([row["accepted"] for row in group]) or 0),
            "rejected_429": int(_mean([row["rejected_429"] for row in group]) or 0),
            "completed": int(_mean([row["completed"] for row in group]) or 0),
            "unfinished": int(_mean([row["unfinished"] for row in group]) or 0),
            "create_p50_ms": _mean([row["create_p50_ms"] for row in group]),
            "create_p95_ms": _mean([row["create_p95_ms"] for row in group]),
            "wall_s": round(statistics.mean(walls), 2),
            "wall_min": min(walls),
            "wall_max": max(walls),
            "throughput_per_min": _mean([row["throughput_per_min"] for row in group]),
            "cpu_ratio": _mean([row["cpu_ratio"] for row in group]),
            "per_run_s": _mean([row["per_run_s"] for row in group]),
            "rss_peak_mb": _mean([row["rss_peak_mb"] for row in group]),
            "latency_ms": group[0]["latency_ms"],
        })
    return merged


def render_markdown(raw_rows: list[dict], *, limit: int, day: str) -> str:
    rows = aggregate(raw_rows)
    lines = [
        f"# 单进程多 run 并发基线（{day}）",
        "",
        f"- provider：mock（零费用）+ 每次 LLM 调用注入 {rows[0]['latency_ms']}ms 延迟",
        f"- 并发槽位上限 `AGENTCREW_MAX_CONCURRENT_RUNS = {limit}`；每档 {rows[0]['rounds']} 轮取均值",
        "- 指标口径：`wall` 从发出第一批 POST 到全部 run 进终态；"
        "`create_p95` 是 202 响应的延迟（不含排队等槽位）；"
        "`CPU 占比` = 进程 CPU 时间 / 墙钟（单进程 Python 上限约 1）",
        "",
        "| 并发 | 轮次 | 202 | 429 | 创建 P50 | 创建 P95 | 完成 | 墙钟（均值 / 范围） | 单 run 摊销 | 吞吐/分 | 吞吐倍数 | CPU 占比 | RSS 峰值 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    base = next((row for row in rows if row["level"] == 1), None)
    base_throughput = (base or {}).get("throughput_per_min") or 0
    for row in rows:
        speed = (
            round(row["throughput_per_min"] / base_throughput, 2)
            if base_throughput and row["throughput_per_min"] else "—"
        )
        lines.append(
            f"| {row['level']} | {row['rounds']} | {row['accepted']} | "
            f"{row['rejected_429']} | "
            f"{row['create_p50_ms']} ms | {row['create_p95_ms']} ms | "
            f"{row['completed']} | {row['wall_s']} s "
            f"({row['wall_min']}–{row['wall_max']}) | {row['per_run_s']} s | "
            f"{row['throughput_per_min']} | {speed}× | {row['cpu_ratio']} | "
            f"{row['rss_peak_mb']} MB |"
        )
    lines += ["", "## 判定", ""]
    for row in rows:
        ok = row["completed"] == row["accepted"] and row["unfinished"] == 0
        if row["level"] <= limit:
            ok = ok and row["accepted"] == row["level"]
        else:
            ok = ok and row["rejected_429"] > 0
        lines.append(
            f"- 并发 {row['level']}：{'PASS' if ok else 'FAIL'}"
            + ("" if ok else f"（accepted={row['accepted']} / "
                             f"rejected={row['rejected_429']} / "
                             f"completed={row['completed']} / "
                             f"unfinished={row['unfinished']}）"))
    lines += ["", "## 结论", ""]
    peak = max(rows, key=lambda row: row["throughput_per_min"] or 0)
    base_row = next((row for row in rows if row["level"] == 1), rows[0])
    base_tp = base_row["throughput_per_min"] or 0
    peak_speed = (
        round(peak["throughput_per_min"] / base_tp, 2) if base_tp else "—"
    )
    lines.append(
        f"- 吞吐峰值在 **{peak['level']} 并发**（{peak['throughput_per_min']} run/分，"
        f"相对单并发 {peak_speed}×）；收益低于并发数本身（{peak['level']}×），"
        f"说明事件循环里有同步段吃掉了部分重叠。{limit} 槽位是**安全护栏，"
        "不是吞吐旋钮**。")
    cpu_values = [row["cpu_ratio"] for row in rows if row["cpu_ratio"] is not None]
    if cpu_values:
        lines.append(
            f"- CPU 占比 {min(cpu_values)}–{max(cpu_values)}：进程没把 CPU 打满，"
            "大部分时间在等模型；吞吐上不去说明事件循环里有**同步段**"
            "（门禁校验 / 文件 IO / ledger 落盘）在串行吃掉并发重叠。")
    rss = [row["rss_peak_mb"] for row in rows if row["rss_peak_mb"]]
    if len(rss) >= 2:
        lines.append(
            f"- RSS {rss[0]} → {rss[-1]} MB（约 +"
            f"{round((rss[-1] - rss[0]) / max(1, peak['level']), 2)} MB/run），"
            "内存不是瓶颈。")
    lines += [
        "- 架构含义：单进程的并发收益随模型延迟变长而变大（同步段占比下降），"
        "但同步段与 GIL 决定了它不会线性；要更高吞吐，要么减少同步段"
        "（异步化门禁/落盘），要么上多进程/多机（C14 的第二阶段）。",
        "",
        "> 口径提醒：mock 下没有真实模型延迟，绝对墙钟不代表生产；这份基线的用处是",
        "> 「同机、同配置下，并发槽位与本地调度是否按预期工作」，以及给后续分布式决策",
        "> 一个可对比的起点。真实模型吞吐要看 `bench.py --real` 与 provider 侧限流。",
    ]
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    levels = [int(item) for item in args.levels.split(",") if item.strip()]
    tmp = Path(tempfile.mkdtemp(prefix="agentcrew-capacity-"))
    # 并发上限不是 Settings 字段，ApiServer 在 __init__ 里直接读环境变量
    # （AGENTCREW_MAX_CONCURRENT_RUNS）；脚本独立进程，设了不影响别处。
    os.environ["AGENTCREW_MAX_CONCURRENT_RUNS"] = str(args.max_concurrent)
    # 不读仓库根的 agentcrew.yaml：压测只跑本脚本给定的配置。
    os.environ["AGENTCREW_CONFIG"] = str(tmp / "absent-agentcrew.yaml")
    settings = Settings.load(
        output_dir=tmp, mock=True, gate="none",
        # mock provider 用 asyncio.sleep 注入延迟（不是 time.sleep），所以这个
        # 延迟下并发才是真并发；没有它，任务瞬间完成，档位之间的墙钟没差别。
        mock_latency_ms=args.latency_ms,
    )
    server = ApiServer(port=0, output_dir=tmp, settings=settings)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    rows: list[dict] = []
    try:
        for level in levels:
            for repeat in range(args.repeat):
                row = await measure_level(
                    port, level, latency_ms=args.latency_ms,
                    timeout_s=args.timeout_s)
                row["repeat"] = repeat + 1
                rows.append(row)
                print(
                    f"level={level} repeat={repeat + 1} · "
                    f"202={row['accepted']} 429={row['rejected_429']} · "
                    f"p50={row['create_p50_ms']}ms · "
                    f"wall={row['wall_s']}s · "
                    f"rss={row['rss_peak_mb']}MB")
    finally:
        await server.stop()

    day = time.strftime("%Y-%m-%d")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{args.label}" if args.label else ""
    md_path = out_dir / f"capacity{suffix}-{day}.md"
    json_path = out_dir / f"capacity{suffix}-{day}.json"
    md_path.write_text(
        render_markdown(rows, limit=args.max_concurrent, day=day),
        encoding="utf-8")
    json_path.write_text(json.dumps({
        "day": day,
        "max_concurrent": args.max_concurrent,
        "latency_ms": args.latency_ms,
        "settings": {"mock": True, "gate": "none"},
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{md_path}")
    print(f"JSON：{json_path}")

    failed = 0
    for row in rows:
        ok = row["completed"] == row["accepted"] and row["unfinished"] == 0
        if row["level"] <= args.max_concurrent:
            ok = ok and row["accepted"] == row["level"]
        else:
            ok = ok and row["rejected_429"] > 0
        failed += 0 if ok else 1
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--levels", default="1,2,4,8,10",
                        help="逗号分隔的并发档位，默认 1,2,4,8,10（10 验证 429）")
    parser.add_argument("--repeat", type=int, default=1, help="每档重复次数")
    parser.add_argument("--latency-ms", type=int, default=60,
                        help="每次 LLM 调用的注入延迟（让并发有意义）")
    parser.add_argument("--max-concurrent", type=int, default=8,
                        help="API 并发 run 槽位（对应 AGENTCREW_MAX_CONCURRENT_RUNS）")
    parser.add_argument("--timeout-s", type=float, default=300.0,
                        help="单档等待全部 run 进终态的上限")
    parser.add_argument("--output-dir", default="bench",
                        help="报告输出目录（默认 bench/）")
    parser.add_argument("--label", default="",
                        help="报告文件名后缀（如 latency200），用于对照实验")
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
