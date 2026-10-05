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
    layer: Literal["static", "llm", "human"]
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
    acceptance_criteria: list[str]  # 验收标准，供 G2 评审使用
    deliverables: list[ArtifactSpec]  # 交付物契约
    dependencies: list[str] = Field(default_factory=list)  # 前置任务 id
    priority: int = Field(5, ge=1, le=9)  # 数字小者优先
    brief: str = ""  # PM 写给下游的一句话摘要（供上下文压缩）

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
    started_at: datetime | None = None
    finished_at: datetime | None = None
