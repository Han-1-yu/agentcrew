"""V5 P1：ProviderPreset 元数据增强 + 预设库扩充测试。

覆盖：
- 预设总数 ≥ 25；
- 每个预设带 category/family/search_aliases/api_key_url（mock/cli/ollama 例外）；
- 现有 15 个预设的 base_url/api_key_env/implementation 不变；
- ProviderPreset / ModelBinding 新字段默认值；
- parse_alias 把新字段拷进 ModelBinding；
- search_aliases 能按别名反查 provider；
- 按 category 分组统计；
- endpoint_candidates 可读；
- custom_providers 兼容（新字段有默认值）；
- _apply_override 能合并新字段。
"""

import pytest

from agentcrew.models.config import (
    PROVIDER_PRESETS,
    ModelBinding,
    ProviderPreset,
    _apply_override,
    build_directory,
    parse_alias,
)

# ── 1. 数量 ─────────────────────────────────────────────────────────────

def test_preset_count_at_least_25():
    assert len(PROVIDER_PRESETS) >= 25, (
        f"预设数量 {len(PROVIDER_PRESETS)} < 25")


def test_original_15_presets_still_present():
    """原 15 个预设的 key 全部保留。"""
    expected = {
        "openai", "anthropic", "deepseek", "doubao", "qwen", "ernie",
        "ollama", "mock", "cli",
        "openrouter", "siliconflow", "moonshot", "zhipu", "groq", "together",
    }
    assert expected.issubset(PROVIDER_PRESETS.keys())


# ── 2. 新字段默认值 ─────────────────────────────────────────────────────

def test_provider_preset_new_field_defaults():
    """构造一个最小 ProviderPreset，新字段应带默认值。"""
    p = ProviderPreset(
        implementation="openai_compat",
        base_url="https://example.com/v1",
        api_key_env="EXAMPLE_KEY",
        capabilities={},
    )
    assert p.category == "international"
    assert p.family is None
    assert p.search_aliases == []
    assert p.endpoint_candidates is None
    assert p.api_key_url is None


# ── 3. 现有 15 个预设语义不变（base_url/api_key_env/implementation）─────

