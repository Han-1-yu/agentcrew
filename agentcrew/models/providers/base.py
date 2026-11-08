"""Provider 抽象基类与传输结构（spec §12.2）。

本模块只定义接口：真实 Provider 在 Task 18/19 实现，本任务不导入任何 SDK。
"""

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.models.config import ModelBinding, ProviderCapabilities


class _OptionalMessageFields(TypedDict, total=False):
    """消息的可选字段（P2-1 原生工具调用）。

    单列一个 ``total=False`` 的基类再继承，是为了让 ``role`` / ``content`` 仍是
    **必填**（写漏 content 会被类型检查抓住），同时允许 ``tool_calls`` /
    ``tool_call_id`` 只在需要时出现——用 ``typing.NotRequired`` 也行，但那是 3.11+
    才有的名字，本项目支持 3.10。
    """

    tool_calls: list[dict[str, Any]]
    """assistant 消息里的原生工具调用（OpenAI 形状，原样回传即可）。"""

    tool_call_id: str
    """role="tool" 的消息对应的调用 id（与 assistant 的 tool_calls[].id 对齐）。"""


class Message(_OptionalMessageFields):
    """一次对话中的单条消息。

    ``role`` 增加 ``"tool"``：原生工具调用必须用「assistant(tool_calls) →
    tool(tool_call_id)」这一对来回传，否则兼容端点会拒绝请求。文本围栏回退
    路径不产生这类消息，因此对既有行为零影响。
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ToolCallRequest(BaseModel):
    """模型请求调用一次工具（Provider 解析后的统一形状，P2-1）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    """Provider 给的调用 id；文本围栏路径为空串（那时没有 id 可用）。"""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    arguments_error: str | None = None
    """``arguments`` 不是合法 JSON 对象时的原因（保留原文不丢失）。

    不抛异常：模型写出坏 JSON 是**可回灌的**错误——把原因交给下一轮，模型自己
    改。抛异常则等于把一次格式失误升级成任务失败。
    """


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
    #: P2-1：本次响应里的原生工具调用（空列表 = 没有）。
    #: 与 ``text`` 里的 ``<tool_call>`` 围栏是**两条来源、同一个消费端**：
    #: 有原生调用时优先用它，没有才回退到围栏解析。默认空列表，对既有
    #: 结果逐字节兼容。
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)


class ToolCallDelta(BaseModel):
    """一片工具调用增量（流式专用）。

    与 :class:`ToolCallRequest` 的区别：这里 ``arguments`` 是**半截 JSON 字符串**，
    不是解析后的对象。解析只在 :func:`assemble_tool_calls` 里做一次。
    """

    model_config = ConfigDict(extra="forbid")

    index: int = 0
    id: str = ""
    name: str = ""
    arguments: str = ""


def assemble_tool_calls(deltas: Sequence[ToolCallDelta]) -> list[ToolCallRequest]:
    """把分片增量按 ``index`` 拼成完整的 :class:`ToolCallRequest` 列表。

    拼接规则（对齐 OpenAI 的流式约定）：

    - ``index`` 相同的片属于同一次调用；``id`` / ``name`` 取第一个非空值；
    - ``arguments`` 按到达顺序**字符串相加**，最后一次性 ``json.loads``；
    - 解析失败不抛异常，返回 ``arguments_error``（与非流式路径同一口径）。

    乱序到达也安全：结果按 ``index`` 排序，与片到达顺序无关。
    """
    merged: dict[int, ToolCallDelta] = {}
    for delta in deltas:
        current = merged.get(delta.index)
        if current is None:
            merged[delta.index] = ToolCallDelta(
                index=delta.index,
                id=delta.id,
                name=delta.name,
                arguments=delta.arguments,
            )
            continue
        if delta.id and not current.id:
            current.id = delta.id
        if delta.name and not current.name:
            current.name = delta.name
        if delta.arguments:
            current.arguments += delta.arguments
    calls: list[ToolCallRequest] = []
    for index in sorted(merged):
        piece = merged[index]
        if not piece.name:
            continue
        arguments: dict[str, Any] = {}
        error: str | None = None
        body = piece.arguments.strip()
        if body:
            try:
                parsed = json.loads(body)
            except ValueError as exc:
                error = f"arguments 不是合法 JSON：{exc}"
            else:
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    error = (
                        f"arguments 必须是 JSON 对象，实际是 {type(parsed).__name__}")
        calls.append(ToolCallRequest(
            id=piece.id, name=piece.name,
            arguments=arguments, arguments_error=error))
    return calls


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
    #: P2-5：本块携带的工具调用**增量**（OpenAI 的 ``delta.tool_calls`` 形状）。
    #: 流式下工具调用是分片到达的：``index`` 标识是哪一次调用，``name`` 只在
    #: 第一片出现，``arguments`` 是 JSON 文本的片段（可能从中间切开一个字符串
    #: 字面量，所以**必须按 index 拼接后再解析**，不能逐片解析）。
    tool_call_deltas: list[ToolCallDelta] = Field(default_factory=list)


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
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> CompletionResult:
        """对 ``messages`` 生成一次补全。

        ``tools`` / ``tool_choice``（P2-1）是 OpenAI Chat Completions 形状的工具
        声明：``[{"type": "function", "function": {name, description, parameters}}]``。
        **只有 ``capabilities.supports_tool_calls`` 为真的 Provider 会被传这两个
        参数**；不支持的原生实现的签名照样接受并忽略它们（保持接口同形，符合
        LSP），这样调用方不需要按类型分支。
        """
        raise NotImplementedError

    async def stream_complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式生成补全：逐块 yield :class:`StreamChunk`（P3-3）。

        默认实现退化为一次性 :meth:`complete` 后 yield 一个完整块 —— 不支持真
        流式的 Provider（或未覆写时）仍可工作，只是失去实时性。子类（真实 SSE
        Provider、MockProvider）覆写本方法以分块产出。

        这是异步生成器方法（含 ``yield``），调用方用 ``async for`` 消费；流结束
        后由工厂层统一记账（token/成本/``llm_call`` 事件）。
        """
        # P2-5：默认实现把 ``tools`` 原样透传给 ``complete()``——一次性返回的
        # 形态下工具调用本来就是完整的，不需要增量拼装。
        kwargs: dict[str, Any] = (
            {"tools": tools, "tool_choice": tool_choice} if tools else {})
        result = await self.complete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            **kwargs,
        )
        yield StreamChunk(
            delta=result.text,
            finish_reason=result.finish_reason,
            # 没覆写真流式的 Provider：把完整 tool_calls 当成「一片」交给上层，
            # 上层的拼装逻辑对它同样成立（index 全 0 会合并成一次调用）。
            tool_call_deltas=[
                ToolCallDelta(
                    index=index,
                    id=call.id,
                    name=call.name,
                    arguments=json.dumps(call.arguments, ensure_ascii=False)
                    if call.arguments else "",
                )
                for index, call in enumerate(result.tool_calls)
            ],
        )
