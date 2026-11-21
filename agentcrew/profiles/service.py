"""执行体档案服务层：读取视图、诊断、用量与写操作。

CLI 只负责参数与输出，真正的规则都在这里，保证 ``agents --json``、
``agents --check``、``agents --save`` 与 Studio 看到的是同一套判定。
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from agentcrew.config import Settings
from agentcrew.profiles.store import (
    ProfileStoreError,
    delete_profile_file,
    effective_profiles_dir,
    parse_profile_document,
    profile_sources,
    save_profile_file,
    scan_profiles_dir,
)
from agentcrew.roles.binding import (
    AgentProfile,
    BindingError,
    resolve_binding,
    validate_bindings,
)
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.utils.files import atomic_write_bytes


def config_path(settings: Settings | None = None) -> Path:
    """当前生效的配置文件路径。

    优先用 ``Settings`` 记下的来源（支持显式 ``config_path`` 与评测变体）；
    没有 Settings 时回落到与 ``Settings.load`` 相同的环境变量 / 默认路径。
    """
    recorded = getattr(settings, "config_path", None)
    if recorded:
        return Path(recorded)
    return Path(os.environ.get("AGENTCREW_CONFIG") or "agentcrew.yaml")


def _yaml_profile_keys(settings: Settings | None = None) -> set[str]:
    """``agentcrew.yaml`` 里声明的档案 key（用于标「被 agents.d 覆盖」）。"""
    path = config_path(settings)
    if not path.is_file():
        return set()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    agents = document.get("agents") if isinstance(document, dict) else None
    profiles = agents.get("profiles") if isinstance(agents, dict) else None
    return set(profiles) if isinstance(profiles, dict) else set()


def _bound_roles(settings: Settings, catalog: RoleCatalog) -> dict[str, list[str]]:
    """profile key → 绑定它的角色（含 review / judge 等非 primary 职责）。"""
    used: dict[str, list[str]] = defaultdict(list)
    for spec in catalog.all():
        for bound in resolve_binding(spec, settings.role_agents).agents:
            used[bound.ref].append(spec.key)
    return {key: sorted(set(roles)) for key, roles in used.items()}


def storage_info(settings: Settings) -> dict[str, Any]:
    """档案目录与配置文件位置：UI 用它告诉用户「保存到哪里」。"""
    directory = settings.profiles_dir or effective_profiles_dir(
        config_path=config_path(settings))
    path = Path(directory)
    return {
        "profiles_dir": str(path),
        "profiles_dir_exists": path.is_dir(),
        "config_path": str(config_path(settings).resolve()),
        "policy": "agents.d 覆盖 agentcrew.yaml 的同名档案",
    }


def profile_payload(settings: Settings, catalog: RoleCatalog) -> dict[str, Any]:
    """``agents --json`` 的主体：档案（含来源）+ 绑定 + 目录信息 + 摘要。"""
    sources = profile_sources(Path(settings.profiles_dir)) if settings.profiles_dir else {}
    yaml_keys = _yaml_profile_keys(settings)
    used_by = _bound_roles(settings, catalog)

    profiles: list[dict[str, Any]] = []
    for profile in sorted(settings.agent_profiles.values(), key=lambda p: p.key):
        item = profile.model_dump(mode="json")
        file = sources.get(profile.key)
        item["source"] = "agents.d" if file is not None else "yaml"
        item["file"] = str(file) if file is not None else None
        item["used_by"] = used_by.get(profile.key, [])
        item["shadowed"] = file is not None and profile.key in yaml_keys
        profiles.append(item)

    roles: list[dict[str, Any]] = []
    for spec in catalog.all():
        binding = resolve_binding(spec, settings.role_agents)
        roles.append({
            "role": spec.key,
            "title": spec.title,
            "model": spec.model,
            "allowed_tools": list(spec.allowed_tools),
            "agents": [{"ref": b.ref, "duty": b.duty.value, "weight": b.weight}
                       for b in binding.agents],
            "aggregation": (binding.aggregation.model_dump(mode="json")
                            if binding.aggregation else None),
        })

    bound = sum(1 for item in profiles if item["used_by"])
    return {
        "profiles": profiles,
        "roles": roles,
        "storage": storage_info(settings),
        "summary": {
            "profiles": len(profiles),
            "bound": bound,
            "unbound": len(profiles) - bound,
            "shadowed": sum(1 for item in profiles if item["shadowed"]),
        },
    }


# ── 诊断 ────────────────────────────────────────────────────────────────


def _issue(
    level: str, code: str, message: str, *,
    profile: str | None = None, role: str | None = None,
    hint: str = "",
) -> dict[str, Any]:
    return {
        "level": level, "code": code, "message": message,
        "profile": profile, "role": role, "hint": hint,
    }


def _known_tools(settings: Settings | None) -> set[str]:
    code_exec = bool(getattr(settings, "code_exec_enabled", False))
    provider = str(getattr(settings, "search_provider", "") or "")
    try:
        from agentcrew.tools.lab import describe_tools
        return {str(item["name"]) for item in describe_tools(
            code_exec=code_exec, search_provider=provider)}
    except (ImportError, OSError, ValueError):
        # 工具目录只是诊断的加分项：拿不到就不报「未知工具」，绝不因此
        # 让整页诊断失败。
        return set()


def diagnose(
    settings: Settings | None,
    catalog: RoleCatalog | None,
    *,
    load_error: str = "",
    directory: Path | None = None,
) -> dict[str, Any]:
    """配置诊断：返回 ``{errors, warnings, infos, summary}``。

    与「启动即失败」的分工：run 仍然 fail-fast；这里把同一个结论拆成
    可逐条修复的条目，让 UI 能指出是哪份档案、哪个角色、该怎么改。
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    infos: list[dict[str, Any]] = []

    # 目录级错误会在下面按文件逐条展开；再放一条汇总会把同一个问题
    # 说两遍，反而让人以为有两处要修。
    if load_error and "执行体档案目录存在无法加载的文件" not in load_error:
        errors.append(_issue(
            "error", "config.invalid", load_error,
            hint="修好配置后重新加载；Studio 的档案编辑不会写入非法内容"))

    entries: dict[str, Any] = {}
    if settings is not None:
        # 配置加载已把坏文件收敛成 profile_errors；这里只做展示，不再扫一遍
        # （否则同一个问题会出现两次）。
        for failure in settings.profile_errors:
            file = Path(str(failure.get("file") or ""))
            errors.append(_issue(
                "error", "store.invalid_file",
                f"{file.name or '档案文件'}：{failure.get('message') or '加载失败'}",
                profile=failure.get("key"),
                hint=f"直接编辑 {file}，或用 Studio 覆盖保存"))
        if settings.profiles_dir is not None:
            entries = profile_sources(Path(settings.profiles_dir))
    elif directory is not None:
        entries, issues = scan_profiles_dir(directory)
        errors.extend(
            _issue(
                "error", "store.invalid_file",
                f"{issue.file.name}：{issue.message}",
                profile=issue.key,
                hint=f"直接编辑 {issue.file}，或用 Studio 覆盖保存")
            for issue in issues)

    if settings is None:
        return _diagnostic_result(errors, warnings, infos)

    yaml_keys = _yaml_profile_keys(settings)
    warnings.extend(
        _issue(
            "warning", "profile.shadowed",
            f"档案 {key} 同时存在于 agents.d 与 agentcrew.yaml：当前生效的是 agents.d 版本",
            profile=key,
            hint="删除 agents.d 中的文件可回退到 agentcrew.yaml 的定义")
        for key in sorted(set(entries) & yaml_keys))

    if catalog is not None:
        for spec in catalog.all():
            binding = resolve_binding(spec, settings.role_agents)
            try:
                validate_bindings(
                    spec.key, binding, settings.agent_profiles, spec.allowed_tools)
            except BindingError as exc:
                errors.append(_issue(
                    "error", "binding.invalid", str(exc), role=spec.key,
                    hint="在「角色 → 执行体绑定」里修正，或调整档案的工具/路径域"))

    tools = _known_tools(settings)
    used_by = _bound_roles(settings, catalog) if catalog is not None else {}
    from agentcrew.models.config import parse_alias
    for key, profile in sorted(settings.agent_profiles.items()):
        if not profile.description.strip() and not profile.title.strip():
            warnings.append(_issue(
                "warning", "profile.no_description",
                f"档案 {key} 没有 title / description：规划菜单只能显示 key",
                profile=key,
                hint="补一句话说明「擅长什么」，PM 才能按任务类型挑人"))
        if not used_by.get(key):
            infos.append(_issue(
                "info", "profile.unbound", f"档案 {key} 尚未绑定任何角色",
                profile=key, hint="绑定后才会参与编排；未绑定不影响现有 run"))
        unknown = sorted(set(profile.tools or []) - tools - {"*"})
        if tools and unknown:
            warnings.append(_issue(
                "warning", "profile.unknown_tool",
                f"档案 {key} 引用了工具目录里没有的工具：{'、'.join(unknown)}",
                profile=key,
                hint="MCP / 自定义工具可忽略；拼错的工具名会让该档案实际拿不到工具"))
        if (profile.cost_ceiling_usd is not None
                and profile.run_cost_ceiling_usd is not None
                and profile.cost_ceiling_usd > profile.run_cost_ceiling_usd):
            warnings.append(_issue(
                "warning", "profile.budget_conflict",
                f"档案 {key} 的单任务上限 ${profile.cost_ceiling_usd:g} "
                f"高于整场上限 ${profile.run_cost_ceiling_usd:g}",
                profile=key,
                hint="单任务上限不会超过整场剩余预算；两个值保持前者 ≤ 后者更直观"))
        for alias in [profile.model, *profile.fallback]:
            if not alias:
                continue
            try:
                parse_alias(
                    alias,
                    overrides=settings.provider_overrides,
                    custom=settings.custom_providers)
            except (ImportError, ValueError) as exc:
                errors.append(_issue(
                    "error", "profile.model_alias",
                    f"档案 {key} 的模型别名无法解析：{alias}",
                    profile=key,
                    hint=f"{exc}；写成 provider:model（如 deepseek:deepseek-chat）"))

    return _diagnostic_result(errors, warnings, infos)


