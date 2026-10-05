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
from agentcrew.task.task import TaskStatus


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
    d = draft("T-1"); d.acceptance_criteria = []
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
    d = draft("T-1"); d.acceptance_criteria = []
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


def test_with_final_task_on_empty_plan():
    out = with_final_task([])
    assert [t.id for t in out] == [FINAL_TASK_ID]
    assert out[0].dependencies == []
