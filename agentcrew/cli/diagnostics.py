"""doctor / diff 命令的 CLI 薄包装（R66f 自 main.py 拆出）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from agentcrew.cli.common import _run_dir, _settings_from
from agentcrew.cli.diff import (
    diff_runs,
)
from agentcrew.cli.diff import (
    print_diff as print_run_diff,
)
from agentcrew.cli.doctor import (
    DoctorCheck,
    DoctorReport,
    run_diagnostics,
)
from agentcrew.cli.doctor import (
    print_report as print_doctor_report,
)
from agentcrew.crew.crew import (
    EXIT_CODES,
)
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunRecord,
)


def _cmd_doctor(args: argparse.Namespace) -> int:
    """``doctor``：跑环境/配置/依赖/连通性检查，打印健康报告。

    退出码：全部 pass 或仅有 warn → 0；有任何 fail → 3（``config``）。配置本身
    加载失败时也退 3，并在报告里给一条 ``config: fail``，而不是抛 traceback。
    """
    if getattr(args, "config", None):
        # 配置文件路径走 AGENTCREW_CONFIG 环境变量（config.py 的唯一读取点）。
        os.environ["AGENTCREW_CONFIG"] = args.config
    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:
        report = DoctorReport(
            ok=False,
            checks=[DoctorCheck(
                name="config", status="fail", message=f"配置加载失败：{exc}")],
        )
        if args.json:
            print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
        else:
            print_doctor_report(report)
        return EXIT_CODES["config"]
    report = asyncio.run(run_diagnostics(settings))
    if args.json:
        print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
    else:
        print_doctor_report(report)
    return EXIT_CODES["ok"] if report.ok else EXIT_CODES["config"]


def _cmd_diff(args: argparse.Namespace) -> int:
    """``diff <run_a> <run_b>``：对比两个 run 的配置/任务/产物/事件差异。

    退出码：两个 run 都存在 → 0（diff 是信息性命令，有差异也不报错）；任一 run
    不存在 → 3（``config``，与 status / delete / rewind 的「run 不存在」一致）；
    run 存在但读不出来（run.json 损坏等）→ 1（``failed``）。
    ``--json`` 输出结构化差异供脚本解析。
    """
    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CODES["config"]

    def _load(run_id: str) -> tuple[CheckpointStore, RunRecord]:
        run_dir = _run_dir(settings, run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run 不存在: {run_id}")
        store = CheckpointStore(run_dir)
        return store, store.load_record()

    try:
        store_a, record_a = _load(args.run_id_a)
        store_b, record_b = _load(args.run_id_b)
    except FileNotFoundError as exc:
        # 2026-11-18 审计 P2-4：run 不存在是**输入错误**，不是「对比失败」。
        # 此前这里返回 1，与 status / delete / rewind 的 3 不一致——按退出码
        # 分诊的脚本会把「run_id 打错」误判成「run 执行失败」。
        print(f"无法读取 run：{exc}", file=sys.stderr)
        return EXIT_CODES["config"]
    except (OSError, ValueError) as exc:
        print(f"无法读取 run：{exc}", file=sys.stderr)
        return EXIT_CODES["failed"]

    diff = diff_runs(record_a, record_b, store_a, store_b)
    if args.json:
        print(json.dumps(diff.model_dump(), ensure_ascii=False, indent=2))
    else:
        print_run_diff(diff)
    return EXIT_CODES["ok"]

