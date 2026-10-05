"""AgentCrew CLI 入口（spec §15.1）。

六个子命令：``run`` / ``resume`` / ``status`` / ``roles`` / ``clean`` / ``cancel``。
本模块只做三件事：解析参数、装配已提交的引擎组件、把结果翻译成退出码。所有业务
决策都在 ``Crew`` / ``Gatekeeper`` / ``CheckpointStore`` 里 —— 这里不复刻一份，也不
绕过任何一层。

设计约束：

- **退出码全部取自** :data:`agentcrew.crew.crew.EXIT_CODES`（spec §7.4）。本模块不
  重新定义数字，只做「结束方式 → 键」的映射。
- **``--gate`` 指定人工卡点但没有 TTY 时，显式降级为 ``none``** 并在 stderr 说明
  一句，随后用 ``NullPrompter`` 继续（spec §10.3：「非 TTY 自动降级为 none 并在
  日志中注明」）。绝不做「gate 仍写着 both、prompter 悄悄换成 NullPrompter」——
  那正是 Task 27 关掉的 fail-open，操作者会照着「有人工卡点」去规划。降级后的
  gate 会进配置快照与 ledger，不存在「声称有人工卡点、实际全自动」的错位。
- **Ctrl-C 是一等公民**（spec §13.4）：``Crew`` 已把 run 落盘为 ``INTERRUPTED``，
  这里只负责打印 ``resume <run_id>`` 并返回 130。第二次 Ctrl-C 由 :func:`main`
  与模块级入口兜住，不留 traceback。
- **输出编码**：Windows 重定向的 stdout 常是 GBK，而看板的状态符号（``✓`` / ``⟳``）
  在 GBK 里没有码位。入口把「编不出探测串」的 stdout/stderr 包一层替换流，重定向
  永远不会因 ``UnicodeEncodeError`` 崩掉。
- ``on_event`` 接到 :class:`agentcrew.runtime.board_view.BoardView`：``Scheduler``
  发的是 ``(事件名, 数据)``，看板要 ``LedgerEvent``，桥在 :class:`_BoardHook`。
  看板**按需创建** —— run_id 要到 ``Crew`` 建好 run 目录之后才知道。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

from agentcrew.config import GateMode, Settings, redact_overrides
from agentcrew.crew.crew import (
    EXIT_CODES,
    Crew,
    PlanFailedError,
    RunCancelled,
    build_catalog,
)
from agentcrew.gate.cli_prompter import CliPrompter
from agentcrew.gate.gatekeeper import NullPrompter, Prompter
from agentcrew.models.factory import ModelFactoryError
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.board_view import BoardView
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunRecord,
    RunStatus,
    clean,
)
from agentcrew.runtime.ledger import Ledger, LedgerEvent
from examples.todo_web_project import PROJECT_GOAL

#: 含人工卡点的 gate 模式（与 ``Gatekeeper`` 的 ``_PLAN_GATES`` / ``_DELIVERY_GATES`` 同源）。
_HUMAN_GATES: frozenset[str] = frozenset({"both", "plan", "delivery"})

#: 编码探测串：看板状态符号编不进 GBK，中文可以。
_ENCODING_PROBE = "✓⟳—·中文"

#: ``resume`` 时参与「结果可比性」比对的配置键（spec §13.3 第 3 步：防换模型后结果不可比）。
#: 不比对 ``output_dir`` / ``gate`` / ``log_level`` 之类的运行环境字段：它们变了
#: 不影响产物含义，比进去只会逼用户无脑加 ``--force``。
_COMPARABLE_KEYS = (
    "default_model",
    "role_bindings",
    "provider_overrides",
    "context_budget_tokens",
    "mock",
)


# ── 输出编码（GBK 重定向不崩）─────────────────────────────────────────────


class _EncodingSafeStream:
    """把目标流编码不出的字符替换为 ``?`` 的写包装。

    Windows 上 ``python main.py run > log.txt`` 的 stdout 是 GBK；rich 画看板时写的
    ``✓`` / ``⟳`` 会让 ``TextIOWrapper.write`` 抛 ``UnicodeEncodeError`` —— 进程在
    收尾打印时崩掉，比画面少几个符号严重得多。包装只改 ``write``：先按目标编码试编，
    编不出就整体 ``errors="replace"``；其余属性（``isatty`` / ``fileno`` / ``buffer``…）
    原样代理给底层流。
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    @property
    def encoding(self) -> str:
        """底层流的编码（rich 的 ``Console.encoding`` 读它）。"""
        return getattr(self._stream, "encoding", None) or "utf-8"

    def write(self, text: str) -> int:
        """按目标编码降级后写入；返回底层流报告的字符数。"""
        try:
            text.encode(self.encoding)
        except UnicodeEncodeError:
            text = text.encode(self.encoding, "replace").decode(
                self.encoding, "replace")
        return self._stream.write(text)

    def flush(self) -> None:
        """透传 flush。"""
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _install_encoding_safe_stdio() -> None:
    """把编不出探测串的 stdout/stderr 换成 :class:`_EncodingSafeStream`（幂等）。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or isinstance(stream, _EncodingSafeStream):
            continue
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            _ENCODING_PROBE.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            setattr(sys, name, _EncodingSafeStream(stream))


# ── 参数解析 ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """六个子命令（spec §15.1）。

    ``run`` 的 ``--goal`` / ``--demo`` **不用** argparse 的互斥组：互斥组在两者
    同时给出时会以退出码 2 直接结束进程，而这里要求「都没给 / 都给」一律报错并
    返回退出码 3（:data:`EXIT_CODES` 的 ``config``）。
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AgentCrew：8 角色多 Agent 协作平台（规划 → 执行 → 三层门禁 → 交付）",
    )
    sub = parser.add_subparsers(
        dest="command", metavar="{run,resume,status,roles,clean,cancel}")

    run = sub.add_parser("run", help="执行一次完整 run（规划 → 执行 → 交付）")
    run.add_argument("--goal", metavar="TEXT", help="项目目标（与 --demo 二选一）")
    run.add_argument(
        "--demo", action="store_true",
        help="使用 examples/todo_web_project.py 的内置 Todo Web 目标（与 --goal 二选一）")
    _add_run_flags(run)

    resume = sub.add_parser("resume", help="续跑一个中断或未完成的 run")
    resume.add_argument("run_id", help="run 目录名（见 status / clean 的输出）")
    resume.add_argument(
        "--force", action="store_true", help="配置快照与当前配置不一致时仍继续")
    _add_run_flags(resume)

    status = sub.add_parser("status", help="打印一个 run 的记录与任务表")
    status.add_argument("run_id")
    status.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")

    sub.add_parser("roles", help="列出 8 个内置角色（职责 / 边界 / 产出）")

    clean_cmd = sub.add_parser(
        "clean", help="保留最近 N 个 run，删除更旧的（默认 N=5）")
    clean_cmd.add_argument("--keep", type=int, default=5, metavar="N",
                           help="保留的 run 个数（默认 5）")
    clean_cmd.add_argument("--dry-run", action="store_true",
                           help="只列出将删除的 run，不真删")
    clean_cmd.add_argument("--output-dir", metavar="DIR")

    cancel = sub.add_parser(
        "cancel", help="给离线 run 打 cancelled 标记（下次 resume 直接收尾）")
    cancel.add_argument("run_id")
    cancel.add_argument("--output-dir", metavar="DIR")

    return parser


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    """``run`` / ``resume`` 共用的配置与看板开关。"""
    parser.add_argument("--mock", action="store_true",
                        help="强制 mock provider（覆盖所有角色绑定，无需 API Key）")
    parser.add_argument("--auto", action="store_true",
                        help="等价于 --gate none（全自动，跳过人工闸门）")
    parser.add_argument("--gate", choices=("both", "plan", "delivery", "none"),
                        help="人工闸门模式；含人工卡点但无 TTY 时降级为 none")
    parser.add_argument("--max-concurrency", type=int, metavar="N",
                        help="全局并发上限（含 LLM 调用）")
    parser.add_argument("--default-model", metavar="ALIAS",
                        help="默认模型别名，形如 provider:model")
    parser.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")
    parser.add_argument("--context-budget", type=int, metavar="N",
                        help="上下文预算（tokens）")
    parser.add_argument("--no-live", action="store_true",
                        help="关闭实时看板，只保留结束报告（CI 友好）")


