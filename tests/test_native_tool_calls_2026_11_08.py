"""P2-1：原生 function calling（tools / tool_choice / tool_call_id / 并行）。

分三层验证，每层只断言行为：

1. **Provider 层**（``OpenAICompatProvider``）：带 ``tools`` 时请求体里真的有
   ``tools`` / ``tool_choice``；响应里的 ``message.tool_calls`` 被解析成
   :class:`ToolCallRequest`；坏 ``arguments`` 不抛异常而是留原因；续传消息里的
   ``tool_calls`` / ``tool_call_id`` 原样透传。
2. **能力位**：默认 ``supports_tool_calls=False`` → 走的还是围栏（零行为变化）；
   打开后 ``ModelFactory.supports_native_tools`` 为真。
3. **Agent 层**：同一条任务、同一个工具，用「原生 tool_calls」和「文本围栏」两条
   路径各跑一遍，**最终产物与工具执行结果必须一致**（方案 P2 的验收标准）。
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest

from agentcrew.agents.generic import GenericAgent
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.models.config import (
    ModelBinding,
    ProviderCapabilities,
    parse_alias,
)
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import (
    CompletionResult,
    ToolCallRequest,
)
from agentcrew.models.providers.openai_compat import (
    OpenAICompatProvider,
    parse_tool_calls,
)
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard
from agentcrew.tools import ReadFileTool, ToolRegistry

# ── fake SDK：只认本用例需要的形状 ─────────────────────────────────────


class _Fn:
    def __init__(self, name: str, arguments: Any) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, ident: str, name: str, arguments: Any) -> None:
        self.id = ident
        self.type = "function"
        self.function = _Fn(name, arguments)


class _Message:
    def __init__(self, content: str | None, tool_calls: list[Any] | None = None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message: _Message, finish_reason: str) -> None:
        self.message = message
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, message: _Message, finish_reason: str) -> None:
        self.choices = [_Choice(message, finish_reason)]
        self.usage = types.SimpleNamespace(prompt_tokens=11, completion_tokens=7)


class _Completions:
    def __init__(self, script: list[_Response]) -> None:
        self._script = script
        self.payloads: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Response:
        self.payloads.append(kwargs)
        return self._script.pop(0)


class _Client:
    def __init__(self, script: list[_Response]) -> None:
        self.chat = types.SimpleNamespace(completions=_Completions(script))


def _binding(**caps: bool) -> ModelBinding:
    return ModelBinding.model_validate(dict(
        alias="openai:test", provider="openai", model="test-model",
        implementation="openai_compat", base_url="http://127.0.0.1:1/v1",
        api_key_env="", capabilities=ProviderCapabilities(**caps),
    ))


def _provider(script: list[_Response], **caps: bool) -> tuple[OpenAICompatProvider, _Client]:
    client = _Client(script)
    return OpenAICompatProvider(_binding(**caps), api_key="k", client=client), client


TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}]

# ── 1. Provider 层 ─────────────────────────────────────────────────────


async def test_tools_and_tool_choice_reach_the_request():
    provider, client = _provider(
        [_Response(_Message("好的", [_ToolCall("c1", "read_file",
                                             '{"path": "a.txt"}')]), "tool_calls")],
        supports_tool_calls=True)

    result = await provider.complete(
        [{"role": "user", "content": "读一下 a.txt"}],
        temperature=0.0, max_tokens=64, tools=TOOLS, tool_choice="auto")

    payload = client.chat.completions.payloads[0]
    assert payload["tools"] == TOOLS
    assert payload["tool_choice"] == "auto"
    assert [call.name for call in result.tool_calls] == ["read_file"]
    assert result.tool_calls[0].arguments == {"path": "a.txt"}
    assert result.tool_calls[0].id == "c1"
    assert result.finish_reason == "tool_calls"


async def test_no_tools_means_payload_unchanged():
    """不带工具时请求体与改造前逐字节一致（不多出 tools/tool_choice 键）。"""
    provider, client = _provider([_Response(_Message("hi"), "stop")])
    await provider.complete([{"role": "user", "content": "x"}],
                            temperature=0.0, max_tokens=8)
    payload = client.chat.completions.payloads[0]
    assert "tools" not in payload and "tool_choice" not in payload


async def test_broken_arguments_are_reported_not_raised():
    """坏 JSON 不炸：留空参数 + 原因，交给下一轮回灌。"""
    provider, _ = _provider([_Response(
        _Message("", [_ToolCall("c1", "read_file", "{不是 JSON")]), "tool_calls")],
        supports_tool_calls=True)
    result = await provider.complete([{"role": "user", "content": "x"}],
                                     temperature=0.0, max_tokens=8, tools=TOOLS)
    call = result.tool_calls[0]
    assert call.arguments == {}
    assert call.arguments_error and "JSON" in call.arguments_error


def test_parse_tool_calls_tolerates_weird_shapes():
    assert parse_tool_calls(None) == []
    assert parse_tool_calls("nonsense") == []
    # 没有 function.name 的条目跳过（无法执行）
    assert parse_tool_calls([_ToolCall("c1", "", "{}")]) == []
    # arguments 是对象（有些网关直接给 dict）
    calls = parse_tool_calls([_ToolCall("c2", "t", {"a": 1})])
    assert calls[0].arguments == {"a": 1}
    # JSON 数组不是对象 → 报原因
    calls = parse_tool_calls([_ToolCall("c3", "t", "[1, 2]")])
    assert calls[0].arguments == {} and calls[0].arguments_error


async def test_prepare_messages_passes_tool_fields_through():
    provider, client = _provider([_Response(_Message("done"), "stop")])
    await provider.complete([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "content": "文件内容", "tool_call_id": "c1"},
        {"role": "user", "content": "继续"},
    ], temperature=0.0, max_tokens=8)
    messages = client.chat.completions.payloads[0]["messages"]
    assert messages[0]["tool_calls"][0]["id"] == "c1"
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "c1"


# ── 2. 能力位 ──────────────────────────────────────────────────────────


def test_capability_defaults_to_false():
    """默认全 False：既有部署不会因为这次改动突然带上 tools 参数。"""
    assert ProviderCapabilities().supports_tool_calls is False
    assert parse_alias("openai:gpt-4o-mini").capabilities.supports_tool_calls is False


# ── 3. Agent 层：原生 vs 围栏，同任务同结果 ────────────────────────────

REPLY_FINAL = "```python file=a.py\ndef f():\n    return 1\n```\n"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
                bus=bus, proj=proj, tmp_path=tmp_path)


def _role() -> RoleSpec:
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件",
                    system_prompt="你是后端工程师。")


def _resp(text: str, calls: list[ToolCallRequest] | None = None) -> CompletionResult:
    return CompletionResult(text=text, provider="mock", model="m",
                            prompt_tokens=1, completion_tokens=10,
                            latency_ms=0, tool_calls=calls or [])


def _agent(env, registry: ToolRegistry, native: bool):
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    # 只替换能力探测，不动真实调用：下面 fake_call 直接给结果。
    models.supports_native_tools = lambda alias: native   # type: ignore[method-assign]
    return GenericAgent(_role(), models=models, store=env["store"],
                        ledger=env["ledger"], project_goal="目标",
                        tool_registry=registry)


async def _run(env, native: bool) -> tuple[str, str, dict[str, Any]]:
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    agent = _agent(env, registry, native)
    if native:
        queue = [
            _resp("", [ToolCallRequest(id="call_1", name="read_file",
                                      arguments={"path": "notes.txt"})]),
            _resp(REPLY_FINAL),
        ]
    else:
        queue = [
            _resp('<tool_call>\n{"name": "read_file", "args": {"path": '
                  '"notes.txt"}}\n</tool_call>\n我先读一下说明。'),
            _resp(REPLY_FINAL),
        ]
    seen: list[list[dict[str, Any]]] = []
    payloads: list[Any] = []

    async def fake_call(alias, messages, **kw):
        seen.append([dict(m) for m in messages])
        payloads.append(kw.get("tools"))
        return queue.pop(0)

    agent._models.call = fake_call
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    artifact = env["store"].read("a.py")
    event = list(env["ledger"].events("tool_call"))[-1].data
    return artifact, event["mode"], {
        "messages": seen, "payloads": payloads, "event": event}


async def test_native_and_fence_paths_produce_the_same_result(env):
    """方案 P2 的验收标准：原生与回退**同任务同结果**。"""
    native_artifact, native_mode, native_ctx = await _run(env, native=True)
    env["ledger"].__init__  # noqa: B018 —— 同一 ledger 复用即可，事件按 mode 区分
    fence_artifact, fence_mode, fence_ctx = await _run(env, native=False)

    assert native_mode == "native" and fence_mode == "fence"
    # 产物逐字节相同
    assert native_artifact == fence_artifact
    assert native_artifact.startswith("def f()")
    # 工具都被真的执行了：第二轮消息里都带回了文件内容
    for ctx in (native_ctx, fence_ctx):
        joined = "\n".join(m["content"] for m in ctx["messages"][1])
        assert "先看这个说明" in joined
    # 两条路径的 ledger 事件内容一致（除了 mode）
    assert native_ctx["event"]["tool_name"] == fence_ctx["event"]["tool_name"]
    assert native_ctx["event"]["success"] is fence_ctx["event"]["success"] is True


async def test_native_path_uses_tool_role_messages(env):
    _, mode, ctx = await _run(env, native=True)
    assert mode == "native"
    second = ctx["messages"][1]
    # system, user, assistant(tool_calls), tool
    assert second[2]["role"] == "assistant"
    assert second[2]["tool_calls"][0]["id"] == "call_1"
    assert second[3]["role"] == "tool"
    assert second[3]["tool_call_id"] == "call_1"
    assert "先看这个说明" in second[3]["content"]


async def test_native_path_sends_tool_specs(env):
    _, _, ctx = await _run(env, native=True)
    specs = ctx["payloads"][0]
    assert specs and specs[0]["function"]["name"] == "read_file"
    assert specs[0]["type"] == "function"
    # 第二次调用同样带 tools（续传时端点要求声明一致）
    assert ctx["payloads"][1]


async def test_fence_path_keeps_user_tool_result_block(env):
    _, mode, ctx = await _run(env, native=False)
    assert mode == "fence"
    second = ctx["messages"][1]
    assert second[2]["role"] == "assistant"
    assert second[3]["role"] == "user"
    assert '<tool_result name="read_file">' in second[3]["content"]
    assert ctx["payloads"][0] is None, "不支持原生时不应传 tools"


async def test_broken_native_arguments_are_fed_back(env):
    """原生 arguments 坏掉时：工具不执行，原因回灌，下一轮还能继续。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    agent = _agent(env, registry, native=True)
    queue = [
        _resp("", [ToolCallRequest(id="call_1", name="read_file",
                                  arguments={}, arguments_error="坏 JSON")]),
        _resp(REPLY_FINAL),
    ]

    async def fake_call(alias, messages, **kw):
        return queue.pop(0)

    agent._models.call = fake_call
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    event = list(env["ledger"].events("tool_call"))[-1].data
    assert event["success"] is False
    assert event["mode"] == "native"


def test_native_system_prompt_does_not_teach_fences(env):
    """原生模式下提示词不再以围栏为主（避免两套协议同时下发）。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    agent = _agent(env, registry, native=True)
    messages = agent.build_messages(
        env["task"], env["bus"].build(env["task"]), (), native_tools=True)
    system = messages[0]["content"]
    assert "函数调用（tools）" in system
    assert "调用方式：在回复中插入一个 `<tool_call>` 块" not in system


def test_json_roundtrip_of_tool_call_request():
    """ToolCallRequest 是 pydantic 模型：能进 ledger / 快照而不炸。"""
    call = ToolCallRequest(id="c1", name="read_file", arguments={"path": "a"})
    dumped = json.loads(call.model_dump_json())
    assert dumped["name"] == "read_file" and dumped["id"] == "c1"