@pytest.mark.parametrize("name,base_url,env_name,impl", [
    ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "openai_compat"),
    ("anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "anthropic"),
    ("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "openai_compat"),
    ("doubao", "https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY", "openai_compat"),
    ("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "openai_compat"),
    ("ernie", "https://qianfan.baidubce.com/v2", "QIANFAN_API_KEY", "openai_compat"),
    ("ollama", "http://localhost:11434/v1", "", "openai_compat"),
    ("mock", None, "", "mock"),
    ("cli", None, "", "cli"),
    ("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openai_compat"),
    ("siliconflow", "https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY", "openai_compat"),
    ("moonshot", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", "openai_compat"),
    ("zhipu", "https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY", "openai_compat"),
    ("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai_compat"),
    ("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY", "openai_compat"),
])
def test_original_15_presets_unchanged(name, base_url, env_name, impl):
    p = PROVIDER_PRESETS[name]
    assert p.base_url == base_url
    assert p.api_key_env == env_name
    assert p.implementation == impl


# ── 4. 每个预设元数据完整度 ──────────────────────────────────────────────

# 这些预设不需要 api_key_url（本地/离线/CLI）
# claude-code / codex 是 R60 的 agentic CLI 预设：认证由 CLI 自带，平台不持有密钥。
_NO_API_KEY_URL = {"ollama", "mock", "cli", "claude-code", "codex"}
# 这些预设不需要 search_aliases（无对外品牌名）
_NO_SEARCH_ALIAS = {"mock", "cli"}


def test_all_presets_have_category():
    for name, p in PROVIDER_PRESETS.items():
        assert p.category in {
            "aggregator", "national", "international", "local", "specialized",
        }, f"{name} category 非法：{p.category!r}"


def test_all_presets_have_family():
    for name, p in PROVIDER_PRESETS.items():
        assert isinstance(p.family, str) and p.family, (
            f"{name} family 为空")


def test_presets_have_api_key_url_when_expected():
    for name, p in PROVIDER_PRESETS.items():
        if name in _NO_API_KEY_URL:
            continue
        assert isinstance(p.api_key_url, str) and p.api_key_url.startswith("http"), (
            f"{name} api_key_url 缺失")


def test_presets_have_search_aliases_when_expected():
    for name, p in PROVIDER_PRESETS.items():
        if name in _NO_SEARCH_ALIAS:
            continue
        assert isinstance(p.search_aliases, list) and len(p.search_aliases) >= 1, (
            f"{name} search_aliases 为空")


# ── 5. 现有 15 个预设的 category/family/search_aliases/api_key_url 回填 ───

def test_original_15_metadata_backfilled():
    expected = {
        "openai": ("international", "openai_compat",
                   ["gpt", "open ai"], "https://platform.openai.com/api-keys"),
        "anthropic": ("international", "anthropic",
                      ["claude"], "https://console.anthropic.com/settings/keys"),
        "deepseek": ("national", "openai_compat",
                     ["ds", "深度求索"], "https://platform.deepseek.com/api_keys"),
        "doubao": ("national", "openai_compat",
                   ["豆包", "volcengine", "火山"],
                   "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey"),
        "qwen": ("national", "openai_compat",
                 ["通义", "千问", "aliyun", "阿里云"],
                 "https://dashscope.console.aliyun.com/apiKey"),
        "ernie": ("national", "openai_compat",
                  ["文心", "百度", "baidu"],
                  "https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application"),
        "ollama": ("local", "openai_compat", ["本地", "local"], None),
        "mock": ("specialized", "mock", [], None),
        "cli": ("specialized", "cli", [], None),
        "openrouter": ("aggregator", "openai_compat",
                       ["or", "路由"], "https://openrouter.ai/keys"),
        "siliconflow": ("national", "openai_compat",
                        ["硅基流动", "sf"], "https://cloud.siliconflow.cn/account/ak"),
        "moonshot": ("national", "openai_compat",
                     ["月之暗面", "kimi"],
                     "https://platform.moonshot.cn/console/api-keys"),
        "zhipu": ("national", "openai_compat",
                  ["智谱", "glm", "chatglm"],
                  "https://open.bigmodel.cn/usercenter/apikeys"),
        "groq": ("aggregator", "openai_compat",
                 ["groqcloud"], "https://console.groq.com/keys"),
        "together": ("aggregator", "openai_compat",
                     ["together ai"], "https://api.together.xyz/settings/api-keys"),
    }
    for name, (cat, fam, aliases, key_url) in expected.items():
        p = PROVIDER_PRESETS[name]
        assert p.category == cat, f"{name} category={p.category} != {cat}"
        assert p.family == fam, f"{name} family={p.family} != {fam}"
        assert p.search_aliases == aliases, (
            f"{name} aliases={p.search_aliases} != {aliases}")
        assert p.api_key_url == key_url, (
            f"{name} api_key_url={p.api_key_url} != {key_url}")


# ── 6. 新增 13 个预设存在且基本字段正确 ────────────────────────────────────

NEW_PRESETS = {
    # 聚合/中转
    "fireworks": ("aggregator", "https://api.fireworks.ai/inference/v1",
                  "FIREWORKS_API_KEY"),
    "deepinfra": ("aggregator", "https://api.deepinfra.com/v1/openai",
                  "DEEPINFRA_API_KEY"),
    "replicate": ("aggregator", "https://openai.replicate.com/v1",
                  "REPLICATE_API_TOKEN"),
    "huggingface": ("aggregator", "https://router.huggingface.co/v1",
                    "HF_API_TOKEN"),
    # 国内
    "baichuan": ("national", "https://api.baichuan-ai.com/v1",
                 "BAICHUAN_API_KEY"),
    "minimax": ("national", "https://api.minimax.chat/v1",
                "MINIMAX_API_KEY"),
    "stepfun": ("national", "https://api.stepfun.com/v1",
                "STEPFUN_API_KEY"),
    "lingyi": ("national", "https://api.lingyiwanwu.com/v1",
               "YI_API_KEY"),
    "xinghuo": ("national", "https://spark-api-open.xf-yun.com/v1",
                "XINGHUO_API_KEY"),
    # 国际
    "gemini": ("international",
               "https://generativelanguage.googleapis.com/v1beta/openai",
               "GEMINI_API_KEY"),
    "mistral": ("international", "https://api.mistral.ai/v1",
                "MISTRAL_API_KEY"),
    "cohere": ("international", "https://api.cohere.com/v2",
               "CO_API_KEY"),
}


@pytest.mark.parametrize(
    "name,category,base_url,env_name",
    [(name, *vals) for name, vals in NEW_PRESETS.items()],
)
def test_new_presets_exist_with_basic_fields(name, category, base_url, env_name):
    assert name in PROVIDER_PRESETS, f"缺少预设 {name}"
    p = PROVIDER_PRESETS[name]
    assert p.category == category
    assert p.base_url == base_url
    assert p.api_key_env == env_name
    assert p.implementation == "openai_compat"
    # 新预设必须带 suggested_models（2-4 个）
    assert 2 <= len(p.suggested_models) <= 4, (
        f"{name} suggested_models 数量异常：{p.suggested_models}")
    # 新预设必须带 family / search_aliases / api_key_url
    assert p.family == "openai_compat"
    assert len(p.search_aliases) >= 1
    assert p.api_key_url and p.api_key_url.startswith("http")


# ── 7. ModelBinding 携带新字段 ──────────────────────────────────────────

def test_model_binding_carries_new_fields():
    b = parse_alias("deepseek:deepseek-chat")
    # 显式断言 ModelBinding 上有这些属性
    assert hasattr(b, "category")
    assert hasattr(b, "family")
    assert hasattr(b, "search_aliases")
    assert hasattr(b, "endpoint_candidates")
    assert hasattr(b, "api_key_url")
    # 值与 preset 一致
    p = PROVIDER_PRESETS["deepseek"]
    assert b.category == p.category
    assert b.family == p.family
    assert b.search_aliases == p.search_aliases
    assert b.endpoint_candidates == p.endpoint_candidates
    assert b.api_key_url == p.api_key_url


def test_model_binding_new_fields_default_for_custom():
    """自定义 provider 不传新字段时，ModelBinding 用默认值。"""
    custom = {
        "myrelay": {
            "implementation": "openai_compat",
            "base_url": "http://localhost:9/v1",
            "api_key_env": "MYRELAY_KEY",
            "capabilities": {"supports_json_object": True},
        }
    }
    b = parse_alias("myrelay:m1", custom=custom)
    assert b.category == "international"
    assert b.family is None
    assert b.search_aliases == []
    assert b.endpoint_candidates is None
    assert b.api_key_url is None


# ── 8. search_aliases 搜索功能 ──────────────────────────────────────────

def test_search_aliases_resolve_to_provider():
    """按别名反查 provider 名。"""
    alias_map = {}
    for name, p in PROVIDER_PRESETS.items():
        for a in p.search_aliases:
            alias_map[a.lower()] = name
    # 几个关键别名必须能找到
    assert alias_map["ds"] == "deepseek"
    assert alias_map["深度求索"] == "deepseek"
    assert alias_map["kimi"] == "moonshot"
    assert alias_map["claude"] == "anthropic"
    assert alias_map["gpt"] == "openai"
    assert alias_map["通义"] == "qwen"
    assert alias_map["智谱"] == "zhipu"


# ── 9. category 分组统计 ────────────────────────────────────────────────

def test_category_grouping():
    groups: dict[str, int] = {}
    for p in PROVIDER_PRESETS.values():
        groups[p.category] = groups.get(p.category, 0) + 1
    # 每个分类至少 1 个
    for cat in ("aggregator", "national", "international", "local", "specialized"):
        assert groups.get(cat, 0) >= 1, f"分类 {cat} 为空"
    # 国内平台至少 6 个（deepseek/doubao/qwen/ernie/siliconflow/moonshot/zhipu + baichuan/minimax/stepfun/lingyi/xinghuo）
    assert groups["national"] >= 10
    # 聚合至少 5 个（openrouter/groq/together/fireworks/deepinfra/replicate/huggingface）
    assert groups["aggregator"] >= 5


# ── 10. endpoint_candidates 字段 ─────────────────────────────────────────

def test_endpoint_candidates_field_readable():
    """所有预设都能读 endpoint_candidates（值可能为 None 或 list）。"""
    for name, p in PROVIDER_PRESETS.items():
        v = p.endpoint_candidates
        assert v is None or isinstance(v, list), (
            f"{name} endpoint_candidates 类型异常：{type(v)}")
        if isinstance(v, list):
            for url in v:
                assert url.startswith("http"), (
                    f"{name} endpoint_candidate 非 URL：{url}")


# ── 11. custom_providers 兼容（新字段有默认值）──────────────────────────

def test_custom_provider_backward_compat():
    """custom provider 不传新字段，构造不报错，新字段走默认值。"""
    custom = {
        "myrelay": {
            "implementation": "openai_compat",
            "base_url": "http://localhost:9/v1",
            "api_key_env": "MYRELAY_KEY",
            "capabilities": {"supports_json_object": True},
            "suggested_models": ["a", "b"],
        }
    }
    d = build_directory(custom=custom)
    assert "myrelay" in d
    p = d["myrelay"]
    assert p.category == "international"  # 默认值
    assert p.family is None
    assert p.search_aliases == []
    assert p.endpoint_candidates is None
    assert p.api_key_url is None


def test_custom_provider_can_set_new_fields():
    """custom provider 也能显式设置新字段。"""
    custom = {
        "myrelay": {
            "implementation": "openai_compat",
            "base_url": "http://localhost:9/v1",
            "api_key_env": "MYRELAY_KEY",
            "capabilities": {"supports_json_object": True},
            "category": "aggregator",
            "family": "openai_compat",
            "search_aliases": ["我的中转"],
            "api_key_url": "http://localhost:9/keys",
        }
    }
    d = build_directory(custom=custom)
    p = d["myrelay"]
    assert p.category == "aggregator"
    assert p.family == "openai_compat"
    assert p.search_aliases == ["我的中转"]
    assert p.api_key_url == "http://localhost:9/keys"


# ── 12. _apply_override 合并新字段 ───────────────────────────────────────

def test_apply_override_merges_new_fields():
    p = PROVIDER_PRESETS["openai"]
    patched = _apply_override(p, {
        "category": "aggregator",
        "search_aliases": ["gpt", "chatgpt"],
        "api_key_url": "https://example.com/new-keys",
    })
    assert patched.category == "aggregator"
    assert patched.search_aliases == ["gpt", "chatgpt"]
    assert patched.api_key_url == "https://example.com/new-keys"
    # 未覆盖字段保持
    assert patched.base_url == p.base_url
    assert patched.implementation == p.implementation


def test_apply_override_partial_keeps_others():
    p = PROVIDER_PRESETS["deepseek"]
    original_aliases = list(p.search_aliases)
    patched = _apply_override(p, {"family": "openai_compat-v2"})
    assert patched.family == "openai_compat-v2"
    assert patched.search_aliases == original_aliases
    assert patched.category == p.category
    assert patched.api_key_url == p.api_key_url


# ── 13. ModelBinding 是 BaseModel 且新字段类型正确 ───────────────────────

def test_model_binding_is_basemodel():
    b = parse_alias("openai:gpt-4o-mini")
    assert isinstance(b, ModelBinding)


def test_no_duplicate_aliases_within_preset():
    """同一预设内 search_aliases 不应有重复项。"""
    for name, p in PROVIDER_PRESETS.items():
        assert len(p.search_aliases) == len(set(p.search_aliases)), (
            f"{name} search_aliases 有重复：{p.search_aliases}")
