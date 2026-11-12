"""C5：文档 RAG——团队文档的分块索引与混合检索。

**定位**：用户把团队手册 / 规范 / 约定（Markdown / 文本）放进
``<output_dir>/knowledge/docs/``，平台在 run 启动时摄取分块；规划期的 PM
提示词与每个任务的上下文包按相关性注入最相关的片段。文档是用户自己放的，
平台**只读不写**。

三条边界（与经验库 R11/R27 同源）：

1. **BM25 为主**：复用 :class:`~agentcrew.knowledge.retrieval.BM25Index`
   （中文单字 + 二元组切分、IDF、长度归一、单字命中不算相关）。
2. **向量可选**：给了 embedder（离线哈希兜底或真实端点）就走「BM25 归一 +
   余弦」混合打分，与经验库的 R27 同一套公式。注意上下文组装是**同步**路径，
   所以 ``query_sync`` 只对本地 ``HashEmbedder`` 做同步向量计算；真实端点
   （异步）在文档检索里退化为纯 BM25——如实退化，不假装。
3. **增量摄取**：manifest 按 ``sha256(内容)`` 记录已摄取文档；内容没变的文档
   不重建，删掉的文档连同其块一并出索引。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentcrew.knowledge.embeddings import Embedder, HashEmbedder, cosine
from agentcrew.knowledge.retrieval import BM25Index

__all__ = ["DocChunk", "DocHit", "DocStore", "IngestResult", "MANIFEST_NAME"]

_logger = logging.getLogger(__name__)

MANIFEST_NAME = "_docs_manifest.json"

#: 支持的文档扩展名。
DOC_SUFFIXES = (".md", ".txt", ".markdown")

#: 单块目标长度（字符）；超长段落按此长度切段。
MAX_CHUNK_CHARS = 700

#: 相邻块的重叠（字符）：让跨块的段落不丢上下文。
CHUNK_OVERLAP = 80


class DocChunk(BaseModel):
    """一块文档片段（检索与注入的最小单位）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    """块 id：``sha1(doc 相对路径 + 序号)[:16]``。"""

    doc: str
    """来源文档的文件名（相对 docs 根）。"""

    heading: str
    """该块所属的最近一层 Markdown 标题；没有标题时为文件名。"""

    text: str


class DocHit(BaseModel):
    """一次检索的命中（``score`` 为 0~1 的混合分，已按批归一）。"""

    model_config = ConfigDict(extra="forbid")

    chunk: DocChunk
    score: float


class IngestResult(BaseModel):
    """一次摄取的结果；``rebuilt`` / ``skipped`` 按文档名分列。"""

    model_config = ConfigDict(extra="forbid")

    docs: int
    """本次索引中的文档总数。"""

    chunks: int
    """本次索引中的块总数。"""

    rebuilt: list[str]
    """内容有变化（或新出现）而重建索引的文档。"""

    skipped: list[str]
    """内容没变、直接复用已有块的文档。"""


