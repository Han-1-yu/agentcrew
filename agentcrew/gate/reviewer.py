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

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.gate.verdict import ReviewVerdict, derive_review
from agentcrew.gate.verdict import is_malformed as _is_malformed
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import Message
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.models.structured import StructuredOutputError
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import ReviewRecord, Task
from agentcrew.utils.clock import utc_now
from agentcrew.utils.files import UnsafePathError
from agentcrew.utils.tokens import estimate_tokens
from agentcrew.utils.trace import TraceContext, reset_trace, set_trace

#: 评审调用在 ledger 与 mock 回放里的 purpose（``MockProvider`` 据此查 reviews.json）。
_REVIEW_PURPOSE = "review"

#: 门禁评审的成本归因前缀：``gate:<评审角色>``。
#: 它不是 ``AgentProfile``（门禁评审没有执行体），但必须能进 ``llm.by_profile``，
#: 否则这笔开销会落进 ``unknown``，看起来像"账没算清"。
_GATE_PROFILE_PREFIX = "gate:"

#: ``RoleSpec.model`` 的哨兵值：字面量 ``"default"`` 表示按三级优先级解析。
_DEFAULT_ALIAS = "default"

#: 无评审人可用时的放行说明（写进 ``issues``，让报告能解释「为什么没评审」）。
_NO_REVIEWER_ISSUE = "无可用评审人，跳过 LLM 评审"
_NO_REVIEWER_KEY = "none"

#: 评审器重试耗尽仍是格式错误时的放行说明（spec §10.2）。
_FORMAT_ERROR_ISSUE = "评审器返回格式错误，已放行"

#: 产物节选被预算截断时附加在正文后的说明。
_TRUNCATION_NOTE = "（内容超出节选预算，已截断）"

#: 验收标准里可能被引用的**其他任务产物**的路径形态。
#:
#: 背景（realrun9 实测）：PM 会写出「README 的参数与 ``logstat/cli.py`` 实现一致」
#: 这类跨任务标准，而 G2 过去只渲染本任务的交付物 —— 标准结构上无法核对，评审员
#: 只能连续驳回，返工额度烧光后任务 FAILED、下游 BLOCKED。这不是评审员苛刻，是
#: 材料不全。所以这里按标准文本里的路径把它们补进「参考材料」。
_REFERENCE_PATH_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_./-]*\."
    r"(?:py|md|json|ya?ml|toml|txt|js|ts|html|css|sh|sql|ini|cfg|go|rs|java)\b"
)

#: 单次评审最多补几个参考文件：防止一条标准里塞满路径把预算吃光。
_MAX_REFERENCE_FILES = 3


