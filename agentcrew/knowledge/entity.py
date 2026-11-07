"""实体记忆：跨 run 累积命名实体，并在规划阶段注入 PM 上下文（R3）。

对标 CrewAI 的 entity memory 与 MetaGPT ``BrainMemory.knowledge``：经验库
（:mod:`agentcrew.knowledge.experience`）记录的是「哪类任务怎么解」，实体库补上
另一半 —— **这个项目里出现的名字**：技术选型、文件路径、角色、缩写、关键概念。
PM 拆解新目标时能看到「上次用的是 FastAPI、`src/api.py`、SSE」，避免同一套系统
每次换一套命名。

刻意保持轻量、可测、零依赖：

- **纯规则抽取**，不调 LLM：反引号内标识符、路径、``@role``、书名号/方括号概念、
  全大写缩写、常用技术词表。多一条噪音比多一次模型调用便宜，且结果确定。
- **不引入向量库**：默认「关键词命中 + 实体名加权排序」；R29 起可由调用方
  注入 embedding 向量走混合打分（与经验库同一套 embedder 与权重），
  默认关闭时行为与之前逐字一致。
- **追加写、读取时按名字归并**：与 ``ledger.jsonl`` / ``experiences.jsonl`` 一样
  「写入严、读取宽」，不会因一次崩溃留下半行就毁掉整库；``mentions`` 在归并时累加。
- 位于 ``knowledge`` 层，**不 import** ``runtime`` / ``crew`` / ``agents`` /
  ``models``（``test_architecture.py`` 的分层护栏）。装配与事件留痕全在 Crew 层。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.knowledge.embeddings import cosine
from agentcrew.knowledge.experience import Experience

_logger = logging.getLogger(__name__)

#: 与经验库同款分词（英文数字按词、CJK 按单字），用于上下文命中打分。
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")
_BACKTICK_RE = re.compile(r"`([^`\n]{1,80})`")
_PATH_RE = re.compile(r"(?<![\w`/])(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,10}")
_ROLE_RE = re.compile(r"@([a-z][a-z0-9_]{1,40})")
_BRACKET_RE = re.compile(r"《([^》\n]{1,40})》|【([^】\n]{1,40})】")
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9+#.-]{1,9}\b")
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")

#: 常见技术名词（小写比较）。只在文本里出现才收录，不凭空造实体。
_TECH_TERMS = (
    "python", "fastapi", "flask", "django", "pytest", "pydantic", "asyncio",
    "docker", "kubernetes", "redis", "postgresql", "mysql", "sqlite", "mongodb",
    "react", "vue", "typescript", "javascript", "nodejs", "nginx", "graphql",
    "websocket", "restful", "jwt", "oauth", "rbac", "orm", "json", "yaml",
    "sqlite3", "pytest-qt", "llm", "rag", "sdk", "cli", "sse", "mcp", "dag",
    "curl", "git", "linux", "windows", "html", "css", "sql", "api", "http",
)
#: 中文领域词（本项目的稳定概念），避免单字分词把「的」「了」当成实体。
_CN_TERMS = (
    "任务面板", "交付清单", "产物契约", "经验库", "实体记忆", "角色绑定",
    "依赖图", "状态机", "人工卡点", "门禁", "待办清单", "事件审计",
)
#: 全大写缩写里这些不是实体，只是行文里的普通词。
_STOP_ACRONYMS = frozenset((
    "I", "A", "OK", "TODO", "NOTE", "TIP", "FAQ", "EOF", "UTF", "GET", "POST",
    "PUT", "N", "X", "Y", "T", "S", "P", "M", "PM", "QA", "UI", "UX", "ID",
))


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def extract_entities(text: str, *, max_entities: int = 12) -> list[tuple[str, str]]:
    """从一段文本里抽取 ``(实体名, 类型)``，按出现顺序去重，最多 ``max_entities`` 条。

    类型：``file`` / ``technology`` / ``role`` / ``concept`` / ``acronym``。
    这是纯函数，不落盘、不调模型 —— 同样的输入永远给出同样的结果。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, kind: str) -> None:
        cleaned = name.strip().strip("`").strip()
        if not cleaned or len(cleaned) > 80 or cleaned.casefold() in seen:
            return
        seen.add(cleaned.casefold())
        found.append((cleaned, kind))

    for match in _BACKTICK_RE.finditer(text):
        value = match.group(1)
        kind = "file" if ("/" in value or _FILE_EXT_RE.search(value)) else "technology"
        add(value, kind)
    for match in _PATH_RE.finditer(text):
        add(match.group(0), "file")
    for match in _ROLE_RE.finditer(text):
        add(match.group(1), "role")
    for match in _BRACKET_RE.finditer(text):
        add(match.group(1) or match.group(2), "concept")
    for match in _ACRONYM_RE.finditer(text):
        value = match.group(0)
        if value not in _STOP_ACRONYMS and len(value) >= 2:
            add(value, "acronym")

    lowered = text.lower()
    for term in _TECH_TERMS:
        if len(term) >= 3 and term in lowered:
            add(term, "technology")
    for term in _CN_TERMS:
        if term in text:
            add(term, "concept")

    return found[: max(0, max_entities)]


