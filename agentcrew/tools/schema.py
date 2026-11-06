"""工具入参的 JSON Schema 子集校验（对标 CrewAI ``BaseTool._validate_kwargs``）。

:class:`~agentcrew.tools.tool.Tool.input_schema` 过去只被渲染进系统提示词，模型
写错参数（缺必填、类型错、越界）时只能靠工具自己 ``try/except`` 兜底 —— 有的
工具会抛异常冒泡，有的静默把 ``"abc"`` 当下限，错误信息还不一定能让模型改对。
本模块在**执行前**做一次确定性校验，把失败收敛成带「正确形状」提示的错误文本
回灌给模型，让 ReAct 循环自我纠正。

设计边界（都是刻意的）：

- **零第三方依赖**：不引入 ``jsonschema``（仓库依赖白名单不允许），只实现
  ``type`` / ``required`` / ``enum`` / ``const`` / ``minimum`` / ``maximum`` /
  ``minLength`` / ``maxLength`` / ``pattern`` / ``items`` / ``additionalProperties``
  这些实际会用到的关键字；未认识的关键字一律忽略，不误伤外部 MCP 工具。
- **零项目内依赖**：本模块位于 ``tools`` 层，架构护栏禁止它反向依赖
  ``runtime`` / ``agents`` / ``crew`` 等上层。
- **宽容回落**：空 schema、非 dict schema、没有 ``properties`` 的自由形状 schema
  一律放行 —— 自定义工具与第三方 MCP 的 ``inputSchema`` 都走原执行路径。
- **不强制类型转换**：与 CrewAI 的 pydantic 强转不同，这里只判不改。模型给的
  ``"10"`` 不会被悄悄变成 ``10``；错就是错，回灌后让模型自己写对，行为可预测。

调用方（:meth:`~agentcrew.agents.base_agent.BaseAgent._execute_tool_calls`）负责把
:func:`validate_tool_args` 的结果拼成错误文本，并用 :func:`schema_hint` 附上正确形状。
"""

from __future__ import annotations

import math
import re
from typing import Any

#: 校验错误最多回灌条数：一次给 30 条中文错误对模型没有帮助，还会挤占上下文。
MAX_ERRORS = 8

#: ``schema_hint`` 里最多列出的属性数。
MAX_HINT_PROPERTIES = 12


def _type_matches(value: Any, expected: str) -> bool:
    """按 JSON Schema 的 ``type`` 判类型；未知类型一律视为匹配（宽容）。"""
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        # bool 是 int 的子类，但 JSON Schema 里 true 不是 1。
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if expected == "null":
        return value is None
    return True


def _validate_value(name: str, value: Any, rule: dict[str, Any]) -> list[str]:
    """校验一个属性值，返回中文错误列表（空列表 = 通过）。"""
    errors: list[str] = []
    expected = rule.get("type")
    if isinstance(expected, str) and not _type_matches(value, expected):
        errors.append(f"{name} 应为 {expected} 类型，实际是 {type(value).__name__}")
        return errors  # 类型都不对，后续数值/长度比较没有意义

    if "const" in rule and value != rule["const"]:
        errors.append(f"{name} 必须等于 {rule['const']!r}")
    enum = rule.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{name} 必须是以下取值之一：{enum}")

    if isinstance(value, str):
        minimum_length = rule.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{name} 长度不能小于 {minimum_length}")
        maximum_length = rule.get("maxLength")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            errors.append(f"{name} 长度不能大于 {maximum_length}")
        pattern = rule.get("pattern")
        if isinstance(pattern, str) and pattern:
            try:
                if re.search(pattern, value) is None:
                    errors.append(f"{name} 不匹配格式 {pattern}")
            except re.error:
                pass  # schema 自身写坏了正则：不让它反过来拒绝所有合法入参

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = rule.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{name} 不能小于 {minimum}")
        maximum = rule.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{name} 不能大于 {maximum}")
        exclusive_min = rule.get("exclusiveMinimum")
        if isinstance(exclusive_min, (int, float)) and value <= exclusive_min:
            errors.append(f"{name} 必须大于 {exclusive_min}")
        exclusive_max = rule.get("exclusiveMaximum")
        if isinstance(exclusive_max, (int, float)) and value >= exclusive_max:
            errors.append(f"{name} 必须小于 {exclusive_max}")

    if isinstance(value, list):
        item_rule = rule.get("items")
        if isinstance(item_rule, dict):
            for index, item in enumerate(value):
                if isinstance(item_rule.get("type"), str) and not _type_matches(
                    item, item_rule["type"]
                ):
                    errors.append(
                        f"{name}[{index}] 应为 {item_rule['type']} 类型，"
                        f"实际是 {type(item).__name__}"
                    )
                    break
    return errors


