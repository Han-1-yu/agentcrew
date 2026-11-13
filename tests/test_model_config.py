import pytest

from agentcrew.models.config import (
    PROVIDER_PRESETS,
    DuplicateProviderError,
    UnknownProviderError,
    build_directory,
    parse_alias,
)


def test_all_providers_present():
    assert set(PROVIDER_PRESETS) == {
        "openai", "anthropic", "deepseek", "doubao", "qwen", "ernie",
        "ollama", "mock", "cli",
        # A2 新增
        "openrouter", "siliconflow", "moonshot", "zhipu", "groq", "together",
        # V5 P1 新增：聚合/中转
        "fireworks", "deepinfra", "replicate", "huggingface",
        # V5 P1 新增：国内
        "baichuan", "minimax", "stepfun", "lingyi", "xinghuo",
        # V5 P1 新增：国际
        "gemini", "mistral", "cohere",
        # R60 新增：外部 coding CLI 真 agentic 执行体
        "claude-code", "codex",
    }


def test_openai_supports_json_schema():
    assert PROVIDER_PRESETS["openai"].capabilities.supports_json_schema


def test_anthropic_uses_native_implementation():
    assert PROVIDER_PRESETS["anthropic"].implementation == "anthropic"
    assert not PROVIDER_PRESETS["anthropic"].capabilities.supports_json_schema


def test_anthropic_models_url_explicit():
    """anthropic base_url 不带 /v1，显式 models_url 指向 /v1/models，避免先试
    /models 收到非 JSON 后因 parse 错误不再回退。"""
    assert PROVIDER_PRESETS["anthropic"].models_url == \
        "https://api.anthropic.com/v1/models"


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


# ── A1：自定义 Provider 注册表 ──────────────────────────────────────────

_CUSTOM = {
    "myrelay": {
        "implementation": "openai_compat",
        "base_url": "http://localhost:9/v1",
        "api_key_env": "MYRELAY_KEY",
        "capabilities": {"supports_json_object": True},
        "suggested_models": ["relay-model-a", "relay-model-b"],
    }
}


def test_custom_provider_resolves():
    b = parse_alias("myrelay:some-model", custom=_CUSTOM)
    assert b.provider == "myrelay"
    assert b.model == "some-model"
    assert b.base_url == "http://localhost:9/v1"
    assert b.api_key_env == "MYRELAY_KEY"


def test_custom_provider_unknown_raises():
    with pytest.raises(UnknownProviderError):
        parse_alias("nosuch:x", custom=_CUSTOM)


def test_custom_provider_duplicate_builtin_rejected():
    bad = {"openai": {
        "implementation": "openai_compat", "base_url": "http://x",
        "api_key_env": "X", "capabilities": {}}}
    with pytest.raises(DuplicateProviderError):
        build_directory(custom=bad)


def test_build_directory_merges_builtin_and_custom():
    d = build_directory(custom=_CUSTOM)
    assert "openai" in d
    assert "myrelay" in d
    assert d["myrelay"].suggested_models == ["relay-model-a", "relay-model-b"]


def test_custom_provider_overrides_apply():
    """overrides 仍只作用于内置 provider（config.py 校验已拦未知 provider）。"""
    d = build_directory(
        custom=_CUSTOM,
        overrides={"openai": {"base_url": "http://proxy/v1"}},
    )
    assert d["openai"].base_url == "http://proxy/v1"
    # custom provider 不被 overrides 影响
    assert d["myrelay"].base_url == "http://localhost:9/v1"


def test_provider_preset_has_suggested_models_field():
    p = PROVIDER_PRESETS["openai"]
    assert hasattr(p, "suggested_models")
    assert isinstance(p.suggested_models, list)


# ── A2：新增 6 个平台预设 ───────────────────────────────────────────────


@pytest.mark.parametrize("name,base_url,env_name", [
    ("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    ("siliconflow", "https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY"),
    ("moonshot", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    ("zhipu", "https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"),
    ("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    ("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
])
def test_new_presets_basic(name, base_url, env_name):
    p = PROVIDER_PRESETS[name]
    assert p.implementation == "openai_compat"
    assert p.base_url == base_url
    assert p.api_key_env == env_name
    assert len(p.suggested_models) >= 3


def test_new_presets_resolve_as_alias():
    for name in ("openrouter", "siliconflow", "moonshot", "zhipu", "groq", "together"):
        b = parse_alias(f"{name}:some-model")
        assert b.provider == name
        assert b.base_url == PROVIDER_PRESETS[name].base_url


def test_old_9_presets_unchanged():
    """原 9 个预设的 base_url / api_key_env 不变。"""
    assert PROVIDER_PRESETS["openai"].base_url == "https://api.openai.com/v1"
    assert PROVIDER_PRESETS["anthropic"].base_url == "https://api.anthropic.com"
    assert PROVIDER_PRESETS["deepseek"].base_url == "https://api.deepseek.com/v1"
    assert PROVIDER_PRESETS["doubao"].base_url == (
        "https://ark.cn-beijing.volces.com/api/v3")
    assert PROVIDER_PRESETS["qwen"].base_url == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert PROVIDER_PRESETS["ernie"].base_url == "https://qianfan.baidubce.com/v2"
    assert PROVIDER_PRESETS["ollama"].base_url == "http://localhost:11434/v1"
    assert PROVIDER_PRESETS["mock"].implementation == "mock"
    assert PROVIDER_PRESETS["cli"].implementation == "cli"
