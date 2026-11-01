"""P3-5 成功经验沉淀的测试。

覆盖：

- ``ExperienceStore`` 的 add / all / query（关键词匹配、role 过滤、limit）；
- run 以 COMPLETED 收尾后，成功任务被沉淀进**全局**经验库
  （``output_dir/knowledge/experiences.jsonl``，跨 run 共享）；
- ledger 记录 ``experience_captured`` 事件（含 task_id / experience_id）；
- ``capture_experiences=False`` 时不沉淀；
- 非 SUCCEEDED 任务不进经验库（``_extract_experiences`` 的过滤逻辑）。
"""

import json
from datetime import datetime

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.crew.crew import Crew, _extract_experiences
from agentcrew.knowledge.experience import Experience, ExperienceStore
from agentcrew.runtime.checkpoint import RunStatus
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskResult, TaskStatus
from agentcrew.task.task_board import TaskBoard

# ── ExperienceStore 单元测试 ───────────────────────────────────────────────


def _make_exp(exp_id: str, description: str, *, role: str = "backend_dev",
              run_id: str = "run-x", success: bool = True,
              summary: str = "默认解决方案") -> Experience:
    return Experience(
        id=exp_id,
        task_description=description,
        role=role,
        solution_summary=summary,
        artifacts=["backend/app/main.py"],
        key_decisions=["数据用内存存储"],
        success=success,
        run_id=run_id,
        created_at=datetime(2026, 1, 1),
    )


def test_store_add_and_all_roundtrip(tmp_path):
    store = ExperienceStore(tmp_path / "out")
    store.add(_make_exp("e1", "写后端待办接口"))
    store.add(_make_exp("e2", "写前端页面"))
    all_exps = store.all()
    assert [e.id for e in all_exps] == ["e1", "e2"]
    assert all_exps[0].task_description == "写后端待办接口"


def test_store_missing_file_is_empty(tmp_path):
    store = ExperienceStore(tmp_path / "out")
    assert store.all() == []
    assert store.query("任何东西") == []


def test_store_query_matches_keywords(tmp_path):
    store = ExperienceStore(tmp_path / "out")
    store.add(_make_exp("e1", "写后端待办 CRUD 接口", role="backend_dev",
                        summary="FastAPI 路由实现增删改查"))
    store.add(_make_exp("e2", "设计前端页面交互", role="frontend_dev",
                        summary="原生 JS 渲染页面布局"))
    store.add(_make_exp("e3", "数据库表结构设计", role="backend_dev",
                        summary="关系型数据库表拆分"))

    hits = store.query("CRUD 待办 接口")
    assert [e.id for e in hits] == ["e1"]

    # role 过滤：只看 backend_dev 的命中
    hits = store.query("表 结构", role="backend_dev")
    assert [e.id for e in hits] == ["e3"]

    # 无命中返回空，不把整库端上来（选用与全部经验零共字的词）
    assert store.query("量子引力弦论 M 理论") == []


def test_store_query_respects_limit(tmp_path):
    store = ExperienceStore(tmp_path / "out")
    for i in range(6):
        store.add(_make_exp(f"e{i}", "通用待办任务" + str(i)))
    hits = store.query("待办", limit=3)
    assert len(hits) == 3


# ── _extract_experiences：只收 SUCCEEDED ──────────────────────────────────


def _mk_task(task_id: str, desc: str, *, status: TaskStatus,
             role: str = "backend_dev", with_result: bool = False) -> Task:
    task = Task(
        id=task_id,
        title=task_id,
        description=desc,
        role=role,
        acceptance_criteria=["a"],
        deliverables=[
            ArtifactSpec(path=f"out/{task_id}.md", kind="markdown", description="x")
        ],
        status=status,
    )
    if with_result:
        task = task.model_copy(update={
            "result": TaskResult(
                task_id=task_id,
                role=role,
                summary=f"{task_id} 的解决方案摘要",
                artifacts=[],
                notes="补充说明",
                started_at=datetime(2026, 1, 1),
                finished_at=datetime(2026, 1, 1),
                duration_ms=10,
            )
        })
    return task


