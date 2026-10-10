"""全局配置：五级优先级合并（spec §17.1）。

合并顺序（低 → 高）：
代码默认 → ``.env``（python-dotenv）→ ``agentcrew.yaml`` → ``AGENTCREW_*``
环境变量 → CLI 覆盖。
"""

import json
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
    "AGENTCREW_REVIEW_SCORE_THRESHOLD": "review_score_threshold",
    "AGENTCREW_MAX_COST_USD": "max_cost_usd",
    "AGENTCREW_MAX_LLM_CALLS": "max_llm_calls",
    "AGENTCREW_HEALTH_CHECK_ENABLED": "health_check_enabled",
    "AGENTCREW_HEALTH_CHECK_INTERVAL": "health_check_interval_secs",
    "AGENTCREW_LLM_CACHE_ENABLED": "llm_cache_enabled",
    "AGENTCREW_API_AUTH_ENABLED": "api_auth_enabled",
    "AGENTCREW_API_KEYS": "api_keys",
    "AGENTCREW_API_RATE_LIMIT_ENABLED": "api_rate_limit_enabled",
    "AGENTCREW_API_RATE_LIMIT_MAX": "api_rate_limit_max",
}

#: ``AGENTCREW_MODEL_<ROLE>`` → ``role_bindings[<role 小写>]``（spec §17.2）。
_ROLE_MODEL_ENV_PREFIX = "AGENTCREW_MODEL_"

#: ``agentcrew.yaml`` 顶层标量字段（spec §17.3）。
_YAML_SCALAR_KEYS = ("max_concurrency", "context_budget_tokens", "gate", "roles_dir")

#: 分层合并时按 key 覆盖（映射型）/ 按序合并去重（列表型）而非整体替换的字段。
_MAPPING_FIELDS = (
    "role_bindings", "provider_overrides", "model_tiers", "model_failover",
    "mcp_servers", "gate_config", "tool_permissions", "api_keys",
    "custom_providers",
)

#: ``AGENTCREW_TIER_<LEVEL>`` → ``model_tiers[<level>]``（V2：按复杂度选模型）。
_TIER_ENV_PREFIX = "AGENTCREW_TIER_"

#: ``AGENTCREW_FAILOVER_<ALIAS>`` → ``model_failover[<alias>]``（P0-2：Provider 故障转移）。
#: 别名中的 ``:`` 在环境变量名里用 ``__`` 表示：``AGENTCREW_FAILOVER_OPENAI__GPT-4O-MINI``
#: 对应别名 ``openai:gpt-4o-mini``；值为逗号分隔的备用别名列表。
_FAILOVER_ENV_PREFIX = "AGENTCREW_FAILOVER_"

#: ``provider_overrides.<provider>`` 允许出现的键 = ``ProviderPreset`` 的字段。
#: 引擎只读这些（``parse_alias._apply_override``）；别的键会被静默忽略，
#: 因此宁可在这里报错（见 :meth:`Settings._validate_provider_overrides`）。
_OVERRIDE_KEYS: frozenset[str] = frozenset(ProviderPreset.model_fields)

#: 名字命中这些词的覆盖键按「疑似密钥」拒绝：真正的 API Key 只从环境变量读
#: （spec §17.2），而 ``provider_overrides`` 会进 ``run.json`` 的配置快照。
#: 校验与脱敏**都按 schema 递归**（``capabilities`` 里藏一个 ``api_key`` 同样
#: 会落到配置文件里），所以这条正则对每一层的键都成立。
_SECRET_KEY_RE = re.compile(
    r"key|token|secret|password|passwd|credential|auth", re.IGNORECASE)

#: 脱敏后的占位值（配置快照与 ``resume`` 的差异行共用）。
REDACTED = "***"


def redact_overrides(value: Any) -> Any:
    """把 ``provider_overrides`` 形状的映射里名字像密钥的字段值换成 ``***``。

    两层防御里的第二层：读取时的校验（:meth:`Settings._validate_provider_overrides`）
    已经拒掉了新配置里的密钥字段，但**旧的 ``run.json``** 可能是修复之前写下的，
    明文照旧躺在盘上 —— ``resume`` 的配置差异行会把它打到 stderr，所以打印前
    再抹一次。

    递归**按 schema 走**（:func:`_nested_models`），而不是只看顶层：覆盖结构是
    「映射套映射」（``capabilities`` 今天就嵌套一层），只查第一层等于把承诺的深度
    交给「猜密钥会写在哪一层」。schema 认识的字段一律不动 —— 能力位里真的有
    ``max_output_tokens`` / ``max_context_tokens`` 这种名字含 ``token`` 的合法
    字段，按名字一刀切会把它们也抹成 ``***``；只有**未知**字段才按名字判密钥，
    未知字段的子树继续按名字递归找密钥。非映射值原样返回。
    """
    if not isinstance(value, Mapping):
        return value
    return {
        provider: _redact_fields(patch, ProviderPreset)
        for provider, patch in value.items()
    }


