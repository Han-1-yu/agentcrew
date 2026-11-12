"""``POST /runs`` 的 ``options`` 白名单 + 类型/范围校验（P1-11 单一事实源）。

``Settings.model_copy(update=...)`` 既不校验也不过滤：任意键都会被塞进
``Settings.__dict__``，``{"mock": "false"}`` 里的字符串还会被当成 truthy 的
开关（客户端想关 mock、实际却强制开了）。此前唯一的兜底是 ``except
Exception: settings = self._settings`` —— 失败被静默吞掉、请求照样回 202，
用户以为 options 生效了。所以校验必须在入口做，而且**只能有一份**：

- HTTP 入口（``api/server.py``）直接调用 :func:`validate_run_options`；
- SDK / CLI / Webhook 等调用方复用同一份 :data:`ALLOWED_RUN_OPTIONS`，
  不各自维护一份会漂移的键表；
- 进程内库调用方（不经 HTTP）用 :func:`apply_run_options` 得到
  ``(Settings, error)``，与 HTTP 端逐字一致地拒绝非法键。

白名单按「可安全按 run 覆盖且不会绕过服务端边界」取最小集合；``gate`` 恒被
Crew 覆写为 none，``output_dir`` / ``api_keys`` 等属于服务端配置，均不开放。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

__all__ = ["ALLOWED_RUN_OPTIONS", "apply_run_options", "validate_run_options"]

#: 允许按 run 覆盖的键。新增能力时在这里加一行，校验分支里补类型检查——
#: 只加键不加检查等于默认放行任意值。
ALLOWED_RUN_OPTIONS: frozenset[str] = frozenset({
    "mock", "default_model", "max_concurrency", "context_budget_tokens",
    "max_cost_usd", "max_llm_calls", "model_tiers", "gate_config",
    # R3：两个默认关闭的能力开关，允许按 run 显式打开。
    "tools_enabled", "entity_memory_enabled",
    # R4：允许按 run 打开「审批单挂起」模式。
    "approval_mode",
    # R5：允许按 run 打开跨角色咨询并收紧配额。
    "peer_dialogue_enabled", "max_peer_messages",
    # R6：咨询沉淀与动态委派治理。
    "peer_consult_capture_enabled", "delegation_mode",
    "max_delegation_depth", "max_dynamic_tasks",
    # R7：主管审核；R8：委派拒绝原因回灌次数；R9：覆盖范围与主管角色。
    "supervisor_review_enabled", "max_subtask_corrections",
    "supervisor_scope", "supervisor_role", "supervisor_chain",
    # R10：主管驳回后换人做。
    "role_fallback", "max_supervisor_reassignments",
    # R12：出站 Webhook 的**行为参数**（密钥与 URL 均不经 HTTP 接口传，只认配置
    # 文件/环境变量）。``webhook_url`` 刻意不在白名单里：接收端地址属于服务端
    # 配置——webhook 的内网目标豁免（见 ``config._validate_webhook_target_url``）
    # 前提是「运维显式配置」，开放给任意 ``runs:write`` key 等于把这个前提变成
    # SSRF（且 ``urlopen`` 不跟随重定向，见 ``webhook._post``）。
    "webhook_events", "webhook_timeout_seconds",
    "webhook_max_retries",
    # R28：流程直通——flow 是内联流程对象，flow_name 指向流程库里的名字。
    "flow", "flow_name",
})

_BOOL_KEYS = frozenset({
    "mock", "tools_enabled", "entity_memory_enabled",
    "peer_dialogue_enabled", "peer_consult_capture_enabled",
    "supervisor_review_enabled",
})

#: 允许为 ``null``（= 显式清空）的键；其余键给 ``null`` 视为非法输入。
_NULLABLE_KEYS = frozenset({"default_model", "max_cost_usd", "max_llm_calls"})

_NON_NEGATIVE_INT_KEYS = frozenset({
    "max_delegation_depth", "max_dynamic_tasks", "max_subtask_corrections",
    "max_supervisor_reassignments", "max_peer_messages",
})

#: B-7：每个 run 一个投递线程，``close()`` 只 ``join(5s)`` 后放手——上界防止
#: options 把后台线程与连接数放大成资源攻击面。
_MAX_WEBHOOK_RETRIES = 5
_MAX_WEBHOOK_TIMEOUT_SECONDS = 30.0


def validate_run_options(options: Mapping[str, Any]) -> str | None:
    """校验一个 options 映射；合法返回 ``None``，非法返回可直接回给调用方的文案。"""
    unknown = sorted(set(options) - ALLOWED_RUN_OPTIONS)
    if unknown:
        return ("options 含不支持的键：" + "、".join(unknown)
                + "；可用：" + "、".join(sorted(ALLOWED_RUN_OPTIONS)))
    if "flow" in options and "flow_name" in options:
        return "options.flow 与 options.flow_name 只能给一个"
    if "flow" in options and not isinstance(options["flow"], Mapping):
        return "options.flow 必须是对象（{version, name, tasks}）"
    if "flow_name" in options and not isinstance(options["flow_name"], str):
        return "options.flow_name 必须是字符串"
    for key, value in options.items():
        if value is None:
            if key not in _NULLABLE_KEYS:
                return f"options.{key} 不能为 null"
            continue
        if key in _BOOL_KEYS:
            if not isinstance(value, bool):
                return f"options.{key} 必须是布尔值"
        elif key == "delegation_mode":
            if value not in ("any", "manager_only"):
                return "options.delegation_mode 必须是 any 或 manager_only"
        elif key == "supervisor_scope":
            if value not in ("delegated", "all"):
                return "options.supervisor_scope 必须是 delegated 或 all"
        elif key == "supervisor_role":
            if not isinstance(value, str) or not value.strip():
                return "options.supervisor_role 必须是非空字符串"
        elif key == "supervisor_chain":
            if isinstance(value, str) or not isinstance(value, list):
                return "options.supervisor_chain 必须是角色 key 数组"
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    return "options.supervisor_chain 的每一项都必须是非空字符串"
        elif key == "role_fallback":
            if not isinstance(value, dict):
                return "options.role_fallback 必须是对象（角色 → 备选角色数组）"
            for role, candidates in value.items():
                if isinstance(candidates, str) or not isinstance(candidates, list):
                    return f"options.role_fallback.{role} 必须是角色 key 数组"
        elif key in _NON_NEGATIVE_INT_KEYS:
            if isinstance(value, bool) or not isinstance(value, int):
                return f"options.{key} 必须是整数"
            if value < 0:
                return f"options.{key} 必须 >= 0"
        elif key == "approval_mode":
            if value not in ("interactive", "deferred"):
                return "options.approval_mode 必须是 interactive 或 deferred"
        elif key == "webhook_max_retries":
            if isinstance(value, bool) or not isinstance(value, int):
                return "options.webhook_max_retries 必须是整数"
            if value < 0:
                return "options.webhook_max_retries 必须 >= 0"
            if value > _MAX_WEBHOOK_RETRIES:
                return f"options.webhook_max_retries 必须 <= {_MAX_WEBHOOK_RETRIES}"
        elif key == "webhook_events":
            if isinstance(value, str) or not isinstance(value, list):
                return "options.webhook_events 必须是事件名数组"
            if not value:
                return "options.webhook_events 不能为空数组（要全推请用 [\"*\"]）"
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    return "options.webhook_events 的每一项都必须是非空字符串"
        elif key == "default_model":
            if not isinstance(value, str) or not value.strip():
                return "options.default_model 必须是非空字符串"
        elif key in ("max_concurrency", "context_budget_tokens", "max_llm_calls"):
            if isinstance(value, bool) or not isinstance(value, int):
                return f"options.{key} 必须是整数"
            lower = 0 if key == "max_llm_calls" else 1
            if value < lower:
                return f"options.{key} 必须 >= {lower}"
        elif key == "webhook_timeout_seconds":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "options.webhook_timeout_seconds 必须是数字"
            if value <= 0:
                return "options.webhook_timeout_seconds 必须 > 0"
            if value > _MAX_WEBHOOK_TIMEOUT_SECONDS:
                return (f"options.webhook_timeout_seconds 必须 "
                        f"<= {_MAX_WEBHOOK_TIMEOUT_SECONDS:g}")
        elif key == "max_cost_usd":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "options.max_cost_usd 必须是数值"
            if not math.isfinite(value) or value < 0:
                return "options.max_cost_usd 必须是 >= 0 的有限数"
        elif key == "flow":
            if not isinstance(value, dict):
                return "options.flow 必须是对象（{version, name, tasks}）"
        elif key == "flow_name":
            if not isinstance(value, str) or not value.strip():
                return "options.flow_name 必须是非空字符串"
        elif not isinstance(value, dict):
            return f"options.{key} 必须是对象"
    return None


def apply_run_options(settings: Any, options: Mapping[str, Any]) -> tuple[Any, str | None]:
    """进程内库调用方：校验 + 应用到 Settings，返回 ``(新 Settings, 错误文案)``。

    与 HTTP 端同一套规则、同一份错误文案；不合法时**不改动**原 Settings，调用
    方按错误处理（HTTP 回 400，CLI 退 3），不再有「静默吞掉、照样开跑」的路径。
    """
    error = validate_run_options(options)
    if error is not None:
        return settings, error
    try:
        return settings.model_copy(update=dict(options)), None
    except (TypeError, ValueError) as exc:
        return settings, f"options 无法应用：{exc}"
