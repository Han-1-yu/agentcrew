"""R4 条件分支的纯语义：结果标签、条件边、预期跳过（零上层依赖）。

把「边的有效性」从 :mod:`agentcrew.task.graph` 的纯拓扑算法里分出来，是这次
结构重构的关键一步：``TaskGraph`` 继续只回答「谁依赖谁」，本模块回答「在当前
上游结果下，这条边还算不算数」。

设计取舍（对齐 CrewAI Flows / LangGraph conditional edges，但不引入表达式引擎）：

- Agent 用 ``<outcome>标签</outcome>`` 声明本任务的结果标签，写进
  ``TaskResult.outcome``。标签是受约束的小写 slug，不是可执行表达式 ——
  调度决策依旧**全程无 LLM、无 eval**，确定性可测。
- 下游任务用 ``Task.when_outcomes`` 声明「只在命中这些标签时执行」。
- 未命中的分支不是失败：它以 ``condition-not-met`` 的原因进入 ``SKIPPED``，
  并沿分支传播 ``conditional-skip-propagated``。这两种 reason 让
  ``Crew._final_status`` 能把「预期跳过」与「上游失败导致的跳过」区分开。
- 汇合节点只要**至少有一个** ``SUCCEEDED`` 依赖就能继续；条件性 SKIPPED 的
  依赖被视为「已满足」。于是互斥分支天然收敛，不需要引入 OR 依赖语法。

本模块位于 ``task`` 层，只依赖标准库与同层 ``Task``，不 import
``runtime`` / ``crew`` / ``gate`` / ``agents``（架构护栏）。
"""

from __future__ import annotations

import re
from typing import Any

from agentcrew.task.task import Task, TaskStatus

#: 条件未命中时的跃迁原因。
CONDITION_SKIP_REASON = "condition-not-met"
#: 条件性跳过沿分支向下游传播时的跃迁原因。
CONDITION_SKIP_PROPAGATED = "conditional-skip-propagated"
#: 被终局判定视为「预期跳过」而非失败的全部 reason。
CONDITIONAL_SKIP_REASONS = frozenset(
    (CONDITION_SKIP_REASON, CONDITION_SKIP_PROPAGATED)
)

_OUTCOME_RE = re.compile(
    r"<outcome>\s*([^<]{1,64}?)\s*</outcome>", re.IGNORECASE
)
_OUTCOME_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: 上游状态视为「已到终局」的集合（条件判定时不再等待这些依赖）。
SETTLED_STATUSES = frozenset(
    (TaskStatus.SUCCEEDED, TaskStatus.FAILED,
     TaskStatus.CANCELLED, TaskStatus.SKIPPED)
)


def normalize_outcome(raw: Any) -> str | None:
    """把任意输入收敛成合法标签；非法 / 空 / 非字符串一律返回 ``None``。"""
    if not isinstance(raw, str):
        return None
    label = raw.strip().lower()
    if not label or _OUTCOME_LABEL_RE.fullmatch(label) is None:
        return None
    return label


def parse_outcome(text: str) -> str | None:
    """从 Agent 回复里取**最后一个** ``<outcome>`` 围栏的标签。

    取最后一个而不是第一个：模型可能在中间先试探着写一个、末尾再改主意；
    没有围栏或标签非法时返回 ``None``（= 不声明结果标签）。
    """
    if not isinstance(text, str):
        return None
    matches = _OUTCOME_RE.findall(text)
    if not matches:
        return None
    for raw in reversed(matches):
        label = normalize_outcome(raw)
        if label is not None:
            return label
    return None


def is_conditional_skip(task: Task) -> bool:
    """该任务是否因条件分支（而非上游失败）被跳过。"""
    if task.status is not TaskStatus.SKIPPED:
        return False
    return bool(
        task.status_history
        and task.status_history[-1].reason in CONDITIONAL_SKIP_REASONS
    )


def condition_matches(task: Task, upstream_outcomes: list[str | None]) -> bool:
    """``task`` 的条件边是否被上游结果命中。

    ``upstream_outcomes`` 按 ``task.dependencies`` 顺序给出每个上游的
    ``TaskResult.outcome``（未声明为 ``None``）。无 ``when_outcomes`` 的任务
    恒为 True（无条件任务）。
    """
    if not task.when_outcomes:
        return True
    declared = {normalize_outcome(item) for item in task.when_outcomes}
    declared.discard(None)
    if not declared:
        return True
    observed = {
        normalize_outcome(item) for item in upstream_outcomes
    }
    observed.discard(None)
    return bool(declared & observed)
