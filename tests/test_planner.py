"""拆解校验与 T-FINAL 追加的行为测试（spec §7.0/§7.2，Task 9）。

校验必须**收集全部错误**再一次性抛出：Task 29 的 Crew 依赖这一点把具体问题
整体回灌给 PM 重新拆解，任何早退都会让修复轮次变多、甚至修不干净。
"""

import json

import pytest
from pydantic import ValidationError

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.task.planner import (
    FINAL_TASK_ID,
    PlanOutput,
    PlanValidationError,
    TaskDraft,
    plan_output_json_schema,
    to_tasks,
    validate_plan,
    with_final_task,
)
from agentcrew.task.task import Task, TaskStatus


def draft(tid, role="backend_dev", deps=(), paths=("backend/app/main.py",), priority=5):
    return TaskDraft(id=tid, title=tid, description="d", role=role,
                     acceptance_criteria=["ok"],
                     deliverables=[ArtifactSpec(path=p, kind="python", description="x")
                                   for p in paths],
                     dependencies=list(deps), priority=priority)


@pytest.fixture
def catalog():
    return RoleCatalog([RoleSpec(key="backend_dev", title="后端", goal="g",
                                 backstory="b", responsibilities=["r"],
                                 boundaries=["x"], output_contract="o",
                                 system_prompt="p"),
                        RoleSpec(key="project_manager", title="PM", goal="g",
                                 backstory="b", responsibilities=["r"],
                                 boundaries=["x"], output_contract="o",
                                 system_prompt="p")])


def test_to_tasks_converts_drafts():
    ts = to_tasks(PlanOutput(tasks=[draft("T-1"), draft("T-2", deps=["T-1"])]))
    assert [t.id for t in ts] == ["T-1", "T-2"]
    assert ts[1].dependencies == ["T-1"]


def test_valid_plan_passes(catalog):
    # 合法计划要求产物路径全局唯一（§7.2 校验 5），故两条任务各占一个路径。
    validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", paths=("backend/app/main.py",)),
                                            draft("T-2", deps=["T-1"],
                                                  paths=("backend/app/models.py",))])),
                  catalog)


def test_duplicate_id_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", paths=("a.py",)),
                                                draft("T-1", paths=("b.py",))])), catalog)
    assert any("重复" in e for e in ei.value.errors)


def test_unknown_role_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", role="wizard")])), catalog)
    assert any("wizard" in e for e in ei.value.errors)


def test_dangling_dependency_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", deps=["T-99"])])), catalog)
    assert any("T-99" in e for e in ei.value.errors)


def test_self_dependency_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", deps=["T-1"])])), catalog)
    assert any("自身" in e or "环" in e for e in ei.value.errors)


def test_cycle_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", deps=["T-2"], paths=("a.py",)),
                                                draft("T-2", deps=["T-1"], paths=("b.py",))])),
                      catalog)
    assert any("环" in e for e in ei.value.errors)


def test_empty_acceptance_criteria_rejected(catalog):
    d = draft("T-1")
    d.acceptance_criteria = []
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[d])), catalog)
    assert any("验收标准" in e for e in ei.value.errors)


def test_all_errors_reported_together(catalog):
    """校验必须一次性报出全部问题，便于回灌 PM。"""
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[
            draft("T-1", role="wizard"),
            draft("T-1"),
        ])), catalog)
    assert len(ei.value.errors) >= 2


# ── Review Focus #4：同一路径被两个任务声明 ──
def test_duplicate_artifact_path_across_tasks_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[
            draft("T-1", paths=("backend/app/main.py",)),
            draft("T-2", paths=("backend/app/main.py",)),
        ])), catalog)
    msg = " ".join(ei.value.errors)
    assert "backend/app/main.py" in msg
    assert "T-1" in msg and "T-2" in msg      # 错误信息须指出冲突双方


def test_same_task_may_not_declare_same_path_twice(catalog):
    with pytest.raises(PlanValidationError):
        validate_plan(to_tasks(PlanOutput(tasks=[
            draft("T-1", paths=("a.py", "a.py")),
        ])), catalog)


# ── 校验 1：id 非空且唯一 ──
def test_empty_id_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("   ")])), catalog)
    assert any("id" in e and "空" in e for e in ei.value.errors)


