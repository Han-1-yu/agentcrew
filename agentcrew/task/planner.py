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

import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.task.conditions import normalize_condition
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
    timeout_seconds: int = 300  # P1-4：PM 可指定单次执行超时（秒），默认 300
    when_outcomes: list[str] = Field(default_factory=list)
    """R4 条件边：只有上游 ``<outcome>`` 标签命中这里任一值时本任务才执行。

    空列表（默认）= 无条件任务。字段会随 ``PlanOutput.model_json_schema()``
    自动进入 PM 的结构化输出约束，无需手写第二份提示词里的字段清单。
    """


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
            timeout_seconds=draft.timeout_seconds,
            when_outcomes=list(draft.when_outcomes),
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
    7. ``acceptance_criteria`` 非空；
    8. 要求文档的验收标准必须有对应的文档类交付物（见
       :func:`_check_criteria_have_deliverables`）。

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
    errors.extend(_check_criteria_have_deliverables(tasks))
    errors.extend(_check_condition_edges(tasks))
    if errors:
        raise PlanValidationError(errors)


def _check_condition_edges(tasks: Sequence[Task]) -> list[str]:
    """R4：条件边的标签形状与「必须有上游」两条约束。

    只在 PM 真的声明了 ``when_outcomes`` 时触发；既有拆解（字段缺省）不会
    产生任何新错误，也不改变既有错误文案。
    """
    errors: list[str] = []
    for task in _drafts(tasks):
        if not task.when_outcomes:
            continue
        if not task.dependencies:
            errors.append(
                f"任务 {task.id!r} 声明了 when_outcomes 但没有 dependencies："
                "条件边必须挂在至少一个上游任务之后"
            )
        for raw in task.when_outcomes:
            if normalize_condition(raw) is None:
                errors.append(
                    f"任务 {task.id!r} 的 when_outcomes 含非法条件 {raw!r}："
                    "允许小写字母/数字/下划线/连字符的标签（1–64），"
                    "或否定写法 !label，或兜底写法 *"
                )
    return errors


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


#: 验收标准里出现这些词，就认为该标准**要求一份文档**。这是定向的 lint 关键词表，
#: 不是通用语义分析 —— 它针对的是一个实测到、且会让整条交付链死掉的失败类。
#:
#: **不要加 ``docs``**：它是 ``docs/api-spec.yaml`` 这类**路径**的常见前缀，裸子串
#: 匹配会把「全局路径与 docs/api-spec.yaml 一致」这种标准误判成「要求文档」。实测
#: 就这么误伤过演示夹具的 T-003。``文档``/``说明``/``documentation`` 已覆盖原意。
_DOC_DEMANDING_KEYWORDS = (
    "说明", "文档", "手册", "指南", "readme", "documentation", "manual", "guide",
)

#: ``说明`` 后接这些词时，它是**动词**（「说明参数与返回值」），不是在要一份文档。
#: 依据：真实 run 里 PM 写过一条「add 函数带有说明参数与返回值的 docstring」，
#: 裸子串匹配把它当成「要求文档」，而该任务只有 ``.py`` 交付物 —— 合法计划被拒、
#: 重拆几次措辞仍类似，最后整条 run 以退出码 2 死掉。名词用法不受影响：
#: 「启动说明」「使用说明」「接口说明」后面跟的是标点或结尾，照旧命中。
_EXPLAIN_VERB_OBJECTS = (
    "参数", "返回值", "用法", "作用", "原因", "理由", "设计", "步骤", "思路",
    "字段", "行为", "格式", "示例", "算法", "依赖关系",
)

#: 标准要求的是**代码内**的说明（docstring / 注释），不是独立文档文件。
#: 这类要求由代码本身就能满足，不该逼着任务额外交一份 markdown。
_IN_CODE_DOC_HINTS = ("docstring", "注释", "代码内", "行内")

#: 能充当「文档」的产物类型。
_DOC_ARTIFACT_KINDS = frozenset({"markdown", "text", "html"})

#: 但 ``kind=text`` 不都是文档：依赖清单与锁文件既不是说明、也证明不了「启动方法」。
#: 真实运行里 ``T-BACKEND`` 正是靠一份 ``backend/requirements.txt``（kind=text）
#: 让「已有文档交付物」成立，从而躲过检查的 —— 而它确实没有启动说明。
_NOT_A_DOCUMENT = (
    "requirements.txt", "requirements-dev.txt", "package.json", "package-lock.json",
    "pyproject.toml", "pipfile", "pipfile.lock", "poetry.lock", "cargo.toml",
    "go.mod", "pom.xml", "build.gradle",
)


