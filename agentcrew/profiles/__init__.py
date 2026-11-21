"""执行体档案（AgentProfile）的存储、诊断与用量汇总。

与 :mod:`agentcrew.roles.binding` 的分工：那里定义「执行体是什么」，这里负责
「执行体放在哪、怎么改、改完会不会把配置跑挂」。纯配置层，不依赖 Crew。
"""

from agentcrew.profiles.store import (
    PROFILE_KEY_PATTERN,
    ProfileEntry,
    ProfileFileIssue,
    ProfileStoreError,
    delete_profile_file,
    effective_profiles_dir,
    load_profiles_dir,
    parse_profile_document,
    profile_sources,
    save_profile_file,
    scan_profiles_dir,
    validate_profile_key,
)

__all__ = [
    "PROFILE_KEY_PATTERN",
    "ProfileEntry",
    "ProfileFileIssue",
    "ProfileStoreError",
    "delete_profile_file",
    "effective_profiles_dir",
    "load_profiles_dir",
    "parse_profile_document",
    "profile_sources",
    "save_profile_file",
    "scan_profiles_dir",
    "validate_profile_key",
]
