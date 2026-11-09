"""P2-3：OTel GenAI 语义约定 + 零依赖 OTLP/HTTP 导出。

**为什么自己写导出器**：方案 §4.3 的结论是「借 ``gen_ai.*`` 语义、先 OTLP 输出，
不早期自建重型后端」。官方 ``opentelemetry-sdk`` 会拖进一串依赖（api/sdk/exporter
三件套 + protobuf），而本项目对依赖白名单极严（HTTP 服务、SDK、工具库全是零依赖
手写）。OTLP/HTTP **支持 JSON 编码**，``urllib`` + ``json`` 就够——所以这里只实现
「够用的一条通道」：

- 一次 run 一个 trace（:attr:`OTLPTracer.trace_id`），run 是根 span；
- 子 span：``agentcrew.task``（一次任务派发）、``chat {model}``（一次 LLM 调用，
  带 ``gen_ai.*``）、``agentcrew.tool``（一次工具调用）；
- 默认**不采集** prompt / completion 内容（``otel_capture_content=false`）：
  内容里可能有源码、密钥、用户数据，采集开关必须是显式的运维决定；
- 导出失败**绝不影响 run**：``flush()`` 返回错误结构，Crew 落一条
  ``otel_export_failed`` 事件后继续收尾——可观测性组件不许成为新的故障点。

**语义约定取用范围**（``open-telemetry/semantic-conventions-genai``）：

| 属性 | 用在哪 |
|---|---|
| ``gen_ai.operation.name`` / ``gen_ai.system`` | LLM span 必备 |
| ``gen_ai.request.model`` / ``gen_ai.response.model`` | 请求模型 / 实际模型 |
| ``gen_ai.request.temperature`` / ``gen_ai.request.max_tokens`` | 采样参数 |
| ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens`` | 用量 |
| ``gen_ai.response.finish_reasons`` | 停止原因（截断排查靠它） |
| ``gen_ai.tool.name`` / ``gen_ai.tool.call.id`` | 工具 span |

不自定义新的 ``gen_ai.*`` 键：加了就不是「按约定输出」，接进 Langfuse / Phoenix /
Tempo 时会变成只有本项目认得的字段。
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_ENDPOINT",
    "MAX_BUFFERED_SPANS",
    "OTLPTracer",
    "Span",
    "encode_attribute",
]

#: OTLP/HTTP 默认端口（collector 的 ``http/protobuf`` 与 ``http/json`` 共用 4318）。
DEFAULT_ENDPOINT = "http://127.0.0.1:4318"

#: 内存里最多攒多少个 span。超了丢弃并记数——宁可少几条 trace，不可让可观测性
#: 组件把长跑 run 的内存吃穿。
MAX_BUFFERED_SPANS = 5000

#: 导出超时（秒）。collector 挂了不能把 run 的收尾卡住。
EXPORT_TIMEOUT_SECONDS = 3.0

#: 采集内容时每条内容最多保留多少字符。
_MAX_CONTENT_CHARS = 4000

#: OTLP 的 span kind（``SPAN_KIND_INTERNAL`` = 1）。本项目全用 internal：
#: 没有跨进程 RPC，server/client 语义在这里没有意义。
_KIND_INTERNAL = 1

#: OTLP 的 status code（``STATUS_CODE_OK`` = 1 / ``STATUS_CODE_ERROR`` = 2）。
_STATUS_OK = 1
_STATUS_ERROR = 2


def encode_attribute(value: Any) -> dict[str, Any]:
    """Python 值 → OTLP ``AnyValue``。

    OTLP JSON 里整数必须**以字符串形式**给出（``intValue`` 是 64 位有符号，JSON
    数字会丢精度）；布尔与浮点各自有独立键；其余一律按字符串（``None`` 由调用方
    过滤，不在这里编造语义）。
    """
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attributes(items: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """过滤 ``None`` 后编码成 OTLP 的 ``[{key, value}]`` 列表。"""
    if not items:
        return []
    return [
        {"key": str(key), "value": encode_attribute(value)}
        for key, value in items.items()
        if value is not None
    ]


@dataclass
class Span:
    """一个已结束（或正在跑）的 span。``end_ns is None`` 表示还没结束。"""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    start_ns: int = field(default_factory=lambda: time.time_ns())
    end_ns: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    def to_otlp(self) -> dict[str, Any]:
        """转成 OTLP JSON 的单个 span 对象。"""
        end = self.end_ns if self.end_ns is not None else self.start_ns
        payload: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": _KIND_INTERNAL,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(end),
            "attributes": _attributes(self.attributes),
            "status": {
                "code": _STATUS_ERROR if self.status == "error" else _STATUS_OK,
            },
        }
        if self.parent_span_id:
            payload["parentSpanId"] = self.parent_span_id
        return payload


class OTLPTracer:
    """一次 run 一个 trace 的轻量 tracer（缓冲 + 一次性 OTLP/HTTP 导出）。

    线程/协程安全性：本类只被「同一个 run 的调度线程 + 事件循环」使用，内部不做
    加锁；``flush()`` 由 Crew 在收尾时**单次**调用（``asyncio.to_thread``），不会
    与仍在跑的 worker 并发——真出现并发也只影响那一份 buffer 的完整性，不影响 run。
    """

    def __init__(
        self,
        *,
        service_name: str = "agentcrew",
        endpoint: str = DEFAULT_ENDPOINT,
        capture_content: bool = False,
        timeout: float = EXPORT_TIMEOUT_SECONDS,
        max_spans: int = MAX_BUFFERED_SPANS,
    ) -> None:
        self.service_name = service_name
        self.endpoint = endpoint.rstrip("/")
        self.capture_content = capture_content
        self.timeout = timeout
        self.max_spans = max(1, max_spans)
        self.trace_id = secrets.token_hex(16)   # 16 字节 → 32 hex
        self._spans: list[Span] = []
        self._dropped = 0
        self._root: Span | None = None

    # ── span 生命周期 ────────────────────────────────────────────────────

    def start_span(
        self, name: str, *,
        parent: Span | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Span:
        """开一个 span；不给 ``parent`` 时挂到根 span 上（根自身除外）。"""
        if len(self._spans) >= self.max_spans:
            self._dropped += 1
            return Span(name=name, trace_id=self.trace_id,
                        span_id=secrets.token_hex(8), attributes={},
                        status="dropped")
        parent_span = parent or self._root
        span = Span(
            name=name,
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=parent_span.span_id if parent_span is not None else None,
            attributes=dict(attributes or {}),
        )
        if self._root is None:
            self._root = span
        self._spans.append(span)
        return span

    def end_span(
        self, span: Span, *,
        status: str = "ok",
        attributes: Mapping[str, Any] | None = None,
    ) -> Span:
        """结束一个 span（重复结束是幂等的：保留第一次的结束时间）。"""
        if span.end_ns is None:
            span.end_ns = time.time_ns()
        if attributes:
            span.attributes.update(attributes)
        if status == "error":
            span.status = "error"
        return span

    @contextmanager
    def span(
        self, name: str, *,
        parent: Span | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        """``with tracer.span(...) as sp:`` —— 异常时自动标 ``error`` 并继续抛出。

        注意：内部会 ``await`` 也没问题（span 只是记账），但**跨任务**使用会让
        parent 关系错乱，所以调用方应保证在同一协程内进出。
        """
        span = self.start_span(name, parent=parent, attributes=attributes)
        try:
            yield span
        except BaseException:
            self.end_span(span, status="error")
            raise
        else:
            self.end_span(span)

    def record_llm(
        self, *,
        model: str,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        finish_reason: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_model: str | None = None,
        error: str | None = None,
        parent: Span | None = None,
        alias: str | None = None,
        request_preview: str | None = None,
        response_preview: str | None = None,
    ) -> Span:
        """记一次 LLM 调用（``gen_ai.*`` 语义）。

        内容（prompt / completion）只在 ``capture_content`` 为真时附加，且截断到
        :data:`_MAX_CONTENT_CHARS`——默认不采集是刻意的。
        """
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": provider,
            "gen_ai.request.model": model,
            "gen_ai.response.model": response_model or model,
            "gen_ai.usage.input_tokens": prompt_tokens,
            "gen_ai.usage.output_tokens": completion_tokens,
            "gen_ai.request.temperature": temperature,
            "gen_ai.request.max_tokens": max_tokens,
            "gen_ai.response.finish_reasons": (
                [finish_reason] if finish_reason else None),
            "agentcrew.latency_ms": latency_ms,
            "agentcrew.model_alias": alias,
        }
        if error:
            attributes["error.message"] = error[:500]
        if self.capture_content:
            if request_preview:
                attributes["gen_ai.request.content"] = request_preview[
                    :_MAX_CONTENT_CHARS]
            if response_preview:
                attributes["gen_ai.response.content"] = response_preview[
                    :_MAX_CONTENT_CHARS]
        span = self.start_span(
            f"chat {model}", parent=parent, attributes=attributes)
        span.end_ns = span.start_ns + max(0, int(latency_ms)) * 1_000_000
        if error:
            span.status = "error"
        return span

    def record_tool(
        self, *, tool_name: str, task_id: str, role: str,
        success: bool, duration_ms: int, call_id: str = "",
        error: str | None = None, parent: Span | None = None,
    ) -> Span:
        """记一次工具调用（``gen_ai.tool.*``）。"""
        attributes: dict[str, Any] = {
            "gen_ai.tool.name": tool_name,
            "gen_ai.tool.call.id": call_id or None,
            "agentcrew.task_id": task_id,
            "agentcrew.role": role,
            "agentcrew.tool.success": success,
            "agentcrew.tool.duration_ms": duration_ms,
        }
        if error:
            attributes["error.message"] = error[:500]
        span = self.start_span(
            f"execute_tool {tool_name}", parent=parent, attributes=attributes)
        span.end_ns = span.start_ns + max(0, int(duration_ms)) * 1_000_000
        if not success:
            span.status = "error"
        return span

    # ── 导出 ────────────────────────────────────────────────────────────

    @property
    def span_count(self) -> int:
        return len(self._spans)

    @property
    def dropped_spans(self) -> int:
        return self._dropped

    def payload(self) -> dict[str, Any]:
        """组装 OTLP/HTTP JSON 请求体（纯计算，可直接断言）。"""
        attributes = _attributes({
            "service.name": self.service_name,
            "service.version": "agentcrew",
            "agentcrew.trace_id": self.trace_id,
            "agentcrew.dropped_spans": self._dropped or None,
        })
        return {"resourceSpans": [{
            "resource": {"attributes": attributes},
            "scopeSpans": [{
                "scope": {"name": "agentcrew", "version": "1"},
                "spans": [span.to_otlp() for span in self._spans],
            }],
        }]}

    def flush(self) -> dict[str, Any]:
        """导出并清空；**任何异常都收敛成返回值**，绝不向上抛。

        返回 ``{"ok": bool, "spans": N, "endpoint": ..., "error": str|None}``。
        """
        spans = len(self._spans)
        if not spans:
            return {"ok": True, "spans": 0, "endpoint": self.endpoint,
                    "error": None}
        body = json.dumps(self.payload(), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/v1/traces",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        error: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                # OTLP 成功响应是 200 + 空 body（JSON 编码时可能是 {}）
                if getattr(resp, "status", 200) >= 300:
                    error = f"HTTP {getattr(resp, 'status', '?')}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            self._spans.clear()
        return {"ok": error is None, "spans": spans,
                "endpoint": self.endpoint, "error": error}
