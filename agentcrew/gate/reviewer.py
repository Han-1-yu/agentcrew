"""G2 LLM 评审（spec §10.2）：验收标准逐条核对 + 多评审人聚合。

G1（:mod:`agentcrew.gate.static_checks`）零 token 拦下机器可判的问题之后 G2 才
被调用；本模块只产出一条 :class:`~agentcrew.task.task.ReviewRecord` 与一条
``gate_result`` 审计事件，**不改任何任务状态**——转 ``REWORK``/``SUCCEEDED`` 与
``rework_count`` 自增都是 Task 27 ``Gatekeeper`` 的职责。

四条设计决定：

- **受治理入口**：评审调用一律走 ``ModelFactory.complete_structured()``（同一个
  全局 LLM 信号量 + 同一段记账）。``models/structured.py`` 的裸阶梯只认 Provider，
  拿它直接调用会同时绕过限流与 ``llm_call`` 审计，spec §7.3 的限额与 §14.3 的
  运行报告都会缺掉整块评审开销。``purpose="review"`` 与 ``role=<评审角色>``
  让这笔账落在正确的角色与用途上。
- **格式错误 ≠ 质量驳回**：``reject`` 却不带任何实质 ``issues`` 的裁决对返工方
  毫无信息量——照着它返工只会烧掉返工额度。这类输出按「评审器故障」处理：重试
  ``format_retry_attempts`` 次，仍失败就放行并附 ``评审器返回格式错误，已放行``。
  JSON 彻底解析失败（``StructuredOutputError``，例如 review 夹具缺失）走同一条
  通道。重试**不碰** ``task.rework_count``：它不是一次质量驳回。
- **多评审人聚合**：全部 ``approve`` 才 ``approve``；任一 ``reject`` 即整体
  ``reject``，``issues``/``fix_hints`` 取并集（去重保序），``score`` 取最小值
  （悲观者胜）。``reviewer`` 是单评审人的 key，多评审人为
  ``"+".join(sorted(keys))``。
- **无评审人不阻塞**：``catalog.reviewer_for`` 返回空列表时直接放行
  （``reviewer="none"``）并写明原因——一个没有评审人的角色不该卡死整条流水线，
  更不该为「没人可问」付一次 LLM 调用。

评审材料（任务描述 + 验收标准 + 产物内容 + 返工历史）全部经 ``ArtifactStore``
读取、按 ``excerpt_budget_tokens`` 截断；产物只读**本任务已落盘**的声明路径
（G1 已保证必需产物存在），不存在的可选产物不读、不报错。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import Message
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.models.structured import StructuredOutputError
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import ReviewRecord, Task
from agentcrew.utils.tokens import estimate_tokens


class ReviewVerdict(BaseModel):
    """一次 LLM 评审的原始裁决（``complete_structured`` 的目标 schema）。"""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["approve", "reject"]
    score: float
    issues: list[str]
    fix_hints: list[str]


#: 与 ``ReviewVerdict`` 派生自同一处定义，供需要裸 schema 的调用方使用
#: （实际调用由降级链按级别注入或经 ``response_format`` 携带，不重复内嵌）。
REVIEW_OUTPUT_SCHEMA: dict[str, Any] = ReviewVerdict.model_json_schema()

#: 评审调用在 ledger 与 mock 回放里的 purpose（``MockProvider`` 据此查 reviews.json）。
_REVIEW_PURPOSE = "review"

#: ``RoleSpec.model`` 的哨兵值：字面量 ``"default"`` 表示按三级优先级解析。
_DEFAULT_ALIAS = "default"

#: 无评审人可用时的放行说明（写进 ``issues``，让报告能解释「为什么没评审」）。
_NO_REVIEWER_ISSUE = "无可用评审人，跳过 LLM 评审"
_NO_REVIEWER_KEY = "none"

#: 评审器重试耗尽仍是格式错误时的放行说明（spec §10.2）。
_FORMAT_ERROR_ISSUE = "评审器返回格式错误，已放行"

#: 产物节选被预算截断时附加在正文后的说明。
_TRUNCATION_NOTE = "（内容超出节选预算，已截断）"


@dataclass(frozen=True)
class _Opinion:
    """一个评审人的有效意见（已过格式校验；不是每个评审人都给得出 ``ReviewVerdict``）。

    格式错误放行时 ``score=None``：那是评审器故障，它的分数不可信，不能污染
    聚合后的最低分。
    """

    approved: bool
    score: float | None
    issues: tuple[str, ...]
    fix_hints: tuple[str, ...]


class Reviewer:
    """G2 评审器：按 ``catalog.reviewer_for`` 逐个评审人调用 LLM 并聚合。"""

    def __init__(
        self,
        models: ModelFactory,
        catalog: RoleCatalog,
        store: ArtifactStore,
        ledger: Ledger,
        *,
        excerpt_budget_tokens: int = 8000,
        format_retry_attempts: int = 2,
    ) -> None:
        self._models = models
        self._catalog = catalog
        self._store = store
        self._ledger = ledger
        self._excerpt_budget_tokens = excerpt_budget_tokens
        self._format_retry_attempts = format_retry_attempts

    async def review(self, task: Task, *, round_no: int) -> ReviewRecord:
        """评审一个任务，返回本轮的 ``ReviewRecord``（``layer="llm"``）。

        评审人是 ``catalog.reviewer_for(task.role)`` 的全部角色，按目录顺序
        **串行**评审：聚合语义（全 approve 才 approve）与「哪个评审人说了什么」
        都要求逐人拿到完整意见；mock 回放上下文的隔离已由 ``set_purpose`` 的
        contextvar 保证（C1），故串行是刻意保留的现有形状，不再是并发安全性的
        妥协（评审调用本身也已受全局信号量限流）。
        """
        reviewer_keys = self._catalog.reviewer_for(task.role)
        if not reviewer_keys:
            return self._finish(
                task, round_no,
                verdict="approve", score=None,
                issues=[_NO_REVIEWER_ISSUE], fix_hints=[],
                reviewer=_NO_REVIEWER_KEY)

        material = self._render_material(task, round_no)
        opinions: list[tuple[str, _Opinion]] = []
        for key in reviewer_keys:
            opinions.append((key, await self._opinion(key, task, material)))

        approved = all(opinion.approved for _, opinion in opinions)
        scores = [
            opinion.score for _, opinion in opinions if opinion.score is not None
        ]
        keys = [key for key, _ in opinions]
        overall: Literal["approve", "reject"] = "approve" if approved else "reject"
        return self._finish(
            task, round_no,
            verdict=overall,
            score=min(scores) if scores else None,
            issues=_dedup(
                issue for _, opinion in opinions for issue in opinion.issues),
            fix_hints=_dedup(
                hint for _, opinion in opinions for hint in opinion.fix_hints),
            reviewer=keys[0] if len(keys) == 1 else "+".join(sorted(keys)))

    # ── 单个评审人 ────────────────────────────────────────────────────────

    async def _opinion(self, reviewer_key: str, task: Task, material: str) -> _Opinion:
        """取一个评审人的有效意见；格式错误重试耗尽后按 ``approve`` 放行。

        重试**不修改任务**：评审器故障不是质量驳回，返工额度分文不动
        （返工计数归 Task 27 的 ``Gatekeeper``）。
        """
        spec = self._catalog.get(reviewer_key)
        alias = _resolve_alias(spec, self._models.settings)
        messages = _build_messages(spec, material)
        attempt = 0
        while True:
            attempt += 1
            verdict = await self._request(alias, reviewer_key, task, messages, spec)
            if verdict is not None and not _is_malformed(verdict):
                return _Opinion(
                    approved=verdict.verdict == "approve",
                    score=verdict.score,
                    issues=tuple(verdict.issues),
                    fix_hints=tuple(verdict.fix_hints))
            if attempt > self._format_retry_attempts:
                # 故障不阻塞：放行并留下可追溯的原因，让报告看得见这次降级。
                return _Opinion(
                    approved=True, score=None,
                    issues=(_FORMAT_ERROR_ISSUE,), fix_hints=())

    async def _request(
        self,
        alias: str,
        reviewer_key: str,
        task: Task,
        messages: list[Message],
        spec: RoleSpec,
    ) -> ReviewVerdict | None:
        """发一次评审调用；结构化输出彻底失败时返回 ``None``（按格式错误处理）。

        ``StructuredOutputError`` 是降级链与修复循环都拿不到合法 JSON 的终态
        （mock 的 review 夹具缺失正是这条路径）：与「reject 无 issues」同为
        格式错误，交给 :meth:`_opinion` 的重试→放行通道。其余异常（网络、
        配置、取消）**原样穿透**——基础设施故障不该被静默成一次放行。
        """
        provider = self._models.get(alias)
        if isinstance(provider, MockProvider):
            # purpose/task_id 是 mock 夹具的查找键，由调用方在每次调用前设置
            # （同 BaseAgent._configure_replay 的分工）。
            set_purpose(provider, _REVIEW_PURPOSE, role=reviewer_key, task_id=task.id)
        try:
            result = await self._models.complete_structured(
                alias,
                messages,
                ReviewVerdict,
                temperature=spec.temperature,
                max_tokens=spec.max_output_tokens,
                purpose=_REVIEW_PURPOSE,
                role=reviewer_key,
            )
        except StructuredOutputError:
            return None
        return cast(ReviewVerdict, result)

    # ── 评审材料 ──────────────────────────────────────────────────────────

    def _render_material(self, task: Task, round_no: int) -> str:
        """渲染评审材料：任务 + 验收标准 + 返工历史 + 产物节选。"""
        parts: list[str] = [
            "## 评审对象",
            f"- 任务 ID：{task.id}",
            f"- 标题：{task.title}",
            f"- 执行角色：{task.role}",
            f"- 本次是第 {round_no} 轮评审",
            "",
            "## 任务描述",
            task.description or "（无）",
            "",
            "## 验收标准",
        ]
        if task.acceptance_criteria:
            parts += [
                f"{index}. {criterion}"
                for index, criterion in enumerate(task.acceptance_criteria, 1)
            ]
        else:
            parts.append("（无验收标准）")
        parts += ["", "## 返工历史", *self._render_history(task), "", "## 产物内容"]
        parts += self._render_artifacts(task)
        return "\n".join(parts)

    @staticmethod
    def _render_history(task: Task) -> list[str]:
        """返工历史：返工次数 + 历次驳回的 issues/fix_hints（让评审人看到旧账）。"""
        lines = [f"- 已返工次数：{task.rework_count}"]
        for record in task.reviews:
            if record.verdict != "reject":
                continue
            lines.append(
                f"- 第 {record.round} 轮（{record.layer} 层）驳回："
                + "；".join(record.issues))
            if record.fix_hints:
                lines.append("  当时给出的修复建议：" + "；".join(record.fix_hints))
        if len(lines) == 1 and not task.rework_count:
            lines.append("- （无）")
        return lines

    def _render_artifacts(self, task: Task) -> list[str]:
        """产物节选：按契约声明顺序读**已落盘**的路径，共享一个 token 预算。

        先来先占、装不下即截断（与 ``ContextBus`` 的预算语义一致）：靠前的
        产物拿到完整内容，靠后的拿到空节选并附截断说明。未落盘的路径不读
        （``store.read`` 会抛 ``FileNotFoundError``）：必需产物缺失已归 G1，
        G2 只在该跑的时候跑。
        """
        produced = {record.path for record in self._store.by_task(task.id)}
        paths = [spec.path for spec in task.deliverables if spec.path in produced]
        if not paths:
            return ["（无产物）"]
        remaining = self._excerpt_budget_tokens
        blocks: list[str] = []
        for path in paths:
            excerpt, truncated = _truncate_lines(self._store.read(path), remaining)
            remaining = max(0, remaining - estimate_tokens(excerpt))
            block = f"### {path}\n{excerpt}"
            if truncated:
                block += f"\n{_TRUNCATION_NOTE}"
            blocks.append(block)
        return blocks

    # ── 出口 ──────────────────────────────────────────────────────────────

    def _finish(
        self,
        task: Task,
        round_no: int,
        *,
        verdict: Literal["approve", "reject"],
        score: float | None,
        issues: list[str],
        fix_hints: list[str],
        reviewer: str,
    ) -> ReviewRecord:
        """落一条 ``gate_result`` 并组装 ``ReviewRecord``——本模块的唯一出口。"""
        self._ledger.emit(
            "gate_result",
            task_id=task.id,
            round=round_no,
            layer="llm",
            verdict=verdict,
            score=score,
            issues=issues,
            reviewer=reviewer,
        )
        return ReviewRecord(
            task_id=task.id,
            round=round_no,
            layer="llm",
            verdict=verdict,
            score=score,
            issues=issues,
            fix_hints=fix_hints,
            reviewer=reviewer,
            created_at=datetime.now(),
        )


# ── 模块级辅助 ────────────────────────────────────────────────────────────


def _resolve_alias(spec: RoleSpec, settings: Settings) -> str:
    """spec §12.2 的三级优先级，与 ``BaseAgent._resolve_alias`` 一致。

    ``RoleSpec.model`` → ``settings.role_bindings[key]`` → ``settings.default_model``；
    字面量 ``"default"`` 是「没指定」的哨兵，不是模型名。评审人也遵守
    ``AGENTCREW_MODEL_<ROLE>`` 的角色级绑定，否则评审会悄悄跑在另一个模型
    （和另一个价钱）上。
    """
    if spec.model != _DEFAULT_ALIAS:
        return spec.model
    return settings.role_bindings.get(spec.key) or settings.default_model


def _is_malformed(verdict: ReviewVerdict) -> bool:
    """``reject`` 却不带任何实质 ``issues``（全空白也算无）：返工方无从下手。"""
    if verdict.verdict != "reject":
        return False
    return not any(issue.strip() for issue in verdict.issues)


def _dedup(items: Iterable[str]) -> list[str]:
    """去重并保持首次出现顺序（并集语义 + 确定性输出，跨评审人重复项只留一份）。"""
    return list(dict.fromkeys(items))


def _build_messages(spec: RoleSpec, material: str) -> list[Message]:
    """评审对话：系统提示词钉死评审要求，用户消息装全部评审材料。

    ``ReviewVerdict`` 的 JSON Schema **不在这里内嵌**：降级链第 2/3 级会自己把
    它注入系统消息，第 1 级由 ``response_format`` 携带——重复一遍只是白烧上下文
    （同 ``structured._messages_for_level`` 的理由）。
    """
    return [
        {"role": "system", "content": _system_prompt(spec)},
        {"role": "user", "content": material},
    ]


def _system_prompt(spec: RoleSpec) -> str:
    """评审系统提示词（spec §10.2 的三条硬要求：逐条核对 / 具体问题与修复 / 只输出 JSON）。"""
    return (
        f"你是{spec.title}（{spec.key}），现在以该角色的专业视角评审另一个角色的任务产出。"
        "评审要求：\n"
        "1. 严格依据验收标准逐条核对，逐条说明是否满足；\n"
        "2. 只有全部验收标准都满足时才给出 approve，否则给出 reject；\n"
        "3. 给出 reject 时必须写明具体问题（issues）与可执行的修复建议"
        "（fix_hints），两者都不得为空；\n"
        "4. verdict 取 \"approve\" 或 \"reject\"，score 为 0 到 1 的质量分，"
        "issues 与 fix_hints 为字符串列表；\n"
        "5. 只输出 JSON，不要任何解释文字。"
    )


def _truncate_lines(text: str, max_tokens: int) -> tuple[str, bool]:
    """按行贪心截断到 ``estimate_tokens <= max_tokens`` 的最大前缀。

    返回 ``(截断后的文本, 是否发生截断)``。与 ``context_bus._truncate_lines``
    同一语义（二分版），此处材料最多几十个产物、贪心即可，不值得再写一份二分。
    ``max_tokens <= 0`` 时非空文本一律截断为空串。
    """
    if estimate_tokens(text) <= max_tokens:
        return text, False
    kept: list[str] = []
    used = 0
    for line in text.splitlines(keepends=True):
        cost = estimate_tokens(line)
        if used + cost > max_tokens:
            break
        kept.append(line)
        used += cost
    return "".join(kept), True