def test_every_duplicated_id_is_collected(catalog):
    """重复 id 必须逐个报出，不能只报第一个。"""
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[
            draft("T-1", paths=("a.py",)), draft("T-1", paths=("b.py",)),
            draft("T-2", paths=("c.py",)), draft("T-2", paths=("d.py",)),
        ])), catalog)
    duplicated = [e for e in ei.value.errors if "重复" in e]
    assert len(duplicated) == 2
    assert any("T-1" in e for e in duplicated)
    assert any("T-2" in e for e in duplicated)


# ── 校验 2：role 命中目录 ──
def test_unknown_role_error_hints_available_keys(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", role="wizard")])), catalog)
    msg = [e for e in ei.value.errors if "wizard" in e][0]
    assert "backend_dev" in msg and "project_manager" in msg   # 列出可用 key 供 PM 修正


# ── 校验 3：依赖存在、不自引用 ──
def test_dependency_errors_name_the_offending_task(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", deps=["T-99", "T-1"])])), catalog)
    assert any("T-1" in e and "T-99" in e for e in ei.value.errors)   # 悬空依赖
    assert any("T-1" in e and "自身" in e for e in ei.value.errors)   # 自引用


# ── 校验 4：无环 ──
def test_cycle_error_lists_the_path(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", deps=["T-2"], paths=("a.py",)),
                                                draft("T-2", deps=["T-1"], paths=("b.py",))])),
                      catalog)
    msg = [e for e in ei.value.errors if "环" in e][0]
    assert "T-1" in msg and "T-2" in msg and "→" in msg


# ── 校验 5：同任务内路径冲突的消息要指明是谁 ──
def test_self_conflicting_path_error_names_task_and_path(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", paths=("a.py", "a.py"))])), catalog)
    msg = [e for e in ei.value.errors if "a.py" in e][0]
    assert "T-1" in msg and "重复" in msg


# ── 校验 6：路径安全由 ArtifactSpec 的 validator 保证，非法路径根本进不来 ──
@pytest.mark.parametrize("bad", ["../etc/passwd", "/abs.py", "C:/x.py", "a\\..\\b.py"])
def test_unsafe_artifact_path_rejected_at_model_level(bad):
    with pytest.raises(ValidationError):
        ArtifactSpec(path=bad, kind="python", description="x")


# ── 校验 7：acceptance_criteria 非空 ──
def test_missing_acceptance_criteria_error_names_task(catalog):
    d = draft("T-1")
    d.acceptance_criteria = []
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[d])), catalog)
    assert any("验收标准" in e and "T-1" in e for e in ei.value.errors)


# ── 不早退：三个互不相关的错误必须同时报出（否则 Task 29 的修复回灌会退化成串行）──
def test_independent_problems_are_never_masked(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[
            draft("T-1", role="wizard", deps=["T-99"], paths=("shared.py",)),
            draft("T-2", paths=("shared.py",)),
        ])), catalog)
    errors = ei.value.errors
    assert any("wizard" in e for e in errors)                                    # 校验 2
    assert any("T-99" in e for e in errors)                                      # 校验 3
    assert any("shared.py" in e and "T-1" in e and "T-2" in e for e in errors)   # 校验 5


# ── to_tasks：只做字段映射，不校验 ──
def test_to_tasks_maps_fields_and_keeps_runtime_defaults():
    d = TaskDraft(id="T-1", title="标题", description="描述", role="backend_dev",
                  acceptance_criteria=["a"],
                  deliverables=[ArtifactSpec(path="a.py", kind="python", description="x")],
                  dependencies=[], priority=9, brief="一句话摘要")
    task = to_tasks(PlanOutput(tasks=[d]))[0]
    assert (task.id, task.title, task.description, task.role) == \
        ("T-1", "标题", "描述", "backend_dev")
    assert task.acceptance_criteria == ["a"] and task.priority == 9
    assert task.brief == "一句话摘要" and task.dependencies == []
    assert task.status is TaskStatus.PENDING
    assert task.attempts == 0 and task.rework_count == 0
    assert task.result is None and task.reviews == [] and task.status_history == []


def test_to_tasks_does_not_validate():
    """路径冲突等非法拆解由 validate_plan 负责；to_tasks 只管映射。"""
    assert len(to_tasks(PlanOutput(tasks=[draft("T-1"), draft("T-2")]))) == 2