def _redact_fields(patch: Any, model: type[BaseModel]) -> Any:
    """按 ``model`` 的 schema 脱敏一层的值；未知字段按名字判、子树继续递归。"""
    if not isinstance(patch, Mapping):
        return patch
    nested = _nested_models(model)
    redacted: dict[Any, Any] = {}
    for field, item in patch.items():
        if field in model.model_fields:
            sub_model = nested.get(field)
            redacted[field] = (
                _redact_fields(item, sub_model) if sub_model is not None else item)
        else:
            redacted[field] = (
                REDACTED if _SECRET_KEY_RE.search(str(field))
                else _redact_unknown(item))
    return redacted


def _redact_unknown(value: Any) -> Any:
    """没有 schema 可依的子树：逐层按**字段名**判密钥，其余原样保留。"""
    if not isinstance(value, Mapping):
        return value
    return {
        key: (
            REDACTED if _SECRET_KEY_RE.search(str(key))
            else _redact_unknown(item))
        for key, item in value.items()
    }


def _nested_models(model: type[BaseModel]) -> dict[str, type[BaseModel]]:
    """``model`` 里类型是 pydantic 模型的字段 → 它的模型（递归校验的下一层）。"""
    return {
        name: info.annotation
        for name, info in model.model_fields.items()
        if isinstance(info.annotation, type) and issubclass(info.annotation, BaseModel)
    }


