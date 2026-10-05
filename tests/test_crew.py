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
from pathlib import Path

import pytest

from agentcrew.agents import registry
from agentcrew.agents.base_agent import BaseAgent
from agentcrew.agents.project_manager import ProjectManagerAgent
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactConflictError, ArtifactStore
from agentcrew.config import Settings
from agentcrew.crew.crew import EXIT_CODES, Crew, PlanFailedError, RunCancelled
from agentcrew.gate.gatekeeper import DeliveryDecision, NullPrompter, PlanDecision
from agentcrew.models.factory import ContextBudgetError
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.checkpoint import RunStatus
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.planner import PlanOutput
from agentcrew.task.task import Task, TaskStatus

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

APPROVE = {"verdict": "approve", "score": 0.9, "issues": [], "fix_hints": []}
REJECT = {
    "verdict": "reject",
    "score": 0.3,
    "issues": ["功能清单缺失"],
    "fix_hints": ["补充功能清单"],
}

#: 真实演示夹具（9 个任务，含一次 T-005 返工）；Task 33 的端到端套件共用。
DEMO_FIXTURES = Path(__file__).resolve().parents[1] / "examples" / "fixtures"


def _response(lang: str, path: str, body: str) -> str:
    return f"```{lang} file={path}\n{body}```\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "plan_todo.json").write_text(json.dumps(PLAN), encoding="utf-8")
    (fx / "reviews.json").write_text(
        json.dumps({"_default": APPROVE}), encoding="utf-8")
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


async def test_resume_continues_interrupted_run(env):
    crew = Crew(env["settings"])
    rec = await crew.run("做一个 Todo 应用")
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
    resumed = Crew.resume_from(Settings.load(), rec.run_id)
    out = await resumed.resume()
    assert out.status is RunStatus.COMPLETED
    board = resumed.checkpoint.load_board()
    assert all(task.status is TaskStatus.SUCCEEDED for task in board.all())
    assert board.get("T-002").attempts == 2  # 一次中断消耗一次尝试（spec §13.3）


async def test_resume_rehydrates_artifact_records_from_manifest(env):
    """resume 必须重建产物记录：否则 §9.2 的跨任务冲突检测在重启后失明。"""
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    art = _artifacts(env["settings"], rec.run_id)
    store = ArtifactStore(art, run_id=rec.run_id)
    assert store.records() == []  # 新进程：盘上有文件，内存里没有任何记录
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
        json.dumps({"T-001": REJECT, "_default": APPROVE}), encoding="utf-8")
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