# ── 边界住在 TaskDraft 上：越界 priority 必须死在 PlanOutput 解析期（Task 20 的
#    结构化输出阶梯会重试），而不是在 to_tasks 里抛 ValidationError —— 那会逃出
#    Task 29 的 except PlanValidationError，把修复回合变成整次 run 崩溃。──
@pytest.mark.parametrize("bad_priority", [-1, 0, 10, 99])
def test_plan_output_rejects_out_of_range_priority(bad_priority):
    with pytest.raises(ValidationError):
        PlanOutput(tasks=[draft("T-1", priority=bad_priority)])


@pytest.mark.parametrize("priority", [1, 5, 9])
def test_plan_output_accepts_boundary_priority(priority):
    assert to_tasks(PlanOutput(tasks=[draft("T-1", priority=priority)]))[0].priority == priority


def test_to_tasks_never_raises_for_a_parseable_plan():
    """只要 PlanOutput 解析通过，to_tasks 就不得再抛任何异常（无剩余校验面）。"""
    plan = PlanOutput(tasks=[
        TaskDraft(id="T-1", title="标题" * 50, description="描述" * 50, role="backend_dev",
                  acceptance_criteria=["验收" * 20, "第二条"],
                  deliverables=[ArtifactSpec(path="深/目录/文件.py", kind="python",
                                             description="x" * 100,
                                             must_contain=["完成"], min_bytes=100)],
                  dependencies=["T-0"], priority=9, brief="摘要" * 20),
        TaskDraft(id="T-0", title="", description="", role="backend_dev",
                  acceptance_criteria=[], deliverables=[], dependencies=[], priority=1),
    ])
    assert [t.id for t in to_tasks(plan)] == ["T-1", "T-0"]


def test_validate_plan_returns_none_when_valid(catalog):
    assert validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1")])), catalog) is None


# ── PlanValidationError ──
def test_plan_validation_error_is_value_error_with_joined_message():
    exc = PlanValidationError(["错误一", "错误二"])
    assert isinstance(exc, ValueError)
    assert exc.errors == ["错误一", "错误二"]
    assert "错误一" in str(exc) and "错误二" in str(exc)


# ── plan_output_json_schema ──
def test_plan_output_json_schema_matches_model():
    schema = plan_output_json_schema()
    assert schema == PlanOutput.model_json_schema()
    assert "tasks" in schema["properties"]
    json.dumps(schema)          # 必须可直接交给提示词/结构化输出层


# ── with_final_task ──
def test_with_final_task_appends_sink(catalog):
    # 两条并行任务 → 两个叶子；T-FINAL 依赖全部叶子，最后收尾。
    tasks = to_tasks(PlanOutput(tasks=[draft("T-1", paths=("a.py",)),
                                       draft("T-2", paths=("b.py",))]))
    out = with_final_task(tasks)
    final = [t for t in out if t.id == FINAL_TASK_ID][0]
    assert final.role == "project_manager"
    assert set(final.dependencies) == {"T-1", "T-2"}     # 所有叶子
    assert any(d.path == "DELIVERY.md" for d in final.deliverables)


def test_with_final_task_depends_only_on_sinks(catalog):
    """链式 T-1 → T-2 → T-3 里只有 T-3 没有下游；T-FINAL 只依赖它。"""
    tasks = to_tasks(PlanOutput(tasks=[draft("T-1", paths=("a.py",)),
                                       draft("T-2", deps=["T-1"], paths=("b.py",)),
                                       draft("T-3", deps=["T-2"], paths=("c.py",))]))
    final = with_final_task(tasks)[-1]
    assert final.dependencies == ["T-3"]
    assert final.priority == 9                            # 数字小者优先 → 9 最后执行


def test_with_final_task_contract_is_self_consistent(catalog):
    """追加结果必须能通过自己这关校验（DELIVERY.md 不与其他产物冲突）。"""
    tasks = to_tasks(PlanOutput(tasks=[draft("T-1", paths=("a.py",)),
                                       draft("T-2", paths=("b.py",))]))
    out = with_final_task(tasks)
    validate_plan(out, catalog)
    final = out[-1]
    assert final.acceptance_criteria
    spec = final.deliverables[0]
    assert (spec.path, spec.kind, spec.must_contain, spec.min_bytes) == \
        ("DELIVERY.md", "markdown", ["完成"], 100)


