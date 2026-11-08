"""P2-5：真实流式 + 流式工具调用（``delta.tool_calls`` 增量拼装）。

三块，各自独立可验：

1. **拼装**（纯函数）：分片按 ``index`` 合并、``arguments`` 字符串相加后**一次性**
   解析，坏 JSON 留因不抛；乱序到达也正确。
2. **Provider**：``stream_complete`` 真把 ``stream=True`` 与 ``tools`` 发出去，并把
   ``delta.tool_calls`` 解析成 :class:`ToolCallDelta`（不解析 JSON，那是拼装的事）。
3. **Agent**：流式路径把增量攒完拼成 ``CompletionResult.tool_calls``，ReAct 循环
   照常执行工具——与非流式走**同一条消费路径**（``mode=native``）。
"""

from __future__ import annotations

import json
import types
from typing import Any

import pytest

from agentcrew.agents.generic import GenericAgent
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.models.config import ModelBinding, ProviderCapabilities
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import (
    CompletionResult,
    StreamChunk,
    ToolCallDelta,
    assemble_tool_calls,
)
from agentcrew.models.providers.openai_compat import (
    OpenAICompatProvider,
    parse_tool_call_deltas,
)
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard
from agentcrew.tools import ReadFileTool, ToolRegistry

# ── 1. 拼装 ────────────────────────────────────────────────────────────


def test_fragments_are_concatenated_then_parsed():
    """``arguments`` 是分片到达的 JSON 文本，拼完再解析。"""
    deltas = [
        ToolCallDelta(index=0, id="c1", name="read_file", arguments='{"pa'),
        ToolCallDelta(index=0, arguments='th": "a'),
        ToolCallDelta(index=0, arguments='.txt"}'),
    ]
    calls = assemble_tool_calls(deltas)
    assert len(calls) == 1
    assert calls[0].id == "c1"
    assert calls[0].arguments == {"path": "a.txt"}
    assert calls[0].arguments_error is None


def test_parallel_calls_are_separated_by_index():
    deltas = [
        ToolCallDelta(index=0, id="c1", name="read_file", arguments='{"path": "a"}'),
        ToolCallDelta(index=1, id="c2", name="grep", arguments='{"pattern": "x"}'),
    ]
    calls = assemble_tool_calls(deltas)
    assert [call.name for call in calls] == ["read_file", "grep"]
    assert [call.id for call in calls] == ["c1", "c2"]


def test_out_of_order_fragments_still_assemble():
    """片乱序到达也要对：结果按 index 排序，与到达顺序无关。"""
    deltas = [
        ToolCallDelta(index=1, id="c2", name="grep", arguments='{"pattern": "x"}'),
        ToolCallDelta(index=0, id="c1", name="read_file", arguments='{"path'),
        ToolCallDelta(index=0, arguments='": "a"}'),
    ]
    calls = assemble_tool_calls(deltas)
    assert [call.name for call in calls] == ["read_file", "grep"]
    assert calls[0].arguments == {"path": "a"}


def test_split_inside_a_string_literal_is_handled():
    """最典型的分片边界：把一个字符串字面量从中间切开。"""
    body = json.dumps({"content": "写了一半的内容"}, ensure_ascii=False)
    cut = body.index("一半")
    deltas = [
        ToolCallDelta(index=0, name="write_file", arguments=body[:cut]),
        ToolCallDelta(index=0, arguments=body[cut:]),
    ]
    call = assemble_tool_calls(deltas)[0]
    assert call.arguments["content"] == "写了一半的内容"


def test_broken_json_is_reported_not_raised():
    call = assemble_tool_calls(
        [ToolCallDelta(index=0, name="read_file", arguments="{不是 JSON")])[0]
    assert call.arguments == {}
    assert call.arguments_error and "JSON" in call.arguments_error


def test_missing_name_is_skipped():
    """没有函数名无法执行，连回灌都没有意义 → 直接跳过。"""
    assert assemble_tool_calls([ToolCallDelta(index=0, arguments="{}")]) == []


def test_empty_deltas_mean_no_calls():
    assert assemble_tool_calls([]) == []