def _diagnostic_result(
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    infos: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "errors": errors,
        "warnings": warnings,
        "infos": infos,
        "summary": {
            "errors": len(errors),
            "warnings": len(warnings),
            "infos": len(infos),
        },
    }


# ── 写操作 ──────────────────────────────────────────────────────────────


def _validate_against_bindings(
    settings: Settings, catalog: RoleCatalog, profiles: dict[str, AgentProfile],
) -> None:
    """假设新档案已生效，把所有角色的绑定校验一遍。

    这是「保存前就知道会不会把 run 跑挂」的关键：工具只能收窄、split_by_path
    的路径域必须互斥、best_of 的 judge 不能参赛——这些规则在启动期会拒绝整次
    run，等到那时才发现就太晚了。
    """
    hypothetical = settings.model_copy(update={"agent_profiles": profiles})
    problems: list[str] = []
    from agentcrew.models.config import parse_alias

    for key, profile in profiles.items():
        for alias in [profile.model, *profile.fallback]:
            if not alias:
                continue
            try:
                parse_alias(
                    alias,
                    overrides=settings.provider_overrides,
                    custom=settings.custom_providers)
            except ValueError as exc:
                problems.append(f"档案 {key} 的模型别名无法解析：{exc}")
    for spec in catalog.all():
        binding = resolve_binding(spec, hypothetical.role_agents)
        try:
            validate_bindings(
                spec.key, binding, profiles, spec.allowed_tools)
        except BindingError as exc:
            problems.append(str(exc))
    if problems:
        raise ProfileStoreError(
            "保存后配置将无法启动：\n" + "\n".join(f"- {item}" for item in problems))


