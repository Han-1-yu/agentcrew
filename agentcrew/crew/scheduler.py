"""调度引擎：就绪队列、两级并发限额与状态流转（spec §7.3，B7）。

``Scheduler`` 是**确定性**的执行引擎：它不含任何 LLM 逻辑，只做四件事 ——
每轮重算就绪任务、在 ``max_concurrency`` 个任务槽位内派发 worker、把完成事件
经 ``asyncio.Queue`` 回流、按 ``attempts`` 额度与门禁裁决发起状态跃迁。LLM 只
出现在被派发的 Agent 与门禁里；Task 29 的 ``Crew`` 驱动本类。

四条不可动摇的边界（spec §7.3 的修正点，改动前先读这里）：

1. **槽位边界**：``async with self._task_slot`` 只包住 ``Agent.execute``；门禁
   ``Gatekeeper.evaluate`` 在槽位**释放之后**执行。门禁的 LLM 调用要取
   ``ModelFactory`` 的**调用级**信号量，两级限额一旦嵌套（持任务槽等调用槽），
   就把执行与评审的并发预算耦合在一起，还可能演进成自锁。
   ``tests/test_scheduler_deadlock.py`` 用超时兜底（单任务下嵌套不会真的等死，
   所以它是冒烟测试）；真正**结构性**钉住这条边界的是
   ``test_scheduler.py::test_gate_evaluates_after_task_slot_release`` —— 它断言
   ``evaluate`` 执行期间任务槽位未处于 locked 状态，实验证实内嵌实现只能被它
   拦下。
2. **worker 不写状态**：执行成功后的 ``RUNNING → REVIEW`` + ``set_result`` 由
   :meth:`Scheduler._execute_one` 发起、失败后的 ``READY``（重试）/``FAILED``
   与硬取消的 ``CANCELLED`` 由 :meth:`Scheduler._handle_completion` 发起。
   门禁出口的 ``REVIEW → {SUCCEEDED|REWORK|FAILED}`` 归 ``Gatekeeper``（Task 27
   的边界）。``TaskBoard`` 始终是状态的唯一写入点（§3.1 原则 3）。
3. **在跑判定以 worker 注册表为准**：``self._workers`` 记录调度器自己派发的
   asyncio 任务，而不是板上的 ``RUNNING`` 状态。板上的 ``RUNNING`` 可能没有对应
   worker（resume 修复前、硬取消之后）；据板判定会让
   ``test_deadlock_reported_when_no_ready_and_no_running`` 永远等不到死锁报告
   —— 那个用例构造的正是「``RUNNING`` 但引擎无对应 worker」（原实现会挂在该
   用例的 3 秒超时上）。
4. **返工从干净状态开始**：派发 ``REWORK`` 前调 ``ArtifactStore.discard_task``
   丢弃该任务此前的产物记录与文件（Task 13 的 carry-forward）。没有它，一次
   多产出会一直挂在 ``store.by_task`` 上，让此后每一次返工都被 G1 的「契约外
   产物」检查驳回 —— 一个多余文件就把任务判死。
5. **停在 ``REVIEW`` 的任务必须被补派发到门禁**（C2）：``repair_statuses``
   刻意保留 ``REVIEW``（产物已完整落盘，只需重跑门禁），而重启后没有任何
   worker 会认领它 —— 只靠 ``take_ready`` 派发 ``READY``/``REWORK`` 的话，
   它就永远不终结、下游全部卡死（整轮 run 判停滞、退出码 4）。触发窗口是
   **每一次门禁评估**，可达路径是硬杀 / 断电 / 文档化的第二次 Ctrl-C。
   补派发的是一条**只跑门禁**的 worker（不占任务槽位：该任务早已执行完毕，
   没有 Agent 要跑），出口仍归 ``Gatekeeper``。

取消语义（spec §7.3）：``cancel(hard=False)`` 只置事件 —— 主循环不再派发新任务，
在跑的 worker 自然结束并照常回流；``cancel(hard=True)`` 额外 ``cancel()`` 掉在跑
的 asyncio 任务，被取消的任务由取消结局置为 ``CANCELLED``。
``asyncio.CancelledError`` 一律显式捕获后**原样重抛**（吞掉它会破坏取消）。

停滞：无就绪、无在跑但仍未终结时，``deadlock_report`` 取
``TaskBoard.explain_stall()`` 后退出 —— 全程无轮询、无忙等。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentcrew.agents.base_agent import AgentExecutionError
from agentcrew.agents.registry import AgentFactory
from agentcrew.artifacts.spec import ArtifactSpec, parse_kind
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.contracts import AgentBudgetExceeded
from agentcrew.gate.gatekeeper import GateDecision, Gatekeeper
from agentcrew.models.complexity import ComplexityCalibrator, get_calibrator
from agentcrew.models.factory import BudgetExhaustedError
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.checkpoint import CheckpointStore
from agentcrew.runtime.ledger import Ledger
from agentcrew.runtime.superstep import SuperStepJournal
from agentcrew.task.task import Task, TaskResult, TaskStatus
from agentcrew.task.task_board import TaskBoard

#: ``WorkerOutcome.error`` 的取消哨兵：worker 被 ``cancel(hard=True)`` 取消时用它
#: 把「这不是执行失败」带给 :meth:`Scheduler._handle_completion`。真实异常串一律
#: 带 ``类型名: `` 前缀，不会与这个裸串相等。
CANCELLED_ERROR = "cancelled"

#: 执行失败重试时写进跃迁理由的前缀。理由随 ``status_history`` 持久化进
#: ``tasks.json``，下一次派发由 :meth:`Scheduler._fix_hints` 取出来交给模型 ——
#: 这是让重试「有的可改」而不是原样重放的唯一通道。
_RETRY_REASON_PREFIX = "retry: "

#: P2-2：停滞检测的唤醒周期（秒）。主循环每这么久醒一次检查「距上次进展是否
#: 已超过 ``stall_timeout_seconds``」。取较小值让停滞能及时被发现，同时又不会
#: 忙等；真正的判停阈值是构造参数 ``stall_timeout_seconds``，不是这个常量。
_STALL_CHECK_INTERVAL = 1.0


class WorkerOutcome(BaseModel):
    """一个 worker 的终局：执行结果、门禁裁决与错误（至多一个非空）。

    ``decision`` 非空表示执行 + 门禁走完全程（``REVIEW → …`` 已由 ``Gatekeeper``
    完成）；``error`` 非空表示执行或门禁中途失败，由调度器按 ``attempts`` 额度
    决定重试还是 ``FAILED``；``error == CANCELLED_ERROR`` 表示硬取消。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    result: TaskResult | None = None
    decision: GateDecision | None = None
    error: str | None = None
    # 2026-11 容错加固：异常类型名显式成列。历史上类型名被拍进 ``error`` 头部
    # （"Type: msg"），``_emit_error`` 再用 ``partition(":")[0]`` 反解析回来——
    # 消息里任何带冒号的前缀都会让 exc_type 审计字段失真。新字段优先于反解析，
    # 旧字符串格式保持不变（``error`` 仍是完整消息，消费方不受影响）。
    error_type: str | None = None


