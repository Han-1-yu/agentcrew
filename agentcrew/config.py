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
from agentcrew.roles.binding import AgentProfile, RoleAgents

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
    "AGENTCREW_LOOP_GUARD": "loop_guard_enabled",
    "AGENTCREW_HEALTH_CHECK_ENABLED": "health_check_enabled",
    "AGENTCREW_HEALTH_CHECK_INTERVAL": "health_check_interval_secs",
    "AGENTCREW_LLM_CACHE_ENABLED": "llm_cache_enabled",
    "AGENTCREW_API_AUTH_ENABLED": "api_auth_enabled",
    "AGENTCREW_API_KEYS": "api_keys",
    "AGENTCREW_API_RATE_LIMIT_ENABLED": "api_rate_limit_enabled",
    "AGENTCREW_API_RATE_LIMIT_MAX": "api_rate_limit_max",
    "AGENTCREW_REDACT_ENABLED": "redact_enabled",
    "AGENTCREW_REDACT_API_KEYS": "redact_api_keys",
    "AGENTCREW_REDACT_PII": "redact_pii",
    "AGENTCREW_REDACT_CREDENTIALS": "redact_credentials",
    "AGENTCREW_ROUTING_STRATEGY": "routing_strategy",
    "AGENTCREW_AFFINITY_MODE": "affinity_mode",
    "AGENTCREW_TOOLS_ENABLED": "tools_enabled",
    "AGENTCREW_ENTITY_MEMORY_ENABLED": "entity_memory_enabled",
    "AGENTCREW_APPROVAL_MODE": "approval_mode",
    "AGENTCREW_PEER_DIALOGUE_ENABLED": "peer_dialogue_enabled",
    "AGENTCREW_MAX_PEER_MESSAGES": "max_peer_messages",
    "AGENTCREW_DELEGATION_MODE": "delegation_mode",
    "AGENTCREW_MAX_DELEGATION_DEPTH": "max_delegation_depth",
    "AGENTCREW_MAX_DYNAMIC_TASKS": "max_dynamic_tasks",
    "AGENTCREW_PEER_CONSULT_CAPTURE_ENABLED": "peer_consult_capture_enabled",
    "AGENTCREW_SUPERVISOR_REVIEW_ENABLED": "supervisor_review_enabled",
    "AGENTCREW_MAX_SUBTASK_CORRECTIONS": "max_subtask_corrections",
    "AGENTCREW_SUPERVISOR_SCOPE": "supervisor_scope",
    "AGENTCREW_SUPERVISOR_ROLE": "supervisor_role",
    "AGENTCREW_MAX_SUPERVISOR_REASSIGNMENTS": "max_supervisor_reassignments",
}

#: ``AGENTCREW_MODEL_<ROLE>`` → ``role_bindings[<role 小写>]``（spec §17.2）。
_ROLE_MODEL_ENV_PREFIX = "AGENTCREW_MODEL_"

#: ``agentcrew.yaml`` 顶层标量字段（spec §17.3）。
_YAML_SCALAR_KEYS = (
    "max_concurrency", "context_budget_tokens", "gate", "roles_dir",
    # R3：两个能力开关都是顶层标量，走与 max_concurrency 同一条读取/合并路径。
    "tools_enabled", "entity_memory_enabled", "approval_mode",
    # R5：跨角色咨询的能力开关与配额。
    "peer_dialogue_enabled", "max_peer_messages",
    # R6：动态委派的治理参数。
    "delegation_mode", "max_delegation_depth", "max_dynamic_tasks",
    "peer_consult_capture_enabled", "supervisor_review_enabled",
    "max_subtask_corrections", "supervisor_scope", "supervisor_role",
    "max_supervisor_reassignments",
)

