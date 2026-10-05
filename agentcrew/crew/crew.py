"""``Crew``：两阶段编排（spec §7.0 规划 + §7.3 执行）。

一次 run 由本项目唯一的顶层协调者驱动，分层装配下面每一层：

- **阶段 0 · 规划**（非确定性，但输出被严格校验）：``ProjectManagerAgent.decompose``
  产出的拆解经 ``to_tasks`` → ``with_final_task`` → ``validate_plan``；任何
  ``PlanValidationError`` 都把 ``errors`` 整体回灌给 PM 重拆，上限
  ``settings.plan_max_attempts``（默认 2），仍不过则抛 :class:`PlanFailedError`
  （Task 32 映射退出码 2）。**校验对象是追加 T-FINAL 之后的列表** —— PM 若声明了
  ``DELIVERY.md``，冲突只在追加后才存在，校验追加前的列表等于白烧一轮重拆额度。
- **阶段 1 · 执行**（确定性）：``Scheduler`` 在任务槽位内派发 Agent、槽位外跑
  门禁，直到全部终结 / 停滞 / 取消；``Crew`` 不做任何调度决策。

G3 人工闸门的两个卡点都由 ``Gatekeeper`` 询问：计划卡点在**校验通过且 T-FINAL
已追加**之后打开（人工批准的就是将要执行的那份计划），交付卡点在调度循环结束后
打开；``edit`` 结果重新追加 T-FINAL 并重跑 §7.2 校验，``rework`` 把指定任务送回
调度（``FAILED`` 走 ``TaskBoard.reset_failed``，``SUCCEEDED`` 因状态机无出边而经
重建面板人工置回 ``READY`` —— 与 ``reset_failed`` 同类的显式绕过，历史里留下
``reason="delivery-rework"``）。``prompter`` 一律**原样透传**：开了人工卡点却没接线
时由 ``Gatekeeper`` 在构造期抛 ``ValueError``（Task 27），Crew 不做静默兜底 ——
替调用方塞一个 ``NullPrompter`` 会把「忘了接」重新变成「自动放行」，且离错误更远、
更难看见。合法出口只有两个显式声明：``gate="none"`` 或显式传入 ``NullPrompter()``。

resume（spec §13.3）跳过规划与计划卡点，只修复状态后重入执行段；两处
carry-forward 是 load-bearing 的：``ArtifactStore.load_manifest`` 重建产物记录
（否则 §9.2 的跨任务冲突检测与返工清理在重启后失明），决策 JSONL **只追加新增
行**（``ContextBus.save_decisions`` 是朴素追加器，重存全量会让历史决策翻倍）。

状态一律取 ``RunStatus.value`` / ``TaskStatus.value`` 再插值：这两个枚举是
``str`` 混入，直接进 f-string 会渲染成 ``RunStatus.COMPLETED``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from agentcrew.agents.base_agent import BaseAgent
from agentcrew.agents.project_manager import ProjectManagerAgent
from agentcrew.agents.registry import AGENT_CLASSES, AgentFactory
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.crew.scheduler import Scheduler
from agentcrew.gate.gatekeeper import Gatekeeper, Prompter, Reviewer
from agentcrew.models import factory as model_factory
from agentcrew.models.factory import ModelFactory, ModelFactoryError
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunRecord,
    RunStatus,
    repair_statuses,
)
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.planner import (
    PlanValidationError,
    to_tasks,
    validate_plan,
    with_final_task,
)
from agentcrew.task.task import (
    ReviewRecord,
    RunStats,
    StatusChange,
    Task,
    TaskStatus,
)
from agentcrew.task.task_board import TaskBoard
from agentcrew.utils.files import atomic_write_text

#: run 级退出码（spec §7.4）；Task 32 的 CLI 直接消费本表。
EXIT_CODES: dict[str, int] = {
    "ok": 0,
    "failed": 1,
    "plan": 2,
    "config": 3,
    "stalled": 4,
    "cancelled": 130,
}

#: 打开计划卡点的 gate 模式（与 ``Gatekeeper`` 的判定一致）。
_PLAN_GATES: frozenset[str] = frozenset({"plan", "both"})

#: 打开交付卡点的 gate 模式。
_DELIVERY_GATES: frozenset[str] = frozenset({"delivery", "both"})

#: ``RoleSpec.model`` 的哨兵值：字面量 ``"default"`` 表示按三级优先级解析。
_DEFAULT_ALIAS = "default"

#: 交付卡点可以打回的状态：``FAILED``（``reset_failed``）与 ``SUCCEEDED``
#: （快照层置回 ``READY``）。其余状态要么已经在返工队列里，要么没有合法的回退路径。
_REWORKABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.FAILED, TaskStatus.SUCCEEDED}
)

#: 人工打回留痕的 ``reviewer`` 标识（与 ``static:contract`` 同族，spec §5.3）。
_HUMAN_REVIEWER = "human:delivery"

#: 人工打回的 issue / fix_hint：前者进报告，后者经 ``Scheduler._fix_hints`` 回灌给
#: 重新执行的任务（返工的唯一解释必须到执行者手里）。
_HUMAN_REWORK_ISSUE = "交付卡点人工打回：本次交付未被接受"
_HUMAN_REWORK_HINT = "按交付要求修正后重新提交"

#: 停滞判定里的「已定局」状态：终态 + ``BLOCKED``。``BLOCKED`` 在状态机里不是终态
#: （它留着 ``BLOCKED → READY`` 的人工恢复边），但在 run 的结局上已经定局 —— 它的
#: 上游永久失败/取消，spontaneous 恢复不可能发生。
_SETTLED_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
        TaskStatus.SKIPPED,
    }
)

#: 模型别名解析的优先级（spec §12.2）：角色级 → ``role_bindings`` → 默认模型。
#: 与 ``BaseAgent._resolve_alias`` 同一口径 —— 预算校验必须校验**真正会跑**的别名。
def _alias_for(spec: RoleSpec, settings: Settings) -> str:
    """某角色实际绑定的模型别名。"""
    if spec.model != _DEFAULT_ALIAS:
        return spec.model
    return settings.role_bindings.get(spec.key) or settings.default_model


def build_catalog(settings: Settings) -> RoleCatalog:
    """内置八角色 + 可选的 ``roles_dir``（spec §8.3）。

    公开函数：``main.py`` 在构造 ``CliPrompter`` 之前就要拿到与 ``Crew``
    同一个目录，才能让计划编辑路径也用上自定义角色（否则编辑后的计划会被
    「角色未注册」误拒）。同一个 ``RoleCatalog`` 实例必须同时交给两者。
    """
    catalog = RoleCatalog(BUILTIN_ROLES)
    if settings.roles_dir is not None:
        catalog.load_dir(settings.roles_dir)
    return catalog


class PlanFailedError(RuntimeError):
    """规划阶段用尽 ``plan_max_attempts`` 仍未通过校验（spec §7.0，退出码 2）。

    ``errors`` 是最后一次拆解的全部校验错误；Task 32 的 CLI 据此报错并映射退出码 2。
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("\n".join(self.errors) or "拆解连续失败")


