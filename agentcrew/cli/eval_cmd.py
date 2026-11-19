"""eval / samples 命令与评测矩阵装配（R66f 自 main.py 拆出）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path

from rich.console import Console

from agentcrew.cli.common import _fail, _out
from agentcrew.crew.crew import (
    EXIT_CODES,
)


def _cmd_eval(args: argparse.Namespace) -> int:
    """``eval``：跑离线评测集，用产物断言 + ``pass^k`` 给出可复现的结论。

    退出码：全部 case 的 K 次运行都通过 → 0；有任一 case 没过 → 1（与「测试失败」
    同一口径）；数据集本身不合法 / 运行环境错误 → 3（配置类）。

    刻意**不**做「部分通过也算过」：评测的价值就在于是非题。想只看趋势就加
    ``--json`` 自己算，别让工具替人下结论。
    """
    from agentcrew.evals import DatasetError, load_dataset, run_dataset

    try:
        cases = load_dataset(args.dataset)
    except DatasetError as exc:
        return _fail(f"数据集不合法：{exc}")
    repeat = max(1, int(getattr(args, "repeat", 1) or 1))
    output_dir = getattr(args, "output_dir", None)
    live = bool(getattr(args, "live", False))
    base = _eval_base_settings(args)
    if isinstance(base, str):  # 参数非法，已经是可展示的错误文案
        return _fail(base)
    variants_raw = getattr(args, "variant", None)
    if variants_raw:
        return _eval_matrix(args, cases, repeat, output_dir, variants_raw, base)
    try:
        report = asyncio.run(run_dataset(
            cases, repeat=repeat, base_settings=base, live=live,
            output_dir=Path(output_dir) if output_dir else None))
    except Exception as exc:  # noqa: BLE001 —— 评测跑不起来是环境/配置问题
        return _fail(f"评测执行失败：{type(exc).__name__}: {exc}")

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        console = Console()
        _out(console, f"评测集 {args.dataset} · {len(cases)} 条 case · "
                      f"每条跑 {repeat} 次")
        grouped = report.by_case()
        for case_id, runs in grouped.items():
            bad = [run for run in runs if not run.ok]
            # ``_out`` 走 ``Text``（rich 的标记会被当字面量），所以这里用纯文本
            # 而不是 [green] 标记——宁可朴素，也不要控制台里出现 "[/]"。
            mark = "PASS" if not bad else "FAIL"
            _out(console, f"  {mark} {case_id}  "
                          f"{len(runs) - len(bad)}/{len(runs)} 次通过")
            for run in bad[:1]:
                for failure in run.failures:
                    _out(console, f"       - {failure.kind}: {failure.detail}")
                if run.error:
                    _out(console, f"       - 运行错误：{run.error}")
        _out(console, "")
        _out(console, f"pass@1 = {report.pass_at_1:.2%} · "
                      f"pass^{repeat} = {report.pass_pow_k:.2%}")
    return EXIT_CODES["ok"] if report.pass_pow_k >= 1.0 else EXIT_CODES["failed"]


def _eval_base_settings(args: argparse.Namespace):
    """C11：评测的基线 Settings——``--live`` 切真实通道、``--max-cost`` 设硬上限。

    返回 ``Settings``，或在参数非法时返回错误文案字符串（调用方 ``_fail``）。
    预算校验刻意放在这里：``nan`` / ``inf`` 与成本比较恒为 False，等于没设上限。
    """
    from agentcrew.config import Settings

    settings = Settings.load()
    max_cost = getattr(args, "max_cost", None)
    if max_cost is not None:
        if isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)):
            return "--max-cost 必须是数值"
        if not math.isfinite(float(max_cost)) or float(max_cost) < 0:
            return "--max-cost 必须是 >= 0 的有限数"
        settings = settings.model_copy(update={"max_cost_usd": float(max_cost)})
    if getattr(args, "live", False):
        settings = settings.model_copy(update={"mock": False})
    return settings


def _eval_matrix(
    args: argparse.Namespace, cases: list, repeat: int,
    output_dir: str | None, variants_raw: list[str], base_settings=None,
) -> int:
    """``eval --variant``：同一批 case × 多套配置，给出可对比的数字。

    退出码与单配置一致：**基线**变体的 pass^k 全过才算 0。其余变体只作对照，
    不参与判定——否则「换便宜档掉了一个点」会让整条命令失败，人就学会忽略它。
    """
    from agentcrew.evals import load_variant_file, run_matrix

    variants: list[tuple[str, dict]] = []
    for item in variants_raw:
        name, sep, path = str(item).partition("=")
        if not sep or not name.strip() or not path.strip():
            return _fail(f"--variant 需要 NAME=FILE 形式：{item!r}")
        try:
            variants.append((name.strip(), load_variant_file(path.strip())))
        except ValueError as exc:
            return _fail(f"变体不合法：{exc}")
    try:
        matrix = asyncio.run(run_matrix(
            variants, cases, repeat=repeat, base_settings=base_settings,
            live=bool(getattr(args, "live", False)),
            output_dir=Path(output_dir) if output_dir else None))
    except ValueError as exc:
        return _fail(f"矩阵评测无法开始：{exc}")
    except Exception as exc:  # noqa: BLE001 —— 跑不起来属环境/配置问题
        return _fail(f"矩阵评测执行失败：{type(exc).__name__}: {exc}")

    data = matrix.to_dict()
    out_path = getattr(args, "matrix_out", None)
    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        console = Console()
        _out(console, f"配置矩阵 · {len(cases)} 条 case × {len(variants)} 个变体 · "
                      f"每条跑 {repeat} 次")
        for item in data["variants"]:
            metrics, delta = item["metrics"], item["delta_vs_baseline"]
            tail = ""
            if delta:
                tail = (
                    f"   Δpass^k {delta['pass_pow_k']:+.2f}"
                    f" · Δcalls {delta['llm_calls']:+.0f}"
                    f" · Δcost ${delta['cost_usd']:+.4f}"
                    f" · Δ修订 {delta['revise_rounds']:+.0f}")
            _out(console, f"  {item['name']:<14} pass^k={metrics['pass_pow_k']:.2%}"
                          f"  calls={metrics['llm_calls']}"
                          f"  cost=${metrics['cost_usd']:.4f}"
                          f"  修订={metrics['revise_rounds']}{tail}")
        if out_path:
            _out(console, f"\n矩阵已写入 {out_path}")
    baseline = matrix.baseline
    return (EXIT_CODES["ok"]
            if baseline is not None and baseline.report.pass_pow_k >= 1.0
            else EXIT_CODES["failed"])


def _cmd_samples(args: argparse.Namespace) -> int:
    """``samples [name]``：列出预设 Crew 样例。

    每个样例一行：名字 / 形状 / 任务数 / 目标摘要，随后给出可直接复制的运行命令。
    逐样例分块而不是大表格 —— 目标句很长，表格会把它压成一行省略号。
    """
    from examples.samples import SAMPLES, get, names

    if args.name:
        try:
            specs = [get(args.name)]
        except KeyError as exc:
            return _fail(str(exc).strip("'\""))
    else:
        specs = [SAMPLES[name] for name in names()]

    if args.json:
        payload = [
            {
                "name": spec.name,
                "title": spec.title,
                "shape": spec.shape,
                "goal": spec.goal,
                "tasks": spec.expected_total_tasks,
                "expected_task_ids": list(spec.expected_task_ids),
                "fixtures": str(spec.fixtures),
            }
            for spec in specs
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]

    console = Console()
    for spec in specs:
        _out(console, f"[{spec.name}] {spec.title} · {spec.shape} · {spec.expected_total_tasks} 个任务")
        _out(console, f"  目标  {spec.goal}")
        _out(console, f"  运行  python main.py run --sample {spec.name} --no-live")
        _out(console, "")
    _out(console, f"共 {len(SAMPLES)} 个样例；全部离线可跑（自动开启 --mock，无需任何 API Key）。")
    return EXIT_CODES["ok"]