# ── 入口 ──────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """解析并分派；返回进程退出码（spec §7.4 的那张表）。"""
    _install_encoding_safe_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        _print_bare_invocation(parser)
        return EXIT_CODES["ok"]
    handler = _HANDLERS[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        # 第二次 Ctrl-C（第一次已在 _drive 里打印 resume 提示）：静默收尾，不留 traceback。
        print("已中断。", file=sys.stderr)
        return EXIT_CODES["cancelled"]


def _print_bare_invocation(parser: argparse.ArgumentParser) -> None:
    """无子命令时的默认入口：帮助 + 已注册角色数 + 最近 3 次 run 概览（spec §15.1）。

    这是 CLI 的默认发现路径（Task 34 的 README 指向它），所以不只打 help：角色数
    用 ``BUILTIN_ROLES``，最近 run 用 ``CheckpointStore.list_runs``（与 ``clean``
    同一份列表，new→old，取前三条）。配置坏了也照常返回帮助：这里的目标是「告诉
    用户有哪些工具」，不该因为一个坏 YAML 变成非零退出。
    """
    parser.print_help()
    console = Console()
    _out(
        console,
        f"已注册角色 {len(BUILTIN_ROLES)} 个"
        "（python main.py roles 可看职责 / 边界 / 产出）",
    )
    try:
        output_dir = Settings.load().output_dir
    except (OSError, ValueError) as exc:
        _out(console, f"最近 3 次 run：（配置不可用，略过：{exc}）")
        return
    runs = CheckpointStore.list_runs(output_dir)[:3]
    if not runs:
        _out(console, f"最近 3 次 run：无（{output_dir} 下还没有 run）")
        return
    _out(console, f"最近 3 次 run（{output_dir}）：")
    for run_id in runs:
        _out(console, f"  {run_id}")


# ── run / resume ──────────────────────────────────────────────────────────


def _cmd_run(args: argparse.Namespace) -> int:
    """``run --goal TEXT | --demo``：二选一，否则退出码 3。"""
    if args.goal and args.demo:
        return _fail("run：--goal 与 --demo 互斥，只能给一个。")
    if args.demo:
        goal: str | None = PROJECT_GOAL
    else:
        goal = args.goal
    if not goal:
        return _fail("run：必须给 --goal 或 --demo 之一。")
    invalid = _invalid_numbers(args)
    if invalid is not None:
        return _fail(invalid)
    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:  # pydantic ValidationError / YAML 解析错误
        return _fail(f"配置错误：{exc}")
    try:
        # 目录只建一次：CliPrompter 的编辑校验与 Crew 的调度必须看到同一批角色，
        # 否则编辑后的计划会被「角色未注册」误拒（roles.d 的自定义角色）。
        catalog = build_catalog(settings)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    console = Console()
    gate, prompter, downgraded = _resolve_gate(
        settings.gate, interactive=_stdin_isatty(), console=console, catalog=catalog)
    if downgraded:
        print(
            "非 TTY（管道 / CI）：--gate 含人工卡点但无法交互，已显式降级为 "
            "gate=none（不会向任何人提问）。",
            file=sys.stderr,
        )
    settings = settings.model_copy(update={"gate": gate})
    hook = _BoardHook(goal, enabled=not args.no_live, console=console)
    crew = Crew(settings, catalog=catalog, prompter=prompter, on_event=hook)
    hook.bind(crew)
    return _drive(crew, lambda: crew.run(goal), hook=hook, console=console)


def _cmd_resume(args: argparse.Namespace) -> int:
    """``resume <run_id>``：先比对配置快照，不一致时要求 ``--force``（spec §13.3）。"""
    invalid = _invalid_numbers(args)
    if invalid is not None:
        return _fail(invalid)
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
        print("确认无误请加 --force 重试。", file=sys.stderr)
        return EXIT_CODES["config"]
    try:
        # 与 run 同一条目录：续跑时的计划编辑校验也要认得自定义角色。
        catalog = build_catalog(settings)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    console = Console()
    gate, prompter, downgraded = _resolve_gate(
        settings.gate, interactive=_stdin_isatty(), console=console, catalog=catalog)
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
    except (OSError, ValueError) as exc:
        # resume 的用户级失败模式是「run 目录与配置/清单不一致」（复制来的 run、
        # 手工改过的 run.json、被清掉一半的 artifacts/）：这类问题要在完整链路
        # 里才暴露（如 ArtifactStore.load_manifest 的 run_id 校验），以配置错误
        # 退出并给出可读消息，好过让用户对着 traceback 猜。
        hook.stop()
        return _fail(f"resume 失败（run 目录与清单/配置不一致）：{exc}")


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
    if record.status in (RunStatus.CANCELLED, RunStatus.INTERRUPTED):
        return EXIT_CODES["cancelled"]
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


# ── status / roles / clean / cancel ───────────────────────────────────────


def _cmd_status(args: argparse.Namespace) -> int:
    """打印 ``run.json`` 记录 + ``tasks.json`` 任务表 + ledger 统计。

    ledger 读取端本来就宽容（文件不存在 = 零事件，见 ``Ledger.events``），这里
    直接读、不预先判存在。
    """
    try:
        settings = _settings_from(args)
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
        f"创建    {record.created_at:%Y-%m-%d %H:%M:%S}"
        f"    更新    {record.updated_at:%Y-%m-%d %H:%M:%S}",
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
    return EXIT_CODES["ok"]


def _cmd_roles(args: argparse.Namespace) -> int:
    """列出 8 个内置角色：key / 角色 / 职责 / 边界 / 产出（Task 34 的 README 指向这里）。

    逐角色分块，而不是一张五列表：80 列的终端里五列表会把「职责/边界/产出」挤到
    每列十几个字，rich 还会把放不下的 key 截成 ``project_mana…`` —— 本命令存在的
    意义就是让人拿到完整的 key 与契约。每一行都走 ``soft_wrap``：内容只换行、不截断。
    """
    del args  # 无参数：角色表来自 BUILTIN_ROLES
    console = Console()
    for spec in BUILTIN_ROLES:
        _out(console, f"[{spec.key}] {spec.title} —— {spec.goal}")
        _out(console, f"  职责  {'；'.join(spec.responsibilities)}")
        _out(console, f"  边界  {'；'.join(spec.boundaries)}")
        _out(console, f"  产出  {spec.output_contract}")
        _out(console, "")
    _out(
        console,
        f"共 {len(BUILTIN_ROLES)} 个内置角色；用 --gate plan 可在执行前人工确认拆解。",
    )
    return EXIT_CODES["ok"]


def _cmd_clean(args: argparse.Namespace) -> int:
    """按 run_id 倒序保留最近 ``--keep`` 个 run，删除其余（spec §13.5）。

    ``--dry-run`` 只列不删（:func:`agentcrew.runtime.checkpoint.clean` 的真跑与
    干跑共用同一套边界校验）。
    """
    if args.keep < 0:
        return _fail("clean：--keep 必须 >= 0。")
    try:
        settings = _settings_from(args)
        removed = clean(settings.output_dir, keep=args.keep, dry_run=args.dry_run)
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
    record.updated_at = datetime.now()
    checkpoint.save_record(record)
    _out(
        Console(),
        f"已把 run {record.run_id} 标记为 cancelled；下次 resume 将直接以 CANCELLED 收尾。",
    )
    return EXIT_CODES["ok"]


#: 子命令 → 处理器（``build_parser`` 里注册的 dest 值）。
_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "run": _cmd_run,
    "resume": _cmd_resume,
    "status": _cmd_status,
    "roles": _cmd_roles,
    "clean": _cmd_clean,
    "cancel": _cmd_cancel,
}