class EntityRecord(BaseModel):
    """一条跨 run 归并后的实体记录。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    context: str = ""  # 最近一次出现时的一句话上下文
    sources: list[str] = Field(default_factory=list)  # 来源 run_id（去重）
    mentions: int = 1
    updated_at: datetime


def entities_from_experience(
    experience: Experience, *, run_id: str, max_entities: int = 8
) -> list[EntityRecord]:
    """把一条成功经验转成实体记录（纯函数，可单测）。

    抽取来源包含任务描述、解决方案摘要、关键决策与产物路径 —— 产物路径往往是
    最稳定的「命名事实」。上下文取解决方案摘要（无则回落任务描述），截到 160 字。
    """
    parts = (
        experience.task_description,
        experience.solution_summary,
        " ".join(experience.key_decisions),
        " ".join(experience.artifacts),
    )
    text = " ".join(part for part in parts if part)
    context = (experience.solution_summary or experience.task_description)[:160]
    now = datetime.now()
    return [
        EntityRecord(
            name=name,
            kind=kind,
            context=context,
            sources=[run_id],
            mentions=1,
            updated_at=now,
        )
        for name, kind in extract_entities(text, max_entities=max_entities)
    ]


class EntityMemoryStore:
    """全局实体库：``output_dir/knowledge/entities.jsonl``，跨 run 共享。

    写入是**追加**（与经验库一致，历史不可覆盖）；读取时按实体名大小写不敏感归并，
    ``mentions`` 累加、``sources`` 并集、``context`` 取最近一次。坏行跳过并记
    warning，不让一条崩溃截断的尾行毁掉整个库。
    """

    def __init__(self, output_dir: Path) -> None:
        self._path = Path(output_dir) / "knowledge" / "entities.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def append(self, records: Sequence[EntityRecord]) -> None:
        if not records:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(record.model_dump_json() + "\n")

    def all(self) -> list[EntityRecord]:
        """读取并按名字归并；文件不存在 / 坏行 → 空库 / 跳过。"""
        if not self._path.is_file():
            return []
        merged: dict[str, EntityRecord] = {}
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            _logger.warning("实体记忆库无法读取，按空库处理 %s: %s", self._path, exc)
            return []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = EntityRecord.model_validate_json(stripped)
            except ValueError:
                _logger.warning("跳过结构未知的实体记忆行 %s", self._path)
                continue
            key = record.name.casefold()
            existing = merged.get(key)
            if existing is None:
                merged[key] = record.model_copy(deep=True)
                continue
            newer = record.updated_at >= existing.updated_at
            merged[key] = existing.model_copy(update={
                "sources": list(dict.fromkeys(
                    existing.sources + record.sources))[:10],
                "mentions": existing.mentions + record.mentions,
                "context": record.context if newer else existing.context,
                "updated_at": max(existing.updated_at, record.updated_at),
            })
        return sorted(merged.values(), key=lambda item: (-item.mentions, item.name))

    @staticmethod
    def haystack(record: EntityRecord) -> str:
        """一个实体参与向量化的文本：名字 + 类别 + 上下文。

        与经验库的 ``haystack`` 对称——换 embedding 通道时不用改这一层。
        """
        return "\n".join((record.name, record.kind, record.context))

    def query(
        self,
        text: str,
        *,
        limit: int = 5,
        min_score: int = 2,
        doc_vectors: Sequence[Sequence[float]] | None = None,
        query_vector: Sequence[float] | None = None,
        vector_weight: float = 0.5,
        min_similarity: float = 0.15,
    ) -> list[EntityRecord]:
        """按关键词检索相关实体：名字命中 +3 分，上下文每命中一个 token +1 分。

        ``min_score=2`` 是降噪阈值：中文单字分词下，只沾上一个字的实体不该被
        塞进 PM 上下文。无任何命中时返回空列表，而不是把整库端上来。

        R29 向量混合：给了 ``doc_vectors``（与 :meth:`all` 同序）与
        ``query_vector`` 时，关键词分归一到 [0,1] 后与余弦按 ``vector_weight``
        加权。**关键词没过阈值（名字/上下文都没沾上）但余弦 ≥
        ``min_similarity`` 的实体也会被召回**——这正是向量通道的价值：
        「同一个东西换个说法」也能对上。权重 0 时行为与 R29 之前一致。
        """
        tokens = _tokenize(text or "")
        records = self.all()
        if not tokens:
            return []
        scored: list[tuple[int, EntityRecord]] = []
        for record in records:
            name = record.name.lower()
            score = 3 if any(token in name for token in tokens) else 0
            context_tokens = _tokenize(record.context)
            score += sum(1 for token in tokens if token in context_tokens)
            if score >= min_score:
                scored.append((score, record))
        if (doc_vectors is None or query_vector is None
                or len(doc_vectors) != len(records)):
            scored.sort(key=lambda item: (-item[0], -item[1].mentions, item[1].name))
            return [record for _, record in scored[: max(0, limit)]]

        weight = min(1.0, max(0.0, float(vector_weight)))
        ceiling = max((item[0] for item in scored), default=0) or 1
        combined: list[tuple[float, EntityRecord]] = []
        keyword_ids = {id(record) for _, record in scored}
        for index, record in enumerate(records):
            keyword_score = 0.0
            if id(record) in keyword_ids:
                raw = next(score for score, item in scored if item is record)
                keyword_score = raw / ceiling
            semantic = max(0.0, cosine(query_vector, doc_vectors[index]))
            if semantic >= min_similarity or keyword_score > 0:
                total = (1.0 - weight) * keyword_score + weight * semantic
                combined.append((total, record))
        combined.sort(key=lambda item: (-item[0], -item[1].mentions, item[1].name))
        return [record for _, record in combined[: max(0, limit)]]

    def inject_context(
        self,
        text: str,
        *,
        limit: int = 5,
        doc_vectors: Sequence[Sequence[float]] | None = None,
        query_vector: Sequence[float] | None = None,
        vector_weight: float = 0.5,
    ) -> str:
        """渲染成可直接拼进 PM ``context_hint`` 的文本；无命中返回空串。"""
        records = self.query(
            text, limit=limit, doc_vectors=doc_vectors,
            query_vector=query_vector, vector_weight=vector_weight)
        if not records:
            return ""
        lines = [
            "## 实体记忆（跨 run 累积）",
            "以下是历史成功 run 里出现过的命名事实，拆解任务时请沿用既有命名与技术选型：",
        ]
        for record in records:
            brief = record.context.replace("\n", " ")[:120]
            lines.append(
                f"- `{record.name}`（{record.kind}，累计 {record.mentions} 次）：{brief}"
            )
        return "\n".join(lines)
