"""评测用例与结果的数据形状（P2-5）。

数据集是 **JSONL**：一行一条 case，空行与 ``#`` 注释行跳过。选 JSONL 而不是 YAML：
case 会越攒越多（几十上百条），JSONL 天然可 diff、可 append、可被任何语言读，
也避免「缩进写错整份失效」这种事故。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "CaseOutput",
    "CaseResult",
    "CheckResult",
    "DatasetError",
    "EvalCase",
    "load_dataset",
]


class DatasetError(ValueError):
    """数据集本身不合法（JSON 坏、字段缺失、check 类型不认识）。"""


class EvalCase(BaseModel):
    """一条评测用例。

    ``sample`` 指向 ``examples/samples`` 里已登记的样例（离线夹具随样例走）；
    ``goal`` 只在「不绑定样例」时用（真实 provider 的探索性 case）。二者必须给一个。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    sample: str | None = None
    goal: str | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    #: 按 case 覆盖的 Settings 字段（如 ``{"tools_enabled": True}``）。
    #: 只用于「同一份夹具、不同开关」的对照 case。
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_must_be_slug(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("case.id 不能为空")
        if "/" in text or "\\" in text or text in (".", ".."):
            raise ValueError(f"case.id 不能含路径分隔符：{text!r}")
        return text

    @field_validator("checks")
    @classmethod
    def _checks_must_be_objects(
        cls, value: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        for index, check in enumerate(value or []):
            if not isinstance(check, dict):
                raise ValueError(f"checks[{index}] 必须是对象")
            if not str(check.get("kind") or "").strip():
                raise ValueError(f"checks[{index}] 缺少 kind")
        return value


class CheckResult(BaseModel):
    """单条断言的结论。``detail`` 在失败时给出「期望 vs 实际」。"""

    model_config = ConfigDict(extra="forbid")

    kind: str
    ok: bool
    detail: str = ""


class CaseOutput(BaseModel):
    """一次运行的可断言快照（产物全文 + 状态 + 事件计数）。"""

    model_config = ConfigDict(extra="forbid")

    status: str = ""
    run_id: str = ""
    artifacts: dict[str, str] = Field(default_factory=dict)
    """产物相对路径 → 全文（只收文本产物；二进制跳过）。"""

    task_status: dict[str, str] = Field(default_factory=dict)
    """task_id → 终态（``succeeded`` / ``failed`` / …）。"""

    event_counts: dict[str, int] = Field(default_factory=dict)
    """ledger 事件名 → 次数。"""

    cost_usd: float = 0.0
    """本次 run 的估算成本（mock 通道恒为 0；真实通道才有区分度）。

    矩阵对比要靠它回答"换成便宜档到底省了多少"——没有这个字段，A/B 只剩通过率。
    """

    error: str = ""
    """运行本身失败时的原因（此时 checks 一律判失败）。"""


class CaseResult(BaseModel):
    """一条 case 的**一次**运行结果；``repeat`` 时由 runner 聚合。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    ok: bool
    checks: list[CheckResult] = Field(default_factory=list)
    status: str = ""
    error: str = ""

    # ── 成本/过程指标（R45 矩阵对比用；默认 0 保持旧调用方兼容）─────────────
    cost_usd: float = 0.0
    llm_calls: int = 0
    revise_rounds: int = 0
    """复核回灌主笔的轮数（``agent_revise`` 事件数）。"""

    @property
    def failures(self) -> list[CheckResult]:
        return [item for item in self.checks if not item.ok]


def load_dataset(path: Path | str) -> list[EvalCase]:
    """读 JSONL 数据集；任何结构问题都以 :class:`DatasetError` 抛出（带行号）。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise DatasetError(f"数据集不存在：{file_path}")
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for number, raw in enumerate(
        file_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{file_path}:{number} 不是合法 JSON：{exc}") from None
        if not isinstance(data, dict):
            raise DatasetError(f"{file_path}:{number} 必须是一个 JSON 对象")
        try:
            case = EvalCase.model_validate(data)
        except ValueError as exc:
            raise DatasetError(f"{file_path}:{number} 用例不合法：{exc}") from None
        if case.sample is None and case.goal is None:
            raise DatasetError(
                f"{file_path}:{number} 必须给 sample（离线样例）或 goal（自定义目标）")
        if case.id in seen:
            raise DatasetError(f"{file_path}:{number} case.id 重复：{case.id}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise DatasetError(f"{file_path} 里没有可执行的用例")
    return cases


def summarize_checks(results: Iterable[CheckResult]) -> str:
    """一句话概括：``3/4 通过``（报告与 CLI 共用）。"""
    items = list(results)
    passed = sum(1 for item in items if item.ok)
    return f"{passed}/{len(items)} 通过"
