"""P3-7 跨 run 知识复用的测试。

覆盖：

- ``ExperienceStore.inject_context`` 的渲染格式；
- 规划阶段把命中的历史经验注入 PM 上下文（spy ``complete_structured`` 的 user 消息）；
- 经验库为空时不注入（向后兼容）；
- ``reuse_knowledge=False`` 时不注入；
- ledger 记录 ``knowledge_reused`` 事件；
- 注入不破坏原有规划流程（run 照常 COMPLETED）。
"""

import json
from datetime import datetime

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.knowledge.experience import Experience, ExperienceStore
from agentcrew.models.factory import ModelFactory
from agentcrew.runtime.checkpoint import RunStatus
from agentcrew.runtime.ledger import Ledger

# ── inject_context 单元测试 ────────────────────────────────────────────────


def _exp(exp_id: str, desc: str, *, role: str = "backend_dev",
         solution: str = "FastAPI 路由实现 CRUD") -> Experience:
    return Experience(
        id=exp_id, task_description=desc, role=role,
        solution_summary=solution, artifacts=["backend/app/main.py"],
        key_decisions=["数据用内存存储"], success=True, run_id="old-run",
        created_at=datetime(2026, 1, 1),
    )


def test_inject_context_renders_formatted_block(tmp_path):
    store = ExperienceStore(tmp_path / "out")
    store.add(_exp("e1", "写后端待办接口", role="backend_dev"))
    text = store.inject_context("做一个待办后端接口")
    assert "## 历史经验（来自之前的成功 run）" in text
    assert "### 经验 1：写后端待办接口" in text
    assert "- 角色：backend_dev" in text
    assert "- 解决方案：FastAPI 路由实现 CRUD" in text
    assert "- 关键决策：数据用内存存储" in text


def test_inject_context_empty_when_no_match(tmp_path):
    store = ExperienceStore(tmp_path / "out")
    assert store.inject_context("做量子计算") == ""


# ── Crew 端到端 ────────────────────────────────────────────────────────────

PLAN = {
    "tasks": [
        {
            "id": "T-001", "title": "需求", "description": "写 PRD",
            "role": "product_manager", "acceptance_criteria": ["含功能清单"],
            "dependencies": [], "priority": 1, "brief": "PRD",
            "deliverables": [
                {"path": "docs/PRD.md", "kind": "markdown",
                 "description": "PRD", "must_contain": ["#"]}
            ],
        },
        {
            "id": "T-002", "title": "后端", "description": "写后端",
            "role": "backend_dev", "acceptance_criteria": ["有 API"],
            "dependencies": ["T-001"], "priority": 3, "brief": "后端实现",
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
    settings = Settings.load()
    return {"settings": settings, "tmp_path": tmp_path}


def _seed_relevant_experience(settings: Settings) -> None:
    """往全局经验库里塞一条与「Todo 应用」目标强相关的历史经验。"""
    store = ExperienceStore(settings.output_dir)
    store.add(_exp(
        "old1",
        "用 FastAPI 实现待办 CRUD 后端接口",
        role="backend_dev",
        solution="FastAPI 路由 + 内存 dict 存储待办增删改查",
    ))


async def test_planning_injects_relevant_experience(env, monkeypatch):
    _seed_relevant_experience(env["settings"])
    seen: list[str] = []
    original = ModelFactory.complete_structured

    async def spy(self, alias, messages, schema, **kwargs):
        if kwargs.get("purpose") == "plan":
            seen.append(messages[-1]["content"])
        return await original(self, alias, messages, schema, **kwargs)

    monkeypatch.setattr(ModelFactory, "complete_structured", spy)
    rec = await Crew(env["settings"]).run("做一个 Todo 待办应用")
    assert rec.status is RunStatus.COMPLETED
    assert seen, "规划阶段应至少调用一次 PM"
    assert any("历史经验" in text for text in seen)
    assert any("FastAPI 路由" in text for text in seen)


async def test_empty_knowledge_base_injects_nothing(env, monkeypatch):
    """经验库为空（首次 run）→ PM 上下文不含经验段，行为与改造前一致。"""
    seen: list[str] = []
    original = ModelFactory.complete_structured

    async def spy(self, alias, messages, schema, **kwargs):
        if kwargs.get("purpose") == "plan":
            seen.append(messages[-1]["content"])
        return await original(self, alias, messages, schema, **kwargs)

    monkeypatch.setattr(ModelFactory, "complete_structured", spy)
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    assert rec.status is RunStatus.COMPLETED
    assert seen
    assert not any("历史经验" in text for text in seen)


async def test_reuse_knowledge_off_injects_nothing(env, monkeypatch):
    _seed_relevant_experience(env["settings"])
    seen: list[str] = []
    original = ModelFactory.complete_structured

    async def spy(self, alias, messages, schema, **kwargs):
        if kwargs.get("purpose") == "plan":
            seen.append(messages[-1]["content"])
        return await original(self, alias, messages, schema, **kwargs)

    monkeypatch.setattr(ModelFactory, "complete_structured", spy)
    rec = await Crew(env["settings"], reuse_knowledge=False).run("做一个 Todo 应用")
    assert rec.status is RunStatus.COMPLETED
    assert not any("历史经验" in text for text in seen)


async def test_ledger_records_knowledge_reused(env):
    _seed_relevant_experience(env["settings"])
    rec = await Crew(env["settings"]).run("做一个 Todo 待办应用")
    ledger = Ledger(
        env["settings"].output_dir / rec.run_id / "ledger.jsonl",
        run_id=rec.run_id,
    )
    events = list(ledger.events("knowledge_reused"))
    assert len(events) == 1
    assert events[0].data["matched_experiences"] >= 1
    assert "Todo" in events[0].data["goal"]


async def test_reuse_does_not_break_planning(env):
    """注入经验不影响原有规划：run 照常 COMPLETED、任务数不变。"""
    _seed_relevant_experience(env["settings"])
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    assert rec.status is RunStatus.COMPLETED
    assert rec.stats.total_tasks == 3
    assert rec.stats.succeeded == 3