def test_with_final_task_is_idempotent(catalog):
    tasks = to_tasks(PlanOutput(tasks=[draft("T-1", paths=("a.py",)),
                                       draft("T-2", paths=("b.py",))]))
    twice = with_final_task(with_final_task(tasks))
    assert [t.id for t in twice] == ["T-1", "T-2", FINAL_TASK_ID]
    assert FINAL_TASK_ID not in twice[-1].dependencies     # 不得产生自依赖
    validate_plan(twice, catalog)


def test_with_final_task_returns_new_list_and_leaves_input_untouched(catalog):
    tasks = to_tasks(PlanOutput(tasks=[draft("T-1", paths=("a.py",))]))
    out = with_final_task(tasks)
    assert out is not tasks
    assert [t.id for t in tasks] == ["T-1"]                # 输入不被修改
    assert all(t.id != FINAL_TASK_ID for t in tasks)


def test_with_final_task_on_empty_plan_raises():
    """空拆解必须报错，而不是「只追加一个 T-FINAL」（I6）。

    引擎追加的 ``T-FINAL`` 是**汇总**任务，不是交付：空计划放行的话，PM 返回
    ``{"tasks": []}`` 会产出一个只有 ``DELIVERY.md`` 的 run，退出码 0、报告写着
    「成功」—— 一次什么都没做的 run 被当成交付。
    """
    with pytest.raises(PlanValidationError) as ei:
        with_final_task([])
    assert any("空" in error for error in ei.value.errors)


def test_with_final_task_rejects_a_plan_containing_only_the_final_task():
    """只交回 ``T-FINAL`` 的拆解同样等于空计划（它会被摘掉再重建）。"""
    only_final = with_final_task(to_tasks(PlanOutput(
        tasks=[draft("T-1", paths=("a.py",))])))
    final = [task for task in only_final if task.id == FINAL_TASK_ID]
    with pytest.raises(PlanValidationError):
        with_final_task(final)


def test_validate_plan_rejects_empty_plan(catalog):
    """``validate_plan([])`` 过去是通过的 —— 空列表因此一路走到成功。"""
    with pytest.raises(PlanValidationError) as ei:
        validate_plan([], catalog)
    assert any("空" in error for error in ei.value.errors)


# ── 校验 8：要求文档的验收标准，必须有文档类交付物 ─────────────────────────
# 真实模型运行 output/realrun6 的 T-BACKEND：标准第 7 条要求「提供启动命令与依赖
# 说明」，而契约只声明了 app.py 与 requirements.txt —— agent 写 README 会被 G1 判
# 「未在契约中声明」驳回，不写又被 G2 按这条标准驳回，两条路都堵死，该任务永远
# 不可能通过（三轮评审里这条问题每轮都在，返工额度烧光后 FAILED、下游 5 个阻塞）。


def _task(tid, criteria, deliverables):
    return Task(id=tid, title=tid, description="d", role="backend_dev",
                acceptance_criteria=criteria, deliverables=deliverables)


def _py(path):
    return ArtifactSpec(path=path, kind="python", description="d")


def _md(path):
    return ArtifactSpec(path=path, kind="markdown", description="d")


def _txt(path):
    return ArtifactSpec(path=path, kind="text", description="d")


def test_doc_criterion_without_doc_deliverable_is_rejected(catalog):
    """真实 run6 的复现：要求「启动命令与依赖说明」，却只声明了代码与依赖清单。"""
    t = _task("T-BE", ["代码可运行，提供启动命令与依赖说明"],
              [_py("backend/app.py"), _txt("backend/requirements.txt")])
    with pytest.raises(PlanValidationError) as ei:
        validate_plan([t], catalog)
    msg = " ".join(ei.value.errors)
    assert "T-BE" in msg
    assert "说明" in msg                       # 指名是哪条标准
    assert "T-BE 不可能通过" in msg or "不可能通过" in msg   # 说明后果


def test_doc_criterion_with_markdown_deliverable_passes(catalog):
    t = _task("T-BE", ["提供启动说明"],
              [_py("backend/app.py"), _md("backend/README.md")])
    validate_plan([t], catalog)