# ── 配置、闸门与辅助 ──────────────────────────────────────────────────────


def _settings_from(args: argparse.Namespace) -> Settings:
    """把 CLI 覆盖合并进五级配置（spec §17.1）。

    只写「命令行真的给了」的键：``store_true`` 的 ``False`` 缺省不能进 overrides，
    否则会把 ``AGENTCREW_MOCK=true`` 之类的环境变量反过来盖掉。
    """
    overrides: dict[str, Any] = {}
    if getattr(args, "mock", False):
        overrides["mock"] = True
    gate = _effective_gate(
        getattr(args, "gate", None), auto=getattr(args, "auto", False))
    if gate is not None:
        overrides["gate"] = gate
    if getattr(args, "max_concurrency", None) is not None:
        overrides["max_concurrency"] = args.max_concurrency
    if getattr(args, "default_model", None) is not None:
        overrides["default_model"] = args.default_model
    if getattr(args, "context_budget", None) is not None:
        overrides["context_budget_tokens"] = args.context_budget
    if getattr(args, "output_dir", None):
        overrides["output_dir"] = Path(args.output_dir)
    return Settings.load(**overrides)


def _effective_gate(gate: GateMode | None, *, auto: bool) -> GateMode | None:
    """``--gate`` / ``--auto`` → 传给配置层的 gate 覆盖值（``None`` = 不覆盖）。

    ``--auto`` 与 ``--gate`` 同时给出时 ``--auto`` 胜出：它本身就是「等价于
    ``--gate none``」的缩写，两者写出不同的值属于自相矛盾的调用。
    """
    if auto:
        return "none"
    return gate