def _check_override_fields(
    patch: Mapping[Any, Any], model: type[BaseModel], *, path: str
) -> None:
    """按 schema 校验**一层**覆盖：未知字段与疑似密钥字段都报错（只点名字段）。

    递归的依据是 schema 本身（:func:`_nested_models`），不是写死的
    ``capabilities``：将来 ``ProviderPreset`` 再多一个模型型字段，这一层自动覆盖。
    报错消息绝不回显字段值 —— 把密钥抄进异常消息等于换个地方泄漏（它会进终端与
    CI 日志）。
    """
    available = "、".join(sorted(model.model_fields))
    nested = _nested_models(model)
    for field, item in patch.items():
        if field not in model.model_fields:
            if _SECRET_KEY_RE.search(str(field)):
                raise ValueError(
                    f"{path}.{field} 疑似密钥字段："
                    "API Key 只从环境变量读取（spec §17.2），"
                    "provider_overrides 会明文进 run.json 的配置快照；"
                    "请删除该字段（若这其实是字段名笔误，它同样不会被任何代码"
                    f"读取）。可覆盖字段：{available}")
            raise ValueError(
                f"{path}.{field} 是未知字段："
                f"它不会被任何代码读取（静默忽略只会让人以为生效了）；"
                f"可覆盖字段：{available}")
        sub_model = nested.get(field)
        if sub_model is None:
            continue
        if not isinstance(item, Mapping):
            raise ValueError(
                f"{path}.{field} 必须是映射（字段 → 值），"
                f"实际是 {type(item).__name__}；可覆盖字段："
                + "、".join(sorted(sub_model.model_fields)))
        _check_override_fields(item, sub_model, path=f"{path}.{field}")


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
    # A6：配置版本号 —— 高于当前支持的版本直接报错（不静默吞掉未知字段）。
    config_version = data.get("config_version")
    if config_version is not None:
        values["config_version"] = config_version
    models = data.get("models") or {}
    if not isinstance(models, dict):
        raise ValueError(f"配置文件 models 段必须是映射：{path}")
    if models.get("default"):
        values["default_model"] = models["default"]
    if models.get("role_bindings"):
        values["role_bindings"] = dict(models["role_bindings"])
    if models.get("providers"):
        values["provider_overrides"] = dict(models["providers"])
    if models.get("custom_providers"):
        custom = models["custom_providers"]
        if not isinstance(custom, dict):
            raise ValueError(
                f"配置文件 models.custom_providers 段必须是映射：{path}")
        values["custom_providers"] = dict(custom)
    mcp_servers = data.get("mcp_servers")
    if mcp_servers:
        if not isinstance(mcp_servers, dict):
            raise ValueError(f"配置文件 mcp_servers 段必须是映射：{path}")
        values["mcp_servers"] = dict(mcp_servers)
    gate_config = data.get("gate_config")
    if gate_config:
        if not isinstance(gate_config, dict):
            raise ValueError(f"配置文件 gate_config 段必须是映射：{path}")
        values["gate_config"] = dict(gate_config)
    tool_permissions = data.get("tool_permissions")
    if tool_permissions:
        if not isinstance(tool_permissions, dict):
            raise ValueError(f"配置文件 tool_permissions 段必须是映射：{path}")
        values["tool_permissions"] = dict(tool_permissions)
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
    # V2：按复杂度选模型 —— AGENTCREW_TIER_LOW/MEDIUM/HIGH
    model_tiers: dict[str, str] = {}
    for env_name, raw in os.environ.items():
        if env_name.startswith(_TIER_ENV_PREFIX) and raw.strip():
            level = env_name[len(_TIER_ENV_PREFIX):].lower()
            if level in ("low", "medium", "high"):
                model_tiers[level] = raw.strip()
    if model_tiers:
        values["model_tiers"] = model_tiers
    # P0-2：Provider 故障转移 —— AGENTCREW_FAILOVER_<ALIAS>（别名中 : 用 __ 表示）
    model_failover: dict[str, list[str]] = {}
    for env_name, raw in os.environ.items():
        if env_name.startswith(_FAILOVER_ENV_PREFIX) and raw.strip():
            suffix = env_name[len(_FAILOVER_ENV_PREFIX):].lower()
            alias = suffix.replace("__", ":")
            backups = [b.strip() for b in raw.split(",") if b.strip()]
            if backups:
                model_failover[alias] = backups
    if model_failover:
        values["model_failover"] = model_failover
    # E-3：AGENTCREW_API_KEYS 逗号分隔 → list[str]。通用循环把它当成普通标量
    # 塞进了 raw 字符串，而 pydantic 不能把 "a,b" 直接强转成 list[str]，在这里拆。
    if isinstance(values.get("api_keys"), str):
        values["api_keys"] = [
            key.strip() for key in values["api_keys"].split(",") if key.strip()
        ]
    # A1：AGENTCREW_CUSTOM_PROVIDERS 紧凑 JSON → dict[str, dict]。
    # 形如 '{"myrelay":{"implementation":"openai_compat","base_url":"http://h/v1",
    # "api_key_env":"MYRELAY_KEY"}}'。
    raw_custom = os.environ.get("AGENTCREW_CUSTOM_PROVIDERS", "").strip()
    if raw_custom:
        try:
            parsed = json.loads(raw_custom)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"AGENTCREW_CUSTOM_PROVIDERS 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                "AGENTCREW_CUSTOM_PROVIDERS 必须是 JSON 对象（名字 → 预设映射）")
        values["custom_providers"] = parsed
    return values


