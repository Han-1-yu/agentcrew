"""全局配置：五级优先级合并（spec §17.1）。

合并顺序（低 → 高）：
代码默认 → ``.env``（python-dotenv）→ ``agentcrew.yaml`` → ``AGENTCREW_*``
环境变量 → CLI 覆盖。
"""

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentcrew.models.config import PROVIDER_PRESETS, ProviderPreset

GateMode = Literal["both", "plan", "delivery", "none"]

#: provider → 其 API Key 所在环境变量（spec §17.2）。
_PROVIDER_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "doubao": "ARK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "ernie": "QIANFAN_API_KEY",
}

_OLLAMA_KEY_ENV = "OLLAMA_BASE_URL"
#: ollama 无需密钥，但 SDK 要求 api_key 非空。
_OLLAMA_PLACEHOLDER = "ollama"

#: Settings 字段 ← ``AGENTCREW_*`` 环境变量（spec §17.2）。
_ENV_TO_FIELD: dict[str, str] = {
    "AGENTCREW_DEFAULT_MODEL": "default_model",
    "AGENTCREW_MAX_CONCURRENCY": "max_concurrency",
    "AGENTCREW_CONTEXT_BUDGET": "context_budget_tokens",
    "AGENTCREW_GATE": "gate",
    "AGENTCREW_OUTPUT_DIR": "output_dir",
    "AGENTCREW_LOG_LEVEL": "log_level",
    "AGENTCREW_MOCK": "mock",
    "AGENTCREW_MOCK_LATENCY_MS": "mock_latency_ms",
}

#: ``AGENTCREW_MODEL_<ROLE>`` → ``role_bindings[<role 小写>]``（spec §17.2）。
_ROLE_MODEL_ENV_PREFIX = "AGENTCREW_MODEL_"

#: ``agentcrew.yaml`` 顶层标量字段（spec §17.3）。
_YAML_SCALAR_KEYS = ("max_concurrency", "context_budget_tokens", "gate", "roles_dir")

#: 分层合并时按 key 覆盖而非整体替换的字段。
_MAPPING_FIELDS = ("role_bindings", "provider_overrides")

#: ``provider_overrides.<provider>`` 允许出现的键 = ``ProviderPreset`` 的字段。
#: 引擎只读这些（``parse_alias._apply_override``）；别的键会被静默忽略，
#: 因此宁可在这里报错（见 :meth:`Settings._validate_provider_overrides`）。
_OVERRIDE_KEYS: frozenset[str] = frozenset(ProviderPreset.model_fields)

#: 名字命中这些词的覆盖键按「疑似密钥」拒绝：真正的 API Key 只从环境变量读
#: （spec §17.2），而 ``provider_overrides`` 会进 ``run.json`` 的配置快照。
_SECRET_KEY_RE = re.compile(
    r"key|token|secret|password|passwd|credential|auth", re.IGNORECASE)

#: 脱敏后的占位值（配置快照与 ``resume`` 的差异行共用）。
REDACTED = "***"


def redact_overrides(value: Any) -> Any:
    """把 ``provider_overrides`` 形状的映射里名字像密钥的字段值换成 ``***``。

    两层防御里的第二层：读取时的校验（:meth:`Settings._validate_provider_overrides`）
    已经拒掉了新配置里的密钥字段，但**旧的 ``run.json``** 可能是修复之前写下的，
    明文照旧躺在盘上 —— ``resume`` 的配置差异行会把它打到 stderr，所以打印前
    再抹一次。非映射值原样返回（调用方拿到的类型不变）。
    """
    if not isinstance(value, Mapping):
        return value
    redacted: dict[str, Any] = {}
    for provider, patch in value.items():
        if isinstance(patch, Mapping):
            redacted[provider] = {
                field: (REDACTED if _SECRET_KEY_RE.search(str(field)) else item)
                for field, item in patch.items()
            }
        else:
            redacted[provider] = patch
    return redacted


def _default_gate() -> GateMode:
    """默认 ``both``；非 TTY（管道 / CI）降级为 ``none``（spec §17.1）。"""
    try:
        return "both" if sys.stdin.isatty() else "none"
    except (AttributeError, ValueError, OSError):
        return "none"


def _yaml_values() -> dict[str, object]:
    """读取 ``AGENTCREW_CONFIG`` 或 ``./agentcrew.yaml``；缺失则返回空。"""
    path = Path(os.environ.get("AGENTCREW_CONFIG") or "agentcrew.yaml")
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 YAML 映射：{path}")
    values: dict[str, object] = {
        key: data[key] for key in _YAML_SCALAR_KEYS if data.get(key) is not None
    }
    models = data.get("models") or {}
    if not isinstance(models, dict):
        raise ValueError(f"配置文件 models 段必须是映射：{path}")
    if models.get("default"):
        values["default_model"] = models["default"]
    if models.get("role_bindings"):
        values["role_bindings"] = dict(models["role_bindings"])
    if models.get("providers"):
        values["provider_overrides"] = dict(models["providers"])
    return values


def _env_values() -> dict[str, object]:
    """``AGENTCREW_*`` 环境变量；空值视为未设置。"""
    values: dict[str, object] = {}
    for env_name, field_name in _ENV_TO_FIELD.items():
        raw = os.environ.get(env_name, "").strip()
        if raw:
            values[field_name] = raw
    role_bindings: dict[str, str] = {}
    for env_name, raw in os.environ.items():
        if env_name.startswith(_ROLE_MODEL_ENV_PREFIX) and raw.strip():
            role = env_name[len(_ROLE_MODEL_ENV_PREFIX):].lower()
            role_bindings[role] = raw.strip()
    if role_bindings:
        values["role_bindings"] = role_bindings
    return values