def _demands_document(criterion: str) -> str | None:
    """这条验收标准是否在**要求一份文档**？返回命中的关键词，否则 ``None``。

    比裸子串匹配多做两件事，都是被真实运行逼出来的：

    1. ``说明`` 作动词时跳过（后接 ``参数`` / ``返回值`` / ``用法`` …）——
       「docstring 说明参数与返回值」是要代码内的注释，不是要一份 markdown；
    2. 标准里出现 ``docstring`` / ``注释`` 时跳过——这类要求代码本身即可满足。

    其余关键词（``文档`` / ``手册`` / ``指南`` / ``readme`` …）没有这种动宾歧义，
    仍是直接命中。
    """
    lowered = criterion.lower()
    if any(hint in lowered for hint in _IN_CODE_DOC_HINTS):
        return None
    for word in _DOC_DEMANDING_KEYWORDS:
        start = lowered.find(word)
        while start != -1:
            if word != "说明":
                return word
            tail = lowered[start + len(word): start + len(word) + 4]
            if not any(tail.startswith(obj) for obj in _EXPLAIN_VERB_OBJECTS):
                return word
            start = lowered.find(word, start + 1)
    return None


def _counts_as_document(path: str, kind: str) -> bool:
    """该产物能否用来证明一条「需要文档」的验收标准。"""
    if kind not in _DOC_ARTIFACT_KINDS:
        return False
    name = path.rsplit("/", 1)[-1].lower()
    return name not in _NOT_A_DOCUMENT


def _check_criteria_have_deliverables(tasks: Sequence[Task]) -> list[str]:
    """校验 8：要求文档的验收标准，必须有一份文档类交付物来证明它。

    **这是针对实测失败类的定向检查，不是通用语义分析。** 真实模型运行
    （``output/realrun6``）里 ``T-BACKEND`` 的验收标准第 7 条要求「提供启动方法与
    依赖说明」，但它的交付物只声明了 ``backend/app.py`` 与 ``backend/requirements.txt``。
    于是 agent 被夹死在两条规则之间：

    - 写出 ``backend/README.md`` → G1 判「产出未在契约中声明的文件」而驳回；
    - 不写 → G2 按第 7 条判「缺少启动说明」而驳回。

    **两条路都堵死，该任务永远不可能通过。** 证据吻合：那条问题在**三轮评审里每轮
    都在**（其余问题在被逐个修掉，所以问题数 7→12→8 在变），返工额度烧光后
    ``FAILED``，下游 5 个任务全部阻塞。

    根因在计划期：PM 写了一条它的交付物契约无法满足的验收标准，而校验里没有
    任何一条会发现这件事。检查很窄 —— 标准里出现文档类词、且该任务的 deliverables
    里没有任何 ``markdown``/``text``/``html`` → 报错，交回 PM 重新拆解。
    """
    errors: list[str] = []
    for task in tasks:
        if any(_counts_as_document(spec.path, spec.kind) for spec in task.deliverables):
            continue
        for criterion in task.acceptance_criteria:
            hit = _demands_document(criterion)
            if hit is None:
                continue
            errors.append(
                f"任务 {task.id} 的验收标准要求文档（含「{hit}」）：{criterion!r}，"
                f"但该任务的交付物里没有任何 markdown/text/html 文件 —— "
                f"写出文档会被 G1 判「未在契约中声明」而驳回，不写又被这条标准驳回，"
                f"该任务不可能通过。请为它补一个文档类交付物，或删掉这条标准。"
            )
    return errors


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


# ── P2-6：计划质量评分（多维度，低分只警告不阻断）───────────────────────────

#: 综合分低于此值记 ``plan_low_quality`` 警告（校验已通过，故**不**阻断执行）。
MIN_PLAN_SCORE = 0.5

#: specificity 归一化的标定：平均描述长度达到该字符数记满分。
_SPEC_DESC_TARGET = 100.0
#: specificity 归一化的标定：平均验收标准条数达到该数记满分。
_SPEC_CRITERIA_TARGET = 2.0

#: 综合分四维权重（与 spec 给的口径一致）：覆盖 0.3 / 具体 0.2 / 依赖 0.2 / 交付 0.3。
_W_COVERAGE = 0.3
_W_SPECIFICITY = 0.2
_W_DEPENDENCY = 0.2
_W_DELIVERABLE = 0.3

_ASCII_TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_\-]*")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


class PlanScore(BaseModel):
    """一次拆解的多维度质量评分（P2-6）。

    四个维度都归一到 ``0..1``，``total`` 是四维加权平均。``details`` 保留
    中间量（关键词命中数、平均描述长度、环数等），供 ``plan_low_quality``
    事件与计划卡点展示，避免「一个总分」黑箱。
    """

    model_config = ConfigDict(extra="forbid")

    total: float  # 0-1 综合分
    coverage: float  # 任务覆盖度（是否覆盖项目目标各方面）
    specificity: float  # 描述具体度（描述长度、验收标准数量）
    dependency_quality: float  # 依赖关系质量（是否有循环依赖、是否合理）
    deliverable_clarity: float  # 交付物清晰度（必需产物比例、路径规范性）
    details: dict[str, Any]  # 详细评分信息


