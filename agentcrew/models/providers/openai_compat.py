"""OpenAI 兼容 Provider：OpenAI 及 DeepSeek/豆包/通义/文心/Ollama（spec §12.1）。

设计要点：

- **SDK 延迟 import**：只在 ``client is None`` 时于构造函数内 import
  ``openai.AsyncOpenAI``。本模块（及其测试）因此在未安装 ``openai`` 的环境里
  也能正常 import —— 测试一律注入 fake client，不触网。
- **错误分类 + 重试**：SDK 异常在**被重试的可调用体内**翻译成
  :class:`~agentcrew.utils.retry.TransientError` /
  :class:`~agentcrew.utils.retry.PermanentError`，``retry_async`` 只看到类型化错误；
  429/500/502/503/504 与超时/连接错误重试 4 次，400/401/403/404/422 立即失败。
- **Retry-After**：经 ``retry_async`` 的 ``retry_after`` 钩子读取 SDK 异常的
  ``response.headers``，把服务端退避指令带进重试引擎（spec §17.6）。服务端指令
  优先，但**有上限**（:data:`MAX_RETRY_AFTER_SECONDS`）：截断发生时在异常对象上
  留痕（该对象正是传给 ``on_retry`` 的那一个），ledger 可见，绝不静默。
"""

import math
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

from agentcrew.models.config import ModelBinding
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.utils.retry import (
    PermanentError,
    TransientError,
    retry_async,
)

#: openai SDK 把 HTTP 状态码写进异常字符串的形式，如 ``Error code: 429 - ...``。
_HTTP_STATUS_RE = re.compile(r"Error code:\s*(\d{3})")

#: 可重试的 HTTP 状态码：限流（429）与服务端瞬时故障（5xx）。
#: 其余状态码（400/401/403/404/422 等）与无法识别的错误一律快速失败。
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

#: 异常类型名含这些子串即视为瞬时故障：各 SDK/httpx 的超时与连接错误
#: （``APITimeoutError``、``ConnectTimeout``、``APIConnectionError``、
#: ``httpx.ConnectError``、``TransportError`` …）。按类型名匹配而非 import
#: httpx —— 硬 import 会破坏本模块「未装 SDK 也能 import」的延迟加载缝。
_TRANSIENT_TYPE_MARKERS = ("Timeout", "Connection", "Connect", "Transport", "Network")

#: ``Retry-After`` 上限（秒）。服务端指令优先，但闲聊补全端点没有正当理由要求
#: 更久的等待；不设上限等于把「停摆本进程」的能力交给远端（DoS），
#: 一次 ``Retry-After: 86400`` 就能让 ``--auto`` 跑一天不返回。
MAX_RETRY_AFTER_SECONDS = 300.0

#: 截断发生时挂在异常上的属性名，值为服务端**原始**要求（秒）。
#: 截断经 ``on_retry`` 上报（异常对象即通道），ledger 可读该属性向操作者说明
#: 「服务端要了多久、我们实际等多久」，而不是只看到一个 5 分钟等待。
RETRY_AFTER_CLAMPED_ATTR = "retry_after_clamped_from"

#: 生产重试参数（spec §17.6）。
_ATTEMPTS = 4
_BASE_DELAY = 1.0


def classify_http_error(exc: Exception) -> type[Exception]:
    """把 provider 异常分类为 ``TransientError`` 或 ``PermanentError``。

    顺序：先解析异常字符串里的 ``Error code: NNN``（openai SDK 的格式），
    再看异常类型名是否命中 :data:`_TRANSIENT_TYPE_MARKERS`（超时/连接/传输/网络）；
    两者都不命中时返回 ``PermanentError`` —— 未知错误快速失败，胜过无限重试。
    """
    match = _HTTP_STATUS_RE.search(str(exc))
    if match is not None:
        status = int(match.group(1))
        return TransientError if status in _TRANSIENT_STATUSES else PermanentError
    type_name = type(exc).__name__
    if any(marker in type_name for marker in _TRANSIENT_TYPE_MARKERS):
        return TransientError
    return PermanentError