def test_extract_only_collects_succeeded(tmp_path):
    board = TaskBoard([
        _mk_task("T-1", "成功任务描述", status=TaskStatus.SUCCEEDED,
                 with_result=True),
        _mk_task("T-2", "失败任务描述", status=TaskStatus.FAILED),
        _mk_task("T-3", "取消任务描述", status=TaskStatus.CANCELLED),
    ])
    store = ArtifactStore(tmp_path / "art", run_id="run-x")
    bus = ContextBus("goal", board, store)
    extracted = _extract_experiences(board, bus, store=store, run_id="run-x")
    ids = [task_id for task_id, _ in extracted]
    assert ids == ["T-1"]
    exp = extracted[0][1]
    assert exp.success is True
    assert exp.task_description == "成功任务描述"
    assert exp.solution_summary.startswith("T-1 的解决方案摘要")
    assert exp.run_id == "run-x"


# ── Crew 端到端：成功 run 沉淀经验 ────────────────────────────────────────

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
                {"path": "docs/PRD.md", "kind": "markdown",
                 "description": "PRD", "must_contain": ["#"]}
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
                {"path": "backend/app/main.py", "kind": "python",
                 "description": "入口", "must_contain": ["def create_app"]}
            ],
        },
    ]
}

PRD_BODY = (
    "# PRD\n\n## 功能清单\n- 新增待办 (add)\n- 删除待办 (delete)\n\n"
    "## 验收标准\n- AC-01: 新增后列表包含该条目\n"
)
MAIN_BODY = "def create_app():\n    return None\n"
DELIVERY_BODY = (
    "# 交付总结\n\n## 完成\n- T-001 已交付\n- T-002 已交付\n"
    "- 全部任务无阻塞\n\n## 运行\nuvicorn app.main:app --reload\n"
)


def _response(lang: str, path: str, body: str) -> str:
    return f"```{lang} file={path}\n{body}```\n"


def _approve(count: int) -> dict:
    return {
        "judgements": [
            {"criterion_index": i, "satisfied": True,
             "evidence": f"满足第 {i + 1} 条"}
            for i in range(count)
        ],
        "suggestions": [], "score": 0.9,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "plan_todo.json").write_text(json.dumps(PLAN), encoding="utf-8")
    (fx / "reviews.json").write_text(
        json.dumps({"T-001": _approve(1), "T-002": _approve(1),
                    "T-FINAL": _approve(2)}),
        encoding="utf-8")
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
    return {"settings": Settings.load(), "tmp_path": tmp_path}


async def test_successful_run_captures_experiences(env):
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    assert rec.status is RunStatus.COMPLETED
    knowledge = ExperienceStore(env["settings"].output_dir)
    # 全局共享：不在 run 目录下
    assert knowledge.path.parent.name == "knowledge"
    assert knowledge.path.parent.parent == env["settings"].output_dir
    exps = knowledge.all()
    # 2 个计划任务 + T-FINAL 全部 SUCCEEDED → 3 条经验
    assert len(exps) == 3
    assert all(e.success for e in exps)
    descs = {e.task_description for e in exps}
    assert "写 PRD" in descs and "写后端" in descs
    # 产物路径被记录
    by_desc = {e.task_description: e for e in exps}
    assert "backend/app/main.py" in by_desc["写后端"].artifacts


async def test_experiences_are_global_and_cross_run_shared(env):
    """经验库在 output_dir 根下，第二次 run 能读到第一次沉淀的经验。"""
    rec1 = await Crew(env["settings"]).run("做一个 Todo 应用")
    assert rec1.status is RunStatus.COMPLETED
    # 第二次 run：经验库已有第一次的沉淀（全局，不随 run 目录隔离）
    store = ExperienceStore(env["settings"].output_dir)
    assert len(store.all()) == 3
    # query 能按关键词命中第一次的经验
    hits = store.query("PRD 需求")
    assert hits, "应能命中第一次 run 沉淀的 PRD 经验"


async def test_capture_experiences_off_writes_nothing(env):
    rec = await Crew(env["settings"], capture_experiences=False).run("做一个 Todo 应用")
    assert rec.status is RunStatus.COMPLETED
    knowledge = ExperienceStore(env["settings"].output_dir)
    assert not knowledge.path.exists()
    assert knowledge.all() == []


async def test_ledger_records_experience_captured(env):
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    ledger = Ledger(
        env["settings"].output_dir / rec.run_id / "ledger.jsonl",
        run_id=rec.run_id,
    )
    events = list(ledger.events("experience_captured"))
    assert len(events) == 3
    task_ids = {e.data["task_id"] for e in events}
    assert task_ids == {"T-001", "T-002", "T-FINAL"}
    assert all(e.data["experience_id"] for e in events)