def validate_tool_args(schema: Any, args: Any) -> list[str]:
    """按 ``schema`` 校验 ``args``，返回中文错误列表；空列表表示通过。

    ``schema`` 为空 / 不是映射 / 没有 ``properties`` 时返回空列表 —— 自定义工具与
    外部 MCP 工具的自由形状 schema 一律走原执行路径（向后兼容）。
    ``args`` 不是映射时无论 schema 如何都判失败（``run(**args)`` 本就要求映射）。
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return ["工具入参必须是 JSON 对象（键 → 值映射）"]
    if not isinstance(schema, dict) or not schema:
        return []
    declared_type = schema.get("type")
    if declared_type is not None and declared_type != "object":
        # 非对象 schema（例如 MCP 工具给的是单个字符串）：本校验器不覆盖，放行。
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return []

    errors: list[str] = []
    required = schema.get("required")
    if isinstance(required, (list, tuple)):
        for name in required:
            if name not in args:
                errors.append(f"缺少必填参数 {name}")

    if schema.get("additionalProperties") is False:
        for name in args:
            if name not in properties:
                errors.append(f"未知参数 {name}（该工具不接受额外参数）")

    for name, rule in properties.items():
        if name not in args or not isinstance(rule, dict):
            continue
        errors.extend(_validate_value(str(name), args[name], rule))
        if len(errors) >= MAX_ERRORS:
            break
    return errors[:MAX_ERRORS]


def schema_hint(schema: Any) -> str:
    """把 schema 渲染成一句给模型看的「正确形状」提示（校验失败时附在错误后）。

    输出形如 ``必填 path(string)、pattern(string)；可选 timeout(number，范围 0<x<=60)``。
    自由形状 / 空 schema 返回空串，调用方据此不追加提示。
    """
    if not isinstance(schema, dict) or not schema:
        return ""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return ""
    required = schema.get("required")
    required_set = set(required) if isinstance(required, (list, tuple)) else set()

    def describe(name: str, rule: Any) -> str:
        if not isinstance(rule, dict):
            return name
        bits = [f"{name}({rule.get('type') or 'any'}"]
        if isinstance(rule.get("minimum"), (int, float)) and isinstance(
            rule.get("maximum"), (int, float)
        ):
            bits.append(f"，范围 {rule['minimum']}~{rule['maximum']}")
        if isinstance(rule.get("enum"), list) and rule["enum"]:
            bits.append(f"，可选值 {rule['enum']}")
        if isinstance(rule.get("minLength"), int) and isinstance(
            rule.get("maxLength"), int
        ):
            bits.append(f"，长度 {rule['minLength']}~{rule['maxLength']}")
        return "".join(bits) + ")"

    must = [
        describe(str(name), properties[name])
        for name in list(properties)[:MAX_HINT_PROPERTIES]
        if name in required_set
    ]
    optional = [
        describe(str(name), properties[name])
        for name in list(properties)[:MAX_HINT_PROPERTIES]
        if name not in required_set
    ]
    parts: list[str] = []
    if must:
        parts.append("必填 " + "、".join(must))
    if optional:
        parts.append("可选 " + "、".join(optional))
    return "；".join(parts)
