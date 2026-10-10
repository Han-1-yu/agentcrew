"""流式循环检测（P1.5）—— 借鉴 Magpie ``loop_guard.go`` 的滑动窗口算法。

推理模型（deepseek-r1 / qwen-qwq / claude thinking）陷入死循环时会一直烧
token 直到 ``max_tokens``。本模块在流式产出过程中逐 unit 统计 distinct 数：

- **滑动窗口**：``LOOP_WINDOW = 256`` 个 unit（约 600 tokens）内若 distinct
  unit 数 ≤ ``LOOP_DISTINCT = 24``，判定为循环。
- **环形缓冲 + 哈希计数**：每个 unit 归一化（去标点、小写、截前 256 字节）后
  做 FNV-1a 64 位哈希，存入长度 256 的环形数组；``counts[hash]`` 记频次。窗口
  满后新 unit 顶掉最老的 unit，对应频次 -1（归零即删 key）—— O(1) 维护窗口内
  distinct 数。
- **unit 切分**：按句末标点（``. ! ? 。 ！ ？ ; ;``）切分，但**只在标点后跟
  空白时切**，避免把 ``Mr. Smith`` 这种缩写切开。
- **无字母 unit 过滤**：归一化后不含任何拉丁字母 / 中日韩字符的 unit 直接跳过，
  避免把代码块 / JSON / hex 串误判成循环。
- **双通道**：reasoning channel 与 text channel 各持一个 :class:`UnitRun`，
  互不干扰。当前 ``StreamChunk`` 尚未区分 channel，工厂统一喂 text 通道；
  预留 reasoning 通道供未来 reasoning 流接入。

检测到循环后工厂层立即抛 :class:`LoopDetectedError`，中断当前流（已 yield 的
chunk 无法收回，调用方拿到部分内容后收到异常），并落一条 ``loop_detected``
ledger 事件。
"""

from __future__ import annotations

import re

#: FNV-1a 64 位参数（offset basis / prime）。
_FNV64_OFFSET = 14695981039346656037
_FNV64_PRIME = 1099511628211
_FNV64_MASK = 0xFFFFFFFFFFFFFFFF

#: 句末标点后跟至少一个空白字符时切分（缩写 ``Mr.`` 后面跟字母不切）。
#: 一个或多个连续句末标点 + 后续空白作为一个切分边界；边界落在空白之后。
_SENT_END_RE = re.compile(r"[.!?。！？；;]+\s+")

#: 归一化后必须至少包含一个拉丁字母或中日韩字符，否则视为纯符号 / 数字 / hex。
_HAS_LETTER_RE = re.compile(r"[a-zA-Z一-鿿]")

#: 归一化时剥掉所有标点（保留 \w 与空白；Python re 默认 Unicode，CJK 保留）。
_PUNCT_RE = re.compile(r"[^\w\s]+")


class LoopDetectedError(RuntimeError):
    """检测到模型回复陷入重复循环（P1.5）。

    携带 channel（"reasoning" / "text"）、循环的 unit 样本列表与窗口大小，
    便于 ledger 事件与日志事后诊断「模型在重复什么」。
    """

    def __init__(self, channel: str, sample_units: list[str], window_size: int) -> None:
        self.channel = channel
        self.sample_units = sample_units
        self.window_size = window_size
        distinct = len({_normalize(u) for u in sample_units})
        super().__init__(
            f"检测到{channel}通道循环：窗口{window_size}个unit中仅有{distinct}个不同")


def _fnv1a_64(data: bytes) -> int:
    """FNV-1a 64 位哈希（跨进程稳定，不依赖 Python 内置 ``hash()`` 的随机种子）。"""
    h = _FNV64_OFFSET
    for byte in data:
        h ^= byte
        h = (h * _FNV64_PRIME) & _FNV64_MASK
    return h


def _normalize(raw: str) -> str:
    """归一化 unit：小写、去标点、strip；用于哈希键。"""
    return _PUNCT_RE.sub("", raw.lower()).strip()