def save_profiles(
    text: str,
    settings: Settings,
    catalog: RoleCatalog,
    *,
    force: bool = False,
    dry_run: bool = False,
    source: str = "<输入>",
) -> dict[str, Any]:
    """校验并写入一批档案；批量操作要么全部写入，要么一份都不写。"""
    parsed = parse_profile_document(text, source=source)
    directory = Path(settings.profiles_dir or effective_profiles_dir(
        config_path=config_path(settings)))
    # 同名判断必须看「文件是否存在」而不是「是否加载成功」：一份写坏的
    # 档案正是最需要被覆盖修复的对象，按可加载集合判断会把它当成新档案，
    # 然后在写入时撞上「已存在」——用户永远修不好它。
    existing = {
        file.stem
        for pattern in ("*.yaml", "*.yml")
        for file in directory.glob(pattern)
        if file.is_file()
    }
    conflicts = sorted({p.key for p in parsed} & existing)
    if conflicts and not force:
        raise ProfileStoreError(
            f"档案已存在：{'、'.join(conflicts)}；确认覆盖后重试（--force / 勾选覆盖）")

    merged = dict(settings.agent_profiles)
    for profile in parsed:
        merged[profile.key] = profile
    _validate_against_bindings(settings, catalog, merged)

    yaml_keys = _yaml_profile_keys(settings)
    warnings = [
        f"{profile.key} 在 agentcrew.yaml 里也有定义；agents.d 版本将覆盖它"
        for profile in parsed if profile.key in yaml_keys
    ]

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "saved": [p.key for p in parsed],
            "created": sorted({p.key for p in parsed} - existing),
            "updated": conflicts,
            "warnings": warnings,
            "files": {},
        }

    files: dict[str, str] = {}
    created: list[str] = []
    updated: list[str] = []
    # 批量写入的 I/O 回滚快照：校验已经保证「全有或全无」，但磁盘错误仍可能
    # 发生在第 N 份；把已写的恢复到保存前，别让用户面对半套档案。
    snapshots: dict[str, tuple[Path, bytes | None]] = {}
    for profile in parsed:
        target = directory / f"{profile.key}.yaml"
        if not target.is_file():
            legacy = directory / f"{profile.key}.yml"
            target = legacy if legacy.is_file() else target
        snapshots[profile.key] = (
            target, target.read_bytes() if target.is_file() else None)

    try:
        for profile in parsed:
            path = save_profile_file(
                directory, profile, overwrite=profile.key in existing)
            files[profile.key] = str(path)
            (updated if profile.key in existing else created).append(profile.key)
    except (ProfileStoreError, OSError) as exc:
        for target, previous in snapshots.values():
            try:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(target, previous)
            except OSError:
                # 回滚尽力而为；原始错误更重要，不要在这里把它盖掉。
                pass
        raise ProfileStoreError(f"批量保存失败，已回滚：{exc}") from exc
    return {
        "ok": True,
        "dry_run": False,
        "saved": [p.key for p in parsed],
        "created": created,
        "updated": updated,
        "warnings": warnings,
        "files": files,
        "profiles_dir": str(directory),
    }


