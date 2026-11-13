"""任务、评审与运行统计的领域模型（spec §5.1/§5.3/§5.5）。

本模块只做类型与边界校验；状态跃迁、额度耗尽等业务规则由
``TaskBoard``/状态机负责。时间字段一律用 naive 本地时间。
"""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.artifacts.spec import ArtifactRecord, ArtifactSpec


class TaskStatus(str, Enum):
    """任务状态；``str`` 混入保证序列化为裸字符串。"""

    PENDING = "pending"  # 依赖未满足，尚未进入调度
    READY = "ready"  # 依赖已满足，等待派发（含重试等待、返工等待）
    RUNNING = "running"  # 已派发给 Agent，执行中
    REVIEW = "review"  # Agent 已提交，正在过门禁
    REWORK = "rework"  # 被驳回，等待再次执行
    SUCCEEDED = "succeeded"  # 终态：通过全部门禁
    FAILED = "failed"  # 终态：重试/返工额度用尽，或不可恢复错误
    BLOCKED = "blocked"  # 上游永久失败导致本任务无法开始
    CANCELLED = "cancelled"  # 终态：用户取消
    SKIPPED = "skipped"  # 终态：上游被跳过导致本任务跳过


class StatusChange(BaseModel):
    """一次状态跃迁的审计记录。"""

    model_config = ConfigDict(extra="forbid")

    from_status: TaskStatus
    to_status: TaskStatus
    reason: str = ""
    at: datetime


class ReviewRecord(BaseModel):
    """一次评审裁决（静态 / LLM / 人工三层之一）。

    不在模型层校验「reject 必须带 issues」等业务规则，那是门禁的职责。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    round: int  # 第几轮评审（从 1 起）
    layer: Literal["static", "llm", "human", "supervisor"]
    verdict: Literal["approve", "reject"]
    score: float | None = None  # 0..1，仅 LLM 层有
    issues: list[str] = Field(default_factory=list)
    fix_hints: list[str] = Field(default_factory=list)
    reviewer: str  # "static:contract" / role key / "human:cli"
    created_at: datetime


class TaskResult(BaseModel):
    """任务执行结果：实际落盘的产物与 Agent 自述摘要。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    role: str
    summary: str  # Agent 自述摘要（进上下文包）
    artifacts: list[ArtifactRecord]
    notes: str = ""  # 围栏块之外的说明文字（不落盘）
    outcome: str | None = None
    """R4：Agent 用 ``<outcome>标签</outcome>`` 声明的结果标签。

    条件边（:attr:`Task.when_outcomes`）据此选择下游分支。``None`` = 未声明，
    此时所有条件边任务都按「未命中」处理；既有 run 与 mock 夹具天然兼容。
    """
    started_at: datetime
    finished_at: datetime
    duration_ms: int


class Task(BaseModel):
    """一个可调度任务（spec §5.1）。

    不含状态跃迁校验：``status`` 只能经 ``TaskBoard`` 修改。
    """

    model_config = ConfigDict(extra="forbid")

    id: str  # "T-001"，run 内唯一
    title: str
    description: str  # 发给 Agent 的任务描述
    role: str  # RoleSpec.key
    agent: str = ""
    """R59：规划期指派的执行体 profile key（空 = 沿用角色默认绑定）。

    PM 拆解时可为每个任务从执行体池里挑一个最合适的执行体（``role`` 决定干什么活、
    ``agent`` 决定谁来干）。派发优先级：**运行时人工覆盖 > 本字段 > 角色默认绑定**
    （见 ``AgentFactory.agent_for_task``）。非法指派在规划校验期被拒；运行期目标
    失效则丢指派、记 ``task_agent_assignment_dropped`` 并按角色默认继续。"""
    acceptance_criteria: list[str]  # 验收标准，供 G2 评审使用
    deliverables: list[ArtifactSpec]  # 交付物契约
    dependencies: list[str] = Field(default_factory=list)  # 前置任务 id
    parent_id: str | None = None  # V2：树状拆解的父任务 id（None = 根任务）
    dynamic: bool = False  # P2-1：是否运行时由 Agent 请求动态创建（vs 规划阶段静态）
    delegated_by: str = ""
    """R6：派发者角色 key（动态委派留痕；空串 = 不是委派产生的任务）。

    与 ``parent_id`` 的区别是「父任务可能被返工丢弃、角色不会」：事后看板想回答
    「这个活是谁派的」，只能靠这里。规划阶段的任务与既存 run 数据一律为空串。
    """
    reassign_count: int = 0
    """R10：主管驳回后「换个人做」的次数（默认 0 = 没换过）。

    复用既有的 ``REWORK → RUNNING`` 通道：只改 ``role``，不动状态机。上限由
    ``max_supervisor_reassignments`` 控制，用尽后驳回改回原执行者，避免无限换人。
    """
    priority: int = Field(default=5, ge=1, le=9)  # 数字小者优先
    brief: str = ""  # PM 写给下游的一句话摘要（供上下文压缩）
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    """P1-4：单次执行超时（秒），超时走 attempts 重试通道。

    ``ge=1`` / ``le=3600``：值来自 PM 的结构化输出，0 或负数会让
    ``asyncio.wait_for`` 立即超时、每次执行都烧一次 attempt；极大值则彻底
    丧失超时保护。构造期即拒绝，比在调度器里静默 clamp 更诚实。
    """
    when_outcomes: list[str] = Field(default_factory=list)
    """R4 条件边：只有上游 ``TaskResult.outcome`` 命中这里任一标签时本任务才执行。

    ``dependencies`` 仍是 AND 关系，但「条件性跳过」的上游被视为已满足 ——
    因此依赖多条互斥分支的汇合任务只要有一条分支成功就能继续。空列表（默认）
    = 无条件任务，行为与 R4 之前逐字一致。标签由 PM 在拆解时声明、由上游
    Agent 在回复里用 ``<outcome>`` 围栏给出，不引入表达式引擎或外部依赖。
    """

    # 运行时字段
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0  # 执行尝试次数
    rework_count: int = 0  # 返工次数
    max_attempts: int = 3  # 执行重试上限（含首次）
    max_reworks: int = 2  # 返工上限
    result: TaskResult | None = None
    reviews: list[ReviewRecord] = Field(default_factory=list)
    status_history: list[StatusChange] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class RunStats(BaseModel):
    """一次 run 的汇总统计。"""

    model_config = ConfigDict(extra="forbid")

    total_tasks: int = 0
    succeeded: int = 0
    failed: int = 0
    blocked: int = 0
    skipped: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    agent_costs: dict[str, dict[str, float]] = Field(default_factory=dict)
    """按执行体拆分的用量（profile → calls/cost_usd/prompt_tokens/…）。

    单执行体（未配置多 agent）时为空字典，与旧版 run.json 完全兼容。
    """
    started_at: datetime | None = None
    finished_at: datetime | None = None
