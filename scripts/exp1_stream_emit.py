"""EXP-1（R66i）：流式逐 chunk 审计 emit 的事件循环阻塞测量。

机制（SOURCE）：base_agent._invoke_model 的 ``async for`` 里每 chunk 同步调
``Ledger.emit`` → open("a")+write+flush+os.fsync+model_validate_json，fsync
阻塞事件循环；同进程其他 run 与 SSE 心跳被卡。

边界声明：**组件边界**实验——复刻消费循环形状（mock 流 ≥500 chunk × 并发
{1,4}），测真实 Ledger 的落盘成本与循环滞后；差量不外推到端到端 run。

候选（单 delta）：落盘半 ``write_event`` 经 asyncio.to_thread 下沉，观察者半
``notify`` 留在循环线程（EventBus 的 asyncio.Queue 非线程安全，整体下沉会破坏
跨边界语义——这是候选 a 的唯一安全形态）。

oracle：同 fingerprint 下 baseline/candidate 的 ledger 行（去 ts）逐条一致、
观察者回调序列逐条一致。mutant：drop-frame 丢一帧落盘 / skip-notify 丢观察者
回调，oracle 必须红。

门槛预登记：采样前先跑 3 次 baseline-only 重复，noise=(max-min)/median；
改善 = candidate 墙钟中位数 ≤ baseline×(1-noise) 且滞后 p95 下降；
回归守卫 = candidate 墙钟中位数 ≤ baseline×(1+noise)。无来源数字不预填。

用法：
    python scripts/exp1_stream_emit.py                 # 正式测量
    python scripts/exp1_stream_emit.py --mutant drop-frame
    python scripts/exp1_stream_emit.py --mutant skip-notify
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 允许 `python scripts/exp1_stream_emit.py` 直接跑
    sys.path.insert(0, str(REPO_ROOT))

from agentcrew.runtime.ledger import Ledger  # noqa: E402

CHUNK_DELTA = "增量块文本" * 2  # 每帧 10 字符，贴近真实增量粒度


async def mock_stream(chunks: int):
    for _ in range(chunks):
        await asyncio.sleep(0)  # 协作让出：真实流式每帧来自网络 await
        yield SimpleNamespace(delta=CHUNK_DELTA)


async def consume_baseline(ledger: Ledger, task_id: str, chunks: int) -> None:
    async for chunk in mock_stream(chunks):
        ledger.emit("agent_stream_chunk", task_id=task_id, role="coder",
                    delta_length=len(chunk.delta))


async def consume_candidate(
    ledger: Ledger, task_id: str, chunks: int, mutant: str,
) -> None:
    index = 0
    async for chunk in mock_stream(chunks):
        if mutant == "drop-frame" and index == chunks // 2:
            index += 1
            continue  # 故意丢一帧：oracle 必须红
        written = await asyncio.to_thread(
            ledger.write_event, "agent_stream_chunk", task_id=task_id,
            role="coder", delta_length=len(chunk.delta))
        if mutant != "skip-notify":
            ledger.notify("agent_stream_chunk", written)
        index += 1


#: 自适应变体的并发计数（镜像生产机制：仅循环线程读写，无需锁）。
_ACTIVE = 0


async def consume_adaptive(
    ledger: Ledger, task_id: str, chunks: int, mutant: str,
) -> None:
    """EXP-1 裁决后的采纳形态：仅当同进程并发流 >1 时下沉落盘半。

    单流保持同步 emit（c1 实测 to_thread dispatch 开销是回归）；多流下沉
    （c4 实测 −62.8% 墙钟、滞后 p95 27→10ms）。逐帧 await 语义不变。
    """
    global _ACTIVE
    _ACTIVE += 1
    try:
        index = 0
        async for chunk in mock_stream(chunks):
            if mutant == "drop-frame" and index == chunks // 2:
                index += 1
                continue
            if _ACTIVE > 1:
                written = await asyncio.to_thread(
                    ledger.write_event, "agent_stream_chunk", task_id=task_id,
                    role="coder", delta_length=len(chunk.delta))
                if mutant != "skip-notify":
                    ledger.notify("agent_stream_chunk", written)
            elif mutant == "skip-notify":
                ledger.write_event("agent_stream_chunk", task_id=task_id,
                                   role="coder", delta_length=len(chunk.delta))
            else:
                ledger.emit("agent_stream_chunk", task_id=task_id,
                            role="coder", delta_length=len(chunk.delta))
            index += 1
    finally:
        _ACTIVE -= 1


async def lag_probe(stop: asyncio.Event, drifts: list[float]) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        t0 = loop.time()
        await asyncio.sleep(0.005)
        drifts.append(loop.time() - t0 - 0.005)


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


_RUN_SEQ = itertools.count()


async def run_variant(
    variant: str, concurrency: int, chunks: int, root: Path, mutant: str,
) -> dict[str, Any]:
    # 每次 run 独立子目录：ledger 是 append 模式，复用文件会跨 run 累积，
    # 污染行数 oracle 与 fsync 成本（首版踩过）。
    root = root / f"run-{next(_RUN_SEQ)}"
    root.mkdir(parents=True)
    ledgers, observers = [], []
    for i in range(concurrency):
        seen: list[tuple[str, dict]] = []
        observers.append(seen)
        ledgers.append(Ledger(
            root / f"{variant}-{i}.jsonl", run_id="EXP1",
            observer=lambda e, d, _s=seen: _s.append((e, dict(d)))))
    stop = asyncio.Event()
    drifts: list[float] = []
    probe = asyncio.create_task(lag_probe(stop, drifts))
    cpu0 = time.process_time()
    t0 = time.perf_counter()
    if variant == "baseline":
        await asyncio.gather(*(
            consume_baseline(ledgers[i], f"T{i}", chunks)
            for i in range(concurrency)))
    elif variant == "adaptive":
        await asyncio.gather(*(
            consume_adaptive(ledgers[i], f"T{i}", chunks, mutant)
            for i in range(concurrency)))
    else:
        await asyncio.gather(*(
            consume_candidate(ledgers[i], f"T{i}", chunks, mutant)
            for i in range(concurrency)))
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    stop.set()
    await probe
    # oracle 载荷：ledger 行（去 ts）+ 观察者序列
    lines = []
    for i in range(concurrency):
        path = root / f"{variant}-{i}.jsonl"
        rows = [json.loads(ln) for ln in path.read_text("utf-8").splitlines() if ln]
        for row in rows:
            row.pop("ts", None)
        lines.append(rows)
    return {
        "variant": variant, "concurrency": concurrency, "wall": wall,
        "cpu": cpu, "lag_p95": p95(drifts),
        "lag_max": max(drifts, default=0.0), "lag_mean": statistics.fmean(drifts) if drifts else 0.0,
        "ledger_writes": sum(len(x) for x in lines),
        "oracle_lines": lines, "oracle_observer": observers,
    }


def oracle_equal(a: dict, b: dict) -> tuple[bool, str]:
    if a["oracle_lines"] != b["oracle_lines"]:
        na = sum(len(x) for x in a["oracle_lines"])
        nb = sum(len(x) for x in b["oracle_lines"])
        return False, f"ledger 行不一致（{na} vs {nb}）"
    if a["oracle_observer"] != b["oracle_observer"]:
        return False, "观察者回调序列不一致"
    return True, ""


async def measure(concurrency: int, chunks: int, pairs: int, mutant: str,
                  variant: str = "candidate") -> dict:
    with tempfile.TemporaryDirectory(prefix="exp1-") as tmp:
        root = Path(tmp)
        # 预热（线程池首次 dispatch / 文件系统缓存），不计样本
        await run_variant("baseline", concurrency, chunks, root, "")
        await run_variant(variant, concurrency, chunks, root, "")
        if mutant:
            a = await run_variant("baseline", concurrency, chunks, root, "")
            b = await run_variant(variant, concurrency, chunks, root, mutant)
            ok, why = oracle_equal(a, b)
            return {"mutant": mutant, "concurrency": concurrency,
                    "killed": not ok, "reason": why}
        # 门槛预登记：3 次 baseline-only 重复估计噪声
        base_walls = [
            (await run_variant("baseline", concurrency, chunks, root, ""))["wall"]
            for _ in range(3)
        ]
        med = statistics.median(base_walls)
        noise = (max(base_walls) - min(base_walls)) / med if med > 0 else 0.0
        thresholds = {
            "noise_rel_spread": noise,
            "improve_wall_factor": 1 - noise,
            "regression_wall_factor": 1 + noise,
            "lag_p95_must_decrease": True,
        }
        samples, oracle_failures = [], []
        for pair in range(pairs):
            order = ("baseline", variant) if pair % 2 == 0 else (variant, "baseline")
            runs = {}
            for v in order:
                runs[v] = await run_variant(v, concurrency, chunks, root, "")
            ok, why = oracle_equal(runs["baseline"], runs[variant])
            if not ok:
                oracle_failures.append({"pair": pair, "reason": why})
            samples.append({
                "pair": pair, "order": list(order),
                "baseline": {k: runs["baseline"][k] for k in
                             ("wall", "cpu", "lag_p95", "lag_max", "lag_mean", "ledger_writes")},
                "candidate": {k: runs[variant][k] for k in
                              ("wall", "cpu", "lag_p95", "lag_max", "lag_mean", "ledger_writes")},
            })
        bw = statistics.median(s["baseline"]["wall"] for s in samples)
        cw = statistics.median(s["candidate"]["wall"] for s in samples)
        bl = statistics.median(s["baseline"]["lag_p95"] for s in samples)
        cl = statistics.median(s["candidate"]["lag_p95"] for s in samples)
        if oracle_failures or cw > bw * thresholds["regression_wall_factor"]:
            verdict = "REJECT"
        elif cw <= bw * thresholds["improve_wall_factor"] and cl < bl:
            verdict = "ACCEPT"
        else:
            verdict = "REMEASURE"
        return {
            "concurrency": concurrency, "verdict": verdict,
            "thresholds": thresholds, "baseline_repeat_walls": base_walls,
            "median_wall_baseline": bw, "median_wall_candidate": cw,
            "median_lag_p95_baseline": bl, "median_lag_p95_candidate": cl,
            "oracle_failures": oracle_failures, "samples": samples,
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=int, default=500)
    parser.add_argument("--pairs", type=int, default=7)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--mutant", choices=["drop-frame", "skip-notify"], default="")
    parser.add_argument("--variant", choices=["candidate", "adaptive"], default="candidate")
    parser.add_argument("--out", type=Path, default=Path(".exp1-r66.json"))
    args = parser.parse_args()
    results = []
    for concurrency in args.concurrency:
        result = await measure(concurrency, args.chunks, args.pairs, args.mutant, args.variant)
        results.append(result)
        if args.mutant:
            tag = "MUTANT_KILLED" if result["killed"] else "MUTANT_SURVIVED"
            print(f"[{tag}] concurrency={concurrency} mutant={args.mutant} "
                  f"reason={result['reason'] or 'oracle 通过（不应该）'}")
        else:
            print(f"[{result['verdict']}] concurrency={concurrency} "
                  f"wall {result['median_wall_baseline']:.3f}s → "
                  f"{result['median_wall_candidate']:.3f}s | "
                  f"lag_p95 {result['median_lag_p95_baseline']*1000:.2f}ms → "
                  f"{result['median_lag_p95_candidate']*1000:.2f}ms | "
                  f"noise={result['thresholds']['noise_rel_spread']:.3f}")
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
    print(f"raw samples → {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
