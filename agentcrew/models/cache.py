"""LLM 响应缓存（P3-2）。

相同的 LLM 调用（相同 alias + messages + 采样参数）在确定性采样下必然得到相同
结果；对它重复调用只是白白烧 token。本模块提供一个进程内、LRU 容量上限的响应
缓存，由 :class:`~agentcrew.models.factory.ModelFactory` 在 ``temperature == 0``
时启用。

设计取舍：

- **只缓存确定性调用**：``temperature > 0`` 的结果本就非确定，缓存它会让同一
  输入在不同时间得到「相同」答案，掩盖采样的随机性 —— 一律不缓存。
- **键是内容哈希**：alias / messages / temperature / max_tokens 四元组的
  SHA-256。messages 按 role+content 规范化，避免键里出现不稳定的对象 id。
- **命中不记账**：缓存命中直接返回，不经过 Provider、不占信号量、不计入 token
  与成本；工厂侧另记 ``llm_cache_hit`` 与一条 usage=0 的 ``llm_call(cached=True)``
  便于审计。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Sequence

from agentcrew.models.providers.base import CompletionResult, Message

#: 默认缓存容量：超出后按 LRU 淘汰最久未命中的条目。
_DEFAULT_MAX_ENTRIES = 1000


class LLMCache:
    """进程内 LRU 响应缓存：键为内容哈希，值为 :class:`CompletionResult`。

    不是线程/事件循环安全的并发原语，但所有读写都发生在单线程事件循环内且
    无 ``await`` 穿插，因此 dict 操作天然原子（与工厂其它状态一致）。
    """

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        if max_entries <= 0:
            raise ValueError(f"max_entries 必须为正整数，得到 {max_entries!r}")
        self._max_entries = max_entries
        #: 有序 dict 实现 LRU：头部最旧、尾部最新；命中时挪到尾部，写入超限时
        #: 弹出头部（最旧）。
        self._store: OrderedDict[str, CompletionResult] = OrderedDict()
        self.hit_count = 0
        self.miss_count = 0

    @staticmethod
    def _make_key(
        alias: str,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """生成缓存键：四元组的规范化 JSON 的 SHA-256 十六进制摘要。"""
        payload = {
            "alias": alias,
            "messages": [
                {"role": m["role"], "content": m["content"]} for m in messages
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> CompletionResult | None:
        """查缓存：命中返回结果（并把条目挪到 LRU 尾部），未命中返回 ``None``。"""
        result = self._store.get(key)
        if result is None:
            self.miss_count += 1
            return None
        self.hit_count += 1
        self._store.move_to_end(key)
        return result

    def set(self, key: str, result: CompletionResult) -> None:
        """写缓存：若已存在则刷新到尾部；超容量时弹出最旧条目。"""
        self._store[key] = result
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    def clear(self) -> None:
        """清空缓存并重置命中统计。"""
        self._store.clear()
        self.hit_count = 0
        self.miss_count = 0

    def __len__(self) -> int:
        return len(self._store)