def split_into_chunks(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """把一篇文档切成检索块：先按 Markdown 标题分节，超长节再按段落切。

    每块附带「最近一层标题」作为前缀，保证跨块检索时块自带上下文。
    """
    chunks: list[str] = []
    current_heading = ""
    buffer: list[str] = []

    def _flush() -> None:
        joined = "\n".join(buffer).strip()
        buffer.clear()
        if not joined:
            return
        prefixed = f"[{current_heading}] {joined}" if current_heading else joined
        if len(prefixed) <= max_chars:
            chunks.append(prefixed)
            return
        # 超长节：按段落切段，保留 CHUNK_OVERLAP 字符的尾部重叠；
        # 单个段落本身就超长的，硬切成 max_chars 长的段（长手册常见）。
        paragraphs = [p for p in prefixed.split("\n\n") if p.strip()]
        pieces: list[str] = []
        for paragraph in paragraphs:
            while len(paragraph) > max_chars:
                pieces.append(paragraph[:max_chars])
                paragraph = paragraph[max_chars - CHUNK_OVERLAP:]
            pieces.append(paragraph)
        piece = ""
        for paragraph in pieces:
            if len(piece) + len(paragraph) + 2 > max_chars and piece:
                chunks.append(piece.strip())
                piece = piece[-CHUNK_OVERLAP:] + "\n\n" + paragraph
            else:
                piece = f"{piece}\n\n{paragraph}" if piece else paragraph
        if piece.strip():
            chunks.append(piece.strip())

    for line in text.splitlines():
        if line.startswith("#") and line.lstrip("#").strip():
            _flush()
            current_heading = line.lstrip("#").strip()
            continue
        buffer.append(line)
    _flush()
    return chunks


class DocStore:
    """``<output_dir>/knowledge/docs/`` 的摄取与检索器。

    ``embedder`` 可选：给了（通常是被 :class:`~agentcrew.knowledge.embeddings.
    CachedEmbedder` 包住的真实端点或哈希兜底）就走混合打分；没给 = 纯 BM25。
    """

    def __init__(
        self,
        docs_dir: Path,
        *,
        cache_path: Path | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self._docs_dir = Path(docs_dir)
        self._manifest_path = self._docs_dir / MANIFEST_NAME
        self._embedder = embedder
        self._chunks: list[DocChunk] = []
        self._vectors: list[list[float]] = []
        self._index: BM25Index | None = None

    @property
    def docs_dir(self) -> Path:
        """文档根目录。"""
        return self._docs_dir

    async def ingest(self) -> IngestResult:
        """扫描文档根目录并（增量）重建索引；目录不存在视为空库。

        manifest 记录每篇文档的内容摘要与分块结果：内容没变的文档直接复用
        已有块（不重复分块），被删除的文档连同其块一起出索引。
        """
        manifest = self._load_manifest()
        rebuilt: list[str] = []
        skipped: list[str] = []
        chunks: list[DocChunk] = []
        seen: set[str] = set()
        if self._docs_dir.is_dir():
            for path in sorted(self._docs_dir.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in DOC_SUFFIXES:
                    continue
                if path.name == MANIFEST_NAME:
                    continue
                relative = path.relative_to(self._docs_dir).as_posix()
                seen.add(relative)
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    _logger.warning("文档读取失败 %s: %s", relative, exc)
                    continue
                digest = hashlib.sha256(
                    text.encode("utf-8", errors="replace")).hexdigest()
                if manifest.get(relative) == digest:
                    cached = self._manifest_chunks(manifest, relative)
                    if cached:
                        skipped.append(relative)
                        chunks.extend(cached)
                        continue
                rebuilt.append(relative)
                doc_chunks: list[DocChunk] = []
                for order, chunk_text in enumerate(split_into_chunks(text)):
                    doc_chunks.append(DocChunk(
                        id=hashlib.sha1(
                            f"{relative}:{order}".encode()).hexdigest()[:16],
                        doc=relative,
                        heading=_heading_of(chunk_text, relative),
                        text=chunk_text,
                    ))
                chunks.extend(doc_chunks)
                manifest[relative] = digest
                manifest[f"{relative}#chunks"] = [
                    chunk.model_dump(mode="json") for chunk in doc_chunks]
        # 被删除的文档：manifest 里清掉，块自然不再出现在索引里。
        for key in [key for key in manifest if not key.endswith("#chunks")]:
            if key not in seen:
                manifest.pop(key, None)
                manifest.pop(f"{key}#chunks", None)
        self._save_manifest(manifest)
        self._chunks = chunks
        await self._rebuild_index()
        return IngestResult(
            docs=len({chunk.doc for chunk in chunks}),
            chunks=len(chunks),
            rebuilt=rebuilt,
            skipped=skipped,
        )

    async def _rebuild_index(self) -> None:
        self._index = BM25Index([chunk.text for chunk in self._chunks])
        self._vectors = []
        if self._embedder is not None and self._chunks:
            self._vectors = await self._embedder.embed(
                [chunk.text for chunk in self._chunks])

    async def query(
        self, text: str, *, limit: int = 4, vector_weight: float = 0.5,
    ) -> list[DocHit]:
        """异步检索：支持真实端点 embedder（查询词也走向量），完整混合打分。"""
        if self._index is None or not self._chunks or limit <= 0:
            return []
        bm25_hits = self._index.search(text, limit * 4)
        if not bm25_hits:
            return []
        weight = min(1.0, max(0.0, float(vector_weight)))
        ceiling = max((score for _, score in bm25_hits), default=0.0) or 1.0
        query_vector: list[float] | None = None
        if self._embedder is not None and self._vectors:
            try:
                query_vector = (await self._embedder.embed([text]))[0]
            except Exception as exc:  # noqa: BLE001 —— 端点失败退回纯 BM25
                _logger.warning("文档查询向量失败，退回纯 BM25: %s", exc)
        scored: list[DocHit] = []
        for offset, score in bm25_hits:
            total = score / ceiling
            if query_vector is not None and offset < len(self._vectors):
                semantic = max(0.0, cosine(query_vector, self._vectors[offset]))
                total = (1.0 - weight) * total + weight * semantic
            scored.append(DocHit(chunk=self._chunks[offset], score=total))
        scored.sort(key=lambda hit: -hit.score)
        return [hit for hit in scored[:max(0, limit)] if hit.score > 0]

    def query_sync(
        self, text: str, *, limit: int = 4, vector_weight: float = 0.5,
    ) -> list[DocHit]:
        """同步检索（上下文组装路径）；真实端点 embedder 退化为纯 BM25。

        本地 :class:`HashEmbedder` 的向量计算是纯 CPU、确定性的，可以在同步
        路径里直接算；真实端点必须走事件循环，这里不碰——如实退化并写进
        配置 docstring，而不是在同步路径里偷偷起线程。
        """
        if self._index is None or not self._chunks or limit <= 0:
            return []
        bm25_hits = self._index.search(text, limit * 4)
        if not bm25_hits:
            return []
        weight = min(1.0, max(0.0, float(vector_weight)))
        ceiling = max((score for _, score in bm25_hits), default=0.0) or 1.0
        query_vector: list[float] | None = None
        if self._vectors and isinstance(self._embedder, HashEmbedder):
            query_vector = HashEmbedder._one(text)
        scored: list[DocHit] = []
        for offset, score in bm25_hits:
            total = score / ceiling
            if query_vector is not None and offset < len(self._vectors):
                semantic = max(0.0, cosine(query_vector, self._vectors[offset]))
                total = (1.0 - weight) * total + weight * semantic
            scored.append(DocHit(chunk=self._chunks[offset], score=total))
        scored.sort(key=lambda hit: -hit.score)
        return [hit for hit in scored[:max(0, limit)] if hit.score > 0]

    def _load_manifest(self) -> dict[str, Any]:
        if not self._manifest_path.is_file():
            return {}
        try:
            data = json.loads(
                self._manifest_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        try:
            self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self._manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as exc:
            _logger.warning("docs manifest 写盘失败: %s", exc)

    def _manifest_chunks(self, manifest: dict[str, Any], doc: str) -> list[DocChunk]:
        """从 manifest 里恢复未变化文档的块（避免重复分块）。"""
        cached = manifest.get(f"{doc}#chunks")
        if not isinstance(cached, list):
            return []
        out: list[DocChunk] = []
        for item in cached:
            try:
                out.append(DocChunk.model_validate(item))
            except Exception:  # noqa: BLE001 —— 坏行跳过，与 Ledger 读取宽容一致
                continue
        return out


def _heading_of(chunk_text: str, fallback: str) -> str:
    if chunk_text.startswith("[") and "]" in chunk_text:
        return chunk_text[1:chunk_text.index("]")]
    return fallback