def _goal_keywords(goal: str) -> list[str]:
    """从 goal 抽取待覆盖的关键词：ASCII 词（≥2 字符）+ 中文二元滑窗。

    中文没有空格分词，二元滑窗是最简单的「覆盖面」近似 —— 它不做语义分析，
    只衡量任务正文里是否反复出现 goal 里的字组。去重保序。
    """
    keys: list[str] = []
    for word in _ASCII_TOKEN.findall(goal):
        if len(word) >= 2:
            keys.append(word.lower())
    for run in _CJK_RUN.findall(goal):
        if len(run) >= 2:
            keys.extend(run[i : i + 2] for i in range(len(run) - 1))
        elif run:
            keys.append(run)
    return list(dict.fromkeys(keys))


def _path_normative(path: str) -> bool:
    """产物路径是否为干净的 POSIX 相对路径（无盘符/反斜杠/回退段）。"""
    if not path or path.startswith("/") or "\\" in path:
        return False
    return ".." not in path.split("/")


def score_plan(tasks: Sequence[Task], goal: str) -> PlanScore:
    """对一份拆解做多维度质量评分（P2-6）。

    只评**真实任务**（``T-FINAL`` 是平台追加的收尾任务，它永远有交付物、有依赖，
    算进去会把任何计划的 ``deliverable_clarity`` 抬高）。四个维度：

    - ``coverage``：goal 关键词在任务标题+正文里出现的比例（无关键词时给 1.0）；
    - ``specificity``：平均描述长度（标定 100 字符）与平均验收标准条数（标定 2 条）
      各占一半；
    - ``dependency_quality``：有环 = 0；无环且至少有一条依赖边 = 1；完全无依赖
      的散点计划 = 0.5（没有任何结构，下游无法复用上游产物）；
    - ``deliverable_clarity``：有交付物的任务比例（0.5）+ 必需交付物比例（0.25）
      + 路径规范比例（0.25）。

    ``total`` 按 0.3/0.2/0.2/0.3 加权。本函数**只读**：不修改输入，也不抛异常
    （评分是观测，不是校验 —— 非法拆解早在 :func:`validate_plan` 就被拦下）。
    """
    drafts = _drafts(tasks)

    # ── coverage ──
    keywords = _goal_keywords(goal)
    corpus = " ".join(f"{task.title} {task.description}" for task in drafts).lower()
    if keywords:
        covered = [key for key in keywords if key in corpus]
        coverage = len(covered) / len(keywords)
    else:
        covered = []
        coverage = 1.0

    # ── specificity ──
    if drafts:
        avg_desc = sum(len(task.description) for task in drafts) / len(drafts)
        avg_crit = sum(len(task.acceptance_criteria) for task in drafts) / len(drafts)
    else:
        avg_desc = 0.0
        avg_crit = 0.0
    desc_score = min(1.0, avg_desc / _SPEC_DESC_TARGET)
    crit_score = min(1.0, avg_crit / _SPEC_CRITERIA_TARGET)
    specificity = 0.5 * desc_score + 0.5 * crit_score

    # ── dependency_quality ──
    cycles = TaskGraph.build(drafts).cycles() if drafts else []
    if cycles:
        dependency_quality = 0.0
    elif any(task.dependencies for task in drafts):
        dependency_quality = 1.0
    else:
        dependency_quality = 0.5

    # ── deliverable_clarity ──
    if drafts:
        with_deliv = sum(1 for task in drafts if task.deliverables) / len(drafts)
    else:
        with_deliv = 0.0
    all_deliverables = [spec for task in drafts for spec in task.deliverables]
    if all_deliverables:
        required_ratio = (
            sum(1 for spec in all_deliverables if spec.required)
            / len(all_deliverables)
        )
        path_ok = (
            sum(1 for spec in all_deliverables if _path_normative(spec.path))
            / len(all_deliverables)
        )
    else:
        required_ratio = 0.0
        path_ok = 0.0
    deliverable_clarity = (
        0.5 * with_deliv + 0.25 * required_ratio + 0.25 * path_ok
    )

    total = (
        _W_COVERAGE * coverage
        + _W_SPECIFICITY * specificity
        + _W_DEPENDENCY * dependency_quality
        + _W_DELIVERABLE * deliverable_clarity
    )
    details: dict[str, Any] = {
        "draft_task_count": len(drafts),
        "goal_keywords": keywords,
        "goal_keywords_covered": covered,
        "avg_description_len": round(avg_desc, 2),
        "avg_acceptance_criteria": round(avg_crit, 2),
        "cycles": cycles,
        "deliverable_count": len(all_deliverables),
    }
    return PlanScore(
        total=total,
        coverage=coverage,
        specificity=specificity,
        dependency_quality=dependency_quality,
        deliverable_clarity=deliverable_clarity,
        details=details,
    )


