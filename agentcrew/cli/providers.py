"""providers / models 目录命令（R66f 自 main.py 拆出）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from rich.console import Console

from agentcrew.cli.common import _fail, _out, _settings_from
from agentcrew.crew.crew import (
    EXIT_CODES,
)
from agentcrew.models.config import PROVIDER_PRESETS as PROVIDER_PRESETS_NAMES


def _cmd_providers(args: argparse.Namespace) -> int:
    """``providers [name]``：列出合并后的 provider 目录（内置 + 自定义）。

    显示每个 provider 的 kind（builtin/custom）、implementation、base_url、
    api_key_env（环境变量名，不显示值）、能力位摘要、建议模型、category、
    api_key_url。``--json`` 输出结构化 JSON；``name`` 参数只显示单个 provider。
    ``--category`` / ``--search`` 可组合（AND）过滤。
    """
    import os

    from agentcrew.models.config import build_directory

    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")

    directory = build_directory(
        custom=settings.custom_providers,
        overrides=settings.provider_overrides,
    )
    builtin_names = set(PROVIDER_PRESETS_NAMES)

    def _entry(name: str, preset) -> dict[str, Any]:
        env_name = preset.api_key_env or ""
        key_configured = bool(env_name) and bool(os.environ.get(env_name, "").strip())
        return {
            "name": name,
            "kind": "builtin" if name in builtin_names else "custom",
            "implementation": preset.implementation,
            "base_url": preset.base_url,
            "api_key_env": env_name,
            "key_configured": key_configured,
            "capabilities": preset.capabilities.model_dump(),
            "suggested_models": list(preset.suggested_models),
            "command": preset.command,
            "category": preset.category,
            "family": preset.family,
            "search_aliases": list(preset.search_aliases),
            "endpoint_candidates": list(preset.endpoint_candidates) if preset.endpoint_candidates else None,
            "api_key_url": preset.api_key_url,
        }

    if args.name:
        if args.name not in directory:
            return _fail(f"未知 provider：{args.name!r}")
        entries = [_entry(args.name, directory[args.name])]
    else:
        entries = [_entry(n, directory[n]) for n in sorted(directory)]

    # ── 过滤：--category 与 --search 可组合（AND）─────────────────────────
    category = getattr(args, "category", None)
    keyword = getattr(args, "search", None)
    if category or keyword:
        kw = keyword.lower().strip() if keyword else ""
        filtered: list[dict[str, Any]] = []
        for e in entries:
            if category and e["category"] != category:
                continue
            if kw:
                haystack = [e["name"].lower()] + [a.lower() for a in e["search_aliases"]]
                if not any(kw in h for h in haystack):
                    continue
            filtered.append(e)
        entries = filtered

    if args.json:
        print(json.dumps(
            entries if args.name else {"providers": entries},
            ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]

    console = Console()
    if not entries:
        print("没有匹配的 provider（试试放宽 --category / --search 条件）。",
              flush=True)
        return EXIT_CODES["ok"]
    for e in entries:
        cat_tag = f"  [{e['category']}]" if e["category"] else ""
        _out(console, f"[{e['kind']}] {e['name']}  ({e['implementation']}){cat_tag}")
        _out(console, f"  base_url       {e['base_url']}")
        _out(console, f"  api_key_env    {e['api_key_env'] or '(无)'}"
                       + ("  [已配置]" if e["key_configured"] else "  [未配置]"))
        caps = e["capabilities"]
        caps_summary = (
            f"json_schema={caps['supports_json_schema']} "
            f"json_object={caps['supports_json_object']} "
            f"tools={caps['supports_tool_calls']}")
        _out(console, f"  capabilities   {caps_summary}")
        if e["suggested_models"]:
            _out(console, f"  suggested      {', '.join(e['suggested_models'])}")
        if e["endpoint_candidates"]:
            n = len(e["endpoint_candidates"])
            _out(console, f"  endpoints      {n} 端点候选")
        if e["api_key_url"]:
            _out(console, f"  api_key_url    {e['api_key_url']}")
        _out(console, "")
    return EXIT_CODES["ok"]


def _cmd_models(args: argparse.Namespace) -> int:
    """``models <provider>``：按需拉取该 provider 的 /models 端点。

    错误分类清晰（network/auth/not_found/parse/timeout），不栈崩溃。
    ``--verbose`` 显示端点候选尝试过程；``--json`` 附带 endpoints_tried /
    successful_endpoint 元数据。
    """
    from agentcrew.models.config import build_directory, parse_alias
    from agentcrew.models.discovery import (
        ModelDiscoveryError,
        candidate_urls,
        fetch_models,
    )

    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")

    directory = build_directory(
        custom=settings.custom_providers,
        overrides=settings.provider_overrides,
    )
    preset = directory.get(args.provider)
    if preset is None:
        return _fail(f"未知 provider：{args.provider!r}")

    # 构造一个临时 binding：model 名用 suggested_models 第一个或 "test"。
    dummy_model = preset.suggested_models[0] if preset.suggested_models else "test"
    binding = parse_alias(
        f"{args.provider}:{dummy_model}",
        overrides=settings.provider_overrides,
        custom=settings.custom_providers,
    )
    api_key = settings.api_key_for(args.provider) or ""

    # 预计算端点候选列表（用于展示 / JSON 元数据）。
    endpoints_tried = candidate_urls(binding)
    verbose = getattr(args, "verbose", False)

    try:
        models = asyncio.run(fetch_models(binding, api_key=api_key))
    except ModelDiscoveryError as exc:
        payload: dict[str, Any] = {"provider": args.provider, "error": {
            "kind": exc.kind, "message": str(exc)}}
        if endpoints_tried:
            payload["endpoints_tried"] = endpoints_tried
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"[{exc.kind}] {args.provider} 模型发现失败：{exc}",
                file=sys.stderr)
        return EXIT_CODES["config"]

    # fetch_models 按候选顺序尝试、第一个成功即返回；主端点即首条候选。
    successful_endpoint = endpoints_tried[0] if endpoints_tried else None

    if args.json:
        print(json.dumps(
            {"provider": args.provider,
             "models": models,
             "endpoints_tried": endpoints_tried,
             "successful_endpoint": successful_endpoint},
            ensure_ascii=False, indent=2))
    else:
        console = Console()
        if verbose and endpoints_tried:
            _out(console, f"provider {args.provider}：{len(endpoints_tried)} 个端点候选")
            for i, url in enumerate(endpoints_tried, 1):
                _out(console, f"  [{i}] {url}")
            _out(console, f"  → 成功端点：{successful_endpoint}")
        _out(console, f"provider {args.provider}：{len(models)} 个模型")
        for m in models:
            _out(console, f"  {m}")
    return EXIT_CODES["ok"]

