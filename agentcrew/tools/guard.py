"""P2-4：工具级 guardrails —— 在**副作用发生前**拦截。

对标 LangChain/LangGraph 的 guardrails，但只做「能确定性判断、能在执行前判断」的
那一类：正则 / 长度 / 字段定位。**不用 LLM 判**——用模型判断模型，既不稳定又贵，
而且失败模式恰好是「该拦的没拦」。

三个刻意设计：

1. **三态而不是两态**：``block``（拒绝执行，原因回灌给模型）/ ``warn``（放行但留痕）/
   ``allow``（规则写得明白但不动手，用于文档化与灰度）。真按 bool 设计，第一次遇到
   「这句话有点危险但业务确实需要」时，人就会去改代码——三态留出了这条路。
2. **两个相位**：``input``（工具执行**前**，拦副作用）/ ``output``（结果回灌**前**，
   拦泄漏）。前者才是安全边界所在：``write_file`` / ``run_python`` 一旦跑完，
   拦截就只是「事后报警」。
3. **配置驱动、默认空**：没有规则时整条链路是一次 ``if`` 判断，行为与改造前逐字节
   一致。规则写在 ``agentcrew.yaml`` 的 ``tool_guards:`` 段，加载期校验（正则写错、
   action 拼错都在启动时报，而不是跑到一半才发现某条规则从来不生效）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ACTIONS",
    "PHASES",
    "GuardHit",
    "ToolGuard",
    "build_tool_guard",
]

#: 支持的动作。``allow`` 是显式的「这条规则不拦」——写下来是为了让配置本身可读
#: （先记录、后收紧），不必删掉再写回。
ACTIONS: frozenset[str] = frozenset({"allow", "warn", "block"})

#: 支持的两个相位。
PHASES: frozenset[str] = frozenset({"input", "output"})


class GuardHit(BaseModel):
    """一次命中。``action`` 为 ``block`` 时上层必须**不执行**工具。"""

    model_config = ConfigDict(extra="forbid")

    rule: str
    phase: str
    action: str
    tool: str = ""
    field: str = ""
    reason: str = ""


class ToolGuard:
    """一组已校验的规则；``check_input`` / ``check_output`` 各自返回命中列表。"""

    def __init__(self, rules: Sequence[Mapping[str, Any]]) -> None:
        self._rules: list[dict[str, Any]] = []
        for rule in rules:
            # 默认值在这里补齐，而不是指望调用方（或 Settings 的校验器）已经填好：
            # 库层直接 ``ToolGuard([{"name": "x", "pattern": "y"}])`` 也必须可用。
            entry: dict[str, Any] = {
                "name": str(rule.get("name") or "unnamed"),
                "phase": str(rule.get("phase") or "input"),
                "action": str(rule.get("action") or "block"),
                "field": str(rule.get("field") or ""),
                "tools": tuple(rule.get("tools") or ()),
                "pattern": rule.get("pattern"),
                "max_length": rule.get("max_length"),
            }
            pattern = entry["pattern"]
            if pattern is not None:
                # 编译期就炸：配置里写错正则该在加载时报，而不是第一次调用时
                # 静默变成「这条规则永远不命中」。
                entry["_regex"] = re.compile(str(pattern))
            self._rules.append(entry)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def check_input(
        self, tool_name: str, args: Mapping[str, Any],
    ) -> list[GuardHit]:
        """工具执行**前**检查入参（``phase: input``）。"""
        payload = json.dumps(dict(args), ensure_ascii=False, default=str)
        return self._check("input", tool_name, args, payload)

    def check_output(self, tool_name: str, text: str) -> list[GuardHit]:
        """结果回灌**前**检查输出（``phase: output``）。"""
        return self._check("output", tool_name, {}, text)

    def has_blocking(self, hits: Sequence[GuardHit]) -> bool:
        return any(hit.action == "block" for hit in hits)

    def _check(
        self, phase: str, tool_name: str,
        args: Mapping[str, Any], payload: str,
    ) -> list[GuardHit]:
        hits: list[GuardHit] = []
        for rule in self._rules:
            if rule["phase"] != phase:
                continue
            tools = rule.get("tools") or ()
            if tools and tool_name not in tools:
                continue
            field = str(rule.get("field") or "")
            target = payload
            if field:
                if phase == "input":
                    if field not in args:
                        continue      # 没有这个字段 = 这条规则不适用
                    value = args[field]
                    target = value if isinstance(value, str) else json.dumps(
                        value, ensure_ascii=False, default=str)
                else:
                    # output 相位没有「字段」概念：规则里写了 field 也按全文匹配，
                    # 但命中记录里保留 field 便于人读。
                    target = payload
            reason = ""
            regex = rule.get("_regex")
            if regex is not None and regex.search(target):
                reason = f"命中模式 {rule.get('pattern')!r}"
            max_length = rule.get("max_length")
            if not reason and isinstance(max_length, int) and len(target) > max_length:
                reason = f"长度 {len(target)} 超过上限 {max_length}"
            if not reason:
                continue
            action = str(rule.get("action") or "block")
            if action == "allow":
                continue              # 显式放行：不留痕也不拦（规则只作记录）
            hits.append(GuardHit(
                rule=str(rule.get("name") or "unnamed"),
                phase=phase,
                action=action,
                tool=tool_name,
                field=field,
                reason=reason,
            ))
        return hits


def build_tool_guard(rules: Sequence[Mapping[str, Any]] | None) -> ToolGuard | None:
    """从配置建 guard；空列表 / ``None`` 返回 ``None``（调用方据此跳过整段逻辑）。"""
    if not rules:
        return None
    return ToolGuard(rules)