class RunCancelled(RuntimeError):
    """G3 卡点取消 / 保存退出（spec §10.3）；CLI 映射退出码 130。"""


@dataclass(frozen=True)
class _Run:
    """一次 ``run()`` / ``resume()`` 的装配（spec §7.0 步骤 1–3 的产物）。

    绑定到单个 run 目录的资源一次性建好，各阶段方法显式接收它 —— 避免把
    「本次 run 的 ledger/记录」散成一堆可选实例属性。
    """

    goal: str
    record: RunRecord
    checkpoint: CheckpointStore
    ledger: Ledger
    store: ArtifactStore
    models: ModelFactory
    agents: AgentFactory
    base_stats: RunStats


class Crew:
    """两阶段编排器：装配全部层并驱动 规划 → 执行 → 交付 的完整流程。"""

    def __init__(
        self,
        settings: Settings,
        *,
        catalog: RoleCatalog | None = None,
        store: ArtifactStore | None = None,
        checkpoint: CheckpointStore | None = None,
        prompter: Prompter | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        """``catalog`` / ``store`` 缺省由本类按需构造；``prompter`` **原样透传**。

        ``prompter`` 没有默认实现，也不会被替换成 ``NullPrompter``：``gate`` 含
        人工卡点而这里为 ``None`` 时，``Gatekeeper`` 在构造期抛 ``ValueError``
        （Task 27），不会被吞掉或降级。合法出口只有两个显式声明 —— 配
        ``gate="none"``（全自动路径）或显式传入 ``NullPrompter()``。
        该错误在 ``run()`` / ``resume()`` 装配 ``Gatekeeper`` 时浮现（计划阶段
        每次都会重建它），而不是 ``Crew.__init__``：构造期还没有面板可绑定。

        ``checkpoint`` 只服务于 :meth:`resume_from`（``run()`` 每个 run 都必须有
        自己的 run 目录，总是新建）。``on_event`` 是调度事件的观察接口
        （``(事件名, 数据)``，与 ``Scheduler.on_event`` 同形），Task 32 用它接
        ``BoardView``，不接线时零开销。
        """
        self._settings = settings
        self._catalog = catalog if catalog is not None else build_catalog(settings)
        self._prompter = prompter
        self._on_event = on_event
        self._injected_store = store
        self._run: _Run | None = None
        self._injected_checkpoint = checkpoint
        self._resume_board: TaskBoard | None = None
        self._board: TaskBoard | None = None
        self._stalled = False
        self._active = False
        self._finalized = False

    # ── 只读视图 ──────────────────────────────────────────────────────────

    @property
    def catalog(self) -> RoleCatalog:
        """角色目录（``register_role`` 与测试使用；注册顺序保持稳定）。"""
        return self._catalog

    @property
    def checkpoint(self) -> CheckpointStore | None:
        """当前 run 的 checkpoint；``run()`` / ``resume_from()`` 之前为 ``None``。"""
        if self._run is not None:
            return self._run.checkpoint
        return self._injected_checkpoint

    @property
    def stalled(self) -> bool:
        """最近一次 run 是否以停滞（死锁）告终；CLI 据此映射退出码 4。"""
        return self._stalled

    # ── 角色注册（spec §8.3）──────────────────────────────────────────────

    def register_role(self, spec: RoleSpec, cls: type[BaseAgent] | None = None) -> None:
        """注册一个角色：进目录，并（可选）登记专用 Agent 类。

        ``cls`` 写进模块级 :data:`AGENT_CLASSES`，``AgentFactory`` 在 ``get()``
        时现读它 —— 因此注册必须发生在该 key 的首次 ``get`` 之前（引擎在每次
        run 开始时才构造工厂并首次取用），这正是「不碰引擎代码就能加角色」的实现。
        """
        self._catalog.register(spec)
        if cls is not None:
            AGENT_CLASSES[spec.key] = cls

    # ── 入口 ──────────────────────────────────────────────────────────────

    @classmethod
    def resume_from(cls, settings: Settings, run_id: str, **kw: Any) -> Crew:
        """绑定一个既有 run 目录并完成状态修复（spec §13.3 的 1–2 步）。

        ``RUNNING → READY``（产物可能只写了一半）由 :func:`repair_statuses` 完成；
        ``REWORK`` 保持原状（``take_ready`` 本就派发它），``attempts`` 一律不重置。
        ``kw`` 透传给构造函数（如 ``prompter=`` / ``store=`` / ``catalog=``）。
        """
        checkpoint = CheckpointStore(settings.output_dir / run_id)
        checkpoint.load_record()          # schema_version 不匹配在这里就报错
        board = checkpoint.load_board()
        repair_statuses(board)
        crew = cls(settings, checkpoint=checkpoint, **kw)
        crew._resume_board = board
        return crew

    async def run(self, goal: str) -> RunRecord:
        """跑完整流程（规划 → 执行 → 交付），返回落盘的 :class:`RunRecord`。"""
        self._begin()
        try:
            checkpoint = CheckpointStore.create(
                self._settings.output_dir,
                project_goal=goal,
                config_snapshot=self._settings.model_dump(mode="json"),
            )
            return await self._start(checkpoint, goal=goal, resume_board=None)
        finally:
            self._active = False

    async def resume(self) -> RunRecord:
        """续跑 :meth:`resume_from` 绑定的 run：跳过规划与计划卡点，直接重入执行。"""
        resume_board = self._resume_board
        checkpoint = self._injected_checkpoint
        if resume_board is None or checkpoint is None:
            raise RuntimeError(
                "resume() 需要先用 Crew.resume_from(settings, run_id, ...) 构造实例"
            )
        self._begin()
        try:
            return await self._start(checkpoint, goal=None, resume_board=resume_board)
        finally:
            self._active = False

    # ── 流程外壳 ──────────────────────────────────────────────────────────

    def _begin(self) -> None:
        """进入一次 run：拒绝重入并复位本次 run 的观察状态。"""
        if self._active:
            raise RuntimeError("Crew 不可重入：请等待上一次 run()/resume() 返回")
        self._active = True
        self._finalized = False
        self._stalled = False
        self._board = None
        self._run = None

    async def _start(
        self,
        checkpoint: CheckpointStore,
        *,
        goal: str | None,
        resume_board: TaskBoard | None,
    ) -> RunRecord:
        """装配 run 级资源并驱动两个阶段；善后（记录状态、报告、关账）在此收口。"""
        record = checkpoint.load_record()
        ledger = Ledger(
            checkpoint.run_dir / "ledger.jsonl",
            run_id=record.run_id,
            secrets=self._settings.secret_values(),
        )
        store = self._injected_store
        if store is None:
            store = ArtifactStore(
                checkpoint.artifacts_dir, run_id=record.run_id, ledger=ledger
            )
        goal = record.project_goal if goal is None else goal
        models = ModelFactory(
            self._settings, ledger=ledger, fixtures_dir=_fixtures_dir()
        )
        run = _Run(
            goal=goal,
            record=record,
            checkpoint=checkpoint,
            ledger=ledger,
            store=store,
            models=models,
            # 工厂按 run 构造：Agent 绑定 goal 与本次 run 的 store/ledger。
            agents=AgentFactory(models, store, ledger, goal, self._catalog),
            base_stats=record.stats.model_copy(deep=True),
        )
        self._run = run
        if resume_board is not None:
            # carry-forward：进程重启后盘上有文件、内存里没有记录。不重建记录，
            # §9.2 的跨任务冲突检测与返工前的 discard_task 都会失明。
            store.load_manifest(checkpoint.artifacts_dir / "MANIFEST.json")
        ledger.emit("run_started", goal=run.goal)
        try:
            self._validate_config(run)
            if resume_board is not None and record.status is RunStatus.CANCELLED:
                # `main.py cancel <run_id>` 的离线取消标记：直接以 CANCELLED 收尾
                self._board = resume_board
                return self._finalize(run, RunStatus.CANCELLED)
            record.status = RunStatus.RUNNING
            record.updated_at = datetime.now()
            checkpoint.save_record(record)
            if resume_board is None:
                board = await self._plan_phase(run)
            else:
                # 重建而不是直接复用 load_board() 的面板：on_change 回调（每次
                # 状态跃迁即落盘）只在构造函数上挂得进去，而 load_board 建的面板
                # 没有它。缺了它，resume 后 READY→RUNNING 那次 attempts 扣除要等到
                # 下一个完成事件才落盘，硬中断会丢掉这次扣除 —— §13.3 靠「派发即
                # 消耗一次尝试」把「中断→resume→中断」变成有界循环，丢了就不是。
                board = self._build_board(run, resume_board.all())
                self._board = board
                checkpoint.save_tasks(board)   # 修复后的状态立即落盘
            return await self._execute(run, board)
        except asyncio.CancelledError:
            # Ctrl-C（spec §13.4）：落盘为 INTERRUPTED 便于 resume，再原样重抛。
            if not self._finalized:
                self._finalize(run, RunStatus.INTERRUPTED)
            raise
        except PlanFailedError:
            if not self._finalized:
                self._finalize(
                    run, RunStatus.FAILED, exit_code=EXIT_CODES["plan"]
                )
            raise
        except ModelFactoryError:
            if not self._finalized:
                self._finalize(
                    run, RunStatus.FAILED, exit_code=EXIT_CODES["config"]
                )
            raise

    # ── 阶段 0：规划 ──────────────────────────────────────────────────────

    async def _plan_phase(self, run: _Run) -> TaskBoard:
        """拆解 → 校验（含 T-FINAL）→ G3 计划卡点，返回待执行的面板。"""
        tasks = await self._decompose(run)
        board = self._build_board(run, tasks)
        gatekeeper = self._build_gatekeeper(run, board, bus=None)
        decision = await gatekeeper.human_plan_gate(tasks)
        if self._gate_enabled(_PLAN_GATES):
            run.ledger.emit(
                "human_decision",
                checkpoint="plan",
                choice=decision.action,
                task_ids=[task.id for task in tasks],
            )
        if decision.action == "cancel":
            self._board = board
            self._finalize(run, RunStatus.CANCELLED)
            raise RunCancelled("计划卡点取消（cancel），run 结束为 cancelled")
        if decision.action == "save_exit":
            self._board = board
            self._finalize(run, RunStatus.INTERRUPTED)
            raise RunCancelled(
                f"计划已保存（save_exit）：用 resume {run.record.run_id} 续跑"
            )
        if decision.action == "edit":
            board = self._build_board(run, self._accepted_plan(decision.tasks or []))
        # 规划一成功就落盘：进程死在执行开始之前时，resume 仍有面板可续（否则
        # load_board 抛 FileNotFoundError，只能整轮重拆）。
        run.checkpoint.save_tasks(board)
        return board

    async def _decompose(self, run: _Run) -> list[Task]:
        """规划循环：失败就把 ``errors`` 回灌重拆，上限 ``plan_max_attempts``。

        校验的是 ``with_final_task`` 之后的列表：``T-FINAL`` 与 ``DELIVERY.md``
        是引擎追加的保留资源，PM 若也声明，冲突只在追加后可见。
        """
        pm = cast(ProjectManagerAgent, run.agents.get("project_manager"))
        feedback: tuple[str, ...] = ()
        errors: list[str] = []
        attempts = max(1, self._settings.plan_max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                plan = await pm.decompose(run.goal, feedback=feedback)
                tasks = with_final_task(to_tasks(plan))
                validate_plan(tasks, self._catalog)
            except PlanValidationError as exc:
                errors = list(exc.errors)
                feedback = tuple(errors)
                run.ledger.emit("plan_rejected", attempt=attempt, errors=errors)
                continue
            run.ledger.emit(
                "plan_created",
                task_count=len(tasks),
                roles=sorted({task.role for task in tasks}),
                planner_attempts=attempt,
            )
            return tasks
        raise PlanFailedError(errors)

    def _accepted_plan(self, edited: Sequence[Task]) -> list[Task]:
        """G3 编辑结果：重新追加 T-FINAL 并重跑 §7.2 校验（spec §10.3）。"""
        try:
            tasks = with_final_task(edited)
            validate_plan(tasks, self._catalog)
        except PlanValidationError as exc:
            raise PlanFailedError(exc.errors) from exc
        return tasks

    # ── 阶段 1：执行 ──────────────────────────────────────────────────────

    async def _execute(self, run: _Run, board: TaskBoard) -> RunRecord:
        """调度循环 + 交付卡点；交付打回会重建面板并重入循环。

        每轮循环重建 ``Gatekeeper`` / ``Scheduler``：它们绑定面板，而交付打回
        可能整体替换面板（``SUCCEEDED → READY`` 无状态机出边，只能重建）。

        **交付卡点只在终局为 COMPLETED 时打开**（spec §10.3「全部任务 SUCCEEDED、
        封版以前」）。失败 / 停滞的 run 问「接受交付？」既没有意义，又让 ``abort``
        把 FAILED 记成 CANCELLED（退出码 1 被改写成 130，真实结局被掩盖）。
        """
        decisions_path = run.checkpoint.run_dir / "context" / "decisions.jsonl"
        bus = ContextBus(
            run.goal, board, run.store,
            budget_tokens=self._settings.context_budget_tokens,
        )
        # 新 run 下该文件不存在（no-op）；resume 时载入既有决策供上下文包引用。
        bus.load_decisions(decisions_path)
        saved = len(bus.decisions())
        while True:
            self._board = board
            gatekeeper = self._build_gatekeeper(run, board, bus)
            scheduler = Scheduler(
                board,
                run.agents,
                gatekeeper,
                bus,
                ledger=run.ledger,
                checkpoint=run.checkpoint,
                max_concurrency=self._settings.max_concurrency,
                on_event=self._on_event,
            )
            await scheduler.run()
            if scheduler.deadlock_report is not None and _is_stalled(board):
                self._stalled = True
                self._write_stall_report(run, scheduler.deadlock_report)
            saved = self._flush_decisions(run, bus, board, decisions_path, saved)
            self._sync_stats(run)
            outcome = self._final_status(board)
            if outcome is not RunStatus.COMPLETED:
                return self._finalize(run, outcome)
            decision = await gatekeeper.human_delivery_gate(run.record)
            if self._gate_enabled(_DELIVERY_GATES):
                run.ledger.emit(
                    "human_decision",
                    checkpoint="delivery",
                    choice=decision.action,
                    task_ids=list(decision.task_ids),
                )
            if decision.action == "abort":
                return self._finalize(run, RunStatus.CANCELLED)
            if decision.action != "rework" or not decision.task_ids:
                return self._finalize(run, outcome)
            board, replaced = self._apply_rework(run, board, decision.task_ids)
            self._board = board
            if replaced:
                bus = self._clone_bus(run, bus, board)

    def _apply_rework(
        self, run: _Run, board: TaskBoard, task_ids: Sequence[str]
    ) -> tuple[TaskBoard, bool]:
        """交付卡点打回：把指定任务送回调度，返回 ``(面板, 是否重建)``。

        ``FAILED`` 走 ``TaskBoard.reset_failed``（``FAILED`` 无出边的显式人工入口）；
        ``SUCCEEDED`` 也没有出边，只能在快照层人工置回 ``READY``（与 ``reset_failed``
        同类的绕过），并显式留下 ``reason="delivery-rework"`` 的历史 —— 这是引擎
        唯一一处直接改状态的代码，它必须可见。每条打回另留一条 ``layer="human"``
        的评审记录（spec §10.3 的人工留痕）。
        """
        targets = _rework_targets(board, task_ids)
        for task_id in targets:
            if board.get(task_id).status is TaskStatus.FAILED:
                board.reset_failed(task_id)
        succeeded = [
            task_id
            for task_id in targets
            if board.get(task_id).status is TaskStatus.SUCCEEDED
        ]
        if succeeded:
            now = datetime.now()
            tasks = [
                task
                if task.id not in succeeded
                else task.model_copy(
                    update={
                        "status": TaskStatus.READY,
                        "updated_at": now,
                        "status_history": [
                            *task.status_history,
                            StatusChange(
                                from_status=TaskStatus.SUCCEEDED,
                                to_status=TaskStatus.READY,
                                reason="delivery-rework",
                                at=now,
                            ),
                        ],
                    }
                )
                for task in board.all()
            ]
            board = self._build_board(run, tasks)
        _record_human_rework(board, targets)
        # record_review 不触发 on_change（没有 StatusChange）：人工留痕必须自己补落盘。
        run.checkpoint.save_tasks(board)
        return board, bool(succeeded)

    def _flush_decisions(
        self,
        run: _Run,
        bus: ContextBus,
        board: TaskBoard,
        path: Path,
        saved: int,
    ) -> int:
        """把 ``saved`` 之后新增的决策**逐行追加**，返回新的已保存条数。

        ``ContextBus.save_decisions`` 是朴素追加器（给什么写什么，这是正确的
        原语）：把加载进来的全量列表再存一次，会让每条历史决策在文件里出现两遍。
        增量装进一个临时 bus 再交给同一个方法，JSONL 的写法因此只有一处。
        """
        records = bus.decisions()
        fresh = records[saved:]
        if not fresh:
            return saved
        writer = ContextBus(
            run.goal, board, run.store,
            budget_tokens=self._settings.context_budget_tokens,
        )
        for record in fresh:
            writer.add_decision(record)
        writer.save_decisions(path)
        return len(records)

    def _clone_bus(self, run: _Run, bus: ContextBus, board: TaskBoard) -> ContextBus:
        """为重建后的面板克隆一个 bus，并带上既有决策（下游上下文包据此接续）。"""
        clone = ContextBus(
            run.goal, board, run.store,
            budget_tokens=self._settings.context_budget_tokens,
        )
        for record in bus.decisions():
            clone.add_decision(record)
        return clone

    # ── 装配与善后 ────────────────────────────────────────────────────────

    def _build_board(self, run: _Run, tasks: Sequence[Task]) -> TaskBoard:
        """建面板并把每次状态跃迁落盘：``tasks.json`` + ``MANIFEST.json``。

        spec §13.2 要求状态跃迁后立即落盘。MANIFEST 也在这里刷新，而不是只在收尾
        写一次：硬杀（Ctrl-C／断电）之后 resume 靠 ``load_manifest`` 重建产物记录，
        只在收尾写的话，被杀那一刻盘上有文件、清单里一条记录都没有 —— 重建恰好
        在最需要它的时候失明，§9.2 的跨任务冲突检测与返工清理跟着一起失效。
        """
        board: TaskBoard | None = None

        def _on_change(_task: Task, _change: StatusChange) -> None:
            if board is not None:
                run.checkpoint.save_tasks(board)
                run.store.write_manifest()

        board = TaskBoard(tasks, on_change=_on_change)
        return board

    def _build_gatekeeper(
        self, run: _Run, board: TaskBoard, bus: ContextBus | None
    ) -> Gatekeeper:
        """每轮按面板重装门禁编排器；``prompter`` **原样透传**（可为 ``None``）。

        Task 27 的守卫在这里必须原样生效：``gate`` 含人工卡点却没给 prompter 时
        ``Gatekeeper`` 构造期抛 ``ValueError``，Crew **不替调用方兜底**。静默塞一个
        ``NullPrompter`` 会把「忘了接线」重新变成「自动放行」——正是那个守卫要拦的
        事，且在离错误更远的一层，更难看见。两条合法出口都是显式声明：``gate="none"``
        （全自动路径，即 ``--auto``）或显式传入 ``NullPrompter()``。
        """
        reviewer = Reviewer(run.models, self._catalog, run.store, run.ledger)
        return Gatekeeper(
            reviewer,
            run.store,
            board,
            gate=self._settings.gate,
            ledger=run.ledger,
            prompter=self._prompter,
            context_bus=bus,
        )

    def _validate_config(self, run: _Run) -> None:
        """上下文预算必须装得下每个角色**实际绑定**的模型窗口（spec §11.2）。

        逐个别名校验：默认模型窗口再大，也拦得住某个小窗口的本地模型（退出码 3）。
        别名本身非法（缺 ``:``、未知 provider）同样是配置错误，在这里一并收成
        ``ModelFactoryError``，免得它以裸 ``ValueError`` 逃出 CLI 的退出码映射。
        """
        bindings = {
            spec.key: _alias_for(spec, self._settings)
            for spec in self._catalog.all()
        }
        try:
            run.models.validate_context_budget(bindings)
        except ModelFactoryError:
            raise
        except ValueError as exc:
            raise ModelFactoryError(f"模型绑定非法：{exc}") from exc

    def _sync_stats(self, run: _Run) -> None:
        """把面板统计与 LLM 用量写回 ``record.stats``（可重复调用，不重复计数）。

        以 ``base_stats``（本次 run 开始时已落盘的累计，resume 后即上次的账）为
        基线重算：多次调用得到同一个结果，跨 resume 的 token/成本也不会丢。
        """
        board = self._board
        stats = board.stats() if board is not None else RunStats()
        stats.llm_calls = run.base_stats.llm_calls + run.models.llm_calls
        stats.prompt_tokens = (
            run.base_stats.prompt_tokens + run.models.total_prompt_tokens
        )
        stats.completion_tokens = (
            run.base_stats.completion_tokens + run.models.total_completion_tokens
        )
        stats.estimated_cost_usd = (
            run.base_stats.estimated_cost_usd + run.models.total_cost_usd
        )
        stats.started_at = run.base_stats.started_at or run.record.created_at
        stats.finished_at = datetime.now()
        run.record.stats = stats

    def _final_status(self, board: TaskBoard) -> RunStatus:
        """终局判定（spec §7.4）：有失败/阻塞/跳过或未终结即 FAILED。"""
        stats = board.stats()
        if stats.failed or stats.blocked or stats.skipped or not board.is_terminal():
            return RunStatus.FAILED
        if any(task.status is TaskStatus.CANCELLED for task in board.all()):
            return RunStatus.CANCELLED
        return RunStatus.COMPLETED

    def _exit_code(self, status: RunStatus) -> int:
        """状态 → 退出码（spec §7.4）；停滞优先，其次按 run 状态。"""
        if self._stalled:
            return EXIT_CODES["stalled"]
        if status is RunStatus.COMPLETED:
            return EXIT_CODES["ok"]
        if status in (RunStatus.CANCELLED, RunStatus.INTERRUPTED):
            return EXIT_CODES["cancelled"]
        return EXIT_CODES["failed"]

    def _finalize(
        self, run: _Run, status: RunStatus, *, exit_code: int | None = None
    ) -> RunRecord:
        """封版：写状态与统计、锁 MANIFEST、发 ``run_finished``、关账。"""
        self._sync_stats(run)
        record = run.record
        record.status = status
        record.updated_at = datetime.now()
        run.checkpoint.save_record(record)
        if self._board is not None:
            run.checkpoint.save_tasks(self._board)
            run.store.write_manifest()
        started = record.stats.started_at or record.created_at
        code = self._exit_code(status) if exit_code is None else exit_code
        run.ledger.emit(
            "run_finished",
            goal=record.project_goal,
            status=status.value,
            exit_code=code,
            duration_ms=int((datetime.now() - started).total_seconds() * 1000),
        )
        run.ledger.close()
        self._finalized = True
        return record

    def _write_stall_report(self, run: _Run, report: str) -> Path:
        """停滞诊断落 ``diagnostics/stall-report.md``（spec §7.4/§13.1）。"""
        path = run.checkpoint.run_dir / "diagnostics" / "stall-report.md"
        atomic_write_text(
            path,
            f"# 停滞诊断 · {run.record.run_id}\n\n{report}\n",
        )
        return path

    def _gate_enabled(self, modes: frozenset[str]) -> bool:
        """当前 ``gate`` 模式是否包含给定卡点（决定是否记 ``human_decision``）。"""
        return self._settings.gate in modes

    # ── 运行报告（spec §14.3）──────────────────────────────────────────────

    def delivery_summary(self, record: RunRecord) -> str:
        """渲染 §14.3 的中文运行报告文本（任务统计/返工/用量/通过率/耗时/产物）。"""
        board = self._summary_board(record)
        tasks = board.all()
        stats = record.stats
        lines = [
            f"━━━ AgentCrew 运行报告 · {record.run_id} ━━━",
            f"任务      {stats.total_tasks} 个   成功 {stats.succeeded} · "
            f"失败 {stats.failed} · 阻塞 {stats.blocked} · 跳过 {stats.skipped}",
            f"返工      {_rework_line(tasks)}",
            f"LLM 调用  {stats.llm_calls} 次   "
            f"prompt {_format_tokens(stats.prompt_tokens)} tok · "
            f"completion {_format_tokens(stats.completion_tokens)} tok",
            f"估算成本  ${stats.estimated_cost_usd:.2f}",
            f"评审通过率 {_review_line(tasks)}",
            f"耗时      {_elapsed_line(record)}",
            f"产物      {self._artifacts_dir(record)}",
        ]
        failed = [
            task
            for task in tasks
            if task.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
        ]
        if failed:
            lines.append("━━━ 失败任务 ━━━")
            lines.extend(_failure_line(task) for task in failed)
        return "\n".join(lines)

    def _artifacts_dir(self, record: RunRecord) -> Path:
        """产物目录的**绝对**路径（spec §7.4 要求退出时打印它）。"""
        return (self._settings.output_dir / record.run_id / "artifacts").resolve()

    def _summary_board(self, record: RunRecord) -> TaskBoard:
        """报告用的面板：优先本次内存态，否则从 run 目录读 ``tasks.json``。"""
        if (
            self._board is not None
            and self._run is not None
            and self._run.record.run_id == record.run_id
        ):
            return self._board
        return CheckpointStore(self._settings.output_dir / record.run_id).load_board()


# ── 模块级辅助（可单测的纯函数）───────────────────────────────────────────


def _fixtures_dir() -> Path:
    """Mock 夹具根目录：从 ``agentcrew.models.factory`` **现读**。

    不绑成模块级常量：测试与 Task 33 用 monkeypatch 替换
    ``agentcrew.models.factory.DEFAULT_FIXTURES_DIR`` 注入临时夹具。
    """
    return Path(model_factory.DEFAULT_FIXTURES_DIR)


def _rework_targets(board: TaskBoard, task_ids: Sequence[str]) -> list[str]:
    """校验并归一化交付卡点指定的任务 id（去重保序）；不可返工一律 ``ValueError``。

    不存在与「存在但状态不可返工」都必须报错，不能静默跳过：跳过会让操作者以为
    打回生效，下一次交付卡点又把同一批 id 端上来 —— 无限循环，且每次都不说为什么。
    """
    known = board.map()
    unknown = sorted({task_id for task_id in task_ids if task_id not in known})
    if unknown:
        raise ValueError(f"交付卡点指定了不存在的任务 id：{'、'.join(unknown)}")
    targets = list(dict.fromkeys(task_ids))
    unwelcome = [
        (task_id, board.get(task_id).status.value)
        for task_id in targets
        if board.get(task_id).status not in _REWORKABLE_STATUSES
    ]
    if unwelcome:
        detail = "、".join(f"{task_id}（{status}）" for task_id, status in unwelcome)
        raise ValueError(
            "交付卡点只能打回 FAILED / SUCCEEDED 的任务，以下任务不可返工："
            f"{detail}"
        )
    return targets


def _is_stalled(board: TaskBoard) -> bool:
    """调度器退出时是否构成**真**停滞（spec §7.4 的退出码 4）。

    调度器在「无就绪、无在跑」时就留报告，而那条路径同时覆盖两种结局：

    - **失败传播**：任务 ``FAILED`` 后下游全部 ``BLOCKED``。板上仍有非终态
      （``BLOCKED`` 留了人工恢复边），但这是**失败**的结局 —— spec §7.4 明列
      「存在 FAILED / BLOCKED → 1」，此时报死锁会把退出码 1 改写成 4，掩盖真因。
    - **真停滞**：还有任务既没跑完、也没有失败上游把它定死（例如 ``RUNNING``
      却没有对应 worker）。这才是 4。

    判据取「是否还有未定局的任务」，而不是 ``board.is_terminal()``：后者把
    ``BLOCKED`` 当未终结，于是每一次失败传播都会被误判成死锁。
    """
    stats = board.stats()
    if stats.failed or stats.blocked or stats.skipped:
        return False   # 失败传播的结局是退出码 1
    return any(task.status not in _SETTLED_STATUSES for task in board.all())


def _record_human_rework(board: TaskBoard, task_ids: Sequence[str]) -> None:
    """每条人工打回落一条 ``ReviewRecord(layer="human")``（spec §10.3）。

    人工裁决必须完整留痕，且这是 §14.3 报告里「人工驳回」计数的**唯一**来源 ——
    不写它，报告那一行永远是 0，而「永远为零」的一行读起来像证据，比没有更糟。
    ``fix_hints`` 同时是返工任务下次执行时看到的解释（``Scheduler._fix_hints``
    取最近一轮驳回的提示）。``record_review`` 不触发 ``on_change``，调用方负责补落盘。
    """
    now = datetime.now()
    for task_id in task_ids:
        task = board.get(task_id)
        board.record_review(
            task_id,
            ReviewRecord(
                task_id=task_id,
                round=max((review.round for review in task.reviews), default=0) + 1,
                layer="human",
                verdict="reject",
                issues=[_HUMAN_REWORK_ISSUE],
                fix_hints=[_HUMAN_REWORK_HINT],
                reviewer=_HUMAN_REVIEWER,
                created_at=now,
            ),
        )


def _format_tokens(count: int) -> str:
    """token 数（spec §14.3 的 ``812k`` 量级写法）；千位以下给原值。"""
    if count >= 10_000:
        return f"{count / 1000:.0f}k"
    return str(count)


def _format_duration(seconds: float) -> str:
    """耗时：``4m12s`` / ``1h02m03s`` / ``7s``。"""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def _rework_line(tasks: Sequence[Task]) -> str:
    """返工总次数与逐任务明细（``3 次（T-006 ×2, T-009 ×1）``）。"""
    total = sum(task.rework_count for task in tasks)
    reworked = [task for task in tasks if task.rework_count > 0]
    if not reworked:
        return f"{total} 次"
    detail = ", ".join(f"{task.id} ×{task.rework_count}" for task in reworked)
    return f"{total} 次（{detail}）"


def _review_line(tasks: Sequence[Task]) -> str:
    """评审通过率与各层驳回数；没有评审记录时不假装 100%。"""
    reviews = [review for task in tasks for review in task.reviews]
    if not reviews:
        return "—（无评审记录）"
    rejected = [review for review in reviews if review.verdict == "reject"]
    rate = round(100 * (len(reviews) - len(rejected)) / len(reviews))
    g1 = sum(1 for review in rejected if review.layer == "static")
    g2 = sum(1 for review in rejected if review.layer == "llm")
    human = sum(1 for review in rejected if review.layer == "human")
    return f"{rate}%（G1 拦截 {g1} / G2 驳回 {g2} / 人工驳回 {human}）"


def _elapsed_line(record: RunRecord) -> str:
    """耗时：起止都取 ``record.stats``，缺失时回落到记录的创建/更新时间。"""
    stats = record.stats
    start = stats.started_at or record.created_at
    end = stats.finished_at or record.updated_at
    return _format_duration((end - start).total_seconds())


def _failure_line(task: Task) -> str:
    """一行失败任务：``T-011 标题  FAILED  最后一次评审：…``。"""
    last = task.reviews[-1] if task.reviews else None
    if last is None:
        detail = "（无评审记录）"
    elif last.issues:
        detail = "；".join(last.issues)
    else:
        detail = "（无具体问题）"
    return f"{task.id} {task.title}  {task.status.value.upper()}  最后一次评审：{detail}"
