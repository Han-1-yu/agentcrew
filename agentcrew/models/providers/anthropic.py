"""Anthropic 原生 Messages API Provider（spec §12.1）。

设计要点：

- **SDK 延迟 import**：只在 ``client is None`` 时于构造函数内 import
  ``anthropic.AsyncAnthropic``（Python 3 绝对导入：模块名与本文件同名不冲突）。
  测试注入 fake client，未安装 ``anthropic`` 的环境也能 import 本模块。
- **system 提到顶层参数**：Anthropic Messages API 的 system 不是 messages 里的
  角色，而是顶层 ``system=``；无 system 消息时**整个键都不传**。
- **只拼 text 块**：响应可能含 thinking/tool_use 等非 text 块，盲拼会污染输出。
- **``response_format`` 静默忽略**：Anthropic 没有这个参数，能力位已声明不支持，
  降级到围栏提取由 ``models/structured.py``（Task 20）决定。
- **错误分类与重试复用 Task 18** 的 ``classify_http_error`` / ``retry_after_seconds``，
  不另写第二套分类器。
"""

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
from agentcrew.models.providers.openai_compat import (
    classify_http_error,
    retry_after_seconds,
)
from agentcrew.utils.retry import (
    TransientError,
    retry_async,
)

#: 生产重试参数（spec §17.6），与 OpenAI 兼容 Provider 保持一致。
_ATTEMPTS = 4
_BASE_DELAY = 1.0


class AnthropicProvider(BaseProvider):
    """Anthropic Messages API Provider。

    ``client`` 用于注入（测试或复用连接）；为 ``None`` 时才延迟 import SDK 并
    构造 ``anthropic.AsyncAnthropic``。``on_retry`` 由调用方（Task 21 的 factory）
    注入 ledger 记账回调，直接在每次重试前转发给 ``retry_async``。
    """

    def __init__(
        self,
        binding: ModelBinding,
        *,
        api_key: str | None,
        client: Any | None = None,
        on_retry: Callable[[int, Exception, float], None] | None = None,
        request_timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(binding)
        self._on_retry = on_retry
        if client is None:
            from anthropic import AsyncAnthropic

            # max_retries=0：重试语义由 retry_async 独家负责（spec §17.6）。
            # A-9：显式请求超时（默认 120s），避免单次调用 4×600s 放大。
            client = AsyncAnthropic(
                api_key=api_key, base_url=binding.base_url, max_retries=0,
                timeout=request_timeout_seconds)
        self._client = client

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> CompletionResult:
        """调用 Messages API；``response_format`` / ``tools`` 静默忽略。

        ``latency_ms`` 为含重试等待的总耗时。

        P2-1 只在 OpenAI 兼容实现上落地原生工具调用；Anthropic 的 tool_use
        形状不同，单独适配是另一件事，这里保持同名参数以维持接口同形。
        """
        system, chat_messages = self._split_system(messages)
        kwargs: dict[str, Any] = {
            "model": self.binding.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
        }
        if system is not None:
            kwargs["system"] = system
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
            text="".join(b.text for b in response.content if b.type == "text"),
            provider=self.binding.provider,
            model=self.binding.model,
            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            latency_ms=latency_ms,
            # Anthropic 用 ``stop_reason``，达到上限时是 ``"max_tokens"``。
            finish_reason=getattr(response, "stop_reason", None),
        )

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        """单次 SDK 调用；异常在重试引擎看到之前翻译成类型化错误。

        捕获 ``Exception`` 而非具体 SDK 类型：注入的 fake client 与各 SDK 抛出
        的异常类各不相同，而本 try 只包住 SDK 调用本身。``BaseException`` 不在
        此列，故 ``CancelledError`` 等照常穿透。
        """
        try:
            return await self._client.messages.create(**kwargs)
        except Exception as exc:
            raise classify_http_error(exc)(str(exc)) from exc

    @staticmethod
    def _split_system(
        messages: Sequence[Message],
    ) -> tuple[str | None, list[dict[str, str]]]:
        """拆出 system 内容（多条以空行拼接）与其余消息；无 system 时返回 ``None``。"""
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        chat_messages: list[dict[str, str]] = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] != "system"
        ]
        if not system_parts:
            return None, chat_messages
        return "\n\n".join(system_parts), chat_messages
