"""Provider 抽象基类与传输结构（spec §12.2）。

本模块只定义接口：真实 Provider 在 Task 18/19 实现，本任务不导入任何 SDK。
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
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
