"""V5 P4：``providers`` / ``models`` CLI 增强测试。

覆盖：
- ``providers --category <cat>`` 按分类过滤；
- ``providers --search <kw>`` 按 name / search_aliases 子串搜索；
- ``--category`` 与 ``--search`` 组合（AND）；
- ``--json`` 输出新增 category/family/search_aliases/endpoint_candidates/api_key_url；
- 文本输出含 category 与 api_key_url；
- ``models`` 命令的端点候选详情（mock fetch_models）；
- 非法 category 被 argparse 拒绝；
- 过滤后无结果时的空态提示。
"""

from __future__ import annotations

import json

import pytest

import main as cli

# ── 辅助：跑 providers 并解析 ──────────────────────────────────────────────


def _run_providers(*args: str, tmp_path, monkeypatch):
    """跑 ``python main.py providers ...`` 并返回 (退出码, stdout, stderr)。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    code = cli.main(["providers", *args])
    return code


def _run_providers_json(*args: str, tmp_path, monkeypatch):
    """跑 providers --json，返回 (退出码, 解析后的 JSON)。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["providers", "--json", *args])
    data = json.loads(buf.getvalue())
    return code, data


# ── --category 过滤 ──────────────────────────────────────────────────────


def test_category_aggregator_only_returns_aggregators(tmp_path, monkeypatch):
    """--category aggregator 只返回 category==aggregator 的 provider。"""
    code, data = _run_providers_json("--category", "aggregator",
                                     tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == 0
    names = {p["name"] for p in data["providers"]}
    # openrouter / groq / together / fireworks / deepinfra / replicate / huggingface
    assert "openrouter" in names
    assert "groq" in names
    # national 的 provider 不应出现
    assert "deepseek" not in names
    assert "qwen" not in names
    # 每一条都标了 aggregator
    for p in data["providers"]:
        assert p["category"] == "aggregator"


def test_category_national_returns_domestic(tmp_path, monkeypatch):
    """--category national 返回国内平台。"""
    code, data = _run_providers_json("--category", "national",
                                     tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == 0
    names = {p["name"] for p in data["providers"]}
    assert "deepseek" in names
    assert "qwen" in names
    assert "doubao" in names
    for p in data["providers"]:
        assert p["category"] == "national"


def test_invalid_category_rejected(tmp_path, monkeypatch):
    """非法 category 被 argparse 拒绝（退出码 2）。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["providers", "--category", "nosuchcat"])
    assert exc.value.code == 2


# ── --search 搜索 ────────────────────────────────────────────────────────


def test_search_deep_matches_deepseek(tmp_path, monkeypatch):
    """--search deep 应匹配 deepseek（name 子串）。"""
    code, data = _run_providers_json("--search", "deep",
                                     tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == 0
    names = {p["name"] for p in data["providers"]}
    assert "deepseek" in names


def test_search_chinese_alias_matches_deepseek(tmp_path, monkeypatch):
    """--search 深度 通过 search_aliases['深度求索'] 匹配 deepseek。"""
    code, data = _run_providers_json("--search", "深度",
                                     tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == 0
    names = {p["name"] for p in data["providers"]}
    assert "deepseek" in names


def test_search_case_insensitive(tmp_path, monkeypatch):
    """--search OPENAI 大小写不敏感，匹配 openai。"""
    code, data = _run_providers_json("--search", "OPENAI",
                                     tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == 0
    names = {p["name"] for p in data["providers"]}
    assert "openai" in names


# ── 组合过滤 ──────────────────────────────────────────────────────────────


def test_category_and_search_combined(tmp_path, monkeypatch):
    """--category national --search qwen：AND 组合，qwen 是 national。"""
    code, data = _run_providers_json(
        "--category", "national", "--search", "qwen",
        tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == 0
    names = {p["name"] for p in data["providers"]}
    assert "qwen" in names
    # openrouter 是 aggregator，即使名字里没有 qwen，也不应出现
    assert "openrouter" not in names


# ── --json 新字段 ─────────────────────────────────────────────────────────


def test_json_contains_new_metadata_fields(tmp_path, monkeypatch):
    """--json 每条记录含 category/family/search_aliases/endpoint_candidates/api_key_url。"""
    code, data = _run_providers_json(tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == 0
    by_name = {p["name"]: p for p in data["providers"]}
    openai = by_name["openai"]
    assert openai["category"] == "international"
    assert openai["family"] == "openai_compat"
    assert "gpt" in openai["search_aliases"]
    assert openai["api_key_url"] == "https://platform.openai.com/api-keys"
    # endpoint_candidates 可能为 None（未显式配置多端点），但字段必须存在
    assert "endpoint_candidates" in openai


# ── 文本输出 ──────────────────────────────────────────────────────────────


def test_text_output_shows_category(tmp_path, monkeypatch, capsys):
    """默认文本输出里能看到 category 标签。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    code = cli.main(["providers"])
    assert code == 0
    out = capsys.readouterr().out
    # openai 是 international
    assert "openai" in out
    assert "[international]" in out or "international" in out


def test_text_output_shows_api_key_url(tmp_path, monkeypatch, capsys):
    """文本输出里有 api_key_url 行（openai 有）。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    code = cli.main(["providers", "openai"])
    assert code == 0
    out = capsys.readouterr().out
    assert "api_key_url" in out
    assert "platform.openai.com" in out


def test_text_output_empty_state(tmp_path, monkeypatch, capsys):
    """过滤后无结果时显示空态提示，不崩。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    code = cli.main(["providers", "--search", "zzz_no_such_provider_xyz"])
    assert code == 0
    out = capsys.readouterr().out
    assert "没有" in out or "无匹配" in out or "空" in out


# ── models 命令端点候选详情 ──────────────────────────────────────────────


def test_models_json_includes_endpoints_tried(tmp_path, monkeypatch, capsys):
    """models --json 输出含 endpoints_tried / successful_endpoint 字段。"""
    from agentcrew.models import discovery

    async def fake_fetch(binding, *, api_key=""):
        return ["gpt-4o", "gpt-4o-mini"]

    monkeypatch.setattr(discovery, "fetch_models", fake_fetch)
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))

    code = cli.main(["models", "openai", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    data = json.loads(out)
    assert data["provider"] == "openai"
    assert data["models"] == ["gpt-4o", "gpt-4o-mini"]
    assert "endpoints_tried" in data
    assert isinstance(data["endpoints_tried"], list)
    assert len(data["endpoints_tried"]) >= 1
    assert "successful_endpoint" in data
    assert data["successful_endpoint"] is not None


def test_models_verbose_shows_endpoint_details(tmp_path, monkeypatch, capsys):
    """models --verbose 文本输出里能看到端点候选信息。"""
    from agentcrew.models import discovery

    async def fake_fetch(binding, *, api_key=""):
        return ["gpt-4o"]

    monkeypatch.setattr(discovery, "fetch_models", fake_fetch)
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))

    code = cli.main(["models", "openai", "--verbose"])
    assert code == 0
    out = capsys.readouterr().out
    # verbose 模式应提到端点候选
    assert "端点" in out or "endpoint" in out.lower()
    assert "gpt-4o" in out


def test_models_non_verbose_stays_concise(tmp_path, monkeypatch, capsys):
    """非 verbose 模式保持现有简洁输出（不刷屏端点明细）。"""
    from agentcrew.models import discovery

    async def fake_fetch(binding, *, api_key=""):
        return ["gpt-4o", "gpt-4o-mini"]

    monkeypatch.setattr(discovery, "fetch_models", fake_fetch)
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))

    code = cli.main(["models", "openai"])
    assert code == 0
    out = capsys.readouterr().out
    assert "个模型" in out
    assert "gpt-4o" in out
