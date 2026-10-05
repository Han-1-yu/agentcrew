"""DAG 图算法：建图、环检测、拓扑序与就绪/阻塞集判定（spec §6.2）。

纯函数模块：无 I/O、无日志，不修改传入的 ``Task`` 对象，也不修改其
``dependencies`` 列表。``TaskBoard``（Task 8）、``planner``（Task 9）与
调度器（Task 28）共用本模块的结果，故所有输出都保持确定性。

方向约定：边 ``dep -> task`` 表示 ``task`` 依赖 ``dep``（``dep`` 先跑）。
``deps[task]`` 是直接前置，``dependents[task]`` 是直接后继；
``cycles()`` / ``topo_order()`` 返回的 id 序列都沿 ``dependents`` 方向。

接口细节：brief 同时要求「``dependents`` 字段是 ``dict``」与「方法
``dependents(task_id)``」，而 Python 里实例属性必然遮蔽同名方法。为同时满足
两种用法，该字段用 :class:`_DependentsIndex`——一个 ``dict`` 子类，既可按
``g.dependents[id]`` 访问全图映射，也可按 ``g.dependents(id)`` 取单点。
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from agentcrew.task.task import Task, TaskStatus

#: 一旦前置处于这些状态，下游 PENDING 任务就不可能再开工（转入 BLOCKED）。
_BLOCKING_DEP_STATES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def _is_succeeded(tasks: Mapping[str, Task], dep: str) -> bool:
    """``dep`` 是否已是 ``SUCCEEDED``；不在 ``tasks`` 里的一律不算满足。"""
    task = tasks.get(dep)
    return task is not None and task.status == TaskStatus.SUCCEEDED


def _is_blocking_dep(tasks: Mapping[str, Task], dep: str) -> bool:
    """``dep`` 是否已永久失败/被取消；不在 ``tasks`` 里的一律不算。"""
    task = tasks.get(dep)
    return task is not None and task.status in _BLOCKING_DEP_STATES


class _DependentsIndex(dict[str, frozenset[str]]):
    """``dependents`` 字段的类型：是 ``dict``，同时可当方法调用。

    见模块 docstring：brief 要求 ``g.dependents["A"]`` 与 ``g.dependents("A")``
    都可用，而实例属性会遮蔽同名方法，故用一个可调用的 ``dict`` 子类调和。
    """

    def __call__(self, task_id: str) -> frozenset[str]:
        """``task_id`` 的直接后继；未在图中出现过的 id 抛 ``KeyError``。"""
        return self[task_id]


class CyclicGraphError(ValueError):
    """图存在环、无法给出拓扑序；``cycles`` 携带检测到的全部环。"""

    def __init__(self, cycles: list[list[str]]) -> None:
        self.cycles = cycles
        super().__init__(f"任务图存在环，无法拓扑排序：{cycles}")


@dataclass(frozen=True)
class TaskGraph:
    """任务依赖图；经 :meth:`build` 构造。

    两张表的键都覆盖图中出现过的每个 id（含只作为依赖出现、自身不在
    ``tasks`` 里的悬挂 id）：无前置的节点映射到 ``frozenset()``。
    """

    deps: dict[str, frozenset[str]]
    dependents: _DependentsIndex

    @classmethod
    def build(cls, tasks: Sequence[Task]) -> TaskGraph:
        """由任务序列建图；重复依赖自动去重。

        依赖里出现、但自身不在 ``tasks`` 中的 id（悬挂依赖）按「无前置的
        节点」收录：它是两张表的键（``deps`` 为空），并保有出边。本模块既不
        崩、也不替它伪造前置——悬挂依赖的校验是 planner（Task 9）的职责，
        而只要它没被满足，:func:`ready_ids` 就不会放行下游任务。
        """
        deps: dict[str, set[str]] = {}
        dependents: dict[str, set[str]] = {}
        for task in tasks:
            deps.setdefault(task.id, set())
            dependents.setdefault(task.id, set())
        for task in tasks:
            for dep in task.dependencies:
                deps.setdefault(dep, set())
                dependents.setdefault(dep, set())
                deps[task.id].add(dep)
                dependents[dep].add(task.id)
        return cls(
            deps={node: frozenset(edges) for node, edges in deps.items()},
            dependents=_DependentsIndex(
                {node: frozenset(edges) for node, edges in dependents.items()}
            ),
        )

    def dependencies(self, task_id: str) -> frozenset[str]:
        """``task_id`` 的直接前置；未在图中出现过的 id 抛 ``KeyError``。"""
        return self.deps[task_id]

    def descendants(self, task_id: str) -> set[str]:
        """``task_id`` 的全部传递后继，不含自身；未知 id 抛 ``KeyError``。

        带已访集合的迭代 DFS：有环输入也保证终止；输出与发现顺序无关。
        """
        seen: set[str] = {task_id}
        stack = list(self.dependents[task_id])
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self.dependents[node])
        return seen - {task_id}

    def cycles(self) -> list[list[str]]:
        """返回图中的**全部**简单环。

        先用 Kahn 剥离入度为 0 的节点，剩下的节点集合 = 所有环上的节点 ∪
        环的下游节点；再在剩余子图里枚举简单环。每个环从自身最小 id 出发、
        只走 id 不小于起点的节点，因此每个环恰好报告一次，且：
        - 自环 ``A -> A`` 返回 ``[["A"]]``；
        - 互不相连的多个环全部返回（不会只报第一个）；
        - 挂在环下游的无环节点不会出现在任何环里；
        - 每个条目的相邻两项之间都有 ``dependents`` 方向的边，末项接回首项。

        开销：输出规模本身由简单环数量决定（最坏可指数级）；枚举部分在
        计划规模（几十个任务）下可忽略。超大图（数千节点的长环）的*报错*
        路径会退化为 O(V²)，正常路径（:func:`ready_ids` / :meth:`topo_order`
        的无环分支）不受影响。
        """
        return self._enumerate_cycles(self._cyclic_nodes())

    def topo_order(self) -> list[str]:
        """Kahn 拓扑序；同一时刻就绪的节点按 id 升序出队，结果完全确定。

        有环（含自环）时抛 :class:`CyclicGraphError`，``.cycles`` 携带全部环。
        每个节点至多入堆一次，任何输入都不会死循环。
        """
        indegree: dict[str, int] = {
            node: len(edges) for node, edges in self.deps.items()
        }
        heap: list[str] = [node for node, degree in indegree.items() if degree == 0]
        heapq.heapify(heap)
        order: list[str] = []
        while heap:
            node = heapq.heappop(heap)
            order.append(node)
            for dependent in self.dependents[node]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(heap, dependent)
        if len(order) != len(self.deps):
            raise CyclicGraphError(self.cycles())
        return order

    def _cyclic_nodes(self) -> set[str]:
        """Kahn 剥离后剩下的节点：环上的节点，以及从环可达的节点。"""
        indegree: dict[str, int] = {
            node: len(edges) for node, edges in self.deps.items()
        }
        ready: list[str] = [node for node, degree in indegree.items() if degree == 0]
        remaining: set[str] = set(self.deps)
        while ready:
            node = ready.pop()
            remaining.discard(node)
            for dependent in self.dependents[node]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        return remaining

    def _enumerate_cycles(self, remaining: set[str]) -> list[list[str]]:
        """在 ``remaining`` 子图内枚举全部简单环（显式栈 DFS，不用递归）。

        限定起点为路径最小 id 且只走 id ≥ 起点的节点：每个简单环只从其
        最小 id 节点被发现一次，既去重又让输出顺序稳定。
        """
        cycles: list[list[str]] = []
        for start in sorted(remaining):
            path: list[str] = [start]
            on_path: set[str] = {start}
            # 不变式：len(stack) == len(path)，栈顶迭代器对应 path[-1] 的出边。
            stack: list[Iterator[str]] = [iter(sorted(self.dependents[start]))]
            while stack:
                try:
                    nxt = next(stack[-1])
                except StopIteration:
                    stack.pop()
                    on_path.discard(path.pop())
                    continue
                if nxt not in remaining or nxt < start:
                    continue
                if nxt == start:
                    cycles.append(list(path))  # path 是简单路径，接回起点即成环
                elif nxt not in on_path:
                    path.append(nxt)
                    on_path.add(nxt)
                    stack.append(iter(sorted(self.dependents[nxt])))
        return cycles


def ready_ids(tasks: Mapping[str, Task]) -> list[str]:
    """返回可立即派发的任务 id，按 id 升序。

    判据：``status == PENDING`` 且**全部**依赖都已 ``SUCCEEDED``。依赖处于
    其他任何状态（``SKIPPED`` / ``RUNNING`` / ``CANCELLED`` / 终态失败……）
    或根本不在 ``tasks`` 里，都不算满足——绝不冒险派发。``SKIPPED`` 向下游
    的传播由 Task 8 的 ``TaskBoard`` 负责，本模块不做。
    """
    ready: list[str] = []
    for task_id in sorted(tasks):
        task = tasks[task_id]
        if task.status != TaskStatus.PENDING:
            continue
        if all(_is_succeeded(tasks, dep) for dep in task.dependencies):
            ready.append(task_id)
    return ready


def blocked_ids(tasks: Mapping[str, Task]) -> list[str]:
    """返回已被上游拖死的 PENDING 任务 id，按 id 升序。

    判据：``status == PENDING`` 且有任一依赖处于 ``FAILED`` 或 ``CANCELLED``。
    与 :func:`ready_ids` 是**不同**的谓词：``SKIPPED`` 依赖既不算就绪也不算
    阻塞（其传播同样由 Task 8 的 ``TaskBoard`` 负责）。
    """
    blocked: list[str] = []
    for task_id in sorted(tasks):
        task = tasks[task_id]
        if task.status != TaskStatus.PENDING:
            continue
        if any(_is_blocking_dep(tasks, dep) for dep in task.dependencies):
            blocked.append(task_id)
    return blocked
