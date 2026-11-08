"""P2-3：OTel GenAI 语义 + 零依赖 OTLP/HTTP 导出。

三层：

1. **payload 结构**：OTLP JSON 的 resource / scopeSpans / span 字段齐全，
   一次 run 共享一个 ``traceId``，子 span 的 ``parentSpanId`` 指向根；
2. **语义约定**：LLM span 带 ``gen_ai.*``（含 usage / finish_reasons），工具 span
   带 ``gen_ai.tool.*``；**默认不采集内容**，显式打开才带且截断；
3. **导出**：真起一个本地 HTTP 端点收 POST，断言路径 ``/v1/traces``、body 可解析、
   导出后缓冲清空；端点不可达 / 5xx 时 ``ok=false`` 且**不抛异常**（可观测性不许
   打断 run）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agentcrew.config import Settings
from agentcrew.models.config import ModelBinding, ProviderCapabilities
from agentcrew.models.providers.base import CompletionResult
from agentcrew.runtime.otel import (
    DEFAULT_ENDPOINT,
    OTLPTracer,
    encode_attribute,
)

# ── 1. payload 结构 ────────────────────────────────────────────────────


def _spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return {
        item["key"]: next(iter(item["value"].values()))
        for item in span.get("attributes", [])
    }


def test_resource_carries_service_name():
    tracer = OTLPTracer(service_name="agentcrew-test")
    tracer.start_span("agentcrew.run")
    resource = _spans(tracer.payload()) and tracer.payload()["resourceSpans"][0]
    attributes = {
        item["key"]: next(iter(item["value"].values()))
        for item in resource["resource"]["attributes"]
    }
    assert attributes["service.name"] == "agentcrew-test"


def test_one_run_is_one_trace_with_parent_links():
    tracer = OTLPTracer()
    root = tracer.start_span("agentcrew.run")
    tracer.record_llm(model="m", provider="openai", prompt_tokens=1,
                      completion_tokens=2, latency_ms=5)
    tracer.end_span(root)
    spans = _spans(tracer.payload())

    assert {span["traceId"] for span in spans} == {tracer.trace_id}
    assert len({span["spanId"] for span in spans}) == 2
    # 第二个 span 挂着根
    assert spans[1]["parentSpanId"] == spans[0]["spanId"]
    # 根没有父
    assert "parentSpanId" not in spans[0]


def test_span_times_are_nanoseconds_and_ordered():
    tracer = OTLPTracer()
    tracer.record_llm(model="m", provider="p", prompt_tokens=0,
                      completion_tokens=0, latency_ms=120)
    span = _spans(tracer.payload())[0]
    start = int(span["startTimeUnixNano"])
    end = int(span["endTimeUnixNano"])
    assert end - start == 120 * 1_000_000, "LLM span 时长取自 provider 报告的延迟"


def test_attribute_encoding_types():
    assert encode_attribute(True) == {"boolValue": True}
    assert encode_attribute(7) == {"intValue": "7"}, "OTLP 的 int 必须转字符串"
    assert encode_attribute(1.5) == {"doubleValue": 1.5}
    assert encode_attribute("x") == {"stringValue": "x"}


def test_dropped_spans_are_counted_not_fatal():
    tracer = OTLPTracer(max_spans=2)
    for _ in range(5):
        tracer.start_span("x")
    assert tracer.span_count == 2
    assert tracer.dropped_spans == 3


# ── 2. 语义约定 ────────────────────────────────────────────────────────


def test_llm_span_uses_gen_ai_semantic_conventions():
    tracer = OTLPTracer()
    tracer.record_llm(model="gpt-4o-mini", provider="openai",
                      prompt_tokens=120, completion_tokens=30, latency_ms=800,
                      finish_reason="stop", temperature=0.2, max_tokens=4096,
                      alias="openai:gpt-4o-mini")
    span = _spans(tracer.payload())[0]
    assert span["name"] == "chat gpt-4o-mini"
    attributes = _attrs(span)
    assert attributes["gen_ai.operation.name"] == "chat"
    assert attributes["gen_ai.system"] == "openai"
    assert attributes["gen_ai.request.model"] == "gpt-4o-mini"
    assert attributes["gen_ai.response.model"] == "gpt-4o-mini"
    assert attributes["gen_ai.usage.input_tokens"] == "120"
    assert attributes["gen_ai.usage.output_tokens"] == "30"
    assert attributes["gen_ai.request.temperature"] == 0.2
    assert attributes["gen_ai.request.max_tokens"] == "4096"
    assert attributes["agentcrew.model_alias"] == "openai:gpt-4o-mini"


def test_content_is_not_captured_by_default():
    tracer = OTLPTracer()
    tracer.record_llm(model="m", provider="p", prompt_tokens=1,
                      completion_tokens=1, latency_ms=1,
                      request_preview="机密提示词",
                      response_preview="机密回复")
    attributes = _attrs(_spans(tracer.payload())[0])
    assert "gen_ai.request.content" not in attributes
    assert "gen_ai.response.content" not in attributes


def test_content_is_captured_when_enabled_and_truncated():
    tracer = OTLPTracer(capture_content=True)
    tracer.record_llm(model="m", provider="p", prompt_tokens=1,
                      completion_tokens=1, latency_ms=1,
                      request_preview="长" * 10_000,
                      response_preview="回复")
    attributes = _attrs(_spans(tracer.payload())[0])
    assert attributes["gen_ai.response.content"] == "回复"
    assert len(attributes["gen_ai.request.content"]) <= 4000


def test_tool_span_uses_tool_conventions():
    tracer = OTLPTracer()
    tracer.record_tool(tool_name="read_file", task_id="T-1",
                       role="backend_dev", success=False, duration_ms=12,
                       call_id="c1", error="文件不存在")
    span = _spans(tracer.payload())[0]
    assert span["name"] == "execute_tool read_file"
    assert span["status"]["code"] == 2, "失败的工具调用必须标 error"
    attributes = _attrs(span)
    assert attributes["gen_ai.tool.name"] == "read_file"
    assert attributes["gen_ai.tool.call.id"] == "c1"
    assert attributes["agentcrew.tool.success"] is False
    assert attributes["error.message"] == "文件不存在"


def test_exception_inside_span_marks_error_and_reraises():
    tracer = OTLPTracer()
    with pytest.raises(RuntimeError):
        with tracer.span("boom"):
            raise RuntimeError("x")
    span = _spans(tracer.payload())[0]
    assert span["status"]["code"] == 2


# ── 3. 导出 ────────────────────────────────────────────────────────────


async def _start_collector() -> tuple[asyncio.AbstractServer, list[dict[str, Any]], int]:
    """起一个最小 OTLP 收集端：收 POST，记录 path 与 body。"""
    received: list[dict[str, Any]] = []

    async def handler(reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.decode("latin-1").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            body = await reader.readexactly(length) if length else b""
            received.append({
                "path": head.decode("latin-1").split(" ")[1],
                "body": json.loads(body.decode("utf-8") or "{}"),
            })
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, received, port


async def test_flush_posts_otlp_json_to_v1_traces():
    server, received, port = await _start_collector()
    try:
        tracer = OTLPTracer(endpoint=f"http://127.0.0.1:{port}")
        tracer.start_span("agentcrew.run")
        result = await asyncio.to_thread(tracer.flush)
        assert result["ok"] is True, result
        assert result["spans"] == 1
        assert received[0]["path"] == "/v1/traces"
        assert _spans(received[0]["body"])[0]["name"] == "agentcrew.run"
        # 导出后缓冲清空，重复导出是空操作
        assert tracer.span_count == 0
        assert await asyncio.to_thread(tracer.flush) == {
            "ok": True, "spans": 0, "endpoint": tracer.endpoint, "error": None}
    finally:
        server.close()
        await server.wait_closed()


def test_flush_failure_is_returned_not_raised():
    """端点不可达：ok=false + 错误文案，绝不抛异常（可观测性不许打断 run）。"""
    tracer = OTLPTracer(endpoint="http://127.0.0.1:1", timeout=0.5)
    tracer.start_span("agentcrew.run")
    result = tracer.flush()
    assert result["ok"] is False
    assert result["spans"] == 1
    assert result["error"]
    assert tracer.span_count == 0, "失败也要清空缓冲，否则内存只涨不落"


def test_flush_of_empty_buffer_is_noop():
    tracer = OTLPTracer()
    assert tracer.flush()["ok"] is True
    assert tracer.flush()["spans"] == 0


# ── 4. 与工厂 / Crew 的接线 ────────────────────────────────────────────


class _FakeProviderTracer:
    """只记录调用的假 tracer，用来验证工厂真的记了 span。"""

    capture_content = False

    def __init__(self) -> None:
        self.llm_calls: list[dict[str, Any]] = []

    def record_llm(self, **kwargs: Any) -> None:
        self.llm_calls.append(kwargs)


def _binding() -> ModelBinding:
    return ModelBinding.model_validate(dict(
        alias="mock:m", provider="mock", model="m", implementation="mock",
        base_url=None, api_key_env="", capabilities=ProviderCapabilities(),
    ))


def test_factory_records_llm_span_without_content():
    from agentcrew.models.factory import ModelFactory

    tracer = _FakeProviderTracer()
    factory = ModelFactory(Settings.load(), tracer=tracer)  # type: ignore[arg-type]
    result = CompletionResult(text="SECRET-BODY", provider="mock", model="m",
                              prompt_tokens=3, completion_tokens=4,
                              latency_ms=10, finish_reason="stop")
    factory._record_llm_span(result, binding=_binding(), alias="mock:m",
                             temperature=0.1, max_tokens=64)
    call = tracer.llm_calls[0]
    assert call["model"] == "m" and call["prompt_tokens"] == 3
    assert call["response_preview"] is None, "默认不采集内容"


def test_factory_without_tracer_is_noop():
    from agentcrew.models.factory import ModelFactory

    factory = ModelFactory(Settings.load())
    assert factory.tracer is None
    # 直接调用不炸（内部 early return）
    factory._record_llm_span(
        CompletionResult(text="", provider="mock", model="m", prompt_tokens=0,
                         completion_tokens=0, latency_ms=0),
        binding=_binding(), alias=None, temperature=0.0, max_tokens=1)


def test_otel_disabled_by_default():
    settings = Settings.load()
    assert settings.otel_enabled is False
    assert settings.otel_capture_content is False
    assert settings.otel_endpoint == DEFAULT_ENDPOINT


def test_otel_env_switch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTCREW_OTEL_ENABLED", "true")
    monkeypatch.setenv("AGENTCREW_OTEL_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("AGENTCREW_OTEL_SERVICE_NAME", "agentcrew-ci")
    settings = Settings.load()
    assert settings.otel_enabled is True
    assert settings.otel_capture_content is True
    assert settings.otel_service_name == "agentcrew-ci"


def test_crew_build_tracer_respects_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agentcrew.crew.crew import Crew
    from agentcrew.runtime.checkpoint import CheckpointStore

    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    store = CheckpointStore.create(
        tmp_path / "o", project_goal="g", config_snapshot={},
        run_id="20260101-000000-otel")
    record = store.load_record()

    assert Crew(Settings.load())._build_tracer(record) is None

    monkeypatch.setenv("AGENTCREW_OTEL_ENABLED", "true")
    tracer = Crew(Settings.load())._build_tracer(record)
    assert tracer is not None and tracer.trace_id
