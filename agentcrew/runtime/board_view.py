"""rich 终端看板与运行报告（spec §14.3 / §15.2）。

看板由 ledger 事件驱动：Task 32 把 ``Scheduler.on_event`` 接到
:meth:`BoardView.on_event`，事件只更新内存状态，``Live`` 的
``get_renderable`` 回调在刷新时自行拉取最新画面 —— 刷新由事件驱动，不轮询。
:meth:`BoardView.render` 是纯函数式的：给定 ``(board, record)`` 返回可渲染
对象，不依赖真实终端，测试可用固定宽度的 ``Console`` 捕获后断言。

设计约束：

- **状态键一律用 ``TaskStatus.value``。** ``TaskStatus`` 是 ``str`` 混入枚举，
  ``f"{TaskStatus.PENDING}"`` 在 Python 3.12 上渲染为 ``"TaskStatus.PENDING"``；
  :data:`STATUS_STYLE` 覆盖规格的十个状态值，渲染端仍用 ``.get`` 兜底，
  将来新增状态只会显示 ``?`` 而不会在看板中途炸掉。
- **未知事件静默忽略、绝不抛。** 事件名是公开的扩展面：Task 21 的
  ``llm_retry`` 与 Task 24 的 ``task_notes`` 都不在 spec §14.1 的事件表里。
  表内事件更新 ``last_event_text``；表外事件原样跳过，``last_event_text``
  保持最近一条**已知**事件的描述（向前兼容）。
- **一个 Console 服务所有输出。** ``Live``、降级行日志与 ``print_report``
  共用同一个 ``Console`` —— 两个写入者各持一个 Console 会撕裂画面
  （spec §15.2）。
- **非 TTY 不启动 Live。** ``Console.is_terminal`` 为假（CI、管道、重定向）
  时退化为逐行日志，控制序列绝不进入日志文件。
- **``enabled=False`` 不产生任何输出**（``--no-live``）：``start`` / ``stop``
  空操作，``on_event`` 只更新内存状态（``last_event_text`` 仍可用），
  ``render`` 照常返回可渲染对象；``print_report`` 照常打印 —— 关掉实时看板
  不等于关掉运行结束的报告。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agentcrew.runtime.checkpoint import RunRecord
from agentcrew.runtime.ledger import LedgerEvent
from agentcrew.task.state_machine import is_terminal
from agentcrew.task.task import Task, TaskStatus
from agentcrew.task.task_board import TaskBoard

#: 状态值 → ``(符号, 颜色)``（spec §15.2）。必须覆盖 ``TaskStatus`` 的全部十个
#: 值（``{s.value for s in TaskStatus}``），键是新状态字符串而不是枚举成员。
#: 灰色用 ``bright_black``：rich 15 里 ``"grey"`` / ``"gray"`` 都不是合法颜色名，
#: 渲染时才解析会抛 ``ColorParseError`` —— 一个颜色名就能把看板打挂。
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "succeeded": ("✓", "green"),
    "running": ("⟳", "yellow"),
    "review": ("⟳", "yellow"),
    "rework": ("↻", "magenta"),
    "failed": ("✗", "red"),
    "blocked": ("⊘", "bright_black"),
    "pending": ("◷", "cyan"),
    "ready": ("◷", "cyan"),
    "skipped": ("—", "bright_black"),
    "cancelled": ("—", "bright_black"),
}

#: 表格中的中文状态名；同样按 ``.value`` 索引。
_STATUS_LABEL: dict[str, str] = {
    "pending": "待分配",
    "ready": "就绪",
    "running": "进行中",
    "review": "待验收",
    "rework": "返工",
    "succeeded": "完成",
    "failed": "失败",
    "blocked": "阻塞",
    "cancelled": "已取消",
    "skipped": "已跳过",
}

#: spec §14.1 事件表内、看板认得的全部事件名。表外事件（``llm_retry`` /
#: ``task_notes`` / ``mock_fallback`` …）静默忽略：看板是只读消费者，
#: 生产者增加事件名不应让看板抛错。
_KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        "run_started",
        "run_finished",
        "plan_created",
        "task_transition",
        "task_started",
        "task_finished",
        "llm_call",
        "llm_degraded",
        "artifact_written",
        "gate_result",
        "human_decision",
        "error",
    }
)


def _field(data: dict[str, Any], key: str, default: str = "?") -> str:
    """事件字段的文本形式；缺失或 ``None`` 时退回 ``default``（绝不抛）。"""
    value = data.get(key)
    return default if value is None else str(value)


def _as_float(value: Any) -> float:
    """宽松数值转换；坏值按 0 计（ledger 读取端宽容，看板同理）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_clock(seconds: float) -> str:
    """``HH:MM:SS``（表头「已运行」）。"""
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_duration(seconds: float) -> str:
    """紧凑时长：``42s`` / ``4m12s`` / ``1h03m``。"""
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _format_tokens(count: int) -> str:
    """token 数的人类可读形式：``812`` / ``812k`` / ``1.2M``。"""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


