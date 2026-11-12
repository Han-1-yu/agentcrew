"""联网搜索工具 ``web_search``（C1）：可插拔提供方 + 离线可测，默认关闭。

**为什么要做。** 在 C1 之前，仓库只有 :class:`~agentcrew.tools.network.WebFetchTool`
（单页只读抓取），``network.py`` 开头还明写着「``web_search`` 依赖外部搜索 API 与
密钥，本轮不引入」。结果是调研类目标根本做不了：Agent 只能抓它已经知道的 URL，
无法「先搜再读」。这是与市面通用 agent 最直观的一处能力差。

设计上的四条边界（与仓库既有口径一致）：

- **密钥绝不落盘**：只从环境变量读（``AGENTCREW_SEARCH_API_KEY``，或提供方专属的
  ``TAVILY_API_KEY`` / ``BRAVE_API_KEY``）。配置里只有一个提供方名字。
- **默认关闭**：``search_provider`` 为空时不注册 ``web_search``——不配密钥就没有
  这个工具，模型看不见，也不会在提示词里被承诺一个跑不了的能力。
- **离线可测**：:class:`MockSearchProvider` 用固定夹具回放，端到端用例与评测不
  碰网络；真实通道另跑一次 smoke。
- **不放大 SSRF 面**：端点写死为公网 https，不跟随重定向（
  :func:`~agentcrew.tools.network.build_no_redirect_opener`），返回结果里的 URL
  先过 :func:`~agentcrew.tools.network.validate_fetch_url` 再回灌——即使提供方被
  投毒返回 ``http://169.254.169.254/``，模型也拿不到那个地址。

本模块位于 ``tools`` 层，不依赖 ``runtime`` / ``crew`` / ``agents``。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from pydantic import Field

from agentcrew.tools.network import build_no_redirect_opener, validate_fetch_url
from agentcrew.tools.tool import Tool

__all__ = [
    "DEFAULT_MAX_CHARS",
    "BraveSearchProvider",
    "DuckDuckGoSearchProvider",
    "MockSearchProvider",
    "SearchProvider",
    "SearchResult",
    "TavilySearchProvider",
    "WebSearchTool",
    "build_search_provider",
]

#: 回灌给模型的默认正文长度与硬上限（字符）。
DEFAULT_MAX_CHARS = 8000
MAX_MAX_CHARS = 30000

#: 单次搜索最多取多少条（与 ``search_max_results`` 的上限一致）。
MAX_RESULTS = 20

#: 请求体硬上限：搜索结果是摘要，不需要几百 KB。
_MAX_RESPONSE_BYTES = 512 * 1024

#: 通用的搜索密钥环境变量名（优先于提供方专属变量）。
GENERIC_KEY_ENV = "AGENTCREW_SEARCH_API_KEY"

#: 提供方 → 专属密钥环境变量。
PROVIDER_KEY_ENV: dict[str, str] = {
    "tavily": "TAVILY_API_KEY",
    "brave": "BRAVE_API_KEY",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class SearchResult:
    """一条搜索结果。``snippet`` 可能为空（有的提供方只给标题与链接）。"""

    title: str
    url: str
    snippet: str = ""

    def clean(self) -> SearchResult:
        return SearchResult(
            title=_clean_text(self.title)[:300],
            url=self.url.strip(),
            snippet=_clean_text(self.snippet)[:600],
        )


def _clean_text(text: str) -> str:
    """去 HTML 标签、压空白：提供方的摘要里常带 ``<b>`` 高亮。"""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text or "")).strip()


def _read_limited(response: Any, limit: int = _MAX_RESPONSE_BYTES) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"响应超过 {limit} 字节上限")
    return data


class SearchProvider(ABC):
    """搜索提供方接口：一个查询 → 一组 :class:`SearchResult`。"""

    name: str = ""

    @abstractmethod
    async def search(
        self, query: str, *, limit: int, timeout: float
    ) -> list[SearchResult]:
        """执行一次搜索。网络/解析失败抛异常，由工具收敛成错误文本。"""

    @property
    def api_key(self) -> str:
        """本提供方的密钥（只从环境变量读，绝不进配置/日志/快照）。"""
        for env_name in (GENERIC_KEY_ENV, PROVIDER_KEY_ENV.get(self.name, "")):
            if env_name:
                value = os.environ.get(env_name, "").strip()
                if value:
                    return value
        return ""

    def key_env_hint(self) -> str:
        """缺少密钥时给用户看的变量名（不含任何密钥值）。"""
        specific = PROVIDER_KEY_ENV.get(self.name, "")
        if specific:
            return f"{specific}（或 {GENERIC_KEY_ENV}）"
        return GENERIC_KEY_ENV

    def require_key(self) -> str:
        key = self.api_key
        if not key:
            raise ValueError(
                f"搜索提供方 {self.name} 缺少 API Key：请设置 "
                f"{self.key_env_hint()} 环境变量")
        return key


def _request_json(
    url: str,
    *,
    timeout: float,
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    method: str = "GET",
) -> dict[str, Any]:
    """同步发一次请求并解析 JSON（在 ``to_thread`` 里跑，不阻塞事件循环）。"""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "User-Agent": "AgentCrew-WebSearch/1.0",
            "Accept": "application/json",
            **dict(headers or {}),
        },
    )
    with build_no_redirect_opener().open(request, timeout=timeout) as response:
        body = _read_limited(response)
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"搜索接口返回的不是合法 JSON：{exc}") from None
    if not isinstance(parsed, dict):
        raise ValueError("搜索接口返回的 JSON 不是对象")
    return parsed


def _request_text(
    url: str,
    *,
    timeout: float,
    form: Mapping[str, str],
    headers: Mapping[str, str] | None = None,
) -> str:
    """同步发一次表单 POST 并返回文本（DuckDuckGo 的 HTML 端点）。"""
    request = Request(
        url,
        data=urlencode(dict(form)).encode("utf-8"),
        method="POST",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; AgentCrew-WebSearch/1.0)"),
            "Accept": "text/html,application/xhtml+xml",
            "Content-Type": "application/x-www-form-urlencoded",
            **dict(headers or {}),
        },
    )
    with build_no_redirect_opener().open(request, timeout=timeout) as response:
        return _read_limited(response).decode("utf-8", errors="replace")


class TavilySearchProvider(SearchProvider):
    """Tavily（``https://api.tavily.com/search``，专为 LLM 场景设计）。"""

    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    async def search(
        self, query: str, *, limit: int, timeout: float
    ) -> list[SearchResult]:
        key = self.require_key()
        payload = {
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        data = await asyncio.to_thread(
            _request_json, self.endpoint, timeout=timeout, payload=payload,
            headers={"Authorization": f"Bearer {key}"}, method="POST",
        )
        raw = data.get("results")
        if not isinstance(raw, list):
            return []
        return [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or ""),
            )
            for item in raw
            if isinstance(item, Mapping)
        ]