class SubtaskDispatcher:
    """R8：可调用的子任务入口，额外暴露**最近一次拒绝的原因**。

    P2-1 起 Agent 只能拿到 ``str | None``：``None`` 表示「没建成」，但不知道为什么。
    模型下一轮往往换个同样错的名字继续试，或者干脆把这件事丢掉。这里把 Scheduler
    的拒绝原因挂到可调用对象上，:class:`~agentcrew.agents.base_agent.BaseAgent`
    据此做一次自我纠正。

    用对象而不是闭包/绑定方法，是因为绑定方法挂不上实例状态。既有的测试与库层用法
    传的是裸 ``lambda``，没有 ``last_rejection`` 属性 —— ``BaseAgent`` 视为「无原因
    可回灌」，行为与 R8 之前逐字一致。
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler

    @property
    def last_rejection(self) -> str | None:
        """最近一次 ``__call__`` 的拒绝原因；成功时为 ``None``。"""
        return self._scheduler._last_subtask_rejection

    def __call__(self, spec: dict[str, Any]) -> str | None:
        return self._scheduler._create_subtask(spec)


class Scheduler:
    """就绪队列调度器：派发、回流、状态跃迁与取消（spec §7.3）。

    构造参数即 Task 29 ``Crew`` 的装配点：``agents`` / ``gatekeeper`` /
    ``context_bus`` 三个协作者 + 审计 ledger，可选 checkpoint 与观测回调。
    """

    def __init__(
        self,
        board: TaskBoard,
        agents: AgentFactory,
        gatekeeper: Gatekeeper,
        context_bus: ContextBus,
        *,
        ledger: Ledger,
        checkpoint: CheckpointStore | None = None,
        max_concurrency: int = 4,
        stall_timeout_seconds: int = 300,
        on_event: Callable[[str, dict], None] | None = None,
        on_checkpoint: Callable[[], None] | None = None,
        calibrator: ComplexityCalibrator | None = None,
        catalog: RoleCatalog | None = None,
        settings: Settings | None = None,
        step_gate: Callable[[list[Task]], None] | None = None,
        journal: SuperStepJournal | None = None,
        tracer: Any | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency 必须 >= 1：{max_concurrency}")
        self._board = board
        self._agents = agents
        self._gatekeeper = gatekeeper
        self._context_bus = context_bus
        self._ledger = ledger
        self._checkpoint = checkpoint
        #: R6：委派治理用。两者任一为 ``None``（直连 Scheduler 的库层用法与旧
        #: 测试）时四道闸全部跳过，行为与 R6 之前逐字一致。
        self._catalog = catalog
        self._settings = settings
        # 2026-11 会话隔离改造：run 级校准器由 Crew 显式注入；未注入时
        # _record_calibration 回退读全局（直连 Scheduler 的旧测试行为不变）。
        self._calibrator = calibrator
        #: R20：单步断点门（``Crew`` 在 ``debug_step`` 开启时注入
        #: :class:`~agentcrew.gate.stepper.StepGate`）。``None``（默认）时
        #: 主循环不做任何断点检查，执行路径与 R20 之前逐字一致。
        self._step_gate = step_gate
        #: P2-2：super-step 标记（崩溃后能看出「停在第几轮、在跑哪些任务」）。
        #: ``None``（直连 Scheduler 的库层用法与旧测试）时全程零 I/O。
        self._journal = journal
        #: P2-3：OTel tracer（``None`` = 不采集）。任务 span 挂到 run 根 span 下。
        self._tracer = tracer
        self._on_event = on_event
        #: 每次 checkpoint 落盘后的回调（Task 29 的 Crew 用它同步 ``run.json``
        #: 的统计，见 :meth:`_save_checkpoint`）；未接线时零开销。
        self._on_checkpoint = on_checkpoint
        #: 产物仓库与门禁、Agent 共用同一个实例（从 ContextBus 取，见其 docstring）。
        self._store: ArtifactStore = context_bus.store
        self._max_concurrency = max_concurrency
        #: 任务级并发限额：**只**在 worker 内包住 ``execute``，门禁在槽位外。
        self._task_slot = asyncio.Semaphore(max_concurrency)
        self._completion_queue: asyncio.Queue[WorkerOutcome] = asyncio.Queue()
        #: 在跑 worker 注册表（``task_id → asyncio.Task``）；「是否有任务在跑」
        #: 一律看它，不看板上状态（见模块 docstring 边界 3）。
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._cancel_event = asyncio.Event()
        self._deadlock_report: str | None = None
        self._peak_concurrency = 0
        self._running = False
        #: P0-3：预算耗尽标志。worker 中捕获 BudgetExhaustedError 时置位，
        #: 主循环检测后停止派发、等在跑 worker 收尾，再重抛该异常给 Crew。
        self._budget_exhausted: BudgetExhaustedError | None = None
        #: P2-1：动态子任务 id 序号（``T-DYN-001`` …），保证 run 内唯一。
        self._subtask_seq = 0
        #: R8：最近一次建子任务被拒的**人类可读原因**（成功时清空）。
        #: ``SubtaskDispatcher`` 把它转给 Agent，让模型知道自己错在哪，
        #: 而不是只看到「没建成」。
        self._last_subtask_rejection: str | None = None
        # P2-1：把「在本面板上创建动态子任务」的闭包注入 Agent 工厂——
        # worker 里 Agent 检测到 <subtask> 围栏时回调它。工厂 setter 会同步刷新
        # 已缓存的 Agent 实例；在任何 get() 之前设置，首轮构造即带上回调。
        # R8：换成可调用对象而不是绑定方法——它需要额外暴露 ``last_rejection``，
        # 绑定方法挂不上实例状态。
        self._agents.subtask_creator = SubtaskDispatcher(self)
        # P2-2：运行时停滞检测。``stall_timeout_seconds`` 是「有任务在跑却长时间
        # 无任何状态进展」的阈值（默认 5 分钟）；``_last_progress_at`` 记录最近一次
        # 派发/完成/跃迁的时刻（monotonic 时钟，不受系统时间回拨影响）。
        self._stall_timeout_seconds = stall_timeout_seconds
        self._last_progress_at = time.monotonic()
        self._stalled = False

    # ---- 只读视图 ----

    @property
    def deadlock_report(self) -> str | None:
        """停滞诊断（``None`` 表示本次 run 未判定为停滞）。"""
        return self._deadlock_report

    @property
    def stalled(self) -> bool:
        """P2-2：本次 run 是否因「有任务在跑却超时无进展」被判停滞。

        与 :attr:`deadlock_report` 的「无就绪、无在跑」结构性死锁互补：这个是
        「有 worker 在跑但长时间没有任何状态跃迁」的运行时停滞（worker 僵在某个
        永不返回的 await 上）。Crew 据此映射退出码 4。
        """
        return self._stalled

    @property
    def peak_concurrency(self) -> int:
        """观测到的最大同时 worker 数（派发时实测，不是配置回读）。"""
        return self._peak_concurrency

    # ---- 取消 ----

    def cancel(self, *, hard: bool = False) -> None:
        """请求停止调度：不再派发新任务；``hard`` 时额外取消在跑的 asyncio 任务。

        只置事件与 ``cancel()`` 句柄 —— 状态跃迁仍留在主循环（硬取消的 worker
        经 :data:`CANCELLED_ERROR` 回流后由 :meth:`_handle_completion` 置
        ``CANCELLED``），从而保持 ``TaskBoard`` 单写入点纪律。可重复调用。
        """
        self._cancel_event.set()
        if hard:
            for worker in self._workers.values():
                worker.cancel()

    # ---- 主循环 ----

    async def run(self) -> None:
        """跑到全部任务终结 / 停滞 / 取消为止（spec §7.3 的调度循环）。

        每轮：``refresh`` → ``take_ready(limit=空闲槽位)`` → 派发 → 停在
        ``REVIEW`` 且无人认领的任务补派发门禁（边界 5）→ 有在跑就
        ``await queue.get()`` 处理一个完成事件 → 落 checkpoint。无就绪、无在跑
        但仍未终结 → 记 ``deadlock_report`` 后退出。取消置位后停止派发，等在跑的
        worker 收尾（``_drain_workers``）再返回。
        """
        if self._running:
            raise RuntimeError("Scheduler.run 不可重入：请等待上一次 run 返回")
        self._running = True
        self._deadlock_report = None
        try:
            while not self._board.is_terminal():
                self._board.refresh()
                if self._cancel_event.is_set():
                    self._notify("scheduler_cancelled")
                    break
                free = self._max_concurrency - len(self._workers)
                # R20：单步断点——必须在 take_ready **之前**（那一步扣 attempts
                # 并跃迁 RUNNING，断点上下文要的是「还没派发」的任务）。只在
                # 无在跑 worker 时停留：带病挂起会留下 RUNNING 孤儿；候选为空
                # （只剩 REVIEW 补门禁）也不停留，断点管执行不管重评审。
                if (self._step_gate is not None and not self._workers
                        and not self._cancel_event.is_set()):
                    candidates = self._board.peek_ready(limit=self._max_concurrency)
                    if candidates:
                        self._step_gate(candidates)
                ready = self._board.take_ready(limit=free)
                if ready and self._journal is not None:
                    # P2-2：派发前先落「这一轮在跑谁」。崩溃发生在本轮内时，
                    # resume 读到 phase=dispatch + 这些 task id，就知道上次停在
                    # 半步上（而不是「什么都没发生」）。
                    self._journal.begin(running=[
                        *self._workers, *(task.id for task in ready)])
                for task in ready:
                    self._spawn(task, discarded=self._discard_before_rework(task))
                # 限额**重算**：上一行可能刚把槽位派满，复用同一个 free 会让两个
                # 循环各派一轮，注册表峰值到 2 × max_concurrency（peak_concurrency
                # 是运行报告里读的实测值，不能撒谎）。
                for task_id in self._regate_ids(
                    limit=self._max_concurrency - len(self._workers)
                ):
                    self._spawn_regate(task_id)
                if self._workers:
                    outcome = await self._wait_for_outcome()
                    if outcome is None:
                        # P2-2：超时无进展已判停滞并硬取消在跑 worker —— 退出循环。
                        break
                    self._handle_completion(outcome)
                    self._touch_progress()
                    self._save_checkpoint()
                    # P0-3：预算耗尽 —— 停止派发新任务，等在跑 worker 自然收尾。
                    if self._budget_exhausted is not None:
                        break
                    continue
                # 无就绪、无在跑，却仍未终结：停滞（死锁或全部阻塞），报告后退出。
                self._deadlock_report = self._board.explain_stall()
                self._notify("deadlock", report=self._deadlock_report)
                break
            await self._drain_workers()
            if self._journal is not None:
                # 正常收尾才标 idle：异常/取消路径不写，留下的 dispatch 标记
                # 正好是「这次没跑完」的证据。
                self._journal.end()
            # P0-3：worker 收尾完毕后重抛预算耗尽异常，由 Crew 捕获并优雅收尾。
            if self._budget_exhausted is not None:
                raise self._budget_exhausted
        finally:
            self._running = False

    # ---- worker ----

    async def _worker(self, task: Task) -> None:
        """一个任务的完整生命周期：执行（占槽）→ 门禁（槽外）→ 回流。

        门禁必须在 ``async with self._task_slot`` **之外**调用（模块 docstring
        边界 1）。``asyncio.CancelledError`` 显式捕获、把取消结局送回队列后
        **原样重抛** —— 吞掉它会让取消静默失效。
        """
        span = None
        if self._tracer is not None:
            try:
                span = self._tracer.start_span(
                    f"agentcrew.task {task.id}",
                    attributes={
                        "agentcrew.task_id": task.id,
                        "agentcrew.task_title": task.title,
                        "agentcrew.role": task.role,
                        "agentcrew.attempt": task.attempts,
                    })
            except Exception:  # noqa: BLE001 —— 采集失败不影响调度
                span = None
        try:
            async with self._task_slot:
                outcome = await self._execute_one(task)
            if outcome.error is None:
                decision = await self._gatekeeper.evaluate(
                    self._board.get(task.id), round_no=self._round_no(task)
                )
                outcome = outcome.model_copy(update={"decision": decision})
        except asyncio.CancelledError:
            self._completion_queue.put_nowait(
                WorkerOutcome(task_id=task.id, error=CANCELLED_ERROR)
            )
            raise
        except BudgetExhaustedError as exc:
            # P0-3：预算耗尽 —— 置位标志让主循环停止派发，同时收敛成错误结局
            # 回流（该任务的状态由 _handle_error 正常处理）。主循环检测到标志后
            # 等在跑 worker 收尾再重抛此异常给 Crew。
            self._budget_exhausted = exc
            outcome = _error_outcome(task.id, exc)
        except Exception as exc:
            # 非预期异常（provider 网络/鉴权、门禁崩溃…）也收敛成结局回流：
            # 让主循环按板上状态决定重试/失败，绝不让 run() 等在空队列上。
            outcome = _error_outcome(task.id, exc)
        if span is not None:
            try:
                self._tracer.end_span(
                    span,
                    status="error" if outcome.error is not None else "ok",
                    attributes={
                        # 落板上的最终状态（REVIEW / FAILED / …）比 outcome 的
                        # 内部字段更稳：WorkerOutcome 的字段集合会随门禁演化。
                        "agentcrew.task_status": (
                            getattr(self._board.get(task.id), "status", None)
                            and self._board.get(task.id).status.value),
                        "agentcrew.task_error": (
                            str(outcome.error)[:300] if outcome.error else None),
                    })
            except Exception:  # noqa: BLE001
                pass
        self._completion_queue.put_nowait(outcome)

    async def _regate_worker(self, task_id: str) -> None:
        """只跑门禁的 worker：把停在 ``REVIEW`` 的任务重新送给 ``Gatekeeper``。

        **故意不占任务槽位**：``async with self._task_slot`` 的语义是「一个 Agent
        正在执行」，而这里的任务早在被杀之前就执行完了（产物完整落盘）—— 门禁
        本来就在槽位之外跑（边界 1），给它占一个槽位只会把执行并发无谓地掐掉。
        出口与 :meth:`_worker` 完全一致：``Gatekeeper.evaluate`` 负责
        ``REVIEW → {SUCCEEDED|REWORK|FAILED}``，异常收敛成错误结局回流。
        """
        try:
            task = self._board.get(task_id)
            decision = await self._gatekeeper.evaluate(
                task, round_no=self._round_no(task))
            outcome = WorkerOutcome(task_id=task_id, decision=decision)
        except asyncio.CancelledError:
            self._completion_queue.put_nowait(
                WorkerOutcome(task_id=task_id, error=CANCELLED_ERROR)
            )
            raise
        except BudgetExhaustedError as exc:
            # P0-3：预算耗尽（门禁的 LLM 调用也可能触发）。
            self._budget_exhausted = exc
            outcome = _error_outcome(task_id, exc)
        except Exception as exc:
            outcome = _error_outcome(task_id, exc)
        self._completion_queue.put_nowait(outcome)

    async def _execute_one(self, task: Task) -> WorkerOutcome:
        """在任务槽位内执行一个任务；成功即写结果并转 ``REVIEW``。

        ``RUNNING → REVIEW`` 必须先于门禁（``Gatekeeper`` 的出口是从 ``REVIEW``
        出发的），因此它是执行阶段的一部分。``AgentExecutionError`` 是预期内的
        执行失败（解析/契约/落盘），作为错误结局返回，由
        :meth:`_handle_completion` 按额度决定去向。
        """
        fix_hints = self._fix_hints(task)
        try:
            # P1-4：任务级超时 —— 外包 asyncio.wait_for，LLM 调用僵死不会永久占槽位。
            # 超时转为 AgentExecutionError，走正常 attempts 重试通道（不单独计数）。
            async def _run_with_timeout() -> TaskResult:
                # V2: 预取上游摘要的 LLM 压缩（未配置摘要模型时为 no-op 截断）。
                await self._context_bus.prefetch_summaries(task)
                pack = self._context_bus.build(task, fix_hints=fix_hints)
                return await self._agents.agent_for_task(task.role, task.id).execute(
                    task, pack, fix_hints=fix_hints
                )
            result = await asyncio.wait_for(
                _run_with_timeout(), timeout=task.timeout_seconds
            )
        except asyncio.TimeoutError:
            return _error_outcome(
                task.id,
                AgentExecutionError(
                    f"任务超时（{task.timeout_seconds}s）：{task.id}",
                    task_id=task.id, role=task.role,
                ),
            )
        except AgentBudgetExceeded as exc:
            # 执行体预算耗尽：转成执行失败，走 attempts 通道；因为闸门在调用前，
            # 重试不会再多花钱（每次都在第一次调用前被拦下）。
            return _error_outcome(task.id, AgentExecutionError(
                str(exc), task_id=task.id, role=task.role))
        except AgentExecutionError as exc:
            return _error_outcome(task.id, exc)
        self._board.set_result(task.id, result)
        self._transition(task.id, TaskStatus.REVIEW, reason="executed")
        return WorkerOutcome(task_id=task.id, result=result)

    # ---- 派发 ----

    def _spawn(self, task: Task, *, discarded: int) -> None:
        """登记并启动一个 worker；``peak_concurrency`` 在此实测更新。"""
        self._workers[task.id] = asyncio.create_task(self._worker(task))
        self._peak_concurrency = max(self._peak_concurrency, len(self._workers))
        self._touch_progress()
        self._notify(
            "task_dispatched",
            task_id=task.id,
            role=task.role,
            attempt=task.attempts,
            rework=_is_rework_dispatch(task),
            discarded=discarded,
        )

    def _spawn_regate(self, task_id: str) -> None:
        """登记并启动一个只跑门禁的 worker（``REVIEW`` 补派发，见边界 5）。

        事件名 ``task_regated`` 是**增量**：看板按事件名过滤，未知事件静默忽略
        （与 ``llm_retry`` / ``task_notes`` 同族）；它必须与 ``task_dispatched``
        区分开 —— 那不是一次执行派发，``attempts`` 一分不少。
        """
        self._workers[task_id] = asyncio.create_task(self._regate_worker(task_id))
        self._peak_concurrency = max(self._peak_concurrency, len(self._workers))
        self._touch_progress()
        self._notify("task_regated", task_id=task_id)

    def _regate_ids(self, *, limit: int) -> list[str]:
        """停在 ``REVIEW`` 且**当前没有 worker** 的任务 id（按 id 升序，取前 limit 个）。

        两个筛选条件缺一不可：

        - ``REVIEW``：只有这个状态「没有 worker 也没有就绪通道」；
        - 不在 ``self._workers`` 里：``evaluate`` 内部要 ``await`` 评审调用，
          完成任务之前板上状态仍是 ``REVIEW``，没有这个检查就会在下一轮重复
          派发同一个任务（同一次门禁被并行判两遍）。

        ``limit`` 由调用方给**当前**的空闲槽位数 —— 注意是 ``take_ready`` 派发
        **之后**重算的那个值（``max_concurrency − len(self._workers)``），而不是
        同一个 ``free`` 的复用：复用会让两个循环各派一轮，注册表峰值到 2 ×
        ``max_concurrency``。它同时充当「本轮最多补派发几个」的节流。
        """
        return [
            task.id
            for task in self._board.all()
            if task.status is TaskStatus.REVIEW and task.id not in self._workers
        ][: max(0, limit)]

    def _discard_before_rework(self, task: Task) -> int:
        """返工派发前清空该任务旧产物；非返工派发不做任何事。

        返回被清除的记录数（仅用于观测；``delete_files`` 的原子性语义见
        ``ArtifactStore.discard_task``）。
        """
        if not _is_rework_dispatch(task):
            return 0
        return self._store.discard_task(task.id)

    # ---- 完成事件 ----

    def _handle_completion(self, outcome: WorkerOutcome) -> None:
        """处理一个完成事件：先摘除注册表，再做调度器侧的状态跃迁。

        门禁裁决引发的 ``REVIEW → {SUCCEEDED|REWORK|FAILED}`` 已由 ``Gatekeeper``
        完成（Task 27 边界）；这里只处理执行/门禁错误与硬取消。
        """
        task_id = outcome.task_id
        self._workers.pop(task_id, None)
        if outcome.error == CANCELLED_ERROR:
            self._cancel_running(task_id)
        elif outcome.error is not None:
            # error_type（2026-11）：异常类型名显式传递，不再依赖字符串反解析。
            self._handle_error(task_id, outcome.error, outcome.error_type)
        decision = outcome.decision
        self._notify(
            "task_completed",
            task_id=task_id,
            status=self._board.get(task_id).status.value,
            verdict=None if decision is None else decision.verdict,
        )
        # P3-1：任务走到终局（SUCCEEDED / FAILED）时把实际结局喂给全局校准器。
        # 仍在 READY（待重试）/ REWORK（待返工）的任务还没定局，不记录 ——
        # 否则一次重试的中间失败就会污染同类任务的成功率。CANCELLED / BLOCKED
        # 是外部结局，不反映模型选得对不对，同样不记。
        self._record_calibration(task_id)

    def _record_calibration(self, task_id: str) -> None:
        """终局任务喂给复杂度校准器（P3-1）；校准器未注入时零开销跳过。"""
        # 2026-11 会话隔离改造：优先用 run 级注入实例，未注入回退全局（兼容）。
        calibrator = (
            self._calibrator if self._calibrator is not None
            else get_calibrator())
        if calibrator is None:
            return
        task = self._board.get(task_id)
        if task.status is TaskStatus.SUCCEEDED:
            calibrator.record(task, True, task.attempts)
        elif task.status is TaskStatus.FAILED:
            calibrator.record(task, False, task.attempts)

    def _handle_error(
        self, task_id: str, error: str, error_type: str | None = None
    ) -> None:
        """执行/门禁错误的去向：按板上状态与 ``attempts`` 额度决定。

        执行阶段（``RUNNING``）：``attempts < max_attempts`` → ``READY`` 重试，
        否则 ``FAILED``。``attempts`` 已在 ``take_ready`` 派发时自增（Task 8），
        故比较用的是板上（派发后）的值 —— 返工派发不扣 ``attempts``，其判据仍
        是同一个数。

        门禁阶段（``REVIEW``）：``REVIEW`` 没有 → ``READY`` 的边，重跑评审也无
        额度可扣，故判 ``FAILED`` 并留 ``error`` 审计事件，而不是把整次 run
        打崩。已终结/``REWORK`` 后的迟到错误无合法边，原样保留。
        """
        task = self._board.get(task_id)
        if task.status is TaskStatus.RUNNING:
            self._emit_error("agent.execute", task_id, error,
                             error_type=error_type)
            if task.attempts < task.max_attempts:
                # 把失败原因**写进跃迁理由**：它会随 status_history 落进
                # tasks.json，下一次派发时由 _fix_hints 取出来交给模型。
                # 没有这一条，重试就是原样重放 —— 实测架构师的回复连续 3 次被
                # 输出上限截断，模型每次都在做同一件徒劳的事。
                self._transition(
                    task_id,
                    TaskStatus.READY,
                    reason=f"{_RETRY_REASON_PREFIX}{error[:400]}",
                )
            else:
                self._transition(
                    task_id, TaskStatus.FAILED, reason="attempts-exhausted"
                )
            return
        if task.status is TaskStatus.REVIEW:
            self._emit_error("gate.evaluate", task_id, error,
                             error_type=error_type)
            self._transition(task_id, TaskStatus.FAILED, reason="gate-error")

    def _cancel_running(self, task_id: str) -> None:
        """硬取消：把有 worker 的任务推进到 ``CANCELLED``（RUNNING/REVIEW 两条边）。"""
        if self._board.get(task_id).status in (
            TaskStatus.RUNNING,
            TaskStatus.REVIEW,
        ):
            self._transition(task_id, TaskStatus.CANCELLED, reason="hard-cancel")

    async def _drain_workers(self) -> None:
        """等在跑的 worker 全部收尾并处理它们的完成事件（取消/退出路径用）。

        取消后不再派发任何新任务，只把已在跑的任务处理完 —— 协作式停止
        （spec §7.3）。
        """
        while self._workers:
            outcome = await self._completion_queue.get()
            self._handle_completion(outcome)
            self._save_checkpoint()

    # ---- 辅助 ----

    def _round_no(self, task: Task) -> int:
        """本轮评审轮次 = 已完成的评审轮数 + 1。

        每轮 ``Gatekeeper.evaluate`` 恰好留下一条 ``layer="static"`` 记录（不论
        G1 是否短路 G2），据此计数才不会把 G2 的额外一条重复计成一整轮。
        """
        return sum(1 for r in task.reviews if r.layer == "static") + 1

    def _fix_hints(self, task: Task) -> list[str]:
        """这次派发给模型的提示，按重要性排序，去重保序。

        两个来源：

        1. **执行失败的原因**（``retry:`` 前缀的跃迁理由）—— 没有它，重试只是
           把同一件事原样重放。实测架构师的回复连续 3 次撞上输出上限被截断，
           而模型每次都看不到任何反馈，三次失败方式完全一致。
        2. **最后一次驳回轮次的 ``fix_hints``**（返工）—— 同一轮里 G1/G2 取并集。
        """
        hints: list[str] = []
        for change in reversed(task.status_history):
            if change.reason.startswith(_RETRY_REASON_PREFIX):
                error = change.reason[len(_RETRY_REASON_PREFIX):].strip()
                if error:
                    hints.append(f"上次执行失败，请据此修正：{error}")
                break
        rejected = [r for r in task.reviews if r.verdict == "reject"]
        if rejected:
            last_round = max(r.round for r in rejected)
            hints.extend(
                hint
                for record in rejected
                if record.round == last_round
                for hint in record.fix_hints
            )
        return list(dict.fromkeys(hints))

    def _transition(self, task_id: str, to: TaskStatus, *, reason: str) -> None:
        """经 ``TaskBoard`` 跃迁并落一条 ``task_transition`` 审计事件。

        ``from`` 是 Python 关键字，只能经 ``**`` 传进 ``emit`` 的 ``data``。
        """
        before = self._board.get(task_id).status
        self._board.transition(task_id, to, reason=reason)
        self._ledger.emit(
            "task_transition",
            task_id=task_id,
            **{"from": before.value, "to": to.value, "reason": reason},
        )
        # 状态跃迁本身就是「板子往前动了」的证据。慢门禁（G2 评审超过
        # stall_timeout_seconds）期间只有跃迁、没有完成事件，此前漏刷会让
        # _wait_for_outcome 误判停滞、硬取消一个正在正常评审的 worker。
        self._touch_progress()

    def _emit_error(
        self,
        where: str,
        task_id: str,
        error: str,
        *,
        error_type: str | None = None,
    ) -> None:
        """落一条 ``error`` 审计事件（spec §14.1）。

        ``exc_type`` 优先取显式传入的 ``error_type``（WorkerOutcome 新字段）；
        未提供时回退到从错误串头部分析（历史行为，保留兼容）。
        """
        self._ledger.emit(
            "error",
            where=where,
            task_id=task_id,
            exc_type=error_type or error.partition(":")[0] or "Exception",
            message=error,
        )

    # ---- P2-1：运行时动态子任务 ----

    #: R6：``manager_only`` 模式下允许派活的角色（对标 CrewAI hierarchical 的 manager）。
    _DELEGATION_MANAGERS = frozenset({"project_manager", "product_manager"})

    def _delegation_depth(self, task_id: str) -> int:
        """``task_id`` 的委派层数：根任务 ``0``，它的子任务 ``1``，依此类推。

        沿 ``parent_id`` 链回溯，并用 ``seen`` 兜住环（坏数据不该把调度器转死）。
        任务不在板上（父任务已被返工丢弃）时按已走过的层数返回。
        """
        depth = 0
        seen: set[str] = set()
        current = task_id
        known = set(self._board.ids())
        while current and current not in seen:
            seen.add(current)
            if current not in known:
                break
            parent = getattr(self._board.get(current), "parent_id", None) or ""
            if parent:
                depth += 1
            current = parent
        return depth

    def _delegation_rejection(
        self, spec: dict[str, Any], parent_id: str
    ) -> str | None:
        """R6：委派四道闸——通过返回 ``None``，拒绝返回一句人类可读的原因。

        顺序刻意从「最便宜的判断」到「最贵的」：角色名（一次集合查询）→ 权限
        （一次角色比较）→ 深度（走父链）→ 配额（扫全板）。每个拒绝分支都自己发
        一条带原因的事件，调用方只管返回 ``None``，不重复记账。

        ``settings`` 未注入时**全部跳过**——库层直接构造 Scheduler 的用法（以及
        R6 之前写下的测试）行为逐字不变。
        """
        if self._settings is None:
            return None
        role = str(spec.get("role") or "generic")

        # ① 角色必须真实存在。拼错的角色名会静默落到通用兜底（没有专长提示词），
        #    任务照样跑完、产物看着也在，质量却塌了一档——这是最隐蔽的一种坏。
        if (self._catalog is not None
                and role != "generic"
                and not self._catalog.has(role)):
            self._ledger.emit(
                "subtask_role_unknown",
                parent_id=parent_id,
                role=role,
                available=list(self._catalog.keys()),
            )
            return (
                f"团队里没有 {role!r} 这个角色"
                f"（可用：{'、'.join(self._catalog.keys())}）"
            )

        # ② manager_only：只有经理能派活。判断依据是父任务的角色——它才是
        #    「谁在派」的权威来源（Agent 自己说自己是不是经理不算数）。
        if self._settings.delegation_mode == "manager_only":
            parent = (self._board.get(parent_id)
                      if parent_id and parent_id in set(self._board.ids()) else None)
            parent_role = getattr(parent, "role", "") or ""
            if parent_role not in self._DELEGATION_MANAGERS:
                self._ledger.emit(
                    "subtask_unauthorized",
                    parent_id=parent_id,
                    role=parent_role,
                    requested_role=role,
                )
                return (
                    f"当前 delegation_mode=manager_only："
                    f"只有 {'/'.join(sorted(self._DELEGATION_MANAGERS))} "
                    f"可以派发子任务，{parent_role or '未知角色'} 不能"
                )

        # ③ 层数：新任务的深度 = 父任务深度 + 1。
        parent_depth = self._delegation_depth(parent_id) if parent_id else 0
        new_depth = parent_depth + 1
        if new_depth > self._settings.max_delegation_depth:
            self._ledger.emit(
                "subtask_depth_exceeded",
                parent_id=parent_id,
                depth=new_depth,
                max_depth=self._settings.max_delegation_depth,
            )
            return (
                f"委派层数已达上限（本任务将是第 {new_depth} 层，"
                f"上限 {self._settings.max_delegation_depth}）：请自己完成"
            )

        # ④ 总数配额：深度管「能套多深」，这条管「总共能裂变多少个」。
        used = sum(1 for task in self._board.all() if task.dynamic)
        if used >= self._settings.max_dynamic_tasks:
            self._ledger.emit(
                "subtask_quota_exceeded",
                parent_id=parent_id,
                used=used,
                max_tasks=self._settings.max_dynamic_tasks,
            )
            return (
                f"本次 run 的动态子任务已达上限（{self._settings.max_dynamic_tasks} 个）"
                f"：请自己完成剩余工作"
            )
        return None

    def _create_subtask(self, spec: dict[str, Any]) -> str | None:
        """把 Agent 请求的子任务 spec 构造成 ``Task`` 并经 ``board.add_task`` 入板。

        ``spec`` 由模型产出（title/description/role/deliverables）+ Agent 注入的
        ``parent_id``。子任务无依赖、直接 ``READY``（独立调度），并打上
        ``dynamic=True``。返回新任务 id——Agent 随后记 ``subtask_created`` 事件。

        **R6 委派治理**先过四道闸（详见 :meth:`_delegation_rejection`）：角色必须
        真实存在、``manager_only`` 模式下只有经理能派、层数不超
        ``max_delegation_depth``、总数不超 ``max_dynamic_tasks``。任一道不过就
        返回 ``None`` 并记一条带原因的事件——不静默降级，也不拖垮 run。

        **契约路径先占先得**（realrun10 实测）：T-IMPL 声明交付
        ``logstat/__init__.py``，它拆出的动态子任务又声明同一个路径，子任务先跑
        完写走文件，父任务落盘时被跨任务冲突检测拒掉，三次 attempt 烧光后
        FAILED、下游全 BLOCKED。路径冲突本身必须拒绝（那是防覆盖的护栏），
        但**不该发生在子任务与既有契约之间**——所以入板前先剔除已被任何任务
        声明过的路径；一个都不剩就干脆不建这个子任务，返回 ``None``。
        """
        parent_id = str(spec.get("parent_id") or "")
        # R8：每次进入先清空——成功时它必须为 None，否则 Agent 会把上一次的
        # 旧原因当成这一次的（循环里连建多个子任务时尤其容易串）。
        self._last_subtask_rejection = None
        rejection = self._delegation_rejection(spec, parent_id)
        if rejection is not None:
            self._last_subtask_rejection = rejection
            self._ledger.emit(
                "subtask_rejected_by_policy",
                parent_id=parent_id,
                role=str(spec.get("role") or "generic"),
                title=str(spec.get("title") or ""),
                reason=rejection,
            )
            return None
        requested = [
            _deliverable_from_spec(item) for item in (spec.get("deliverables") or [])
        ]
        reserved = {
            declared.path
            for other in self._board.all()
            for declared in other.deliverables
        }
        deliverables = [d for d in requested if d.path not in reserved]
        if requested and not deliverables:
            conflicts = sorted({d.path for d in requested})
            self._last_subtask_rejection = (
                f"请求交付的路径已被其他任务占用：{'、'.join(conflicts)}"
                f"——请另选路径，或改为自己完成"
            )
            self._ledger.emit(
                "subtask_conflict",
                parent_id=parent_id,
                title=str(spec.get("title") or ""),
                role=str(spec.get("role") or "generic"),
                conflicts=conflicts,
            )
            return None
        if len(deliverables) != len(requested):
            self._ledger.emit(
                "subtask_deliverable_pruned",
                parent_id=parent_id,
                pruned=[d.path for d in requested if d.path in reserved],
            )
        task_id = self._next_subtask_id()
        known = set(self._board.ids())
        parent_task = (
            self._board.get(parent_id) if parent_id and parent_id in known else None)
        task = Task(
            id=task_id,
            title=str(spec.get("title") or "动态子任务"),
            description=str(spec.get("description") or ""),
            role=str(spec.get("role") or "generic"),
            acceptance_criteria=[],
            deliverables=deliverables,
            dependencies=[],
            parent_id=parent_id or None,
            dynamic=True,
            # R6：留痕「谁派的」——父任务可能被返工丢弃，角色不会。
            delegated_by=(getattr(parent_task, "role", "") or ""),
        )
        self._board.add_task(task)
        return task_id

    def _next_subtask_id(self) -> str:
        """生成 run 内唯一的动态任务 id（``T-DYN-001`` 递增，避开已用 id）。"""
        existing = set(self._board.ids())
        self._subtask_seq += 1
        while f"T-DYN-{self._subtask_seq:03d}" in existing:
            self._subtask_seq += 1
        return f"T-DYN-{self._subtask_seq:03d}"

    def _save_checkpoint(self) -> None:
        """每处理完一个完成事件就落一次 checkpoint（``tasks.json`` + 回调）。

        ``on_checkpoint`` 在这里被调用**正是为了 ``run.json``**：它的统计只在
        run/resume 开始与收尾写的话，硬杀之后盘上的 ``stats.llm_calls`` 会停在 0，
        而 ledger 里已经躺着几条 ``llm_call``（token 已经花掉了）—— resume 以那份
        陈旧记录为基线，被中断那一段的开销就永久从 run.json 里消失，而 ``status``
        与运行报告读的正是 run.json。
        """
        if self._checkpoint is not None:
            self._checkpoint.save_tasks(self._board)
        if self._on_checkpoint is not None:
            self._on_checkpoint()

    def _notify(self, event: str, **data: Any) -> None:
        """把一条调度事件交给可选的 ``on_event`` 观察者（未接线时零开销）。"""
        if self._on_event is not None:
            self._on_event(event, data)

    # ---- P2-2：运行时停滞检测 ----

    def _touch_progress(self) -> None:
        """把「最近一次进展」重置为现在（monotonic 时钟）。

        任何让任务板往前动一下的动作都调它：派发、完成事件、状态跃迁。停滞判定
        读的是 ``now - _last_progress_at``，只要还有任何任务在动就一直被重置。
        """
        self._last_progress_at = time.monotonic()

    async def _wait_for_outcome(self) -> WorkerOutcome | None:
        """等一个完成事件；期间周期性检查是否超时无进展。

        原本这里是裸 ``await queue.get()``——worker 僵在某个永不返回的 await 上时
        它会永远阻塞。改成 ``asyncio.wait_for`` 分段等：每 ``_STALL_CHECK_INTERVAL``
        秒醒一次，若**仍有在跑 worker** 且距上次进展已超过 ``stall_timeout_seconds``，
        就判停滞、硬取消在跑 worker，返回 ``None`` 让主循环退出。

        正常的长任务（一次合法的 LLM 调用跑很久）不会误触：只要它在阈值内产出
        过任意一次完成/跃迁，``_last_progress_at`` 就被刷新。
        """
        while True:
            try:
                outcome = await asyncio.wait_for(
                    self._completion_queue.get(),
                    timeout=_STALL_CHECK_INTERVAL,
                )
                return outcome
            except asyncio.TimeoutError:
                if not self._workers:
                    continue
                idle = time.monotonic() - self._last_progress_at
                if idle >= self._stall_timeout_seconds:
                    self._trigger_stall(idle)
                    return None

    def _trigger_stall(self, idle_seconds: float) -> None:
        """置停滞标志、留痕、硬取消在跑 worker。

        ledger 记 ``run_stalled``（含 ``stalled_for_seconds`` 与 ``active_tasks``
        ——当时哪些任务还挂着，是停滞诊断的第一手线索）。硬取消让僵住的 worker
        立刻抛 ``CancelledError`` 回流，主循环随后 ``_drain_workers`` 收干净。
        """
        self._stalled = True
        active = sorted(self._workers)
        self._ledger.emit(
            "run_stalled",
            stalled_for_seconds=int(idle_seconds),
            active_tasks=active,
        )
        self._notify(
            "run_stalled",
            stalled_for_seconds=int(idle_seconds),
            active_tasks=active,
        )
        self.cancel(hard=True)


def _error_outcome(task_id: str, exc: BaseException) -> WorkerOutcome:
    """把异常收敛成错误结局（类型名进 ``error`` 头部与 ``error_type``，供审计）。"""
    return WorkerOutcome(
        task_id=task_id,
        result=None,
        decision=None,
        error=f"{type(exc).__name__}: {exc}",
        error_type=type(exc).__name__,
    )


def _deliverable_from_spec(item: Any) -> ArtifactSpec:
    """把子任务请求里的一个 deliverables 项归一化成 :class:`ArtifactSpec`。

    模型给的可能是路径字符串，也可能是部分字段的 dict；缺省 kind 取 ``text``、
    description 回落到路径。无法识别的形态一律给一份诚实的兜底契约，而不是抛错
    ——动态子任务的契约本就粗糙，让它进调度再由门禁/执行反馈修正。
    """
    if isinstance(item, dict):
        path = str(item.get("path") or item.get("description") or "subtask_output.md")
        kind = parse_kind(str(item.get("kind") or "text"))
        description = str(item.get("description") or path)
        try:
            return ArtifactSpec(path=path, kind=kind, description=description)
        except Exception:
            return ArtifactSpec(path=path, kind="text", description=description)
    path = str(item)
    return ArtifactSpec(path=path, kind="text", description=path)


def _is_rework_dispatch(task: Task) -> bool:
    """``take_ready`` 刚派发的这个任务是否走的 ``REWORK → RUNNING`` 通道。

    只看最后一条 ``StatusChange``（``TaskBoard.take_ready`` 刚追加的那条）：
    ``READY → RUNNING`` 是 ``dispatched``，``REWORK → RUNNING`` 是
    ``rework-dispatched``（Task 8 的两条派发通道故意不对称）。
    """
    if not task.status_history:
        return False
    last = task.status_history[-1]
    return (
        last.from_status is TaskStatus.REWORK
        and last.to_status is TaskStatus.RUNNING
    )
