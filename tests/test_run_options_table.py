"""R66c/R66d：表驱动校验的顺序契约与 NaN 入口拦截。

文案与逐键语义已由既有 9 个测试文件钉住；这里补的是重构后容易漂移的
**结构契约**（检查顺序、nullable、枚举不做类型预检）与 R66d 修复的
NaN 漏洞（修复前 NaN 穿过范围检查，运行时才在 socket.settimeout 炸）。
"""

from __future__ import annotations

import math

from agentcrew.api.run_options import validate_run_options


def test_unknown_keys_report_before_any_value_check():
    error = validate_run_options({"nope": 1, "mock": "bad"})
    assert error is not None
    assert error.startswith("options 含不支持的键：nope")


def test_first_invalid_key_in_insertion_order_wins():
    a = validate_run_options({"mock": "bad", "max_concurrency": 0})
    b = validate_run_options({"max_concurrency": 0, "mock": "bad"})
    assert a == "options.mock 必须是布尔值"
    assert b == "options.max_concurrency 必须 >= 1"


def test_flow_mutual_exclusion_precedes_value_checks():
    error = validate_run_options({"flow": [], "flow_name": 3})
    assert error == "options.flow 与 options.flow_name 只能给一个"


def test_nullable_keys_accept_none_others_reject():
    assert validate_run_options(
        {"default_model": None, "max_cost_usd": None, "max_llm_calls": None}) is None
    assert validate_run_options({"mock": None}) == "options.mock 不能为 null"


def test_enum_keys_reject_non_string_without_type_precheck():
    # 与旧链一致：数组/对象给枚举键走同一条枚举文案，而非类型文案。
    assert validate_run_options({"delegation_mode": ["any"]}) == (
        "options.delegation_mode 必须是 any 或 manager_only")


def test_webhook_timeout_nan_rejected_at_entry():
    """R66d：NaN 曾在入口放行、运行时才炸；现在必须入口拒绝。"""
    assert validate_run_options({"webhook_timeout_seconds": math.nan}) == (
        "options.webhook_timeout_seconds 必须 > 0")


def test_webhook_timeout_positive_infinity_keeps_upper_bound_message():
    assert validate_run_options({"webhook_timeout_seconds": math.inf}) == (
        "options.webhook_timeout_seconds 必须 <= 30")


def test_max_cost_rejects_non_finite():
    for bad in (math.nan, math.inf, -math.inf):
        assert validate_run_options({"max_cost_usd": bad}) == (
            "options.max_cost_usd 必须是 >= 0 的有限数")


def test_peer_history_turns_range_and_type_check():
    assert validate_run_options({"peer_consult_history_turns": 0}) is None
    assert validate_run_options({"peer_consult_history_turns": 5}) is None
    assert validate_run_options({"peer_consult_history_turns": 6}) == (
        "options.peer_consult_history_turns 必须落在 0~5 之间（0 = 不带历史）")
    assert validate_run_options({"peer_consult_history_turns": -1}) == (
        "options.peer_consult_history_turns 必须落在 0~5 之间（0 = 不带历史）")
    assert validate_run_options({"peer_consult_history_turns": "3"}) == (
        "options.peer_consult_history_turns 必须是整数")
    assert validate_run_options({"peer_consult_history_turns": True}) == (
        "options.peer_consult_history_turns 必须是整数")
