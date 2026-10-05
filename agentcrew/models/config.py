"""Provider 能力位、内置预设与模型别名解析（spec §12.1 / §12.2）。

别名格式 ``"<provider>:<model>"``，只按**第一个**冒号切分，因此模型名可以
自带冒号（如 ``ollama:qwen2.5:14b``）。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

#: provider 的实现方式（决定 Task 21 的 ModelFactory 如何构造 Provider）。
Implementation = Literal["openai_compat", "anthropic", "mock"]


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


class ModelBinding(BaseModel):
    """``parse_alias`` 的产物：一个别名解析后的完整绑定。"""

    model_config = ConfigDict(extra="forbid")

    alias: str
    provider: str
    model: str
    base_url: str | None
    api_key_env: str
    capabilities: ProviderCapabilities


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
    ),
    "anthropic": ProviderPreset(
        implementation="anthropic",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        capabilities=ProviderCapabilities(),  # 结构化输出走围栏提取
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
    ),
    "doubao": ProviderPreset(
        implementation="openai_compat",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key_env="ARK_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
    ),
    "qwen": ProviderPreset(
        implementation="openai_compat",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
    ),
    "ernie": ProviderPreset(
        implementation="openai_compat",
        base_url="https://qianfan.baidubce.com/v2",
        api_key_env="QIANFAN_API_KEY",
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=False),
    ),
    "ollama": ProviderPreset(
        implementation="openai_compat",
        base_url="http://localhost:11434/v1",
        api_key_env="",  # 本地模型：api_key 可为任意非空串
        capabilities=ProviderCapabilities(
            supports_json_schema=False, supports_json_object=True),
    ),
    "mock": ProviderPreset(
        implementation="mock",
        base_url=None,
        api_key_env="",  # 离线确定性回放
        capabilities=ProviderCapabilities(
            supports_json_schema=True, supports_json_object=True),
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


def parse_alias(alias: str, *, overrides: dict[str, dict] | None = None) -> ModelBinding:
    """把 ``"<provider>:<model>"`` 解析成 :class:`ModelBinding`。

    - 只按第一个冒号切分（模型名可含冒号）；
    - 无冒号或模型名为空 → :class:`ValueError`；
    - provider 不在 :data:`PROVIDER_PRESETS` 中 → :class:`UnknownProviderError`；
    - ``overrides`` 形如 ``{"doubao": {"capabilities": {...}, "base_url": "..."}}``。
    """
    provider, sep, model = alias.partition(":")
    if not sep:
        raise ValueError(f"模型别名缺少 ':'：{alias!r}（应为 '<provider>:<model>'）")
    if not model:
        raise ValueError(f"模型别名为空：{alias!r}")
    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        raise UnknownProviderError(f"未知 provider：{provider!r}（alias={alias!r}）")
    if overrides and provider in overrides:
        preset = _apply_override(preset, overrides[provider])
    return ModelBinding(
        alias=alias,
        provider=provider,
        model=model,
        base_url=preset.base_url,
        api_key_env=preset.api_key_env,
        capabilities=preset.capabilities,
    )
