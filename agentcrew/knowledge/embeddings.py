"""R27：向量检索的 embedding 通道——真实端点 / 本地确定性退化 / 磁盘缓存。

三条边界（与项目「离线可跑」的红线对齐）：

1. **默认关闭**：``embedding_enabled=False`` 时检索仍是纯 BM25，行为与 R11 逐字
   一致；开启才走向量通道。
2. **无网络也能跑**：没配 API Key / 端点不可达时用 :class:`HashEmbedder` 兜底
   ——字符 n-gram 哈希到固定维度，**确定、可复现、零成本**，但语义质量明显弱于
   真实 embedding（它只捕捉字面重合，不捕捉近义）。文档与输出都要如实标注。
3. **不重复计费**：:class:`CachedEmbedder` 按 ``sha256(model + text)`` 落盘缓存
   （``<output>/_cache/embeddings.jsonl``），同一文本第二次检索不再打网络。

混合打分在 :mod:`agentcrew.knowledge.experience` 里完成：BM25 分归一到 [0,1]
后与余弦相似度按 ``vector_weight`` 加权；权重 0 = 纯 BM25，1 = 纯向量。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: 缓存文件名（相对 output_dir）。
CACHE_DIR_NAME = "_cache"
CACHE_FILE_NAME = "embeddings.jsonl"

#: 本地退化向量的维度（512 足够让不同文本碰撞率可接受，又不用存太多）。
HASH_DIM = 512

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


class EmbeddingError(RuntimeError):
    """embedding 通道不可用（配置错误 / 端点失败），调用方应降级或报错。"""


@runtime_checkable
class Embedder(Protocol):
    """最小接口：一批文本 → 一批等长向量。"""

    name: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度；任一侧为零向量返回 0（不抛异常）。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


class HashEmbedder:
    """本地确定性退化：字符 n-gram（1–3）哈希到 :data:`HASH_DIM` 维后归一。

    它**不是**语义向量：只把「共享的字面片段」映射到相近方向（"幂等"与"幂等性"
    会靠近，"幂等"与"原子性"不会）。用途是让整条链路在没有网络时也能跑通、
    让测试可复现；生产环境请配真实 embedding 端点。
    """

    name = "hash"
    dim = HASH_DIM

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    @staticmethod
    def _one(text: str) -> list[float]:
        vector = [0.0] * HASH_DIM
        lowered = (text or "").lower()
        tokens = _TOKEN_RE.findall(lowered)
        grams: list[str] = list(tokens)
        for token in tokens:
            grams.extend(token[index:index + 2] for index in range(len(token) - 1))
            grams.extend(token[index:index + 3] for index in range(len(token) - 2))
        for gram in grams:
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % HASH_DIM
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


class OpenAIEmbedder:
    """OpenAI 兼容的 ``/v1/embeddings`` 通道（复用项目已有的 provider 配置）。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        dim: int = 0,
        client: Any | None = None,
    ) -> None:
        if not model:
            raise EmbeddingError("embedding 模型名为空")
        self.model = model
        self.dim = dim
        self.name = f"openai:{model}"
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key or "not-needed",
                                 base_url=base_url or None, max_retries=0)
        self._client = client

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = await self._client.embeddings.create(
                model=self.model, input=list(texts))
        except Exception as exc:  # noqa: BLE001 —— SDK 异常族随版本变化
            raise EmbeddingError(
                f"embedding 调用失败（{self.model}）：{type(exc).__name__}: {exc}"
            ) from exc
        vectors = [list(item.embedding) for item in response.data]
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"embedding 返回条数不符：请求 {len(texts)} 条，返回 {len(vectors)} 条")
        if vectors and self.dim == 0:
            self.dim = len(vectors[0])
        return vectors


class CachedEmbedder:
    """包一层磁盘缓存：命中不再调用内层通道（省钱、也让重跑可复现）。"""

    def __init__(self, inner: Embedder, cache_path: Path) -> None:
        self._inner = inner
        self.path = Path(cache_path)
        self.name = f"cached:{inner.name}"
        self.dim = inner.dim
        self._cache: dict[str, list[float]] | None = None

    def _load(self) -> dict[str, list[float]]:
        if self._cache is not None:
            return self._cache
        cache: dict[str, list[float]] = {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        cache[str(item["key"])] = [float(v) for v in item["vector"]]
                    except (KeyError, TypeError, ValueError):
                        continue     # 坏行跳过：缓存不该让检索整体失败
        except OSError:
            pass
        self._cache = cache
        return cache

    def _key(self, text: str) -> str:
        return hashlib.sha256(
            f"{self._inner.name}\x00{text}".encode()).hexdigest()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        cache = self._load()
        keys = [self._key(text) for text in texts]
        missing = [index for index, key in enumerate(keys) if key not in cache]
        if missing:
            fresh = await self._inner.embed([texts[index] for index in missing])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                for index, vector in zip(missing, fresh, strict=False):
                    cache[keys[index]] = vector
                    handle.write(json.dumps(
                        {"key": keys[index], "model": self._inner.name,
                         "vector": vector}, ensure_ascii=False) + "\n")
            if self.dim == 0 and fresh:
                self.dim = len(fresh[0])
        return [cache[key] for key in keys]


def build_embedder(
    settings: Any,
    *,
    output_dir: Path,
    target: tuple[str, str, str | None] | None = None,
) -> Embedder | None:
    """按配置造一个 embedding 通道；未启用返回 ``None``（检索保持纯 BM25）。

    ``target`` 是 ``(模型名, api_key, base_url)``——由 **models 层**解析
    ``embedding_model`` 别名后注入（knowledge 包不允许依赖 models，分层守卫会
    抓这一类反向依赖）。``target`` 为 ``None`` 时降级为本地哈希向量：没配端点、
    没配密钥、别名解析失败都走这条路，行为一致且可复现。
    """
    if not getattr(settings, "embedding_enabled", False):
        return None
    dim = int(getattr(settings, "embedding_dim", 0) or 0)
    cache_path = Path(output_dir) / CACHE_DIR_NAME / CACHE_FILE_NAME
    if target is None:
        return CachedEmbedder(HashEmbedder(), cache_path)
    model, api_key, base_url = target
    if not model:
        return CachedEmbedder(HashEmbedder(), cache_path)
    inner = OpenAIEmbedder(model=model, api_key=api_key,
                           base_url=base_url, dim=dim)
    return CachedEmbedder(inner, cache_path)


def is_semantic_embedder(embedder: Embedder | None) -> bool:
    """这个通道给的是不是**语义**向量。

    在只有 :class:`HashEmbedder` 兜底时答案是「不是」——它只对齐共享字面片段
    （"幂等"与"幂等性"近，"幂等"与"原子性"不近）。实测（``bench/rag-hitrate-2026-11-13.md``）：
    同一批 486 条查询、同一语料下，纯 BM25 的 hit@1 是 84.4%，按默认
    ``docs_rag_vector_weight=0.5`` 混进去反而掉到 82.9%、权重 1.0 掉到 53.3%。
    所以调用方在兜底通道下应当把向量权重压到 0，而不是照配置发车。
    """
    if embedder is None:
        return False
    if isinstance(embedder, HashEmbedder):
        return False
    inner = getattr(embedder, "_inner", None)
    if isinstance(inner, HashEmbedder):
        return False
    return True
