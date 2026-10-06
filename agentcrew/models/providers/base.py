"""Provider 抽象基类与传输结构（spec §12.2）。

本模块只定义接口：真实 Provider 在 Task 18/19 实现，本任务不导入任何 SDK。
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from agentcrew.models.config import ModelBinding, ProviderCapabilities


class Message(TypedDict):
    """一次对话中的单条消息。"""

    role: Literal["system", "user", "assistant"]
    content: str


class _ResponseFormatRequired(TypedDict):
    type: str


class ResponseFormat(_ResponseFormatRequired, total=False):
    """OpenAI 风格的结构化输出请求。

    ``json_schema`` 只在降级链第 1 级（``supports_json_schema``）出现；
    第 2 级只有 ``{"type": "json_object"}``。
    """

    json_schema: dict


class CompletionResult(BaseModel):
    """一次 ``complete()`` 调用的结果（含用量与延迟）。"""

    model_config = ConfigDict(extra="forbid")

    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    #: provider 给出的停止原因（OpenAI 兼容端点 ``choices[0].finish_reason``，
    #: Anthropic ``stop_reason``）。``"length"`` / ``"max_tokens"`` 表示回复是
    #: **被输出上限截断的**，不是模型写完了 —— 实测真实运行里架构师的回复正好
    #: 停在 8192，围栏因此未闭合，而平台把它当成「格式错误」，用同一个上限重试
    #: 三次，三次都必然失败。留下这个字段，上层才能区分「写砸了」和「没写完」。
    finish_reason: str | None = None
    #: P3-2：本结果是否来自响应缓存命中。真实调用产出为 ``False``；从
    #: :class:`~agentcrew.models.cache.LLMCache` 命中返回时由工厂包装成 ``True``，
    #: 不消耗 token / 成本。默认 ``False`` 对既有结果逐字节兼容。
    from_cache: bool = False


class StreamChunk(BaseModel):
    """流式响应中的一个增量块（P3-3）。

    ``delta`` 是本次新增的文本片段（不含历史内容，调用方自行拼接）；
    ``finish_reason`` 只在最后一个块上携带（与 :attr:`CompletionResult.finish_reason`
    同语义），中间块为 ``None``。刻意不带 usage：token 用量在流结束后由工厂
    按拼接全文统一估算/汇总（与 ``complete()`` 同一条记账路径）。
    """

    model_config = ConfigDict(extra="forbid")

    delta: str
    finish_reason: str | None = None


class BaseProvider(ABC):
    """所有 Provider 的统一接口。

    ``capabilities`` 委托给 ``binding.capabilities``，不复制字段，避免两者漂移。
    """

    def __init__(self, binding: ModelBinding) -> None:
        self.binding = binding

    @property
    def capabilities(self) -> ProviderCapabilities:
        """本 Provider 的能力位（即其绑定的能力位）。"""
        return self.binding.capabilities

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        """对 ``messages`` 生成一次补全。"""
        raise NotImplementedError

    async def stream_complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式生成补全：逐块 yield :class:`StreamChunk`（P3-3）。

        默认实现退化为一次性 :meth:`complete` 后 yield 一个完整块 —— 不支持真
        流式的 Provider（或未覆写时）仍可工作，只是失去实时性。子类（真实 SSE
        Provider、MockProvider）覆写本方法以分块产出。

        这是异步生成器方法（含 ``yield``），调用方用 ``async for`` 消费；流结束
        后由工厂层统一记账（token/成本/``llm_call`` 事件）。
        """
        result = await self.complete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        yield StreamChunk(delta=result.text, finish_reason=result.finish_reason)