def test_requirements_txt_does_not_count_as_a_document(catalog):
    """``kind=text`` 不都是文档：依赖清单证明不了「启动说明」。

    这一条是被真实数据逼出来的 —— 第一版校验把 ``text`` 一律当文档，于是
    ``requirements.txt`` 让「已有文档交付物」成立，真实失败案例从校验下溜了过去。
    """
    t = _task("T-BE", ["提供依赖说明"], [_txt("backend/requirements.txt")])
    with pytest.raises(PlanValidationError):
        validate_plan([t], catalog)


def test_docs_path_inside_criterion_is_not_a_document_demand(catalog):
    """标准里**提到** ``docs/...`` 路径不等于「要求文档」。

    第二个被真实数据逼出来的修正：``docs`` 曾是关键词之一，于是演示夹具 T-003 的
    「全局路径与 ``docs/api-spec.yaml`` 一致」被误判成「要求文档」而整份计划被拒。
    """
    t = _task("T-003", ["全局路径与 docs/api-spec.yaml 一致"], [_py("backend/app.py")])
    validate_plan([t], catalog)


def test_docstring_criterion_is_not_a_document_demand(catalog):
    """「docstring 说明参数与返回值」是要代码内的注释，不是要一份 markdown。

    真实 run 复现：PM 写了这条标准，任务只有 ``a.py`` 交付物，整份计划被判「不可
    能通过」，重拆几次措辞仍类似，最终 run 以退出码 2 直接死掉——而计划本身是
    合理的。裸子串匹配把动词「说明」当成了名词「文档」。
    """
    t = _task(
        "T1",
        ["add 函数带有说明参数与返回值的 docstring，且该 docstring 内容在 a.py 中可直接读到"],
        [_py("a.py")],
    )
    validate_plan([t], catalog)


def test_explain_as_verb_is_not_a_document_demand(catalog):
    """``说明`` 后接宾语时是动词：说明用法 / 说明字段 都不该被判成「要求文档」。"""
    for criterion in ("在 docstring 里说明用法", "在注释中说明字段含义"):
        t = _task("T1", [criterion], [_py("a.py")])
        validate_plan([t], catalog)


def test_requirement_spec_reference_is_not_a_document_demand(catalog):
    """「需求说明中约定」/「按需求说明约定」是**引用目标里已有的需求文本**，不是产出文档。

    真实 run 复现：`greet('World') 返回需求说明中约定的问候语` 与
    `按需求说明约定处理` 都被误判成「要求文档」而整份计划退出码 2——任务是写
    hello.py，需求说明指的是目标文本本身，不是要额外交一份 markdown。
    """
    for criterion in (
        "hello.py 可被 import，且 greet('World') 返回需求说明中约定的问候语",
        "对空字符串、None、非字符串输入按需求说明约定处理，不抛出未捕获异常",
    ):
        t = _task("T2", [criterion], [_py("hello.py")])
        validate_plan([t], catalog)


def test_production_verb_with_shuoming_still_demands_document(catalog):
    """产出动词 + 说明 仍是要求文档：编写使用说明 / 提供安装说明。"""
    t = _task("T1", ["编写使用说明"], [_py("a.py")])
    with pytest.raises(PlanValidationError):
        validate_plan([t], catalog)


def test_noun_usage_still_demands_document(catalog):
    """收窄不能把原意弄丢：「提供使用说明」仍是要求文档（名词用法，说明在句尾）。"""
    t = _task("T1", ["提供使用说明"], [_py("a.py")])
    with pytest.raises(PlanValidationError):
        validate_plan([t], catalog)


def test_english_docs_keyword_is_gone(catalog):
    """``docs`` 已从关键词表移除；``documentation`` 仍在。"""
    from agentcrew.task.planner import _DOC_DEMANDING_KEYWORDS
    assert "docs" not in _DOC_DEMANDING_KEYWORDS
    assert "documentation" in _DOC_DEMANDING_KEYWORDS


def test_code_only_criterion_without_docs_passes(catalog):
    t = _task("T-003", ["实现 GET /todos 返回任务列表"], [_py("backend/app.py")])
    validate_plan([t], catalog)
