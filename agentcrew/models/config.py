"""Provider 能力位、内置预设与模型别名解析（spec §12.1 / §12.2）。

别名格式 ``"<provider>:<model>"``，只按**第一个**冒号切分，因此模型名可以
自带冒号（如 ``ollama:qwen2.5:14b``）。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: provider 的实现方式（决定 Task 21 的 ModelFactory 如何构造 Provider）。
Implementation = Literal["openai_compat", "anthropic", "mock", "cli"]

#: 预设的业务分类（用于 Studio / CLI 的分组与过滤）。
#: - ``aggregator``：聚合/中转平台（一家 Key 调多家模型）
#: - ``national``：国内主流大模型平台
#: - ``international``：海外主流大模型平台
#: - ``local``：本地/自托管
#: - ``specialized``：特殊用途（mock、cli 等）
ProviderCategory = Literal[
    "aggregator", "national", "international", "local", "specialized",
]


class UnknownProviderError(ValueError):
    """别名引用了不在 :data:`PROVIDER_PRESETS` 里的 provider。"""


class ProviderCapabilities(BaseModel):
    """一个 provider 是否支持 OpenAI 风格的结构化输出机制（spec §12.1）。"""

    model_config = ConfigDict(extra="forbid")

    supports_json_schema: bool = False
    supports_json_object: bool = False
    supports_tool_calls: bool = False
    supports_system_role: bool = True
    max_context_tokens: int = 128_000
    max_output_tokens: int = 8_192


class ProviderPreset(BaseModel):
    """内置 provider 预设（spec §12.1 表格；可被 ``agentcrew.yaml`` 覆盖）。"""

    model_config = ConfigDict(extra="forbid")

    implementation: Implementation
    base_url: str | None
    api_key_env: str
    capabilities: ProviderCapabilities
    #: CLI 引擎的命令模板（仅 implementation="cli" 时使用）。
    #: 支持占位符 {model} {prompt} {max_tokens} {temperature}。
    command: str | None = None
    #: 建议模型名清单（供 Studio / CLI 模型选择器展示，不参与调度）。
    suggested_models: list[str] = Field(default_factory=list)
    #: 模型发现端点覆写（缺省时由 base_url 派生 ``/models``）。
    models_url: str | None = None
    # ── V5 P1：元数据增强字段（全部可选，保持向后兼容）─────────────────
    #: 业务分类（aggregator/national/international/local/specialized）。
    category: ProviderCategory = "international"
    #: 预设家族（如 ``openai_compat``、``anthropic``、``mock``、``cli``），
    #: 用于按协议族批量匹配能力/行为。
    family: str | None = None
    #: 搜索别名（中文名、品牌缩写、旧名），仅用于 Studio/CLI 的搜索框。
    search_aliases: list[str] = Field(default_factory=list)
    #: 多端点候选（用于模型发现/测速时按顺序回退）；``None`` 表示只用 base_url。
    endpoint_candidates: list[str] | None = None
    #: 获取 API Key 的官方页面 URL（本地/离线 provider 为 ``None``）。
    api_key_url: str | None = None


class ModelBinding(BaseModel):
    """``parse_alias`` 的产物：一个别名解析后的完整绑定。"""

    model_config = ConfigDict(extra="forbid")

    alias: str
    provider: str
    model: str
    implementation: Implementation
    base_url: str | None
    api_key_env: str
    capabilities: ProviderCapabilities
    command: str | None = None
    models_url: str | None = None
    # ── V5 P1：与 ProviderPreset 对齐的元数据（从 preset 拷贝）─────────
    category: ProviderCategory = "international"
    family: str | None = None
    search_aliases: list[str] = Field(default_factory=list)
    endpoint_candidates: list[str] | None = None
    api_key_url: str | None = None


#: 内置能力预设，逐行对应 spec §12.1 的能力表（8 行）。
#: ``api_key_env`` 取自 spec §17.2 的环境变量清单；ollama 无需密钥、mock 离线，
#: 二者为空串。``supports_tool_calls`` 全为 False —— V1 不使用工具调用。
PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=True, supports_json_object=True),
        suggested_models=["gpt-5.6", "gpt-5.5", "gpt-5.4", "gpt-4o"],
        category="international",
        family="openai_compat",
        search_aliases=["gpt", "open ai"],
        api_key_url="https://platform.openai.com/api-keys",
    ),
    "anthropic": ProviderPreset(
        implementation="anthropic",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        capabilities=ProviderCapabilities(),  # 结构化输出走围栏提取
        # anthropic 的 base_url 不带 /v1，模型列表固定在 /v1/models；显式指过来，
        # 避免先试 /models（可能回 200 HTML 而非 404）后因 parse 错误不再回退。
        models_url="https://api.anthropic.com/v1/models",
        suggested_models=[
            "claude-sonnet-5-5",
            "claude-opus-5-5",
            "claude-haiku-5-5",
            "claude-fable-5-1",
        ],
        category="international",
        family="anthropic",
        search_aliases=["claude"],
        api_key_url="https://console.anthropic.com/settings/keys",
    ),
    "deepseek": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        # supports_json_schema 实测为 False：2026-10-05 对 deepseek-chat 真发
        # response_format={"type":"json_schema",...} 得到 400
        # "This response_format type is unavailable now"。
        # 降级链会正确地把该 400 判为 PermanentError 并降到第 2 级，
        # 但每一次结构化调用都会白烧一次请求 —— 所以这里必须如实标 False。
        #
        # 另注（跨 provider 的硬约束）：deepseek 的 json_object 模式要求
        # **提示词里必须出现 "json" 这个词**，否则 400
        # "Prompt must contain the word 'json' in some form"。
        # 第 2/3 级的注入指令已满足（见 models/structured.py 的
        # _messages_for_level），并有测试钉住，改动那句措辞前先看那里的注释。
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
        category="national",
        family="openai_compat",
        search_aliases=["ds", "深度求索"],
        api_key_url="https://platform.deepseek.com/api_keys",
    ),
    "doubao": ProviderPreset(
        implementation="openai_compat",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key_env="ARK_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=["doubao-pro-32k", "doubao-lite-32k", "doubao-1.5-pro-32k"],
        category="national",
        family="openai_compat",
        search_aliases=["豆包", "volcengine", "火山"],
        api_key_url="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
    ),
    "qwen": ProviderPreset(
        implementation="openai_compat",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=["qwen3.8-max", "qwen3.7-max", "qwen3.7-plus", "qwen3.7-flash"],
        category="national",
        family="openai_compat",
        search_aliases=["通义", "千问", "aliyun", "阿里云"],
        api_key_url="https://dashscope.console.aliyun.com/apiKey",
    ),
    "ernie": ProviderPreset(
        implementation="openai_compat",
        base_url="https://qianfan.baidubce.com/v2",
        api_key_env="QIANFAN_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=False),
        suggested_models=["ernie-4.0", "ernie-3.5", "ernie-speed"],
        category="national",
        family="openai_compat",
        search_aliases=["文心", "百度", "baidu"],
        api_key_url=(
            "https://console.bce.baidu.com/qianfan/ais/console/"
            "applicationConsole/application"),
    ),
    "ollama": ProviderPreset(
        implementation="openai_compat",
        base_url="http://localhost:11434/v1",
        api_key_env="",  # 本地模型：api_key 可为任意非空串
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=["llama3.3", "qwen3", "deepseek-r1", "mistral-small"],
        category="local",
        family="openai_compat",
        search_aliases=["本地", "local"],
        api_key_url=None,
    ),
    "mock": ProviderPreset(
        implementation="mock",
        base_url=None,
        api_key_env="",  # 离线确定性回放
        capabilities=ProviderCapabilities(
            supports_json_schema=True, supports_json_object=True),
        suggested_models=["mock-model"],
        category="specialized",
        family="mock",
        search_aliases=[],
        api_key_url=None,
    ),
    "cli": ProviderPreset(
        implementation="cli",
        base_url=None,
        api_key_env="",  # 本地 CLI：无需 API Key
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=False),
        # command 必须通过 provider_overrides 显式指定（不同 CLI 工具语法不同）
        command=None,
        suggested_models=["cli-model"],
        category="specialized",
        family="cli",
        search_aliases=[],
        api_key_url=None,
    ),
    # ── A2：新增主流平台预设（全部 OpenAI 兼容）─────────────────────────
    "openrouter": ProviderPreset(
        implementation="openai_compat",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "openai/gpt-5.6",
            "anthropic/claude-sonnet-5-5",
            "deepseek/deepseek-v4-pro",
            "meta-llama/llama-3.3-70b-instruct",
        ],
        category="aggregator",
        family="openai_compat",
        search_aliases=["or", "路由"],
        api_key_url="https://openrouter.ai/keys",
    ),
    "siliconflow": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.siliconflow.cn/v1",
        api_key_env="SILICONFLOW_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "deepseek-ai/DeepSeek-V4",
            "Qwen/Qwen3-72B-Instruct",
            "moonshotai/Kimi-K3",
        ],
        category="national",
        family="openai_compat",
        search_aliases=["硅基流动", "sf"],
        api_key_url="https://cloud.siliconflow.cn/account/ak",
    ),
    "moonshot": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "kimi-k3",
            "kimi-k2.6",
            "kimi-k2.7-code",
        ],
        category="national",
        family="openai_compat",
        search_aliases=["月之暗面", "kimi"],
        api_key_url="https://platform.moonshot.cn/console/api-keys",
    ),
    "zhipu": ProviderPreset(
        implementation="openai_compat",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_env="ZHIPU_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "glm-5",
            "glm-4-plus",
            "glm-4-flash",
        ],
        category="national",
        family="openai_compat",
        search_aliases=["智谱", "glm", "chatglm"],
        api_key_url="https://open.bigmodel.cn/usercenter/apikeys",
    ),
    "groq": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "llama-3.3-70b-versatile",
            "llama-3.3-8b-instant",
            "deepseek-v3",
        ],
        category="aggregator",
        family="openai_compat",
        search_aliases=["groqcloud"],
        api_key_url="https://console.groq.com/keys",
    ),
    "together": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.together.xyz/v1",
        api_key_env="TOGETHER_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "deepseek-ai/DeepSeek-V4",
            "Qwen/Qwen3-72B-Instruct-Turbo",
        ],
        category="aggregator",
        family="openai_compat",
        search_aliases=["together ai"],
        api_key_url="https://api.together.xyz/settings/api-keys",
    ),
    # ── V5 P1：聚合/中转平台（4 个）─────────────────────────────────────
    "fireworks": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY",
        # 未实测：默认按 OpenAI 兼容的 json_object 处理，json_schema 标 False
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "accounts/fireworks/models/llama-v3p1-70b-instruct",
            "accounts/fireworks/models/qwen2p5-72b-instruct",
            "accounts/fireworks/models/deepseek-v3",
        ],
        category="aggregator",
        family="openai_compat",
        search_aliases=["fireworks ai", "烟花"],
        api_key_url="https://fireworks.ai/account/api-keys",
    ),
    "deepinfra": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.deepinfra.com/v1/openai",
        api_key_env="DEEPINFRA_API_KEY",
        # 未实测
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "meta-llama/Llama-3.1-70B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
            "deepseek-ai/DeepSeek-V3",
        ],
        category="aggregator",
        family="openai_compat",
        search_aliases=["deep infra"],
        api_key_url="https://deepinfra.com/dash/api_keys",
    ),
    "replicate": ProviderPreset(
        implementation="openai_compat",
        base_url="https://openai.replicate.com/v1",
        api_key_env="REPLICATE_API_TOKEN",
        # 未实测
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "meta/meta-llama-3.1-70b-instruct",
            "mistralai/mistral-7b-instruct-v0.2",
        ],
        category="aggregator",
        family="openai_compat",
        search_aliases=["replicate", "复制"],
        api_key_url="https://replicate.com/account/api-tokens",
    ),
    "huggingface": ProviderPreset(
        implementation="openai_compat",
        base_url="https://router.huggingface.co/v1",
        api_key_env="HF_API_TOKEN",
        # 未实测
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "meta-llama/Llama-3.1-70B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.3",
        ],
        category="aggregator",
        family="openai_compat",
        search_aliases=["hf", "hugging face", "拥抱脸"],
        api_key_url="https://huggingface.co/settings/tokens",
    ),
    # ── V5 P1：国内平台（5 个）─────────────────────────────────────────
    "baichuan": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.baichuan-ai.com/v1",
        api_key_env="BAICHUAN_API_KEY",
        # 未实测
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "Baichuan4-Turbo",
            "Baichuan3-Turbo",
            "Baichuan4-Air",
        ],
        category="national",
        family="openai_compat",
        search_aliases=["百川", "baichuan"],
        api_key_url="https://platform.baichuan-ai.com/console/apikey",
    ),
    "minimax": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.minimax.chat/v1",
        api_key_env="MINIMAX_API_KEY",
        # 未实测
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "abab6.5s-chat",
            "abab6.5-chat",
            "MiniMax-Text-01",
        ],
        category="national",
        family="openai_compat",
        search_aliases=["minimax", "海螺"],
        api_key_url="https://platform.minimaxi.com/user-center/basic-information/interface-key",
    ),
    "stepfun": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.stepfun.com/v1",
        api_key_env="STEPFUN_API_KEY",
        # 未实测
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "step-1-8k",
            "step-1-32k",
            "step-2-16k",
        ],
        category="national",
        family="openai_compat",
        search_aliases=["阶跃星辰", "step"],
        api_key_url="https://platform.stepfun.com/interface/key",
    ),
    "lingyi": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.lingyiwanwu.com/v1",
        api_key_env="YI_API_KEY",
        # 未实测
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "yi-large",
            "yi-medium",
            "yi-lightning",
        ],
        category="national",
        family="openai_compat",
        search_aliases=["零一万物", "01ai", "yi"],
        api_key_url="https://platform.lingyiwanwu.com/apikeys",
    ),
    "xinghuo": ProviderPreset(
        implementation="openai_compat",
        base_url="https://spark-api-open.xf-yun.com/v1",
        api_key_env="XINGHUO_API_KEY",
        # 未实测
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "4.0Ultra",
            "generalv3.5",
            "generalv3",
        ],
        category="national",
        family="openai_compat",
        search_aliases=["讯飞", "星火", "xfyun"],
        api_key_url="https://console.xfyun.cn/services/cbm",
    ),
    # ── V5 P1：国际主流（3 个）───────────────────────────────────────────
    "gemini": ProviderPreset(
        implementation="openai_compat",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        # 未实测：Gemini 的 OpenAI 兼容端点，默认 json_object=True
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "gemini-1.5-pro",
            "gemini-1.5-flash",
            "gemini-2.0-flash-exp",
        ],
        category="international",
        family="openai_compat",
        search_aliases=["gemini", "google", "谷歌", "bard"],
        api_key_url="https://aistudio.google.com/app/apikey",
    ),
    "mistral": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        # 未实测
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "mistral-large-latest",
            "mistral-small-latest",
            "open-mistral-nemo",
        ],
        category="international",
        family="openai_compat",
        search_aliases=["mistral", "米斯特拉尔"],
        api_key_url="https://console.mistral.ai/api-keys",
    ),
    "cohere": ProviderPreset(
        implementation="openai_compat",
        base_url="https://api.cohere.com/v2",
        api_key_env="CO_API_KEY",
        # 未实测
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
        suggested_models=[
            "command-r-plus",
            "command-r",
            "command-a-03-2025",
        ],
        category="international",
        family="openai_compat",
        search_aliases=["cohere", "coral"],
        api_key_url="https://dashboard.cohere.com/api-keys",
    ),
}


def _apply_override(preset: ProviderPreset, override: dict) -> ProviderPreset:
    """把 ``agentcrew.yaml`` 的单 provider 覆盖合并进预设。

    读取现有字段值后用 ``model_copy(update=...)`` 合并，**未指定的字段保持
    预设值**；``capabilities`` 逐位合并而不是整体替换。
    """
    updates = dict(override)
    caps_patch = updates.pop("capabilities", None)
    if caps_patch is not None:
        updates["capabilities"] = preset.capabilities.model_copy(update=caps_patch)
    return preset.model_copy(update=updates)


class DuplicateProviderError(ValueError):
    """自定义 provider 与内置预设重名（不允许覆盖内置，想覆盖走 provider_overrides）。"""


def build_directory(
    *,
    custom: dict[str, dict] | None = None,
    overrides: dict[str, dict] | None = None,
) -> dict[str, ProviderPreset]:
    """返回「内置预设 + 用户自定义 + provider_overrides」合并后的只读目录。

    - 自定义 provider 与内置重名时抛 :class:`DuplicateProviderError`
      （想改内置走 ``provider_overrides``，不允许静默覆盖）；
    - 自定义 provider 的形状与 :class:`ProviderPreset` 一致，pydantic 会校验
      必填字段与未知字段（``extra="forbid"``）；
    - ``overrides``（即 ``provider_overrides``）逐 provider 叠加在内置与自定义之上。
    """
    directory: dict[str, ProviderPreset] = dict(PROVIDER_PRESETS)
    for name, patch in (custom or {}).items():
        if name in PROVIDER_PRESETS:
            raise DuplicateProviderError(
                f"自定义 provider {name!r} 与内置预设重名；"
                "想调整内置 provider 请用 models.providers 段（provider_overrides），"
                "不要在 models.custom_providers 里覆盖内置。")
        directory[name] = ProviderPreset.model_validate(patch)
    for name, patch in (overrides or {}).items():
        existing = directory.get(name)
        if existing is None:
            # provider_overrides 只允许覆盖内置（config.py 的校验已拦未知 provider），
            # 但这里防御性跳过——不应发生。
            continue
        directory[name] = _apply_override(existing, patch)
    return directory


def parse_alias(
    alias: str,
    *,
    overrides: dict[str, dict] | None = None,
    custom: dict[str, dict] | None = None,
) -> ModelBinding:
    """把 ``"<provider>:<model>"`` 解析成 :class:`ModelBinding`。

    - 只按第一个冒号切分（模型名可含冒号）；
    - 无冒号或模型名为空 → :class:`ValueError`；
    - provider 不在合并目录（内置 + ``custom``）中 → :class:`UnknownProviderError`；
    - ``overrides`` 形如 ``{"doubao": {"capabilities": {...}, "base_url": "..."}}``；
    - ``custom`` 形如 ``{"myrelay": {"implementation": "openai_compat", ...}}``。
    """
    provider, sep, model = alias.partition(":")
    if not sep:
        raise ValueError(f"模型别名缺少 ':'：{alias!r}（应为 '<provider>:<model>'）")
    if not model:
        raise ValueError(f"模型别名为空：{alias!r}")
    directory = build_directory(custom=custom, overrides=overrides)
    preset = directory.get(provider)
    if preset is None:
        raise UnknownProviderError(f"未知 provider：{provider!r}（alias={alias!r}）")
    return ModelBinding(
        alias=alias,
        provider=provider,
        model=model,
        implementation=preset.implementation,
        base_url=preset.base_url,
        api_key_env=preset.api_key_env,
        capabilities=preset.capabilities,
        command=preset.command,
        models_url=preset.models_url,
        category=preset.category,
        family=preset.family,
        search_aliases=preset.search_aliases,
        endpoint_candidates=preset.endpoint_candidates,
        api_key_url=preset.api_key_url,
    )
