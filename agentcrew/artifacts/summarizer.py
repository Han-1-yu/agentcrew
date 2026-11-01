"""产物正文智能摘要（P3-6）。

对**落盘产物的正文**做结构性摘要，替代 :meth:`~agentcrew.agents.base_agent.BaseAgent.summarize`
原来只取 ``notes`` 前 400 字符的做法。摘要策略按产物 ``kind`` 区分：

- ``markdown``：提取标题层级（``#`` / ``##`` / ``###``）与首个非空段落，生成结构摘要；
- ``python``：提取 ``def`` / ``class`` 定义行与模块级 docstring，生成接口摘要；
- ``json`` / ``yaml`` / ``toml``：解析后提取顶层键及其值类型，生成 schema 摘要；
- ``html`` / ``css`` / ``js``：提取 ``<title>``/``<h1>``/id、顶层选择器、函数名；
- 其他（``text`` / ``dockerfile`` / ``sql`` / ``shell``）：取前 N 行 + 总行数与
  字符数统计。

全部为**纯函数**：不调用 LLM，避免额外成本与不确定性。输出统一前缀
``[<kind> 摘要] ...``，便于下游上下文包识别摘要来源。任何解析异常（畸形
JSON、损坏的 YAML 等）都**不抛错**——摘要只是辅助信息，绝不能因为它把
Agent 执行拖崩；异常一律回落取前若干行。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentcrew.artifacts.spec import ArtifactRecord
    from agentcrew.artifacts.store import ArtifactStore

#: 未知 / 无专项策略类型回落时取的前 N 行。
_FALLBACK_HEAD_LINES = 12
#: 单个产物摘要中标题 / 接口 / 键等条目的最大条数（防止大文件把摘要撑爆）。
_MAX_ENTRIES = 12

#: markdown 标题行（1~3 级）。
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")
#: python 函数 / 类定义行（含 async def）。
_DEF_RE = re.compile(
    r"^\s*(?:async\s+)?(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\s*[\(:]"
)
#: 模块级 docstring（首个三引号块），容许 r/u 前缀。
_DOCSTRING_RE = re.compile(
    r"^\s*(?:[rub]{0,2})\"\"\"(.*?)\"\"\"", re.DOTALL
)
#: yaml/toml 风格的顶层键（``key:`` 或 ``key =``）。
_FLAT_KEY_RE = re.compile(r"^([A-Za-z0-9_\-.]+)\s*[:=]")
#: html 标签内容清洗。
_TAG_RE = re.compile(r"<[^>]+>")


def summarize_artifact(
    path: str,
    content: str,
    *,
    kind: str,
    max_length: int = 500,
) -> str:
    """对单个产物正文生成结构性摘要，不超过 ``max_length`` 字符。

    ``path`` 仅用于在摘要开头标注来源文件名（人读）；``content`` 是产物全文。
    返回形如 ``[python 摘要] src/app.py: 定义：...；说明：...``。
    """
    kind_norm = (kind or "").strip().lower()
    try:
        if kind_norm == "markdown":
            body = _summarize_markdown(content)
        elif kind_norm == "python":
            body = _summarize_python(content)
        elif kind_norm in ("json", "yaml", "toml"):
            body = _summarize_structured(content, kind_norm)
        elif kind_norm in ("html", "css", "js"):
            body = _summarize_web(content, kind_norm)
        else:
            body = _summarize_fallback(content)
    except Exception:  # 摘要兜底：任何解析异常都不允许逃逸到 Agent 层
        body = _summarize_fallback(content)
    header = f"[{kind_norm or 'text'} 摘要] {path}: "
    return _truncate(header + body, max_length)


def summarize_records(
    records: list[ArtifactRecord],
    store: ArtifactStore,
    *,
    max_total: int = 1500,
) -> str:
    """对多个产物批量摘要，拼接成一段上下文文本。

    逐个经 :meth:`ArtifactStore.read` 取正文（带 ``max_bytes`` 上限防止大产物
    打爆内存），每个产物按 ``max_total / len(records)`` 分配额度。读取失败
    （产物被删等）的产物只标注路径、不中断批量摘要。最终整体截断到
    ``max_total``。空列表返回空串。
    """
    if not records:
        return ""
    per = max(80, max_total // max(1, len(records)))
    blocks: list[str] = []
    for record in records:
        try:
            content = store.read(record.path, max_bytes=64 * 1024)
        except Exception:  # noqa: BLE001 - 单个产物读失败不影响其余
            blocks.append(f"[{record.kind} 摘要] {record.path}：（正文不可读）")
            continue
        blocks.append(
            summarize_artifact(
                record.path, content, kind=record.kind, max_length=per
            )
        )
    return _truncate("\n".join(blocks), max_total)


# ── 各 kind 的专项策略 ────────────────────────────────────────────────


def _summarize_markdown(content: str) -> str:
    """提取标题层级（全文）与首个非空段落。"""
    headings: list[str] = []
    paragraph: list[str] = []
    paragraph_done = False
    in_front_matter = False
    lines = content.split("\n")
    # 跳过 YAML front matter（--- ... ---）。
    if lines and lines[0].strip() == "---":
        in_front_matter = True
    for line in lines:
        stripped = line.strip()
        if in_front_matter:
            if stripped == "---":
                in_front_matter = False
            continue
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            headings.append(("  " * (level - 1)) + match.group(2).strip())
            # 已有正文段落时，新标题视为段落结束（空段落不结束——H1 在正文之前）。
            if paragraph:
                paragraph_done = True
            continue
        if stripped == "":
            if paragraph:
                paragraph_done = True  # 首段已结束
            continue
        # 跳过代码围栏、分隔线、图片等，不进首段。
        if stripped.startswith(("#", "```", "---", "![")):
            continue
        if not paragraph_done:
            paragraph.append(stripped)
            if len(" ".join(paragraph)) > 140:
                paragraph_done = True
    parts: list[str] = []
    if headings:
        parts.append("标题：" + " / ".join(headings[:_MAX_ENTRIES]))
    first_paragraph = " ".join(paragraph)[:160]
    if first_paragraph:
        parts.append("首段：" + first_paragraph)
    if not parts:
        return _summarize_fallback(content)
    return "；".join(parts)


def _summarize_python(content: str) -> str:
    """提取 def/class 定义行与模块级 docstring。"""
    entries: list[str] = []
    for line in content.split("\n"):
        match = _DEF_RE.match(line)
        if match:
            label = "类" if match.group(1) == "class" else "函数"
            entries.append(f"{label}{match.group(2)}")
            if len(entries) >= _MAX_ENTRIES:
                break
    doc = _docstring_excerpt(content)
    parts: list[str] = []
    if entries:
        parts.append("定义：" + "、".join(entries))
    if doc:
        parts.append("说明：" + doc)
    if not parts:
        return _summarize_fallback(content)
    return "；".join(parts)


def _docstring_excerpt(content: str) -> str:
    """首个三引号 docstring 的首句摘录（≤160 字符）。"""
    match = _DOCSTRING_RE.search(content)
    if not match:
        return ""
    text = re.sub(r"\s+", " ", match.group(1)).strip()
    return text[:160]


def _summarize_structured(content: str, kind: str) -> str:
    """json/yaml/toml 顶层键与值类型摘要。"""
    data: Any = None
    if kind == "json":
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return _summarize_fallback(content)
    else:
        # yaml / toml：优先尝试 yaml，失败则按行提取扁平键。
        try:
            import yaml  # type: ignore[import-untyped]

            data = yaml.safe_load(content)
        except Exception:  # noqa: BLE001 - 损坏 yaml 回落行提取
            keys = _flat_keys(content)
            if keys:
                return "顶层键：" + "、".join(keys)
            return _summarize_fallback(content)
    return _describe_json(data)


def _flat_keys(content: str) -> list[str]:
    """按行提取 ``key:`` / ``key =`` 形式的顶层键（忽略注释与列表项）。"""
    keys: list[str] = []
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-", "//")):
            continue
        match = _FLAT_KEY_RE.match(stripped)
        if match:
            keys.append(match.group(1))
            if len(keys) >= _MAX_ENTRIES:
                break
    return keys


def _describe_json(data: Any) -> str:
    """把解析后的 JSON 值概括为顶层键 → 类型说明。"""
    if isinstance(data, dict):
        keys: list[str] = []
        for key, value in list(data.items())[:_MAX_ENTRIES]:
            label = _value_label(value)
            keys.append(f"{key}({label})")
        return f"顶层 {len(data)} 个键：" + "、".join(keys)
    if isinstance(data, list):
        return f"顶层为数组，{len(data)} 个元素"
    if data is None:
        return "顶层为 null"
    return f"顶层为 {type(data).__name__}"


def _value_label(value: Any) -> str:
    """JSON 值类型的短标签。"""
    if isinstance(value, dict):
        return f"对象×{len(value)}"
    if isinstance(value, list):
        return f"数组×{len(value)}"
    if isinstance(value, bool):
        return "布尔"
    if isinstance(value, str):
        return "字符串"
    if isinstance(value, (int, float)):
        return "数值"
    if value is None:
        return "空"
    return type(value).__name__


def _summarize_web(content: str, kind: str) -> str:
    """html/css/js 关键结构摘要。"""
    if kind == "html":
        title = re.search(
            r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL
        )
        h1s = re.findall(
            r"<h1[^>]*>(.*?)</h1>", content, re.IGNORECASE | re.DOTALL
        )
        ids = re.findall(r"""id=["']([^"']+)["']""", content)
        parts: list[str] = []
        if title:
            parts.append(
                "标题：" + _TAG_RE.sub("", title.group(1)).strip()[:120]
            )
        if h1s:
            cleaned = [_TAG_RE.sub("", h).strip() for h in h1s[:3]]
            parts.append("H1：" + " / ".join(c for c in cleaned if c))
        if ids:
            parts.append("关键 id：" + "、".join(ids[:_MAX_ENTRIES]))
        if parts:
            return "；".join(parts)
        return _summarize_fallback(content)

    if kind == "css":
        selectors: list[str] = []
        for line in content.split("\n"):
            stripped = line.strip()
            if not stripped.endswith("{"):
                continue
            selector = stripped[:-1].strip()
            if selector and not selector.startswith(("@", "/*")):
                selectors.append(selector)
                if len(selectors) >= _MAX_ENTRIES:
                    break
        if selectors:
            return "选择器：" + "、".join(selectors)
        return _summarize_fallback(content)

    # js：function 名 + 被赋值为函数的常量名。
    named = re.findall(
        r"function\s+([A-Za-z_$][\w$]*)"
        r"|(?:?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function)",
        content,
    )
    names = [a or b for a, b in named][:_MAX_ENTRIES]
    if names:
        return "符号：" + "、".join(names)
    return _summarize_fallback(content)


def _summarize_fallback(content: str) -> str:
    """无专项策略时：前 N 行 + 总行数 / 字符数统计。"""
    lines = content.split("\n")
    total_lines = len(lines)
    total_chars = len(content)
    head = re.sub(r"\s+", " ", "\n".join(lines[:_FALLBACK_HEAD_LINES])).strip()
    head = head[:200]
    return (
        f"前 {_FALLBACK_HEAD_LINES} 行（共 {total_lines} 行 / "
        f"{total_chars} 字符）：{head}"
    )


def _truncate(text: str, max_length: int) -> str:
    """截断到 ``max_length`` 字符，超长时以省略号收尾（占位算进额度）。"""
    if max_length <= 0 or len(text) <= max_length:
        return text
    if max_length == 1:
        return "…"
    return text[: max_length - 1].rstrip() + "…"
