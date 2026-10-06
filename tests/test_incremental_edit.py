"""P2-7：增量计划编辑（添加 / 删除 / 修改）与 CLI 子菜单留痕。

钉死三类行为：planner 四个纯函数的不可变语义、CliPrompter 子菜单在注入
``input_fn`` 下的脚本化交互、以及 Crew 把增量操作落 ``plan_incremental_edit``。
"""

import json

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.gate.cli_prompter import CliPrompter
from agentcrew.gate.gatekeeper import PlanDecision
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.planner import (
    FINAL_TASK_ID,
    add_task_to_plan,
    modify_task_in_plan,
    remove_task_from_plan,
    validate_incremental_edit,
    with_final_task,
)
from agentcrew.task.task import Task


def _py(path: str) -> ArtifactSpec:
    return ArtifactSpec(path=path, kind="python", description="d")


def _task(tid, *, deps=(), deliverables=("a.py",), criteria=("验收",)):
    return Task(
        id=tid,
        title=f"任务{tid}",
        description="d",
        role="backend_dev",
        acceptance_criteria=list(criteria),
        deliverables=[_py(p) for p in deliverables],
        dependencies=list(deps),
    )


# ── 纯函数：不可变的增量编辑 ──────────────────────────────────────────────


def test_add_task_insembles_before_final_task():
    drafts = [_task("T-1", deliverables=("a.py",))]
    plan = with_final_task(drafts)  # [T-1, T-FINAL]
    new = _task("T-2", deliverables=("b.py",))
    out = add_task_to_plan(plan, new)
    assert [t.id for t in out] == ["T-1", "T-2", FINAL_TASK_ID]
    # 不修改输入
    assert [t.id for t in plan] == ["T-1", FINAL_TASK_ID]


def test_add_task_without_final_appends():
    out = add_task_to_plan([_task("T-1", deliverables=("a.py",))],
                           _task("T-2", deliverables=("b.py",)))
    assert [t.id for t in out] == ["T-1", "T-2"]


def test_add_duplicate_id_raises():
    with pytest.raises(ValueError):
        add_task_to_plan([_task("T-1", deliverables=("a.py",))],
                         _task("T-1", deliverables=("b.py",)))


def test_remove_task_clears_dependent_dependencies():
    tasks = [
        _task("T-1", deliverables=("a.py",)),
        _task("T-2", deps=["T-1"], deliverables=("b.py",)),
    ]
    out = remove_task_from_plan(tasks, "T-1")
    assert [t.id for t in out] == ["T-2"]
    assert out[0].dependencies == []  # 对 T-1 的悬空引用被清理
    # 输入原样
    assert tasks[1].dependencies == ["T-1"]


def test_remove_unknown_or_final_raises():
    with pytest.raises(ValueError):
        remove_task_from_plan([_task("T-1", deliverables=("a.py",))], "nope")
    with pytest.raises(ValueError):
        remove_task_from_plan(
            with_final_task([_task("T-1", deliverables=("a.py",))]), FINAL_TASK_ID
        )


def test_modify_task_updates_only_named_field():
    task = _task("T-1", deliverables=("a.py",))
    out = modify_task_in_plan([task], "T-1", {"title": "新标题"})
    assert out[0].title == "新标题"
    assert task.title != "新标题"  # 不可变
    assert out[0].description == "d"  # 其他字段不动


def test_modify_rejects_unknown_field_and_id():
    with pytest.raises(ValueError):
        modify_task_in_plan([_task("T-1", deliverables=("a.py",))],
                            "T-1", {"status": "running"})
    with pytest.raises(ValueError):
        modify_task_in_plan([_task("T-1", deliverables=("a.py",))],
                            "nope", {"title": "x"})


# ── validate_incremental_edit ──────────────────────────────────────────────


def test_validate_incremental_edit_passes_clean_plan():
    clean = [
        _task("T-1", deliverables=("a.py",)),
        _task("T-2", deps=["T-1"], deliverables=("b.py",)),
    ]
    assert validate_incremental_edit([], clean) == []


def test_validate_incremental_edit_reports_cycle():
    cyclic = [
        _task("T-1", deps=["T-2"], deliverables=("a.py",)),
        _task("T-2", deps=["T-1"], deliverables=("b.py",)),
    ]
    errors = validate_incremental_edit([], cyclic)
    assert any("环" in e for e in errors)


def test_validate_incremental_edit_reports_path_conflict():
    conflict = [
        _task("T-1", deliverables=("shared.py",)),
        _task("T-2", deliverables=("shared.py",)),
    ]
    errors = validate_incremental_edit([], conflict)
    assert any("shared.py" in e for e in errors)


# ── CliPrompter 子菜单（注入 input_fn）────────────────────────────────────


def _prompter(answers):
    it = iter(answers)
    return CliPrompter(input_fn=lambda _: next(it))


