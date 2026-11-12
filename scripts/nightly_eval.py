"""C11 夜间真实模型评测：一条命令跑完、落盘、给出可引用的结论。

**为什么要单独一个脚本。** ``main.py eval --live`` 已经把能力跑通了，但「夜间
限额跑一次」还需要三件它不该管的事：

1. **预算护栏**：真实通道必须带 ``--max-cost``，且脚本自己在开跑前确认有密钥；
   没有密钥就明确退出，而不是发起一堆注定失败的调用。
2. **可追溯**：每次结果落盘到 ``bench/eval-live-<日期>.json``，报告里能引用
   「哪一天、哪个数据集、多少次、什么成本」。
3. **结论摘要**：pass^k、成本、失败归因（哪类 check 挂得最多）直接打出来，
   不用人去 JSON 里数。

用法（建议放 cron / 计划任务，夜间跑）：

```bash
python scripts/nightly_eval.py --max-cost 1.50
python scripts/nightly_eval.py --dataset examples/evals/smoke.jsonl --repeat 2   # 离线冒烟
```

**故意不做的**：不自动改阈值、不自动提交、不自动切模型。评测输出是证据，
不是决策——决策留给人。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 真实通道可用的密钥环境变量（任一存在即视为「有密钥」）。
_KEY_ENVS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "ARK_API_KEY", "DASHSCOPE_API_KEY", "QIANFAN_API_KEY",
)

DEFAULT_DATASET = "examples/evals/capability-live.jsonl"
DEFAULT_MAX_COST = 1.50


def _has_key() -> bool:
    return any(os.environ.get(name, "").strip() for name in _KEY_ENVS)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="C11 夜间真实模型评测")
    parser.add_argument("--dataset", default=DEFAULT_DATASET,
                        help=f"数据集（默认 {DEFAULT_DATASET}）")
    parser.add_argument("--repeat", type=int, default=1, metavar="K",
                        help="每条 case 跑 K 次，用 pass^K 衡量稳定性（默认 1）")
    parser.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST,
                        metavar="USD", help=f"成本硬上限（默认 {DEFAULT_MAX_COST}）")
    parser.add_argument("--output-dir", default="output/_eval",
                        help="保留每次 run 的目录（默认 output/_eval）")
    parser.add_argument("--offline", action="store_true",
                        help="走 mock 通道（不产生费用，用于验证脚本本身）")
    parser.add_argument("--timeout", type=float, default=3600.0,
                        help="整批超时秒数（默认 3600）")
    return parser.parse_args(argv)


def _command(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable, str(ROOT / "main.py"), "eval", args.dataset,
        "--repeat", str(max(1, args.repeat)),
        "--json",
        "--output-dir", str(ROOT / args.output_dir),
    ]
    if not args.offline:
        cmd += ["--live", "--max-cost", str(args.max_cost)]
    return cmd


def _summarize(payload: dict) -> dict:
    """从 ``eval --json`` 的输出里提炼结论（pass^k / 成本 / 失败归因）。"""
    results = payload.get("results") or []
    failures: Counter[str] = Counter()
    for item in results:
        for check in item.get("checks") or []:
            if not check.get("ok"):
                failures[check.get("kind", "?")] += 1
    cost = sum(float(item.get("cost_usd") or 0.0) for item in results)
    calls = sum(int(item.get("llm_calls") or 0) for item in results)
    return {
        "cases": payload.get("cases"),
        "runs": payload.get("runs"),
        "repeat": payload.get("repeat"),
        "pass_at_1": payload.get("pass_at_1"),
        "pass_pow_k": payload.get("pass_pow_k"),
        "cost_usd": round(cost, 4),
        "llm_calls": calls,
        "failure_kinds": dict(failures),
        "failed_cases": sorted(
            {item["case_id"] for item in results if not item.get("ok")}),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.offline and not _has_key():
        print(
            "未检测到任何 provider 密钥（OPENAI_API_KEY / ANTHROPIC_API_KEY / "
            "DEEPSEEK_API_KEY …）。真实评测需要密钥；"
            "只想验证脚本本身可加 --offline。",
            file=sys.stderr,
        )
        return 3
    cmd = _command(args)
    started = time.time()
    print("跑：" + " ".join(cmd), flush=True)
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"评测超时（>{args.timeout:g}s），已中止", file=sys.stderr)
        return 4
    duration = round(time.time() - started, 1)
    if proc.returncode != 0 and not proc.stdout.strip():
        print(proc.stderr[-4000:], file=sys.stderr)
        return proc.returncode or 1
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("评测输出不是 JSON，无法落盘。原始输出：", file=sys.stderr)
        print((proc.stdout or proc.stderr)[-4000:], file=sys.stderr)
        return 2

    summary = _summarize(payload)
    stamp = time.strftime("%Y-%m-%d")
    # 文件名带数据集与通道：同一天跑 mock 与真实、或跑两份数据集时不会互相覆盖。
    stem = Path(args.dataset).stem
    kind = "offline" if args.offline else "live"
    target = ROOT / "bench" / f"eval-{stem}-{kind}-{stamp}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "date": stamp,
        "dataset": args.dataset,
        "offline": bool(args.offline),
        "duration_seconds": duration,
        "max_cost_usd": None if args.offline else args.max_cost,
        "summary": summary,
        "report": payload,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"数据集 {args.dataset} · {summary['runs']} 次运行 · {duration}s")
    print(f"pass@1 = {summary['pass_at_1']:.2%} · "
          f"pass^{summary['repeat']} = {summary['pass_pow_k']:.2%}")
    print(f"成本 ${summary['cost_usd']:.4f} · LLM 调用 {summary['llm_calls']} 次")
    if summary["failure_kinds"]:
        kinds = "、".join(f"{k}×{v}" for k, v in
                          sorted(summary["failure_kinds"].items()))
        print(f"失败归因：{kinds}")
    if summary["failed_cases"]:
        print("未通过：" + "、".join(summary["failed_cases"]))
    print(f"报告已写入 {target.relative_to(ROOT).as_posix()}")
    return 0 if (summary["pass_pow_k"] or 0.0) >= 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
