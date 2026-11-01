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
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from agentcrew.agents.base_agent import BaseAgent
from agentcrew.agents.project_manager import ProjectManagerAgent
from agentcrew.agents.registry import AGENT_CLASSES, AgentFactory
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.context.summarizer import summarizer_from_factory
from agentcrew.crew import report
from agentcrew.crew.scheduler import Scheduler
from agentcrew.gate.gatekeeper import Gatekeeper, Prompter, Reviewer
from agentcrew.knowledge.experience import Experience, ExperienceStore
from agentcrew.models import factory as model_factory
from agentcrew.models.cache import LLMCache
from agentcrew.models.complexity import ComplexityCalibrator, set_calibrator
from agentcrew.models.factory import BudgetExhaustedError, ModelFactory, ModelFactoryError
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
    MIN_PLAN_SCORE,
    PlanValidationError,
    score_plan,
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


_OVERRIDE_FILE = "binding_overrides.json"
_TASK_OVERRIDE_FILE = "task_overrides.json"


def _load_overrides(run_dir: Path, filename: str) -> dict[str, dict]:
    """读取落盘的覆盖；没有/损坏都当空（不让坏文件废掉 resume）。"""
    path = Path(run_dir) / filename
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_binding_overrides(run_dir: Path) -> dict[str, dict]:
    return _load_overrides(run_dir, _OVERRIDE_FILE)


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
        capture_experiences: bool = True,
        reuse_knowledge: bool = True,
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

        ``capture_experiences``（P3-5，默认开启）：run 以 COMPLETED 收尾后，把每个
        成功任务的「描述 → 解决方案 / 关键决策 / 产物」沉淀进全局经验库；置 False
        时本次 run 不写任何经验。

        ``reuse_knowledge``（P3-7，默认开启）：规划阶段从全局经验库检索与项目目标
        相关的历史成功经验，注入 PM 拆解上下文；置 False（或经验库为空）时不注入，
        行为与改造前一致。
        """
        self._settings = settings
        self._catalog = catalog if catalog is not None else build_catalog(settings)
        self._prompter = prompter
        self._on_event = on_event
        self._injected_store = store
        self._capture_experiences_enabled = capture_experiences
        self._reuse_knowledge_enabled = reuse_knowledge
        self._run: _Run | None = None
        self._injected_checkpoint = checkpoint
        self._resume_board: TaskBoard | None = None
        self._board: TaskBoard | None = None
        self._stalled = False
        self._active = False
        self._finalized = False
        # P3-1：每次 Crew 构造都装入一个全新的全局校准器 —— 本次 run 的执行结果
        # 据此调整后续任务的复杂度选模。未注入（旧测试直连 ModelFactory）时
        # ``get_calibrator()`` 为 None，工厂走静态评估，行为不变。
        set_calibrator(ComplexityCalibrator())
        #: 外部取消请求（API ``POST /runs/{id}/cancel``）：软取消 —— 不再派发新任务，
        #: 等在跑任务收尾后以 CANCELLED 收尾（退出码 130）。
        self._cancel_event = asyncio.Event()
        #: 当前装配的调度器（每轮交付卡点循环重建）；取消时转发给它停止派发。
        self._current_scheduler: Scheduler | None = None

    # ── 只读视图 ──────────────────────────────────────────────────────────

    @property
    def catalog(self) -> RoleCatalog:
        """角色目录（``register_role`` 与测试使用；注册顺序保持稳定）。"""
        return self._catalog

    def _route_message(self, from_role: str, to_role: str, content: str) -> str:
        """P3-4 多轮对话路由：把某 Agent 发出的 ``<message to="…">`` 转成回复。

        同步、轻量——不真去派发另一个 Agent 的一次完整执行，只给一条「足够让
        提问方继续」的回复：

        - ``to_role == "human"``：本环境无人值守时返回固定占位回复（接了
          ``prompter`` 才真去问人，这里先给占位，接线是显式可选的）；
        - 其他角色：从目录取该角色的一句话职责（title + goal），作为简要答复；
          未知角色也回一条占位文本，不抛错——路由不该因为一个名字而打断任务。

        默认**不**把本方法注入 Agent（``message_router`` 默认 ``None``）；需要
        对话能力时由装配方把 ``functools.partial(self._route_message, from_role=...)``
        传进对应 Agent 构造。
        """
        del from_role, content  # 当前为简化路由，发问方/原文不参与答复
        if to_role == "human":
            return "（无人值守：请按最合理的默认值继续完成本任务。）"
        if not self._catalog.has(to_role):
            return f"（未注册角色 {to_role!r}：按通用做法继续即可。）"
        spec = self._catalog.get(to_role)
        return f"来自{spec.title}的说明：{spec.goal}"

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

    # ── 取消（P1-5：API 软取消）────────────────────────────────────────────

    def cancel(self) -> None:
        """请求取消本次 run（``POST /runs/{id}/cancel``）。

        软取消：置本实例的取消标志，并转发给当前调度器停止派发新任务；在跑的任务
        自然收尾后，``_execute`` 以 ``RunStatus.CANCELLED`` 封版（退出码 130）。
        可重复调用；run 尚未进入执行段（规划中）时也安全 —— 执行段装配调度器时会
        立刻看到已置位的标志。
        """
        self._cancel_event.set()
        scheduler = self._current_scheduler
        if scheduler is not None:
            scheduler.cancel(hard=False)

    # ── P1-6：运行时切换模型 ───────────────────────────────────────────────

    def current_model_bindings(self) -> dict[str, Any]:
        """当前生效的模型绑定（默认 + 角色级）；run 未开始时抛 ``RuntimeError``。"""
        if self._run is None:
            raise RuntimeError("当前 run 尚未装配 ModelFactory，请在 run 开始后调用")
        return self._run.models.current_bindings()

    def switch_model(self, alias: str, *, role: str | None = None) -> dict[str, Any]:
        """运行时切换模型绑定（API / CLI 菜单 / Dashboard 共用）。

        ``role=None`` 切默认模型；指定 ``role`` 切角色级绑定。委托本次 run 的
        :class:`ModelFactory` 改配置 + 清 provider 缓存，并把新配置快照回写
        ``run.json``（resume 的配置差异行因此看到切换后的口径）。记 ``model_switched``
        事件（含 from/to/role）。返回切换后的绑定。
        """
        if self._run is None:
            raise RuntimeError("当前 run 尚未装配 ModelFactory，请在 run 开始后调用")
        models = self._run.models
        from_alias: str | None
        if role is None:
            from_alias = models.settings.default_model
            models.set_default_alias(alias)
        else:
            from_alias = models.settings.role_bindings.get(role)
            models.set_role_binding(role, alias)
        # 让 Crew 自己持有的 settings 与工厂同步（_validate_config / _gate_enabled
        # 仍读 self._settings；Agent 经 factory.settings 读同一份新配置）。
        self._settings = models.settings
        record = self._run.record
        record.config_snapshot = models.settings.config_snapshot()
        self._run.checkpoint.save_record(record)
        self._run.ledger.emit(
            "model_switched",
            from_alias=from_alias, to_alias=alias, role=role,
        )
        return models.current_bindings()

    # ── 角色注册（spec §8.3）──────────────────────────────────────────────

    def switch_agent(self, role: str, profile: str, *, reason: str = "") -> list:
        """运行中把某角色的 primary 换成另一个已解析的执行体。

        生效点是**下一次派发**：在途任务继续用旧 agent 跑完（中断等于白烧 token）。
        切换结果落 ``<run_dir>/binding_overrides.json``，resume 时自动恢复。
        """
        if self._run is None:
            raise RuntimeError("当前没有进行中的 run，无法切换 agent")
        members = self._run.agents.switch_agent(role, profile, reason=reason)
        self._persist_overrides()
        return members

    def reset_agent(self, role: str) -> tuple[list, bool]:
        """撤销 ``switch_agent`` 的覆盖，回到配置文件里的绑定。

        幂等：没切过也返回 ``(当前成员, False)``。落盘文件在覆盖清空后删除，
        这样 resume 时 ``_load_binding_overrides`` 读到的是"没有覆盖"。
        """
        if self._run is None:
            raise RuntimeError("当前没有进行中的 run，无法恢复绑定")
        members, changed = self._run.agents.reset_agent(role)
        self._persist_overrides()
        return members, changed

    def task_role(self, task_id: str) -> str:
        """查任务所属角色；任务不在板上时返回空串。

        API 的「任务级换人」需要 role 做权限/别名解析，而调用方通常只知道
        任务 id——服务端的 RunState 只记录 ``task_transition``，不一定覆盖到
        每个任务，所以以**板上真实任务**为准。
        """
        if self._board is None:
            return ""
        try:
            task = self._board.get(task_id)
        except KeyError:
            return ""
        return str(getattr(task, "role", "") or "")

    def set_task_agent(
        self, task_id: str, profile: str, *, role: str = "", reason: str = ""
    ) -> Any:
        """只给**这一个任务**换执行体；其他任务与角色绑定不受影响。

        幂等覆盖：同一任务再设一次就是改目标。落盘到 ``task_overrides.json``，
        resume 后仍生效（任务还没跑完就重启的场景）。
        """
        if self._run is None:
            raise RuntimeError("当前没有进行中的 run，无法设置任务级执行体")
        if not role:
            raise ValueError("需要 role（任务所属角色）才能校验执行体")
        member = self._run.agents.set_task_override(
            role, task_id, profile, reason=reason)
        self._persist_task_overrides()
        return member

    def reset_task_agent(self, task_id: str) -> tuple[str, bool]:
        """撤销任务级覆盖（幂等）。"""
        if self._run is None:
            raise RuntimeError("当前没有进行中的 run，无法恢复任务级执行体")
        previous, changed = self._run.agents.reset_task_override(task_id)
        self._persist_task_overrides()
        return previous, changed

    def _persist_task_overrides(self) -> None:
        if self._run is None:
            return
        run_dir = self._run.checkpoint.artifacts_dir.parent
        path = run_dir / _TASK_OVERRIDE_FILE
        payload = self._run.agents.export_task_overrides()
        if payload:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        elif path.is_file():
            path.unlink()

    def _persist_overrides(self) -> None:
        """把运行中覆盖写进 run 目录；空则删文件（避免"空对象"被当成有覆盖）。"""
        if self._run is None:
            return
        run_dir = self._run.checkpoint.artifacts_dir.parent
        path = run_dir / _OVERRIDE_FILE
        payload = self._run.agents.export_bindings()
        if payload:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        elif path.is_file():
            path.unlink()

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
                config_snapshot=self._settings.config_snapshot(),
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
        # P3-2：按配置决定是否给工厂挂响应缓存（默认 None = 不缓存，向后兼容）。
        resp_cache = LLMCache() if self._settings.llm_cache_enabled else None
        models = ModelFactory(
            self._settings, ledger=ledger, fixtures_dir=_fixtures_dir(),
            cache=resp_cache,
        )
        # 执行体级 run 累计成本：resume 后把上一段的花费灌回来，
        # 否则额度在重启瞬间清零，"整场上限"就名存实亡。
        for profile, usage in (record.stats.agent_costs or {}).items():
            models.seed_agent_cost(profile, float(usage.get("cost_usd", 0.0)))
        run = _Run(
            goal=goal,
            record=record,
            checkpoint=checkpoint,
            ledger=ledger,
            store=store,
            models=models,
            # 工厂按 run 构造：Agent 绑定 goal 与本次 run 的 store/ledger。
            agents=AgentFactory(
                models, store, ledger, goal, self._catalog,
                settings=self._settings, run_id=record.run_id),
            base_stats=record.stats.model_copy(deep=True),
        )
        self._run = run
        # 多 agent：启动期解析编排计划并落事件，配置错误在此 fail fast（退出码 3）。
        # 运行中切换的落盘结果：resume 时先恢复，再解析计划（顺序不能反）
        run.agents.import_bindings(_load_binding_overrides(
            checkpoint.artifacts_dir.parent))
        run.agents.import_task_overrides(_load_overrides(
            checkpoint.artifacts_dir.parent, _TASK_OVERRIDE_FILE))
        _plan = run.agents.plan()
        if _plan is not None:
            ledger.emit("run_plan_resolved", run_id=record.run_id,
                        agents=_plan.to_snapshot())
        if resume_board is not None:
            # carry-forward：进程重启后盘上有文件、内存里没有记录。不重建记录，
            # §9.2 的跨任务冲突检测与返工前的 discard_task 都会失明。
            store.load_manifest(checkpoint.artifacts_dir / "MANIFEST.json")
        # P1-5：把 run 开始（含 run_id）推给 ``on_event`` 观察者 —— API 服务器据此
        # 把后台任务与 HTTP 响应里的 ``run_id`` 对上；终端看板同理提前开版。
        if self._on_event is not None:
            self._on_event("run_started", {
                "run_id": record.run_id,
                "goal": run.goal,
            })
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
            # 规划开始前已收到取消（API 软取消在 run 落盘后到达）：直接以 CANCELLED
            # 收尾，不烧一轮规划 token。
            if self._cancel_event.is_set():
                return self._finalize(run, RunStatus.CANCELLED)
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
        except BudgetExhaustedError as exc:
            # P0-3：成本预算耗尽 —— 调度器已停止派发、等在跑任务收尾。
            # 记 budget_exhausted 事件（含当前累计值与上限），以 FAILED 收尾，退出码 1。
            run.ledger.emit(
                "budget_exhausted",
                kind=exc.kind,
                current=exc.current,
                limit=exc.limit,
                total_cost_usd=run.models.total_cost_usd,
                llm_calls=run.models.llm_calls,
            )
            if not self._finalized:
                self._finalize(
                    run, RunStatus.FAILED, exit_code=EXIT_CODES["failed"]
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
            edited_tasks = self._accepted_plan(decision.tasks or [])
            # P2-7：增量编辑（添加/删除/修改）逐条留痕；全量编辑器无此记录。
            for change in decision.incremental_edits:
                run.ledger.emit("plan_incremental_edit", **change)
            board = self._build_board(run, edited_tasks)
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
        # P3-7：整个规划循环共享同一份历史经验参考（与具体 attempt 无关）。
        context_hint = self._prepare_knowledge_context(run)
        attempts = max(1, self._settings.plan_max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                # context_hint 为空（未开启复用 / 经验库无命中）时走原签名调用，
                # 与改造前逐字一致——连 kwargs 都不多传，兼容只接受 (goal, feedback)
                # 的旧 spy / 子类。
                if context_hint:
                    plan = await pm.decompose(
                        run.goal, feedback=feedback, context_hint=context_hint
                    )
                else:
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
            # P2-6：校验通过后做质量评分。低分只警告不阻断 —— 二元校验已放行，
            # 评分是观测信号：回灌 PM 重拆的额度已用尽再拦，只会把「能用的计划」
            # 退成 PlanFailedError。
            score = score_plan(tasks, run.goal)
            run.ledger.emit(
                "plan_scored",
                total=round(score.total, 4),
                coverage=round(score.coverage, 4),
                specificity=round(score.specificity, 4),
                dependency_quality=round(score.dependency_quality, 4),
                deliverable_clarity=round(score.deliverable_clarity, 4),
            )
            if score.total < MIN_PLAN_SCORE:
                run.ledger.emit(
                    "plan_low_quality",
                    total=round(score.total, 4),
                    min_score=MIN_PLAN_SCORE,
                    coverage=round(score.coverage, 4),
                    specificity=round(score.specificity, 4),
                    dependency_quality=round(score.dependency_quality, 4),
                    deliverable_clarity=round(score.deliverable_clarity, 4),
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
            summarizer=summarizer_from_factory(run.models),
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
                on_checkpoint=lambda: self._persist_record(run),
            )
            # P1-5：持有当前调度器供外部 ``cancel()`` 转发；若取消请求已在路上
            # （规划阶段到达），新装配的调度器立刻停止派发。
            self._current_scheduler = scheduler
            if self._cancel_event.is_set():
                scheduler.cancel(hard=False)
            try:
                await scheduler.run()
            finally:
                self._current_scheduler = None
            if self._cancel_event.is_set():
                # 软取消收尾：在跑任务已自然结束，不再开交付卡点，直接 CANCELLED。
                return self._finalize(run, RunStatus.CANCELLED)
            if scheduler.stalled:
                # P2-2：调度器判定「有任务在跑却超时无进展」并已硬取消在跑 worker。
                # 置停滞标志（_exit_code 据此映射 4），记 run 级事件并落诊断报告。
                # 详细的 stalled_for_seconds / active_tasks 已由调度器在 run_stalled
                # 事件里落过，这里只补一条 run 级收口事件。
                self._stalled = True
                run.ledger.emit("run_stalled", kind="timeout")
                self._write_stall_report(
                    run,
                    scheduler.deadlock_report
                    or "运行停滞：超过阈值仍无任务状态变化，在跑任务已被取消",
                )
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
                # P3-5：全部任务 SUCCEEDED 且交付卡点放行 —— run 真的成功了，
                # 此刻才沉淀经验（失败 / 取消 / 停滞的 run 不进经验库）。
                self._capture_experiences(run, board, bus)
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
            summarizer=summarizer_from_factory(run.models),
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
            review_score_threshold=self._settings.review_score_threshold,
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

    def _sync_stats(self, run: _Run, *, finished: bool = True) -> None:
        """把面板统计与 LLM 用量写回 ``record.stats``（可重复调用，不重复计数）。

        以 ``base_stats``（本次 run 开始时已落盘的累计，resume 后即上次的账）为
        基线重算：多次调用得到同一个结果，跨 resume 的 token/成本也不会丢。

        ``finished=False`` 用于**周期性**落盘（:meth:`_persist_record`）：那次写盘
        不代表 run 结束，``finished_at`` 必须保持原值（None 或上一次收尾的时刻），
        否则一份跑到一半的 run.json 会声称自己「已经结束」，而 ``status`` 与报告
        正是从这份记录里读耗时的。
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
        merged: dict[str, dict[str, float]] = {
            k: dict(v) for k, v in run.base_stats.agent_costs.items()}
        for profile, usage in run.models.agent_usage.items():
            slot = merged.setdefault(profile, {
                "calls": 0.0, "cost_usd": 0.0,
                "prompt_tokens": 0.0, "completion_tokens": 0.0})
            for key, value in usage.items():
                slot[key] = slot.get(key, 0.0) + value
        stats.agent_costs = merged
        stats.started_at = run.base_stats.started_at or run.record.created_at
        stats.finished_at = (
            datetime.now() if finished else run.record.stats.finished_at)
        run.record.stats = stats

    def _persist_record(self, run: _Run) -> None:
        """周期性把 ``run.json`` 的统计同步落盘（``Scheduler.on_checkpoint``，I4）。

        与 ``tasks.json`` 同一个节拍写：两条记录必须描述同一个瞬间，只写其中一条
        会让盘上出现「任务都跑完了、统计还是 0」这种自相矛盾的快照。统计走
        ``_sync_stats``（可重复调用、以 ``base_stats`` 为基线，不重复计数），因此
        每次落盘的都是一份完整自洽的账；硬杀最多丢一个完成事件的开销。
        """
        self._sync_stats(run, finished=False)
        run.record.updated_at = datetime.now()
        run.checkpoint.save_record(run.record)

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

    def _prepare_knowledge_context(self, run: _Run) -> str:
        """P3-7：规划前从全局经验库检索与目标相关的历史经验，渲染成 PM 上下文。

        开关关闭（``reuse_knowledge=False``）或经验库无命中时返回空串——PM 收到
        空 ``context_hint``，拆解提示词与改造前逐字一致（向后兼容）。有命中时记
        一条 ``knowledge_reused`` 事件（含 goal 与命中条数），与经验沉淀对称留痕。
        """
        if not self._reuse_knowledge_enabled:
            return ""
        store = ExperienceStore(self._settings.output_dir)
        matched = store.query(run.goal, limit=3)
        if not matched:
            return ""
        run.ledger.emit(
            "knowledge_reused",
            goal=run.goal,
            matched_experiences=len(matched),
        )
        return store.inject_context(run.goal, limit=3)

    def _capture_experiences(
        self, run: _Run, board: TaskBoard, bus: ContextBus
    ) -> None:
        """P3-5：run 成功（COMPLETED）后把成功任务沉淀进全局经验库。

        只沉淀 ``SUCCEEDED`` 任务——失败 / 取消 / 被跳过的任务不进经验库（它们的
        「解决方案」不成立）。每条经验落盘后记一条 ``experience_captured`` 事件
        （含 task_id / experience_id），与 Ledger 的「诚实留痕」一致。开关关闭时
        （``capture_experiences=False``）整个方法零副作用。
        """
        if not self._capture_experiences_enabled:
            return
        extracted = _extract_experiences(
            board, bus, store=run.store, run_id=run.record.run_id
        )
        if not extracted:
            return
        knowledge = ExperienceStore(self._settings.output_dir)
        for task_id, exp in extracted:
            knowledge.add(exp)
            run.ledger.emit(
                "experience_captured",
                task_id=task_id,
                experience_id=exp.id,
                role=exp.role,
            )

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
        # P1-5：同一事件同步推给 ``on_event`` 观察者（API SSE / 终端看板），
        # 与 ledger 里的字段同形 —— 实时流不用等落盘就能看到 run 结局。
        if self._on_event is not None:
            self._on_event("run_finished", {
                "run_id": record.run_id,
                "status": status.value,
                "exit_code": code,
            })
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
        """渲染 §14.3 的中文运行报告文本（任务统计/返工/用量/通过率/耗时/产物）。

        措辞与排版住在 :mod:`agentcrew.crew.report`（纯函数）；这里只负责提供
        两个上下文：报告用的面板（内存态或从 run 目录重建）与产物的绝对路径。
        """
        return report.delivery_summary(
            record,
            board=self._summary_board(record),
            artifacts_dir=self._artifacts_dir(record),
        )

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


