"""任务认知复杂度评估与模型分级路由（V2 第 2 项）。

不同任务的认知负荷差异很大：写 README 用便宜模型足够，架构设计需要更强
的推理能力。本模块把任务映射到 :class:`ComplexityLevel`，供
:class:`ModelFactory` 按级选模型。

评估规则是**启发式**的（不调用 LLM），依据可观测的任务属性：
角色权重、验收标准条数、描述长度、依赖数、交付物数。规则刻意简单可解释，
避免「评估本身需要一个大模型」的递归问题。

P3-1：静态评估之上再加一层**运行时校准**（:class:`ComplexityCalibrator`）。
启发式分数不知道「这一类任务在实际跑下来是不是被低估/高估了」——一个
静态被判 LOW 的技术写作任务若反复返工，说明它实际更接近 MEDIUM。校准器
按任务特征分桶累计实际成功率与平均尝试次数，据此把级别上调/下调一级。
没有历史数据时完全回落到静态 :func:`evaluate`，行为与改动前一致。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from agentcrew.task.task import Task


class ComplexityLevel(str, Enum):
    """任务复杂度分级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: 级别升降序：用于上调 / 下调一级。
_LEVEL_ORDER: tuple[ComplexityLevel, ...] = (
    ComplexityLevel.LOW,
    ComplexityLevel.MEDIUM,
    ComplexityLevel.HIGH,
)

#: 校准触发阈值（P3-1）。
#: 同类任务成功率低于此值，或平均尝试次数高于此值 → 上调一级。
_UP_SUCCESS_RATE = 0.5
_UP_AVG_ATTEMPTS = 1.5
#: 同类任务成功率高于此值且平均尝试次数恰为 1 → 下调一级。
_DOWN_SUCCESS_RATE = 0.9
_DOWN_AVG_ATTEMPTS = 1.0


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


# ── P3-1：运行时复杂度校准 ──────────────────────────────────────────────────


def _feature_key(task: Task) -> str:
    """任务特征桶键：角色 + 描述长度档 + 交付物数量档。

    校准只关心「这一类任务实际好不好做」，而不是单个任务 id —— 描述长度档
    （每 200 字符一档）与交付物数量档把形态相近的任务聚到同一桶，避免每个
    任务 id 各自只有一条样本、校准永远起不来。
    """
    desc_bucket = len(task.description) // 200
    return f"{task.role}|desc={desc_bucket}|deliverables={len(task.deliverables)}"


def _shift(level: ComplexityLevel, delta: int) -> ComplexityLevel:
    """在 :data:`_LEVEL_ORDER` 里平移 ``delta`` 级，越界夹在两端。"""
    idx = _LEVEL_ORDER.index(level)
    return _LEVEL_ORDER[max(0, min(len(_LEVEL_ORDER) - 1, idx + delta))]


class _BucketStats:
    """单个特征桶的累计结果（P3-1 校准输入）。"""

    __slots__ = ("count", "successes", "total_attempts")

    def __init__(self) -> None:
        self.count = 0
        self.successes = 0
        self.total_attempts = 0

    def record(self, success: bool, attempts: int) -> None:
        self.count += 1
        if success:
            self.successes += 1
        self.total_attempts += attempts

    @property
    def success_rate(self) -> float:
        return self.successes / self.count if self.count else 0.0

    @property
    def avg_attempts(self) -> float:
        return self.total_attempts / self.count if self.count else 0.0


class ComplexityCalibrator:
    """按任务特征桶累计实际执行结果，并据此静态复杂度上调/下调一级（P3-1）。

    调级规则（都在「有历史样本」时才生效）：

    - 同类任务成功率 < 50% **或** 平均尝试次数 > 1.5 → 上调一级
      （静态低估了它的难度，换更强模型）；
    - 同类任务成功率 > 90% **且** 平均尝试次数恰为 1 → 下调一级
      （静态高估了它，换更便宜模型）；
    - 其余情况（或无历史）→ 原样返回静态评估。

    上调优先于下调（两者同时成立时按失败处理更稳妥）。级别在 LOW/MEDIUM/HIGH
    两端夹断，不会越出三档。
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _BucketStats] = {}

    def record(self, task: Task, success: bool, attempts: int) -> None:
        """记录一次任务执行的实际结局（成功 / 失败 + 尝试次数）。"""
        key = _feature_key(task)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _BucketStats()
            self._buckets[key] = bucket
        bucket.record(success, attempts)

    def calibrate(self, task: Task) -> ComplexityLevel:
        """基于历史统计校准复杂度；无历史时回落静态 :func:`evaluate`。"""
        original = evaluate(task)
        bucket = self._buckets.get(_feature_key(task))
        if bucket is None or bucket.count == 0:
            return original
        if bucket.success_rate < _UP_SUCCESS_RATE or (
            bucket.avg_attempts > _UP_AVG_ATTEMPTS
        ):
            return _shift(original, +1)
        if (
            bucket.success_rate > _DOWN_SUCCESS_RATE
            and bucket.avg_attempts == _DOWN_AVG_ATTEMPTS
        ):
            return _shift(original, -1)
        return original

    def stats(self) -> dict[str, Any]:
        """全部桶的校准统计（调试 / ledger / 运行报告用）。"""
        return {
            key: {
                "count": bucket.count,
                "successes": bucket.successes,
                "success_rate": round(bucket.success_rate, 4),
                "avg_attempts": round(bucket.avg_attempts, 4),
            }
            for key, bucket in self._buckets.items()
        }


#: 模块级全局校准器单例（P3-1）。Crew 构造时经 :func:`set_calibrator` 注入；
#: :class:`~agentcrew.models.factory.ModelFactory` 在 ``alias_for_task`` 里经
#: :func:`get_calibrator` 读取。未注入（``None``）时工厂行为与改动前完全一致。
_default_calibrator: ComplexityCalibrator | None = None


def get_calibrator() -> ComplexityCalibrator | None:
    """返回全局校准器；未设置时返回 ``None``（工厂据此走静态评估）。"""
    return _default_calibrator


def set_calibrator(calibrator: ComplexityCalibrator | None) -> None:
    """设置 / 清空全局校准器（``None`` 表示恢复为静态评估）。"""
    global _default_calibrator
    _default_calibrator = calibrator
