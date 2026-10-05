"""产物契约与落盘记录（spec §5.2）。

``ArtifactSpec`` 是任务交付物的契约（发给 Agent、供 G1/G2 校验），
``ArtifactRecord``/``Manifest`` 是一次 run 实际落盘产物的审计记录。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentcrew.utils.files import safe_relpath

ArtifactKind = Literal[
    "python", "json", "yaml", "toml", "markdown", "text",
    "html", "css", "js", "dockerfile", "sql", "shell",
]


class ArtifactSpec(BaseModel):
    """单个交付物的契约：相对路径、类型与最低要求。"""

    model_config = ConfigDict(extra="forbid")

    path: str  # 相对 artifacts/ 根，必须是 POSIX 相对路径
    kind: ArtifactKind
    description: str  # 该文件应当包含什么
    required: bool = True
    min_bytes: int = 1
    must_contain: list[str] = Field(default_factory=list)
    max_bytes: int = 512 * 1024  # 单文件上限，防模型跑飞

    @field_validator("path")
    @classmethod
    def _normalize_path(cls, raw: str) -> str:
        """按 spec §9.3 校验路径并归一化（``./a.py`` → ``a.py``）。

        ``safe_relpath`` 抛出的 ``UnsafePathError`` 是 ``ValueError`` 子类，
        Pydantic 将其包成 ``ValidationError``。归一化结果回写字段：Task 11
        ``ArtifactStore.commit`` 用解析路径与 ``spec.path`` 全等比较。
        """
        return safe_relpath(raw).as_posix()


class ArtifactRecord(BaseModel):
    """一个实际落盘产物的记录（清单条目）。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: ArtifactKind
    size_bytes: int
    sha256: str
    produced_by_task: str
    produced_by_role: str
    written_at: datetime


class Manifest(BaseModel):
    """一次 run 的产物清单（``MANIFEST.json``）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    generated_at: datetime
    entries: list[ArtifactRecord]