class BraveSearchProvider(SearchProvider):
    """Brave Search API（``https://api.search.brave.com/res/v1/web/search``）。"""

    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    async def search(
        self, query: str, *, limit: int, timeout: float
    ) -> list[SearchResult]:
        key = self.require_key()
        url = f"{self.endpoint}?{urlencode({'q': query, 'count': limit})}"
        data = await asyncio.to_thread(
            _request_json, url, timeout=timeout,
            headers={
                "X-Subscription-Token": key,
                "Accept-Encoding": "identity",
            },
        )
        web = data.get("web")
        raw = web.get("results") if isinstance(web, Mapping) else None
        if not isinstance(raw, list):
            return []
        return [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("description") or ""),
            )
            for item in raw
            if isinstance(item, Mapping)
        ]


class DuckDuckGoSearchProvider(SearchProvider):
    """DuckDuckGo 免密钥 HTML 端点（最后兜底；非官方接口，可能限流）。

    它存在的意义是「没有 API Key 也能做一次真实检索」——用于本机 smoke，而不是
    生产主力。返回的链接是 ``//duckduckgo.com/l/?uddg=<encoded>`` 形式的跳转，
    这里解出真实 URL，再交给调用方做 SSRF 校验。
    """

    name = "duckduckgo"
    endpoint = "https://html.duckduckgo.com/html/"
    _LINK_RE = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.S,
    )
    _SNIPPET_RE = re.compile(
        r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S)

    async def search(
        self, query: str, *, limit: int, timeout: float
    ) -> list[SearchResult]:
        html = await asyncio.to_thread(
            _request_text, self.endpoint, timeout=timeout, form={"q": query})
        links = self._LINK_RE.findall(html)
        snippets = [m.group(1) for m in self._SNIPPET_RE.finditer(html)]
        results: list[SearchResult] = []
        for index, (href, title_html) in enumerate(links[:limit]):
            url = _unwrap_ddg(href)
            if not url:
                continue
            snippet = snippets[index] if index < len(snippets) else ""
            results.append(SearchResult(
                title=_clean_text(title_html), url=url,
                snippet=_clean_text(snippet)))
        return results


