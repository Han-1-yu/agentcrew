"""从已经落盘的 eval run 目录重算断言（进程中途崩溃后的证据回收）。

`main.py eval` 只在**全部跑完**后打印汇总；本机进程级崩溃（访问冲突 / CLR 错）
会让已完成 case 的结论一起丢失。产物与 ledger 都还在盘上，因此可以按
``run_case`` 同一口径（``_run_crew`` 读盘方式）重建 :class:`CaseOutput` 再逐条
重算断言——**不重新调用模型，不产生任何费用**。

用法::

    python scripts/regrade_eval.py examples/evals/capability-live-rest-a.jsonl \
        --output-dir output/_eval

约定：case 目录名为 ``<case-id>-<k>``（与 ``run_case`` 一致），其下最后一个含
``run.json`` 的子目录就是该次 run。缺目录的 case 记为「未跑」。

**注意**：同一 case 多次跑会留下多个时间戳目录，「最新」按目录名（即时间戳）
取。若目录里存在**前一轮**的旧 run，请用 ``--since`` 限定只认这之后的目录，
否则旧结论会被当成本轮结果（本脚本踩过这个坑）。

退出码：全部通过 0，有失败 1，有未跑 2（便于脚本判断"还要不要补跑"）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentcrew.evals.case import CaseOutput, EvalCase  # noqa: E402
from agentcrew.evals.checks import evaluate_checks  # noqa: E402
from agentcrew.evals.runner import _readartifacts, _shrink  # noqa: E402
from agentcrew.runtime.checkpoint import CheckpointStore  # noqa: E402
from agentcrew.runtime.ledger import Ledger  # noqa: E402

_MAX_EVENTS = 800


def _load_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(EvalCase.model_validate(json.loads(line)))
    return cases


def _latest_run_dir(case_dir: Path, since: datetime | None = None) -> Path | None:
    if not case_dir.is_dir():
        return None
    candidates = [p for p in case_dir.iterdir() if p.is_dir() and (p / "run.json").is_file()]
    if since is not None:
        # 目录名是 ``YYYYMMDD-HHMMSS-xxxx``，直接按字符串比较即可（同格式定长）。
        stamp = since.strftime("%Y%m%d-%H%M%S")
        candidates = [p for p in candidates if p.name >= stamp]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.name)


def _collect(run_dir: Path) -> CaseOutput:
    """与 ``runner._run_crew`` 同一读盘口径，唯一差别是 run_id 取目录名。"""
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    board = CheckpointStore(run_dir).load_board()
    counts: dict[str, int] = {}
    events: list[dict] = []
    for event in Ledger(run_dir / "ledger.jsonl", run_id=run_dir.name).events():
        counts[event.event] = counts.get(event.event, 0) + 1
        if len(events) < _MAX_EVENTS:
            events.append({"event": event.event, "data": _shrink(event.data)})
    stats = record.get("stats") or {}
    return CaseOutput(
        status=str(record.get("status") or ""),
        run_id=run_dir.name,
        artifacts=_readartifacts(run_dir / "artifacts"),
        task_status={t.id: t.status.value for t in board.all()},
        event_counts=counts,
        events=events,
        cost_usd=float(stats.get("estimated_cost_usd") or 0.0),
        # run 级失败原因必须带回来：``evaluate_checks`` 见到 error 就把全部断言判
        # 失败（防「运行失败但 artifact_not_contains 恰好通过」的假绿）。少这一步
        # 会让规划期就崩掉的 reject 用例被误判成通过。
        error=str(record.get("error") or ""),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("output/_eval"))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--since",
        default="",
        help="只认这个时间之后的 run 目录（YYYY-MM-DDTHH:MM 或 YYYYMMDD-HHMMSS）",
    )
    args = parser.parse_args(argv)

    since: datetime | None = None
    if args.since:
        raw = args.since.strip()
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y%m%d-%H%M%S"):
            try:
                since = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if since is None:
            print(f"--since 无法解析：{args.since!r}", file=sys.stderr)
            return 3

    cases = _load_cases(args.dataset)
    missing: list[str] = []
    failed: list[str] = []
    total_cost = 0.0
    print(f"{'case':<28} {'status':<10} {'checks':<8} cost")
    for case in cases:
        results = []
        for k in range(1, args.repeat + 1):
            run_dir = _latest_run_dir(args.output_dir / f"{case.id}-{k}", since)
            if run_dir is None:
                missing.append(case.id)
                continue
            output = _collect(run_dir)
            results.extend(evaluate_checks(case.checks, output))
            total_cost += output.cost_usd
        if not results:
            print(f"{case.id:<28} {'未跑':<10} {'-':<8} -")
            continue
        ok = sum(1 for r in results if r.ok)
        bad = [r for r in results if not r.ok]
        status = "PASS" if not bad else "FAIL"
        if bad:
            failed.append(case.id)
        print(f"{case.id:<28} {status:<10} {ok}/{len(results):<7} {output.cost_usd:.4f}")
        for r in bad:
            print(f"    ✗ {r.kind}: {r.detail[:200]}")

    passed = len(cases) - len(failed) - len(set(missing))
    print("-" * 66)
    print(
        f"通过 {passed}/{len(cases)} · 失败 {len(set(failed))} · "
        f"未跑 {len(set(missing))} · 合计成本 ${total_cost:.4f}"
    )
    if failed:
        return 1
    if missing:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
