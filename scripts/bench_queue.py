"""C14：多 worker 横向扩展验收——同一队列，1 / 2 / 4 个 worker 的吞吐对比。

    python scripts/bench_queue.py                       # 24 个 run，1/2/4 worker
    python scripts/bench_queue.py --runs 48 --workers 1,2,4,8 --concurrency 2

**怎么测**

1. 起一个队列模式的 `ApiServer`（mock、零费用），`POST /runs` 只入队；
2. 起 N 个 `main.py worker --once` 子进程消费同一个队列目录；
3. 入队 `--runs` 个 run，轮询到全部终态，量墙钟与吞吐；
4. 同一份代码、同一台机器，只改 worker 数——吞吐曲线就是横向扩展的证据。

**边界**：mock provider + 固定 LLM 延迟，测的是「多进程调度与文件队列」的扩展性，
不是模型侧吞吐；`--concurrency` 是每个 worker 内并发的 run 数。产物：
``bench/queue-<日期>.md`` 与同名 ``.json``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agentcrew.api.server import ApiServer  # noqa: E402
from agentcrew.config import Settings  # noqa: E402
from agentcrew.runtime.queue import RunQueue  # noqa: E402

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


async def _http_json(port: int, method: str, path: str, payload: dict | None = None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    data = b"" if payload is None else json.dumps(payload).encode("utf-8")
    head = (
        f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
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


def _workers(env: dict[str, str], queue_dir: Path, output_dir: Path,
             count: int, concurrency: int) -> list[subprocess.Popen]:
    procs = []
    for _ in range(count):
        procs.append(subprocess.Popen(
            [sys.executable, "main.py", "worker",
             "--queue-dir", str(queue_dir), "--output-dir", str(output_dir),
             "--once", "--auto", "--mock",
             "--concurrency", str(concurrency), "--poll-seconds", "0.2"],
            cwd=REPO, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        ))
    return procs


async def measure(port: int, queue: RunQueue, *, runs: int, timeout_s: float):
    run_ids: list[str] = []
    for index in range(runs):
        status, data = await _http_json(port, "POST", "/runs", {
            "goal": f"队列基线 {index}：写一个 slugify 函数", "options": {},
        })
        if status == 202 and isinstance(data, dict):
            run_ids.append(str(data["run_id"]))
    started = time.perf_counter()
    pending = set(run_ids)
    completed = 0
    failed = 0
    deadline = started + timeout_s
    while pending and time.perf_counter() < deadline:
        for run_id in list(pending):
            status, body = await _http_json(
                port, "GET", f"/runs/{run_id}/status")
            state = str((body or {}).get("status") or "").lower() \
                if isinstance(body, dict) else ""
            if state in TERMINAL:
                pending.discard(run_id)
                if state == "completed":
                    completed += 1
                else:
                    failed += 1
        if pending:
            await asyncio.sleep(0.3)
    wall = time.perf_counter() - started
    return {
        "enqueued": len(run_ids),
        "completed": completed,
        "failed": failed,
        "unfinished": len(pending),
        "wall_s": round(wall, 2),
        "throughput_per_min": round(completed / wall * 60, 2) if wall else None,
    }


async def main_async(args: argparse.Namespace) -> int:
    worker_counts = [int(item) for item in args.workers.split(",") if item.strip()]
    rows = []
    for count in worker_counts:
        tmp = Path(tempfile.mkdtemp(prefix=f"agentcrew-queue-{count}-"))
        queue_dir = tmp / "_queue"
        queue = RunQueue(queue_dir)
        os.environ["AGENTCREW_MAX_CONCURRENT_RUNS"] = str(args.max_concurrent)
        os.environ["AGENTCREW_CONFIG"] = str(tmp / "absent-agentcrew.yaml")
        settings = Settings.load(
            output_dir=tmp, mock=True, gate="none",
            mock_latency_ms=args.latency_ms)
        server = ApiServer(port=0, output_dir=tmp, settings=settings, queue=queue)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        env = {
            **os.environ,
            "AGENTCREW_MOCK": "true",
            "AGENTCREW_GATE": "none",
            "AGENTCREW_CONFIG": str(tmp / "absent-agentcrew.yaml"),
            "AGENTCREW_MOCK_LATENCY_MS": str(args.latency_ms),
            "PYTHONUTF8": "1",
        }
        procs = _workers(env, queue_dir, tmp, count, args.concurrency)
        try:
            row = await measure(
                port, queue, runs=args.runs, timeout_s=args.timeout_s)
            row.update({"workers": count, "concurrency": args.concurrency,
                        "latency_ms": args.latency_ms})
            rows.append(row)
            print(f"workers={count} · 完成 {row['completed']}/{row['enqueued']} · "
                  f"wall={row['wall_s']}s · 吞吐={row['throughput_per_min']}/分")
        finally:
            await server.stop()
            for proc in procs:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.terminate()

    day = time.strftime("%Y-%m-%d")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = next((row for row in rows if row["workers"] == 1), None)
    lines = [
        f"# 多 worker 队列吞吐（{day}）",
        "",
        f"- 每档 {args.runs} 个 run；worker 内并发 {args.concurrency}；"
        f"mock 延迟 {args.latency_ms}ms；API 并发槽位 {args.max_concurrent}",
        "",
        "| worker 数 | 入队 | 完成 | 失败 | 墙钟 | 吞吐/分 | 相对 1 worker |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        speed = (
            round(row["throughput_per_min"] / base["throughput_per_min"], 2)
            if base and base["throughput_per_min"] else "—"
        )
        lines.append(
            f"| {row['workers']} | {row['enqueued']} | {row['completed']} | "
            f"{row['failed']} | {row['wall_s']} s | {row['throughput_per_min']} | "
            f"{speed}× |")
    lines += [
        "",
        "> 口径：mock provider（零费用）+ 固定延迟；测的是多进程/文件队列的扩展性，",
        "> 不是模型侧吞吐。worker 数超过 CPU 核数后，收益会受 GIL 之外的 IO 与",
        "> 磁盘竞争限制；这条曲线用来判断「加机器/加进程」是否还值得。",
    ]
    md_path = out_dir / f"queue-{day}.md"
    json_path = out_dir / f"queue-{day}.json"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps({
        "day": day, "runs": args.runs, "concurrency": args.concurrency,
        "latency_ms": args.latency_ms, "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{md_path}")
    return 0 if all(row["completed"] == row["enqueued"] for row in rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=24, help="每档入队的 run 数")
    parser.add_argument("--workers", default="1,2,4", help="逗号分隔的 worker 数")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="每个 worker 内并发的 run 数")
    parser.add_argument("--latency-ms", type=int, default=60,
                        help="每次 LLM 调用的注入延迟")
    parser.add_argument("--max-concurrent", type=int, default=64,
                        help="API 并发槽位（队列模式下不占槽位，这里只防误配）")
    parser.add_argument("--timeout-s", type=float, default=600.0)
    parser.add_argument("--output-dir", default="bench")
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