def _unwrap_ddg(href: str) -> str:
    """把 DDG 的跳转链接还原成目标 URL；无法还原时原样返回（后续会校验）。"""
    text = href.strip()
    if text.startswith("//"):
        text = "https:" + text
    parts = urlsplit(text)
    if "duckduckgo.com" in parts.netloc and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg")
        if target:
            return target[0]
    return text


class MockSearchProvider(SearchProvider):
    """离线夹具：按查询返回固定结果，供单测与评测端到端用（不碰网络）。"""

    name = "mock"

    def __init__(self, results: Sequence[SearchResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[str] = []

    async def search(
        self, query: str, *, limit: int, timeout: float
    ) -> list[SearchResult]:
        self.calls.append(query)
        return self.results[:limit]


def build_search_provider(name: str) -> SearchProvider:
    """按名字造提供方；空串表示关闭，未知名字报错（不静默降级成 mock）。"""
    normalized = (name or "").strip().lower()
    if normalized == "tavily":
        return TavilySearchProvider()
    if normalized == "brave":
        return BraveSearchProvider()
    if normalized == "duckduckgo":
        return DuckDuckGoSearchProvider()
    if normalized == "mock":
        return MockSearchProvider()
    raise ValueError(
        f"未知 search_provider：{name!r}（可选 tavily / brave / duckduckgo / mock）")


def _iter_valid(results: Iterable[SearchResult]) -> list[SearchResult]:
    """去重（按 URL）+ 丢掉不合法的 URL（非 http/https、内网、带凭据）。"""
    seen: set[str] = set()
    kept: list[SearchResult] = []
    for item in results:
        cleaned = item.clean()
        if not cleaned.url:
            continue
        if validate_fetch_url(cleaned.url) is not None:
            continue
        key = cleaned.url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(cleaned)
    return kept


class WebSearchTool(Tool):
    """检索互联网，返回多条结果（标题 + URL + 摘要）；读取正文仍用 ``web_fetch``。"""

    name: str = "web_search"
    description: str = (
        "检索互联网并返回多条结果（标题、URL、摘要），用于调研、找资料、确认事实。"
        "拿到链接后可用 web_fetch 读取全文。默认返回 5 条，最多 20 条。"
    )
    input_schema: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "query": {
                "type": "string", "minLength": 1, "maxLength": 500,
                "description": "检索词（自然语言或关键词）",
            },
            "max_results": {
                "type": "integer", "minimum": 1, "maximum": MAX_RESULTS,
                "description": "最多返回多少条结果，默认 5",
            },
        },
        "required": ["query"],
    })
    #: 提供方实例（构造期注入，对模型不可见）。
    provider: Any = None
    max_results: int = 5
    timeout_secs: float = 15.0
    max_chars: int = DEFAULT_MAX_CHARS

    async def run(self, query: str = "", max_results: int | None = None,
                  **kwargs: Any) -> str:
        if not isinstance(query, str) or not query.strip():
            return "错误：query 必须是非空字符串"
        if len(query) > 500:
            return "错误：query 不能超过 500 字符"
        limit = self.max_results
        if max_results is not None:
            if isinstance(max_results, bool) or not isinstance(max_results, int):
                return "错误：max_results 必须是整数"
            if not 1 <= max_results <= MAX_RESULTS:
                return f"错误：max_results 必须落在 1~{MAX_RESULTS} 之间"
            limit = max_results
        provider = self.provider
        if provider is None:
            return "错误：web_search 未配置搜索提供方（search_provider）"
        try:
            raw = await provider.search(
                query.strip(), limit=limit, timeout=self.timeout_secs)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            return f"错误：搜索失败：{exc}"
        except Exception as exc:  # noqa: BLE001 —— 工具不把异常冒泡给 Agent
            return f"错误：搜索失败：{type(exc).__name__}: {exc}"
        if not isinstance(raw, list):
            return "错误：搜索提供方没有返回结果列表"
        results = _iter_valid(item for item in raw if isinstance(item, SearchResult))
        if not results:
            return f"（没有检索到与 {query!r} 相关的结果）"
        lines = [f"搜索结果（{getattr(provider, 'name', '')}，共 {len(results)} 条）："]
        for index, item in enumerate(results, start=1):
            lines.append(f"{index}. {item.title or '(无标题)'}")
            lines.append(f"   URL: {item.url}")
            if item.snippet:
                lines.append(f"   摘要: {item.snippet}")
        text = "\n".join(lines)
        if len(text) > self.max_chars:
            return text[: self.max_chars] + f"\n…（已截断到 {self.max_chars} 字符）"
        return text
