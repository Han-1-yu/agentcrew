"""R3 实体记忆：纯规则抽取、全局库归并与检索、Crew 规划/沉淀接线。

覆盖：五类实体抽取、去重与上限、经验→实体转换、追加写+读取归并（mentions/
sources/context）、坏行宽容、名字加权检索与降噪、上下文渲染；Crew 默认关闭
（向后兼容）、显式覆盖、``entity_memory_reused`` / ``entity_memory_updated`` 留痕。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.knowledge.entity import (
    EntityMemoryStore,
    EntityRecord,
    entities_from_experience,
    extract_entities,
)
from agentcrew.knowledge.experience import Experience, ExperienceStore
from agentcrew.runtime.ledger import Ledger


def _exp(**overrides) -> Experience:
    base = dict(
        id="exp-1",
        task_description="用 FastAPI 实现待办清单的后端接口",
        role="backend_dev",
        solution_summary="采用 `FastAPI` + `src/api.py`，用 SSE 推送任务面板更新",
        artifacts=["backend/api.py", "backend/schema.sql"],
        key_decisions=["沿用 DAG 调度，不引入新的队列"],
        success=True,
        run_id="R-1",
        created_at=datetime.now(),
    )
    base.update(overrides)
    return Experience(**base)


def _record(name: str, *, context: str = "", run_id: str = "R-1",
            when: datetime | None = None) -> EntityRecord:
    return EntityRecord(
        name=name, kind="technology", context=context, sources=[run_id],
        mentions=1, updated_at=when or datetime.now(),
    )


def _run_stub(tmp_path: Path, goal: str = "用 FastAPI 做待办清单") -> SimpleNamespace:
    return SimpleNamespace(
        goal=goal,
        ledger=Ledger(tmp_path / "crew.jsonl", run_id="R-1"),
        record=SimpleNamespace(run_id="R-1"),
    )


# ── 纯规则抽取 ─────────────────────────────────────────────────────────

def test_extract_entities_covers_all_kinds():
    text = (
        "`FastAPI` 负责 `src/api.py`，见 docs/PRD.md；@qa_engineer 复核【交付清单】，"
        "SSE 与 DAG 使用 React。"
    )
    found = extract_entities(text)
    kinds = {kind for _, kind in found}
    assert {"file", "technology", "role", "concept", "acronym"} <= kinds
    names = {name for name, _ in found}
    assert "src/api.py" in names
    assert "docs/PRD.md" in names
    assert "qa_engineer" in names
    assert "交付清单" in names
    assert "SSE" in names


def test_extract_entities_dedupes_case_insensitively_and_limits():
    text = "`Docker` docker DOCKER `Redis` `FastAPI`"
    found = extract_entities(text, max_entities=3)
    assert len(found) == 3
    lowered = [name.casefold() for name, _ in found]
    assert len(lowered) == len(set(lowered))


def test_extract_entities_ignores_empty_and_non_text():
    assert extract_entities("") == []
    assert extract_entities("   ") == []
    assert extract_entities(None) == []  # type: ignore[arg-type]


def test_entities_from_experience_keeps_run_id_and_short_context():
    records = entities_from_experience(_exp(), run_id="R-9", max_entities=4)
    assert 0 < len(records) <= 4
    assert all(record.sources == ["R-9"] for record in records)
    assert all(len(record.context) <= 160 for record in records)
    assert all(record.mentions == 1 for record in records)


# ── 全局库 ─────────────────────────────────────────────────────────────

def test_store_missing_file_is_empty(tmp_path: Path):
    store = EntityMemoryStore(tmp_path)
    assert store.all() == []
    assert store.query("FastAPI") == []
    assert store.inject_context("FastAPI") == ""


def test_store_merges_by_name(tmp_path: Path):
    store = EntityMemoryStore(tmp_path)
    older = datetime.now() - timedelta(days=1)
    newer = datetime.now()
    store.append([
        _record("FastAPI", context="旧上下文", run_id="R-1", when=older),
        _record("fastapi", context="新上下文", run_id="R-2", when=newer),
        _record("Redis", context="缓存", run_id="R-1", when=older),
    ])
    merged = store.all()
    assert len(merged) == 2
    fastapi = next(item for item in merged if item.name.casefold() == "fastapi")
    assert fastapi.mentions == 2
    assert set(fastapi.sources) == {"R-1", "R-2"}
    assert fastapi.context == "新上下文", "上下文取最近一次"
    assert fastapi.updated_at == newer


def test_store_tolerates_broken_lines(tmp_path: Path):
    store = EntityMemoryStore(tmp_path)
    store.append([_record("FastAPI")])
    with open(store.path, "a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")
        handle.write("\n")
    assert [item.name for item in store.all()] == ["FastAPI"]


def test_query_prefers_name_hits_and_denoises(tmp_path: Path):
    """名字命中 +3；只沾上中文单字的无关实体不该被塞进上下文。"""
    store = EntityMemoryStore(tmp_path)
    store.append([
        _record("FastAPI", context="FastAPI 提供 REST 接口"),
        _record("Redis", context="缓存队列"),
    ])
    matched = store.query("用 FastAPI 做待办清单", limit=5)
    assert matched[0].name == "FastAPI"
    assert all(item.name != "Redis" for item in matched)


def test_query_and_inject_context(tmp_path: Path):
    store = EntityMemoryStore(tmp_path)
    store.append([
        _record("FastAPI", context="FastAPI 提供 REST 接口"),
        _record("Redis", context="Redis 做缓存"),
    ])
    matched = store.query("FastAPI 接口", limit=2)
    assert matched[0].name == "FastAPI"
    rendered = store.inject_context("FastAPI 接口", limit=2)
    assert rendered.startswith("## 实体记忆（跨 run 累积）")
    assert "`FastAPI`" in rendered
    assert "累计" in rendered
    assert store.inject_context("zzzz") == ""


# ── Crew 接线 ──────────────────────────────────────────────────────────

async def test_crew_switch_defaults_off(tmp_path: Path):
    # output_dir 必须隔离到 tmp_path，否则会读到仓库 output/ 里的真实历史经验库
    crew = Crew(Settings.load(output_dir=tmp_path))
    assert crew._entity_memory_enabled is False
    run = _run_stub(tmp_path)
    assert await crew._prepare_knowledge_context(run) == ""  # type: ignore[arg-type]
    assert run.ledger.count("entity_memory_reused") == 0


async def test_crew_explicit_override(tmp_path: Path):
    crew = Crew(Settings.load(output_dir=tmp_path), entity_memory_enabled=True)
    assert crew._entity_memory_enabled is True


async def test_prepare_context_injects_entities_when_enabled(tmp_path: Path):
    EntityMemoryStore(tmp_path).append([
        _record("FastAPI", context="FastAPI 提供 REST 接口")
    ])
    crew = Crew(Settings.load(output_dir=tmp_path),
                entity_memory_enabled=True, reuse_knowledge=False)
    run = _run_stub(tmp_path, goal="用 FastAPI 做接口")
    hint = await crew._prepare_knowledge_context(run)  # type: ignore[arg-type]
    assert "## 实体记忆" in hint
    assert "`FastAPI`" in hint
    assert run.ledger.count("entity_memory_reused") == 1
    assert run.ledger.count("knowledge_reused") == 0, "reuse_knowledge=False 应完全隔离经验块"


async def test_prepare_context_appends_entity_block_after_experience(tmp_path: Path):
    """两个开关同开时按「经验 → 实体」拼接；单独开经验时与改造前逐字一致。"""
    ExperienceStore(tmp_path).add(_exp())
    EntityMemoryStore(tmp_path).append([
        _record("FastAPI", context="FastAPI 提供 REST 接口")
    ])
    settings = Settings.load(output_dir=tmp_path)
    run_default = _run_stub(tmp_path, goal="FastAPI 待办清单")
    legacy = await Crew(
        settings, reuse_knowledge=True)._prepare_knowledge_context(
        run_default)  # type: ignore[arg-type]
    assert legacy.startswith("## 历史经验")
    assert "实体记忆" not in legacy

    run_both = _run_stub(tmp_path, goal="FastAPI 待办清单")
    both = await Crew(
        settings, entity_memory_enabled=True)._prepare_knowledge_context(
        run_both)  # type: ignore[arg-type]
    assert both.startswith("## 历史经验")
    assert "## 实体记忆" in both
    assert run_both.ledger.count("entity_memory_reused") == 1


def test_capture_entities_writes_store_and_emits(tmp_path: Path):
    crew = Crew(Settings.load(output_dir=tmp_path), entity_memory_enabled=True)
    run = _run_stub(tmp_path)
    crew._capture_entities(run, [("T-1", _exp())])  # type: ignore[arg-type]
    assert EntityMemoryStore(tmp_path).path.is_file()
    assert EntityMemoryStore(tmp_path).all(), "经验里的命名事实应落盘"
    event = list(run.ledger.events("entity_memory_updated"))[0]
    assert event.data["run_id"] == "R-1"
    assert event.data["entities"] > 0
    assert event.data["distinct"] > 0


def test_capture_entities_noop_without_records(tmp_path: Path):
    crew = Crew(Settings.load(output_dir=tmp_path), entity_memory_enabled=True)
    run = _run_stub(tmp_path)
    crew._capture_entities(run, [])  # type: ignore[arg-type]
    assert run.ledger.count("entity_memory_updated") == 0
    assert not EntityMemoryStore(tmp_path).path.exists()


# ── R29：向量混合 ─────────────────────────────────────────────────────

def test_haystack_includes_name_kind_and_context():
    record = _record("FastAPI", context="FastAPI 提供 REST 接口")
    text = EntityMemoryStore.haystack(record)
    assert "FastAPI" in text and "technology" in text and "REST" in text


def test_hybrid_weight_zero_matches_keyword_only(tmp_path):
    store = EntityMemoryStore(tmp_path)
    store.append([_record("FastAPI", context="FastAPI 提供 REST 接口"),
                  _record("Redis", context="Redis 做缓存")])
    baseline = store.query("FastAPI")
    vectors = [[1.0, 0.0], [1.0, 0.0]]
    mixed = store.query("FastAPI", doc_vectors=vectors,
                        query_vector=[0.0, 1.0], vector_weight=0.0)
    assert [r.name for r in mixed] == [r.name for r in baseline]


def test_vector_recalls_entity_below_keyword_threshold(tmp_path):
    """关键词完全没过阈值（只提一个泛词）时，向量仍能把近义实体召回来。"""
    store = EntityMemoryStore(tmp_path)
    store.append([_record("FastAPI", context="FastAPI 提供 REST 接口"),
                  _record("同步阻塞", context="同步调用会阻塞事件循环")])
    # 「接口」在两个上下文里都出现 → 关键词路径命中的是 FastAPI
    assert [r.name for r in store.query("接口")] == ["FastAPI"]
    # 「导出」与两个实体都不沾字面 → 关键词零命中
    assert store.query("导出") == []
    vectors = [[0.0, 1.0], [1.0, 0.0]]
    recalled = store.query("导出", doc_vectors=vectors,
                           query_vector=[1.0, 0.0], vector_weight=1.0)
    assert [r.name for r in recalled] == ["同步阻塞"]


def test_hybrid_falls_back_when_vectors_missing_or_mismatched(tmp_path):
    store = EntityMemoryStore(tmp_path)
    store.append([_record("FastAPI", context="FastAPI 提供 REST 接口")])
    baseline = [r.name for r in store.query("FastAPI")]
    assert [r.name for r in store.query(
        "FastAPI", doc_vectors=[[1.0], [1.0]], query_vector=[1.0],
        vector_weight=0.5)] == baseline


async def test_crew_passes_entity_vectors_when_embedding_enabled(tmp_path):
    """embedding 开启（本地哈希退化）时，实体块照常注入且不炸。"""
    settings = Settings.load(output_dir=tmp_path, entity_memory_enabled=True,
                             embedding_enabled=True, embedding_model="")
    EntityMemoryStore(tmp_path).append([
        _record("FastAPI", context="FastAPI 提供 REST 接口")])
    run = _run_stub(tmp_path, goal="FastAPI 待办清单")
    hint = await Crew(settings, entity_memory_enabled=True,
                      reuse_knowledge=False)._prepare_knowledge_context(run)
    assert "## 实体记忆" in hint and "`FastAPI`" in hint
    events = [json.loads(line) for line in (tmp_path / "crew.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    used = [event for event in events if event.get("event") == "embedding_used"]
    assert used and used[0]["data"]["kind"] == "entity"