#: 分层合并时按 key 覆盖（映射型）/ 按序合并去重（列表型）而非整体替换的字段。
_MAPPING_FIELDS = (
    "role_bindings", "provider_overrides", "model_tiers", "model_failover",
    "mcp_servers", "gate_config", "tool_permissions", "api_keys",
    "role_fallback",
    "custom_providers",
    "agent_profiles", "role_agents",
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
    # YAML 语法错误抛的是 yaml.YAMLError（ScannerError / ParserError / ComposerError），
    # 不是 ValueError / OSError。CLI 的每个子命令都按 ``except (OSError, ValueError)``
    # 收口配置错误，不在这里收敛就会让一次手改坏缩进变成全命令栈崩（连 --help 和
    # doctor 都打不开）。收敛成 ValueError 后，所有入口复用同一条「配置错误」路径。
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"配置文件 YAML 解析失败：{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 YAML 映射：{path}")
    values: dict[str, object] = {
        key: data[key] for key in _YAML_SCALAR_KEYS if data.get(key) is not None
    }
    # A6：配置版本号 —— 高于当前支持的版本直接报错（不静默吞掉未知字段）。
    config_version = data.get("config_version")
    if config_version is not None:
        values["config_version"] = config_version
    agents_cfg = data.get("agents") or {}
    if agents_cfg:
        if not isinstance(agents_cfg, dict):
            raise ValueError(f"配置文件 agents 段必须是映射：{path}")
        profiles = agents_cfg.get("profiles") or {}
        if not isinstance(profiles, dict):
            raise ValueError(f"配置文件 agents.profiles 段必须是映射：{path}")
        if profiles:
            # profile 的 key 就是映射键：补进去，省得配置文件里再抄一遍
            values["agent_profiles"] = {
                name: ({**cfg, "key": name} if isinstance(cfg, dict) else cfg)
                for name, cfg in profiles.items()
            }
    roles_cfg = data.get("roles") or {}
    if roles_cfg:
        if not isinstance(roles_cfg, dict):
            raise ValueError(f"配置文件 roles 段必须是映射：{path}")
        values["role_agents"] = dict(roles_cfg)

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
    role_fallback = data.get("role_fallback")
    if role_fallback:
        if not isinstance(role_fallback, dict):
            raise ValueError(f"配置文件 role_fallback 段必须是映射：{path}")
        # 形状固定为 role → [备选角色…]；写成标量是常见手误，当场报错比默默
        # 把字符串拆成单字符好（Python 对 str 迭代正是这么做的）。
        normalized: dict[str, list[str]] = {}
        for key, raw in role_fallback.items():
            if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
                raise ValueError(
                    f"配置文件 role_fallback.{key} 必须是角色 key 列表：{path}")
            normalized[str(key)] = [str(item) for item in raw if str(item).strip()]
        values["role_fallback"] = normalized
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
    raw_api_keys = values.get("api_keys")
    if isinstance(raw_api_keys, str):
        values["api_keys"] = [
            key.strip() for key in raw_api_keys.split(",") if key.strip()
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
    agent_profiles: dict[str, AgentProfile] = Field(default_factory=dict)
    """执行体档案（多 agent 的"怎么干"）：key → AgentProfile。"""
    role_agents: dict[str, RoleAgents] = Field(default_factory=dict)
    """角色 → 执行体绑定（覆盖 roles.d 里的内联声明）：role → RoleAgents。"""
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
    budget_warning_threshold: float = 0.8
    """预算接近警告阈值（P2 增强）：占 ``max_cost_usd`` 的比例。

    累计成本首次达到 ``max_cost_usd * budget_warning_threshold`` 时，工厂落一条
    ``budget_warning`` 事件（含 current / limit / pct），只警告不中断。
    仅当 ``max_cost_usd`` 不为 ``None`` 时生效；否则行为完全不变。
    """
    budget_downgrade_threshold: float = 0.9
    """预算自动降级阈值（P2 增强）：占 ``max_cost_usd`` 的比例。

    累计成本首次达到 ``max_cost_usd * budget_downgrade_threshold`` 时，若
    ``model_failover`` 里存在 input 单价更低的备用别名，工厂落一条
    ``budget_downgrade`` 事件并把后续调用切到最便宜的备用别名（而不是直接
    抛 :class:`~agentcrew.models.factory.BudgetExhaustedError`）。无更便宜
    选项时保持原行为。仅当 ``max_cost_usd`` 不为 ``None`` 时生效。
    """
    loop_guard_enabled: bool = False
    """流式循环检测总开关（P1.5）。

    ``False``（默认）时 :meth:`~agentcrew.models.factory.ModelFactory.stream_call`
    不挂 :class:`~agentcrew.models.loop_guard.LoopGuard`，行为与改造前逐字节一致。
    ``True`` 时每个流式 chunk 喂入滑动窗口检测器，窗口内 distinct unit 过少即
    抛 :class:`~agentcrew.models.loop_guard.LoopDetectedError` 并落 ``loop_detected``
    事件。环境变量 ``AGENTCREW_LOOP_GUARD``。
    """
    redact_enabled: bool = False
    """请求数据脱敏总开关（V7-P0，借鉴 magpie redact）。

    ``False``（默认）时所有 LLM 请求原样发送，行为与改造前逐字节一致。
    ``True`` 时 :class:`~agentcrew.models.factory.ModelFactory` 在每次调用前用
    :class:`~agentcrew.models.redact.Redactor` 对消息内容中的 API Key、密码、
    个人身份信息等做确定性占位符替换，响应返回后还原。
    环境变量 ``AGENTCREW_REDACT_ENABLED``。
    """
    redact_api_keys: bool = True
    """脱敏时是否替换 API Key / Secret / Token（V7-P0）。仅 ``redact_enabled`` 为真时生效。"""
    redact_pii: bool = True
    """脱敏时是否替换个人身份信息（邮箱、手机号、身份证、银行卡）（V7-P0）。"""
    redact_credentials: bool = True
    """脱敏时是否替换密码和私钥块（V7-P0）。"""
    redact_custom_words: list[str] = Field(default_factory=list)
    """自定义敏感词列表（V7-P0）：精确匹配、大小写不敏感，命中即替换为占位符。"""
    routing_strategy: str = "weighted"
    """多模型路由策略（V7-P1，借鉴 magpie routing）。

    当 ``model_failover`` 配置了权重格式的备用别名池时，本字段决定选择算法：

    - ``weighted``（默认）：按权重随机选择，与 V3-10 行为完全一致；
    - ``rotate``：轮询，每次调用选下一个别名，均匀分摊请求；
    - ``usage``：优先选累计 token 使用量最少的别名（指数衰减，半衰期 1 小时）；
    - ``order``：按列表顺序选第一个可用别名（等价于顺序 failover）。

    未配置权重池时本字段无效，走顺序 failover。环境变量 ``AGENTCREW_ROUTING_STRATEGY``。
    """
    quotas: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """按别名的多窗口配额（V7-P2，借鉴 magpie budget）。

    与全局 ``max_cost_usd`` / ``max_llm_calls``（run 生命周期累计）不同，本配置
    按日/周/月自然窗口滚动重置，且每个别名独立计数。适合多 key 池化时给每个 key
    设独立限额（如免费额度 100 次/天）。

    配置格式::

        quotas:
          openai:gpt-4o-mini:
            daily:
              max_calls: 100
              max_tokens: 100000
            weekly:
              max_cost_usd: 5.0
          anthropic:claude-3-5-sonnet:
            monthly:
              max_calls: 1000

    配额耗尽时工厂抛 :class:`~agentcrew.models.quota.QuotaExhaustedError`，
    触发故障转移到下一个备用别名。状态持久化到 ``output/quota_state.json``，
    跨 run 保留。空 dict = 不启用，行为与改造前完全一致。
    """
    quota_enabled: bool = True
    """多窗口配额总开关（V7-P2）。``False`` 时即使配置了 ``quotas`` 也不检查。"""
    affinity_mode: str = "off"
    """会话亲和性模式（V7-P3，借鉴 magpie affinity）。

    多 key / 多模型池化时，让同一会话保持在同一别名上以利用厂商 prompt 缓存：

    - ``off``（默认）：关闭，每次调用按路由策略选择，行为与改造前一致；
    - ``session``：整个会话固定一个别名，直到该别名不可用才切换；
    - ``turn``：每轮可重新选择（保留追踪能力但不强制亲和）。

    仅当 ``model_failover`` 配置了备用别名池时有意义。环境变量
    ``AGENTCREW_AFFINITY_MODE``。
    """
    affinity_ttl_days: int = 7
    """亲和性记录过期天数（V7-P3）。超过此天数未使用的会话记录自动清理。"""
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
    tools_enabled: bool = False
    """R3：把内置工具链接进真实 run（默认**关闭**）。

    关闭时 ``AgentFactory`` 不注入 ``ToolRegistry``，Agent 走「单次 LLM 调用 →
    落盘」的原有路径，行为与 R3 之前逐字节一致（确定性引擎不变）。开启后 Crew
    为本次 run 装配一份注册表：4 个本地沙箱工具 + ``write_file`` / ``web_fetch``，
    Agent 进入 ReAct 循环；``tool_permissions`` 与 ``mcp_servers`` 中前者同步生效。
    环境变量 ``AGENTCREW_TOOLS_ENABLED=true``，或 YAML 顶层 ``tools_enabled: true``。
    """
    approval_mode: Literal["interactive", "deferred"] = "interactive"
    """R4：人工卡点的实现方式（默认 ``interactive``，行为与 R4 之前一致）。

    - ``interactive``：``CliPrompter`` 在 TTY 里同步问答；非 TTY 按既有规则
      降级为 ``gate=none``；HTTP API 启动的 run 仍强制 ``gate=none``。
    - ``deferred``：换用 :class:`~agentcrew.gate.deferred.DeferredPrompter`，
      卡点把审批请求写进 ``<run_dir>/approvals.jsonl`` 后让 run 以
      ``awaiting_approval`` 挂起退出；人通过 CLI / HTTP API / Studio 在**另一个
      进程**里批准或驳回，再 ``resume`` 从原卡点继续。非 TTY 不再需要降级。
    """
    entity_memory_enabled: bool = False
    """R3：跨 run 实体记忆（默认**关闭**）。

    开启后，成功经验的文本会被抽取成命名实体累积到
    ``output/knowledge/entities.jsonl``，规划阶段把相关实体作为参考块注入 PM 上下文。
    关闭时既不写实体库、也不发任何相关事件，PM 提示词与 R3 之前一致。
    环境变量 ``AGENTCREW_ENTITY_MEMORY_ENABLED=true``，或 YAML 顶层
    ``entity_memory_enabled: true``。
    """
    peer_dialogue_enabled: bool = False
    """R5：Agent 间真实咨询（``<message to="…">``）——默认**关闭**。

    关闭时不注入路由：Agent 的系统提示词里不会出现这段能力，回复里的
    ``<message>`` 围栏也不被检测，行为与 R5 之前逐字一致。开启后一次发问会真的
    触发目标角色的**一次轻量模型调用**（:meth:`BaseAgent.consult`，不带产物
    协议、不落盘、不进看板），答复以 ``<message_reply from="…">`` 回灌。
    环境变量 ``AGENTCREW_PEER_DIALOGUE_ENABLED=true``，或 YAML 同名顶层字段。
    """
    max_peer_messages: int = 8
    """R5：一次 run 内真实跨角色咨询的次数上限（默认 8；``0`` = 全禁）。

    跨角色问答是 token 膨胀的入口——一组互相求证的角色能把预算烧光。超出配额
    后提问方拿到的是一句「配额已用尽，请按已掌握信息继续」，路由不会抛错。
    """
    delegation_mode: Literal["any", "manager_only"] = "any"
    """R6：谁能发起动态委派（``<subtask>`` 围栏建任务），默认 ``any`` = 现状。

    - ``any``：任何角色都能请求拆分子任务（P2-1 以来的既有行为，逐字不变）。
    - ``manager_only``：只有经理类角色（``project_manager`` / ``product_manager``）
      派出的子任务会被接受；执行角色擅自派活记 ``subtask_unauthorized`` 并拒绝。
      对标 CrewAI ``Process.hierarchical`` 的「由经理分配工作」语义。
    """
    max_delegation_depth: int = 2
    """R6：动态委派的层数上限（默认 2：根任务 → 子任务 → 孙任务）。

    委派链没有上限时，一个任务可以无限拆下去，每层都真实花 token。按 ``parent_id``
    链计算深度，超过即拒绝并记 ``subtask_depth_exceeded``。
    """
    max_dynamic_tasks: int = 20
    """R6：一次 run 内动态子任务的总数上限（默认 20）。

    与深度上限互补：深度控制「一层能套多深」，这个控制「总共能裂变多少个」。
    超限记 ``subtask_quota_exceeded`` 并拒绝，run 本身继续。
    """
    peer_consult_capture_enabled: bool = False
    """R6：把成功的跨角色咨询沉淀进经验库（默认**关闭**）。

    开启后每次真实咨询（不含 mock 降级、不含去重命中）写成一条 ``Experience``：
    ``task_description`` 记「谁问谁问了什么」、``solution_summary`` 记答复。
    下次遇到同类问题，经验库的关键词检索能直接命中上一次的答案，省掉一次咨询。

    默认关闭有两个理由：咨询是高频低价值事件，默认全收会把经验库淹没；且
    是否值得沉淀很大程度上取决于项目类型，交给使用方按 run 决定更合适。
    依赖 ``peer_dialogue_enabled``（没开咨询就没有可沉淀的东西）。
    """
    supervisor_review_enabled: bool = False
    """R7：主管审核——动态委派任务的产出先由派活的人验收（默认**关闭**）。

    开启后，``Task.delegated_by`` 非空的动态委派任务在 G1/G2 放行之后、状态跃迁
    之前，再让派活的那个角色评审一次（``layer="supervisor"``）。主管驳回走与 G2
    完全相同的返工通道（共用 ``rework_count`` 额度），但**不受** score 阈值与
    收敛旁路保护——那两个旁路是对机器评审意见的容错，派活的人说不行就是不行。

    规划阶段的任务没有 ``delegated_by``，完全不受影响。离线 mock 通道没有
    supervisor 夹具，开启后会被明确跳过并记 ``supervisor_review_skipped``。
    """
    supervisor_scope: Literal["delegated", "all"] = "delegated"
    """R9：主管审核覆盖哪些任务（默认 ``delegated`` = 只审动态委派任务）。

    - ``delegated``：只有 ``Task.delegated_by`` 非空的任务被主管验收（R7 行为）。
    - ``all``：规划期任务回落到 ``supervisor_role`` 验收——这是 CrewAI
      ``Process.hierarchical`` 里「经理监督下属全部产出」的那一半。

    默认不放开有两个理由：规划期任务每个都要多一次真实评审调用，升级时不该静默
    涨价；且多数场景下 G2 的机器评审已经够用。主管角色本来就在 G2 评审人里时
    自动跳过（例如 ``architect`` 的任务本就由 ``project_manager`` 审），不重复烧钱。
    """
    supervisor_role: str = "project_manager"
    """R9：``supervisor_scope="all"`` 时，规划期任务的主管角色 key（默认 PM）。

    必须真实存在于角色目录；不存在时该任务跳过并记
    ``supervisor_review_skipped(unknown_supervisor)``，不静默改用别人。
    """
    role_fallback: dict[str, list[str]] = Field(default_factory=dict)
    """R10：主管驳回后可以换给谁做（role → 备选角色列表，按声明顺序取第一个可用的）。

    形状为 ``{"backend_dev": ["senior_backend", "architect"]}``，经 YAML
    ``role_fallback:`` 段加载。**空映射（默认）= 永不换人**，驳回仍退回原执行者——
    没有「谁能替谁」的知识就自动挑一个顺眼的角色，比不换更糟。

    候选必须真实存在于角色目录、不等于当前角色、也不等于主管本人（派活的人转身
    去执行会同时丢掉执行者与验收人）。
    """
    max_supervisor_reassignments: int = 1
    """R10：主管驳回触发「换人做」的次数上限（默认 1；``0`` = 不换人）。

    换人**同时消耗返工额度**：同一份额度换一只手试，不是免费加次数。所以额度用尽
    照样 FAILED，不存在「换了人就能无限重试」。
    """
    max_subtask_corrections: int = 1
    """R8：子任务被拒时把原因回灌给模型、要一次修正后回复的次数上限（默认 1）。

    ``0`` = 不回灌（行为与 R8 之前逐字一致）。只有回调对象提供了
    ``last_rejection``（即 Crew 装配的 ``SubtaskDispatcher``）才可能触发——
    裸闭包用法永远不回灌，不会凭空多出一次模型调用。

    为什么要有上限：每次回灌都是一次真实调用。给一次改的机会足够——模型要么
    换对角色名，要么接受「自己完成」，继续追问只会把预算花在同一件事上。
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
            # 别名按「第一个冒号」切成 provider:model（见 parse_alias），因此
            # provider 名一旦自带冒号就永远无法被任何别名寻址
            # （"my:relay:m" 会被切成 provider="my"）。空名同理。配置时就拦下，
            # 好过运行时才报「未知 provider」。
            if not name.strip():
                raise ValueError("custom_providers 的 provider 名不能为空")
            if ":" in name:
                raise ValueError(
                    f"custom_providers.{name!r} 的名字含冒号："
                    "别名按第一个冒号切分 provider:model，名字带冒号将永远无法被引用")
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
