"""全局配置：五级优先级合并（spec §17.1）。

合并顺序（低 → 高）：
代码默认 → ``.env``（python-dotenv）→ ``agentcrew.yaml`` → ``AGENTCREW_*``
环境变量 → CLI 覆盖。
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    @classmethod
    def load(cls, **cli_overrides: object) -> "Settings":
        """按五级优先级合并出 ``Settings``（spec §17.1）。"""
        load_dotenv()
        values: dict[str, object] = {}
        _merge(values, _yaml_values())
        _merge(values, _env_values())
        _merge(values, cli_overrides)
        return cls(**values)

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
