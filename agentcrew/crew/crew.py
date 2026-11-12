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
import base64
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

from agentcrew.agents.base_agent import BaseAgent
from agentcrew.agents.peer import PeerRouter
from agentcrew.agents.project_manager import ProjectManagerAgent
from agentcrew.agents.registry import AGENT_CLASSES, AgentFactory
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.context.summarizer import summarizer_from_factory
from agentcrew.crew import report
from agentcrew.crew.scheduler import Scheduler
from agentcrew.gate.execute_verify import ExecutionVerifier
from agentcrew.gate.gatekeeper import Gatekeeper, Prompter, Reviewer
from agentcrew.gate.reassign import ReassignPolicy
from agentcrew.gate.stepper import StepGate
from agentcrew.gate.supervisor import SupervisorReviewer
from agentcrew.knowledge.entity import (
    EntityMemoryStore,
    EntityRecord,
    entities_from_experience,
)
from agentcrew.knowledge.experience import Experience, ExperienceStore
from agentcrew.mcp.adapter import adapt_tools
from agentcrew.mcp.client import MCPClient
from agentcrew.models import factory as model_factory
from agentcrew.models.cache import LLMCache
from agentcrew.models.complexity import ComplexityCalibrator
from agentcrew.models.factory import BudgetExhaustedError, ModelFactory, ModelFactoryError
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.approvals import (
    ApprovalPending,
    ApprovalRequest,
    ApprovalStore,
)
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunRecord,
    RunStatus,
    is_stale_running,
    repair_statuses,
)
from agentcrew.runtime.ledger import Ledger
from agentcrew.runtime.lifecycle import (
    LIVENESS_STALE_AFTER,
    ResumeLock,
    RunHeartbeat,
    read_heartbeat,
)
from agentcrew.runtime.superstep import SuperStepJournal
from agentcrew.runtime.webhook import (
    SPOOL_DIR_NAME,
    WebhookConfig,
    WebhookDispatcher,
    WebhookTarget,
)
from agentcrew.task.conditions import is_conditional_skip
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
from agentcrew.tools import (
    GrepTool,
    ListFilesTool,
    ReadFileTool,
    RunPythonTool,
    RunTestsTool,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
)
from agentcrew.tools.guard import build_tool_guard
from agentcrew.tools.registry import ToolRegistry
from agentcrew.tools.sandbox import ExecutionBackend, build_backend
from agentcrew.tools.search import build_search_provider
from agentcrew.utils.clock import elapsed_since, seconds_between, utc_now
from agentcrew.utils.files import atomic_write_text

_logger = logging.getLogger(__name__)

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

#: Ledger observer 转发到实时通道（SSE）时应跳过的事件名。这些事件在
#: ``ledger.emit`` 之外已经通过 ``_dispatch_event`` / ``scheduler._notify`` 推过
#: 一次，若 observer 再推会在 SSE 重复；``agent_stream_chunk`` 是逐块高频事件，
#: 正文已由 ``agent_stream_delta`` 单独推送，不该再进 SSE 通道。
_LEDGER_SSE_EXCLUDED: frozenset[str] = frozenset({
    "run_started",
    "run_paused",
    "approval_requested",
    "run_finished",
    "run_stalled",
    "agent_stream_chunk",
})

#: 流式正文落盘目录名（``output/<run>/streams/``）与单文件触发重写的字节阈值 /
#: 保留字符数。正文是模型输出、体积不可控，落盘只为「刷新后回溯」这一件展示用途，
#: 不是审计事实（审计走 ledger 的 ``agent_stream_chunk.delta_length``）；超阈值时
#: 读文本、按字符截断保留尾部（用户回看最关心最新内容），不会切坏多字节字符。
_STREAM_DIR = "streams"
_STREAM_MAX_BYTES = 400_000
_STREAM_KEEP_CHARS = 50_000