@dataclass(frozen=True)
class _Opinion:
    """一个评审人的有效意见（已过格式校验；不是每个评审人都给得出 ``ReviewVerdict``）。

    格式错误放行时 ``score=None``：那是评审器故障，它的分数不可信，不能污染
    聚合后的最低分。

    ``suggestions`` 是标准之外的改进意见：**只进 ledger 与报告，不参与裁决、
    不引发返工**。评审员总会有意见，给它们一条不阻塞的出口，是「还能更好不是
    驳回理由」这条规则能落地的前提。
    """

    approved: bool
    score: float | None
    issues: tuple[str, ...]
    fix_hints: tuple[str, ...]
    suggestions: tuple[str, ...] = ()


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
        # P2-5：限制并行评审人数（None = 用 catalog 给出的全部评审人）。由
        # Gatekeeper 从 gate_config 的 g2_reviewers 写入。
        self.max_reviewers: int | None = None

    async def review(
        self,
        task: Task,
        *,
        round_no: int,
        reviewer_keys: Sequence[str] | None = None,
        layer: Literal["static", "llm", "human", "supervisor"] = "llm",
    ) -> ReviewRecord:
        """评审一个任务，返回本轮的 ``ReviewRecord``（默认 ``layer="llm"``）。

        评审人默认是 ``catalog.reviewer_for(task.role)`` 的全部角色。P1-2 起多评审人
        通过 ``asyncio.gather`` **并行**执行（单评审人保持原串行路径）：聚合语义
        （全 approve 才 approve）与「哪个评审人说了什么」都要求逐人拿到完整意见；
        mock 回放上下文的隔离已由 ``set_purpose`` 的 contextvar 保证（C1），评审
        调用本身也已受全局信号量限流，并行不会突破并发上限。

        ``reviewer_keys``（R7）显式指定评审人，绕过目录的静态 ``reviewer_for``——
        「谁派的活谁验收」需要按**任务实例**决定评审人（主管是动态的），目录里
        没有这种关系。``layer`` 让这条记录在审计里与常规 G2 区分开
        （``supervisor``），按层统计驳回数时不会混在一起。
        """
        keys = (
            list(reviewer_keys) if reviewer_keys is not None
            else self._catalog.reviewer_for(task.role)
        )
        # P2-5：gate_config.g2_reviewers 限制评审人数（取前 N，保持目录顺序）。
        if self.max_reviewers is not None:
            keys = list(keys[:self.max_reviewers])
        if not keys:
            return self._finish(
                task, round_no,
                verdict="approve", score=None,
                issues=[_NO_REVIEWER_ISSUE], fix_hints=[],
                reviewer=_NO_REVIEWER_KEY, layer=layer)

        material = self._render_material(task, round_no)
        # P1-2：多评审人并行（asyncio.gather），单评审人保持原串行路径。
        # mock 的 contextvar 隔离由 set_purpose 在每个 _opinion 协程内独立设置，
        # 已安全；评审调用本身也受全局信号量限流，并行不会突破并发上限。
        if len(keys) == 1:
            key = keys[0]
            opinions: list[tuple[str, _Opinion]] = [
                (key, await self._opinion(key, task, material)),
            ]
        else:
            results = await asyncio.gather(
                *[self._opinion(key, task, material) for key in keys]
            )
            # 两边都由 keys 派生，长度必然一致；strict 让不一致当场炸，
            # 而不是悄悄少评一位
            opinions = list(zip(keys, results, strict=True))

        approved = all(opinion.approved for _, opinion in opinions)
        scores = [
            opinion.score for _, opinion in opinions if opinion.score is not None
        ]
        keys = [key for key, _ in opinions]
        overall: Literal["approve", "reject"] = "approve" if approved else "reject"
        # P2-3：issues 在跨评审人取并集后再做模糊去重（规范化 + Jaccard 合并）；
        # fix_hints 仍走精确去重（它是逐条 evidence，措辞差异不该被并成一条）。
        raw_issues = [
            issue for _, opinion in opinions for issue in opinion.issues]
        issues, removed = _deduplicate_tracked(raw_issues)
        if removed:
            self._ledger.emit(
                "review_dedup",
                task_id=task.id,
                round=round_no,
                original_count=len(raw_issues),
                deduped_count=len(issues),
                removed_issues=removed,
            )
        return self._finish(
            task, round_no,
            verdict=overall,
            score=min(scores) if scores else None,
            issues=issues,
            fix_hints=_dedup(
                hint for _, opinion in opinions for hint in opinion.fix_hints),
            suggestions=_dedup(
                item for _, opinion in opinions for item in opinion.suggestions),
            reviewer=keys[0] if len(keys) == 1 else "+".join(sorted(keys)),
            layer=layer)

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
            if verdict is not None and not _is_malformed(verdict, task):
                approved, issues, fix_hints = derive_review(verdict, task)
                return _Opinion(
                    approved=approved,
                    score=verdict.score,
                    issues=tuple(issues),
                    fix_hints=tuple(fix_hints),
                    suggestions=tuple(verdict.suggestions))
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
            # ``attempt=0``：门禁评审**不分轮次**。用 0 而不是默认的 1，是为了
            # 避开角色内复核的按轮次夹具（``"T-003@1"``）——两者都在 attempt=1
            # 时，门禁会误读"第 1 轮修订前"的裁决，把好产物驳回。
            set_purpose(provider, _REVIEW_PURPOSE, role=reviewer_key,
                        task_id=task.id, attempt=0)
        # 门禁评审不是"某个执行体在干活"，但它的花费必须可归因，否则
        # ``llm.by_profile`` 里会出现一大坨 ``unknown``（实测 7 次里有 6 次是它）。
        # 用 ``gate:<评审角色>`` 作为合成 profile：既区别于执行体，也能看出是哪个
        # 评审角色的账单。
        trace_token = set_trace(TraceContext(
            task_id=task.id, role=reviewer_key, attempt=task.attempts,
            profile=f"{_GATE_PROFILE_PREFIX}{reviewer_key}"))
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
        finally:
            reset_trace(trace_token)
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
        own_blocks = self._render_artifacts(task)
        parts += own_blocks
        references = self._render_references(task, own_blocks)
        if references:
            parts += ["", *references]
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
            excerpt, truncated = _truncate_lines(
                self._store.read_for_prompt(path), remaining)
            remaining = max(0, remaining - estimate_tokens(excerpt))
            block = f"### {path}\n{excerpt}"
            if truncated:
                block += f"\n{_TRUNCATION_NOTE}"
            blocks.append(block)
        return blocks

    def _referenced_paths(self, task: Task) -> list[str]:
        """验收标准/任务描述里点到、且确实已落盘的其他任务产物。

        只认 ``store.exists`` 为真的路径：标准里出现的路径可能还没产出（上游失败）、
        可能是举例，也可能根本不是本 run 的文件 —— 一律静默跳过，不报错、不占位。
        """
        own = {spec.path for spec in task.deliverables}
        text = "\n".join([*(task.acceptance_criteria or []), task.description or ""])
        found: list[str] = []
        for raw in _REFERENCE_PATH_RE.findall(text):
            rel = raw.replace("\\", "/")
            while rel.startswith("./"):      # 只剥「当前目录」前缀，不吞掉 ".."
                rel = rel[2:]
            if not rel or rel in own or rel in found:
                continue
            if rel.startswith("/") or ".." in rel.split("/"):
                continue                     # 绝对路径/越界路径：绝不读盘
            try:
                if not self._store.exists(rel):
                    continue
            except UnsafePathError:  # 越界路径当不存在，绝不因标准文本而读盘出错
                continue
            found.append(rel)
            if len(found) >= _MAX_REFERENCE_FILES:
                break
        return found

    def _render_references(self, task: Task, own_blocks: list[str]) -> list[str]:
        """参考材料节选：与本任务产物**共享**同一个 token 预算，先本任务后参考。

        参考材料只在验收标准真的引用了其他产物时才出现；标题里写明「不是本任务
        交付物」，避免评审员把「没交付这些文件」当成不满足。
        """
        paths = self._referenced_paths(task)
        if not paths:
            return []
        used = sum(estimate_tokens(block) for block in own_blocks)
        remaining = max(0, self._excerpt_budget_tokens - used)
        blocks = [
            "## 参考材料（验收标准引用了以下文件：仅用于核对一致性，"
            "**不是本任务的交付物**，不要求本任务产出）"
        ]
        if remaining <= 0:
            blocks.append("（节选预算已被本任务产物用满，未能附上参考文件）")
            return blocks
        for path in paths:
            if remaining <= 0:
                blocks.append("（预算用尽，其余参考文件未附）")
                break
            excerpt, truncated = _truncate_lines(
                self._store.read_for_prompt(path), remaining)
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
        suggestions: list[str] | None = None,
        layer: Literal["static", "llm", "human", "supervisor"] = "llm",
    ) -> ReviewRecord:
        """落一条 ``gate_result`` 并组装 ``ReviewRecord``——本模块的唯一出口。

        ``suggestions`` 只进 ledger：评审员在标准之外的意见需要一个不阻塞的出口，
        但它们**不参与裁决、不写进 ``ReviewRecord.issues``** —— 一旦写进去，
        Task 27 的 Gatekeeper 就会把它们当成返工理由，等价于恢复到「开放式评审」。
        R19 起主管层（``layer="supervisor"``）有非空建议时额外发一条
        ``supervisor_suggestions``，方便订阅与汇总；仍然只是审计事件。

        ``layer``（R7）：默认 ``"llm"``（常规 G2）。主管审核传 ``"supervisor"``，
        让审计与按层统计能把「机器评审驳回」和「派活的人驳回」分开。
        """
        self._ledger.emit(
            "gate_result",
            task_id=task.id,
            round=round_no,
            layer=layer,
            verdict=verdict,
            score=score,
            issues=issues,
            suggestions=list(suggestions or ()),
            reviewer=reviewer,
        )
        # R19：主管建议需要一条独立、可订阅的出口。它虽然也混在 gate_result
        # 的 suggestions 里，但那四层裁决事件混在一起，消费方无法只订阅
        # 「派活的人说了什么」。这里不发 fix_hints、不改 verdict、不碰额度——
        # 建议仍然只进 ledger，绝不参与裁决。
        if layer == "supervisor" and suggestions:
            self._ledger.emit(
                "supervisor_suggestions",
                task_id=task.id,
                supervisor=reviewer,
                round=round_no,
                verdict=verdict,
                suggestions=list(suggestions),
            )
        return ReviewRecord(
            task_id=task.id,
            round=round_no,
            layer=layer,
            verdict=verdict,
            score=score,
            issues=issues,
            fix_hints=fix_hints,
            reviewer=reviewer,
            created_at=utc_now(),
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


def _dedup(items: Iterable[str]) -> list[str]:
    """去重并保持首次出现顺序（并集语义 + 确定性输出，跨评审人重复项只留一份）。"""
    return list(dict.fromkeys(items))


#: 评审意见相似度阈值（P2-3）：token 集合 Jaccard 相似度超过此值视为重复。
_ISSUE_SIMILARITY_THRESHOLD = 0.8


def normalize_issue_text(text: str) -> str:
    """规范化一条评审意见：小写、去标点、压缩空白（P2-3 去重与收敛指纹共用）。

    去标点只留单词字符与空白：CJK 字符在 ``re`` 的 str 默认（``re.UNICODE``）下
    属于 ``\\w``，不会被抹掉；中英文标点一律剔除。
    """
    stripped = re.sub(r"[^\w\s]", "", text.lower())
    return " ".join(stripped.split())


def _issue_tokens(normalized: str) -> set[str]:
    """规范化意见切出的 token 集合（空白分词）。"""
    return {token for token in normalized.split() if token}


def _is_similar_issue(a: str, b: str) -> bool:
    """两条**已规范化**意见是否重复：完全相等，或 token 集合 Jaccard > 阈值。"""
    if a == b:
        return True
    tokens_a, tokens_b = _issue_tokens(a), _issue_tokens(b)
    if not tokens_a or not tokens_b:
        return False
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b) > _ISSUE_SIMILARITY_THRESHOLD