class UnitRun:
    """单通道的循环检测器（对应 Magpie ``unitRun``）。

    状态在实例内：环形缓冲 ``ring`` 存最近 ``LOOP_WINDOW`` 个 unit 的哈希；
    ``counts[hash] = [count, first_text]`` 维护窗口内频次与首见文本样本。
    ``n`` 是累计喂入的**有效**（含字母）unit 数——无字母 unit 不计入窗口。
    """

    LOOP_WINDOW = 256
    LOOP_DISTINCT = 24
    UNIT_KEY_MAX = 256

    def __init__(self) -> None:
        self.ring: list[int] = [0] * self.LOOP_WINDOW
        self.n = 0
        #: hash -> [频次, 首见原文]。首见原文用于触发循环时上报样本。
        self.counts: dict[int, list] = {}
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        """喂入新文本，切出完整 unit 喂给 :meth:`_add_unit`。

        返回触发循环时的样本 unit 列表；未触发返回空列表。一旦触发，缓冲区中
        剩余文本不再处理（流即将被异常中断）。
        """
        self._buffer += text
        while True:
            match = _SENT_END_RE.search(self._buffer)
            if match is None:
                return []
            unit = self._buffer[:match.end()]
            self._buffer = self._buffer[match.end():]
            samples = self._add_unit(unit)
            if samples:
                return samples

    def flush(self) -> list[str]:
        """流结束时处理缓冲区中剩余的半截 unit（同样可能触发循环）。"""
        remainder = self._buffer
        self._buffer = ""
        if not remainder.strip():
            return []
        return self._add_unit(remainder)

    def _add_unit(self, raw: str) -> list[str]:
        """处理一个完整 unit；返回样本列表（空列表 = 未触发循环）。"""
        key = _normalize(raw)
        # 无字母 / 纯符号数字 unit 跳过：代码块、JSON、hex 串不应参与循环检测。
        if not key or _HAS_LETTER_RE.search(key) is None:
            return []
        data = key.encode("utf-8")[: self.UNIT_KEY_MAX]
        h = _fnv1a_64(data)

        slot = self.n % self.LOOP_WINDOW
        # 窗口已满：顶掉最老的 unit（它即将被覆盖）。
        if self.n >= self.LOOP_WINDOW:
            old = self.ring[slot]
            entry = self.counts.get(old)
            if entry is not None:
                entry[0] -= 1
                if entry[0] <= 0:
                    del self.counts[old]
        self.ring[slot] = h
        entry = self.counts.get(h)
        if entry is None:
            self.counts[h] = [1, raw.strip()]
        else:
            entry[0] += 1
        self.n += 1

        if self.n >= self.LOOP_WINDOW and len(self.counts) <= self.LOOP_DISTINCT:
            return [item[1] for item in self.counts.values()]
        return []


class LoopGuard:
    """双通道循环检测器（reasoning / text），工厂在流式期间持有一个实例。

    ``enabled=False`` 时所有方法退化为空操作（向后兼容：默认关闭，不影响
    既有流式调用的性能与行为）。
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.reasoning = UnitRun()
        self.text = UnitRun()

    def feed_text(self, delta: str) -> list[str]:
        """喂 text 通道增量；返回样本（空 = 未触发）。"""
        if not self.enabled:
            return []
        return self.text.feed(delta)

    def feed_reasoning(self, delta: str) -> list[str]:
        """喂 reasoning 通道增量；返回样本（空 = 未触发）。"""
        if not self.enabled:
            return []
        return self.reasoning.feed(delta)

    def flush(self) -> dict[str, list[str]]:
        """流结束时冲刷两个通道；返回 ``{channel: 样本}``（仅含触发的通道）。"""
        if not self.enabled:
            return {}
        result: dict[str, list[str]] = {}
        for channel, run in (("reasoning", self.reasoning), ("text", self.text)):
            samples = run.flush()
            if samples:
                result[channel] = samples
        return result