def _merge(into: dict[str, object], incoming: Mapping[str, object]) -> None:
    """就地合并一层来源：映射型字段按 key 覆盖、列表型字段按序合并去重，其余整体覆盖。"""
    for key, value in incoming.items():
        current = into.get(key)
        if key in _MAPPING_FIELDS and isinstance(current, Mapping) and isinstance(value, Mapping):
            into[key] = {**current, **value}
        elif key in _MAPPING_FIELDS and isinstance(current, list) and isinstance(value, list):
            merged = list(current)
            for item in value:
                if item not in merged:
                    merged.append(item)
            into[key] = merged
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
    custom_providers: dict[str, dict] = Field(default_factory=dict)
    """用户自定义 provider 注册表（A1）：名字 → ProviderPreset 形状的映射。

    与内置预设并存，不允许与内置重名（想改内置走 ``provider_overrides``）。
    每个条目形状同 :class:`~agentcrew.models.config.ProviderPreset`，必填
    ``implementation`` / ``base_url`` / ``api_key_env`` / ``capabilities``。
    ``api_key_env`` 只存环境变量名，密钥值绝不落盘。
    """
    config_version: int = 1
    """配置文件版本号（A6）。当前支持 v1；读取到更高版本时报清晰错误。"""
    model_tiers: dict[str, str] = Field(default_factory=dict)
    model_failover: dict[str, list[str | dict[str, Any]]] = Field(default_factory=dict)
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """外部 MCP 服务器配置（P1-1）：服务器名 → 启动参数映射。

    形状为 ``{"filesystem": {"command": ["npx", "-y",
    "@modelcontextprotocol/server-filesystem", "/tmp"]}}``；Crew 层据此经
    :class:`~agentcrew.mcp.client.MCPClient` 起服务器、发现工具并登记进
    :class:`~agentcrew.tools.registry.ToolRegistry`。配置快照落盘前按字段名
    脱敏（含 key/token/secret 的值替换为 ``***``，见 :meth:`config_snapshot`）。
    环境变量解析从简：当前以 YAML 配置为准。
    """
    """Provider 故障转移（P0-2）：别名 → 备用别名列表。

    当主别名的 Provider 抛出不可用错误（503/429/连接超时/读取超时，重试耗尽后），
    ModelFactory 按列表依次尝试备用别名，记 ``llm_failover`` 事件。故障转移不消耗
    任务的 attempts（那是执行失败，不是模型不可用）。全部备用都失败时抛原始错误。
    环境变量 ``AGENTCREW_FAILOVER_<ALIAS>``（别名中 ``:`` 用 ``__`` 表示，值逗号分隔）。
    """
    review_score_threshold: float | None = None
    max_cost_usd: float | None = None
    """累计 LLM 成本硬上限（USD，P0-3）。

    ``None`` = 不限制。ModelFactory 在每次记账后检查累计成本，超限时抛
    ``BudgetExhaustedError``，Crew 优雅收尾（等在跑任务完成，不再派发新任务），
    记 ``budget_exhausted`` 事件，退出码 1。环境变量 ``AGENTCREW_MAX_COST_USD``。
    """
    max_llm_calls: int | None = None
    """累计 LLM 调用次数硬上限（P0-3）。

    ``None`` = 不限制。与 ``max_cost_usd`` 任一超限即触发预算耗尽收尾。
    环境变量 ``AGENTCREW_MAX_LLM_CALLS``。
    """
    health_check_enabled: bool = True
    """Provider 主动健康监控开关（P2-11）。

    启用后后台定期探测所有已缓存 Provider 的健康状态，failover 时优先跳过
    不健康的 Provider。``False`` = 不启动监控，行为与改动前完全一致。
    环境变量 ``AGENTCREW_HEALTH_CHECK_ENABLED``。
    """
    health_check_interval_secs: int = 30
    """健康探测间隔秒数（P2-11）。

    后台任务每隔这么多秒对所有已缓存的别名做一次健康探测。
    环境变量 ``AGENTCREW_HEALTH_CHECK_INTERVAL``。
    """
    llm_cache_enabled: bool = False
    """LLM 响应缓存总开关（P3-2）。

    ``True`` 时工厂为本次 run 启用进程内响应缓存，对 ``temperature == 0`` 的
    确定性调用按 alias+messages+采样参数复用结果，命中不消耗 token/成本。
    ``False``（默认）时工厂不挂缓存，所有 LLM 调用原样走 Provider，行为与改造前
    完全一致 —— 默认关闭是为了避免意外改变既有输出/计费口径。
    环境变量 ``AGENTCREW_LLM_CACHE_ENABLED``。
    """
    api_auth_enabled: bool = False
    """REST API 的 API Key 认证总开关（E-3）。

    ``False``（默认）时 :class:`~agentcrew.api.server.ApiServer` 不挂认证中间件，
    所有端点匿名可访问，行为与改造前逐字节一致。``True`` 时 serve 子命令用
    :attr:`api_keys` 构造 :class:`~agentcrew.api.auth.APIKeyAuth`，请求须带
    ``Authorization: Bearer <key>`` 或 ``X-API-Key: <key>``，否则回 401。
    环境变量 ``AGENTCREW_API_AUTH_ENABLED``。
    """
    api_keys: list[str] = Field(default_factory=list)
    """REST API 的有效 API Key 白名单（E-3）。

    仅当 :attr:`api_auth_enabled` 为真时生效。环境变量
    ``AGENTCREW_API_KEYS`` 为逗号分隔列表；经分层合并与 YAML / CLI 的同名列表
    按序合并去重。Key 只参与布尔比对，绝不写入日志 / ledger（见
    :class:`~agentcrew.api.auth.APIKeyAuth`）。
    """
    api_rate_limit_enabled: bool = False
    """REST API 的 IP 速率限制总开关（E-3）。

    ``False``（默认）时不挂限流中间件。``True`` 时 serve 子命令按
    :attr:`api_rate_limit_max` / :attr:`api_rate_limit_window` 构造
    :class:`~agentcrew.api.auth.RateLimiter`，超限回 429 + ``Retry-After``。
    环境变量 ``AGENTCREW_API_RATE_LIMIT_ENABLED``。
    """
    api_rate_limit_max: int = 100
    """滑动窗口内每 IP 允许的最大请求数（E-3）；``<= 0`` 视为不限流。"""
    api_rate_limit_window: int = 60
    """速率限制窗口时长（秒，E-3）；与 :attr:`api_rate_limit_max` 共同决定策略。"""
    gate_config: dict[str, Any] = Field(default_factory=dict)
    """门禁规则配置化（P2-5）：G1/G2/G3 开关与评审参数。

    支持的键（缺省回落硬编码默认值；空 dict = 旧行为）：

    - ``g1_enabled`` (bool=True)：G1 静态校验是否执行；
    - ``g2_enabled`` (bool=True)：G2 LLM 评审是否执行；
    - ``g3_enabled`` (bool=True)：G3 人工卡点（与 gate 模式配合，``gate="none"`` 时
      全部关闭）；
    - ``g2_reviewers`` (int=1)：并行评审人数（P1-2），取目录前 N 个评审人；
    - ``convergence_threshold`` (int|None=None)：P2-3 收敛阈值，0/None 关闭
      （默认关闭以保持旧行为）；
    - ``review_score_threshold`` (float|None=None)：P0-1 score 阈值；
    - ``max_review_attempts`` (int=3)：最大评审轮次。

    经 YAML ``gate_config:`` 段加载；进 ``config_snapshot`` 落盘。
    """
    """G2 评审 score 阈值放行（P0-1）。

    ``None`` = 不启用，保持现有行为（reject 即驳回）。设置后，当 LLM 评审
    verdict="reject" 但 score >= threshold 时，Gatekeeper 改为放行，记
    ``gate_result`` 事件 verdict 标为 "approve"、issues 保留、reviewer 追加
    ``:score-bypass``。环境变量 ``AGENTCREW_REVIEW_SCORE_THRESHOLD=0.8``。
    """
    tool_permissions: dict[str, list[str]] = Field(default_factory=dict)
    """角色级工具权限白名单（P3-9）：角色 key → 允许的工具名列表。

    形状为 ``{"backend_dev": ["read_file", "run_python"]}``；Crew 层据此在
    :class:`~agentcrew.tools.registry.ToolRegistry` 上调用
    :meth:`~agentcrew.tools.registry.ToolRegistry.set_role_permissions`。未列出的
    角色默认可使用全部工具（向后兼容）；``["*"]`` 或空列表表示全部允许。被拒绝
    的工具记 ``tool_permission_denied`` ledger 事件、绝不执行。经 YAML
    ``tool_permissions:`` 段加载。
    """

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

        真正的 API Key 只从环境变量读（spec §17.2），因此这里直接报错。校验
        **按 schema 递归**（:func:`_check_override_fields`）：``capabilities`` 那
        一层藏一个 ``api_key`` 同样会被拦住 —— 「密钥绝不落盘」的承诺不能只覆盖
        到猜得到的那一层。报错只点名字段、绝不回显字段值。
        """
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
            _check_override_fields(
                patch, ProviderPreset, path=f"provider_overrides.{provider}")
        return value

    @field_validator("custom_providers")
    @classmethod
    def _validate_custom_providers(
        cls, value: dict[str, dict]
    ) -> dict[str, dict]:
        """校验 ``models.custom_providers``：重名 / 未知字段 / 疑似密钥字段。

        - 与内置预设重名 → 报错（想改内置走 ``provider_overrides``）；
        - 每个条目按 :class:`ProviderPreset` 的 schema 递归校验（未知字段与疑似
          密钥字段一律报错，与 ``provider_overrides`` 同一条规则）；
        - 再用 pydantic 校验必填字段（implementation/base_url/api_key_env/
          capabilities）。
        """
        for name, patch in value.items():
            if name in PROVIDER_PRESETS:
                raise ValueError(
                    f"custom_providers.{name} 与内置预设重名；"
                    "想调整内置 provider 请用 models.providers 段"
                    "（provider_overrides），不要在 custom_providers 里覆盖内置。")
            if not isinstance(patch, Mapping):
                raise ValueError(
                    f"custom_providers.{name} 必须是映射（字段 → 值），"
                    f"实际是 {type(patch).__name__}")
            _check_override_fields(
                patch, ProviderPreset, path=f"custom_providers.{name}")
            try:
                ProviderPreset.model_validate(patch)
            except Exception as exc:
                raise ValueError(
                    f"custom_providers.{name} 校验失败：{exc}") from exc
        return value

    @field_validator("config_version")
    @classmethod
    def _validate_config_version(cls, value: int) -> int:
        """A6：高于当前支持的 v1 报清晰错误。"""
        if value > 1:
            raise ValueError(
                f"配置文件版本 v{value} 高于当前支持的 v1，请升级 AgentCrew")
        return value

    @classmethod
    def load(cls, **cli_overrides: object) -> "Settings":
        """按五级优先级合并出 ``Settings``（spec §17.1）。"""
        load_dotenv()
        values: dict[str, object] = {}
        _merge(values, _yaml_values())
        _merge(values, _env_values())
        _merge(values, cli_overrides)
        return cls.model_validate(values)

    def config_snapshot(self) -> dict[str, Any]:
        """``run.json`` 的 ``config_snapshot``：``provider_overrides`` 脱敏后落盘。

        ``model_dump`` 原样输出这个自由形状的映射，密钥字段一旦混进来就是明文
        落盘（``resume`` 的差异行还会把它打到 stderr）。读取时的校验已经拦住了
        新配置，这里再抹一层：快照是文件，写出去就收不回来了。
        """
        data = self.model_dump(mode="json")
        data["provider_overrides"] = redact_overrides(self.provider_overrides)
        # custom_providers 的值形状与 ProviderPreset 一致，复用同款 schema 递归脱敏。
        data["custom_providers"] = redact_overrides(self.custom_providers)
        # mcp_servers 是自由形状映射（command/args/env…），没有 schema 可依，
        # 逐层按字段名判密钥（含 key/token/secret 即抹成 ***）。
        data["mcp_servers"] = _redact_unknown(self.mcp_servers)
        return data

    def _api_key_env_name(self, provider: str) -> str:
        """``provider`` → 密钥所在环境变量名（overrides → 内置预设 → 自定义）。

        内置预设里的 ``api_key_env`` 才是唯一事实源：A2 新增 openrouter /
        siliconflow / moonshot / zhipu / groq / together 时只改了 ``PROVIDER_PRESETS``，
        没有同步硬编码的 :data:`_PROVIDER_KEY_ENV`，导致即便用户设了对应环境变量，
        :meth:`api_key_for` 仍返回 None、工厂误报「缺少 API Key」。这里直接查合并目录，
        不再维护第二份会漂移的映射。
        """
        override = self.provider_overrides.get(provider)
        if isinstance(override, Mapping) and override.get("api_key_env"):
            return str(override["api_key_env"])
        preset = PROVIDER_PRESETS.get(provider)
        if preset is not None:
            return preset.api_key_env
        custom = self.custom_providers.get(provider)
        if isinstance(custom, Mapping) and custom.get("api_key_env"):
            return str(custom["api_key_env"])
        return ""

    def secret_values(self) -> list[str]:
        """所有已配置的非空 API Key 值，供日志脱敏使用。

        覆盖内置预设（含 A2 新增平台）与自定义 provider 的 ``api_key_env``——
        自定义 provider 的密钥一旦漏进事件/日志也必须被抹掉。
        """
        env_names: list[str] = list(_PROVIDER_KEY_ENV.values())
        for preset in PROVIDER_PRESETS.values():
            if preset.api_key_env:
                env_names.append(preset.api_key_env)
        for custom in self.custom_providers.values():
            if isinstance(custom, Mapping) and custom.get("api_key_env"):
                env_names.append(str(custom["api_key_env"]))
        found = [
            raw.strip()
            for env_name in env_names
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
        env_name = self._api_key_env_name(provider)
        if not env_name:
            return None
        return os.environ.get(env_name, "").strip() or None