def _resolve_gate(
    gate: GateMode,
    *,
    interactive: bool,
    console: Console | None = None,
    catalog: RoleCatalog | None = None,
) -> tuple[GateMode, Prompter, bool]:
    """gate 模式 → ``(生效的 gate, prompter, 是否降级)``。

    人工卡点 + 无 TTY 时**显式降级**为 ``none`` 并返回 ``downgraded=True``，由调用方
    打一行说明。本函数绝不返回「gate 仍含人工卡点 + NullPrompter」的组合：那正是
    Task 27 的构造期守卫要拦的错位（``Gatekeeper`` 会为此抛 ``ValueError``，因为
    「声称有人工卡点、实际全自动放行」比没有闸门更危险）。

    ``catalog`` 透传给 :class:`CliPrompter`：计划卡点的 ``[e] 编辑后执行`` 要用
    它校验编辑结果，自定义角色才不会被误判为未注册。
    """
    if gate not in _HUMAN_GATES:
        return gate, NullPrompter(), False
    if interactive:
        return gate, CliPrompter(console=console, catalog=catalog), False
    return "none", NullPrompter(), True


def _stdin_isatty() -> bool:
    """stdin 是否真终端；取不到（pytest / 重定向 / 无 stdin）一律当非 TTY。"""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _invalid_numbers(args: argparse.Namespace) -> str | None:
    """正整数开关的校验（argparse 只管类型，不管取值）；非法时返回错误消息。"""
    concurrency = getattr(args, "max_concurrency", None)
    if concurrency is not None and concurrency < 1:
        return "run：--max-concurrency 必须 >= 1。"
    budget = getattr(args, "context_budget", None)
    if budget is not None and budget < 1:
        return "run：--context-budget 必须 >= 1。"
    return None


