"""R27：向量检索——本地哈希向量、磁盘缓存、混合打分与配置通道。

覆盖四层：
1. ``HashEmbedder``：维度 / 确定性 / 归一化 / 余弦（本地退化路径的自洽性）；
2. ``CachedEmbedder``：命中不再打后端、缓存落盘、维度透传；
3. ``build_embedder``：未启用 → None；缺模型名 → 哈希兜底；缺密钥/坏别名 → 兜底；
4. ``ExperienceStore.query`` 的混合打分：权重 0 = 纯 BM25（与 R11 一致），
   权重 1 = 纯余弦，向量能把 BM25 漏掉的近义条目前移。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from agentcrew.config import Settings
from agentcrew.knowledge.embeddings import (
    CachedEmbedder,
    EmbeddingError,
    HashEmbedder,
    OpenAIEmbedder,
    build_embedder,
    cosine,
)
from agentcrew.knowledge.experience import Experience, ExperienceStore


async def test_hash_embedder_is_deterministic_and_normalized():
    embedder = HashEmbedder()
    first = (await embedder.embed(["幂等接口设计"]))[0]
    second = (await embedder.embed(["幂等接口设计"]))[0]
    assert len(first) == embedder.dim == 512
    assert first == second                        # 确定性：同样文本同样向量
    norm = sum(value * value for value in first) ** 0.5
    assert abs(norm - 1.0) < 1e-9                 # L2 归一
    assert abs(cosine(first, second) - 1.0) < 1e-9


async def test_hash_embedder_separates_unrelated_text():
    embedder = HashEmbedder()
    a, b, c = await embedder.embed(["幂等重试", "幂等重试机制", "前端样式美化"])
    assert cosine(a, b) > cosine(a, c)             # 共享字面片段更近
    assert cosine(a, c) < 0.5


def test_cosine_edge_cases():
    assert cosine([], [1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0]) == 0.0        # 维度不一致
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0   # 零向量
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert abs(cosine([1.0, 1.0], [2.0, 2.0]) - 1.0) < 1e-12


class _CountingEmbedder:
    """计数用最小后端：返回固定维度的可控向量。"""

    name = "stub"
    dim = 4

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return [[float(len(text) % 3), 1.0, 0.0, 0.0] for text in texts]


async def test_cached_embedder_hits_disk_and_skips_backend(tmp_path):
    inner = _CountingEmbedder()
    cache_path = tmp_path / "_cache" / "embeddings.jsonl"
    cached = CachedEmbedder(inner, cache_path)

    first = await cached.embed(["甲", "乙"])
    assert len(inner.calls) == 1
    assert cache_path.is_file()
    lines = [json.loads(line) for line in cache_path.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2 and lines[0]["model"] == "stub"

    second = await cached.embed(["甲", "乙"])
    assert len(inner.calls) == 1                   # 全命中，不再打后端
    assert second == first

    await cached.embed(["丙"])
    assert len(inner.calls) == 2                   # 只有缺的那条走后端
    assert inner.calls[1] == ["丙"]
    assert cached.dim == 4


async def test_cached_embedder_tolerates_corrupt_line(tmp_path):
    cache_path = tmp_path / "cache.jsonl"
    cache_path.write_text("not json\n" + json.dumps(
        {"key": "x", "model": "stub", "vector": [1.0, 0.0]}) + "\n",
        encoding="utf-8")
    cached = CachedEmbedder(_CountingEmbedder(), cache_path)
    vectors = await cached.embed(["甲"])
    assert len(vectors) == 1                       # 坏行跳过，不影响检索


def test_build_embedder_respects_switch(tmp_path):
    assert build_embedder(Settings.load(), output_dir=tmp_path) is None
    fallback = build_embedder(
        Settings.load(embedding_enabled=True, embedding_model=""),
        output_dir=tmp_path)
    assert isinstance(fallback, CachedEmbedder)
    assert fallback.name == "cached:hash"          # 缺模型名 → 本地退化
    from agentcrew.models.embedding import embedding_target

    broken = Settings.load(embedding_enabled=True, embedding_model="nope:model")
    assert embedding_target(broken) is None        # 别名解析失败 → 上层降级
    fallback_broken = build_embedder(
        broken, output_dir=tmp_path, target=embedding_target(broken))
    assert fallback_broken is not None and "hash" in fallback_broken.name


def test_openai_embedder_rejects_empty_model():
    with pytest.raises(EmbeddingError, match="模型名"):
        OpenAIEmbedder(model="", api_key="k")


def _experience(index: int, description: str, summary: str) -> Experience:
    return Experience(
        id=f"exp-{index}", task_description=description,
        role="backend_dev", solution_summary=summary, artifacts=[],
        key_decisions=[], success=True, run_id=f"R-{index}",
        created_at=datetime(2026, 11, 1, 10, index % 60))


def test_hybrid_query_weight_zero_equals_bm25(tmp_path):
    store = ExperienceStore(tmp_path)
    store.add(_experience(1, "实现幂等重试接口", "加唯一键"))
    store.add(_experience(2, "写前端样式", "用 CSS 变量"))
    baseline = store.query("幂等重试", limit=2)
    assert baseline and baseline[0].id == "exp-1"

    vectors = [[1.0, 0.0], [1.0, 0.0]]             # 与 BM25 结论无关
    mixed = store.query("幂等重试", limit=2, doc_vectors=vectors,
                        query_vector=[0.0, 1.0], vector_weight=0.0)
    assert [exp.id for exp in mixed] == [exp.id for exp in baseline]


def test_hybrid_query_vector_can_promote_semantic_hit(tmp_path):
    """向量把 BM25 排不上（甚至零命中）的条目拉到第一。"""
    store = ExperienceStore(tmp_path)
    store.add(_experience(1, "实现幂等重试接口", "加唯一键"))
    store.add(_experience(2, "接口限流", "令牌桶 + 重试退避"))

    # BM25 只认字面：问「幂等」时 exp-1 第一
    assert store.query("幂等", limit=2)[0].id == "exp-1"
    # 语义更近的是 exp-2（假设真实 embedding 给出这种方向）
    vectors = [[0.2, 0.98], [1.0, 0.0]]
    ranked = store.query("幂等", limit=2, doc_vectors=vectors,
                         query_vector=[1.0, 0.0], vector_weight=1.0)
    assert ranked[0].id == "exp-2"


def test_hybrid_query_falls_back_when_vectors_missing(tmp_path):
    store = ExperienceStore(tmp_path)
    store.add(_experience(1, "实现幂等重试接口", "加唯一键"))
    with_vectors = store.query("幂等重试", limit=2, doc_vectors=[[1.0]],
                               query_vector=[1.0], vector_weight=0.5)
    without = store.query("幂等重试", limit=2)
    assert [exp.id for exp in with_vectors] == [exp.id for exp in without]
    # 长度不匹配也退回 BM25（宁可用文本分，也不抛异常）
    mismatched = store.query("幂等重试", limit=2, doc_vectors=[[1.0], [1.0]],
                             query_vector=[1.0], vector_weight=0.5)
    assert [exp.id for exp in mismatched] == [exp.id for exp in without]


def test_embedding_settings_channels(monkeypatch):
    monkeypatch.setenv("AGENTCREW_EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("AGENTCREW_EMBEDDING_MODEL", "openai:text-embedding-3-small")
    monkeypatch.setenv("AGENTCREW_EMBEDDING_DIM", "1536")
    monkeypatch.setenv("AGENTCREW_RETRIEVAL_VECTOR_WEIGHT", "0.7")
    settings = Settings.load()
    assert settings.embedding_enabled is True
    assert settings.embedding_model == "openai:text-embedding-3-small"
    assert settings.embedding_dim == 1536
    assert settings.retrieval_vector_weight == 0.7