def deduplicate_issues(issues: list[str]) -> list[str]:
    """对评审意见去重并保持首次出现顺序（P2-3）。

    两级去重：先规范化（小写 / 去标点 / 压缩空白）后精确比较；规范化仍不同的，
    再按 token 集合 Jaccard 相似度 > 0.8 合并相似意见。以已保留者为基准，新意见
    与任一保留意见重复即丢弃（保序、确定性）。
    """
    kept: list[str] = []
    for issue in issues:
        normalized = normalize_issue_text(issue)
        if any(_is_similar_issue(normalize_issue_text(existing), normalized)
               for existing in kept):
            continue
        kept.append(issue)
    return kept


def _deduplicate_tracked(issues: list[str]) -> tuple[list[str], list[str]]:
    """:func:`deduplicate_issues` 的记账版：额外返回被丢弃的意见（ledger 留痕）。"""
    kept: list[str] = []
    removed: list[str] = []
    for issue in issues:
        normalized = normalize_issue_text(issue)
        if any(_is_similar_issue(normalize_issue_text(existing), normalized)
               for existing in kept):
            removed.append(issue)
        else:
            kept.append(issue)
    return kept, removed


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
    """评审系统提示词（spec §10.2：G2 的判据是**验收标准**，不是开放式代码评审）。

    关键是**不向模型要结论**。过去让它直接给 approve/reject，等于让它对「这份
    产出好不好」下开放式判断 —— 一个认真的评审员每轮都能再挑出毛病，返工永远
    收敛不了（run6 实测：问题数 7→12→8，额度烧光、下游 5 个任务阻塞）。现在只
    问它逐条判定既定标准，裁决由 :func:`derive_review` 推导。
    """
    return (
        f"你是{spec.title}（{spec.key}），现在以该角色的专业视角评审另一个角色的任务产出。\n"
        "**你的唯一判据是本次评审材料「验收标准」一节里逐条列出的标准**，"
        "不要扩大范围，也不要自行补充标准。\n"
        "要求：\n"
        "1. 对验收标准的**每一条**给出一个 judgement，用 criterion_index 指明是第几条"
        "（**从 0 起**，与材料里的编号一致）；\n"
        "2. **一条都不能漏，也不要多**：judgements 的数量必须与验收标准的条数完全相等；\n"
        "3. satisfied 为 true/false；evidence 写产物中支持该判定的具体依据"
        "（文件名 + 关键内容）；若标准要求与「参考材料」里的文件比对，"
        "**必须打开参考材料核对后再判定**，不得以「本次材料未提供该文件」为由判为不满足；\n"
        "4. satisfied 为 false 时，evidence 必须写清「产物里缺什么、要改成什么」，"
        "让返工方能直接照做；含糊的「不够好」不合格；\n"
        "5. **标准之外的任何意见**（可读性、健壮性、更优写法、性能、风格……）一律放进 "
        "suggestions —— 它们**不构成不满足**，也不会导致返工。"
        "「还能更好」永远不是驳回理由；\n"
        "6. score 是 0 到 1 的诊断性质量分，**不参与裁决**；\n"
        "7. 只输出 JSON，不要任何解释文字。"
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
