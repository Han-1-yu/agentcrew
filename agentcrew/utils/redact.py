"""按密钥值递归脱敏（R-A 去重：ledger / webhook 共用的唯一实现）。

语义以原 ``runtime/ledger.py::_redact_value`` 为基准（两处旧实现中最全的一支）：

- 字符串过 :func:`agentcrew.utils.logger.redact`——含 ``sk-`` / Bearer 固定规则
  与已知密钥字面量；
- 容器**重建**（不改调用方对象）：``Mapping`` 的**键与值都脱敏**（键和值一样
  可能夹带密钥），``list`` 保持 list，``tuple`` 保持 tuple；
- 其余标量原样保留、不提前字符串化（int 仍是 int、bool 仍是 bool）。
  ``set``/``bytes``/自定义对象的安全由序列化器的 ``default`` 兜底提供
  （见 ``Ledger.emit``），不在本函数职责内。

与旧 ``runtime/webhook.py::_redact_value`` 的差异（合并时显式采纳的行为变更）：

1. spool 落盘现在也吃 ``sk-`` / Bearer 固定规则——与「密钥绝不落盘」红线同向；
2. dict 键也脱敏；
3. tuple 保持 tuple（JSON 序列化与 list 相同，落盘字节不变）。

调用方对密钥集合自身的过滤（如 webhook spool 的 ``len >= 4``）留在调用方，
本函数不做——短串是否误伤正常文本是调用点的业务判断。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agentcrew.utils.logger import redact

__all__ = ["redact_value"]


def redact_value(value: Any, secrets: Sequence[str]) -> Any:
    """递归脱敏：字符串过 ``redact``，容器重建（键值都脱敏），其余原样。"""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, Mapping):
        return {
            redact_value(key, secrets): redact_value(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets) for item in value)
    return value