def test_parse_tool_call_deltas_tolerates_none_and_dicts():
    assert parse_tool_call_deltas(None) == []
    assert parse_tool_call_deltas("nonsense") == []
    raw = [types.SimpleNamespace(
        index=0, id="c1",
        function=types.SimpleNamespace(name="read_file", arguments='{"pa'))]
    deltas = parse_tool_call_deltas(raw)
    assert deltas[0].name == "read_file" and deltas[0].arguments == '{"pa'
    # dict 形状（部分网关）也认
    deltas = parse_tool_call_deltas(
        [{"index": 2, "id": "c9", "function": {"name": "grep", "arguments": "{}"}}])
    assert deltas[0].index == 2 and deltas[0].name == "grep"
    assert deltas[0].id == "c9"


# ── 2. Provider：真 SSE ───────────────────────────────────────────────


def _binding(**caps: bool) -> ModelBinding:
    return ModelBinding.model_validate(dict(
        alias="openai:test", provider="openai", model="test-model",
        implementation="openai_compat", base_url="http://127.0.0.1:1/v1",
        api_key_env="", capabilities=ProviderCapabilities(**caps),
    ))


class _Delta:
    def __init__(self, content: str | None = None, tool_calls: list[Any] | None = None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta: _Delta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _Event:
    def __init__(self, delta: _Delta | None, finish_reason: str | None = None) -> None:
        self.choices = [] if delta is None else [_Choice(delta, finish_reason)]


class _StreamingCompletions:
    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self.payloads: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.payloads.append(kwargs)
        script = self._script

        async def iterator():
            for item in script:
                yield item

        return iterator()


class _Client:
    def __init__(self, script: list[Any]) -> None:
        self.chat = types.SimpleNamespace(
            completions=_StreamingCompletions(script))


TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "读", "parameters": {"type": "object"}}}]


async def _collect(provider: OpenAICompatProvider) -> list[StreamChunk]:
    return [chunk async for chunk in provider.stream_complete(
        [{"role": "user", "content": "读一下"}],
        temperature=0.0, max_tokens=64, tools=TOOLS, tool_choice="auto")]


async def test_stream_sends_stream_flag_and_tools():
    client = _Client([_Event(_Delta("hi"), "stop")])
    provider = OpenAICompatProvider(_binding(supports_tool_calls=True),
                                    api_key="k", client=client)
    await _collect(provider)
    payload = client.chat.completions.payloads[0]
    assert payload["stream"] is True
    assert payload["tools"] == TOOLS
    assert payload["tool_choice"] == "auto"


async def test_stream_parses_text_and_tool_deltas_in_order():
    fragments = ['{"path"', ': "a.txt"', "}"]
    script = [
        _Event(_Delta("", [types.SimpleNamespace(
            index=0, id="c1",
            function=types.SimpleNamespace(name="read_file", arguments=fragments[0]))])),
        *[_Event(_Delta("", [types.SimpleNamespace(
            index=0, id=None,
            function=types.SimpleNamespace(name=None, arguments=frag))]))
          for frag in fragments[1:]],
        _Event(_Delta("我先读一下。")),
        _Event(_Delta(None), "tool_calls"),
    ]
    client = _Client(script)
    provider = OpenAICompatProvider(_binding(supports_tool_calls=True),
                                    api_key="k", client=client)
    chunks = await _collect(provider)

    deltas = [d for chunk in chunks for d in chunk.tool_call_deltas]
    calls = assemble_tool_calls(deltas)
    assert calls[0].name == "read_file" and calls[0].arguments == {"path": "a.txt"}
    assert "".join(chunk.delta for chunk in chunks) == "我先读一下。"
    assert chunks[-1].finish_reason == "tool_calls"


async def test_stream_skips_frames_without_delta():
    """只有用量/keepalive 的帧不产出空块（否则调用方会收到一串空 delta）。"""
    client = _Client([_Event(None), _Event(_Delta("x"), None),
                      _Event(_Delta(None), "stop")])
    provider = OpenAICompatProvider(_binding(), api_key="k", client=client)
    chunks = await _collect(provider)
    assert [chunk.delta for chunk in chunks] == ["x", ""]
    assert chunks[-1].finish_reason == "stop"


