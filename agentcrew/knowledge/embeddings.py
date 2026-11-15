"""R27：向量检索的 embedding 通道——真实端点 / 本地确定性退化 / 磁盘缓存。

三条边界（与项目「离线可跑」的红线对齐）：

1. **默认关闭**：``embedding_enabled=False`` 时检索仍是纯 BM25，行为与 R11 逐字
   一致；开启才走向量通道。
2. **无网络也能跑**：没配 API Key / 端点不可达时用 :class:`HashEmbedder` 兜底
   ——字符 n-gram 哈希到固定维度，**确定、可复现、零成本**，但语义质量明显弱于
   真实 embedding（它只捕捉字面重合，不捕捉近义）。文档与输出都要如实标注。
3. **不重复计费**：:class:`CachedEmbedder` 按 ``sha256(model + dim + text)`` 落盘缓存
   （``<output>/_cache/embeddings.jsonl``），同一文本第二次检索不再打网络；维度进键
   是为了防止「哈希 512 维」与「语义 1536 维」复用同一批缓存造成静默全 0 分。

混合打分在 :mod:`agentcrew.knowledge.experience` 里完成：BM25 分归一到 [0,1]
后与余弦相似度按 ``vector_weight`` 加权；权重 0 = 纯 BM25，1 = 纯向量。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: 缓存文件名（相对 output_dir）。
CACHE_DIR_NAME = "_cache"
CACHE_FILE_NAME = "embeddings.jsonl"

#: 本地退化向量的维度（512 足够让不同文本碰撞率可接受，又不用存太多）。
HASH_DIM = 512

#: OpenAI 兼容端点的默认地址（用户没配 ``embedding_base_url`` 时用）。
DEFAULT_EMBEDDING_BASE_URL = "https://api.openai.com/v1"

#: 单批失败的重试上限与退避上限（embedding 比对话调用便宜，但仍不该无限重试）。
_MAX_EMBEDDING_ATTEMPTS = 3
_MAX_EMBEDDING_BACKOFF = 2.0

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
    """OpenAI 兼容的 ``/v1/embeddings`` 通道（openai SDK 版，保留给测试替身）。

    生产装配走 :class:`HttpEmbedder`：它不依赖可选包、可控超时与批量，失败分流也
    更明确。这个类保留是因为既有测试用 ``client=`` 注入假 SDK 响应，且已装 openai
    的调用方可以继续用。
    """

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


class HttpEmbedder:
    """OpenAI 兼容 ``POST {base_url}/embeddings``——裸 urllib，零第三方依赖。

    与 :class:`OpenAIEmbedder`（可选 ``openai`` SDK）的分工：SDK 版留给测试替身与
    已装 SDK 的调用方；生产装配走这里，因为它可控超时、按 ``batch_size`` 切分、
    按状态码决定重不重试，且不会因为「没装 openai」让整条检索链路 import 失败。

    失败分流（逐级可观测，错误消息**不含密钥**）：

    * 401 / 402 / 403 与其余 4xx（429 除外）：配置或额度问题，重试无意义，直接抛
      :class:`EmbeddingError`；
    * 429 / 5xx / 网络错误 / 超时：有限重试（``max_attempts`` 次，指数退避上限 2s）；
    * 响应条数不符、维度不一致、JSON 坏形状：立即抛错，不重试（重试只会重复计费）。
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        base_url: str | None = None,
        dim: int = 0,
        timeout_seconds: float = 10.0,
        batch_size: int = 32,
        max_attempts: int = _MAX_EMBEDDING_ATTEMPTS,
        opener: Any | None = None,
    ) -> None:
        if not model:
            raise EmbeddingError("embedding 模型名为空")
        self.model = model
        self.dim = dim
        self.name = f"openai:{model}"
        self._api_key = api_key or ""
        self._base_url = (base_url or DEFAULT_EMBEDDING_BASE_URL).rstrip("/")
        self._timeout = max(0.1, float(timeout_seconds or 10.0))
        self._batch_size = max(1, int(batch_size or 32))
        self._max_attempts = max(1, int(max_attempts))
        self._opener = opener or urllib.request.build_opener()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start:start + self._batch_size])
            vectors.extend(await asyncio.to_thread(self._embed_batch_sync, batch))
        if vectors and self.dim == 0:
            self.dim = len(vectors[0])
        for vector in vectors:
            if len(vector) != self.dim or not vector:
                raise EmbeddingError(
                    f"embedding 维度不符（{self.model}）：期望 {self.dim}，"
                    f"实际 {len(vector)}")
        return vectors

    def _embed_batch_sync(self, batch: list[str]) -> list[list[float]]:
        payload = json.dumps(
            {"model": self.model, "input": batch},
            ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url}/embeddings"
        last_error = ""
        for attempt in range(self._max_attempts):
            request = urllib.request.Request(
                url, data=payload, headers=headers, method="POST")
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    body = response.read().decode("utf-8")
                return self._parse(body, len(batch))
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                last_error = f"HTTP {status}"
                if status != 429 and 400 <= status < 500:
                    raise EmbeddingError(
                        f"embedding 调用失败（{self.model}）：{last_error}"
                    ) from exc
            except urllib.error.URLError as exc:
                last_error = f"{type(exc).__name__}: {exc.reason}"
            except (TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except ValueError as exc:
                raise EmbeddingError(
                    f"embedding 响应不是合法 JSON（{self.model}）：{exc}"
                ) from exc
            if attempt + 1 < self._max_attempts:
                time.sleep(min(0.3 * (2 ** attempt), _MAX_EMBEDDING_BACKOFF))
        raise EmbeddingError(
            f"embedding 调用失败（{self.model}）：{last_error or 'unknown'}")

    def _parse(self, body: str, expected: int) -> list[list[float]]:
        payload = json.loads(body)
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) != expected:
            raise EmbeddingError(
                f"embedding 返回条数不符：请求 {expected} 条，"
                f"返回 {len(items) if isinstance(items, list) else '?'} 条")
        vectors: list[list[float]] = []
        for item in items:
            raw = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(raw, list) or not raw:
                raise EmbeddingError(
                    f"embedding 响应缺少向量（{self.model}）")
            try:
                vectors.append([float(value) for value in raw])
            except (TypeError, ValueError) as exc:
                raise EmbeddingError(
                    f"embedding 向量含非数值（{self.model}）") from exc
        return vectors


