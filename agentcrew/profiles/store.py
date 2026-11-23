"""执行体档案的文件化存储：``agents.d/<key>.yaml``。

为什么不是直接改写 ``agentcrew.yaml``：

- 主配置里有大量人工注释与顺序，``safe_load + safe_dump`` 会全部抹掉；
- 一条档案写坏就会让整个配置读不出来——那是「改一个执行体，跑挂所有 run」；
- 一档案一文件才能做「备份 / 回收站 / 逐条校验 / 版本控制 diff」。

存储形状与成熟项目的 agent 定义一致（Claude Code ``.claude/agents/*.md``、
CrewAI ``config/agents.yaml``）：**文件名即 key**，文件内容是 ``AgentProfile``
的字段集合。目录默认是配置文件同级的 ``agents.d/``，可用 ``profiles_dir``
指向别处（例如用户级目录）。

加载优先级：``agents.d`` > ``agentcrew.yaml`` 的 ``agents.profiles``。
与 Claude Code 的 project / user / plugin 作用域一样，同名时高优先级胜出，
被覆盖的定义会在诊断里明确列出——绝不静默吞掉。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agentcrew.roles.binding import AgentProfile
from agentcrew.utils.files import atomic_write_text, read_text

#: 与 Claude Code 的 subagent name 同口径：小写字母开头，允许数字 / 连字符 /
#: 下划线。``default`` 是引擎为「未绑定执行体的角色」保留的占位名，不能占用。
PROFILE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
RESERVED_KEYS = frozenset({"default", "unknown"})

PROFILE_SUFFIXES = (".yaml", ".yml")
TRASH_DIR = ".trash"
BACKUP_SUFFIX = ".bak"

_HEADER = (
    "# AgentCrew 执行体档案（由 Studio / `python main.py agents --save` 维护）\n"
    "# 文件名即 key；字段说明见 docs/PROFILES-2026-11-20.md\n"
)


class ProfileStoreError(ValueError):
    """档案存储层错误：路径非法、文件损坏、重名或校验失败。

    继承 ``ValueError``：CLI 各入口统一按「配置错误」收口（退出码 3），
    与 ``Settings.load`` 的既有契约一致。
    """


@dataclass(frozen=True)
class ProfileEntry:
    """一份已校验的档案及其来源文件。"""

    profile: AgentProfile
    path: Path

    @property
    def key(self) -> str:
        return self.profile.key


@dataclass(frozen=True)
class ProfileFileIssue:
    """一个无法加载的档案文件（供诊断页展示，不阻断其它档案）。"""

    file: Path
    message: str
    key: str | None = None


def validate_profile_key(key: str) -> str:
    """校验并规范化 key；非法时抛 :class:`ProfileStoreError`。"""
    normalized = str(key or "").strip()
    if not normalized:
        raise ProfileStoreError("执行体 key 不能为空")
    if not PROFILE_KEY_PATTERN.match(normalized):
        raise ProfileStoreError(
            f"执行体 key 不合法：{normalized!r}"
            "（小写字母开头，只能包含小写字母、数字、- 和 _，最长 64 位）")
    if normalized in RESERVED_KEYS:
        raise ProfileStoreError(
            f"执行体 key {normalized!r} 是引擎保留名，不能使用")
    return normalized


def effective_profiles_dir(
    configured: str | Path | None = None,
    *,
    config_path: str | Path | None = None,
) -> Path:
    """档案目录：显式配置优先，否则取配置文件同级的 ``agents.d/``。

    即使目录还不存在也返回路径——UI 需要先知道「第一次保存会写到哪里」。
    """
    if configured:
        return Path(configured).expanduser().resolve()
    raw = Path(config_path or os.environ.get("AGENTCREW_CONFIG") or "agentcrew.yaml")
    return (raw.parent / "agents.d").resolve()


def _profile_fields() -> frozenset[str]:
    return frozenset(AgentProfile.model_fields)


def _format_validation_error(exc: ValidationError, where: str) -> str:
    details: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc") or ()) or "(根)"
        message = str(error.get("msg") or "字段非法")
        details.append(f"{location}: {message}")
    return f"{where} 校验失败：" + "；".join(details)


def _coerce_profile(raw: Any, key: str, where: str) -> AgentProfile:
    """把一份映射校验成 ``AgentProfile``；错误信息带上文件位置。

    ``writer:``（空值）视为「只有 key、其余继承」的合法短写法；写成标量 /
    列表才是错误——静默忽略会让用户以为字段生效了。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProfileStoreError(f"{where} 必须是映射（当前是 {type(raw).__name__}）")
    payload = dict(raw)
    declared = payload.get("key")
    if declared is not None and str(declared) != key:
        raise ProfileStoreError(
            f"{where} 的 key={declared!r} 与文件名 / 映射键 {key!r} 不一致"
            "（文件名即 key，二者必须相同）")
    payload["key"] = key
    try:
        return AgentProfile.model_validate(payload)
    except ValidationError as exc:
        raise ProfileStoreError(_format_validation_error(exc, where)) from exc