def retry_after_seconds(exc: BaseException) -> float | None:
    """从 SDK 异常的 ``response.headers`` 读 ``Retry-After``（秒）。

    重试引擎看到的是翻译后的类型化错误，原始 SDK 异常挂在 ``__cause__``（或
    ``__context__``）上，因此沿因果链逐层查找 ``Retry-After``。

    服务端指令优先，但截断到 :data:`MAX_RETRY_AFTER_SECONDS`；发生截断时在
    ``exc`` 上留痕（:data:`RETRY_AFTER_CLAMPED_ATTR` + ``add_note``）—— ``exc``
    正是 ``retry_async`` 随后传给 ``on_retry`` 的同一个对象，故这次截断与普通
    重试走同一条 ledger 通道，不会静默。

    并非所有 SDK 异常都带 ``response``（属性访问逐级 guarded）：任一环节缺失、
    值不可解析为数字、或为负数/非有限值时返回 ``None``，由 ``retry_async``
    回落到自身计算的指数退避。绝不抛异常 —— 该函数跑在重试引擎内部。
    """
    for candidate in _exception_chain(exc):
        seconds = _retry_after_header(candidate)
        if seconds is None:
            continue
        if seconds > MAX_RETRY_AFTER_SECONDS:
            _note_clamped(exc, seconds)
            return MAX_RETRY_AFTER_SECONDS
        return seconds
    return None


def _note_clamped(exc: BaseException, requested: float) -> None:
    """在 ``exc`` 上留痕：原始要求写入属性，并附一条人可读的 note（3.11+）。"""
    setattr(exc, RETRY_AFTER_CLAMPED_ATTR, requested)
    add_note = getattr(exc, "add_note", None)  # Python < 3.11 无 PEP 678
    if callable(add_note):
        add_note(
            f"Retry-After={requested:g}s 超过上限 {MAX_RETRY_AFTER_SECONDS:g}s，"
            f"已截断为 {MAX_RETRY_AFTER_SECONDS:g}s（服务端要求的等待不合理）")


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` 及其 ``__cause__`` / ``__context__`` 链（带环保护）。"""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _retry_after_header(exc: BaseException) -> float | None:
    """单个异常对象上的 ``Retry-After`` 解析（缺什么就返回 ``None``）。"""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    raw = getter("Retry-After")
    if raw is None:
        raw = getter("retry-after")  # 普通 dict 不做大小写归一（httpx.Headers 会）
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


class OpenAICompatProvider(BaseProvider):
    """OpenAI Chat Completions 兼容 Provider。

    ``client`` 用于注入（测试或复用连接）；为 ``None`` 时才延迟 import SDK 并
    构造 ``openai.AsyncOpenAI``。``on_retry`` 由调用方（Task 21 的 factory）
    注入 ledger 记账回调，直接在每次重试前转发给 ``retry_async``。
    """

    def __init__(
        self,
        binding: ModelBinding,
        *,
        api_key: str | None,
        client: Any | None = None,
        on_retry: Callable[[int, Exception, float], None] | None = None,
    ) -> None:
        super().__init__(binding)
        self._on_retry = on_retry
        if client is None:
            from openai import AsyncOpenAI

            # max_retries=0：重试语义由 retry_async 独家负责（spec §17.6），
            # 否则 SDK 的内部重试会叠加在本引擎之上，且对 ledger 不可见。
            client = AsyncOpenAI(
                api_key=api_key, base_url=binding.base_url, max_retries=0)
        self._client = client

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        """调用 Chat Completions；``latency_ms`` 为含重试等待的总耗时。"""
        kwargs: dict[str, Any] = {
            "model": self.binding.model,
            "messages": self._prepare_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        started = time.perf_counter()
        response = await retry_async(
            lambda: self._create(kwargs),
            attempts=_ATTEMPTS,
            base_delay=_BASE_DELAY,
            retry_on=(TransientError,),
            on_retry=self._on_retry,
            retry_after=retry_after_seconds,
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        usage = getattr(response, "usage", None)
        return CompletionResult(
            text=response.choices[0].message.content or "",
            provider=self.binding.provider,
            model=self.binding.model,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            latency_ms=latency_ms,
        )

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        """单次 SDK 调用；异常在重试引擎看到之前翻译成类型化错误。

        捕获 ``Exception`` 而非具体 SDK 类型：注入的 fake client 与各 SDK 抛出
        的异常类各不相同，而本 try 只包住 SDK 调用本身。``BaseException`` 不在
        此列，故 ``CancelledError`` 等照常穿透。
        """
        try:
            return await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise classify_http_error(exc)(str(exc)) from exc

    def _prepare_messages(self, messages: Sequence[Message]) -> list[dict[str, str]]:
        """``supports_system_role=False`` 时把 system 内容并入首条 user 消息。"""
        prepared = [dict(message) for message in messages]
        if self.capabilities.supports_system_role:
            return prepared
        system_parts = [m["content"] for m in prepared if m["role"] == "system"]
        if not system_parts:
            return prepared
        system_text = "\n\n".join(system_parts)
        rest = [m for m in prepared if m["role"] != "system"]
        for message in rest:
            if message["role"] == "user":
                message["content"] = f"{system_text}\n\n{message['content']}"
                return rest
        # 没有 user 消息（异常调用方）也不能丢 system 内容：作为首条 user 发出。
        return [{"role": "user", "content": system_text}, *rest]