def delete_profile(
    key: str,
    settings: Settings,
    catalog: RoleCatalog,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """删除 agents.d 里的一份档案；被绑定或被 YAML 兜底时给出明确结论。"""
    directory = Path(settings.profiles_dir or effective_profiles_dir(
        config_path=config_path(settings)))
    sources = profile_sources(directory)
    file = sources.get(key)
    if file is None:
        # 也允许删除一个「加载失败但文件存在」的档案：否则写坏的文件
        # 只能去文件系统里手工处理，诊断页的提示就落空了。
        for suffix in (".yaml", ".yml"):
            candidate = directory / f"{key}{suffix}"
            if candidate.is_file():
                file = candidate
                break
    if file is None:
        if key in settings.agent_profiles:
            raise ProfileStoreError(
                f"档案 {key} 定义在 agentcrew.yaml 中，不在 agents.d；"
                "请直接编辑配置文件（Studio 不重写主配置）")
        raise ProfileStoreError(f"没有这个执行体档案：{key}")

    roles = _bound_roles(settings, catalog).get(key, [])
    if roles and not force:
        raise ProfileStoreError(
            f"档案 {key} 仍被角色绑定：{'、'.join(roles)}；"
            "请先解除绑定，或确认强制删除（会让这些角色在下次启动时报错）")

    trash = delete_profile_file(directory, key)
    revealed = key in _yaml_profile_keys(settings)
    return {
        "ok": True,
        "deleted": key,
        "file": str(file),
        "trash": str(trash),
        "revealed": revealed,
        "bound_roles": roles,
        "warning": (
            f"agentcrew.yaml 里还有同名档案 {key}，删除后它将重新生效"
            if revealed else ""),
    }


def export_yaml(settings: Settings, *, keys: list[str] | None = None) -> str:
    """导出可直接粘贴的 YAML：单档案或 ``agents.profiles`` + ``roles`` 片段。"""
    selected = list(settings.agent_profiles)
    if keys:
        missing = [key for key in keys if key not in settings.agent_profiles]
        if missing:
            raise ProfileStoreError(f"没有这些执行体档案：{'、'.join(missing)}")
        selected = keys

    def dump(profile: AgentProfile) -> dict[str, Any]:
        return {
            name: value
            for name, value in profile.model_dump(mode="json").items()
            if name != "key" and value not in (None, "", [])
        }

    if len(selected) == 1:
        key = selected[0]
        payload: dict[str, Any] = {
            "agents": {"profiles": {key: dump(settings.agent_profiles[key])}}
        }
    else:
        payload = {
            "agents": {"profiles": {
                key: dump(settings.agent_profiles[key]) for key in selected}}
        }
    # 只有导出全部时才带角色绑定：单档案片段粘回去不应顺带改别人的绑定。
    if keys is None and settings.role_agents:
        payload["roles"] = {
            role: value.model_dump(mode="json", exclude_none=True)
            for role, value in settings.role_agents.items()
        }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


# ── 用量 ────────────────────────────────────────────────────────────────


def _empty_usage() -> dict[str, Any]:
    return {
        "runs": set(),
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0.0,
        "roles": Counter(),
        "models": Counter(),
        "last_used": "",
    }


def usage_report(output_dir: Path, *, limit: int = 30) -> dict[str, Any]:
    """跨 run 聚合每个执行体的调用量 / 成本 / 最近使用。

    数据源是 ``ledger.jsonl`` 的 ``llm_call`` 事件：R44 起该事件带 ``profile``，
    因此能回答「这个执行体到底有没有在被用、花了多少钱」——配置页只读卡片
    回答不了这个问题。只扫最近 ``limit`` 个 run，避免为一张卡片翻遍整个
    output 目录。
    """
    root = Path(output_dir)
    if not root.is_dir():
        return {"scanned_runs": 0, "total_runs": 0, "truncated": False, "profiles": {}}
    run_dirs = [
        item for item in root.iterdir()
        if item.is_dir() and (item / "ledger.jsonl").is_file()
    ]
    run_dirs.sort(
        key=lambda item: (item / "ledger.jsonl").stat().st_mtime_ns, reverse=True)
    selected = run_dirs[: max(1, int(limit))]
    buckets: dict[str, dict[str, Any]] = defaultdict(_empty_usage)

    for run_dir in selected:
        try:
            handle = (run_dir / "ledger.jsonl").open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                # 先做廉价的子串过滤：整份台账里绝大多数行不是 llm_call。
                if '"llm_call"' not in line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("event") != "llm_call":
                    continue
                data = item.get("data") or {}
                key = str(data.get("profile") or "default")
                bucket = buckets[key]
                bucket["runs"].add(run_dir.name)
                bucket["calls"] += 1
                bucket["prompt_tokens"] += int(data.get("prompt_tokens") or 0)
                bucket["completion_tokens"] += int(
                    data.get("completion_tokens") or 0)
                bucket["cost_usd"] += float(
                    data.get("estimated_cost_usd") or 0.0)
                bucket["latency_ms"] += float(data.get("latency_ms") or 0.0)
                if data.get("role"):
                    bucket["roles"][str(data["role"])] += 1
                if data.get("model"):
                    bucket["models"][str(data["model"])] += 1
                stamp = str(item.get("ts") or "")
                if stamp > bucket["last_used"]:
                    bucket["last_used"] = stamp

    profiles: dict[str, Any] = {}
    for key, bucket in buckets.items():
        calls = bucket["calls"]
        profiles[key] = {
            "runs": len(bucket["runs"]),
            "calls": calls,
            "prompt_tokens": bucket["prompt_tokens"],
            "completion_tokens": bucket["completion_tokens"],
            "total_tokens": bucket["prompt_tokens"] + bucket["completion_tokens"],
            "cost_usd": round(bucket["cost_usd"], 6),
            "avg_latency_ms": round(bucket["latency_ms"] / calls, 1) if calls else 0.0,
            "roles": dict(bucket["roles"].most_common()),
            "models": dict(bucket["models"].most_common()),
            "last_used": bucket["last_used"],
        }
    return {
        "scanned_runs": len(selected),
        "total_runs": len(run_dirs),
        "truncated": len(run_dirs) > len(selected),
        "profiles": profiles,
    }