def _merge(into: dict[str, object], incoming: Mapping[str, object]) -> None:
    """就地合并一层来源：映射型字段按 key 覆盖，其余整体覆盖。"""
    for key, value in incoming.items():
        current = into.get(key)
        if key in _MAPPING_FIELDS and isinstance(current, Mapping) and isinstance(value, Mapping):
            into[key] = {**current, **value}
        else:
            into[key] = value


class Settings(BaseModel):
    """全局配置（frozen；派生变体用 ``model_copy(update=...)``）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_model: str = "openai:gpt-4o-mini"
    max_concurrency: int = 4
    context_budget_tokens: int = 24000
    gate: GateMode = Field(default_factory=_default_gate)
    output_dir: Path = Field(default_factory=lambda: Path("output").resolve())
    log_level: str = "INFO"
    mock: bool = False
    mock_latency_ms: int = 0
    plan_max_attempts: int = 2
    role_bindings: dict[str, str] = Field(default_factory=dict)
    roles_dir: Path | None = None
    provider_overrides: dict[str, dict] = Field(default_factory=dict)

    @field_validator("output_dir")
    @classmethod
    def _resolve_output_dir(cls, value: Path) -> Path:
        return value.resolve()

    @field_validator("provider_overrides")
    @classmethod
    def _validate_provider_overrides(
        cls, value: dict[str, dict]
    ) -> dict[str, dict]:
        """在**配置读取处**校验 ``agentcrew.yaml`` 的 ``models.providers``。

        Task 17 的 carry-forward：这个字段是唯一一处「自由形状的用户输入直接
        进引擎」的地方，而它在修复前是**双输**的 ——

        - 未知字段被静默忽略（``parse_alias`` 只读 ``ProviderPreset`` 的字段，
          ``_apply_override`` 的 ``model_copy`` 不校验也不拦截）：写错了什么
          都不会发生，操作者以为生效了；
        - 名字像密钥的字段（``api_key`` / ``token`` …）会被原样写进
          ``run.json`` 的 ``config_snapshot``**明文**落盘，并被 ``resume`` 的
          配置差异行打到 stderr。

        真正的 API Key 只从环境变量读（spec §17.2），因此这里直接报错。
        报错消息只点名**字段名**、绝不回显字段值：把密钥抄进异常消息等于换个
        地方泄漏（它会进终端与 CI 日志）。
        """
        available = "、".join(sorted(_OVERRIDE_KEYS))
        for provider, patch in value.items():
            if provider not in PROVIDER_PRESETS:
                known = "、".join(sorted(PROVIDER_PRESETS))
                raise ValueError(
                    f"provider_overrides 含未知 provider {provider!r}；"
                    f"可用 provider：{known}")
            if not isinstance(patch, Mapping):
                raise ValueError(
                    f"provider_overrides.{provider} 必须是映射（字段 → 值），"
                    f"实际是 {type(patch).__name__}")
            for field in patch:
                if field in _OVERRIDE_KEYS:
                    continue
                if _SECRET_KEY_RE.search(str(field)):
                    raise ValueError(
                        f"provider_overrides.{provider}.{field} 疑似密钥字段："
                        "API Key 只从环境变量读取（spec §17.2），"
                        "provider_overrides 会明文进 run.json 的配置快照；"
                        f"请删除该字段。可覆盖字段：{available}")
                raise ValueError(
                    f"provider_overrides.{provider} 含未知字段 {field!r}："
                    f"它不会被任何代码读取（静默忽略只会让人以为生效了）；"
                    f"可覆盖字段：{available}")
        return value

    @classmethod
    def load(cls, **cli_overrides: object) -> "Settings":
        """按五级优先级合并出 ``Settings``（spec §17.1）。"""
        load_dotenv()
        values: dict[str, object] = {}
        _merge(values, _yaml_values())
        _merge(values, _env_values())
        _merge(values, cli_overrides)
        return cls(**values)

    def config_snapshot(self) -> dict[str, Any]:
        """``run.json`` 的 ``config_snapshot``：``provider_overrides`` 脱敏后落盘。

        ``model_dump`` 原样输出这个自由形状的映射，密钥字段一旦混进来就是明文
        落盘（``resume`` 的差异行还会把它打到 stderr）。读取时的校验已经拦住了
        新配置，这里再抹一层：快照是文件，写出去就收不回来了。
        """
        data = self.model_dump(mode="json")
        data["provider_overrides"] = redact_overrides(self.provider_overrides)
        return data

    def secret_values(self) -> list[str]:
        """所有已配置的非空 API Key 值，供日志脱敏使用。"""
        found = [
            raw.strip()
            for env_name in _PROVIDER_KEY_ENV.values()
            if (raw := os.environ.get(env_name, "")).strip()
        ]
        return list(dict.fromkeys(found))

    def api_key_for(self, provider: str) -> str | None:
        """``provider`` → 其 API Key；未配置或为空返回 ``None``。"""
        if provider == "mock":
            return None
        if provider == "ollama":
            # ollama 无需密钥，返回 OLLAMA_BASE_URL 或占位串，保证非空。
            return os.environ.get(_OLLAMA_KEY_ENV, "").strip() or _OLLAMA_PLACEHOLDER
        env_name = _PROVIDER_KEY_ENV.get(provider)
        if env_name is None:
            return None
        return os.environ.get(env_name, "").strip() or None