# ── 3. Agent：流式 + 原生工具调用 ─────────────────────────────────────


FINAL = "```python file=a.py\ndef f():\n    return 1\n```\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts", run_id="R",
                          ledger=ledger)
    spec = ArtifactSpec(path="a.py", kind="python", description="d",
                        must_contain=["def f"])
    task = Task(id="T-1", title="t", description="写 a.py", role="backend_dev",
                acceptance_criteria=["可运行"], deliverables=[spec])
    board = TaskBoard([task])
    bus = ContextBus("目标", board, store)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "notes.txt").write_text("先看这个说明：函数返回 1。", encoding="utf-8")
    return dict(settings=settings, ledger=ledger, store=store, task=task,
                bus=bus, proj=proj)


def _role() -> RoleSpec:
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件", system_prompt="你是后端工程师。")


def _text_resp(text: str) -> CompletionResult:
    return CompletionResult(text=text, provider="mock", model="m",
                            prompt_tokens=0, completion_tokens=0, latency_ms=0)


async def test_streaming_agent_assembles_native_tool_calls(env):
    """流式路径拼出的 tool_calls 必须和流式文本一样被消费（mode=native）。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    models.supports_native_tools = lambda alias: True   # type: ignore[method-assign]
    agent = GenericAgent(_role(), models=models, store=env["store"],
                        ledger=env["ledger"], project_goal="目标",
                        tool_registry=registry, stream=True)

    # 一次调用 = 一串 chunk；工具调用的 JSON 被切在两块里（真实流式就是这样）
    frames = [
        [
            StreamChunk(delta="", tool_call_deltas=[ToolCallDelta(
                index=0, id="s1", name="read_file", arguments='{"path": "no')]),
            StreamChunk(delta="", tool_call_deltas=[ToolCallDelta(
                index=0, arguments='tes.txt"}')], finish_reason="tool_calls"),
        ],
        [StreamChunk(delta=FINAL, finish_reason="stop")],
    ]
    seen_tools: list[Any] = []

    async def fake_stream(alias, messages, **kw):
        seen_tools.append(kw.get("tools"))
        for frame in frames.pop(0):
            yield frame

    agent._models.stream_call = fake_stream
    await agent.execute(env["task"], env["bus"].build(env["task"]))

    # 工具真的执行了（读到了文件内容），且走的是原生路径
    event = list(env["ledger"].events("tool_call"))[-1].data
    assert event["tool_name"] == "read_file"
    assert event["mode"] == "native"
    assert event["success"] is True
    # 流式调用也带上了 tools 声明
    assert seen_tools[0] and seen_tools[0][0]["function"]["name"] == "read_file"


async def test_streaming_without_tool_deltas_is_plain_text(env):
    """没有工具增量时，流式行为与改造前一致（不产生 tool_call 事件）。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    models.supports_native_tools = lambda alias: True   # type: ignore[method-assign]
    agent = GenericAgent(_role(), models=models, store=env["store"],
                        ledger=env["ledger"], project_goal="目标",
                        tool_registry=registry, stream=True)

    async def fake_stream(alias, messages, **kw):
        yield StreamChunk(delta=FINAL[:20], finish_reason=None)
        yield StreamChunk(delta=FINAL[20:], finish_reason="stop")

    agent._models.stream_call = fake_stream
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert list(env["ledger"].events("tool_call")) == []
    assert env["store"].read("a.py").startswith("def f()")


async def test_streaming_agent_passes_tools_only_when_available(env):
    """没有工具时不传 tools——流式与非流式同一条口径。"""
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    models.supports_native_tools = lambda alias: True   # type: ignore[method-assign]
    agent = GenericAgent(_role(), models=models, store=env["store"],
                        ledger=env["ledger"], project_goal="目标",
                        tool_registry=None, stream=True)
    captured: list[Any] = []

    async def fake_stream(alias, messages, **kw):
        captured.append(kw.get("tools"))
        yield StreamChunk(delta=FINAL, finish_reason="stop")

    agent._models.stream_call = fake_stream
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert captured == [None]
