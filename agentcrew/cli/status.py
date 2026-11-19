"""status / approvals / approve / clean / cancel / delete（R66f 自 main.py 拆出）。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Any, cast

from rich.console import Console

from agentcrew.cli.common import _fail, _out, _run_dir, _settings_from
from agentcrew.cli.run import _expire_overdue_approvals
from agentcrew.crew.crew import (
    EXIT_CODES,
    Crew,
)
from agentcrew.runtime.approvals import ApprovalError, ApprovalStore
from agentcrew.runtime.board_view import BoardView
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunStatus,
    clean,
    delete_run,
)
from agentcrew.runtime.guidance import GuidanceStore
from agentcrew.runtime.ledger import Ledger
from agentcrew.utils.clock import as_aware, format_local, utc_now


def _cmd_status(args: argparse.Namespace) -> int:
    """打印 ``run.json`` 记录 + ``tasks.json`` 任务表 + ledger 统计。

    ledger 读取端本来就宽容（文件不存在 = 零事件，见 ``Ledger.events``），这里
    直接读、不预先判存在。
    """
    try:
        settings = _settings_from(args)
        # R30：只结算本次查看的 run——status 不该顺手改别的 run。
        _expire_overdue_approvals(settings, run_id=args.run_id)
        run_dir = _run_dir(settings, args.run_id)
        checkpoint = CheckpointStore(run_dir)
        record = checkpoint.load_record()
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    console = Console()
    ledger = Ledger(
        run_dir / "ledger.jsonl", run_id=record.run_id,
        secrets=settings.secret_values())
    _out(console, f"━━━ AgentCrew run · {record.run_id} ━━━")
    _out(console, f"目标    {record.project_goal}")
    _out(console, f"状态    {record.status.value}")
    _out(
        console,
        f"创建    {format_local(record.created_at)}"
        f"    更新    {format_local(record.updated_at)}",
    )
    _out(
        console,
        f"ledger  {sum(1 for _ in ledger.events())} 条事件"
        f"    llm_call {ledger.count('llm_call')} 次",
    )
    board = None
    try:
        board = checkpoint.load_board()
    except (OSError, ValueError) as exc:
        _out(console, f"任务    （tasks.json 不可读：{exc}）")
    if board is not None:
        _out(console, Crew(settings).delivery_summary(record))
        view = BoardView(record.run_id, record.project_goal,
                         enabled=False, console=console)
        console.print(view.render(board, record))
    # R4：审批挂起的 run 在 status 里就能看到待办，不用先翻 ledger。
    approvals = ApprovalStore(run_dir).all()
    if approvals:
        pending = sum(1 for item in approvals if item.status == "pending")
        _out(
            console,
            f"审批单  {len(approvals)} 条    待裁决 {pending} 条"
            + (f"    （python main.py approvals {record.run_id}）" if pending else ""),
        )
    # C10：待吸收的中途指引同样要能在第一入口看见（否则人写了不知道吃没吃）。
    guidance = GuidanceStore(
        run_dir, secrets=tuple(settings.secret_values())).items()
    if guidance or settings.guidance_enabled:
        pending_guidance = sum(1 for item in guidance if item.consumed_at is None)
        _out(
            console,
            f"指引    {len(guidance)} 条    待吸收 {pending_guidance} 条"
            + ("    （guidance_enabled=false，不会被吸收）"
               if not settings.guidance_enabled else ""),
        )
    # R19：主管建议不参与裁决，但必须「有处可去」——否则它们只埋在 gate_result
    # 里没人看。status 是查看 run 的第一入口，在这里汇总最近几条。
    suggestion_events = list(ledger.events("supervisor_suggestions"))
    if suggestion_events:
        total = sum(
            len(event.data.get("suggestions") or [])
            for event in suggestion_events
        )
        task_count = len({
            event.data.get("task_id") for event in suggestion_events})
        _out(console, f"主管建议  {total} 条（{task_count} 个任务，不阻塞）")
        shown = 0
        for event in reversed(suggestion_events):
            data = event.data
            for text in (data.get("suggestions") or []):
                if shown >= 5:
                    _out(console, "  … 其余建议见 ledger 的 supervisor_suggestions 事件")
                    break
                _out(
                    console,
                    f"  · {data.get('task_id', '?')} "
                    f"[{data.get('supervisor', '?')} · 第{data.get('round', '?')}轮 · "
                    f"{data.get('verdict', '?')}] {text}",
                )
                shown += 1
            if shown >= 5:
                break
    return EXIT_CODES["ok"]


def _cmd_approvals(args: argparse.Namespace) -> int:
    """R4：列出一个 run 的全部审批单（跨进程可读）。"""
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    store = ApprovalStore(run_dir)
    store.expire_overdue()          # R30：先结算超时，展示的才是真实状态
    requests = store.all()
    if not requests:
        print(f"run {args.run_id} 没有审批单")
        return EXIT_CODES["ok"]
    console = Console()
    _out(console, f"━━━ 审批单 · {args.run_id} ━━━")
    for item in requests:
        action = item.decision.get("action") or "-"
        suffix = ""
        if item.status == "pending" and item.expires_at is not None:
            # R30 的超时是 aware UTC；旧审批单可能是 naive 本地 —— 直接和
            # ``datetime.now()`` 相减会 TypeError（CLI 整条命令打崩）。
            # expires_at 非空已由上面的守卫保证，as_aware 不会返回 None。
            deadline = cast("datetime", as_aware(item.expires_at))
            remaining = int((deadline - utc_now()).total_seconds())
            suffix = (f"  · 剩余 {remaining}s" if remaining > 0
                      else "  · 已超时（下次 status 会标记 expired）")
        elif item.status == "expired" and item.expires_at is not None:
            suffix = f"  · 超时于 {format_local(item.expires_at)}"
        _out(
            console,
            f"{item.id}  {item.checkpoint:<9} {item.status:<9} "
            f"action={action:<10} by={item.decided_by or '-'}{suffix}",
        )
    return EXIT_CODES["ok"]


def _cmd_approve(args: argparse.Namespace) -> int:
    """R4：在**另一个进程**里对审批单写裁决；之后用 ``resume`` 从卡点继续。"""
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    decision: dict[str, Any] = {"action": args.decision}
    if args.decision == "rework":
        decision["task_ids"] = list(args.task_ids or [])
    try:
        updated = ApprovalStore(run_dir).decide(
            args.request_id, decision, decided_by=args.by
        )
    except ApprovalError as exc:
        return _fail(f"裁决失败：{exc}")
    print(f"已裁决 {updated.id}（{updated.checkpoint}）：{updated.decision}")
    print(f"用 python main.py resume {args.run_id} 从卡点继续")
    return EXIT_CODES["ok"]


def _cmd_clean(args: argparse.Namespace) -> int:
    """按 run_id 倒序保留最近 ``--keep`` 个 run，删除其余（spec §13.5）。

    ``--dry-run`` 只列不删（:func:`agentcrew.runtime.checkpoint.clean` 的真跑与
    干跑共用同一套边界校验）。``--protect-running`` 显式开启活 run 保护：
    库层 ``clean`` 默认不保护以维持 spec §13.5 的既有契约，生产批量清理时
    由 CLI 开关决定是否跳过「有心跳的 running」，僵尸 RUNNING 始终可清理。
    """
    if args.keep < 0:
        return _fail("clean：--keep 必须 >= 0。")
    try:
        settings = _settings_from(args)
        removed = clean(settings.output_dir, keep=args.keep,
                        dry_run=args.dry_run,
                        protect_running=getattr(args, "protect_running", False))
    except (OSError, RuntimeError, ValueError) as exc:
        return _fail(f"clean 失败：{exc}")
    console = Console()
    action = "[dry-run] 将删除" if args.dry_run else "已删除"
    _out(
        console,
        f"{action} {len(removed)} 个 run（保留最近 {args.keep} 个）：{settings.output_dir}",
    )
    for path in removed:
        _out(console, f"  {path.name}")
    if not removed:
        _out(console, "没有需要清理的 run。")
    return EXIT_CODES["ok"]


def _cmd_cancel(args: argparse.Namespace) -> int:
    """给离线 run 置 ``cancelled`` 标记：下次 ``resume`` 直接以 CANCELLED 收尾（spec §13.4）。"""
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
        checkpoint = CheckpointStore(run_dir)
        record = checkpoint.load_record()
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    if record.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
        _out(Console(), f"run {record.run_id} 已结束（{record.status.value}），无需取消。")
        return EXIT_CODES["ok"]
    record.status = RunStatus.CANCELLED
    # D-14：run.json 的时间一律 aware UTC。这里曾写 ``datetime.now()``（naive 本地），
    # 结果同一个文件里 created_at 是 UTC、updated_at 是本地时间，东八区看上去
    # 「更新比创建晚 8 小时」；任何 ``updated_at - created_at`` 的读者还会直接抛
    # TypeError。cancel 是唯一一条在 run 之外改写这个字段的路径，最容易漏。
    record.updated_at = utc_now()
    checkpoint.save_record(record)
    _out(
        Console(),
        f"已把 run {record.run_id} 标记为 cancelled；下次 resume 将直接以 CANCELLED 收尾。",
    )
    return EXIT_CODES["ok"]


def _cmd_delete(args: argparse.Namespace) -> int:
    """删除单个 run 目录（P0-4，不可恢复）。

    打印 run 概要（目标/状态/时间/产物大小），TTY 下询问确认；``running`` 状态
    拒绝删除（退出码 3，提示先 cancel）；``--force`` 跳过确认。删除后打印释放字节数。
    """
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
        checkpoint = CheckpointStore(run_dir)
        record = checkpoint.load_record()
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")

    # running 状态拒绝删除（退出码 3 = config 类错误，与 spec 退出码表一致）
    if record.status is RunStatus.RUNNING:
        return _fail(
            f"run {record.run_id!r} 正在运行中，拒绝删除。"
            f"请先 cancel：python main.py cancel {record.run_id}",
        )

    console = Console()
    artifacts_dir = run_dir / "artifacts"
    artifact_size = sum(
        f.stat().st_size for f in artifacts_dir.rglob("*") if f.is_file()
    ) if artifacts_dir.is_dir() else 0
    _out(console, f"━━━ 将删除 run · {record.run_id} ━━━")
    _out(console, f"目标    {record.project_goal}")
    _out(console, f"状态    {record.status.value}")
    _out(
        console,
        f"创建    {format_local(record.created_at)}"
        f"    更新    {format_local(record.updated_at)}",
    )
    _out(console, f"产物    {artifact_size:,} 字节    目录    {run_dir}")

    if not args.force and sys.stdin.isatty():
        try:
            answer = input("确认删除？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            _out(console, "已取消删除。")
            return EXIT_CODES["ok"]

    try:
        freed = delete_run(settings.output_dir, record.run_id)
    except (OSError, RuntimeError, ValueError) as exc:
        return _fail(f"删除失败：{exc}")
    _out(console, f"已删除 run {record.run_id}，释放 {freed:,} 字节。")
    return EXIT_CODES["ok"]