def _append_stream(streams_dir: Path, task_id: str, delta: str) -> None:
    """把一帧流式正文追加到 ``streams_dir/<sanitized_task_id>.md``。

    - 追加写、无 fsync：正文是瞬态展示辅助，逐帧 fsync 会拖慢流式，崩溃丢最后一小段可接受。
    - ``task_id`` 消毒：只保留 ``[A-Za-z0-9._-]``，其余替换为 ``_``，防路径越界。
    - 写失败（磁盘满/权限）只降级不抛：正文回溯是锦上添花，不能让流式 run 因此崩掉。
    """
    if not delta:
        return
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in task_id) or "task"
    path = streams_dir / f"{safe}.md"
    try:
        streams_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as file:
            file.write(delta)
        if path.stat().st_size > _STREAM_MAX_BYTES:
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text) > _STREAM_KEEP_CHARS:
                path.write_text(text[-_STREAM_KEEP_CHARS:], encoding="utf-8")
    except OSError:
        pass


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
    journal: SuperStepJournal
    """P2-2：super-step 标记 + pending tool writes 的读写器（绑定本 run 目录）。"""

    tracer: Any | None = None
    """P2-3：OTel tracer（``None`` = 未启用）。run 根 span 由 Crew 持有并收尾。"""


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
        tool_registry: ToolRegistry | None = None,
        entity_memory_enabled: bool | None = None,
        preset_tasks: Sequence[Any] | None = None,
        images: Sequence[Mapping[str, Any]] | None = None,
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

        R3 新增两个**默认关闭**的能力入口：

        ``tool_registry``：显式注入一份 run 级工具注册表（优先级高于配置）。不传
        时看 ``settings.tools_enabled`` —— 关闭则 ``AgentFactory`` 收不到注册表，
        Agent 走「单次调用」原路径；开启则 Crew 自动装配 6 个内置工具并绑定
        ``tool_permissions``。库调用方可用这个参数绕过配置直接跑带工具的 run。

        R28：``preset_tasks`` 给定时**跳过 PM 规划调用**，直接用它作为任务表
        （仍走同一套 ``with_final_task`` / ``validate_plan`` / 评分与留痕）——
        这是「从画布起跑」的通道：人画好的流程不该再让模型重拆一遍。校验失败
        直接 ``PlanFailedError``，不做「回灌重拆」：流程是人写的，重试没有意义。

        ``entity_memory_enabled``：``None``（默认）时取
        ``settings.entity_memory_enabled``；显式传 True/False 可覆盖。开启后经验
        沉淀时同步抽取实体、规划阶段注入实体参考块。
        """
        self._settings = settings
        self._catalog = catalog if catalog is not None else build_catalog(settings)
        self._prompter = prompter
        self._on_event = on_event
        #: R12：出站 Webhook 分发器；``None`` = 未配置 URL（零开销）。
        self._webhook: WebhookDispatcher | None = None
        #: R17：本次 run 的心跳线程；正常/异常收尾都会停掉并删掉心跳文件。
        self._heartbeat: RunHeartbeat | None = None
        #: R13：本次 run 建立的 MCP 连接（收尾时逐个 stop，否则子进程泄漏）。
        self._mcp_clients: list[MCPClient] = []
        self._injected_store = store
        self._capture_experiences_enabled = capture_experiences
        #: R28：人给的流程任务（None = 正常走 PM 规划）。
        self._preset_tasks = list(preset_tasks) if preset_tasks else None
        self._reuse_knowledge_enabled = reuse_knowledge
        #: R3：显式注入的工具注册表优先于配置自动装配（None = 看配置）。
        self._tool_registry_override = tool_registry
        #: R4：resume 时若 run 挂在人工审批上，记住是哪个卡点（plan/delivery）。
        #: ``None`` = 普通 resume，保持 §13.3「跳过规划与计划卡点」的原语义。
        self._resume_checkpoint: str | None = None
        self._entity_memory_enabled = (
            settings.entity_memory_enabled
            if entity_memory_enabled is None
            else entity_memory_enabled
        )
        self._run: _Run | None = None
        self._injected_checkpoint = checkpoint
        self._resume_board: TaskBoard | None = None
        self._board: TaskBoard | None = None
        self._stalled = False
        self._active = False
        self._finalized = False
        #: C2：执行后端惰性构造一次（docker 模式下探测也要花一次进程调用）。
        self._sandbox_backend: ExecutionBackend | None = None
        #: C4：随 run 附带的图片（多模态输入）。空 = 纯文本路径，零开销。
        self._images: list[dict[str, Any]] = [
            dict(item) for item in (images or [])]
        #: C5：文档 RAG 索引（``docs_rag_enabled`` 开启时在 run 启动期摄取）。
        self._docs_store: Any | None = None
        # P3-1（2026-11 会话隔离改造）：校准器从「模块级全局单例」改为 **Crew
        # 实例属性**，构造 ModelFactory 时显式注入。原实现每次构造 Crew 都调用
        # ``set_calibrator(ComplexityCalibrator())`` 全量替换全局单例——API 服务器
        # 对每个 POST /runs 并发新建 Crew，后启动的 run 会当场清空先启动 run 已
        # 积累的校准分桶，两个 run 的执行结局还会互相污染对方的模型选型。改为
        # 实例注入后各 run 完全隔离；直连 ModelFactory 的旧测试仍可用全局
        # ``set_calibrator``（工厂在未注入实例时回退读全局，行为兼容）。
        self._calibrator = ComplexityCalibrator()
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
        record = checkpoint.load_record()  # schema_version 不匹配在这里就报错
        # R22：resume 状态白名单（六层审计遗留——R17 只补了进程锁这一半）。
        # 拒绝只有两种：completed（终局成功，续跑没有意义——成功不是崩溃产物，
        # 不会需要恢复）；running 且心跳文件新鲜（R17+ 的活进程每 30s 写心跳，
        # 这是「正被另一进程跑着」的明确信号，再 resume 会双写坏 checkpoint）。
        # 其余全部放行——**failed 也是恢复路径**：未处理异常的兜底封版会把
        # 「崩溃当场」记成 FAILED，拒了它就堵死崩溃恢复；无心跳文件的老 run
        # （R17 之前 / 心跳写失败）无法区分死活，保留既有行为，与 reconcile
        # 的 24h 判据口径一致。
        if record.status is RunStatus.COMPLETED:
            raise ValueError(
                f"run {run_id!r} 已终局（completed），无需续跑；"
                "要复用结果可直接读产物或用 diff 对比"
            )
        if record.status is RunStatus.RUNNING:
            heartbeat = read_heartbeat(checkpoint.run_dir)
            # A-7：心跳现在是 aware UTC，naive 的 datetime.now() 直接相减会抛
            # TypeError。统一走 elapsed_since：返回 None 表示时钟倒挂超容忍，
            # 此时无法证明进程已死 → 与 A-6 同方向 fail-closed（拒绝 resume）。
            idle = elapsed_since(heartbeat)
            if heartbeat is not None and (idle is None or idle <= LIVENESS_STALE_AFTER):
                raise ValueError(
                    f"run {run_id!r} 正在另一进程运行（心跳新鲜）；"
                    "若是强杀留下的孤儿，等心跳过期 15 分钟后 resume，"
                    "或由 CLI 启动 reconcile 收敛为 interrupted"
                )
            # A-6：心跳缺失时**fail-closed**。此前 ``heartbeat is None``
            # （心跳文件被清 / 写失败）直接放行——但活跃进程的心跳写失败恰恰
            # 是最危险的窗口：放行 = 两个进程同时写同一 run 的 checkpoint。
            # 只有超出 24h 僵尸判据（``is_stale_running``）才认定为死 run 放行；
            # run 更新时间尚新则拒绝，宁可让人显式 reconcile 也不冒双写风险。
            if heartbeat is None and not is_stale_running(checkpoint.run_dir):
                raise ValueError(
                    f"run {run_id!r} 状态为 RUNNING 但无心跳且最近仍在更新，"
                    "可能是另一进程心跳写失败；为避免双写，请确认没有别的进程在"
                    "跑它，或由 CLI 启动 reconcile 收敛为 interrupted 后再 resume"
                )
        board = checkpoint.load_board()
        repair_statuses(board)
        # R20：断点开关属于 run 而不属于进程——首跑带着 debug_step 挂起的 run，
        # resume 时不重供 --debug-step 也必须继续逐任务断点，否则断点会被静默
        # 丢掉、审批单永远 pending。用户想提前放行用 `approve --decision run`。
        if (record.config_snapshot or {}).get("debug_step"):
            settings = settings.model_copy(update={"debug_step": True})
        crew = cls(settings, checkpoint=checkpoint, **kw)
        crew._resume_board = board
        if record.status is RunStatus.AWAITING_APPROVAL:
            # R4：审批挂起的 run 必须回到产生审批单的那个卡点继续，而不是按
            # §13.3 的老规则直接重入执行（那样会把审批整个绕过去）。
            resumed_at = ApprovalStore(checkpoint.run_dir).awaiting_checkpoint()
            if resumed_at is None:
                raise ValueError(
                    f"run {run_id!r} 标记为 awaiting_approval，但找不到未消费的"
                    "审批单（approvals.jsonl 缺失或损坏）；拒绝绕过人工审批续跑"
                )
            crew._resume_checkpoint = resumed_at
        return crew

    async def run(self, goal: str) -> RunRecord:
        """跑完整流程（规划 → 执行 → 交付），返回落盘的 :class:`RunRecord`。"""
        self._begin()
        lock: ResumeLock | None = None
        try:
            checkpoint = CheckpointStore.create(
                self._settings.output_dir,
                project_goal=goal,
                config_snapshot=self._settings.config_snapshot(),
            )
            # A-6：``run`` 与 ``resume`` 一样占进程锁——此前只有 resume 占锁，
            # 而 run 首跑阶段的 checkpoint 写入窗口同样不能与另一个进程的
            # resume 交错（acquire 失败时 release 是幂等空操作）。
            lock = ResumeLock(checkpoint.run_dir)
            lock.acquire()
            return await self._start(checkpoint, goal=goal, resume_board=None)
        finally:
            if lock is not None:
                lock.release()
            # R17：先停心跳，异常路径也一定走到（进程强杀则文件留作孤儿证据）。
            self._stop_heartbeat()
            # R13：先关 MCP 连接——异常路径不走 ``_finalize``，不在这里关就会
            # 留下孤儿子进程。
            await self._close_mcp()
            # R12：兜底排空 Webhook（``_finalize`` 已排空时这里是幂等空操作）。
            # 异常路径不走 ``_finalize``，没有这一步则最后一批通知直接丢。
            self._close_webhook()
            # P2-3：trace 导出放在这里——异步上下文里走 ``to_thread``，不卡事件循环。
            await self._flush_telemetry()
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
        # R17：占锁必须覆盖整个 _start；acquire 失败时 release 是幂等空操作，
        # 绝不会误删别的进程持有的锁。
        lock = ResumeLock(checkpoint.run_dir)
        try:
            lock.acquire()
            return await self._start(checkpoint, goal=None, resume_board=resume_board)
        finally:
            lock.release()
            # R17：先停心跳，异常路径也一定走到（进程强杀则文件留作孤儿证据）。
            self._stop_heartbeat()
            # R13：先关 MCP 连接——异常路径不走 ``_finalize``，不在这里关就会
            # 留下孤儿子进程。
            await self._close_mcp()
            # R12：兜底排空 Webhook（``_finalize`` 已排空时这里是幂等空操作）。
            # 异常路径不走 ``_finalize``，没有这一步则最后一批通知直接丢。
            self._close_webhook()
            # P2-3：trace 导出放在这里——异步上下文里走 ``to_thread``，不卡事件循环。
            await self._flush_telemetry()
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
            observer=self._make_ledger_observer(record.run_id),
        )
        store = self._injected_store
        if store is None:
            store = ArtifactStore(
                checkpoint.artifacts_dir, run_id=record.run_id, ledger=ledger
            )
        # R17：心跳独立于 Ledger（后台线程不写 ledger）与 run.json（不与调度器
        # 的状态落盘竞争）。进程被强杀时留下的过期心跳就是 reconcile 的判据。
        self._heartbeat = RunHeartbeat(checkpoint.run_dir)
        self._heartbeat.start()
        # R12：出站 Webhook。URL 为空时返回 None，整条链路零开销。
        # R15：spool 放在 output_dir 下、所有 run 共用一个目录——挂起/崩溃遗留的
        # 通知由下一次启用 Webhook 的 run 补投；_run_dirs 只认含 run.json 的目录，
        # 所以 clean / delete_run 不会碰它。
        self._webhook = self._build_webhook(
            ledger, spool_dir=checkpoint.run_dir.parent / SPOOL_DIR_NAME)
        # C4：把附带图片落盘到 run 目录（输入证据，随 run 保留），并记一条
        # ``images_attached``——「模型这次能看到图」必须能从审计读出来。
        self._persist_images(checkpoint.run_dir, ledger)
        # C5：文档 RAG——摄取用户放进 knowledge/docs/ 的团队文档（默认关）。
        await self._ingest_docs(ledger)
        goal = record.project_goal if goal is None else goal
        # R3：工具链装配。默认关闭时返回 None，AgentFactory 与改造前完全一致。
        tool_registry = self._build_tool_registry(checkpoint, ledger)
        # R13：把 ``mcp_servers`` 声明的 MCP 服务器连上并登记它们的工具。
        # 必须在 AgentFactory 构造之前完成——工厂此刻才拿得到完整工具清单。
        await self._attach_mcp_tools(tool_registry, ledger)
        # P3-2：按配置决定是否给工厂挂响应缓存（默认 None = 不缓存，向后兼容）。
        resp_cache = LLMCache() if self._settings.llm_cache_enabled else None
        # P2-3：OTel —— 默认不建 tracer（整条链路零开销）。建了就在本次 run 里
        # 只导出一条 trace：run 是根 span，任务/LLM/工具都是它的子 span。
        tracer = self._build_tracer(record)
        models = ModelFactory(
            self._settings, ledger=ledger, fixtures_dir=_fixtures_dir(),
            cache=resp_cache,
            calibrator=self._calibrator,
            tracer=tracer,
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
                settings=self._settings, run_id=record.run_id,
                images=self._images,
                tool_registry=tool_registry,
                # P2-4：工具级 guardrails（未配置规则时为 None，零开销）。
                tool_guard=build_tool_guard(self._settings.tool_guards),
                # P3-3：流式开关从配置一路下发（默认 False，与改造前逐字一致）。
                stream=self._settings.stream,
                # P3-3 → UI：流式增量正文出口（SSE 实时 + streams/ 落盘回溯，不落 ledger、不触发 Webhook）。
                stream_sink=self._make_stream_sink(record.run_id, checkpoint.run_dir / _STREAM_DIR)),
            base_stats=record.stats.model_copy(deep=True),
            journal=SuperStepJournal(checkpoint.run_dir, ledger),
            tracer=tracer,
        )
        self._run = run
        self._open_run_span(run)
        # R5：跨角色咨询路由。默认关闭时不注入——Agent 的系统提示词里连
        # <message> 这一段都不会出现，行为与改造前逐字一致。开启后每个 Agent
        # 都能真的去问队友，答复由目标角色的一次轻量模型调用产生。
        if self._settings.peer_dialogue_enabled:
            run.agents.message_router = PeerRouter(
                run.agents, ledger,
                max_messages=self._settings.max_peer_messages,
                # R6：沉淀回调由 Crew 注入——agents 层被架构约束禁止 import
                # knowledge，写经验库的动作必须留在上层。
                on_consult=(
                    self._make_consult_recorder(run)
                    if self._settings.peer_consult_capture_enabled else None
                ),
            )
            ledger.emit(
                "peer_dialogue_enabled",
                run_id=record.run_id,
                max_messages=self._settings.max_peer_messages,
                capture=self._settings.peer_consult_capture_enabled,
            )
        # R4：DeferredPrompter 需要 run 目录才能读写审批单；run_id 直到
        # CheckpointStore.create 时才生成，只能在装配阶段注入。其它 Prompter
        # 没有这个钩子，零开销（duck typing，不扩 Prompter Protocol）。
        prompter = self._prompter
        if prompter is not None and hasattr(prompter, "bind_approvals"):
            prompter.bind_approvals(checkpoint.run_dir)
            # R30：审批等待窗口来自配置；prompter 自己没有设置时不覆盖。
            if hasattr(prompter, "timeout_seconds"):
                prompter.timeout_seconds = int(
                    self._settings.approval_timeout_seconds)
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
        if self._has_observer:
            self._dispatch_event("run_started", {
                "run_id": record.run_id,
                "goal": run.goal,
            })
        ledger.emit("run_started", goal=run.goal)
        # P2-2：把上一次运行停在哪个半步如实报出来（首次运行什么都没有 → 不发事件）。
        self._recover_superstep(run)
        try:
            self._validate_config(run)
            if resume_board is not None and record.status is RunStatus.CANCELLED:
                # `main.py cancel <run_id>` 的离线取消标记：直接以 CANCELLED 收尾
                self._board = resume_board
                return self._finalize(run, RunStatus.CANCELLED)
            record.status = RunStatus.RUNNING
            record.updated_at = utc_now()
            checkpoint.save_record(record)
            # 规划开始前已收到取消（API 软取消在 run 落盘后到达）：直接以 CANCELLED
            # 收尾，不烧一轮规划 token。
            if self._cancel_event.is_set():
                return self._finalize(run, RunStatus.CANCELLED)
            if resume_board is None:
                board = await self._plan_phase(run)
                # Ask 模式（--plan-only）：计划已生成并落盘（_plan_gate 里
                # save_tasks），到此收口为 PLANNED、不进入执行。计划仍可通过
                # resume 续跑成完整 run——这是「先看计划、再决定是否执行」。
                if self._settings.plan_only:
                    self._board = board
                    run.ledger.emit(
                        "run_planned",
                        run_id=record.run_id,
                        task_count=len(board.all()),
                    )
                    return self._finalize(run, RunStatus.PLANNED)
            else:
                # 规划期被强杀的 run 可能没有 tasks.json（或空板）。空板
                # is_terminal() 恒为 True、stats 全 0，若不拦住，_final_status
                # 会把「一个连计划都没生成的 run」判成 COMPLETED（假成功、静默
                # 丢数据）。这里 fail loud，异常由下面的兜底分支封版为 FAILED。
                if not resume_board.all():
                    raise ValueError(
                        f"run {record.run_id!r} 没有可续跑的任务面板"
                        "（规划未完成或面板已损坏）；请新建 run，不要 resume 这个 run"
                    )
                # 重建而不是直接复用 load_board() 的面板：on_change 回调（每次
                # 状态跃迁即落盘）只在构造函数上挂得进去，而 load_board 建的面板
                # 没有它。缺了它，resume 后 READY→RUNNING 那次 attempts 扣除要等到
                # 下一个完成事件才落盘，硬中断会丢掉这次扣除 —— §13.3 靠「派发即
                # 消耗一次尝试」把「中断→resume→中断」变成有界循环，丢了就不是。
                board = self._build_board(run, resume_board.all())
                self._board = board
                checkpoint.save_tasks(board)   # 修复后的状态立即落盘
                if self._resume_checkpoint == "step":
                    # R20：先消费已裁决的 step 审批单（abort 在这里转 CANCELLED），
                    # 再重入执行；未裁决时断点门会复用同一张单再次挂起。
                    self._apply_step_decision(run)
                if self._resume_checkpoint == "plan":
                    # R4：审批挂在计划卡点 → 拿已落盘的面板重入同一个卡点，
                    # 不再拆解、不再烧规划 token。交付卡点无需特殊分支：
                    # _execute 的终局判定天然会再次走到 human_delivery_gate。
                    board = await self._plan_gate(run, board, board.all())
            return await self._execute(run, board)
        except ApprovalPending as pending:
            # R4：人工审批挂起 —— 这是**正常退出路径**，不是失败。绝不落到
            # 下面的 BaseException 兜底（那会把 run 封版成 FAILED）。
            return self._suspend(run, pending.request)
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
        except BaseException:
            # 兜底封版：任何未列入白名单的异常（配置校验、门禁构造期 ValueError、
            # 落盘 OSError、on_event 回调异常、KeyboardInterrupt…）都不得让
            # run.json 永久停在 RUNNING —— 那正是 output/ 下一批「僵尸 run」的
            # 来源，且盘上与硬杀产生的死 run 完全无法区分。CancelledError 已在
            # 上面单独收口为 INTERRUPTED，这里处理其余全部退出路径。
            if not self._finalized:
                try:
                    self._finalize(run, RunStatus.FAILED)
                except Exception:
                    # 封版自身失败不能掩盖原始异常；原异常必须如实抛给调用方。
                    pass
            raise

    # ── 阶段 0：规划 ──────────────────────────────────────────────────────

    async def _plan_phase(self, run: _Run) -> TaskBoard:
        """拆解 → 校验（含 T-FINAL）→ G3 计划卡点，返回待执行的面板。"""
        tasks = await self._decompose(run)
        board = self._build_board(run, tasks)
        return await self._plan_gate(run, board, tasks)

    async def _plan_gate(
        self, run: _Run, board: TaskBoard, tasks: Sequence[Task]
    ) -> TaskBoard:
        """G3 计划卡点（R4 从 ``_plan_phase`` 抽出）。

        抽出的唯一目的：审批挂起后 ``resume`` 能拿**已落盘的面板**重入同一个
        卡点，而不重新拆解、不再烧一轮规划 token。正常 run 的调用路径与改造前
        完全一致（``_plan_phase`` → ``_plan_gate``）。
        """
        # 注意：**不要**在这里无条件写 ``self._board``。构造期守卫（配了人工
        # 卡点却没 prompter）会在下一行抛 ValueError，若此时 self._board 已设置，
        # _finalize 会顺手写 MANIFEST.json —— 那条路径的契约是「一个产物都不落盘」
        # （test_crew.py::test_human_gate_without_prompter_raises）。只在真正挂起时
        # 才把面板交给 _suspend 落盘。
        gatekeeper = self._build_gatekeeper(run, board, bus=None)
        try:
            decision = await gatekeeper.human_plan_gate(tasks)
        except ApprovalPending:
            self._board = board
            raise
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

    def _decompose_preset(self, run: _Run) -> list[Task]:
        """R28：流程直通——不调用 PM，但仍走同一套追加/校验/评分/留痕。

        校验失败抛 :class:`PlanFailedError` 而**不回灌重试**：流程是人写的，
        「让模型重拆」既违背意图也没意义；错误照常进 ledger，画布侧能读到。
        """
        from agentcrew.task.planner import PlanOutput

        assert self._preset_tasks is not None
        run.ledger.emit("flow_plan_loaded",
                        task_count=len(self._preset_tasks))
        try:
            tasks = with_final_task(
                to_tasks(PlanOutput(tasks=list(self._preset_tasks))))
            validate_plan(tasks, self._catalog)
        except PlanValidationError as exc:
            errors = list(exc.errors)
            run.ledger.emit("plan_rejected", attempt=1, errors=errors,
                            source="flow")
            raise PlanFailedError(errors) from exc
        run.ledger.emit(
            "plan_created",
            task_count=len(tasks),
            roles=sorted({task.role for task in tasks}),
            planner_attempts=0,
            source="flow",
        )
        self._score_and_log(run, tasks)
        return tasks

    def _score_and_log(self, run: _Run, tasks: list[Task]) -> None:
        """计划质量评分与留痕（P2-6）；PM 规划与流程直通共用。"""
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

    async def _decompose(self, run: _Run) -> list[Task]:
        """规划循环：失败就把 ``errors`` 回灌重拆，上限 ``plan_max_attempts``。

        校验的是 ``with_final_task`` 之后的列表：``T-FINAL`` 与 ``DELIVERY.md``
        是引擎追加的保留资源，PM 若也声明，冲突只在追加后可见。

        R28：``preset_tasks`` 给定时走 :meth:`_decompose_preset`，完全不碰 PM。
        """
        if self._preset_tasks is not None:
            return self._decompose_preset(run)
        pm = cast(ProjectManagerAgent, run.agents.get("project_manager"))
        feedback: tuple[str, ...] = ()
        errors: list[str] = []
        # P3-7：整个规划循环共享同一份历史经验参考（与具体 attempt 无关）。
        context_hint = await self._prepare_knowledge_context(run)
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
            # P2-6：校验通过后做质量评分（低分只警告不阻断——评分是观测信号）。
            self._score_and_log(run, tasks)
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

    # ── P2-3：OTel trace（一次 run 一棵）────────────────────────────────

    def _build_tracer(self, record: RunRecord) -> Any | None:
        """按配置建 tracer；未启用返回 ``None``（调用侧全是零开销判断）。"""
        if not self._settings.otel_enabled:
            return None
        from agentcrew.runtime.otel import OTLPTracer

        return OTLPTracer(
            service_name=self._settings.otel_service_name,
            endpoint=self._settings.otel_endpoint,
            capture_content=self._settings.otel_capture_content,
        )

    def _open_run_span(self, run: _Run) -> None:
        """开 run 根 span：goal / run_id / 模型 / 工具开关都进属性。"""
        tracer = run.tracer
        if tracer is None:
            return
        try:
            span = tracer.start_span("agentcrew.run", attributes={
                "agentcrew.run_id": run.record.run_id,
                "agentcrew.goal": run.goal[:200],
                "agentcrew.default_model": self._settings.default_model,
                "agentcrew.tools_enabled": self._settings.tools_enabled,
                "agentcrew.code_exec_enabled": self._settings.code_exec_enabled,
                "agentcrew.max_concurrency": self._settings.max_concurrency,
            })
            self._otel_root_span = span
        except Exception:  # noqa: BLE001 —— 采集失败不影响 run
            self._otel_root_span = None

    def _close_run_span(self, status: RunStatus) -> None:
        """结束根 span（在 ``_finalize`` 里调，纯内存操作、不做网络 I/O）。"""
        span = getattr(self, "_otel_root_span", None)
        tracer = getattr(self, "_run", None)
        tracer = getattr(tracer, "tracer", None)
        if span is None or tracer is None:
            return
        try:
            tracer.end_span(
                span,
                status="ok" if status is RunStatus.COMPLETED else "error",
                attributes={"agentcrew.run_status": status.value})
        except Exception:  # noqa: BLE001
            pass

    async def _flush_telemetry(self) -> None:
        """把本次 run 的 trace 导出（**不阻塞事件循环**，失败只记日志）。

        为什么放在 ``run()`` / ``resume()`` 的 ``finally`` 而不是 ``_finalize``：
        ``_finalize`` 是同步函数，在里面发 HTTP 会卡住事件循环（``serve`` 场景下
        等于卡住所有连接）；而 ledger 在 ``_finalize`` 末尾就 close 了，也来不及
        再记一条导出结果事件。所以导出放在异步收尾处，结果只进日志——trace 是运维
        侧数据，不该反过来影响 run 的账。
        """
        run = getattr(self, "_run", None)
        tracer = getattr(run, "tracer", None) if run is not None else None
        self._otel_root_span = None
        if tracer is None:
            return
        try:
            result = await asyncio.to_thread(tracer.flush)
        except Exception as exc:  # noqa: BLE001 —— 导出永远不许把 run 弄失败
            _logger.warning("otel 导出异常：%s", exc)
            return
        if not result.get("ok"):
            _logger.warning(
                "otel 导出失败（%s，%s 个 span）：%s",
                result.get("endpoint"), result.get("spans"), result.get("error"))
        else:
            _logger.debug("otel 已导出 %s 个 span → %s",
                          result.get("spans"), result.get("endpoint"))

    # ── P2-2：super-step 恢复 ─────────────────────────────────────────────

    def _recover_superstep(self, run: _Run) -> dict[str, Any] | None:
        """续跑/重启时汇报「上次停在哪半步」，并留一条审计事件。

        三种情况：

        - 首次运行：没有 ``superstep.json``，也没有任何工具调用 → 返回 ``None``，
          **不发事件**（新建 run 的 ledger 不该凭空多一条恢复记录）；
        - 上次正常收尾：状态是 ``idle`` 且没有未配对的工具调用 → 返回摘要但不发
          事件（没有需要恢复的东西）；
        - 上次崩在半步上：``phase=dispatch`` 或存在 pending 工具调用 → 发
          ``superstep_recovered``。任务本身由 :func:`repair_statuses`
          （RUNNING → READY）重派，这里只负责让「上一次动过什么」可见——
          尤其 ``write_file`` / ``run_python`` 这类不可回滚的副作用。
        """
        try:
            summary = run.journal.recover()
        except OSError:   # 盘只读 / 权限异常：恢复信息不该阻断续跑
            return None
        pending = summary["pending_tool_calls"]
        interrupted = summary["last_phase"] == "dispatch"
        if not pending and not interrupted:
            return None
        run.ledger.emit(
            "superstep_recovered",
            last_index=summary["last_index"],
            last_phase=summary["last_phase"],
            running_tasks=summary["running_tasks"],
            pending_tool_calls=pending,
            pending_count=len(pending),
        )
        return summary

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
            docs_retriever=self._docs_query_sync(),
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
                on_event=self._dispatch_event,
                on_checkpoint=lambda: self._persist_record(run),
                calibrator=self._calibrator,
                # R6：委派治理——角色是否存在、谁能派、能套多深、能裂变多少。
                catalog=self._catalog,
                settings=self._settings,
                # R20：单步断点（debug_step 开启时非 None，默认 None 零参与）。
                step_gate=self._make_step_gate(run),
                # P2-2：每轮调度落 super-step 标记（崩溃后能看出停在哪半步）。
                journal=run.journal,
                # P2-3：任务 span 挂到 run 根 span 下。
                tracer=run.tracer,
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
            now = utc_now()
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

        def _persist() -> None:
            """A-19：非跃迁写入（评审记录/返工计数/执行结果）的落盘出口。

            只写 tasks.json，不重写 MANIFEST——清单与产物相关，计数变化不改变它，
            每次派发都重建清单会把写放大到不可接受。
            """
            if board is not None:
                run.checkpoint.save_tasks(board)

        def _on_change(_task: Task, _change: StatusChange) -> None:
            if board is not None:
                run.checkpoint.save_tasks(board)
                run.store.write_manifest()

        board = TaskBoard(tasks, on_change=_on_change, on_persist=_persist)
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
        # R7：主管审核（谁派的活谁验收）。默认关闭；离线 mock 通道没有
        # supervisor 夹具，强行启用只会让每次主管评审都撞 StructuredOutputError
        # 再被当成格式错误放行——不如明确跳过并留一条可追溯的原因。
        supervisor = None
        reassign = None
        if self._settings.supervisor_review_enabled:
            if self._settings.mock:
                run.ledger.emit(
                    "supervisor_review_skipped",
                    run_id=run.record.run_id,
                    reason="mock_channel",
                )
            else:
                supervisor = SupervisorReviewer(
                    reviewer, self._catalog, run.ledger,
                    # R9：scope=all 时规划期任务也由 supervisor_role 验收。
                    scope=self._settings.supervisor_scope,
                    fallback_role=self._settings.supervisor_role,
                    # R33：直接主管之上的逐级复核链（默认空 = 单层）。
                    escalation=self._settings.supervisor_chain,
                )
                # R10：只有配了备选映射才接线——空映射时 pick 恒为 None，
                # 不构造也就少一个对象。
                if self._settings.role_fallback:
                    reassign = ReassignPolicy(
                        self._catalog, self._settings.role_fallback,
                        max_reassignments=(
                            self._settings.max_supervisor_reassignments),
                    )
        # C3：G1 执行校验档——默认关，``gate_config.exec_verify=true`` 才装配。
        # ``mock`` 通道显式跳过并留痕：夹具里的产物是跨任务引用的（T-003 的
        # test_*.py 常常 import T-002 的模块），只铺单个任务的产物必然 import
        # 失败；那不是产物的问题，是夹具的形态。真实验证要跑真实模型端到端。
        exec_verifier = None
        if bool(self._settings.gate_config.get("exec_verify", False)):
            if self._settings.mock:
                run.ledger.emit(
                    "g1_exec_check_skipped",
                    run_id=run.record.run_id,
                    reason="mock_channel",
                )
            else:
                exec_verifier = ExecutionVerifier(
                    backend=self._execution_backend(),
                    # 校验目录与 artifacts/ 平级：都在 run 目录内，随 run 一起
                    # 清理；不写进 artifacts/ 是为了不污染 MANIFEST 与产物清单。
                    workspace_root=run.store.root.parent / "verify",
                    ledger=run.ledger,
                    timeout=float(self._settings.gate_config.get(
                        "exec_verify_timeout_seconds", 120)),
                )
        return Gatekeeper(
            reviewer,
            run.store,
            board,
            gate=self._settings.gate,
            # P2-5：门禁规则必须真正接线。此前 Crew 装配时漏传，导致
            # gate_config 里的 g1/g2 开关、评审人数、收敛阈值在真实 run 中
            # 全部回落硬编码默认值（配置形同虚设）。
            gate_config=self._settings.gate_config,
            ledger=run.ledger,
            prompter=self._prompter,
            context_bus=bus,
            review_score_threshold=self._settings.review_score_threshold,
            supervisor=supervisor,
            reassign=reassign,
            exec_verifier=exec_verifier,
        )

    @property
    def _has_observer(self) -> bool:
        """是否值得发事件：有 ``on_event`` 回调或配了 Webhook 任一即可。

        R12 之前这个判断就是 ``self._on_event is not None``；加了 Webhook 之后
        「CLI 不传 on_event 但配了 webhook」也要能发事件，否则通知永远到不了。

        用 ``getattr`` 取 ``_webhook``：有的用例为验证 ``_finalize`` 的幂等会绕过
        ``__init__`` 直接构造对象，那种半成品实例没有这个属性，不该因此崩。
        """
        return (self._on_event is not None
                or getattr(self, "_webhook", None) is not None)

    def _dispatch_event(self, name: str, payload: dict[str, Any]) -> None:
        """事件统一出口：先给 ``on_event``（SSE / 看板），再给 Webhook。

        顺序固定：本地观察者同步执行，Webhook 只是入队（后台线程投递），所以
        这里不会因为网络慢而拖住 run。
        """
        if self._on_event is not None:
            self._on_event(name, payload)
        if self._webhook is not None:
            self._webhook.emit(name, payload)

    def _make_stream_sink(
        self, run_id: str, streams_dir: Path
    ) -> Callable[[str, str, str], None]:
        """造一个流式增量正文的出口（P3-3 → UI + 回溯落盘）。

        双通道：``on_event``（SSE / 看板 / ``curl -N``）推瞬态正文；同时把正文
        追加落盘到 ``streams_dir/<task_id>.md``，供刷新后回溯。ledger 里只留
        ``agent_stream_chunk`` 的 ``delta_length``（审计不膨胀）；Webhook 是事件
        通知不是正文流，几千个块不该打爆接收端——所以这里**刻意跳过**
        :meth:`_dispatch_event`（它会把 Webhook 一起带上）。
        """
        def sink(task_id: str, role: str, delta: str) -> None:
            if not delta:
                return
            _append_stream(streams_dir, task_id, delta)
            if self._on_event is not None:
                self._on_event("agent_stream_delta", {
                    "run_id": run_id,
                    "task_id": task_id,
                    "role": role,
                    "delta": delta,
                })
        return sink

    def _make_ledger_observer(self, run_id: str) -> Callable[[str, dict[str, Any]], None]:
        """造一个 ledger 明细事件的实时出口（P3-3 对话流）。

        ``Ledger.emit`` 落盘后回调这里：把明细事件（``task_started`` /
        ``task_finished`` / ``task_notes`` / ``tool_call`` / ``gate_result`` /
        ``artifact_written`` / ``llm_call`` / ``plan_created`` …）转发给
        ``on_event``（SSE），并补上 ``run_id`` 供 EventBus 按 run 分桶。这样
        「只看这一个 run」的订阅者不用轮询文件也能实时拿到执行明细。

        刻意只走 ``self._on_event``（SSE / 看板），**不走** ``_dispatch_event``：
        后者会把 Webhook 一起带上，明细事件量级远大于 run 级通知，会打爆接收端。
        已走 ``_dispatch_event`` / ``_notify`` 的事件（见 ``_LEDGER_SSE_EXCLUDED``）
        跳过，避免重复推送；``run_id`` 已在 data 里时保持原值。
        """
        def observer(event: str, data: dict[str, Any]) -> None:
            if self._on_event is None or event in _LEDGER_SSE_EXCLUDED:
                return
            payload = dict(data)
            payload.setdefault("run_id", run_id)
            self._on_event(event, payload)
        return observer

    def _build_webhook(
        self, ledger: Ledger, *, spool_dir: Path | None = None
    ) -> WebhookDispatcher | None:
        """按配置构造分发器；URL 为空时返回 ``None``（默认关闭）。

        启用时记一条 ``webhook_enabled``，但**只记打码后的 URL**：查询串里可能
        带 token，整条 URL 写进 ledger 就等于泄露。``spool_dir`` 由 ``_start``
        传入（R15）；库层直接调用不传时零落盘，行为与 R14 一致。
        """
        # 只看旧字段是不够的：只配 ``webhooks``（多接收端）时 ``webhook_url`` 是空的，
        # 早退会让多接收端完全失效——实测踩过一次。
        if not self._settings.webhook_url and not self._settings.webhooks:
            return None
        config = WebhookConfig(
            url=self._settings.webhook_url,
            secret=self._settings.webhook_secret,
            events=tuple(self._settings.webhook_events),
            timeout_seconds=self._settings.webhook_timeout_seconds,
            max_retries=self._settings.webhook_max_retries,
            # R14：多接收端。``webhooks`` 非空时 ``WebhookConfig.resolved()`` 以它
            # 为准，旧的单接收端字段自动让位（避免同一地址被推两遍）。
            targets=tuple(
                WebhookTarget(
                    name=str(raw.get("name") or ""),
                    url=str(raw.get("url") or ""),
                    secret=str(raw.get("secret") or ""),
                    events=tuple(
                        str(item) for item in (raw.get("events") or [])),
                    only_failures=bool(raw.get("only_failures", False)),
                )
                for raw in self._settings.webhooks
                if str(raw.get("url") or "").strip()
            ),
        )
        dispatcher = WebhookDispatcher(
            config, sink=self._webhook_sink(ledger),
            spool_dir=spool_dir,
            # R15：payload 落盘前按同一套密钥表脱敏，不绕过 Ledger 的防护。
            secrets=tuple(self._settings.secret_values()),
        )
        ledger.emit(
            "webhook_enabled",
            # 逐接收端记录，URL 一律打码：查询串里可能有 token。
            targets=[
                {
                    "name": target.name,
                    "url": _redact_url(target.url),
                    "events": list(target.events),
                    "signed": bool(target.secret),
                    "only_failures": target.only_failures,
                }
                for target in config.resolved()
            ],
            spool_enabled=spool_dir is not None,
        )
        return dispatcher

    async def _attach_mcp_tools(
        self, registry: ToolRegistry | None, ledger: Ledger
    ) -> None:
        """R13：连接 ``mcp_servers`` 里的每个服务器，把工具登记进注册表。

        只在 ``tools_enabled``（``registry`` 非空）时装配——MCP 工具也是工具，没有
        注册表就无处安放；这也保证了默认配置下**零子进程、零网络**。

        单个服务器失败（命令不存在、握手超时、配置形状不对）只记一条 ``mcp_failed``
        并跳过，不影响其它服务器与本 run：MCP 是外挂能力，一个外挂坏了不该让整支
        团队停摆。``CancelledError`` 不在此列（它是 ``BaseException``），取消照常穿透。
        """
        if registry is None or not self._settings.mcp_servers:
            return
        # G-1：不可信输入预设下不装配任何 MCP 工具——外部子进程谈不上只读，
        # 挂上去等于在「只读模式」里开一个后门。
        if self._settings.untrusted_inputs:
            ledger.emit(
                "mcp_skipped_untrusted",
                servers=sorted(self._settings.mcp_servers),
            )
            return
        for server, raw in self._settings.mcp_servers.items():
            if not isinstance(raw, Mapping):
                ledger.emit(
                    "mcp_failed", server=str(server),
                    error="mcp_servers 的每个条目都必须是映射")
                continue
            client: MCPClient | None = None
            try:
                client = MCPClient.from_config(dict(raw))
                await client.start()
                specs = await client.list_tools()
            except Exception as exc:  # noqa: BLE001 —— 第三方进程什么都可能抛
                if client is not None:
                    try:
                        await client.stop()
                    except Exception:  # noqa: BLE001 —— 关闭失败不掩盖原始错误
                        pass
                ledger.emit(
                    "mcp_failed", server=str(server),
                    error=f"{type(exc).__name__}: {exc}"[:300])
                continue
            tools = adapt_tools(str(server), client, specs)
            for tool in tools:
                registry.register(tool)
            self._mcp_clients.append(client)
            ledger.emit(
                "mcp_connected",
                server=str(server),
                tools=[tool.name for tool in tools],
            )

    async def _close_mcp(self) -> None:
        """R13：关闭本次 run 建立的全部 MCP 连接（幂等）。

        关不掉的连接静默跳过：收尾阶段再抛异常会把成功的结果变成失败。
        """
        clients, self._mcp_clients = self._mcp_clients, []
        for client in clients:
            try:
                await client.stop()
            except Exception:  # noqa: BLE001 —— 已在收尾，不再向上抛
                pass

    def _webhook_sink(
        self, ledger: Ledger
    ) -> Callable[[str, dict[str, Any]], None]:
        """投递结果的落账口；ledger 已关时静默丢弃。

        收尾顺序里 ledger 可能先关（``_finalize`` 尾部），而队列里还剩几条结果。
        那些结果记不上就记不上——绝不能因为「一条通知的日志」在收尾阶段抛异常。
        """
        def sink(name: str, data: dict[str, Any]) -> None:
            try:
                ledger.emit(name, **data)
            except (RuntimeError, TypeError):
                # RuntimeError：ledger 已关（收尾顺序差异）。
                # TypeError：data 的键与 emit 的位置参数撞名——实测过一次
                # （`event`），这类错误绝不该让 run 挂掉，所以一并兜住。
                pass

        return sink

    def _stop_heartbeat(self) -> None:
        """R17：停心跳线程并删掉心跳文件（幂等；半构造实例也安全）。"""
        heartbeat = getattr(self, "_heartbeat", None)
        if heartbeat is not None:
            heartbeat.stop()
        self._heartbeat = None

    def _close_webhook(self) -> None:
        """收尾：等投递队列排空（最多 5 秒），随后丢弃引用。

        幂等；半构造实例（绕过 ``__init__`` 的用例）没有属性时直接返回。
        """
        dispatcher = getattr(self, "_webhook", None)
        if dispatcher is not None:
            dispatcher.close()
        self._webhook = None

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
            utc_now() if finished else run.record.stats.finished_at)
        run.record.stats = stats

    def _persist_record(self, run: _Run) -> None:
        """周期性把 ``run.json`` 的统计同步落盘（``Scheduler.on_checkpoint``，I4）。

        与 ``tasks.json`` 同一个节拍写：两条记录必须描述同一个瞬间，只写其中一条
        会让盘上出现「任务都跑完了、统计还是 0」这种自相矛盾的快照。统计走
        ``_sync_stats``（可重复调用、以 ``base_stats`` 为基线，不重复计数），因此
        每次落盘的都是一份完整自洽的账；硬杀最多丢一个完成事件的开销。
        """
        self._sync_stats(run, finished=False)
        run.record.updated_at = utc_now()
        run.checkpoint.save_record(run.record)

    def _final_status(self, board: TaskBoard) -> RunStatus:
        """终局判定（spec §7.4）：有失败/阻塞/跳过或未终结即 FAILED。"""
        stats = board.stats()
        # 空板不是「全部成功」：规划未完成 / 面板损坏时绝不能判 COMPLETED。
        # 与 resume_from 的空板校验互为双保险（那里拦入口，这里拦终局判定）。
        if stats.total_tasks == 0:
            return RunStatus.FAILED
        # R4：条件分支未命中的 SKIPPED 是**预期结果**，不是失败；只有上游
        # 真失败导致的 SKIPPED（upstream-skipped）才把整场判成 FAILED。
        # 没有 when_outcomes 的 run 里 unexpected_skips == stats.skipped，
        # 与改造前逐字一致。
        unexpected_skips = sum(
            1 for task in board.all()
            if task.status is TaskStatus.SKIPPED
            and not is_conditional_skip(task)
        )
        if stats.failed or stats.blocked or unexpected_skips or not board.is_terminal():
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
        if status is RunStatus.PLANNED:
            return EXIT_CODES["ok"]
        if status in (RunStatus.CANCELLED, RunStatus.INTERRUPTED):
            return EXIT_CODES["cancelled"]
        # R4：审批挂起是**正常**的中间态，不是失败。EXIT_CODES 字典本身不动
        # （test_crew.py 对它做精确相等断言），这里复用 ok=0。
        if status is RunStatus.AWAITING_APPROVAL:
            return EXIT_CODES["ok"]
        return EXIT_CODES["failed"]

    async def _ingest_docs(self, ledger: Ledger) -> None:
        """C5：摄取团队文档（``docs_rag_enabled`` 开启时）。

        文档在 ``<output_dir>/knowledge/docs/``，由用户自己放置；摄取失败只记
        warning——文档是上下文增强，不能让它把 run 拖死。留痕事件让「这次 run
        有多少文档参与检索」能从审计读出来。
        """
        if not self._settings.docs_rag_enabled:
            return
        from agentcrew.knowledge.docs import DocStore
        from agentcrew.knowledge.embeddings import build_embedder
        from agentcrew.models.embedding import embedding_target

        try:
            embedder = build_embedder(
                self._settings,
                output_dir=self._settings.output_dir,
                target=embedding_target(self._settings),
            )
            store = DocStore(
                self._settings.output_dir / "knowledge" / "docs",
                embedder=embedder,
            )
            result = await store.ingest()
        except Exception as exc:  # noqa: BLE001 —— 检索增强失败不拖死 run
            _logger.warning("文档摄取失败: %s", exc)
            ledger.emit("docs_ingest_failed",
                        error=f"{type(exc).__name__}: {exc}"[:200])
            return
        self._docs_store = store
        ledger.emit("docs_ingested", docs=result.docs, chunks=result.chunks,
                    rebuilt=result.rebuilt, skipped=result.skipped)

    def _docs_query_sync(self) -> Any | None:
        """C5：给 ContextBus 的同步检索回调；未启用返回 ``None``（零开销）。"""
        docs_store = self._docs_store
        if docs_store is None or self._settings.docs_rag_limit <= 0:
            return None

        def retrieve(query: str) -> list[tuple[str, str]]:
            hits = docs_store.query_sync(
                query, limit=self._settings.docs_rag_limit,
                vector_weight=self._settings.docs_rag_vector_weight)
            return [(f"docs:{hit.chunk.doc}", f"[{hit.chunk.heading}] "
                                             f"{hit.chunk.text}") for hit in hits]

        return retrieve

    def _persist_images(self, run_dir: Path, ledger: Ledger) -> None:
        """C4：把附带图片写成 ``<run_dir>/context/images/NN.<ext>``（输入证据）。

        每个文件独立落盘、失败只记 warning 不炸 run——图片是输入增强，不是
        交付路径上的必需品；写盘失败时模型看不到图，``vision_unsupported``
        的降级语义仍然成立。
        """
        if not self._images:
            return
        image_dir = run_dir / "context" / "images"
        extensions = {"image/png": "png", "image/jpeg": "jpg",
                      "image/webp": "webp", "image/gif": "gif"}
        saved: list[str] = []
        total_bytes = 0
        for index, image in enumerate(self._images):
            extension = extensions.get(str(image.get("mime", "")), "png")
            try:
                image_dir.mkdir(parents=True, exist_ok=True)
                path = image_dir / f"{index:02d}.{extension}"
                path.write_bytes(base64.b64decode(image.get("data", "")))
                saved.append(path.name)
                total_bytes += path.stat().st_size
            except (OSError, ValueError) as exc:
                _logger.warning("图片落盘失败 #%s: %s", index, exc)
        if saved:
            ledger.emit(
                "images_attached",
                count=len(saved),
                total_bytes=total_bytes,
                files=saved,
            )

    def _execution_backend(self) -> ExecutionBackend:
        """C2：按配置构造执行后端（local 默认；docker 可选，缺失即报错）。

        惰性构造且只构造一次：``build_backend`` 在 docker 模式下会探测 ``docker``
        可执行文件，gatekeeper 每轮重建、工具注册表每次 run 重建，不该反复探测。
        ``SandboxError`` 原样冒泡——「选了 docker 却没有 docker」是运维配置错误，
        必须让 run 失败并说清原因，不能静默退回不隔离的 local。
        """
        if self._sandbox_backend is None:
            settings = self._settings
            self._sandbox_backend = build_backend(
                settings.sandbox_backend,
                image=settings.sandbox_image,
                memory_mb=settings.sandbox_memory_mb,
                cpus=settings.sandbox_cpus,
                allow_network=settings.sandbox_allow_network,
            )
        return self._sandbox_backend

    def _build_tool_registry(
        self, checkpoint: CheckpointStore, ledger: Ledger
    ) -> ToolRegistry | None:
        """R3：装配 run 级工具注册表；默认关闭时返回 ``None``（行为逐字不变）。

        这是把「库能力」接进生产 run 的**唯一装配点**：此前 ``ToolRegistry`` 只
        在单测里被实例化，``tool_permissions`` / ``mcp_servers`` 因此是死配置，
        Agent 永远走单次调用。现在 ``settings.tools_enabled`` 开启时，为本次 run
        登记内置工具（默认 5 个；``code_exec_enabled`` 打开时含 ``run_python``，
        见 P1 的默认关闭决定）并把 ``tool_permissions`` 下发成执行期白名单。

        边界：读工具以 ``run_dir`` 为根（能看上游 ``artifacts/`` 与 ``context/``），
        写工具只以 ``run_dir/workspace`` 为根（中间草稿与交付产物物理隔离，不进
        ``MANIFEST.json``）；``mcp_servers`` 需要异步连接与生命周期管理，仍走库层
        显式装配，不在这里半接线（见 ``PROJECT-STATE.md`` §7）。
        """
        override = self._tool_registry_override
        if override is not None:
            override.bind_ledger(ledger)
            return override
        if not self._settings.tools_enabled:
            return None

        run_dir = checkpoint.run_dir
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        registry = ToolRegistry(ledger=ledger)
        tools = [
            ReadFileTool(workdir=run_dir),
            ListFilesTool(workdir=run_dir),
            GrepTool(workdir=run_dir),
        ]
        # G-1 不可信输入预设：只给只读工具。写工具（write_file / run_python /
        # web_fetch）一律不下发，code_exec_enabled 在这里也不管用——处理不可信
        # 第三方文档时，「能读不能写」是提示词注入面前唯一可靠的边界。
        if not self._settings.untrusted_inputs:
            # P1：``run_python`` 默认不进注册表。它有进程内隔离（``-I`` / 裁剪
            # 环境 / cwd 限制 / 超时杀进程），但没有网络与系统调用沙箱——默认
            # 打开等于把「配置写错」升级成「远程执行面」。需要时用
            # AGENTCREW_CODE_EXEC_ENABLED 显式打开，边界写在 docs/OPERATIONS.md。
            if self._settings.code_exec_enabled:
                # C2：同一份后端给 run_python 与 run_tests——「换后端」只改配置，
                # 不改工具；``local`` 时行为与引入前逐字一致。
                backend = self._execution_backend()
                tools.append(RunPythonTool(workdir=run_dir, backend=backend))
                # C3：执行验证——写完代码能当场跑，而不是等门禁判决。
                tools.append(RunTestsTool(workdir=run_dir, backend=backend))
            # C1：``web_search`` 只在配置了提供方时注册。没配就是没有这个工具，
            # 而不是「有但一调就报错」——后者会让模型在提示词里被承诺一个假能力。
            if self._settings.search_provider.strip():
                tools.append(WebSearchTool(
                    provider=build_search_provider(self._settings.search_provider),
                    max_results=self._settings.search_max_results,
                    timeout_secs=self._settings.search_timeout_seconds,
                ))
            tools.extend((WriteFileTool(workdir=workspace), WebFetchTool()))
        for tool in tools:
            registry.register(tool)
        permissioned: list[str] = []
        for role, allowed in self._settings.tool_permissions.items():
            if isinstance(allowed, (list, tuple)):
                registry.set_role_permissions(
                    str(role), [str(name) for name in allowed]
                )
                permissioned.append(str(role))
        ledger.emit(
            "tool_registry_ready",
            run_id=run_dir.name,
            tools=[tool.name for tool in registry.list_tools()],
            workspace=workspace.name,
            permissioned_roles=sorted(permissioned),
            untrusted_inputs=bool(self._settings.untrusted_inputs),
            # C2：让审计能回答「这次的代码在哪跑的」——local 与 docker 的责任边界
            # 完全不同，只记工具名不记后端等于没记。
            sandbox_backend=(
                self._settings.sandbox_backend
                if self._settings.code_exec_enabled else ""),
        )
        return registry

    async def _prepare_knowledge_context(self, run: _Run) -> str:
        """P3-7 + R3 + R27：规划前检索历史经验与实体记忆，拼成 PM 的 ``context_hint``。

        两块各自有独立开关与独立留痕。``entity_memory_enabled`` 默认关闭，此时
        返回值与改造前**逐字一致**（有经验命中就是经验块本身，无命中就是空串）——
        不会多一个换行、也不会多发一个事件。

        R27：``embedding_enabled`` 时把经验库与查询词一起算成向量，走混合打分；
        embedding 端点失败**不炸 run**——降级回纯 BM25 并留一条
        ``embedding_fallback`` 事件（失败要可见，但不能让检索把整场 run 拖死）。
        """
        blocks: list[str] = []
        if self._reuse_knowledge_enabled:
            store = ExperienceStore(self._settings.output_dir)
            vectors = await self._retrieval_vectors(run, store)
            matched = store.query(run.goal, limit=3,
                                  doc_vectors=vectors[0], query_vector=vectors[1],
                                  vector_weight=self._settings.retrieval_vector_weight)
            if matched:
                run.ledger.emit(
                    "knowledge_reused",
                    goal=run.goal,
                    matched_experiences=len(matched),
                )
                blocks.append(store.inject_context(
                    run.goal, limit=3,
                    doc_vectors=vectors[0], query_vector=vectors[1],
                    vector_weight=self._settings.retrieval_vector_weight))
        if self._entity_memory_enabled:
            entities = EntityMemoryStore(self._settings.output_dir)
            entity_vectors = await self._retrieval_vectors(
                run, entities, kind="entity")
            matched_entities = entities.query(
                run.goal, limit=8, doc_vectors=entity_vectors[0],
                query_vector=entity_vectors[1],
                vector_weight=self._settings.retrieval_vector_weight)
            if matched_entities:
                run.ledger.emit(
                    "entity_memory_reused",
                    goal=run.goal,
                    matched_entities=len(matched_entities),
                )
                blocks.append(entities.inject_context(
                    run.goal, limit=8, doc_vectors=entity_vectors[0],
                    query_vector=entity_vectors[1],
                    vector_weight=self._settings.retrieval_vector_weight))
        # C5：团队文档——PM 拆解也要看到手册与规范（默认关，零开销）。
        if self._docs_store is not None and self._settings.docs_rag_limit > 0:
            hits = await self._docs_store.query(
                run.goal, limit=self._settings.docs_rag_limit,
                vector_weight=self._settings.docs_rag_vector_weight)
            if hits:
                run.ledger.emit(
                    "docs_context_used",
                    target="planning",
                    hits=len(hits),
                )
                lines = ["## 团队文档（knowledge/docs/ 检索命中）"]
                for hit in hits:
                    lines.append(
                        f"### {hit.chunk.doc} · {hit.chunk.heading}\n"
                        f"{hit.chunk.text}")
                blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    async def _retrieval_vectors(
        self, run: _Run, store: object, *, kind: str = "experience"
    ) -> tuple[list[list[float]] | None, list[float] | None]:
        """R27：为经验库与当前目标准备向量；不可用返回 ``(None, None)``。

        端点失败 / 未配置都走降级：检索退回纯 BM25，并留一条
        ``embedding_fallback`` 事件说明原因——静默降级会让人以为用了向量。
        """
        if not self._settings.embedding_enabled:
            return None, None
        from agentcrew.knowledge.embeddings import EmbeddingError, build_embedder
        from agentcrew.models.embedding import embedding_target

        embedder = build_embedder(
            self._settings, output_dir=self._settings.output_dir,
            target=embedding_target(self._settings))
        if embedder is None:
            return None, None
        try:
            documents = store.all()          # type: ignore[attr-defined]
            if not documents:
                return None, None
            doc_vectors = await embedder.embed(
                [store.haystack(item) for item in documents])  # type: ignore[attr-defined]
            query_vectors = await embedder.embed([run.goal])
        except EmbeddingError as exc:
            run.ledger.emit("embedding_fallback",
                            reason=str(exc), embedder=embedder.name, kind=kind)
            return None, None
        run.ledger.emit("embedding_used", embedder=embedder.name, kind=kind,
                        documents=len(doc_vectors),
                        dim=len(doc_vectors[0]) if doc_vectors else 0,
                        vector_weight=self._settings.retrieval_vector_weight)
        return doc_vectors, query_vectors[0]

    def _make_consult_recorder(self, run: _Run) -> Callable[..., None]:
        """R6：把一次真实咨询的问答对写进全局经验库（默认关闭时不会构造）。

        包成闭包而不是直接把方法交给 ``PeerRouter``：``PeerRouter`` 只需要一个
        ``(提问方, 目标, 问题, 答复)`` 的四参回调，不该知道 ``_Run`` 长什么样。
        """
        def record(
            from_role: str, to_role: str, question: str, answer: str
        ) -> None:
            self._record_consult_experience(
                run, from_role, to_role, question, answer)

        return record

    def _record_consult_experience(
        self,
        run: _Run,
        from_role: str,
        to_role: str,
        question: str,
        answer: str,
    ) -> None:
        """R6：咨询问答 → 一条 ``Experience``。

        字段映射刻意「可检索优先」：``task_description`` 写「谁问谁问了什么」，
        这样下次同类问题走 :meth:`ExperienceStore.query` 的关键词匹配能命中；
        ``solution_summary`` 存答复本身。``artifacts`` / ``key_decisions`` 留空
        ——咨询不产物、不做决策，填进去会让经验库的语义变浑。
        """
        experience = _consult_experience(
            from_role, to_role, question, answer,
            run_id=run.record.run_id,
        )
        if experience is None:
            # 占位文案不配进经验库：它描述的是「模型没答」，不是一条知识。
            return
        ExperienceStore(self._settings.output_dir).add(experience)
        run.ledger.emit(
            "consult_experience_captured",
            from_role=from_role,
            to_role=to_role,
            experience_id=experience.id,
        )

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
        if self._entity_memory_enabled:
            self._capture_entities(run, extracted)

    def _capture_entities(
        self, run: _Run, extracted: list[tuple[str, Experience]]
    ) -> None:
        """R3：把这次沉淀的经验里的命名事实追加进全局实体库。

        实体来自同一批成功经验（因此依赖 ``capture_experiences`` 开启；两者任一
        关闭都不会写盘）。追加而非覆写，读取时按名字归并，与经验库同一口径。
        """
        records: list[EntityRecord] = []
        for _, exp in extracted:
            records.extend(
                entities_from_experience(exp, run_id=run.record.run_id)
            )
        if not records:
            return
        EntityMemoryStore(self._settings.output_dir).append(records)
        run.ledger.emit(
            "entity_memory_updated",
            run_id=run.record.run_id,
            entities=len(records),
            distinct=len({record.name.casefold() for record in records}),
        )

    # ── R20：单步断点 ─────────────────────────────────────────────────────

    def _make_step_gate(
        self, run: _Run
    ) -> Callable[[list[Task]], None] | None:
        """``debug_step`` 开启时返回断点门，否则 ``None``（调度器零参与）。

        「``run`` 裁决已消费」的 run 不再武装断点——用户已经说了「一路跑到
        终局」，resume 不能又停下来。``abort`` 在 resume 路径
        （:meth:`_apply_step_decision`）转成 ``CANCELLED``，走不到这里。
        """
        if not self._settings.debug_step:
            return None
        store = ApprovalStore(run.checkpoint.run_dir)
        consumed, final = store.consumed_steps()
        if final == "run":
            return None
        return StepGate(store, consumed)

    def _apply_step_decision(self, run: _Run) -> None:
        """resume 重入 step 断点前消费已裁决的审批单（DeferredPrompter 纪律 2）。

        - ``abort`` → 与计划卡点 ``cancel`` 同一条收尾路径：``CANCELLED`` 封版 +
          ``RunCancelled``（``main.py`` 捕获后以退出码 130 结束）。
        - ``step`` / ``run`` → 只消费；放行集合与终局指令由
          :meth:`_make_step_gate` 经 ``consumed_steps()`` 快照读取。
        - 仍 ``pending`` → 什么都不做：断点门会复用同一张单再次挂起。
        """
        store = ApprovalStore(run.checkpoint.run_dir)
        opened = store.open_for("step")
        if opened is None or opened.status != "decided":
            return
        store.consume(opened.id)
        if opened.decision.get("action") == "abort":
            # ``self._board`` 已由 resume 分支设为修复后的面板（与 plan 卡点
            # cancel 路径同一纪律：封版前把面板交给 _suspend/_finalize 落盘）。
            self._finalize(run, RunStatus.CANCELLED)
            raise RunCancelled(
                f"调试断点 abort：run {run.record.run_id} 结束为 cancelled"
            )

    def _suspend(self, run: _Run, request: ApprovalRequest) -> RunRecord:
        """R4：人工审批挂起 —— 写 ``awaiting_approval`` 后**正常退出，不封版**。

        与 :meth:`_finalize` 的区别是三处刻意的：不发 ``run_finished``（这不是
        终局）、不置 ``self._finalized``（resume 是另一个 Crew 实例，但同进程里
        的状态保持诚实）、不改任务面板状态（下次 resume 从原卡点接着问）。
        审批单本体由 ``DeferredPrompter`` 写进 ``approvals.jsonl``，这里只负责
        让 run 状态和事件流对外可见。
        """
        if self._finalized:
            return run.record
        self._sync_stats(run, finished=False)
        record = run.record
        record.status = RunStatus.AWAITING_APPROVAL
        record.updated_at = utc_now()
        run.checkpoint.save_record(record)
        if self._board is not None:
            run.checkpoint.save_tasks(self._board)
            run.store.write_manifest()
        payload = {
            "run_id": record.run_id,
            "approval_id": request.id,
            "checkpoint": request.checkpoint,
            "status": record.status.value,
            "exit_code": self._exit_code(record.status),
        }
        run.ledger.emit("run_paused", goal=record.project_goal, **payload)
        run.ledger.emit(
            "approval_requested",
            approval_id=request.id,
            checkpoint=request.checkpoint,
            context=request.context,
        )
        if self._has_observer:
            self._dispatch_event("run_paused", payload)
            self._dispatch_event("approval_requested", {
                "run_id": record.run_id,
                "approval_id": request.id,
                "checkpoint": request.checkpoint,
            })
        # R12：先排空 Webhook 队列（run_paused / run_finished 可能就是最后一条
        # 通知），再关 ledger——顺序反了的话 sink 会往已关闭的 ledger 里写。
        self._close_webhook()
        run.ledger.close()
        return record

    def _finalize(
        self, run: _Run, status: RunStatus, *, exit_code: int | None = None
    ) -> RunRecord:
        """封版：写状态与统计、锁 MANIFEST、发 ``run_finished``、关账（幂等）。"""
        if self._finalized:
            return run.record
        # 先置位再动副作用：封版中途抛异常（on_event 回调 / 落盘 OSError）时，
        # 异常链上的兜底分支不会二次封版把终态改写，也不会向已 close 的
        # ledger 再 emit 一条重复的 run_finished。
        self._finalized = True
        self._sync_stats(run)
        record = run.record
        record.status = status
        record.updated_at = utc_now()
        try:
            # A-4：run.json **最后**写——它是 resume 的入口与终态标记。tasks.json
            # 与 MANIFEST 先写，run.json 写上终态时其余文件必然已就绪；任何一步
            # 失败都回滚 ``_finalized`` 让兜底分支重试，避免「run.json 已
            # COMPLETED 而 tasks.json 仍是旧快照」的自相矛盾快照。
            if self._board is not None:
                run.checkpoint.save_tasks(self._board)
                run.store.write_manifest()
            run.checkpoint.save_record(record)
        except BaseException:
            self._finalized = False
            raise
        started = record.stats.started_at or record.created_at
        code = self._exit_code(status) if exit_code is None else exit_code
        self._close_run_span(status)
        run.ledger.emit(
            "run_finished",
            goal=record.project_goal,
            status=status.value,
            exit_code=code,
            duration_ms=int(seconds_between(started) * 1000),
        )
        # P1-5：同一事件同步推给 ``on_event`` 观察者（API SSE / 终端看板），
        # 与 ledger 里的字段同形 —— 实时流不用等落盘就能看到 run 结局。
        if self._has_observer:
            self._dispatch_event("run_finished", {
                "run_id": record.run_id,
                "status": status.value,
                "exit_code": code,
            })
        # R12：先排空 Webhook 队列（run_paused / run_finished 可能就是最后一条
        # 通知），再关 ledger——顺序反了的话 sink 会往已关闭的 ledger 里写。
        self._close_webhook()
        run.ledger.close()
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


#: R6：凡是以这些文案开头的「答复」都不是知识，是降级说明——不进经验库。
_PLACEHOLDER_ANSWERS = (
    "（离线演示",
    "（对方未能作答",
    "（对方未给出有效答复",
    "（该角色暂不支持咨询",
)


def _consult_experience(
    from_role: str,
    to_role: str,
    question: str,
    answer: str,
    *,
    run_id: str,
) -> Experience | None:
    """把一次咨询的问答对转成 ``Experience``；占位答复返回 ``None``（R6 纯逻辑）。

    独立成模块级函数是为了可单测：``Crew`` 那层只负责「有值就落盘 + 记事件」。
    """
    text = (answer or "").strip()
    if not text or text.startswith(_PLACEHOLDER_ANSWERS):
        return None
    return Experience(
        id=uuid4().hex,
        task_description=f"{from_role} 向 {to_role} 提问：{(question or '').strip()[:200]}",
        role=to_role,
        solution_summary=text[:400],
        artifacts=[],
        key_decisions=[],
        success=True,
        run_id=run_id,
        created_at=utc_now(),
    )


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
            created_at=utc_now(),
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
    now = utc_now()
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


def _redact_url(url: str) -> str:
    """只保留 ``scheme://host/path``，丢掉查询串与片段。

    查询串里经常带 ``?token=…``：把整条 URL 写进 ledger 等于把凭证抄进审计文件。
    启用 Webhook 时记的就是这个函数的结果。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"