def _drafts():
    return [
        _task("T-1", deliverables=("a.py",)),
        _task("T-2", deps=["T-1"], deliverables=("b.py",)),
    ]


async def test_submenu_add_task():
    d = await _prompter([
        "e",            # 主菜单：编辑
        "a",            # 子菜单：添加
        "T-003",        # 新 id
        "新任务",       # 标题
        "描述",         # 描述
        "backend_dev",  # 角色
        "验收1,验收2",  # 验收标准
        "",             # 无交付物
        "b",            # 完成提交
    ]).ask_plan(_drafts())
    assert d.action == "edit"
    ids = [t.id for t in d.tasks]
    assert "T-003" in ids
    assert d.tasks[-1].id == FINAL_TASK_ID  # T-FINAL 仍在最后
    assert d.incremental_edits == [{"action": "add", "task_id": "T-003"}]


async def test_submenu_remove_task():
    d = await _prompter([
        "e",  # 主菜单
        "d",  # 删除
        "T-1",
        "b",
    ]).ask_plan(_drafts())
    assert d.action == "edit"
    ids = [t.id for t in d.tasks]
    assert "T-1" not in ids
    # T-2 对 T-1 的依赖被清理
    t2 = next(t for t in d.tasks if t.id == "T-2")
    assert "T-1" not in t2.dependencies
    assert d.incremental_edits == [{"action": "remove", "task_id": "T-1"}]


async def test_submenu_modify_task():
    d = await _prompter([
        "e",      # 主菜单
        "m",      # 修改
        "T-1",    # 目标 id
        "title",  # 字段
        "新标题", # 新值
        "b",
    ]).ask_plan(_drafts())
    assert d.action == "edit"
    t1 = next(t for t in d.tasks if t.id == "T-1")
    assert t1.title == "新标题"
    assert d.incremental_edits == [
        {"action": "modify", "task_id": "T-1", "changes": ["title"]}
    ]


async def test_submenu_full_editor_still_works_backward_compat():
    """子菜单 ``[e]`` 仍走全量编辑器并立即提交（老行为不变）。"""
    edited = [_task("T-9", deliverables=("x.py",))]
    p = CliPrompter(input_fn=lambda _: "e",
                    edit_fn=lambda tasks: edited)
    d = await p.ask_plan(_drafts())
    assert d.action == "edit"
    assert [t.id for t in d.tasks] == ["T-9"]
    # 全量编辑器记一条 full_editor（不是 add/remove/modify）
    assert d.incremental_edits == [{"action": "full_editor"}]


# ── Crew 留痕：plan_incremental_edit ─────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "plan")
    import tests.test_crew as crew_tests

    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "plan_todo.json").write_text(json.dumps(crew_tests.PLAN), encoding="utf-8")
    (fx / "reviews.json").write_text(json.dumps(crew_tests.REVIEWS), encoding="utf-8")
    for tid, path, body in [
        ("T-001", "docs/PRD.md", crew_tests.PRD_BODY),
        ("T-002", "backend/app/main.py", crew_tests.MAIN_BODY),
        ("T-FINAL", "DELIVERY.md", crew_tests.DELIVERY_BODY),
    ]:
        d = fx / "responses" / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "attempt-1.md").write_text(
            f"```{path.split('.')[-1]} file={path}\n{body}```\n", encoding="utf-8")
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", fx)
    return {"settings": Settings.load(), "tmp_path": tmp_path}


class _PlanStubPrompter:
    def __init__(self, decision):
        self.decision = decision

    async def ask_plan(self, tasks):
        return self.decision

    async def ask_delivery(self, record):
        from agentcrew.gate.gatekeeper import DeliveryDecision
        return DeliveryDecision(action="accept")


async def test_crew_emits_plan_incremental_edit(env):
    """带 incremental_edits 的 edit 裁决 → Crew 逐条落 plan_incremental_edit。"""
    import tests.test_crew as crew_tests

    kept = Task.model_validate(
        {**json.loads(json.dumps(crew_tests.PLAN["tasks"][1])),
         "dependencies": []}
    )
    decision = PlanDecision(
        action="edit",
        tasks=[kept],
        incremental_edits=[
            {"action": "remove", "task_id": "T-001"},
            {"action": "modify", "task_id": "T-002", "changes": ["title"]},
        ],
    )
    crew = Crew(env["settings"], prompter=_PlanStubPrompter(decision))
    rec = await crew.run("做一个 Todo 应用")
    assert rec.status.value == "completed"
    ledger = Ledger(
        env["settings"].output_dir / rec.run_id / "ledger.jsonl",
        run_id=rec.run_id, secrets=env["settings"].secret_values(),
    )
    events = list(ledger.events("plan_incremental_edit"))
    assert [e.data["action"] for e in events] == ["remove", "modify"]
    assert events[0].data["task_id"] == "T-001"
