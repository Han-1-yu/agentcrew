"""C1：联网搜索（提供方抽象 / 结果清洗 / SSRF 过滤 / 默认关闭）。

不做真实网络请求：真实通道的 smoke 由运维用 ``AGENTCREW_SEARCH_PROVIDER=duckduckgo``
手动跑一次（见 ``docs/OPERATIONS.md``）。这里钉的是**不管接到哪个提供方都必须
成立**的约定：

- 结果按 URL 去重，且**不合法 URL 一律丢弃**（``file://``、内网 IP、带凭据）；
- 输出长度有上限，超出截断并显式说明；
- 缺密钥时说清「设哪个环境变量」，且**绝不把密钥值写进错误文本**；
- ``search_provider`` 为空时工具不注册（默认关闭）；未知名字报错，不静默降级。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.runtime.ledger import Ledger
from agentcrew.tools import ToolRegistry
from agentcrew.tools.search import (
    DuckDuckGoSearchProvider,
    MockSearchProvider,
    SearchResult,
    TavilySearchProvider,
    WebSearchTool,
    build_search_provider,
)


def _tool(results: list[SearchResult], **kwargs) -> WebSearchTool:
    return WebSearchTool(provider=MockSearchProvider(results), **kwargs)


async def test_search_formats_results():
    tool = _tool([
        SearchResult("标题一", "https://example.com/a", "摘要一"),
        SearchResult("标题二", "https://example.com/b", "摘要二"),
    ])
    text = await tool.run(query="agentcrew")
    assert "搜索结果" in text and "共 2 条" in text
    assert "1. 标题一" in text and "URL: https://example.com/a" in text
    assert "摘要: 摘要二" in text


async def test_search_dedups_by_url():
    tool = _tool([
        SearchResult("a", "https://example.com/x", ""),
        SearchResult("b", "https://example.com/x/", ""),
    ])
    text = await tool.run(query="q")
    assert "共 1 条" in text


async def test_search_drops_private_and_non_http_urls():
    """提供方被投毒返回内网地址时，模型也不该看到它。"""
    tool = _tool([
        SearchResult("元数据", "http://169.254.169.254/latest/meta-data", ""),
        SearchResult("本机", "http://localhost:8080/admin", ""),
        SearchResult("文件", "file:///etc/passwd", ""),
        SearchResult("正常", "https://example.com/ok", ""),
    ])
    text = await tool.run(query="q")
    assert "example.com/ok" in text
    assert "169.254" not in text and "localhost" not in text and "file://" not in text


async def test_search_reports_empty():
    text = await _tool([]).run(query="不存在的东西")
    assert "没有检索到" in text


async def test_search_truncates_long_output():
    tool = _tool(
        [SearchResult(f"t{i}", f"https://example.com/{i}", "x" * 200)
         for i in range(10)],
        max_chars=200,
    )
    text = await tool.run(query="q")
    assert "已截断到 200 字符" in text


async def test_search_validates_arguments():
    tool = _tool([])
    assert "非空字符串" in await tool.run(query="")
    assert "max_results 必须是整数" in await tool.run(query="q", max_results="3")
    assert "1~20" in await tool.run(query="q", max_results=99)


async def test_search_provider_error_becomes_text():
    class _Boom:
        name = "boom"

        async def search(self, query, *, limit, timeout):  # noqa: ANN001
            raise RuntimeError("upstream 502")

    tool = WebSearchTool(provider=_Boom())
    text = await tool.run(query="q")
    assert text.startswith("错误：搜索失败")
    assert "upstream 502" in text


async def test_search_without_provider_is_explicit():
    text = await WebSearchTool().run(query="q")
    assert "未配置搜索提供方" in text


# ── 密钥：只从环境变量读 ──────────────────────────────────────────────

def test_tavily_requires_key_and_names_the_env(monkeypatch):
    for name in ("TAVILY_API_KEY", "AGENTCREW_SEARCH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    provider = TavilySearchProvider()
    with pytest.raises(ValueError) as excinfo:
        provider.require_key()
    assert "TAVILY_API_KEY" in str(excinfo.value)


def test_generic_key_env_wins(monkeypatch):
    monkeypatch.setenv("AGENTCREW_SEARCH_API_KEY", "generic-key")
    monkeypatch.setenv("TAVILY_API_KEY", "specific-key")
    assert TavilySearchProvider().api_key == "generic-key"


def test_provider_specific_key_used_when_generic_absent(monkeypatch):
    monkeypatch.delenv("AGENTCREW_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "specific-key")
    assert TavilySearchProvider().api_key == "specific-key"


# ── DuckDuckGo HTML 解析（离线夹具）────────────────────────────────────

def test_ddg_unwraps_redirect_links():
    from agentcrew.tools.search import _unwrap_ddg

    wrapped = ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage"
               "&rut=abc")
    assert _unwrap_ddg(wrapped) == "https://example.com/page"


async def test_ddg_parses_fixture_html(monkeypatch):
    from agentcrew.tools import search as search_module

    html = """
    <div class="result">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">标题 A</a>
      <a class="result__snippet">摘要 <b>A</b></a>
    </div>
    <div class="result">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb">标题 B</a>
      <a class="result__snippet">摘要 B</a>
    </div>
    """
    monkeypatch.setattr(
        search_module, "_request_text",
        lambda *args, **kwargs: html)
    results = await DuckDuckGoSearchProvider().search(
        "q", limit=2, timeout=5)
    assert [item.url for item in results] == [
        "https://example.com/a", "https://example.com/b"]
    assert results[0].title == "标题 A"
    assert results[0].snippet == "摘要 A"


# ── 工厂与装配 ────────────────────────────────────────────────────────

def test_build_provider_names():
    assert isinstance(build_search_provider("tavily"), TavilySearchProvider)
    assert isinstance(build_search_provider("duckduckgo"),
                      DuckDuckGoSearchProvider)
    assert isinstance(build_search_provider("mock"), MockSearchProvider)
    with pytest.raises(ValueError):
        build_search_provider("google")


def test_search_default_off(tmp_path, monkeypatch):
    """默认配置下 ``web_search`` 不该出现在注册表里。"""
    monkeypatch.setenv("AGENTCREW_TOOLS_ENABLED", "true")
    settings = Settings.load()
    assert settings.search_provider == ""
    registry = Crew(settings)._build_tool_registry(
        SimpleNamespace(run_dir=tmp_path / "run"),
        Ledger(tmp_path / "l.jsonl", run_id="R"),
    )
    assert isinstance(registry, ToolRegistry)
    assert "web_search" not in [tool.name for tool in registry.list_tools()]


def test_search_registered_when_provider_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_TOOLS_ENABLED", "true")
    monkeypatch.setenv("AGENTCREW_SEARCH_PROVIDER", "mock")
    registry = Crew(Settings.load())._build_tool_registry(
        SimpleNamespace(run_dir=tmp_path / "run"),
        Ledger(tmp_path / "l.jsonl", run_id="R"),
    )
    tool = registry.get("web_search")
    assert tool is not None
    assert tool.name == "web_search"
    assert isinstance(tool.provider, MockSearchProvider)


def test_search_options_are_validated():
    from agentcrew.api.run_options import validate_run_options

    assert validate_run_options({"search_provider": "tavily"}) is None
    assert validate_run_options({"search_provider": ""}) is None
    assert validate_run_options({"search_provider": "google"}) is not None
    assert validate_run_options({"search_max_results": 20}) is None
    assert validate_run_options({"search_max_results": 0}) is not None
    # C2：sandbox_backend 刻意不开放按 run 覆盖（防隔离降级）
    assert validate_run_options({"sandbox_backend": "local"}) is not None
