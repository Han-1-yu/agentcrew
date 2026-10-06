"""任务认知复杂度评估与模型分级路由（V2 第 2 项）。

不同任务的认知负荷差异很大：写 README 用便宜模型足够，架构设计需要更强
的推理能力。本模块把任务映射到 :class:`ComplexityLevel`，供
:class:`ModelFactory` 按级选模型。

评估规则是**启发式**的（不调用 LLM），依据可观测的任务属性：
角色权重、验收标准条数、描述长度、依赖数、交付物数。规则刻意简单可解释，
避免「评估本身需要一个大模型」的递归问题。
"""

from __future__ import annotations

from enum import Enum

from agentcrew.task.task import Task


class ComplexityLevel(str, Enum):
    """任务复杂度分级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: 角色 → 基础复杂度分。架构师和 PM 天然更复杂，文档/部署较低。
_ROLE_BASE_SCORE: dict[str, int] = {
    "architect": 3,
    "project_manager": 2,
    "product_manager": 2,
    "backend_dev": 2,
    "frontend_dev": 1,
    "qa_engineer": 1,
    "devops_engineer": 1,
    "technical_writer": 0,
}

#: 每条验收标准加的分。
_CRITERIA_WEIGHT = 1
#: 每个依赖加的分（依赖多 = 需要理解更多上游）。
_DEPENDENCY_WEIGHT = 1
#: 每个交付物加的分。
_DELIVERABLE_WEIGHT = 1
#: 描述每 200 字符加 1 分（上限 3 分）。
_DESC_CHARS_PER_POINT = 200
_DESC_MAX_POINTS = 3

#: 分数阈值：< LOW_THRESHOLD → LOW，< HIGH_THRESHOLD → MEDIUM，否则 HIGH。
_LOW_THRESHOLD = 4
_HIGH_THRESHOLD = 8


def evaluate(task: Task) -> ComplexityLevel:
    """评估一个任务的复杂度级别。

    纯函数、无 I/O、确定性：同一任务永远返回同一级别。
    """
    score = _ROLE_BASE_SCORE.get(task.role, 1)
    score += len(task.acceptance_criteria) * _CRITERIA_WEIGHT
    score += len(task.dependencies) * _DEPENDENCY_WEIGHT
    score += len(task.deliverables) * _DELIVERABLE_WEIGHT
    score += min(_DESC_MAX_POINTS, len(task.description) // _DESC_CHARS_PER_POINT)

    if score < _LOW_THRESHOLD:
        return ComplexityLevel.LOW
    if score < _HIGH_THRESHOLD:
        return ComplexityLevel.MEDIUM
    return ComplexityLevel.HIGH
