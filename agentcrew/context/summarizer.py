"""LLM 摘要式上下文压缩（V2 第 1 项）。

上下文组装（:class:`ContextBus.build`）是同步的，而 LLM 调用是异步的，因此
摘要分两步：

1. **预取**（async）：Agent 在调用 ``build()`` 之前调用
   :meth:`ContextBus.prefetch_summaries`，识别出预算装不下的上游摘要，用 LLM
   压缩后写入缓存。
2. **组装**（sync）：``build()`` 检查缓存，命中时用摘要替代原文，从而在同样
   预算下装入更多上游信息。

``Summarizer`` 是协议，不直接依赖 :class:`ModelFactory`（避免循环导入）：
``LLMSummarizer`` 接收一个 ``SummarizeFn`` 可调用对象，由 Crew 装配时注入。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, TypeAlias

from agentcrew.utils.tokens import estimate_tokens

if TYPE_CHECKING:
    from agentcrew.models.factory import ModelFactory

#: 摘要调用的类型：(原文, 目标 token 上限) → 摘要文本。
SummarizeFn: TypeAlias = Callable[[str, int], Coroutine[None, None, str]]


class Summarizer(ABC):
    """文本压缩器协议。"""

    @abstractmethod
    async def summarize(self, text: str, max_tokens: int) -> str:
        """把 ``text`` 压缩到不超过 ``max_tokens``。"""
        raise NotImplementedError


class NullSummarizer(Summarizer):
    """无操作压缩器：按行截断到 ``max_tokens``，不调用 LLM。

    用于未配置摘要模型时的降级路径，以及测试。
    """

    async def summarize(self, text: str, max_tokens: int) -> str:
        if estimate_tokens(text) <= max_tokens:
            return text
        lines = text.splitlines(keepends=True)
        low, high = 0, len(lines)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_tokens("".join(lines[:middle])) <= max_tokens:
                low = middle
            else:
                high = middle - 1
        truncated = "".join(lines[:low])
        return truncated + "\n…（内容已截断，未启用 LLM 摘要）"


class LLMSummarizer(Summarizer):
    """用 LLM 压缩文本。

    ``fn`` 由 Crew 注入（通常包装 :class:`ModelFactory.call`），
    本类只负责提示词构造与长度守卫。
    """

    _SYSTEM_PROMPT = (
        "你是一个上下文压缩器。请把用户提供的文本压缩成不超过 {max_tokens} token 的"
        "摘要，保留关键决策、接口签名、文件路径和验收标准，去掉重复表述和修饰语。"
        "直接输出摘要，不要加前缀或解释。"
    )

    def __init__(self, fn: SummarizeFn) -> None:
        self._fn = fn

    async def summarize(self, text: str, max_tokens: int) -> str:
        if estimate_tokens(text) <= max_tokens:
            return text
        prompt = self._SYSTEM_PROMPT.format(max_tokens=max_tokens)
        summary = await self._fn(f"{prompt}\n\n{text}", max_tokens)
        # 守卫：LLM 可能输出超过上限，兜底截断。
        if estimate_tokens(summary) > max_tokens:
            lines = summary.splitlines(keepends=True)
            low, high = 0, len(lines)
            while low < high:
                middle = (low + high + 1) // 2
                if estimate_tokens("".join(lines[:middle])) <= max_tokens:
                    low = middle
                else:
                    high = middle - 1
            summary = "".join(lines[:low])
        return summary


def summarizer_from_factory(
    models: ModelFactory, *, alias: str = "default", purpose: str = "context_summarize"
) -> LLMSummarizer:
    """从 :class:`ModelFactory` 构造一个 :class:`LLMSummarizer`。

    摘要调用走工厂的受治理入口（限流 + 记账 + ledger），与 Agent 的 LLM 调用
    同一条通路。``alias`` 缺省为 ``"default"``（即 Settings.default_model），
    也可指定更便宜的模型做摘要（如 ``"openai:gpt-4o-mini"``）。
    """
    from agentcrew.models.providers.base import Message

    # ``"default"`` 只是「未指定」的哨兵，不是可解析的别名：在这里就把它解析成
    # 配置里的默认模型，否则会原样传进 ``models.call`` → ``parse_alias`` 报错
    # 「模型别名缺少 ':'：'default'」。
    resolved_alias = models.settings.default_model if alias == "default" else alias

    async def _fn(text: str, max_tokens: int) -> str:
        messages: list[Message] = [
            {"role": "user", "content": text},
        ]
        result = await models.call(
            resolved_alias, messages,
            temperature=0.3, max_tokens=max_tokens,
            purpose=purpose, role="context_summarizer",
        )
        return result.text

    return LLMSummarizer(_fn)
