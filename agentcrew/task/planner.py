"""PM 拆解结果的模型、校验与 T-FINAL 追加（spec §7.0/§7.2）。

本模块是 LLM 计划与执行引擎之间的闸门：PM 的自由拆解先经 ``PlanOutput``
结构化，再由 :func:`validate_plan` 按 spec §7.2 的 7 条规则校验，最后由
:func:`with_final_task` 追加收尾任务 ``T-FINAL``。**校验一次收集全部错误**
（不早退）：Task 29 的 Crew 把 ``PlanValidationError.errors`` 整体回灌给 PM
重新拆解，逐条修复会让规划轮次退化成串行。

职责边界：``to_tasks`` 只做字段映射、不做校验（Task 29 按
``to_tasks`` → ``validate_plan`` 的顺序调用）；校验 6（路径安全）已由
``ArtifactSpec.path`` 的 validator 经 ``safe_relpath`` 保证，此处不重复实现。
"""

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.task.graph import TaskGraph
from agentcrew.task.task import Task

#: 收尾任务 id；PM 提示词要求不要自行生成，由 :func:`with_final_task` 追加。
FINAL_TASK_ID = "T-FINAL"

_FINAL_TASK_TITLE = "最终交付总结"
_FINAL_TASK_DESCRIPTION = (
    "汇总本次全部任务的交付成果，撰写 DELIVERY.md：完成清单、验收结论、"
    "遗留问题与运行方式。"
)
_FINAL_TASK_ACCEPTANCE_CRITERIA = ["列出全部完成项与遗留问题", "包含运行方式"]
_FINAL_TASK_PRIORITY = 9  # 数字小者优先 → 最后执行
_FINAL_TASK_BRIEF = "收尾任务：汇总全部交付物并给出运行方式"


class PlanValidationError(ValueError):
    """拆解方案未通过 spec §7.2 的校验；``errors`` 携带**全部**具体错误。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


#: 空拆解的错误消息（PM 交回 ``{"tasks": []}`` 时）。
_EMPTY_PLAN_ERROR = (
    "拆解结果为空：必须至少有一个真实任务 —— 引擎追加的收尾任务 T-FINAL 只负责"
    "汇总，空计划会产出一次只有 DELIVERY.md 的 run（那不是交付）"
)


def _drafts(tasks: Sequence[Task]) -> list[Task]:
    """拆解里**真实的**任务（``T-FINAL`` 是平台保留 id，由引擎追加，不算数）。"""
    return [task for task in tasks if task.id != FINAL_TASK_ID]


class TaskDraft(BaseModel):
    """PM 返回的单个任务草案；与 ``Task`` 的差异是只含 PM 能决定的字段。

    ``priority`` 的边界与 ``Task`` 保持一致（1..9）：越界必须在**解析期**被
    ``PlanOutput`` 拦下（Task 20 的结构化输出阶梯会带着校验错误重试），而不是
    留到 :func:`to_tasks` 抛 pydantic ``ValidationError`` —— 那会逃出 Task 29
    的 ``except PlanValidationError``，把可修复的拆解问题变成整次 run 崩溃。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    role: str  # RoleSpec.key
    acceptance_criteria: list[str]
    deliverables: list[ArtifactSpec]
    dependencies: list[str] = Field(default_factory=list)
    priority: int = Field(5, ge=1, le=9)
    brief: str = ""


class PlanOutput(BaseModel):
    """PM 结构化输出的顶层对象：一次拆解的全部任务草案。"""

    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskDraft]


def plan_output_json_schema() -> dict[str, Any]:
    """返回 ``PlanOutput`` 的 JSON Schema，供 PM 提示词与结构化输出层使用。"""
    return PlanOutput.model_json_schema()


def to_tasks(plan: PlanOutput) -> list[Task]:
    """把拆解草案映射为 ``Task``；运行时字段（状态、计数、评审）保持默认。

    只做映射、不做校验：非法拆解由 :func:`validate_plan` 单独判定。
    ``TaskDraft`` 的每个字段都与 ``Task`` 同约束或更严（``priority`` 已对齐
    1..9，路径安全在 ``ArtifactSpec`` 构造时完成），因此只要 ``PlanOutput``
    解析通过，本函数不会再抛异常。
    """
    return [
        Task(
            id=draft.id,
            title=draft.title,
            description=draft.description,
            role=draft.role,
            acceptance_criteria=list(draft.acceptance_criteria),
            deliverables=list(draft.deliverables),
            dependencies=list(draft.dependencies),
            priority=draft.priority,
            brief=draft.brief,
        )
        for draft in plan.tasks
    ]


def validate_plan(tasks: Sequence[Task], catalog: RoleCatalog) -> None:
    """按 spec §7.2 校验拆解方案；任一条不过则抛 :class:`PlanValidationError`。

    收集全部错误后一次性抛出，消息为中文且指名具体的任务 id / 角色 / 路径，
    便于直接回灌 PM。8 条校验：

    0. 计划非空（``_EMPTY_PLAN_ERROR``）：空列表过去**通过**，于是
       ``with_final_task`` 只追加一个 ``T-FINAL``，run 交出一个只有
       ``DELIVERY.md`` 的「成功」结局 —— 空计划必须是错误，不是交付（I6）；
    1. ``id`` 非空且唯一（重复的每个 id 各报一条）；
    2. ``role`` 命中 ``catalog``；
    3. 依赖 id 存在于本次拆解、且不自引用；
    4. ``TaskGraph.cycles()`` 为空；
    5. 产物路径跨任务、任务内都两两唯一；
    6. 路径安全 —— 已由 ``ArtifactSpec`` 的 validator 保证，此处不重复；
    7. ``acceptance_criteria`` 非空。

    ``tasks`` 为 ``Sequence`` 而非 ``list``：Task 25/31 会传入元组或切片。
    """
    errors: list[str] = []
    if not _drafts(tasks):
        errors.append(_EMPTY_PLAN_ERROR)
    errors.extend(_check_ids(tasks))
    errors.extend(_check_roles(tasks, catalog))
    errors.extend(_check_dependencies(tasks))
    errors.extend(_check_cycles(tasks))
    errors.extend(_check_artifact_paths(tasks))
    errors.extend(_check_acceptance_criteria(tasks))
    if errors:
        raise PlanValidationError(errors)


