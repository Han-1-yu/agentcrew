"""C5：文档 RAG——团队文档的摄取、检索与上下文注入。

四层验证：

1. **DocStore**：按标题/长度分块、manifest 增量（没变的文档复用块、删掉的
   文档出索引）、混合检索命中（哈希向量离线兜底，确定可复现）；
2. **同步退化**：``query_sync`` 对真实端点 embedder 只走 BM25（上下文组装是
   同步路径，不偷偷起线程），对本地哈希向量做完整混合打分；
3. **ContextBus 注入**：retriever 注入后片段作为 ``docs:<来源>`` 的节选进入
   上下文包，走预算记账；不注入时行为与 C5 之前逐字一致；
4. **Crew 接线**：未启用 → ``_docs_query_sync`` 返回 None（零开销）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agentcrew.knowledge.docs import DocStore, split_into_chunks
from agentcrew.knowledge.embeddings import CachedEmbedder, HashEmbedder


def _write_doc(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


async def _ingest(store: DocStore):
    return await store.ingest()


# ── 分块 ───────────────────────────────────────────────────────────────

def test_split_keeps_heading_context():
    doc = "# 部署规范\n\n使用 docker 部署。\n\n## 回滚\n\n回滚走上一版镜像。\n"
    chunks = split_into_chunks(doc)
    assert len(chunks) == 2
    assert chunks[0].startswith("[部署规范]")
    assert chunks[1].startswith("[回滚]")


def test_split_caps_long_sections():
    doc = "# 长文\n\n" + ("很长的段落。" * 400)
    chunks = split_into_chunks(doc)
    assert len(chunks) > 1
    assert all(len(chunk) <= 900 for chunk in chunks)  # 上限 + 重叠余量


# ── DocStore：摄取与检索 ────────────────────────────────────────────────

def _store(tmp_path: Path) -> DocStore:
    docs = tmp_path / "knowledge" / "docs"
    docs.mkdir(parents=True)
    _write_doc(docs, "deploy.md",
               "# 部署规范\n\n生产部署使用 docker compose，非 root 运行。\n"
               "镜像必须带 pytest 才能跑 run_tests。\n")
    _write_doc(docs, "api-style.md",
               "# API 风格\n\n所有写接口必须做幂等键校验，错误统一 Error 形状。\n")
    embedder = CachedEmbedder(HashEmbedder(),
                              tmp_path / "_cache" / "embeddings.jsonl")
    return DocStore(docs, embedder=embedder)


async def test_ingest_builds_chunks(tmp_path):
    store = _store(tmp_path)
    result = await _ingest(store)
    assert result.docs == 2
    assert result.chunks == 2
    assert sorted(result.rebuilt) == ["api-style.md", "deploy.md"]


async def test_ingest_incremental_reuses_unchanged_docs(tmp_path):
    store = _store(tmp_path)
    await _ingest(store)
    second = await _ingest(store)
    assert second.rebuilt == []
    assert sorted(second.skipped) == ["api-style.md", "deploy.md"]
    assert second.chunks == 2


async def test_ingest_drops_deleted_docs(tmp_path):
    store = _store(tmp_path)
    await _ingest(store)
    (tmp_path / "knowledge" / "docs" / "api-style.md").unlink()
    third = await _ingest(store)
    assert third.docs == 1
    assert third.chunks == 1


async def test_query_hits_relevant_chunk(tmp_path):
    store = _store(tmp_path)
    await _ingest(store)
    hits = await store.query("部署镜像要带 pytest 吗", limit=2)
    assert hits
    assert hits[0].chunk.doc == "deploy.md"


def test_query_sync_matches_async_for_hash_embedder(tmp_path):
    store = _store(tmp_path)
    asyncio.run(_ingest(store))
    sync_hits = store.query_sync("部署镜像要带 pytest 吗", limit=2)
    async_hits = asyncio.run(store.query("部署镜像要带 pytest 吗", limit=2))
    assert [hit.chunk.id for hit in sync_hits] \
        == [hit.chunk.id for hit in async_hits]


def test_query_sync_empty_store_returns_empty(tmp_path):
    store = DocStore(tmp_path / "knowledge" / "docs")
    assert store.query_sync("任意") == []


# ── ContextBus 注入 ────────────────────────────────────────────────────

def _bus(tmp_path: Path, retriever=None):
    from agentcrew.artifacts.spec import ArtifactSpec
    from agentcrew.artifacts.store import ArtifactStore
    from agentcrew.context.context_bus import ContextBus
    from agentcrew.runtime.ledger import Ledger
    from agentcrew.task.task import Task
    from agentcrew.task.task_board import TaskBoard

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger)
    task = Task(id="T-1", title="部署服务", description="按部署规范部署服务",
                role="backend_dev", acceptance_criteria=["a"],
                deliverables=[ArtifactSpec(path="deploy.md", kind="markdown",
                                           description="d")])
    board = TaskBoard([task])
    bus = ContextBus("按部署规范部署服务", board, store,
                     docs_retriever=retriever)
    return bus, task


def test_context_bus_injects_docs_as_excerpts(tmp_path):
    calls: list[str] = []

    def retriever(query: str) -> list[tuple[str, str]]:
        calls.append(query)
        return [("docs:deploy.md", "[部署规范] 生产部署使用 docker compose。")]

    bus, task = _bus(tmp_path, retriever)
    pack = bus.build(task)
    sources = [excerpt.path for excerpt in pack.key_excerpts]
    assert "docs:deploy.md" in sources
    assert calls == ["按部署规范部署服务\n按部署规范部署服务"]


def test_context_bus_without_retriever_unchanged(tmp_path):
    """不传 retriever 时行为与 C5 之前逐字一致（向后兼容红线）。"""
    bus, task = _bus(tmp_path)
    pack = bus.build(task)
    assert not any(excerpt.path.startswith("docs:")
                   for excerpt in pack.key_excerpts)


def test_context_bus_budget_exhaustion_marks_truncated(tmp_path):
    """预算耗尽时标记 truncated，而不是静默塞进超预算的片段。"""

    def big_retriever(query: str) -> list[tuple[str, str]]:
        return [("docs:big.md", "x" * 100_000)]

    bus, task = _bus(tmp_path, big_retriever)
    pack = bus.build(task)
    assert pack.truncated is True


# ── Crew 接线 ──────────────────────────────────────────────────────────

def test_crew_docs_query_sync_none_when_disabled(tmp_path):
    from agentcrew.config import Settings
    from agentcrew.crew.crew import Crew

    crew = Crew(Settings.load())
    assert crew._docs_query_sync() is None


# ── 哈希兜底不等于语义向量（B5 实测结论，见 bench/rag-hitrate-2026-11-13.md）──

class _FakeSemantic:
    name = "fake-semantic"
    dim = 3

    async def embed(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


def test_is_semantic_embedder_distinguishes_hash_fallback():
    from agentcrew.knowledge.embeddings import is_semantic_embedder

    assert is_semantic_embedder(None) is False
    assert is_semantic_embedder(HashEmbedder()) is False
    # 生产路径上包了一层磁盘缓存，别被外壳骗过去
    assert is_semantic_embedder(
        CachedEmbedder(HashEmbedder(), Path("/tmp/x.jsonl"))) is False
    assert is_semantic_embedder(_FakeSemantic()) is True


def test_crew_docs_weight_drops_to_zero_on_hash_fallback():
    """只有哈希兜底时，0.5 会把命中率拉低（对照实验见 bench 报告），必须压到 0。

    降级不能静默：ledger 里要留 ``docs_vector_degraded``。
    """
    from agentcrew.crew.crew import Crew

    class _Ledger:
        def __init__(self):
            self.events = []

        def emit(self, name, **fields):
            self.events.append((name, fields))

    crew = Crew.__new__(Crew)
    ledger = _Ledger()
    settings = type("S", (), {"docs_rag_vector_weight": 0.5})()
    crew._settings = settings

    crew._docs_weight_probe = None
    assert crew._docs_effective_weight(
        CachedEmbedder(HashEmbedder(), Path("/tmp/x.jsonl")), ledger) == 0.0
    assert ledger.events[0][0] == "docs_vector_degraded"
    assert ledger.events[0][1]["configured_weight"] == 0.5

    # 真语义向量照配置走，不降级、不留降级事件
    ledger2 = _Ledger()
    assert crew._docs_effective_weight(_FakeSemantic(), ledger2) == 0.5
    assert ledger2.events == []



def test_crew_manifest_persists_chunks(tmp_path):
    """manifest 里存分块结果：第二次摄取直接复用，不用重新分块。"""
    store = _store(tmp_path)
    asyncio.run(_ingest(store))
    manifest = json.loads(
        (tmp_path / "knowledge" / "docs" / "_docs_manifest.json")
        .read_text(encoding="utf-8"))
    assert manifest["deploy.md"]
    assert manifest["deploy.md#chunks"][0]["doc"] == "deploy.md"
