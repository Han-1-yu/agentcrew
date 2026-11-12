"""Task 29：Crew 两阶段编排（spec §7.0/§7.3/§13.3）。

覆盖 brief 的七个用例，并额外钉死分支评审点：

- 规划校验失败时**把具体错误回灌**给 PM 重拆（第二次 decompose 的 feedback 非空），
  重拆成功则整次 run 照常完成；
- 校验的是**追加 T-FINAL 之后**的任务列表（PM 声明 DELIVERY.md 必须当场被拒，
  而不是执行到一半才冲突）；
- G3 计划卡点看到的就是将要执行的任务（含 T-FINAL），cancel / save_exit 的落盘语义；
- resume 的 ``RUNNING → READY`` 修复、``attempts`` 不重置、MANIFEST 记录重建
  （§9.2 冲突检测跨 resume 的前提）、决策 JSONL 只追加新增行；
- 交付卡点 abort / rework（含 ``SUCCEEDED → READY`` 的人工打回）；
- 停滞时写 ``diagnostics/stall-report.md`` 且 run_finished 带退出码 4；
- prompter 不被静默替换：人工卡点 + 无 prompter 一律 ``ValueError``（Task 27 的
  构造期守卫原样生效），``gate="none"`` 与显式 ``NullPrompter()`` 两条出口照常。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agentcrew.agents import registry
from agentcrew.agents.base_agent import BaseAgent
from agentcrew.agents.project_manager import ProjectManagerAgent
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactConflictError, ArtifactStore
from agentcrew.config import Settings
from agentcrew.crew.crew import (
    EXIT_CODES,
    Crew,
    PlanFailedError,
    RunCancelled,
    _rework_targets,
)
from agentcrew.gate.gatekeeper import DeliveryDecision, NullPrompter, PlanDecision
from agentcrew.models.factory import BudgetExhaustedError, ContextBudgetError, ModelFactory
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.planner import PlanOutput, to_tasks, with_final_task
from agentcrew.task.task import Task, TaskStatus
from agentcrew.task.task_board import TaskBoard

#: brief 的两任务拆解（T-FINAL 由 Crew 追加，PM 不得自行生成）。
PLAN = {
    "tasks": [
        {
            "id": "T-001",
            "title": "需求",
            "description": "写 PRD",
            "role": "product_manager",
            "acceptance_criteria": ["含功能清单"],
            "dependencies": [],
            "priority": 1,
            "brief": "PRD",
            "deliverables": [
                {
                    "path": "docs/PRD.md",
                    "kind": "markdown",
                    "description": "PRD",
                    "must_contain": ["#"],
                }
            ],
        },
        {
            "id": "T-002",
            "title": "后端",
            "description": "写后端",
            "role": "backend_dev",
            "acceptance_criteria": ["有 API"],
            "dependencies": ["T-001"],
            "priority": 3,
            "brief": "后端实现",
            "deliverables": [
                {
                    "path": "backend/app/main.py",
                    "kind": "python",
                    "description": "入口",
                    "must_contain": ["def create_app"],
                }
            ],
        },
    ]
}

#: 正文必须过 G1 的 ``has_placeholder``（超短且无 ``=:(){}[];"'`` 结构字符即判占位）
#: 与 ``T-FINAL`` 的 ``min_bytes=100``：brief 的骨架夹具两处都不满足。
PRD_BODY = (
    "# PRD\n\n"
    "## 功能清单\n"
    "- 新增待办 (add)\n"
    "- 删除待办 (delete)\n"
    "- 修改待办 (edit)\n"
    "- 查询待办 (query)\n\n"
    "## 验收标准\n"
    "- AC-01: 新增后列表包含该条目\n"
)
MAIN_BODY = "def create_app():\n    return None\n"
#: ``with_final_task`` 给 DELIVERY.md 的 ``min_bytes=100``：夹具必须真的够长，
#: 否则 T-FINAL 会被 G1 判「空文件」而永远无法 SUCCEEDED。
DELIVERY_BODY = (
    "# 交付总结\n\n"
    "## 完成\n"
    "- T-001 产品需求文档已交付并通过两层门禁验收\n"
    "- T-002 后端入口已交付并通过两层门禁验收\n"
    "- 全部 3 个任务均无遗留阻塞\n\n"
    "## 运行\n"
    "uvicorn app.main:app --reload\n"
)

def _review_approve(count: int) -> dict:
    """G2 夹具：判定条数必须等于该任务的验收标准条数（``_is_malformed`` 强制全覆盖）。"""
    return {
        "judgements": [
            {"criterion_index": i, "satisfied": True,
             "evidence": f"产物已满足第 {i + 1} 条验收标准"}
            for i in range(count)
        ],
        "suggestions": [],
        "score": 0.9,
    }


#: 每个 task_id 一条 —— 兜底键 ``_default`` 在「条数按任务而定」之后不再可用：
#: 内置拆解 T-001/T-002 各 1 条标准，引擎追加的 T-FINAL 有 2 条。
REVIEWS = {
    "T-001": _review_approve(1),
    "T-002": _review_approve(1),
    "T-FINAL": _review_approve(2),
}
#: T-001 的唯一一条标准不满足：evidence 直接成为返工方的 fix_hints。
REJECT_T001 = {
    "judgements": [{"criterion_index": 0, "satisfied": False,
                    "evidence": "docs/PRD.md 缺少功能清单"}],
    "suggestions": [],
    "score": 0.3,
}
#: 「T-001 被驳回、其余任务照常通过」的整套夹具。
REJECT_REVIEWS = {**REVIEWS, "T-001": REJECT_T001}

#: 真实演示夹具（9 个任务，含一次 T-005 返工）；Task 33 的端到端套件共用。
DEMO_FIXTURES = Path(__file__).resolve().parents[1] / "examples" / "fixtures"


def _response(lang: str, path: str, body: str) -> str:
    return f"```{lang} file={path}\n{body}```\n"


def _task(task_id: str, *, status: TaskStatus = TaskStatus.PENDING) -> Task:
    """构造一个最小任务（面板/状态判定的纯逻辑用例用）。"""
    return Task(
        id=task_id,
        title=f"任务{task_id}",
        description="d",
        role="backend_dev",
        acceptance_criteria=["a"],
        deliverables=[],
        priority=5,
        status=status,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "plan_todo.json").write_text(json.dumps(PLAN), encoding="utf-8")
    (fx / "reviews.json").write_text(
        json.dumps(REVIEWS), encoding="utf-8")
    for tid, path, body in [
        ("T-001", "docs/PRD.md", PRD_BODY),
        ("T-002", "backend/app/main.py", MAIN_BODY),
        ("T-FINAL", "DELIVERY.md", DELIVERY_BODY),
    ]:
        d = fx / "responses" / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "attempt-1.md").write_text(
            _response(path.split(".")[-1], path, body), encoding="utf-8")
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", fx)
    return {"settings": Settings.load(), "fx": fx, "tmp_path": tmp_path}


@pytest.fixture
def clean_agent_classes():
    """``register_role(cls=...)`` 会写模块级 ``AGENT_CLASSES``（全局单例）：用完还原。"""
    before = dict(registry.AGENT_CLASSES)
    yield
    registry.AGENT_CLASSES.clear()
    registry.AGENT_CLASSES.update(before)


class _StubPrompter:
    """可编排的 G3 桩：记录计划卡点看到的任务，按序返回交付裁决。"""

    def __init__(self, plan=None, deliveries=()):
        self.plan_decision = plan
        self.deliveries = list(deliveries)
        self.plan_tasks = None
        self.delivery_records = []

    async def ask_plan(self, tasks):
        self.plan_tasks = [task.id for task in tasks]
        if self.plan_decision is not None:
            return self.plan_decision
        return PlanDecision(action="approve", tasks=list(tasks))

    async def ask_delivery(self, record):
        self.delivery_records.append(record)
        if self.deliveries:
            return self.deliveries.pop(0)
        return DeliveryDecision(action="accept")


def _run_dir(settings, run_id):
    return settings.output_dir / run_id


def _ledger(settings, run_id):
    return Ledger(
        _run_dir(settings, run_id) / "ledger.jsonl",
        run_id=run_id,
        secrets=settings.secret_values(),
    )


def _artifacts(settings, run_id):
    return _run_dir(settings, run_id) / "artifacts"


def _mark_interrupted(settings, run_id):
    """夹具：把跑完的 run 标回可恢复状态。

    R22 起 resume 拒绝 completed（终局成功不是崩溃产物）；这些用例拿跑完的
    run 当夹具验修复机制（面板重挂、门禁补派、产物重建），与状态语义无关，
    造假时同步把 run.json 改成 INTERRUPTED 保持自洽。
    """
    store = CheckpointStore(_run_dir(settings, run_id))
    record = store.load_record()
    record.status = RunStatus.INTERRUPTED
    store.save_record(record)


def _patch_tasks(settings, run_id, task_id, **fields):
    """就地改写 ``tasks.json`` 里某个任务的字段（模拟进程死亡那一刻的落盘状态）。"""
    path = _run_dir(settings, run_id) / "tasks.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["tasks"]:
        if item["id"] == task_id:
            item.update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── brief 用例 ────────────────────────────────────────────────────────────


async def test_run_produces_completed_record(env):
    crew = Crew(env["settings"])
    rec = await crew.run("做一个 Todo 应用")
    assert rec.project_goal == "做一个 Todo 应用"
    assert rec.stats.total_tasks == 3  # 2 个计划任务 + T-FINAL
    assert rec.stats.succeeded == 3
    assert rec.status is RunStatus.COMPLETED
    assert crew.checkpoint is not None
    assert crew.catalog.has("backend_dev")


async def test_plan_only_generates_plan_without_execution(env):
    """Ask 模式（--plan-only）：计划落盘、不执行，resume 后可续跑成完整 run。"""
    plan_settings = env["settings"].model_copy(update={"plan_only": True})
    rec = await Crew(plan_settings).run("做一个 Todo 应用")
    assert rec.status is RunStatus.PLANNED
    # 计划已落盘：tasks.json 有任务，但没有任何产物（未执行）
    run_dir = _run_dir(plan_settings, rec.run_id)
    assert (run_dir / "tasks.json").is_file()
    board = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))
    assert len(board["tasks"]) == 3
    assert not (run_dir / "artifacts" / "DELIVERY.md").exists()
    # 续跑：不重供 --plan-only，直接进入执行并交付
    resumed = Crew.resume_from(Settings.load(), rec.run_id)
    rec2 = await resumed.resume()
    assert rec2.status is RunStatus.COMPLETED
    assert rec2.stats.succeeded == 3


async def test_run_creates_artifacts_and_manifest(env):
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    art = _artifacts(env["settings"], rec.run_id)
    prd = (art / "docs" / "PRD.md").read_text(encoding="utf-8")
    assert "功能清单" in prd
    assert (art / "backend" / "app" / "main.py").is_file()
    assert (art / "DELIVERY.md").is_file()
    manifest = json.loads((art / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == rec.run_id
    assert {entry["path"] for entry in manifest["entries"]} == {
        "docs/PRD.md", "backend/app/main.py", "DELIVERY.md",
    }


async def test_empty_plan_is_rejected_not_reported_as_success(env):
    """PM 返回 ``{"tasks": []}`` 必须报错，而不是产出一次「成功」的空交付（I6）。

    修复前：``validate_plan([])`` 通过、``with_final_task([])`` 只追加一个
    ``T-FINAL``，run 以 COMPLETED 收尾、退出码 0、报告写着「成功 1 个」——
    一次什么都没做的 run 被当成交付。校验对象是**追加之前**的那份计划。
    """
    (env["fx"] / "plan_todo.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8")
    with pytest.raises(PlanFailedError) as ei:
        await Crew(env["settings"]).run("做一个 Todo 应用")
    assert any("空" in error for error in ei.value.errors)
    run_dir = next(iter(env["settings"].output_dir.iterdir()))
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed"        # 退出码 2（plan）
    assert record["stats"]["succeeded"] == 0
    assert not (run_dir / "artifacts" / "DELIVERY.md").exists()
    # 错误回灌给 PM 重拆：两次尝试、两次都点名「空计划」
    rejected = list(_ledger(env["settings"], run_dir.name).events("plan_rejected"))
    assert len(rejected) == 2
    assert all("空" in error for event in rejected for error in event.data["errors"])


async def test_plan_validation_failure_retries_then_raises(env, monkeypatch):
    bad = {
        "tasks": [
            {
                "id": "T-1",
                "title": "x",
                "description": "d",
                "role": "does_not_exist",
                "acceptance_criteria": ["a"],
                "deliverables": [],
                "dependencies": [],
            }
        ]
        * 2
    }
    (env["fx"] / "plan_todo.json").write_text(json.dumps(bad), encoding="utf-8")
    calls = []
    original = ProjectManagerAgent.decompose

    async def spy(self, goal, *, feedback=()):
        calls.append(list(feedback))
        return await original(self, goal, feedback=feedback)

    monkeypatch.setattr(ProjectManagerAgent, "decompose", spy)
    with pytest.raises(PlanFailedError) as ei:
        await Crew(env["settings"]).run("g")
    assert ei.value.errors
    # 上限 = settings.plan_max_attempts（默认 2），第二轮带着具体错误重拆
    assert len(calls) == 2
    assert calls[0] == []
    assert calls[1] and any("does_not_exist" in error for error in calls[1])
    run_dirs = list(env["settings"].output_dir.iterdir())
    assert len(run_dirs) == 1
    record = json.loads(
        (run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    rejected = list(_ledger(env["settings"], run_dirs[0].name).events("plan_rejected"))
    assert len(rejected) == 2
    assert rejected[0].data["errors"] == rejected[1].data["errors"]


async def test_register_custom_role_works_without_engine_changes(env):
    crew = Crew(env["settings"])
    crew.register_role(
        RoleSpec(
            key="security_auditor",
            title="安全审计",
            goal="g",
            backstory="b",
            responsibilities=["审计"],
            boundaries=["不写业务代码"],
            output_contract="输出报告",
            system_prompt="你是安全审计员。",
        )
    )
    assert crew.catalog.has("security_auditor")


#: 自定义角色的端到端夹具：计划引用它、契约声明产物、mock 回放该任务的回复。
WIZARD = RoleSpec(
    key="wizard",
    title="向导",
    goal="把项目目标翻译成一份向导说明。",
    backstory="你擅长把复杂目标压缩成三步。",
    responsibilities=["产出向导说明"],
    boundaries=["不写代码"],
    output_contract="产出 docs/wizard.md（Markdown）。",
    system_prompt="你是向导，只输出向导说明。",
    reviewer_for=["project_manager"],
)
WIZARD_BODY = (
    "# 向导\n\n"
    "## 步骤\n"
    "- 第一步：打开页面 (open)\n"
    "- 第二步：新增待办 (add)\n"
    "- 第三步：勾选完成 (done)\n\n"
    "## 说明\n"
    "本文件由自定义角色 wizard 产出，用于验证「角色是数据」的端到端链路。\n"
)


async def test_registered_custom_role_reaches_menu_and_is_dispatched(
        env, monkeypatch):
    """S7 端到端：注册的 wizard 进 PM 菜单、过计划校验、被调度并产出产物。

    ``register_role`` 的 API 一直能用，但旧 PM 只把 ``BUILTIN_ROLES`` 渲染进
    提示词 —— 自定义角色注册了却永远不会被拆解引用，S7 的「引擎零改动」在
    实际链路上不成立。本用例把整条链路钉死：提示词菜单 → 计划校验 → 调度 →
    mock 回放 → 产物落盘，全部走真实代码。
    """
    fx = env["fx"]
    plan = json.loads(json.dumps(PLAN))
    plan["tasks"].append({
        "id": "T-003",
        "title": "向导说明",
        "description": "产出向导说明",
        "role": "wizard",
        "acceptance_criteria": ["含三步步骤"],
        "dependencies": [],
        "priority": 2,
        "brief": "向导说明",
        "deliverables": [
            {
                "path": "docs/wizard.md",
                "kind": "markdown",
                "description": "向导说明",
                "must_contain": ["步骤"],
            }
        ],
    })
    (fx / "plan_todo.json").write_text(json.dumps(plan), encoding="utf-8")
    response = fx / "responses" / "T-003"
    response.mkdir(parents=True)
    (response / "attempt-1.md").write_text(
        _response("markdown", "docs/wizard.md", WIZARD_BODY), encoding="utf-8")

    prompts: list[str] = []
    original = ModelFactory.complete_structured

    async def spy(self, alias, messages, schema, **kwargs):
        prompts.append(messages[0]["content"])
        return await original(self, alias, messages, schema, **kwargs)

    monkeypatch.setattr(ModelFactory, "complete_structured", spy)

    crew = Crew(env["settings"])
    crew.register_role(WIZARD)
    rec = await crew.run("做一个 Todo 应用")

    assert rec.status is RunStatus.COMPLETED
    assert rec.stats.succeeded == 4  # T-001 / T-002 / T-003(wizard) / T-FINAL
    assert prompts and "wizard" in prompts[0] and "向导" in prompts[0], (
        "拆解提示词的可用角色菜单没有来自本次 run 的目录")
    board = crew.checkpoint.load_board()
    assert board.get("T-003").status is TaskStatus.SUCCEEDED
    artifacts = env["settings"].output_dir / rec.run_id / "artifacts"
    assert (artifacts / "docs" / "wizard.md").is_file()


async def test_resume_continues_interrupted_run(env):
    crew = Crew(env["settings"])
    rec = await crew.run("做一个 Todo 应用")
    _mark_interrupted(env["settings"], rec.run_id)
    rs = Settings.load()
    resumed = Crew.resume_from(rs, rec.run_id)
    board = resumed.checkpoint.load_board()
    assert all(t.status.value == "succeeded" for t in board.all())


async def test_delivery_summary_mentions_counts(env):
    crew = Crew(env["settings"])
    rec = await crew.run("做一个 Todo 应用")
    text = crew.delivery_summary(rec)
    assert "成功" in text or "succeeded" in text
    assert str(rec.stats.total_tasks) in text
    assert "LLM 调用" in text
    assert str(_artifacts(env["settings"], rec.run_id).resolve()) in text


async def test_run_record_persisted_with_config_snapshot(env):
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    p = _run_dir(env["settings"], rec.run_id) / "run.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "completed" and data["schema_version"] == 1
    assert data["config_snapshot"]["max_concurrency"] == 4
    assert data["stats"]["succeeded"] == 3
    assert data["stats"]["llm_calls"] > 0
    assert data["stats"]["finished_at"] is not None


# ── 规划阶段 ──────────────────────────────────────────────────────────────


async def test_plan_repair_succeeds_on_second_attempt(env, monkeypatch):
    """坏计划 → 错误回灌 → 重拆成功：两次调用，第二轮 feedback 点名未注册角色。"""
    bad = {"tasks": [dict(PLAN["tasks"][0], role="does_not_exist")]}
    calls = []

    async def spy(self, goal, *, feedback=()):
        calls.append(list(feedback))
        payload = bad if len(calls) == 1 else PLAN
        return PlanOutput.model_validate(payload)

    monkeypatch.setattr(ProjectManagerAgent, "decompose", spy)
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    assert rec.status is RunStatus.COMPLETED
    assert rec.stats.succeeded == 3
    assert len(calls) == 2
    assert calls[0] == []
    assert any("does_not_exist" in error for error in calls[1])


async def test_plan_declaring_delivery_md_is_rejected_before_execution(env):
    """校验对象必须是**追加 T-FINAL 之后**的列表：DELIVERY.md 冲突当场被拒。"""
    plan = json.loads(json.dumps(PLAN))
    plan["tasks"][1]["deliverables"][0]["path"] = "DELIVERY.md"
    (env["fx"] / "plan_todo.json").write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(PlanFailedError) as ei:
        await Crew(env["settings"]).run("做一个 Todo 应用")
    assert any("DELIVERY.md" in error for error in ei.value.errors)
    # 规划期就拦下：一个产物都不该落盘（run 目录由 checkpoint 建好，但里面是空的）
    assert all(
        not any(_artifacts(env["settings"], run_dir.name).iterdir())
        for run_dir in env["settings"].output_dir.iterdir()
    )


async def test_plan_gate_sees_validated_tasks_including_final(env, monkeypatch):
    monkeypatch.setenv("AGENTCREW_GATE", "plan")
    prompter = _StubPrompter()
    rec = await Crew(Settings.load(), prompter=prompter).run("做一个 Todo 应用")
    assert prompter.plan_tasks == ["T-001", "T-002", "T-FINAL"]
    assert rec.status is RunStatus.COMPLETED


async def test_plan_gate_edit_uses_edited_tasks(env, monkeypatch):
    """``edit`` 后的任务列表才是要执行的那份：T-001 被编辑掉，T-FINAL 依约重加。"""
    monkeypatch.setenv("AGENTCREW_GATE", "plan")
    kept = Task.model_validate(
        {
            **PLAN["tasks"][1],
            "dependencies": [],  # 编辑后不再依赖 T-001
        }
    )
    assert kept.id == "T-002"
    prompter = _StubPrompter(plan=PlanDecision(action="edit", tasks=[kept]))
    crew = Crew(Settings.load(), prompter=prompter)
    rec = await crew.run("做一个 Todo 应用")
    assert rec.stats.total_tasks == 2 and rec.stats.succeeded == 2
    board = crew.checkpoint.load_board()
    assert {task.id for task in board.all()} == {"T-002", "T-FINAL"}
    assert board.graph.dependencies("T-FINAL") == frozenset({"T-002"})


async def test_plan_gate_edit_is_revalidated(env, monkeypatch):
    """编辑结果重新走 §7.2 校验：非法 role 直接 PlanFailedError，不放行。"""
    monkeypatch.setenv("AGENTCREW_GATE", "plan")
    broken = Task.model_validate({**PLAN["tasks"][0], "role": "does_not_exist"})
    prompter = _StubPrompter(plan=PlanDecision(action="edit", tasks=[broken]))
    with pytest.raises(PlanFailedError) as ei:
        await Crew(Settings.load(), prompter=prompter).run("做一个 Todo 应用")
    assert any("does_not_exist" in error for error in ei.value.errors)


async def test_plan_gate_cancel_raises_and_persists_cancelled(env, monkeypatch):
    monkeypatch.setenv("AGENTCREW_GATE", "plan")
    prompter = _StubPrompter(plan=PlanDecision(action="cancel"))
    crew = Crew(Settings.load(), prompter=prompter)
    with pytest.raises(RunCancelled):
        await crew.run("做一个 Todo 应用")
    record = crew.checkpoint.load_record()
    assert record.status is RunStatus.CANCELLED
    finished = list(_ledger(Settings.load(), record.run_id).events("run_finished"))
    assert finished and finished[-1].data["status"] == "cancelled"
    assert finished[-1].data["exit_code"] == EXIT_CODES["cancelled"]


async def test_plan_gate_save_exit_is_resumable_without_replanning(env, monkeypatch):
    monkeypatch.setenv("AGENTCREW_GATE", "plan")
    prompter = _StubPrompter(plan=PlanDecision(action="save_exit"))
    crew = Crew(Settings.load(), prompter=prompter)
    with pytest.raises(RunCancelled):
        await crew.run("做一个 Todo 应用")
    run_id = crew.checkpoint.run_dir.name
    assert crew.checkpoint.load_record().status is RunStatus.INTERRUPTED
    board = crew.checkpoint.load_board()
    assert {task.id for task in board.all()} == {"T-001", "T-002", "T-FINAL"}
    # resume 跳过规划与计划闸门，直接执行已保存的计划；gate=plan 仍要求接线
    # prompter（构造期规则与卡点无关），测试环境里用显式 NullPrompter 声明全自动。
    resumed = Crew.resume_from(Settings.load(), run_id, prompter=NullPrompter())
    rec = await resumed.resume()
    assert rec.status is RunStatus.COMPLETED and rec.stats.succeeded == 3
    assert resumed.checkpoint.load_board().get("T-001").attempts == 1


async def test_config_error_marks_run_failed(env, monkeypatch):
    monkeypatch.setenv("AGENTCREW_CONTEXT_BUDGET", "200000")
    with pytest.raises(ContextBudgetError):
        await Crew(Settings.load()).run("做一个 Todo 应用")
    run_dir = next(iter(env["settings"].output_dir.iterdir()))
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    finished = list(_ledger(env["settings"], run_dir.name).events("run_finished"))
    assert finished[-1].data["exit_code"] == EXIT_CODES["config"]


async def test_malformed_model_alias_is_a_config_error(env, monkeypatch):
    """别名缺 ``:`` 是配置错误（退出码 3），不能以裸 ValueError 逃出 CLI 映射。"""
    monkeypatch.setenv("AGENTCREW_DEFAULT_MODEL", "gpt-4o-mini")
    from agentcrew.models.factory import ModelFactoryError

    with pytest.raises(ModelFactoryError):
        await Crew(Settings.load()).run("做一个 Todo 应用")
    run_dir = next(iter(env["settings"].output_dir.iterdir()))
    finished = list(_ledger(env["settings"], run_dir.name).events("run_finished"))
    assert finished[-1].data["exit_code"] == EXIT_CODES["config"]


async def test_plan_is_persisted_before_execution(env, monkeypatch):
    """规划成功即落 ``tasks.json``：死在执行开始前也能 resume，不必重拆。"""
    original = Crew._execute
    calls = {"n": 0}

    async def flaky(self, run, board):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟执行前崩溃")
        return await original(self, run, board)

    monkeypatch.setattr(Crew, "_execute", flaky)
    with pytest.raises(RuntimeError, match="模拟执行前崩溃"):
        await Crew(env["settings"]).run("做一个 Todo 应用")
    run_dir = next(iter(env["settings"].output_dir.iterdir()))
    board = CheckpointStore(run_dir).load_board()
    assert {task.id for task in board.all()} == {"T-001", "T-002", "T-FINAL"}
    rec = await Crew.resume_from(Settings.load(), run_dir.name).resume()
    assert rec.status is RunStatus.COMPLETED and rec.stats.succeeded == 3
    # 面板已在盘上：resume 没有重跑规划（否则 plan_created 会是 2）
    assert _ledger(env["settings"], rec.run_id).count("plan_created") == 1


# ── G3 prompter 守卫（Task 27 的规则必须原样生效）─────────────────────────


@pytest.mark.parametrize("mode", ["plan", "delivery", "both"])
async def test_human_gate_without_prompter_raises(env, monkeypatch, mode):
    """配了人工卡点却没 prompter：一律报错。

    Crew 不替调用方塞 ``NullPrompter`` —— 那会把「忘了接线」重新变成「自动
    放行」，正是 Task 27 的构造期守卫要拦的事。错误在 run 装配 Gatekeeper 时
    抛出（计划阶段每次都会重建它），Crew 既不捕获也不降级。
    """
    monkeypatch.setenv("AGENTCREW_GATE", mode)
    with pytest.raises(ValueError) as ei:
        await Crew(Settings.load()).run("做一个 Todo 应用")
    assert "prompter" in str(ei.value)
    # 没有静默放行：一个产物都不该落盘
    assert all(
        not any(_artifacts(env["settings"], run_dir.name).iterdir())
        for run_dir in env["settings"].output_dir.iterdir()
    )


async def test_explicit_null_prompter_is_the_automated_opt_in(env, monkeypatch):
    """显式传 ``NullPrompter()`` 是「我知道这里没有人」的声明：gate=both 也放行。"""
    monkeypatch.setenv("AGENTCREW_GATE", "both")
    rec = await Crew(Settings.load(), prompter=NullPrompter()).run("做一个 Todo 应用")
    assert rec.status is RunStatus.COMPLETED and rec.stats.succeeded == 3


async def test_gate_none_without_prompter_runs_fully_automatically(env):
    """``gate="none"``（``--auto`` 的路径）不需要 prompter，全自动跑完。"""
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    assert rec.status is RunStatus.COMPLETED and rec.stats.succeeded == 3


# ── resume ────────────────────────────────────────────────────────────────


async def test_resume_keeps_per_transition_checkpointing(env):
    """resume 后的面板必须挂回 ``on_change``：派发时的 attempts 扣除当场落盘。

    ``CheckpointStore.load_board`` 建的面板没有回调（回调只在构造函数上传）。
    直接复用它，``READY → RUNNING`` 这次扣除要等到下一个完成事件或收尾才写盘，
    硬中断就丢掉一次额度 —— 而 §13.3 正是靠「派发即消耗一次尝试」把
    「中断 → resume → 中断」变成有界循环。这里在「已派发、未完成」的窗口里读盘。
    """
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    _patch_tasks(
        env["settings"], rec.run_id, "T-002",
        status="running", attempts=1, result=None, reviews=[], status_history=[],
    )
    _patch_tasks(
        env["settings"], rec.run_id, "T-FINAL",
        status="pending", attempts=0, result=None, reviews=[], status_history=[],
    )
    _mark_interrupted(env["settings"], rec.run_id)
    seen: list[tuple[str, int]] = []
    tasks_path = _run_dir(env["settings"], rec.run_id) / "tasks.json"

    def on_event(name, data):
        if name == "task_dispatched" and data.get("task_id") == "T-002":
            disk = json.loads(tasks_path.read_text(encoding="utf-8"))
            item = next(t for t in disk["tasks"] if t["id"] == "T-002")
            seen.append((item["status"], item["attempts"]))

    resumed = Crew.resume_from(Settings.load(), rec.run_id, on_event=on_event)
    out = await resumed.resume()
    assert out.status is RunStatus.COMPLETED
    assert seen == [("running", 2)]  # 不等完成事件，派发那一刻就已落盘


async def test_manifest_is_written_during_the_run_not_only_at_the_end(env):
    """硬杀可能落在任何时刻：MANIFEST 随状态跃迁落盘，resume 才有记录可载。"""
    class Boom(RuntimeError):
        """模拟进程被硬杀（异常穿出调度循环，收尾代码不再执行）。"""

    def on_event(name, _data):
        if name == "task_completed":
            raise Boom("硬杀")

    with pytest.raises(Boom):
        await Crew(env["settings"], on_event=on_event).run("做一个 Todo 应用")
    run_dir = next(iter(env["settings"].output_dir.iterdir()))
    manifest = json.loads(
        (run_dir / "artifacts" / "MANIFEST.json").read_text(encoding="utf-8"))
    assert [entry["path"] for entry in manifest["entries"]] == ["docs/PRD.md"]
    # 这份清单让 Task 11 的 carry-forward 在最需要它的时候可用
    store = ArtifactStore(run_dir / "artifacts", run_id=run_dir.name)
    out = await Crew.resume_from(
        Settings.load(), run_dir.name, store=store).resume()
    assert out.status is RunStatus.COMPLETED and out.stats.succeeded == 3
    assert {record.path for record in store.records()} == {
        "docs/PRD.md", "backend/app/main.py", "DELIVERY.md",
    }


async def test_resume_repairs_running_task_without_resetting_attempts(env):
    """真·中断续跑：``RUNNING → READY`` 由 repair_statuses 修复，``attempts`` 不重置。"""
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    _patch_tasks(
        env["settings"], rec.run_id, "T-002",
        status="running", attempts=1, result=None, reviews=[], status_history=[],
    )
    _patch_tasks(
        env["settings"], rec.run_id, "T-FINAL",
        status="pending", attempts=0, result=None, reviews=[], status_history=[],
    )
    _mark_interrupted(env["settings"], rec.run_id)
    resumed = Crew.resume_from(Settings.load(), rec.run_id)
    out = await resumed.resume()
    assert out.status is RunStatus.COMPLETED
    board = resumed.checkpoint.load_board()
    assert all(task.status is TaskStatus.SUCCEEDED for task in board.all())
    assert board.get("T-002").attempts == 2  # 一次中断消耗一次尝试（spec §13.3）


async def test_resume_regates_task_stuck_in_review(env):
    """硬杀落在门禁评估窗口（任务停在 ``REVIEW``）时，resume 必须重跑门禁。

    ``repair_statuses`` 刻意保留 ``REVIEW``（产物已完整落盘，只需重跑门禁，比
    重跑 Agent 便宜），但调度循环只处理**自己派发的** worker 的完成事件 ——
    重启后没有人为这个 ``REVIEW`` 任务派 worker，它永远不终结、下游全部卡死：
    resume 以停滞收尾（退出码 4），而引擎其实完全知道该怎么办（重跑门禁）。
    触发窗口 = 每一次门禁评估（真实 provider 下是每任务数秒），可达路径是硬杀 /
    断电 / 文档化的第二次 Ctrl-C。
    """
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    # T-001 已 SUCCEEDED、T-002 停在门禁窗口里、T-FINAL 还在等 T-002
    _patch_tasks(
        env["settings"], rec.run_id, "T-002",
        status="review", attempts=1, result=None, reviews=[], status_history=[],
    )
    _patch_tasks(
        env["settings"], rec.run_id, "T-FINAL",
        status="pending", attempts=0, result=None, reviews=[], status_history=[],
    )
    _mark_interrupted(env["settings"], rec.run_id)
    resumed = Crew.resume_from(Settings.load(), rec.run_id)
    out = await resumed.resume()
    assert resumed.stalled is False            # 不是停滞（退出码 4）
    assert out.status is RunStatus.COMPLETED
    board = resumed.checkpoint.load_board()
    assert all(task.status is TaskStatus.SUCCEEDED for task in board.all())
    # 重跑门禁而不是重跑 Agent：T-002 没有被再执行一次
    assert board.get("T-002").attempts == 1
    assert not any(
        change.to_status is TaskStatus.RUNNING
        for change in board.get("T-002").status_history
    )


def test_run_json_stats_track_the_ledger_across_a_hard_kill(env):
    """硬杀之后 ``run.json`` 的统计必须和 ledger 对得上，resume 也不会丢掉那段开销。

    ``run.json`` 只在 run/resume 开始与收尾写的话，被杀那一刻盘上的
    ``stats.llm_calls`` 会停在 0，而 ledger 里已经躺着好几条 ``llm_call`` ——
    token 已经花掉了。resume 以那份陈旧记录为基线，被中断那一段的开销就**永久**
    从 run.json 里消失（ledger 里却还在），而 ``status`` 与运行报告读的正是
    run.json。

    用 ``asyncio.run`` 而不是 ``await``（本文件其余用例都是 async）：硬杀是**进程**
    死亡，事件循环跟着消失；直接在一个还在跑的事件循环里抛异常，刚 ``create_task``
    出来的 worker 会变成孤儿继续写同一份 ledger，测出来的差异就不是 run.json 的账。
    """
    class Boom(RuntimeError):
        """模拟进程被硬杀（异常穿出调度循环，收尾代码不再执行）。"""

    dispatches: list[str] = []
    killed_at: list[int] = []

    def ledger_now() -> int:
        run_dir = next(iter(env["settings"].output_dir.iterdir()))
        return Ledger(
            run_dir / "ledger.jsonl", run_id=run_dir.name,
        ).count("llm_call")

    def on_event(name, data):
        if name == "task_dispatched":
            dispatches.append(data["task_id"])
            if len(dispatches) == 2:
                # 第二个任务的派发 = 前一个完成事件的 checkpoint 已经落盘，
                # 此刻 ledger 里的账就是 run.json 该有的账。
                killed_at.append(ledger_now())
                raise Boom("硬杀")

    with pytest.raises(Boom):
        asyncio.run(
            Crew(env["settings"], on_event=on_event).run("做一个 Todo 应用"))

    run_dir = next(iter(env["settings"].output_dir.iterdir()))
    ledger = _ledger(env["settings"], run_dir.name)
    stored = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["stats"]
    assert killed_at and killed_at[0] > 0
    assert stored["llm_calls"] == killed_at[0], "run.json 的统计落后于 ledger"
    assert stored["prompt_tokens"] > 0

    # 被杀那一段的账已经落盘 → resume 以它为基线，这段开销不会丢
    killed_ledger = ledger.count("llm_call")
    resumed = Crew.resume_from(Settings.load(), run_dir.name)
    out = asyncio.run(resumed.resume())
    assert out.status is RunStatus.COMPLETED
    resumed_calls = ledger.count("llm_call") - killed_ledger
    assert out.stats.llm_calls == stored["llm_calls"] + resumed_calls


async def test_resume_rehydrates_artifact_records_from_manifest(env):
    """resume 必须重建产物记录：否则 §9.2 的跨任务冲突检测在重启后失明。"""
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    art = _artifacts(env["settings"], rec.run_id)
    store = ArtifactStore(art, run_id=rec.run_id)
    assert store.records() == []  # 新进程：盘上有文件，内存里没有任何记录
    _mark_interrupted(env["settings"], rec.run_id)
    resumed = Crew.resume_from(Settings.load(), rec.run_id, store=store)
    await resumed.resume()
    assert {record.path for record in store.records()} == {
        "docs/PRD.md", "backend/app/main.py", "DELIVERY.md",
    }
    assert [record.path for record in store.by_task("T-001")] == ["docs/PRD.md"]
    # 记录回来了，冲突检测才真的拦得住第二次写入
    intruder = Task(
        id="T-999", title="t", description="d", role="backend_dev",
        acceptance_criteria=["a"],
        deliverables=[
            ArtifactSpec(path="docs/PRD.md", kind="markdown", description="d")
        ],
    )
    with pytest.raises(ArtifactConflictError):
        store.commit(
            "docs/PRD.md", "# 覆盖\n",
            spec=intruder.deliverables[0], task=intruder,
        )


async def test_resume_does_not_duplicate_decision_lines(env):
    """``save_decisions`` 是朴素追加器：resume 后只能追加**新增**决策行。"""
    (env["fx"] / "reviews.json").write_text(
        json.dumps(REJECT_REVIEWS), encoding="utf-8")
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    path = _run_dir(env["settings"], rec.run_id) / "context" / "decisions.jsonl"
    first = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(first) == 2  # T-001 被驳回两次，每次留一条返工决策
    assert rec.status is RunStatus.FAILED and rec.stats.failed == 1
    # 模拟进程死在 T-001 的执行中；resume 后它会被再驳回两次
    _patch_tasks(
        env["settings"], rec.run_id, "T-001",
        status="running", attempts=1, rework_count=0, result=None,
        reviews=[], status_history=[],
    )
    resumed = Crew.resume_from(Settings.load(), rec.run_id)
    await resumed.resume()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 4  # 2 条旧 + 2 条新；朴素全量重写会变成 6 行
    assert lines[:2] == first


async def test_load_manifest_semantics(env):
    """``ArtifactStore.load_manifest``：缺文件 → 0；幂等替换；run_id 不符 → 报错。"""
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    art = _artifacts(env["settings"], rec.run_id)
    store = ArtifactStore(art, run_id=rec.run_id)
    assert store.load_manifest(art / "MANIFEST-MISSING.json") == 0
    assert store.load_manifest() == 3      # 缺省路径 = root/MANIFEST.json
    assert store.load_manifest() == 3      # 幂等：整体替换，不追加
    intruder = ArtifactStore(art, run_id="20260101-000000-dead")
    with pytest.raises(ValueError):
        intruder.load_manifest()


async def test_resume_of_cancelled_run_finishes_cancelled(env):
    """``main.py cancel`` 只改 run.json：下次 resume 直接以 CANCELLED 收尾（§13.4）。"""
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    run_dir = _run_dir(env["settings"], rec.run_id)
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    data["status"] = "cancelled"
    (run_dir / "run.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    out = await Crew.resume_from(Settings.load(), rec.run_id).resume()
    assert out.status is RunStatus.CANCELLED
    assert out.stats.succeeded == 3  # 方案与产物保持原样，不重跑


async def test_resume_from_unknown_run_raises(env):
    with pytest.raises(FileNotFoundError):
        Crew.resume_from(env["settings"], "20260101-000000-dead")


# ── G3 交付卡点 ───────────────────────────────────────────────────────────


async def test_delivery_gate_abort_marks_run_cancelled(env, monkeypatch):
    monkeypatch.setenv("AGENTCREW_GATE", "delivery")
    prompter = _StubPrompter(deliveries=[DeliveryDecision(action="abort")])
    rec = await Crew(Settings.load(), prompter=prompter).run("做一个 Todo 应用")
    assert rec.status is RunStatus.CANCELLED
    assert prompter.delivery_records
    assert prompter.delivery_records[0].stats.succeeded == 3
    finished = list(_ledger(env["settings"], rec.run_id).events("run_finished"))
    assert finished[-1].data["exit_code"] == EXIT_CODES["cancelled"]


async def test_delivery_gate_rework_reschedules_selected_task(env, monkeypatch):
    monkeypatch.setenv("AGENTCREW_GATE", "delivery")
    prompter = _StubPrompter(
        deliveries=[
            DeliveryDecision(action="rework", task_ids=["T-002"]),
            DeliveryDecision(action="accept"),
        ]
    )
    crew = Crew(Settings.load(), prompter=prompter)
    rec = await crew.run("做一个 Todo 应用")
    assert rec.status is RunStatus.COMPLETED and rec.stats.succeeded == 3
    assert len(prompter.delivery_records) == 2  # 打回后重新过交付卡点
    board = crew.checkpoint.load_board()
    assert board.get("T-002").attempts == 2  # 人工打回走后重新派发
    assert all(task.status is TaskStatus.SUCCEEDED for task in board.all())


async def test_delivery_gate_does_not_open_on_failed_run(env, monkeypatch):
    """§10.3：交付卡点只在全部任务 SUCCEEDED 时打开。

    失败 run 被问「接受交付？」没有意义，更危险的是 ``abort`` 会把 FAILED 记成
    CANCELLED（退出码 1 → 130，真实结局被掩盖）。这里给一个「若被问就 abort」的
    桩：它必须一次都没被问。
    """
    monkeypatch.setenv("AGENTCREW_GATE", "delivery")
    (env["fx"] / "reviews.json").write_text(
        json.dumps(REJECT_REVIEWS), encoding="utf-8")
    prompter = _StubPrompter(deliveries=[DeliveryDecision(action="abort")])
    rec = await Crew(Settings.load(), prompter=prompter).run("做一个 Todo 应用")
    assert prompter.delivery_records == []
    assert rec.status is RunStatus.FAILED
    finished = list(_ledger(env["settings"], rec.run_id).events("run_finished"))
    assert finished[-1].data["exit_code"] == EXIT_CODES["failed"]
    # 失败传播不是死锁：不写停滞报告（`BLOCKED` 非终态，别把结局 1 改成 4）
    assert not (
        _run_dir(env["settings"], rec.run_id) / "diagnostics" / "stall-report.md"
    ).exists()


async def test_delivery_rework_of_unknown_id_raises(env, monkeypatch):
    """打回一个不存在的 id：明确报错，不能静默忽略（人工会被反复追问同一件事）。"""
    monkeypatch.setenv("AGENTCREW_GATE", "delivery")
    prompter = _StubPrompter(
        deliveries=[DeliveryDecision(action="rework", task_ids=["T-999"])])
    with pytest.raises(ValueError, match="T-999"):
        await Crew(Settings.load(), prompter=prompter).run("做一个 Todo 应用")


def test_rework_targets_reject_unreworkable_status():
    """存在但状态不可返工的 id 同样报错：只有 FAILED / SUCCEEDED 可打回。"""
    board = TaskBoard([
        _task("T-1", status=TaskStatus.RUNNING),
        _task("T-2", status=TaskStatus.FAILED),
        _task("T-3", status=TaskStatus.BLOCKED),
        _task("T-4", status=TaskStatus.SUCCEEDED),
    ])
    with pytest.raises(ValueError, match="不可返工"):
        _rework_targets(board, ["T-3"])
    with pytest.raises(ValueError, match="T-1（running）"):
        _rework_targets(board, ["T-2", "T-1"])
    with pytest.raises(ValueError, match="不存在"):
        _rework_targets(board, ["T-404"])
    assert _rework_targets(board, ["T-2", "T-4", "T-2"]) == ["T-2", "T-4"]


async def test_human_rework_leaves_a_human_review_record(env, monkeypatch):
    """人工打回必须留 ``ReviewRecord(layer="human")``（spec §10.3）。

    它同时是 §14.3 报告「人工驳回」计数的唯一来源 —— 没有它，那一行永远是 0，
    而永远为零的行读起来像证据。留痕要落盘（这里从 tasks.json 读回）。
    """
    monkeypatch.setenv("AGENTCREW_GATE", "delivery")
    prompter = _StubPrompter(
        deliveries=[
            DeliveryDecision(action="rework", task_ids=["T-002"]),
            DeliveryDecision(action="accept"),
        ]
    )
    crew = Crew(Settings.load(), prompter=prompter)
    rec = await crew.run("做一个 Todo 应用")
    assert rec.status is RunStatus.COMPLETED
    board = crew.checkpoint.load_board()
    human = [r for r in board.get("T-002").reviews if r.layer == "human"]
    assert len(human) == 1
    assert human[0].verdict == "reject"
    assert human[0].reviewer == "human:delivery"
    assert human[0].round == 2 and human[0].issues and human[0].fix_hints
    assert "人工驳回 1" in crew.delivery_summary(rec)
    # 未被打回的任务不多留痕
    assert not [r for r in board.get("T-001").reviews if r.layer == "human"]


# ── 停滞 ──────────────────────────────────────────────────────────────────


async def test_stall_writes_report_and_fails_run(env, monkeypatch):
    from agentcrew.crew import scheduler as scheduler_module

    async def stalled_run(self):
        self._deadlock_report = "停滞诊断：T-002 [pending] ← 阻塞上游：T-001 [failed]"

    monkeypatch.setattr(scheduler_module.Scheduler, "run", stalled_run)
    crew = Crew(env["settings"])
    rec = await crew.run("做一个 Todo 应用")
    assert rec.status is RunStatus.FAILED
    assert crew.stalled is True
    report = _run_dir(env["settings"], rec.run_id) / "diagnostics" / "stall-report.md"
    assert "停滞诊断" in report.read_text(encoding="utf-8")
    finished = list(_ledger(env["settings"], rec.run_id).events("run_finished"))
    assert finished[-1].data["exit_code"] == EXIT_CODES["stalled"]


# ── 其它 ──────────────────────────────────────────────────────────────────


def test_exit_codes_match_spec():
    assert EXIT_CODES == {
        "ok": 0,
        "failed": 1,
        "plan": 2,
        "config": 3,
        "stalled": 4,
        "cancelled": 130,
    }


async def test_register_role_class_lands_in_agent_classes(env, clean_agent_classes):
    class AuditAgent(BaseAgent):
        """自定义角色类：只证明注册落点，不参与调度。"""

    spec = RoleSpec(
        key="security_auditor",
        title="安全审计",
        goal="g",
        backstory="b",
        responsibilities=["审计"],
        boundaries=["不写业务代码"],
        output_contract="输出报告",
        system_prompt="你是安全审计员。",
    )
    Crew(env["settings"]).register_role(spec, AuditAgent)
    assert registry.AGENT_CLASSES["security_auditor"] is AuditAgent


async def test_crew_runs_twice_with_independent_run_dirs(env):
    crew = Crew(env["settings"])
    rec = await crew.run("做一个 Todo 应用")
    second = await crew.run("再做一个 Todo 应用")
    assert second.run_id != rec.run_id
    assert crew.checkpoint.run_dir.name == second.run_id


async def test_concurrent_run_on_same_crew_is_rejected(env):
    crew = Crew(env["settings"])
    first = asyncio.create_task(crew.run("第一次"))
    await asyncio.sleep(0)  # 让第一个 run 进入执行段
    with pytest.raises(RuntimeError):
        await crew.run("第二次")
    assert (await first).status is RunStatus.COMPLETED


async def test_full_offline_demo_completes_nine_tasks(tmp_path, monkeypatch):
    """真实演示夹具端到端：8 个计划任务 + T-FINAL，含一次返工。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setattr(
        "agentcrew.models.factory.DEFAULT_FIXTURES_DIR", DEMO_FIXTURES)
    settings = Settings.load()
    crew = Crew(settings)
    rec = await crew.run("开发一个 Todo List Web 应用：浏览器中增删改查待办事项。")
    assert rec.status is RunStatus.COMPLETED
    assert rec.stats.total_tasks == 9
    assert rec.stats.succeeded == 9 and rec.stats.failed == 0
    art = _artifacts(settings, rec.run_id)
    manifest = json.loads((art / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["entries"]
    for entry in manifest["entries"]:
        assert (art / entry["path"]).is_file(), entry["path"]
    assert (art / "DELIVERY.md").is_file() and (art / "README.md").is_file()
    ledger = _ledger(settings, rec.run_id)
    assert ledger.count("run_finished") == 1
    assert any(
        event.data.get("verdict") == "reject"
        for event in ledger.events("gate_result")
    )
    assert "成功 9" in crew.delivery_summary(rec)


async def test_final_task_runs_last_and_writes_delivery(env):
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    started = [
        event.data["task_id"]
        for event in _ledger(env["settings"], rec.run_id).events("task_started")
    ]
    assert started[-1] == "T-FINAL"
    assert started.index("T-FINAL") > started.index("T-002")
    assert (_artifacts(env["settings"], rec.run_id) / "DELIVERY.md").is_file()


# ── 状态 sole-writer 纪律（spec §6.1:446）──────────────────────────────────

#: 允许写 ``Task.status`` 的地方：``transition`` 是唯一正规入口，另外两处是显式
#: 声明的越权修复 —— ``TaskBoard.reset_failed``（``FAILED`` 无出边的人工重试）与
#: ``Crew._apply_rework``（交付卡点把 ``SUCCEEDED`` 人工置回 ``READY``）。两者都在
#: ``status_history`` 里留下 reason（``manual-retry`` / ``delivery-rework``）。
_SANCTIONED_STATUS_WRITERS = {
    ("task_board.py", "transition"),
    ("task_board.py", "reset_failed"),
    ("crew.py", "_apply_rework"),
}


def _install_status_writer_traps(monkeypatch, hits):
    """装上两道陷阱：直接赋值 ``.status`` 与 ``model_copy(update={"status":…})``。

    pydantic 的 ``model_copy`` 用 ``copied.__dict__.update(...)`` 落字段，绕过
    ``__setattr__`` —— 只盯其中一处会漏掉另一处（``TaskBoard`` 全部写入走的正是
    ``model_copy``），所以两道都要装。命中记录 ``(种类, 文件名, 函数名)``，函数名
    为推导式时上溯一层，好让 ``_apply_rework`` 里的列表推导仍归到它自己名下。
    """
    original_setattr = Task.__setattr__
    original_copy = Task.model_copy

    def caller() -> tuple[str, str]:
        frame = sys._getframe(2)
        while frame.f_code.co_name in ("<listcomp>", "<genexpr>", "<dictcomp>"):
            assert frame.f_back is not None
            frame = frame.f_back
        return Path(frame.f_code.co_filename).name, frame.f_code.co_name

    def trap_setattr(self, name, value):
        if name == "status":
            hits.append(("setattr", *caller()))
        return original_setattr(self, name, value)

    def trap_copy(self, *, update=None, deep=False):
        if update and "status" in update and update["status"] != self.status:
            hits.append(("model_copy", *caller()))
        return original_copy(self, update=update, deep=deep)

    monkeypatch.setattr(Task, "__setattr__", trap_setattr)
    monkeypatch.setattr(Task, "model_copy", trap_copy)


async def test_task_status_is_only_written_through_the_board(env, monkeypatch):
    """spec §6.1:446：``Task.status`` 只经 ``TaskBoard.transition()`` 写。

    这条纪律此前只被 grep 功能性地验证过，没有回归守卫。陷阱覆盖两条写入通道
    （直接赋值 + ``model_copy``），跑一趟真实的 mock run，断言每一次 ``status``
    写入的来源都在白名单里。白名单只有三处：``transition`` 与两处显式声明的越权
    修复（见 :data:`_SANCTIONED_STATUS_WRITERS`）。测试自身还做了两个反向对照，
    证明陷阱真的抓得住（零命中不是「陷阱失效」）。
    """
    hits: list[tuple[str, str, str]] = []
    _install_status_writer_traps(monkeypatch, hits)

    rec = await Crew(env["settings"]).run("做一个 Todo 应用")

    assert rec.status is RunStatus.COMPLETED
    assert hits, "陷阱一次都没命中：写入纪律本身没被观测到，断言等于空转"
    assert any(hit[1:] == ("task_board.py", "transition") for hit in hits), (
        "正常路径（transition）一次都没被观测到，陷阱可能装错了位置")
    unauthorized = [
        hit for hit in hits if (hit[1], hit[2]) not in _SANCTIONED_STATUS_WRITERS
    ]
    assert unauthorized == [], f"这些地方绕过了 TaskBoard 写 status：{unauthorized}"

    # 反向对照 1：直接赋值必须被抓住（``__setattr__`` 那条通道）
    hits.clear()
    victim = _task("T-CTRL")
    victim.status = TaskStatus.SUCCEEDED
    assert hits == [("setattr", "test_crew.py",
                     "test_task_status_is_only_written_through_the_board")]

    # 反向对照 2：白名单之外的地方调 ``model_copy`` 也必须被抓住
    hits.clear()
    _task("T-CTRL2").model_copy(update={"status": TaskStatus.RUNNING})
    assert hits == [("model_copy", "test_crew.py",
                     "test_task_status_is_only_written_through_the_board")]


async def test_sanctioned_status_writer_is_recognized(env, monkeypatch):
    """:meth:`TaskBoard.reset_failed` 的越权修复必须被白名单认下（不误报）。

    白名单若与代码漂移（例如 ``reset_failed`` 改名），上一条用例要么静默放过
    真正合法的修复，要么把人工重试判成违规 —— 两种都会让人把守卫删掉。
    """
    hits: list[tuple[str, str, str]] = []
    _install_status_writer_traps(monkeypatch, hits)
    board = TaskBoard([_task("T-FAIL")])
    board.transition("T-FAIL", TaskStatus.READY)
    board.transition("T-FAIL", TaskStatus.RUNNING)
    board.transition("T-FAIL", TaskStatus.FAILED)
    hits.clear()

    board.reset_failed("T-FAIL")

    assert hits == [("model_copy", "task_board.py", "reset_failed")]
    assert ("task_board.py", "reset_failed") in _SANCTIONED_STATUS_WRITERS


async def test_custom_role_is_actually_dispatched_end_to_end(
    tmp_path, monkeypatch, clean_agent_classes
):
    """S7 的完整闭环：自定义角色不仅要能**加载**，还要真的被**派发执行**。

    验收报告指出：mock 的计划来自预置夹具、里面只有内置角色，所以「注册了自定义
    角色、run 退出 0」只验证了加载链路 —— 自定义角色永远不会被 PM 选中、不会被执行、
    不会落盘。这条测试用一个含自定义角色任务的临时计划跑完整 run，断言
    **拆解 → 派发 → 门禁 → 落盘** 全程打通，而且引擎代码零改动。
    """
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")

    custom = RoleSpec(
        key="security_auditor", title="安全审计师", goal="审计接口安全性",
        backstory="十年渗透测试经验", responsibilities=["审计鉴权与输入校验"],
        boundaries=["不写业务代码"], output_contract="输出 markdown 审计报告",
        system_prompt="你是安全审计师，只输出审计结论与依据，不改动业务代码。")

    plan = {"tasks": [{
        "id": "T-001", "title": "安全审计", "description": "审计 Todo 接口",
        "role": "security_auditor", "acceptance_criteria": ["给出审计结论"],
        "dependencies": [], "priority": 1, "brief": "审计",
        "deliverables": [{"path": "docs/audit.md", "kind": "markdown",
                          "description": "审计报告"}],
    }]}

    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "plan_todo.json").write_text(json.dumps(plan), encoding="utf-8")

    # review 夹具按**最终任务列表**逐条生成：judgement 数必须等于该任务的标准条数，
    # `_default` 兜底在这里不成立（T-001 一条、T-FINAL 两条）。
    plan_obj = PlanOutput.model_validate(plan)
    final_tasks = with_final_task(to_tasks(plan_obj))
    (fx / "reviews.json").write_text(json.dumps({
        t.id: {
            "judgements": [
                {"criterion_index": i, "satisfied": True, "evidence": f"已满足：{c}"}
                for i, c in enumerate(t.acceptance_criteria)
            ],
            "suggestions": [], "score": 0.9,
        } for t in final_tasks
    }), encoding="utf-8")

    for tid, path, body in [
        ("T-001", "docs/audit.md",
         "# 安全审计报告\n\n"
         "## 审计范围\n\n对 Todo 接口的鉴权、输入校验与错误处理做静态审计。\n\n"
         "## 结论\n\n鉴权：本轮为单用户本地服务，无鉴权面，不适用。\n"
         "输入校验：title 非空、长度上限、completed 类型检查均已覆盖。\n"
         "错误处理：不存在 id 返回 404，非法载荷返回 422，语义明确。\n\n"
         "## 遗留风险\n\n内存存储重启即丢，生产部署需替换为持久化实现。\n"),
        ("T-FINAL", "DELIVERY.md", DELIVERY_BODY),
    ]:
        d = fx / "responses" / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "attempt-1.md").write_text(
            _response(path.split(".")[-1], path, body), encoding="utf-8")

    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", fx)

    crew = Crew(Settings.load())
    crew.register_role(custom)          # ← 唯一的「接入」动作，引擎零改动
    record = await crew.run("对现有 Todo 接口做一次安全审计")

    assert record.status is RunStatus.COMPLETED
    board = crew.checkpoint.load_board()
    assert board.get("T-001").role == "security_auditor"
    assert board.get("T-001").status is TaskStatus.SUCCEEDED
    # 产物真的落盘了 —— 这是「被派发执行」而不是「被跳过」的证据
    assert (crew.checkpoint.artifacts_dir / "docs" / "audit.md").is_file()


# ── P0-3：成本预算硬上限 ──────────────────────────────────────────────────


async def test_budget_exhausted_graceful_shutdown(env):
    """max_llm_calls=1：规划阶段消耗 1 次，执行阶段首次调用即超限 → BudgetExhaustedError。

    Crew 捕获后优雅收尾（记 budget_exhausted 事件、FAILED 状态、退出码 1），
    再原样重抛异常。
    """
    s = env["settings"].model_copy(update={"max_llm_calls": 1})
    crew = Crew(s)
    with pytest.raises(BudgetExhaustedError) as ei:
        await crew.run("做一个 Todo 应用")
    assert ei.value.kind == "llm_calls"
    # run 以 FAILED 收尾（不是 COMPLETED，也不是中途崩溃无记录）
    record = crew.checkpoint.load_record()
    assert record.status is RunStatus.FAILED
    # budget_exhausted 事件已落 ledger
    ledger_path = crew.checkpoint.run_dir / "ledger.jsonl"
    events = [
        json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    budget_events = [e for e in events if e.get("event") == "budget_exhausted"]
    assert len(budget_events) == 1
    assert budget_events[0]["data"]["kind"] == "llm_calls"
    assert budget_events[0]["data"]["limit"] == 1.0
