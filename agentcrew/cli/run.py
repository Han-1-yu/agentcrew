"""run / resume / rewind 命令与执行驱动（R66f 自 main.py 拆出）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from agentcrew.cli.common import (
    _apply_sample,
    _config_diff,
    _fail,
    _invalid_numbers,
    _load_cli_images,
    _out,
    _resolve_gate,
    _resume_hint,
    _resume_hint_notes,
    _run_dir,
    _settings_from,
    _stdin_isatty,
)
from agentcrew.config import Settings
from agentcrew.crew.crew import (
    EXIT_CODES,
    Crew,
    PlanFailedError,
    RunCancelled,
    build_catalog,
)
from agentcrew.crew.rewind import rewind_run
from agentcrew.models.factory import ModelFactoryError
from agentcrew.runtime.board_view import BoardView
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunRecord,
    RunStatus,
)
from agentcrew.runtime.ledger import LedgerEvent
from agentcrew.runtime.lifecycle import ResumeLockError, reconcile_stale_runs
from examples.todo_web_project import PROJECT_GOAL


def _expire_overdue_approvals(settings: Settings, *,
                              run_id: str | None = None) -> int:
    """R30：扫描审批单，把超窗口的标记为 ``expired``。

    只警告不改变 run 结局（仍是 interrupted）；每张过期单会在对应 run 的 ledger
    里留一条 ``approval_expired``——有接 Webhook 的部署据此告警。返回本次过期数量。
    扫描失败一律不阻断调用方（status / serve 启动是只读入口，不该被清理拖死）。

    ``run_id`` 给定时**只结算这一个 run**：``status`` 是查看单个 run 的命令，
    顺手清扫整目录会让它变成一个对别的 run 有副作用的「只读」命令（也曾让批量
    测试互相干扰）。全局清扫只在 ``serve`` 启动做一次。
    """
    from agentcrew.runtime.approvals import ApprovalStore
    from agentcrew.runtime.checkpoint import CheckpointStore
    from agentcrew.runtime.ledger import Ledger

    expired_total = 0
    if run_id is not None:
        run_ids = [run_id]
    else:
        try:
            run_ids = CheckpointStore.list_runs(settings.output_dir)
        except (OSError, ValueError):
            return 0
    for run_id in run_ids:
        run_dir = settings.output_dir / run_id
        try:
            store = ApprovalStore(run_dir)
            expired = store.expire_overdue()
        except (OSError, ValueError):
            continue
        if not expired:
            continue
        ledger = Ledger(run_dir / "ledger.jsonl", run_id=run_id)
        for request in expired:
            ledger.emit("approval_expired", request_id=request.id,
                        checkpoint=request.checkpoint,
                        expires_at=request.expires_at.isoformat()
                        if request.expires_at else None)
            expired_total += 1
        print(
            f"run {run_id}：{len(expired)} 张审批单已超时（"
            f"approval_timeout_seconds={settings.approval_timeout_seconds}），"
            "需要重新触发卡点或调大窗口后重跑",
            file=sys.stderr,
        )
    return expired_total


def _reconcile_on_startup(settings: Settings) -> None:
    """R17：启动时收敛孤儿 RUNNING；只警告，绝不因为清理失败阻断本次启动。

    库层直接调 ``Crew.run()`` 不走这里（程序化调用不该有隐性副作用），只有
    CLI ``run`` 与 ``serve`` 这两个进程入口做一次全目录扫描。
    """
    try:
        results = reconcile_stale_runs(settings.output_dir)
    except (OSError, ValueError) as exc:
        print(f"启动时 run 状态收敛跳过（{exc}）", file=sys.stderr)
        return
    for item in results:
        run_id = item["run_id"]
        if item.get("ok"):
            print(
                f"已把孤儿 run {run_id} 标记为 interrupted"
                f"（{item['reason']}），可用 resume 续跑",
                file=sys.stderr,
            )
        else:
            print(
                f"run {run_id} 状态收敛失败：{item.get('error')}",
                file=sys.stderr,
            )


def _cmd_run(args: argparse.Namespace) -> int:
    """``run --goal TEXT | --demo | --sample NAME``：三选一，否则退出码 3。"""
    chosen = [
        flag for flag, given in (
            ("--goal", args.goal), ("--demo", args.demo), ("--sample", args.sample))
        if given
    ]
    if len(chosen) > 1:
        return _fail(f"run：{' 与 '.join(chosen)} 互斥，只能给一个。")
    sample_goal, sample_error = _apply_sample(args)
    if sample_error is not None:
        return _fail(sample_error)
    if args.demo:
        goal: str | None = PROJECT_GOAL
    elif sample_goal is not None:
        goal = sample_goal
    else:
        goal = args.goal
    flow_arg = getattr(args, "flow", None)
    if not goal and not flow_arg:
        return _fail("run：必须给 --goal / --demo / --sample / --flow 之一。")
    invalid = _invalid_numbers(args)
    if invalid is not None:
        return _fail(invalid)
    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:  # pydantic ValidationError / YAML 解析错误
        return _fail(f"配置错误：{exc}")
    _reconcile_on_startup(settings)
    try:
        # 目录只建一次：CliPrompter 的编辑校验与 Crew 的调度必须看到同一批角色，
        # 否则编辑后的计划会被「角色未注册」误拒（roles.d 的自定义角色）。
        catalog = build_catalog(settings)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    console = Console()
    gate, prompter, downgraded = _resolve_gate(
        settings.gate, interactive=_stdin_isatty(), console=console, catalog=catalog,
        approval_mode=settings.approval_mode)
    if downgraded:
        print(
            "非 TTY（管道 / CI）：--gate 含人工卡点但无法交互，已显式降级为 "
            "gate=none（不会向任何人提问）。",
            file=sys.stderr,
        )
    settings = settings.model_copy(update={"gate": gate})
    preset_tasks = None
    if flow_arg:
        try:
            flow_spec = _load_flow_definition(settings, flow_arg)
        except ValueError as exc:
            return _fail(f"run --flow：{exc}")
        preset_tasks = flow_spec.tasks
        if not goal:
            goal = flow_spec.description or f"流程 {flow_spec.name}"
        _out(console, f"按流程 {flow_spec.name} 起跑：{len(preset_tasks)} 个任务"
                      "（跳过 PM 规划）")
    assert goal is not None
    hook = _BoardHook(goal, enabled=not args.no_live, console=console)
    images, image_error = _load_cli_images(getattr(args, "image", None))
    if image_error:
        return _fail(image_error)
    crew = Crew(settings, catalog=catalog, prompter=prompter, on_event=hook,
                preset_tasks=preset_tasks, images=images,
                trigger_source=getattr(args, "trigger_source", None))
    hook.bind(crew)
    return _drive(crew, lambda: crew.run(goal), hook=hook, console=console)


def _load_flow_definition(settings: Settings, source: str):
    """R28：``--flow`` 参数 → ``FlowSpec``（先按文件，再按流程库名字）。

    文件支持 ``.yaml`` / ``.yml``；否则当流程名去 ``<output>/_flows/<name>.yaml``
    找。两条路径都不通时抛 ``ValueError``（CLI 映射退出码 3）。图不合法同样抛
    ``ValueError``——**不带着坏流程起跑**，错误在开跑前就要看得见。
    """
    from agentcrew.flow.spec import (
        FlowError,
        load_flow,
        parse_flow_yaml,
        validate_flow,
    )

    try:
        candidate = Path(source)
        if candidate.suffix in (".yaml", ".yml") and candidate.is_file():
            spec = parse_flow_yaml(candidate.read_text(encoding="utf-8"))
        else:
            spec = load_flow(settings.output_dir, source)
    except (OSError, FlowError) as exc:
        raise ValueError(str(exc)) from exc
    errors = validate_flow(spec)
    if errors:
        raise ValueError("流程不合法：" + "；".join(errors))
    return spec


def _cmd_resume(args: argparse.Namespace) -> int:
    """``resume <run_id>``：先比对配置快照，不一致时要求 ``--force``（spec §13.3）。"""
    invalid = _invalid_numbers(args)
    if invalid is not None:
        return _fail(invalid)
    # 续跑样例时必须挂回同一套夹具，否则「已完成的任务能读到、未完成的读不到」。
    # 目标不参与：resume 的目标永远取 record.project_goal。
    _, sample_error = _apply_sample(args)
    if sample_error is not None:
        return _fail(sample_error)
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
        record = CheckpointStore(run_dir).load_record()
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    differences = _config_diff(record.config_snapshot, settings)
    if differences and not args.force:
        print("配置已变动，resume 的结果与首次运行不可比：", file=sys.stderr)
        for line in differences:
            print(f"  {line}", file=sys.stderr)
        # 用户真正要的是「怎么续跑」：给出补开关后的完整命令，而不只是 --force。
        print(
            "补上与首次运行一致的开关后再试（确认可以换个配置跑时加 --force）：",
            file=sys.stderr,
        )
        print(f"  {_resume_hint(record, settings)}", file=sys.stderr)
        for note in _resume_hint_notes(record):
            print(f"  {note}", file=sys.stderr)
        return EXIT_CODES["config"]
    try:
        # 与 run 同一条目录：续跑时的计划编辑校验也要认得自定义角色。
        catalog = build_catalog(settings)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    console = Console()
    gate, prompter, downgraded = _resolve_gate(
        settings.gate, interactive=_stdin_isatty(), console=console, catalog=catalog,
        approval_mode=settings.approval_mode)
    if downgraded:
        print(
            "非 TTY（管道 / CI）：--gate 含人工卡点但无法交互，已显式降级为 "
            "gate=none（不会向任何人提问）。",
            file=sys.stderr,
        )
    settings = settings.model_copy(update={"gate": gate})
    hook = _BoardHook(record.project_goal, enabled=not args.no_live, console=console)
    try:
        crew = Crew.resume_from(
            settings, args.run_id, prompter=prompter, on_event=hook, catalog=catalog)
    except (OSError, ValueError) as exc:
        return _fail(f"无法恢复 run {args.run_id}：{exc}")
    hook.bind(crew)
    try:
        return _drive(crew, crew.resume, hook=hook, console=console)
    except ResumeLockError as exc:
        # R17：两个进程同时续跑同一个 run —— 给可读提示，不打印 traceback。
        hook.stop()
        return _fail(f"resume 冲突：{exc}")
    except (OSError, ValueError) as exc:
        # resume 的用户级失败模式是「run 目录与配置/清单不一致」（复制来的 run、
        # 手工改过的 run.json、被清掉一半的 artifacts/）：这类问题要在完整链路
        # 里才暴露（如 ArtifactStore.load_manifest 的 run_id 校验），以配置错误
        # 退出并给出可读消息，好过让用户对着 traceback 猜。
        hook.stop()
        return _fail(f"resume 失败（run 目录与清单/配置不一致）：{exc}")


def _cmd_rewind(args: argparse.Namespace) -> int:
    """R62：回滚到指定任务并重跑（时间旅行）。

    纯文件维护操作，不烧模型调用：把目标任务与其下游重置为 ``PENDING``、
    旧产物先快照再移除、run 置回 ``interrupted``；之后用既有的 ``resume``
    从该点重跑。``--dry-run`` 只打印影响集。
    """
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
    except (OSError, ValueError) as exc:
        return _fail(f"无法定位 run {args.run_id}：{exc}")
    try:
        record = CheckpointStore(run_dir).load_record()
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    try:
        result = rewind_run(
            run_dir,
            target=args.task,
            instruction=args.instruction,
            settings=settings,
            dry_run=args.dry_run,
        )
    except ResumeLockError as exc:
        return _fail(f"rewind 冲突：{exc}")
    except (OSError, RuntimeError, ValueError) as exc:
        return _fail(f"rewind 失败：{exc}")
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]

    console = Console()
    affected = "、".join(result.affected)
    if result.dry_run:
        _out(
            console,
            f"[dry-run] run {result.run_id} 回滚到 {result.target}："
            f"将重置 {len(result.affected)} 个任务（{affected}），"
            f"移除 {result.discarded_artifacts} 个旧产物（本次未写盘）",
        )
        return EXIT_CODES["ok"]
    _out(
        console,
        f"已回滚 run {result.run_id} 到 {result.target}："
        f"{result.status_before} → {result.status_after}",
    )
    _out(console, f"重置任务 {len(result.affected)} 个：{affected}")
    if result.instruction:
        _out(console, f"新指令已写入 {result.target} 的任务契约：{result.instruction}")
    if result.snapshot_dir:
        _out(
            console,
            f"旧产物 {result.discarded_artifacts} 个已快照到 "
            f"{Path(result.snapshot_dir).relative_to(run_dir).as_posix()}",
        )
    _out(console, f"下一步：{_resume_hint(record, settings)}   # 从该任务重跑")
    for note in _resume_hint_notes(record):
        _out(console, f"  {note}")
    return EXIT_CODES["ok"]


def _drive(
    crew: Crew,
    run_coro: Callable[[], Coroutine[Any, Any, RunRecord]],
    *,
    hook: _BoardHook,
    console: Console,
) -> int:
    """执行一次 ``run`` / ``resume``，把结束方式翻译成退出码（spec §7.4）。"""
    try:
        record = asyncio.run(run_coro())
    except PlanFailedError as exc:
        hook.stop()
        print("拆解连续未通过校验（退出码 2）：", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_CODES["plan"]
    except ModelFactoryError as exc:
        # ContextBudgetError 是 ModelFactoryError 的子类，一并在这里映射为配置错误。
        hook.stop()
        return _fail(f"配置错误：{exc}")
    except RunCancelled as exc:
        hook.stop()
        print(str(exc))
        _print_resume_hint(crew)
        return EXIT_CODES["cancelled"]
    except KeyboardInterrupt:
        # Crew 已把 checkpoint 落盘为 INTERRUPTED（spec §13.4）；这里只报断点。
        hook.stop()
        _print_resume_hint(crew, prefix="运行被 Ctrl-C 中断，断点已保存")
        return EXIT_CODES["cancelled"]
    hook.stop()
    if record.status is RunStatus.PLANNED:
        console.print(
            Text(f"计划已生成（未执行）：任务已落盘。可用 resume {record.run_id} 续跑执行。"),
            soft_wrap=True)
        return _run_exit_code(crew, record)
    console.print(Text(crew.delivery_summary(record)), soft_wrap=True)
    return _run_exit_code(crew, record)


def _run_exit_code(crew: Crew, record: RunRecord) -> int:
    """结束状态 → 退出码；数字全部取自 :data:`EXIT_CODES`。

    判定顺序与 ``Crew._exit_code``（``run_finished`` 事件里 ``exit_code`` 的来源）
    保持同一口径：停滞 → 终局状态 → 失败。状态先于统计，是因为**两条路径必须给出
    同一个数**：一个带失败任务、随后又被取消的 run，审计记录写 130，进程就不能退
    1。``COMPLETED`` 蕴含「无失败」，由 ``Crew._final_status`` 保证（有 failed /
    blocked / skipped 或未终结时它返回 ``FAILED``）。
    """
    if crew.stalled:
        return EXIT_CODES["stalled"]
    if record.status is RunStatus.COMPLETED:
        return EXIT_CODES["ok"]
    if record.status is RunStatus.PLANNED:
        return EXIT_CODES["ok"]
    if record.status in (RunStatus.CANCELLED, RunStatus.INTERRUPTED):
        return EXIT_CODES["cancelled"]
    # R4：审批挂起是正常中间态，与 Crew._exit_code 同一口径（复用 ok=0，
    # 不改 EXIT_CODES 字典——test_crew.py 对它做精确相等断言）。
    if record.status is RunStatus.AWAITING_APPROVAL:
        return EXIT_CODES["ok"]
    return EXIT_CODES["failed"]


def _print_resume_hint(crew: Crew, *, prefix: str = "可续跑本次 run") -> None:
    """打印 ``resume <run_id>`` 命令；已取消的 run 改为明说「无需 resume」。

    计划卡点选 ``q``（cancel）与 ``s``（save_exit）都抛 ``RunCancelled``，但落盘
    状态不同（``CANCELLED`` / ``INTERRUPTED``）——只有后者值得续跑，对前者喊
    ``resume`` 只会让人白跑一次。run 目录还没建出来时只报断点。
    """
    checkpoint = crew.checkpoint
    if checkpoint is None:
        print(f"{prefix}（run 目录尚未生成）", file=sys.stderr)
        return
    try:
        status = checkpoint.load_record().status
    except (OSError, ValueError):
        status = None
    if status is RunStatus.CANCELLED:
        print(f"{prefix}：本次 run 已取消（cancelled），无需 resume", file=sys.stderr)
        return
    print(f"{prefix}：python main.py resume {checkpoint.run_dir.name}", file=sys.stderr)


class _BoardHook:
    """``Scheduler.on_event`` 的 ``(事件名, 数据)`` → :class:`BoardView` 的桥。

    看板**按需创建**：``BoardView`` 构造时要 run_id，而 run_id 只有 ``Crew`` 建好
    run 目录后才知道。第一条事件到达时（执行阶段开始）run 目录必然已就绪。
    ``--no-live`` 时 ``enabled=False``：看板不产生输出，但仍更新内存状态。
    """

    def __init__(self, goal: str, *, enabled: bool, console: Console) -> None:
        self._goal = goal
        self._enabled = enabled
        self._console = console
        self._crew: Crew | None = None
        self._view: BoardView | None = None
        self._run_id = ""

    def bind(self, crew: Crew) -> None:
        """绑定所属 ``Crew``（构造桥时 Crew 还没建好，只能事后绑定）。"""
        self._crew = crew

    def __call__(self, event: str, data: dict[str, Any]) -> None:
        view = self._ensure_view()
        view.on_event(
            LedgerEvent(ts=datetime.now(), run_id=self._run_id, event=event, data=data))

    def stop(self) -> None:
        """停止实时看板（幂等；看板从未创建时是空操作）。"""
        if self._view is not None:
            self._view.stop()

    def _ensure_view(self) -> BoardView:
        """第一条事件到达时创建并启动看板。"""
        if self._view is None:
            checkpoint = self._crew.checkpoint if self._crew is not None else None
            self._run_id = checkpoint.run_dir.name if checkpoint is not None else ""
            self._view = BoardView(
                self._run_id, self._goal, enabled=self._enabled, console=self._console)
            self._view.start()
        return self._view