class CachedEmbedder:
    """包一层磁盘缓存：命中不再调用内层通道（省钱、也让重跑可复现）。"""

    def __init__(self, inner: Embedder, cache_path: Path) -> None:
        self._inner = inner
        self.path = Path(cache_path)
        self.name = f"cached:{inner.name}"
        self.dim = inner.dim
        #: 键里的维度在构造时固化：同一后端自动推断后仍能命中旧缓存，
        #: 而用户把 ``embedding_dim`` 从 0 改成显式值时旧缓存作废（保守但正确）。
        self._key_dim = int(getattr(inner, "dim", 0) or 0)
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
        # 维度必须进键：cosine 在长度不等时静默返回 0（见 cosine 的实现），
        # 「哈希 512 维」的旧缓存被「语义 1536 维」复用就会得到「不报错、全 0 分」
        # 的静默退化——正是 B5 那类「看起来测了、其实没测」的事故形态。
        return hashlib.sha256(
            f"{self._inner.name}|dim={self._key_dim}\x00{text}".encode()).hexdigest()

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
    # 启用真语义通道：裸 urllib + 可控超时 + 批量切分（不依赖可选 openai SDK）。
    inner = HttpEmbedder(
        model=model,
        api_key=api_key,
        base_url=base_url,
        dim=dim,
        timeout_seconds=float(
            getattr(settings, "embedding_timeout_seconds", 10.0) or 10.0),
        batch_size=int(
            getattr(settings, "embedding_batch_size", 32) or 32),
    )
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
