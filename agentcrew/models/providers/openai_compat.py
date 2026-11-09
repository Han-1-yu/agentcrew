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

import json
import math
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from agentcrew.models.config import ModelBinding
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
    StreamChunk,
    ToolCallDelta,
    ToolCallRequest,
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

#: 触发工厂层故障转移但 Provider 内部**不重试**的状态码。
#: 402 余额不足：再打一次同样扣费失败，毫无意义；但应该让工厂层切到备用模型。
#: 这些状态码被翻译成 :class:`~agentcrew.utils.retry.TransientError`（工厂据此
#: 进入 failover 循环），但经 :data:`_should_retry` 过滤后不进入内部退避重试。
_FAILOVER_ONLY_STATUSES = frozenset({402})

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

    :data:`_FAILOVER_ONLY_STATUSES`（如 402 余额不足）也归类为 ``TransientError``，
    让工厂层能进入故障转移；但 :data:`_should_retry` 会拦掉内部重试。
    """
    match = _HTTP_STATUS_RE.search(str(exc))
    if match is not None:
        status = int(match.group(1))
        if status in _TRANSIENT_STATUSES or status in _FAILOVER_ONLY_STATUSES:
            return TransientError
        return PermanentError
    type_name = type(exc).__name__
    if any(marker in type_name for marker in _TRANSIENT_TYPE_MARKERS):
        return TransientError
    return PermanentError


def _should_retry(exc: BaseException) -> bool:
    """``retry_async`` 的 ``retry_filter``：402 余额不足不在 Provider 内部重试。

    402 已被 :func:`classify_http_error` 标成 TransientError（工厂层据此故障转移），
    但余额不足再打一次同样失败 —— 立即上抛给工厂层切备用模型。
    其他 TransientError（429/5xx/超时/连接错误）照常退避重试。
    """
    return getattr(exc, "status_code", None) != 402


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
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> CompletionResult:
        """调用 Chat Completions；``latency_ms`` 为含重试等待的总耗时。

        P2-1：``tools`` 非空时按 OpenAI 原生 function calling 发请求，并把响应里的
        ``message.tool_calls`` 解析成 :class:`ToolCallRequest`。``text`` 仍然照常
        返回（模型可能同时给文本），两条来源都由调用方决定先后。
        """
        kwargs: dict[str, Any] = {
            "model": self.binding.model,
            "messages": self._prepare_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if tools:
            kwargs["tools"] = [dict(item) for item in tools]
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        started = time.perf_counter()
        response = await retry_async(
            lambda: self._create(kwargs),
            attempts=_ATTEMPTS,
            base_delay=_BASE_DELAY,
            retry_on=(TransientError,),
            on_retry=self._on_retry,
            retry_after=retry_after_seconds,
            retry_filter=_should_retry,
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        usage = getattr(response, "usage", None)
        message = response.choices[0].message
        return CompletionResult(
            text=getattr(message, "content", None) or "",
            provider=self.binding.provider,
            model=self.binding.model,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            latency_ms=latency_ms,
            finish_reason=getattr(response.choices[0], "finish_reason", None),
            tool_calls=parse_tool_calls(getattr(message, "tool_calls", None)),
        )

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
        """真 SSE 流式调用（P2-5）：逐块 yield 文本增量与工具调用增量。

        与 :meth:`complete` 的两点差异都是流式本身决定的：

        - **建流可重试、消费不可重试**：``create(stream=True)`` 返回流对象那一步
          仍在 ``retry_async`` 里（连接失败 / 429 / 5xx 重试），但一旦开始
          ``async for``，已吐出的块无法回滚，中途异常直接上抛——由工厂层决定
          是否在**首块之前**切换备用别名。
        - **不返回 tool_calls 成品**，只 yield :class:`ToolCallDelta`：工具调用的
          ``arguments`` 是分片到达的 JSON 文本，拼接与解析统一在
          :func:`assemble_tool_calls` 里做，避免两处各写一遍解析逻辑。
        """
        kwargs: dict[str, Any] = {
            "model": self.binding.model,
            "messages": self._prepare_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if tools:
            kwargs["tools"] = [dict(item) for item in tools]
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        stream = await retry_async(
            lambda: self._create(kwargs),
            attempts=_ATTEMPTS,
            base_delay=_BASE_DELAY,
            retry_on=(TransientError,),
            on_retry=self._on_retry,
            retry_after=retry_after_seconds,
            retry_filter=_should_retry,
        )
        async for event in stream:
            choices = getattr(event, "choices", None) or []
            if not choices:
                continue      # 用量尾帧 / keepalive：没有 delta 就没有可吐的东西
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            text = getattr(delta, "content", None) or ""
            deltas = parse_tool_call_deltas(getattr(delta, "tool_calls", None))
            finish_reason = getattr(choice, "finish_reason", None)
            if not text and not deltas and finish_reason is None:
                continue
            yield StreamChunk(
                delta=text,
                finish_reason=finish_reason,
                tool_call_deltas=deltas,
            )

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        """单次 SDK 调用；异常在重试引擎看到之前翻译成类型化错误。

        捕获 ``Exception`` 而非具体 SDK 类型：注入的 fake client 与各 SDK 抛出
        的异常类各不相同，而本 try 只包住 SDK 调用本身。``BaseException`` 不在
        此列，故 ``CancelledError`` 等照常穿透。

        翻译后的异常上挂 ``status_code`` 属性（HTTP 状态码，可解析时）—— 工厂层
        的冷却追踪据此区分 402 / 429 / 5xx / 404 并设置不同的冷却时长。
        """
        try:
            return await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            cls = classify_http_error(exc)
            new_exc = cls(str(exc))
            match = _HTTP_STATUS_RE.search(str(exc))
            if match is not None:
                # status_code 是运行时挂到异常实例上的（classify 的返回类型没有它）
                new_exc.status_code = int(match.group(1))  # type: ignore[attr-defined]
            raise new_exc from exc

    def _prepare_messages(
        self, messages: Sequence[Message]
    ) -> list[dict[str, Any]]:
        """把内部消息转成 SDK 形状；``tool_calls`` / ``tool_call_id`` 原样透传。

        P2-1：原生续传必须是「assistant 带 ``tool_calls`` → tool 带 ``tool_call_id``」
        的成对消息，少一半兼容端点会直接 400。这里按字段存在与否复制，文本围栏
        路径的消息没有这两个键，产出与改造前逐字节一致。
        """
        prepared: list[dict[str, Any]] = []
        for message in messages:
            item: dict[str, Any] = {
                "role": message["role"], "content": message["content"]}
            tool_calls = message.get("tool_calls")
            if tool_calls:
                item["tool_calls"] = list(tool_calls)
            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                item["tool_call_id"] = tool_call_id
            prepared.append(item)
        if self.capabilities.supports_system_role:
            return prepared
        system_parts = [
            str(m.get("content") or "") for m in prepared if m["role"] == "system"]
        if not system_parts:
            return prepared
        system_text = "\n\n".join(system_parts)
        rest = [m for m in prepared if m["role"] != "system"]
        for msg in rest:
            if msg["role"] == "user":
                msg["content"] = f"{system_text}\n\n{msg['content']}"
                return rest
        # 没有 user 消息（异常调用方）也不能丢 system 内容：作为首条 user 发出。
        return [{"role": "user", "content": system_text}, *rest]


def parse_tool_call_deltas(raw: Any) -> list[ToolCallDelta]:
    """SDK 的 ``delta.tool_calls`` → :class:`ToolCallDelta` 列表（P2-5）。

    流式下每一片只有部分字段：第一片带 ``id`` / ``function.name``，后续片通常
    只有 ``function.arguments`` 的一段。**这里不做任何拼接与 JSON 解析**——那是
    :func:`~agentcrew.models.providers.base.assemble_tool_calls` 的事，一次做完
    才不会出现「两处解析、两种容错」。
    """
    if not raw or not isinstance(raw, list):
        return []
    deltas: list[ToolCallDelta] = []
    for position, item in enumerate(raw):
        function = getattr(item, "function", None)
        if function is None and isinstance(item, dict):
            function = item.get("function")
        raw_args = getattr(function, "arguments", None)
        if raw_args is None and isinstance(function, dict):
            raw_args = function.get("arguments")
        index = getattr(item, "index", None)
        if index is None and isinstance(item, dict):
            index = item.get("index")
        call_id = getattr(item, "id", None)
        if call_id is None and isinstance(item, dict):
            call_id = item.get("id")
        name = getattr(function, "name", None)
        if name is None and isinstance(function, dict):
            name = function.get("name")
        deltas.append(ToolCallDelta(
            index=int(index) if isinstance(index, int) else position,
            id=str(call_id or ""),
            name=str(name or ""),
            arguments=raw_args if isinstance(raw_args, str) else "",
        ))
    return deltas


def parse_tool_calls(raw: Any) -> list[ToolCallRequest]:
    """SDK 的 ``message.tool_calls`` → :class:`ToolCallRequest` 列表（P2-1）。

    容错策略：坏数据**不抛异常**，而是尽量保留信息——

    - ``arguments`` 不是合法 JSON 对象：留空 dict + ``arguments_error`` 说明原因，
      上层把这条错误回灌给模型（它下一轮能自己改）；
    - 缺 ``function.name`` 的条目直接跳过（没有名字无法执行，回灌也无意义）；
    - 传进来不是列表 / 是 ``None``：返回空列表（很多兼容网关会返回 ``None``）。
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    calls: list[ToolCallRequest] = []
    for item in raw:
        function = getattr(item, "function", None)
        if function is None and isinstance(item, dict):
            function = item.get("function")
        name = str(getattr(function, "name", "") or "")
        if not name:
            continue
        raw_args = getattr(function, "arguments", None)
        if raw_args is None and isinstance(function, dict):
            raw_args = function.get("arguments")
        arguments: dict[str, Any] = {}
        error: str | None = None
        if isinstance(raw_args, dict):
            arguments = dict(raw_args)
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
            except ValueError as exc:
                error = f"arguments 不是合法 JSON：{exc}"
            else:
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    error = f"arguments 必须是 JSON 对象，实际是 {type(parsed).__name__}"
        elif raw_args not in (None, ""):
            error = f"arguments 类型不可解析：{type(raw_args).__name__}"
        calls.append(ToolCallRequest(
            id=str(getattr(item, "id", "") or ""),
            name=name,
            arguments=arguments,
            arguments_error=error,
        ))
    return calls