def _unwrap_document(document: Any) -> dict[str, Any]:
    """兼容三种写法：单档案 / ``profiles:`` / ``agents.profiles:``。"""
    if not isinstance(document, dict):
        raise ProfileStoreError("档案 YAML 顶层必须是映射")
    current: Any = document
    if isinstance(current.get("agents"), dict):
        current = current["agents"]
    if isinstance(current.get("profiles"), dict):
        current = current["profiles"]
    if not isinstance(current, dict):
        raise ProfileStoreError("档案 YAML 顶层必须是映射")
    return current


def parse_profile_document(
    text: str, *, source: str = "<输入>",
) -> list[AgentProfile]:
    """解析一份或多份档案定义。

    支持单档案（含 ``key``）与映射（``key → 字段``），也接受整段
    ``agents.profiles`` 或 ``profiles`` 包装。**全部校验通过才返回**：
    批量导入不做「部分成功」，否则用户以为都进去了。
    """
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileStoreError(f"{source} YAML 解析失败：{exc}") from exc
    if document is None:
        raise ProfileStoreError(f"{source} 为空，没有可保存的档案")
    body = _unwrap_document(document)

    fields = _profile_fields()
    if set(body) <= fields and "key" not in body:
        raise ProfileStoreError(
            f"{source} 缺少 key：单份档案必须声明 key；"
            "多份档案请写成 `key: {字段…}` 的映射")
    single = (
        "key" in body
        and isinstance(body.get("key"), str)
        and set(body) <= fields
    )
    if single:
        key = validate_profile_key(str(body["key"]))
        return [_coerce_profile(body, key, f"{source} 档案 {key}")]

    profiles: list[AgentProfile] = []
    problems: list[str] = []
    for raw_key, raw in body.items():
        where = f"{source} 档案 {raw_key}"
        try:
            key = validate_profile_key(str(raw_key))
            profiles.append(_coerce_profile(raw, key, where))
        except ProfileStoreError as exc:
            problems.append(str(exc))
    if problems:
        raise ProfileStoreError("\n".join(problems))
    if not profiles:
        raise ProfileStoreError(f"{source} 里没有档案定义")
    return profiles


def _profile_file(directory: Path, key: str) -> Path | None:
    """已存在的档案文件（``.yaml`` 优先于 ``.yml``）；没有返回 ``None``。"""
    for suffix in PROFILE_SUFFIXES:
        candidate = directory / f"{key}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def load_profiles_dir(directory: Path) -> dict[str, AgentProfile]:
    """严格加载目录：任一文件损坏直接抛错（与「配置写错就写错」一致）。"""
    entries, issues = scan_profiles_dir(directory)
    if issues:
        raise ProfileStoreError(
            "执行体档案目录存在无法加载的文件：\n"
            + "\n".join(f"- {issue.file.name}: {issue.message}" for issue in issues))
    return {key: entry.profile for key, entry in entries.items()}


def scan_profiles_dir(
    directory: Path,
) -> tuple[dict[str, ProfileEntry], list[ProfileFileIssue]]:
    """宽容扫描目录：返回可加载的档案与逐文件问题（诊断页用）。"""
    entries: dict[str, ProfileEntry] = {}
    issues: list[ProfileFileIssue] = []
    if not directory.is_dir():
        return entries, issues
    seen: dict[str, Path] = {}
    for file in sorted((*directory.glob("*.yaml"), *directory.glob("*.yml"))):
        if not file.is_file():
            continue
        stem = file.stem
        try:
            key = validate_profile_key(stem)
        except ProfileStoreError as exc:
            issues.append(ProfileFileIssue(file=file, message=str(exc), key=stem))
            continue
        if key in seen:
            issues.append(ProfileFileIssue(
                file=file,
                message=f"与 {seen[key].name} 重复定义同一个 key（保留先读到的文件）",
                key=key))
            continue
        try:
            raw = yaml.safe_load(read_text(file))
            profile = _coerce_profile(raw, key, f"档案文件 {file.name}")
        except yaml.YAMLError as exc:
            issues.append(ProfileFileIssue(
                file=file, message=f"YAML 解析失败：{exc}", key=key))
            continue
        except ProfileStoreError as exc:
            issues.append(ProfileFileIssue(file=file, message=str(exc), key=key))
            continue
        entries[key] = ProfileEntry(profile=profile, path=file)
        seen[key] = file
    return entries, issues