def _check_ids(tasks: Sequence[Task]) -> list[str]:
    """校验 1：id 非空且唯一；重复 id 逐个报出，不只报第一个。"""
    errors: list[str] = []
    counts: dict[str, int] = {}
    for index, task in enumerate(tasks):
        if not task.id.strip():
            errors.append(f"第 {index + 1} 个任务的 id 为空，每个任务必须有非空 id")
            continue
        counts[task.id] = counts.get(task.id, 0) + 1
    for task_id, count in counts.items():
        if count > 1:
            errors.append(f"任务 id 重复：{task_id} 出现 {count} 次，id 必须在本次拆解中唯一")
    return errors


def _check_roles(tasks: Sequence[Task], catalog: RoleCatalog) -> list[str]:
    """校验 2：role 必须命中已注册角色；报错时附上可用 key 供 PM 修正。"""
    available = "、".join(catalog.keys()) or "（目录为空）"
    return [
        f"任务 {task.id} 使用了未注册的角色 {task.role}；可用角色：{available}"
        for task in tasks
        if not catalog.has(task.role)
    ]


def _check_dependencies(tasks: Sequence[Task]) -> list[str]:
    """校验 3：依赖必须在本次拆解中、且不得自引用。"""
    errors: list[str] = []
    known_ids = {task.id for task in tasks}
    for task in tasks:
        seen: set[str] = set()
        for dep in task.dependencies:
            if dep in seen:  # 同一依赖写两遍只报一条
                continue
            seen.add(dep)
            if dep == task.id:
                errors.append(f"任务 {task.id} 依赖了自身，请改依赖其他任务")
            elif dep not in known_ids:
                errors.append(f"任务 {task.id} 的依赖 {dep} 不在本次拆解中")
    return errors


def _check_cycles(tasks: Sequence[Task]) -> list[str]:
    """校验 4：无环；每个环各报一条并列出环路径。"""
    return [
        f"任务图存在环：{' → '.join([*cycle, cycle[0]])}"
        for cycle in TaskGraph.build(tasks).cycles()
    ]


def _check_artifact_paths(tasks: Sequence[Task]) -> list[str]:
    """校验 5：产物路径跨任务、任务内都唯一；消息指名冲突双方。

    ``ArtifactSpec`` 构造时已把路径归一化，故直接比较即可。跨任务的归属表按
    id 记账：两个任务共用同一 id 时这里不重复报冲突——校验 1 已指出 id 重复。
    """
    errors: list[str] = []
    owner: dict[str, str] = {}
    for task in tasks:
        local: set[str] = set()
        for artifact in task.deliverables:
            path = artifact.path
            if path in local:
                errors.append(f"产物路径冲突：{path} 在任务 {task.id} 内被重复声明")
                continue
            local.add(path)
            first = owner.get(path)
            if first is None or first == task.id:
                owner[path] = task.id
            else:
                errors.append(f"产物路径冲突：{path} 被 {first} 与 {task.id} 同时声明")
    return errors


def _check_acceptance_criteria(tasks: Sequence[Task]) -> list[str]:
    """校验 7：acceptance_criteria 非空（G2 评审的判据来源）。"""
    return [
        f"任务 {task.id} 缺少验收标准：acceptance_criteria 不能为空"
        for task in tasks
        if not task.acceptance_criteria
    ]


def with_final_task(tasks: Sequence[Task]) -> list[Task]:
    """追加收尾任务 ``T-FINAL``，返回新列表（不修改输入）。

    ``T-FINAL`` 由 ``project_manager`` 承担，依赖**全部叶子任务**（没有任何
    其他任务依赖它的任务，经 ``TaskGraph.dependents`` 计算），产物为
    ``DELIVERY.md``，``priority=9`` 保证最后执行。幂等：若输入已含 ``T-FINAL``
    则先摘掉再重建，重复调用不会出现两个收尾任务或自依赖。

    ``tasks`` 里没有**真实**任务时抛 :class:`PlanValidationError`（I6）：这是
    唯一看得见「追加之前那份计划」的地方，而三个入口（Crew 拆解、G3 计划编辑、
    CLI 编辑器校验）都从这里过 —— 空计划在这里被拦下，就不会变成一次只有
    ``DELIVERY.md`` 的「成功」run。只含 ``T-FINAL`` 的输入同理（它会被摘掉）。
    """
    base = _drafts(tasks)
    if not base:
        raise PlanValidationError([_EMPTY_PLAN_ERROR])
    graph = TaskGraph.build(base)
    leaves = [task.id for task in base if not graph.dependents(task.id)]
    final = Task(
        id=FINAL_TASK_ID,
        title=_FINAL_TASK_TITLE,
        description=_FINAL_TASK_DESCRIPTION,
        role="project_manager",
        acceptance_criteria=list(_FINAL_TASK_ACCEPTANCE_CRITERIA),
        deliverables=[
            ArtifactSpec(
                path="DELIVERY.md",
                kind="markdown",
                description="交付总结",
                must_contain=["完成"],
                min_bytes=100,
            )
        ],
        dependencies=leaves,
        priority=_FINAL_TASK_PRIORITY,
        brief=_FINAL_TASK_BRIEF,
    )
    return [*base, final]
