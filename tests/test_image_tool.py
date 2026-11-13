"""R61 图像生成工具（离线、确定性）。

网络一律 monkeypatch（``_post_json`` / provider），mock 提供方回放固定 PNG。
覆盖：提供方工厂 / 密钥只走环境变量 / b64 解析 / 工具落盘与路径安全 / 错误收敛 /
ledger 留痕。
"""

from __future__ import annotations

import base64

import pytest

from agentcrew.tools import image as image_mod
from agentcrew.tools.image import (
    ArkSeedreamProvider,
    GenerateImageTool,
    MockImageProvider,
    OpenAIImageProvider,
    _extract_b64,
    build_image_provider,
)

_PNG = base64.b64decode(image_mod._MOCK_PNG_B64)


# ── 提供方工厂 / 密钥 ───────────────────────────────────────────────────────

def test_build_image_provider_names():
    assert isinstance(build_image_provider("ark"), ArkSeedreamProvider)
    assert isinstance(build_image_provider("seedream"), ArkSeedreamProvider)
    assert isinstance(build_image_provider("doubao"), ArkSeedreamProvider)
    assert isinstance(build_image_provider("openai"), OpenAIImageProvider)
    assert isinstance(build_image_provider("mock"), MockImageProvider)


def test_build_image_provider_unknown_raises():
    with pytest.raises(ValueError, match="未知 image_provider"):
        build_image_provider("nope")


def test_api_key_only_from_env(monkeypatch):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("AGENTCREW_IMAGE_API_KEY", raising=False)
    provider = ArkSeedreamProvider()
    assert provider.api_key == ""
    with pytest.raises(ValueError, match="缺少 API Key"):
        provider.require_key()
    monkeypatch.setenv("ARK_API_KEY", "secret-ark")
    assert provider.api_key == "secret-ark"


def test_generic_key_env_wins(monkeypatch):
    monkeypatch.setenv("AGENTCREW_IMAGE_API_KEY", "generic")
    monkeypatch.setenv("ARK_API_KEY", "specific")
    assert ArkSeedreamProvider().api_key == "generic"


async def test_mock_provider_returns_png_and_records():
    provider = MockImageProvider()
    result = await provider.generate("a cat", size="1024x1024", timeout=1)
    assert result.data == _PNG and result.mime == "image/png"
    assert provider.calls == ["a cat"]


# ── b64 解析 ────────────────────────────────────────────────────────────────

def test_extract_b64_valid():
    parsed = {"data": [{"b64_json": base64.b64encode(_PNG).decode()}]}
    result = _extract_b64(parsed)
    assert result.data == _PNG


def test_extract_b64_missing_data_raises():
    with pytest.raises(ValueError, match="data"):
        _extract_b64({})


def test_extract_b64_missing_b64_raises():
    with pytest.raises(ValueError, match="b64_json"):
        _extract_b64({"data": [{"url": "http://x"}]})


def test_extract_b64_invalid_b64_raises():
    with pytest.raises(ValueError, match="解码失败"):
        _extract_b64({"data": [{"b64_json": "!!!not-base64!!!"}]})


# ── 真实提供方的请求装配（不碰网络）─────────────────────────────────────────

async def test_ark_generate_posts_b64_format(monkeypatch):
    captured = {}

    def fake_post(url, *, payload, key, timeout):
        captured.update(url=url, payload=payload, key=key)
        return {"data": [{"b64_json": base64.b64encode(_PNG).decode()}]}

    monkeypatch.setattr(image_mod, "_post_json", fake_post)
    monkeypatch.setenv("ARK_API_KEY", "k")
    result = await ArkSeedreamProvider().generate("cat", size="512x512", timeout=5)
    assert result.data == _PNG
    assert captured["payload"]["response_format"] == "b64_json"
    assert captured["payload"]["size"] == "512x512"
    assert captured["key"] == "k"
    assert captured["url"].startswith("https://ark.")


async def test_openai_generate_uses_openai_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, *, payload, key, timeout):
        captured["url"] = url
        return {"data": [{"b64_json": base64.b64encode(_PNG).decode()}]}

    monkeypatch.setattr(image_mod, "_post_json", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    await OpenAIImageProvider().generate("cat", size="1024x1024", timeout=5)
    assert captured["url"].startswith("https://api.openai.com/")


# ── 工具：落盘 / 路径安全 / 错误收敛 / 留痕 ─────────────────────────────────

class _Ledger:
    def __init__(self):
        self.events = []

    def emit(self, event, **data):
        self.events.append((event, data))


def _tool(tmp_path, provider=None, ledger=None):
    return GenerateImageTool(
        provider=provider if provider is not None else MockImageProvider(),
        workdir=tmp_path, ledger=ledger)


async def test_run_writes_bytes_and_returns_path(tmp_path):
    ledger = _Ledger()
    tool = _tool(tmp_path, ledger=ledger)
    out = await tool.run(prompt="a cat", path="assets/cover.png")
    assert "assets/cover.png" in out
    assert (tmp_path / "assets" / "cover.png").read_bytes() == _PNG
    assert ledger.events and ledger.events[0][0] == "image_generated"
    assert ledger.events[0][1]["size_bytes"] == len(_PNG)


async def test_run_rejects_unsafe_path(tmp_path):
    tool = _tool(tmp_path)
    out = await tool.run(prompt="x", path="../escape.png")
    assert out.startswith("错误")
    assert not (tmp_path.parent / "escape.png").exists()


async def test_run_rejects_empty_prompt_and_path(tmp_path):
    tool = _tool(tmp_path)
    assert (await tool.run(prompt="", path="a.png")).startswith("错误")
    assert (await tool.run(prompt="x", path="")).startswith("错误")


async def test_run_without_provider_errors(tmp_path):
    tool = GenerateImageTool(provider=None, workdir=tmp_path)
    assert (await tool.run(prompt="x", path="a.png")).startswith("错误")


async def test_run_without_workdir_errors():
    tool = GenerateImageTool(provider=MockImageProvider(), workdir=None)
    assert (await tool.run(prompt="x", path="a.png")).startswith("错误")


async def test_run_provider_failure_returns_error_text(tmp_path):
    class Boom(MockImageProvider):
        async def generate(self, prompt, *, size, timeout):
            raise ValueError("upstream 500")

    tool = _tool(tmp_path, provider=Boom())
    out = await tool.run(prompt="x", path="a.png")
    assert out.startswith("错误") and "upstream 500" in out
    assert not (tmp_path / "a.png").exists()