def _config_diff(snapshot: dict[str, Any], settings: Settings) -> list[str]:
    """配置快照 vs 当前配置的差异行（空列表 = 可比，spec §13.3 第 3 步）。

    两侧都过 :func:`~agentcrew.config.redact_overrides`：这条差异行会**打印到
    stderr**（还常常被重定向进 CI 日志），而 ``provider_overrides`` 是自由形状
    的用户输入 —— 旧 run.json 里可能留着本次修复之前写下的明文密钥。
    """
    current = settings.config_snapshot()
    return [
        f"{key}: {_redacted(snapshot.get(key))!r} -> "
        f"{_redacted(current.get(key))!r}"
        for key in _COMPARABLE_KEYS
        if _redacted(snapshot.get(key)) != _redacted(current.get(key))
    ]


def _redacted(value: Any) -> Any:
    """映射值先抹掉疑似密钥再比较/打印；其余类型原样返回。"""
    return redact_overrides(value) if isinstance(value, dict) else value


def _run_dir(settings: Settings, run_id: str) -> Path:
    """定位 run 目录，并拦住任何会拼出 ``output_dir`` 之外的名字。

    ``CheckpointStore.__init__`` 只检查「目录存在」，不校验名字；``cancel`` 会往
    ``run.json`` 写，所以路径必须在进 I/O 之前就收进 output_dir 内（与
    ``checkpoint._assert_deletable`` 同一条边界）。
    """
    root = settings.output_dir.resolve()
    resolved = (settings.output_dir / run_id).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"非法 run_id: {run_id!r}（必须位于 {root} 之下）")
    return resolved


def _out(console: Console, text: str) -> None:
    """打印一行长文本；``soft_wrap`` 关掉 rich 的折行，路径 / run_id 不被截断。"""
    console.print(Text(text), soft_wrap=True)


def _fail(message: str) -> int:
    """错误一律走 stderr + 配置类退出码（spec §7.4 的「配置错误」）。"""
    print(message, file=sys.stderr)
    return EXIT_CODES["config"]


# ── 看板桥 ────────────────────────────────────────────────────────────────


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


if __name__ == "__main__":  # pragma: no cover - 进程入口
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # 第二次 Ctrl-C 落在任何地方都不留 traceback（spec §13.4）。
        sys.exit(EXIT_CODES["cancelled"])