class BoardView:
    """run 的终端看板：实时表格 + 结束报告。

    ``run_id`` / ``goal`` 只用于展示；``console`` 缺省时自建一个标准输出
    Console，显式传入则与其共享（日志、Live、报告必须走同一个 Console）。
    """

    def __init__(
        self,
        run_id: str,
        goal: str,
        *,
        enabled: bool = True,
        console: Console | None = None,
    ) -> None:
        self._run_id = run_id
        self._goal = goal
        self._enabled = enabled
        self._console = console if console is not None else Console()
        self._live: Live | None = None
        self._started = False
        self._plain_logging = False
        # 最近一次 render 的入参：Live 的刷新回调靠它重画表格。
        self._board: TaskBoard | None = None
        self._record: RunRecord | None = None
        self._started_at = datetime.now()
        self._cost_usd = 0.0
        #: 最近一条**已知**事件的中文描述；初始为空串（表外事件不改写）。
        self.last_event_text: str = ""

    # ---- 生命周期 ----

    def start(self) -> None:
        """开始实时展示；非 TTY / dumb 终端 / ``enabled=False`` 时退化为无输出或行日志。

        ``Live`` 只在**能处理光标控制**的终端上启动：管道与 CI 里控制序列会撕裂输出；
        ``TERM=dumb``（部分 CI、精简 shell）虽然 ``is_terminal`` 为真，但 rich 的
        ``Live.refresh()`` 在 dumb 终端下不会重绘 —— 结果是看板停在首帧、事件全丢。
        两种情况一律降级为行日志，宁可少一屏表格也不能让事件流断掉。
        """
        if not self._enabled or self._started:
            return
        self._started = True
        if not self._console.is_terminal or self._console.is_dumb_terminal:
            self._plain_logging = True
            self._console.print(
                Text(
                    f"AgentCrew 看板（非交互终端，降级为行日志）· "
                    f"{self._run_id} · {self._goal}"
                )
            )
            return
        self._live = Live(
            console=self._console,
            get_renderable=self._live_renderable,
            refresh_per_second=4,
            # spec §15.2：刷新由 ledger 事件驱动，不轮询 —— 定时器换来的只是
            # 「画面比事件晚 250ms」，却让看板的更新频率与事件流脱钩。
            auto_refresh=False,
        )
        self._live.start()

    def stop(self) -> None:
        """停止实时展示（幂等）；``Live`` 停止时保留最后一帧。"""
        if not self._enabled:
            return
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._started = False
        self._plain_logging = False

    # ---- 事件 ----

    def on_event(self, event: LedgerEvent) -> None:
        """消费一条 ledger 事件；表外事件静默忽略（向前兼容）。

        ``enabled=False`` 时仍更新内存状态（``last_event_text`` 供测试与
        ``--no-live`` 的报告使用），但不写任何输出。

        **事件驱动的刷新**（spec §15.2）：``Live`` 关掉了定时器
        （``auto_refresh=False``），画面只会在这里被主动重画 —— 每次事件都
        ``refresh()`` 一次，看板因此与事件流同节拍，而不是按固定频率轮询。
        """
        description = self._describe(event)
        if description is None:
            return
        self.last_event_text = description
        if event.event == "llm_call":
            self._cost_usd += _as_float(event.data.get("estimated_cost_usd"))
        if self._live is not None:
            self._live.refresh()
            return
        if not self._enabled or not self._plain_logging:
            return
        stamp = event.ts.strftime("%H:%M:%S")
        self._console.print(Text(f"{stamp} {description}"))

    def _describe(self, event: LedgerEvent) -> str | None:
        """已知事件 → 简短中文描述；表外事件返回 ``None``。"""
        if event.event not in _KNOWN_EVENTS:
            return None
        data = event.data
        name = event.event
        if name == "run_started":
            return f"运行开始：{_field(data, 'goal', self._goal)}"
        if name == "run_finished":
            return f"运行结束：{_field(data, 'status')}"
        if name == "plan_created":
            return f"计划完成：{_field(data, 'task_count')} 个任务"
        if name == "task_transition":
            return (
                f"{_field(data, 'task_id')} "
                f"{_field(data, 'from')} → {_field(data, 'to')}"
            )
        if name == "task_started":
            return (
                f"{_field(data, 'task_id')} 开始执行"
                f"（第 {_field(data, 'attempt')} 次）"
            )
        if name == "task_finished":
            return f"{_field(data, 'task_id')} 执行结束"
        if name == "llm_call":
            return f"LLM 调用：{_field(data, 'role')} · {_field(data, 'model')}"
        if name == "llm_degraded":
            return (
                f"模型降级：{_field(data, 'provider')} "
                f"{_field(data, 'from_level')} → {_field(data, 'to_level')}"
            )
        if name == "artifact_written":
            return (
                f"{_field(data, 'task_id')} 产物已写入："
                f"{Path(_field(data, 'path')).name}"
            )
        if name == "gate_result":
            verdict = _field(data, "verdict")
            verdict_text = {"approve": "通过", "reject": "驳回"}.get(verdict, verdict)
            return f"{_field(data, 'task_id')} 门禁{verdict_text}"
        if name == "human_decision":
            return (
                f"人工决策（{_field(data, 'checkpoint')}）："
                f"{_field(data, 'choice')}"
            )
        # 仅剩 "error"：_KNOWN_EVENTS 已排除其余，无需再逐个比较。
        return f"错误（{_field(data, 'where')}）：{_field(data, 'exc_type')}"

    # ---- 渲染 ----

    def render(
        self, board: TaskBoard, record: RunRecord | None = None
    ) -> RenderableType:
        """把当前任务板渲染成 ``Panel(Table + 底栏)``。

        同时记住 ``(board, record)``：:meth:`start` 启动的 ``Live`` 通过
        ``get_renderable`` 回调取用最新一次渲染结果。本方法不产生任何输出，
        也不依赖真实终端（``isatty``）。
        """
        self._board = board
        self._record = record
        table = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold")
        for title in ("ID", "标题", "角色", "状态", "依赖", "尝试", "返工", "耗时"):
            table.add_column(title)
        for task in board.all():
            table.add_row(*self._row(task))
        return Panel(
            Group(table, self._footer(board, record)),
            # 标题与单元格一律包成 Text：目标/标题/角色来自用户与 LLM，可能含
            # ``[/...]`` 之类的富文本标记；裸 str 会被当作 markup 解析，
            # 一个未配对的闭合标签就能抛 MarkupError 打挂运行中的看板。
            title=Text(self._header(record)),
            border_style="cyan",
        )

    def _row(self, task: Task) -> tuple[RenderableType, ...]:
        """一行任务；未知状态用 ``?`` 兜底而不是 ``KeyError``。"""
        symbol, colour = STATUS_STYLE.get(task.status.value, ("?", "bright_black"))
        label = _STATUS_LABEL.get(task.status.value, task.status.value)
        deps = "、".join(task.dependencies) if task.dependencies else "—"
        return (
            Text(task.id),
            Text(task.title),
            Text(task.role),
            Text(f"{symbol} {label}", style=colour),
            Text(deps),
            Text(str(task.attempts)),
            Text(str(task.rework_count)),
            Text(self._elapsed_of(task)),
        )

    def _elapsed_of(self, task: Task) -> str:
        """单任务耗时：有结果取结果时长；在跑取本次执行的起点；否则 ``—``。"""
        if task.result is not None:
            return _format_duration(task.result.duration_ms / 1000)
        if task.status in (TaskStatus.RUNNING, TaskStatus.REVIEW):
            started = next(
                (
                    change.at
                    for change in reversed(task.status_history)
                    if change.to_status is TaskStatus.RUNNING
                ),
                None,
            )
            if started is not None:
                return _format_duration((datetime.now() - started).total_seconds())
        return "—"

    def _header(self, record: RunRecord | None) -> str:
        status = record.status.value if record is not None else "运行中"
        if record is not None:
            # 有 run 记录时用它的起点：resume 后显示整段 run 的真实时长，
            # 而不是本进程看板的存活时长。
            start = record.stats.started_at or record.created_at
        else:
            start = self._started_at
        elapsed = _format_clock((datetime.now() - start).total_seconds())
        return f"AgentCrew · {self._goal} · {self._run_id} · {status} {elapsed}"

    def _footer(self, board: TaskBoard, record: RunRecord | None) -> Text:
        """底栏：进度、并发、累计成本、最近事件（spec §15.2）。"""
        tasks = board.all()
        done = sum(1 for task in tasks if is_terminal(task.status))
        running = sum(1 for task in tasks if task.status is TaskStatus.RUNNING)
        limit = self._max_concurrency(record)
        concurrency = f"{running}/{limit}" if limit is not None else str(running)
        # 成本以 run 记录为准（resume 后跨越多个进程依旧完整）；没有 record 时
        # 用本进程从 llm_call 事件累积的值兜底。
        cost = (
            record.stats.estimated_cost_usd
            if record is not None
            else self._cost_usd
        )
        footer = Text()
        footer.append(f"进度 {done}/{len(tasks)}")
        footer.append(f"   并发 {concurrency}")
        footer.append(f"   成本 ${cost:.2f}")
        footer.append(f"   最近事件：{self.last_event_text or '—'}")
        return footer

    @staticmethod
    def _max_concurrency(record: RunRecord | None) -> int | None:
        if record is None:
            return None
        value = record.config_snapshot.get("max_concurrency")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _live_renderable(self) -> RenderableType:
        """``Live`` 的刷新回调；尚无任务板时显示启动占位。"""
        if self._board is None:
            return Panel(
                Text(f"AgentCrew · {self._goal} · {self._run_id} · 启动中…"),
                border_style="cyan",
            )
        return self.render(self._board, self._record)

    # ---- 运行报告 ----

    def print_report(
        self, record: RunRecord, board: TaskBoard, *, artifacts_dir: Path
    ) -> None:
        """按 spec §14.3 打印中文运行报告；失败任务逐个列出最后一次评审的问题。

        只要板上还有 ``FAILED`` 任务就打印「失败任务」段，没有失败任务时整段
        省略（不打空标题）。任务统计取自 ``record.stats``，评审与返工细节在
        板上重建 —— 两者本就同源（checkpoint 同时落盘 run.json 与 tasks.json）。
        """
        tasks = board.all()
        stats = record.stats
        # 值与标题同样包 Text：run_id / 文件名 / issues 都可能带富文本标记。
        self._console.print(Text(f"━━━ AgentCrew 运行报告 · {record.run_id} ━━━"))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True)
        grid.add_column()
        grid.add_row(
            "任务",
            Text(
                f"{stats.total_tasks} 个   成功 {stats.succeeded} · "
                f"失败 {stats.failed} · 阻塞 {stats.blocked} · 跳过 {stats.skipped}"
            ),
        )
        grid.add_row("返工", Text(self._rework_line(tasks)))
        grid.add_row(
            "LLM 调用",
            Text(
                f"{stats.llm_calls} 次   "
                f"prompt {_format_tokens(stats.prompt_tokens)} tok · "
                f"completion {_format_tokens(stats.completion_tokens)} tok"
            ),
        )
        grid.add_row("估算成本", Text(f"${stats.estimated_cost_usd:.2f}"))
        grid.add_row("评审通过率", Text(self._review_line(tasks)))
        grid.add_row("耗时", Text(self._elapsed_line(record)))
        grid.add_row("产物", Text(str(artifacts_dir)))
        self._console.print(grid)

        failed = [task for task in tasks if task.status is TaskStatus.FAILED]
        if not failed:
            return
        self._console.print(Text("━━━ 失败任务 ━━━"))
        for task in failed:
            self._console.print(Text(self._failure_line(task)))

    @staticmethod
    def _rework_line(tasks: list[Task]) -> str:
        total = sum(task.rework_count for task in tasks)
        reworked = [task for task in tasks if task.rework_count > 0]
        if not reworked:
            return f"{total} 次"
        detail = ", ".join(f"{task.id} ×{task.rework_count}" for task in reworked)
        return f"{total} 次（{detail}）"

    @staticmethod
    def _review_line(tasks: list[Task]) -> str:
        """评审通过率与各层驳回数；没有评审记录时不假装 100%。"""
        reviews = [review for task in tasks for review in task.reviews]
        if not reviews:
            return "—（无评审记录）"
        rejected = [review for review in reviews if review.verdict == "reject"]
        rate = round(100 * (len(reviews) - len(rejected)) / len(reviews))
        g1 = sum(1 for review in rejected if review.layer == "static")
        g2 = sum(1 for review in rejected if review.layer == "llm")
        human = sum(1 for review in rejected if review.layer == "human")
        # R7：主管驳回只在真的发生过时才出现（默认输出逐字不变）。
        supervisor = sum(1 for review in rejected if review.layer == "supervisor")
        tail = f" / 主管驳回 {supervisor}" if supervisor else ""
        return f"{rate}%（G1 拦截 {g1} / G2 驳回 {g2} / 人工驳回 {human}{tail}）"

    @staticmethod
    def _elapsed_line(record: RunRecord) -> str:
        stats = record.stats
        start = stats.started_at or record.created_at
        end = stats.finished_at or record.updated_at
        return _format_duration((end - start).total_seconds())

    @staticmethod
    def _failure_line(task: Task) -> str:
        last = task.reviews[-1] if task.reviews else None
        if last is None:
            detail = "（无评审记录）"
        elif last.issues:
            detail = "；".join(last.issues)
        else:
            detail = "（最后一次评审无问题记录）"
        return (
            f"{task.id} {task.title}  [{task.status.value}]  "
            f"最后一次评审：{detail}"
        )
