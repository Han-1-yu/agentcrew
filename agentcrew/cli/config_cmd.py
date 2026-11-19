"""config / config export / config import（R66f 自 main.py 拆出）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from agentcrew.cli.common import _fail, _settings_from
from agentcrew.config import Settings
from agentcrew.crew.crew import (
    EXIT_CODES,
)


def _cmd_config(args: argparse.Namespace) -> int:
    """``config export`` / ``config import <file>``：配置导入导出（A6）。"""
    if getattr(args, "config_action", None) == "export":
        return _cmd_config_export(args)
    if getattr(args, "config_action", None) == "import":
        return _cmd_config_import(args)
    print("用法：python main.py config export|import ...", file=sys.stderr)
    return EXIT_CODES["config"]


def _cmd_config_export(args: argparse.Namespace) -> int:
    """导出当前合并配置为 YAML（密钥字段已脱敏）。"""
    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    snap = settings.config_snapshot()
    # 只导出可再导入的段，不导出 output_dir / log_level 等运行环境字段。
    export_doc: dict[str, Any] = {
        "config_version": 1,
        "models": {
            "default": snap.get("default_model"),
            "role_bindings": snap.get("role_bindings") or {},
            "providers": snap.get("provider_overrides") or {},
            "custom_providers": snap.get("custom_providers") or {},
        },
        "mcp_servers": snap.get("mcp_servers") or {},
        "gate_config": snap.get("gate_config") or {},
        "tool_permissions": snap.get("tool_permissions") or {},
        # P2-4：guardrails 属于安全配置，导出/导入必须带上——否则
        # `config export` 会静默丢掉整套拦截规则，是最不该出现的「丢配置」。
        "tool_guards": snap.get("tool_guards") or [],
        # R10：换人映射只在非空时导出（默认空映射不写回 yaml）。
        "role_fallback": snap.get("role_fallback") or {},
    }
    # R3：两个能力开关只在显式开启时导出 —— 默认配置的导出文档与改造前一致，
    # 也不会把「关」当成用户配置写回 agentcrew.yaml。
    for key in ("tools_enabled", "entity_memory_enabled", "peer_dialogue_enabled",
                "peer_consult_capture_enabled", "supervisor_review_enabled"):
        if snap.get(key):
            export_doc[key] = True
    if snap.get("max_peer_messages") not in (None, 8):
        export_doc["max_peer_messages"] = int(snap["max_peer_messages"])
    # R6：委派治理只在偏离默认值时导出，默认配置的导出文档保持不变。
    if snap.get("delegation_mode") and snap["delegation_mode"] != "any":
        export_doc["delegation_mode"] = snap["delegation_mode"]
    for key, default in (("max_delegation_depth", 2), ("max_dynamic_tasks", 20),
                         ("max_subtask_corrections", 1),
                         ("max_supervisor_reassignments", 1)):
        if snap.get(key) not in (None, default):
            export_doc[key] = int(snap[key])
    # R9：主管审核的覆盖范围与主管角色，同样只在偏离默认值时导出。
    if snap.get("supervisor_scope") and snap["supervisor_scope"] != "delegated":
        export_doc["supervisor_scope"] = snap["supervisor_scope"]
    if (snap.get("supervisor_role")
            and snap["supervisor_role"] != "project_manager"):
        export_doc["supervisor_role"] = snap["supervisor_role"]
    # R14：多接收端优先——配了它就不再导出单接收端那组，避免两者同时出现让人
    # 以为会推两遍。**每条 target 的 secret 一律剔除**，同 webhook_secret 的处理。
    if snap.get("webhooks"):
        export_doc["webhooks"] = [
            {key: value for key, value in target.items() if key != "secret"}
            for target in snap["webhooks"]
        ]
    # R12：Webhook 只在配了 URL 时导出整组；密钥**永不导出**（它会随文件流传）。
    elif snap.get("webhook_url"):
        export_doc["webhook_url"] = snap["webhook_url"]
        export_doc["webhook_events"] = list(snap.get("webhook_events") or [])
        if snap.get("webhook_timeout_seconds") not in (None, 5.0):
            export_doc["webhook_timeout_seconds"] = float(
                snap["webhook_timeout_seconds"])
        if snap.get("webhook_max_retries") not in (None, 2):
            export_doc["webhook_max_retries"] = int(snap["webhook_max_retries"])
        # C4：过滤表达式只在非默认时导出（默认导出文档保持不变）。
        if snap.get("webhook_filter"):
            export_doc["webhook_filter"] = str(snap["webhook_filter"])
    # R18：限流三参数补齐配置通道，只在偏离默认时导出（默认导出文档保持不变）。
    if snap.get("api_rate_limit_enabled"):
        export_doc["api_rate_limit_enabled"] = True
    if snap.get("api_rate_limit_max") not in (None, 100):
        export_doc["api_rate_limit_max"] = int(snap["api_rate_limit_max"])
    if snap.get("api_rate_limit_window") not in (None, 60):
        export_doc["api_rate_limit_window"] = int(snap["api_rate_limit_window"])
    if snap.get("approval_mode") and snap["approval_mode"] != "interactive":
        export_doc["approval_mode"] = snap["approval_mode"]
    # R20：单步断点只在开启时导出（默认导出文档保持不变）。
    if snap.get("debug_step"):
        export_doc["debug_step"] = True
    # R51：C1/C2 的执行与搜索配置同样只在偏离默认值时导出——否则 `config export`
    # 会静默丢掉「代码在哪跑」这类关键安全设置（同 R18 / P2-4 的教训）。
    export_defaults: tuple[tuple[str, object], ...] = (
        ("sandbox_backend", "local"),
        ("sandbox_image", "python:3.12-slim"),
        ("sandbox_memory_mb", 512),
        ("sandbox_cpus", 1.0),
        ("sandbox_allow_network", False),
        ("search_provider", ""),
        ("search_max_results", 5),
        ("search_timeout_seconds", 15.0),
        ("docs_rag_enabled", False),
        ("docs_rag_limit", 4),
        ("docs_rag_vector_weight", 0.5),
        # C6：真语义 embedding 端点（默认导出文档不变——默认值与字段逐字一致）。
        ("embedding_enabled", False),
        ("embedding_model", ""),
        ("embedding_dim", 0),
        ("embedding_base_url", ""),
        ("embedding_timeout_seconds", 10.0),
        ("embedding_batch_size", 32),
    )
    for field, fallback in export_defaults:
        value = snap.get(field)
        if value not in (None, fallback):
            export_doc[field] = value
    # 去掉空段
    export_doc["models"] = {
        k: v for k, v in export_doc["models"].items() if v
    }
    for key in ("mcp_servers", "gate_config", "tool_permissions",
                "tool_guards", "role_fallback"):
        if not export_doc[key]:
            del export_doc[key]
    text = yaml.safe_dump(export_doc, allow_unicode=True, sort_keys=False)
    if getattr(args, "output", None):
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"已导出到 {args.output}")
    else:
        print(text, end="")
    return EXIT_CODES["ok"]


def _cmd_config_import(args: argparse.Namespace) -> int:
    """从 YAML 文件导入配置：校验 → 备份 → 合并写入 agentcrew.yaml。"""
    import re

    src = Path(args.file)
    if not src.is_file():
        return _fail(f"导入文件不存在：{src}")
    try:
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return _fail(f"无法读取导入文件：{exc}")
    if not isinstance(data, dict):
        return _fail("导入文件必须是 YAML 映射")

    # 密钥字段作为值出现 → 拒绝（密钥只从环境变量读）。
    secret_re = re.compile(
        r"key|token|secret|password|passwd|credential|auth", re.IGNORECASE)
    # 环境变量名约定：字母/下划线开头，后跟字母/数字/下划线。用于识别
    # 「把真实密钥误填进 api_key_env」——那样会被原样写进 agentcrew.yaml
    # （明文落盘红线），且永不生效（os.environ.get(<密钥>) 必为空）。
    env_name_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def _check_secrets(obj: Any, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k) == "api_key_env":
                    # api_key_env 存的是环境变量「名」。空串合法（免钥 provider）；
                    # 非空则必须长得像环境变量名，否则判定为用户把密钥值误填到了
                    # 这里——拒绝并说明，绝不回显这个疑似密钥的值。
                    if (isinstance(v, str) and v.strip()
                            and env_name_re.match(v) is None):
                        raise ValueError(
                            f"导入文件 {path}.{k} 的值不像环境变量名："
                            "api_key_env 应填环境变量名（如 MYRELAY_KEY），"
                            "不要直接粘贴密钥值；密钥请放进环境变量，绝不写进配置文件。")
                    continue
                if secret_re.search(str(k)) and not isinstance(v, (dict, list)):
                    raise ValueError(
                        f"导入文件 {path}.{k} 疑似密钥字段："
                        "API Key 只从环境变量读取，请改用 api_key_env 指定环境变量名。")
                _check_secrets(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _check_secrets(item, f"{path}[{i}]")

    try:
        _check_secrets(data)
    except ValueError as exc:
        return _fail(str(exc))

    # 校验：用 Settings.load 验证（临时设 AGENTCREW_CONFIG 指向导入文件）。
    import os
    tmp_env = os.environ.get("AGENTCREW_CONFIG")
    os.environ["AGENTCREW_CONFIG"] = str(src)
    try:
        Settings.load()
    except (OSError, ValueError) as exc:
        return _fail(f"导入文件校验失败：{exc}")
    finally:
        if tmp_env is not None:
            os.environ["AGENTCREW_CONFIG"] = tmp_env
        else:
            os.environ.pop("AGENTCREW_CONFIG", None)

    # 合并写入现有 agentcrew.yaml（不是整体覆盖）。
    target = Path(os.environ.get("AGENTCREW_CONFIG") or "agentcrew.yaml")
    if not target.is_absolute():
        target = Path.cwd() / target
    existing: dict = {}
    if target.is_file():
        try:
            existing = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            existing = {}
    # 备份
    bak = target.with_suffix(".yaml.bak")
    if target.is_file():
        bak.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    # 合并 models 段
    existing_models = existing.get("models") or {}
    incoming_models = data.get("models") or {}
    for key in ("default", "role_bindings", "providers", "custom_providers"):
        if key in incoming_models:
            existing_models[key] = incoming_models[key]
    existing["models"] = existing_models
    # 合并顶层段
    for key in (
        "mcp_servers", "gate_config", "tool_permissions", "tool_guards",
        "tools_enabled", "entity_memory_enabled",  # R3 顶层能力开关
        "approval_mode",                          # R4 人工审批模式
        "peer_dialogue_enabled", "max_peer_messages",  # R5 跨角色咨询
        "peer_consult_capture_enabled",               # R6 咨询沉淀
        "delegation_mode", "max_delegation_depth", "max_dynamic_tasks",  # R6 委派治理
        "supervisor_review_enabled",                  # R7 主管审核
        "max_subtask_corrections",                    # R8 拒绝原因回灌
        "supervisor_scope", "supervisor_role",        # R9 跨任务层级
        "role_fallback", "max_supervisor_reassignments",  # R10 自动重派
        "webhook_url", "webhook_secret", "webhook_events",  # R12 出站 Webhook
        "webhook_timeout_seconds", "webhook_max_retries",
        "webhook_filter",                             # C4 事件级过滤
        "webhooks",                                   # R14 多接收端
        "api_rate_limit_enabled", "api_rate_limit_max",  # R18 限流配置通道
        "api_rate_limit_window",
        "debug_step",                                 # R20 单步断点
        "sandbox_backend", "sandbox_image",           # R51 C2 执行后端
        "sandbox_memory_mb", "sandbox_cpus", "sandbox_allow_network",
        "search_provider", "search_max_results",      # R51 C1 联网搜索
        "search_timeout_seconds",
        "docs_rag_enabled", "docs_rag_limit",         # R54 C5 文档 RAG
        "docs_rag_vector_weight",
        "embedding_enabled", "embedding_model",       # C6 真语义 embedding
        "embedding_dim", "embedding_base_url",
        "embedding_timeout_seconds", "embedding_batch_size",
    ):
        if key in data:
            existing[key] = data[key]
    if "config_version" in data:
        existing["config_version"] = data["config_version"]
    target.write_text(
        yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    print(f"已导入 {src} → {target}（备份在 {bak}）")
    return EXIT_CODES["ok"]