# ── P2-7：增量计划编辑（添加 / 删除 / 修改，不可变，返回新列表）───────────────

#: 增量编辑允许就地修改的字段（运行期状态字段不在这里 —— 它们由 TaskBoard 管）。
_EDITABLE_TASK_FIELDS = frozenset(
    {
        "title",
        "description",
        "role",
        "acceptance_criteria",
        "deliverables",
        "dependencies",
        "priority",
        "brief",
    }
)


def add_task_to_plan(tasks: Sequence[Task], new_task: Task) -> list[Task]:
    """在计划末尾、``T-FINAL`` **之前**插入 ``new_task``，返回新列表。

    ``T-FINAL`` 是平台追加的收尾任务，必须始终排在最后（它依赖全部叶子任务），
    因此新任务插到它前面；计划里没有 ``T-FINAL`` 时直接追加。id 与已有任务重复
    时抛 :class:`ValueError` —— 这是用户手误，早报比等校验把整条列表退回更友好。
    不修改输入列表，也不修改 ``new_task``。
    """
    if any(task.id == new_task.id for task in tasks):
        raise ValueError(f"任务 id 已存在：{new_task.id}")
    result = list(tasks)
    if result and result[-1].id == FINAL_TASK_ID:
        result.insert(len(result) - 1, new_task)
    else:
        result.append(new_task)
    return result


def remove_task_from_plan(tasks: Sequence[Task], task_id: str) -> list[Task]:
    """删除指定任务，并从其他任务的 ``dependencies`` 里清掉对它的引用。

    不清理依赖会留下悬空边（:func:`validate_plan` 校验 3 会把它当错误）；删除
    ``T-FINAL``（平台保留任务）或不存在的 id 抛 :class:`ValueError`。返回新列表。
    """
    if task_id == FINAL_TASK_ID:
        raise ValueError("不能删除平台保留的收尾任务 T-FINAL")
    if not any(task.id == task_id for task in tasks):
        raise ValueError(f"任务 id 不存在：{task_id}")
    result: list[Task] = []
    for task in tasks:
        if task.id == task_id:
            continue
        deps = [dep for dep in task.dependencies if dep != task_id]
        if deps == list(task.dependencies):
            result.append(task)
        else:
            result.append(task.model_copy(update={"dependencies": deps}))
    return result


def modify_task_in_plan(
    tasks: Sequence[Task], task_id: str, updates: dict[str, Any]
) -> list[Task]:
    """就地修改某任务的可编辑字段，返回新列表（其他任务原样保留）。

    只允许改 :data:`_EDITABLE_TASK_FIELDS` 里的字段（运行期状态 / 评审记录不在此列，
    它们只能经 ``TaskBoard`` 改）；未知字段或不存在的任务 id 抛 :class:`ValueError`。
    修改经 ``model_copy(update=...)`` 完成，不触碰原对象。
    """
    unknown = set(updates) - _EDITABLE_TASK_FIELDS
    if unknown:
        raise ValueError(f"不可修改的字段：{sorted(unknown)}")
    if not any(task.id == task_id for task in tasks):
        raise ValueError(f"任务 id 不存在：{task_id}")
    return [
        task.model_copy(update=dict(updates)) if task.id == task_id else task
        for task in tasks
    ]


def validate_incremental_edit(
    original: Sequence[Task], edited: Sequence[Task]
) -> list[str]:
    """对增量编辑后的计划做结构校验，返回**全部错误**（空列表 = 通过）。

    复用 :func:`validate_plan` 里不需要角色目录的那几条检查（id 唯一、依赖存在且
    不自引用、无环、产物路径冲突、验收标准非空、要求文档的标准有文档交付物）。
    「只检查受影响部分」在工程上等价于全量结构重检：编辑来自一份**此前已通过校验**
    的计划，未被改动的任务保持合法，因此全量重检报出的任何错误必然落在被改动的部分
    （或被改动引入的冲突）。角色是否已注册需要 ``RoleCatalog``，由调用方在真正落盘
    前用 :func:`validate_plan` 兜底（CLI 编辑路径正是这么做的）。

    与 :func:`validate_plan` 不同，本函数**不抛异常**：它把错误收集成列表返回，
    便于 CLI 在子菜单里逐条展示并继续编辑。
    """
    errors: list[str] = []
    errors.extend(_check_ids(edited))
    errors.extend(_check_dependencies(edited))
    errors.extend(_check_cycles(edited))
    errors.extend(_check_artifact_paths(edited))
    errors.extend(_check_acceptance_criteria(edited))
    errors.extend(_check_criteria_have_deliverables(edited))
    return errors
