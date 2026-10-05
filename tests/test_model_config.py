import pytest

from agentcrew.models.config import (
    PROVIDER_PRESETS,
    UnknownProviderError,
    parse_alias,
)


def test_all_eight_providers_present():
    assert set(PROVIDER_PRESETS) == {
        "openai", "anthropic", "deepseek", "doubao", "qwen", "ernie", "ollama", "mock"}


def test_openai_supports_json_schema():
    assert PROVIDER_PRESETS["openai"].capabilities.supports_json_schema


def test_anthropic_uses_native_implementation():
    assert PROVIDER_PRESETS["anthropic"].implementation == "anthropic"
    assert not PROVIDER_PRESETS["anthropic"].capabilities.supports_json_schema


def test_doubao_has_object_but_not_schema():
    c = PROVIDER_PRESETS["doubao"].capabilities
    assert c.supports_json_object and not c.supports_json_schema


def test_ernie_supports_neither():
    c = PROVIDER_PRESETS["ernie"].capabilities
    assert not c.supports_json_schema and not c.supports_json_object


def test_ollama_has_local_base_url():
    assert "11434" in (PROVIDER_PRESETS["ollama"].base_url or "")


def test_parse_alias_simple():
    b = parse_alias("openai:gpt-4o-mini")
    assert b.provider == "openai" and b.model == "gpt-4o-mini"
    assert b.capabilities.supports_json_schema


def test_parse_alias_splits_on_first_colon_only():
    b = parse_alias("ollama:qwen2.5:14b")
    assert b.provider == "ollama" and b.model == "qwen2.5:14b"


def test_parse_alias_without_colon_raises():
    with pytest.raises(ValueError):
        parse_alias("justamodel")


def test_parse_alias_unknown_provider_raises():
    with pytest.raises(UnknownProviderError):
        parse_alias("wizard:merlin")


def test_capability_override_applied():
    b = parse_alias("doubao:x", overrides={"doubao": {
        "capabilities": {"supports_json_schema": True}}})
    assert b.capabilities.supports_json_schema
    assert b.capabilities.supports_json_object      # 未覆盖的字段保持不变


def test_base_url_override_applied():
    b = parse_alias("openai:gpt-4o", overrides={"openai": {
        "base_url": "https://proxy.internal/v1"}})
    assert b.base_url == "https://proxy.internal/v1"