def _extract_experiences(
    board: TaskBoard,
    bus: ContextBus,
    *,
    store: ArtifactStore,
    run_id: str,
) -> list[tuple[str, Experience]]:
    """从面板里提取成功任务的经验（P3-5 的纯逻辑，可单测）。

    只收 ``SUCCEEDED`` 任务；``solution_summary`` 取 Agent 自述 summary，缺则回落
    notes 前 200 字；``artifacts`` 取该任务实际落盘的产物路径；``key_decisions``
    取 ContextBus 里 task_id 命中的决策文本。返回 ``(task_id, Experience)`` 对——
    task_id 不在 :class:`Experience` 的字段里（它描述的是「任务」而非 run 内定位），
    但 ledger 事件要它，故成对返回。
    """
    decisions_by_task: dict[str, list[str]] = {}
    for record in bus.decisions():
        decisions_by_task.setdefault(record.task_id, []).append(record.decision)
    extracted: list[tuple[str, Experience]] = []
    for task in board.all():
        if task.status is not TaskStatus.SUCCEEDED:
            continue
        result = task.result
        if result is not None:
            summary = (result.summary or result.notes)[:200]
        else:
            summary = ""
        artifacts = sorted(record.path for record in store.by_task(task.id))
        exp = Experience(
            id=uuid4().hex,
            task_description=task.description,
            role=task.role,
            solution_summary=summary,
            artifacts=artifacts,
            key_decisions=decisions_by_task.get(task.id, []),
            success=True,
            run_id=run_id,
            created_at=datetime.now(),
        )
        extracted.append((task.id, exp))
    return extracted


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


# ── 报告格式化（实现已搬到 crew/report.py，此处保留旧名字）──────────────
#
# 兼容层：格式化逻辑现在住在 :mod:`agentcrew.crew.report`，本模块只做再导出，
# 既有的调用点与测试不受影响。

_format_tokens = report.format_tokens
_format_duration = report.format_duration
_rework_line = report.rework_line
_review_line = report.review_line
_elapsed_line = report.elapsed_line
_failure_line = report.failure_line
