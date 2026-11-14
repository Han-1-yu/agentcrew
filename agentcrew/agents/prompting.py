"""提示词渲染：把「上下文包 + 任务契约」变成模型可读的 markdown（spec §11.2）。

``BaseAgent`` 负责**流程**（组装 → 调用 → 解析 → 校验 → 落盘），本模块负责**措辞**。
分开的理由不是美观：渲染是纯函数（输入决定输出、无状态、无 IO），可以逐条断言
「契约清单有没有出现」「截断有没有标注」；而基类里有事件、超时、重试与落盘，
混在一起时任何一个渲染细节都只能靠端到端测试覆盖。

全部函数都是模块级纯函数，只依赖传入的参数；``BaseAgent`` 以带下划线的旧名字
（``_render_pack`` 等）再导出，既有调用点与测试不受影响。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from agentcrew.artifacts.parser import normalize_lang
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.context.context_bus import (
    TRUNCATION_NOTICE,
    ContextPack,
    DecisionRecord,
    FileExcerpt,
    UpstreamDigest,
)
from agentcrew.task.task import Task
from agentcrew.utils.files import UnsafePathError, safe_relpath

__all__ = [
    "START_FENCE_RE",
    "TRUNCATED_FINISH_REASONS",
    "canonical_path",
    "fence_languages",
    "is_truncated",
    "join_notes",
    "language_notes",
    "render_decisions",
    "render_deliverables",
    "render_excerpts",
    "render_pack",
    "render_task",
    "render_upstream",
    "section",
    "truncate_summary",
]

#: 起始围栏行：与 ``artifacts/parser.py`` 钉死的语法一致（顶格、≥3 个反引号、
#: 可选语言名、可选 ``file=`` 到行尾）。只用于一致性提示，不裁决任何事。
START_FENCE_RE = re.compile(
    r"^(?P<fence>`{3,})(?P<lang>[A-Za-z0-9_+.\-]*)(?:\s+file=(?P<path>.*?))?\s*$"
)

#: provider 表示「回复撞上输出上限」的停止原因：OpenAI 兼容端点是 ``length``，
#: Anthropic 是 ``max_tokens``。两者含义相同，都必须与「模型写完了」区分开。
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})


def render_pack(pack: ContextPack, fix_hints: Sequence[str]) -> str:
    """把上下文包渲染成 user 消息（spec §11.2 六级内容的可读形态）。

    末两段是**通道**而非信息：``fix_hints`` 是返工的唯一解释（模型必须看到），
    ``pack.truncated`` 时逐字追加 :data:`TRUNCATION_NOTICE`，免得模型把「没给
    全文」误当成「上游不存在」。
    """
    sections = [
        section("项目目标", pack.project_goal),
        section("本任务", render_task(pack.task)),
    ]
    # C10：用户中途指引。刻意排在「本任务」之后、上游摘要之前——它是对计划的
    # 实时修正，优先级高于历史摘要；交付物契约仍在末尾（近因效应，见下方注释）。
    # 关闭态 ``pack.guidance`` 为空，这一段不会出现（黄金用例钉住逐字节不变）。
    if pack.guidance:
        sections.append(section(
            "用户中途指引（优先于计划，与交付物契约冲突时先问清楚再改）",
            "\n".join(f"- {item}" for item in pack.guidance),
        ))
    if pack.upstream:
        sections.append(section("上游任务摘要", render_upstream(pack.upstream)))
    if pack.decisions:
        sections.append(section("全局决策", render_decisions(pack.decisions)))
    if pack.file_tree:
        sections.append(
            section("产物文件树", "\n".join(f"- {path}" for path in pack.file_tree))
        )
    if pack.key_excerpts:
        sections.append(section("关键文件节选", render_excerpts(pack.key_excerpts)))
    # 交付物契约放到**最后**（仅早于返工意见）。
    # 它是文件清单的唯一权威；而上面那些节选可能来自架构文档、带着另一套文件
    # 名与目录结构 —— 两者平级呈现时，实测真实模型会跟设计文档走：PM 契约写
    # `backend/src/app.js`，模型却产 `backend/src/server.js`，连续 3 次尝试全废、
    # 下游 5 个任务阻塞。放在末尾既给了优先级，也吃到近因效应。
    sections.append(
        section("交付物契约（文件清单以此为准）", render_deliverables(pack.task))
    )
    hints = [hint.strip() for hint in fix_hints if hint.strip()]
    if hints:
        sections.append(
            section("上次评审意见，请修正", "\n".join(f"- {h}" for h in hints))
        )
    text = "\n\n".join(sections)
    if pack.truncated:
        text = f"{text}\n\n{TRUNCATION_NOTICE}"
    return text


def section(title: str, body: str) -> str:
    """一个 markdown 二级标题段。"""
    return f"## {title}\n{body}"


def render_task(task: Task) -> str:
    """本任务的元信息与验收标准（描述与验收标准不可压缩，spec §11.2 级 1）。"""
    lines = [
        f"- id: {task.id}",
        f"- 角色: {task.role}",
        f"- 标题: {task.title}",
        f"- 描述: {task.description}",
    ]
    if task.brief:
        lines.append(f"- 摘要: {task.brief}")
    criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria)
    return "\n".join(lines) + f"\n\n验收标准：\n{criteria or '- （无）'}"


def render_deliverables(task: Task) -> str:
    """交付物契约：路径是模型唯一被允许写出的东西，必须逐条列清。"""
    if not task.deliverables:
        return "- （本任务无文件交付物）"
    lines: list[str] = [
        "**本清单是本次输出文件路径的唯一权威。** 上游设计文档若给出不同的文件名"
        "或目录，**以本清单为准**：先按下面的路径落盘，设计文档里的结构体现在文件"
        "内容里。输出任何未列出的路径都会让本次执行直接失败。"
    ]
    for spec in task.deliverables:
        lines.append(_deliverable_line(spec))
    return "\n".join(lines)


def _deliverable_line(spec: ArtifactSpec) -> str:
    """单条交付物：路径、类型、必需性与必须包含的符号。"""
    flags = "必需" if spec.required else "可选"
    line = f"- `{spec.path}`（{spec.kind}，{flags}）：{spec.description}"
    if spec.must_contain:
        line += f"｜必须包含：{'、'.join(spec.must_contain)}"
    return line


def is_truncated(completion: object) -> bool:
    """这次回复是不是被输出上限截断的？

    用 ``getattr`` 而不是类型标注，是为了让测试替身（只有 ``text``/``usage`` 的
    假 provider）也能走同一条路径。
    """
    reason = getattr(completion, "finish_reason", None) or ""
    return str(reason).lower() in TRUNCATED_FINISH_REASONS


def render_upstream(digests: Sequence[UpstreamDigest]) -> str:
    """上游摘要：只给 ``brief``/``summary``/文件路径，不给全文（spec §11.2 级 2）。"""
    blocks: list[str] = []
    for digest in digests:
        parts = [
            f"### {digest.task_id} · {digest.role} · {digest.title}",
            f"brief: {digest.brief}" if digest.brief else "",
            f"summary: {digest.summary}" if digest.summary else "",
            f"文件: {'、'.join(digest.files)}" if digest.files else "",
        ]
        blocks.append("\n".join(part for part in parts if part))
    return "\n\n".join(blocks)


def render_decisions(decisions: Sequence[DecisionRecord]) -> str:
    """全局决策记录（最新在前，由 ``ContextBus`` 决定顺序）。"""
    return "\n".join(
        f"- [{record.task_id} · {record.role}] {record.decision}"
        for record in decisions
    )


def render_excerpts(excerpts: Sequence[FileExcerpt]) -> str:
    """关键文件节选；被预算截断的逐条标注，避免模型把节选当全文。"""
    blocks: list[str] = []
    for excerpt in excerpts:
        title = f"### {excerpt.path}"
        if excerpt.truncated:
            title += "（仅节选，未全文展示）"
        blocks.append(f"{title}\n{excerpt.content}")
    return "\n\n".join(blocks)


def join_notes(*parts: str) -> str:
    """拼接说明段落，丢掉空段（``ParsedOutput.notes`` 原样保留）。"""
    return "\n".join(part for part in parts if part)


def truncate_summary(text: str, max_length: int) -> str:
    """截断摘要到 ``max_length`` 字符，超长时以省略号收尾（占位算进额度）。"""
    if max_length <= 0 or len(text) <= max_length:
        return text
    if max_length == 1:
        return "…"
    return text[: max_length - 1].rstrip() + "…"


def canonical_path(raw: str) -> str:
    """解析路径的契约口径：与 ``ArtifactSpec.path`` 同一条规范化。

    不安全路径（``../x``、绝对路径、Windows 保留名…）返回原样字符串：解析器
    刻意原样透传它们，好让诊断信息是模型真正写下的那个路径；这里它们永远不可能
    等于任何已声明路径，于是按「未声明」报出，而不是抛 ``UnsafePathError``。
    """
    try:
        return safe_relpath(raw).as_posix()
    except UnsafePathError:
        return raw


def language_notes(raw: str, task: Task) -> list[str]:
    """围栏语言名与契约 ``kind`` 的一致性交叉检查（只记提示，不判失败）。

    这是 ``normalize_lang``（``artifacts/parser.py``）的生产消费者：模型把 python
    文件标成 ``json`` 值得让人看见，却不值得判失败 —— 真正的结构校验是 G1 的职责，
    且以契约 ``kind`` 为准（这里落盘用的也正是契约里的 ``ArtifactSpec``）。
    只对契约声明过的路径比较；未声明的路径由 ``validate_output`` 负责。
    """
    declared = {spec.path: spec.kind for spec in task.deliverables}
    notes: list[str] = []
    for path, lang in fence_languages(raw).items():
        expected = declared.get(canonical_path(path))
        actual = normalize_lang(lang) if lang else None
        if expected is None or actual is None or actual == expected:
            continue
        notes.append(
            f"注意：{canonical_path(path)} 的围栏语言标注 {lang!r} 与契约声明的 "
            f"kind {expected!r} 不一致，已按契约 kind 处理"
        )
    return notes


def fence_languages(raw: str) -> dict[str, str | None]:
    """``raw`` 中每个 ``file=`` 块的围栏语言名（按路径，后写覆盖前写）。

    只服务于上面的一致性提示。它复刻 ``parser.py`` 钉死的围栏规则（开栏顶格且
    ≥3 个反引号；闭栏是长度不短于开栏的纯反引号行），因此 4 反引号块**体内**的
    示例行不会被误读成真实块 —— 那正是 README 里写运行命令的合法形态（Task 10
    的裁定：N 开 M 闭当且仅当 M ≥ N）。扫描结果永不参与裁决，与解析器失配的
    最坏后果只是一条多出或缺失的提示。
    """
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    found: dict[str, str | None] = {}
    index = 0
    while index < len(lines):
        match = START_FENCE_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        fence_length = len(match.group("fence"))
        close = len(lines)
        for candidate in range(index + 1, len(lines)):
            stripped = lines[candidate].strip()
            if (
                stripped
                and stripped.strip("`") == ""
                and len(stripped) >= fence_length
            ):
                close = candidate
                break
        path = (match.group("path") or "").strip().replace("\\", "/")
        if path:
            found[path] = match.group("lang").strip() or None
        index = close + 1
    return found