def profile_sources(directory: Path) -> dict[str, Path]:
    """key → 来源文件（只含可加载的档案；供 UI 标来源）。"""
    entries, _ = scan_profiles_dir(directory)
    return {key: entry.path for key, entry in entries.items()}


#: R70：借 Orca「Bot Memory in Markdown」——人格与记忆的 companion 文件。
#: 与档案同目录、按 key 命名：``<key>.SOUL.md`` / ``<key>.MEMORY.md``。
#: **Markdown 是唯一事实源**：YAML 的保存 / 备份 / 回收站一律不触碰它们；
#: 人在平台外编辑后，下一次 run（工厂刷新）生效。
COMPANION_KINDS = ("SOUL", "MEMORY")

#: 单份 companion 的字符上限（防上下文预算膨胀）；超出截断并标注。
COMPANION_MAX_CHARS = 6000


def companion_path(directory: Path, key: str, kind: str) -> Path:
    """companion 文件路径；非法 key / kind 抛 :class:`ProfileStoreError`。"""
    normalized = validate_profile_key(key)
    if kind not in COMPANION_KINDS:
        raise ProfileStoreError(
            f"未知的 companion 类型：{kind!r}（只支持 SOUL / MEMORY）")
    return directory / f"{normalized}.{kind}.md"


def read_companion(
    directory: Path, key: str, kind: str,
    *, max_chars: int = COMPANION_MAX_CHARS,
) -> str:
    """读取 companion 文本；缺失 / 空 / 不可读 → ``""``（绝不阻断 run）。

    去首尾空白；超过字符上限时截断并追加「…（已截断）」标注。截断在读取层
    做一次，提示词层不再二次截断。空串 = 关闭态，提示词逐字不变。
    """
    path = companion_path(directory, key, kind)
    try:
        text = read_text(path).strip()
    except (OSError, ValueError):
        return ""
    if not text:
        return ""
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…（已截断）"
    return text


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    shutil.copy2(path, backup)
    return backup


def save_profile_file(
    directory: Path,
    profile: AgentProfile,
    *,
    overwrite: bool = False,
) -> Path:
    """把一份档案原子写入目录，返回落盘路径。

    - 已存在且 ``overwrite=False`` → 抛错（新建与覆盖必须是两个明确的意图）；
    - 覆盖前留一份 ``<file>.bak``，误改可手工回滚；
    - 使用同目录临时文件 + ``os.replace``，避免半截文件被下一次加载读到。
    """
    key = validate_profile_key(profile.key)
    directory.mkdir(parents=True, exist_ok=True)
    existing = _profile_file(directory, key)
    if existing is not None and not overwrite:
        raise ProfileStoreError(f"档案已存在：{key}（{existing.name}）；如需覆盖请明确确认")
    target = existing or (directory / f"{key}.yaml")
    payload = profile.model_dump(mode="json", exclude_defaults=True)
    payload["key"] = key
    text = _HEADER + yaml.safe_dump(
        payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    try:
        if existing is not None:
            _backup(existing)
        atomic_write_text(target, text)
    except OSError as exc:
        raise ProfileStoreError(f"写入档案失败：{target}（{exc}）") from exc
    return target


def delete_profile_file(directory: Path, key: str) -> Path:
    """把档案移入目录内的 ``.trash/``，返回回收站路径。

    不做物理删除：执行体档案是编排配置，误删会让绑定断链、run 起不来；
    留一份可恢复副本的成本远低于让用户去翻 git 历史。
    """
    normalized = validate_profile_key(key)
    target = _profile_file(directory, normalized)
    if target is None:
        raise ProfileStoreError(
            f"agents.d 里没有档案 {normalized!r}；"
            "它可能定义在 agentcrew.yaml 中，请直接编辑该文件")
    trash = directory / TRASH_DIR
    trash.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidate = trash / f"{normalized}-{stamp}{target.suffix}"
    counter = 1
    while candidate.exists():
        candidate = trash / f"{normalized}-{stamp}-{counter}{target.suffix}"
        counter += 1
    try:
        os.replace(target, candidate)
    except OSError as exc:
        raise ProfileStoreError(f"移动档案到回收站失败：{target}（{exc}）") from exc
    return candidate
