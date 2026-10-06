"""``BaseAgent`` ReAct 工具循环的行为测试（P1-1）。

用伪造的 ``models.call`` 排队返回多段回复，驱动：系统提示词注入工具说明、
工具调用循环、无 tool_call 的单次调用（向后兼容）、``tool_registry=None`` 行为
不变、``max_tool_calls`` 降级、工具不存在/未授权的错误回灌，以及 ledger 的
``tool_call`` 事件留痕。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.agents.base_agent import AgentExecutionError, BaseAgent
from agentcrew.agents.generic import GenericAgent
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import CompletionResult
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard
from agentcrew.tools import ReadFileTool, Tool, ToolRegistry


class _SecretTool(Tool):
    """不在角色白名单里的工具：验证未授权调用被拒。"""

    description: str = "秘密工具"

    async def run(self, **kwargs):
        return "SECRET-DATA"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts", run_id="R", ledger=lg)
    spec = ArtifactSpec(path="a.py", kind="python", description="d",
                        must_contain=["def f"])
    task = Task(id="T-1", title="t", description="写 a.py", role="backend_dev",
                acceptance_criteria=["可运行"], deliverables=[spec])
    board = TaskBoard([task])
    bus = ContextBus("目标", board, store)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "notes.txt").write_text("先看这个说明：函数返回 1。", encoding="utf-8")
    return dict(settings=s, ledger=lg, store=store, task=task, bus=bus,
                proj=proj, tmp_path=tmp_path)


def _role() -> RoleSpec:
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件", system_prompt="你是后端工程师。")


def _resp(text: str) -> CompletionResult:
    return CompletionResult(text=text, provider="mock", model="m",
                            prompt_tokens=1, completion_tokens=10, latency_ms=0)


def _make_agent(env, registry: ToolRegistry | None = None, **kw) -> GenericAgent:
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    return GenericAgent(_role(), models=models, store=env["store"],
                        ledger=env["ledger"], project_goal="目标",
                        tool_registry=registry, **kw)


# ── 系统提示词注入工具说明 ────────────────────────────────────────────

def test_system_prompt_lists_tools_when_registry_present(env):
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    agent = _make_agent(env, registry)
    messages = agent.build_messages(env["task"], env["bus"].build(env["task"]), ())
    system = messages[0]["content"]
    assert "read_file" in system
    assert "<tool_call>" in system
    assert "tool_result" in system


def test_system_prompt_has_no_tool_section_without_registry(env):
    agent = _make_agent(env, None)
    messages = agent.build_messages(env["task"], env["bus"].build(env["task"]), ())
    system = messages[0]["content"]
    assert "<tool_call>" not in system
    assert "可用工具" not in system


# ── ReAct 主循环 ──────────────────────────────────────────────────────

async def test_react_loop_calls_tool_then_commits_artifact(env):
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    agent = _make_agent(env, registry)

    reply_with_call = (
        '<tool_call>\n{"name": "read_file", "args": {"path": "notes.txt"}}\n'
        "</tool_call>\n我先读一下说明。"
    )
    reply_final = "```python file=a.py\ndef f():\n    return 1\n```\n"
    queue = [_resp(reply_with_call), _resp(reply_final)]
    seen: list[list[dict]] = []

    async def fake_call(alias, messages, **kw):
        seen.append([dict(m) for m in messages])
        return queue.pop(0)

    agent._models.call = fake_call
    result = await agent.execute(env["task"], env["bus"].build(env["task"]))

    assert env["store"].read("a.py").startswith("def f()")
    assert result.artifacts
    # 工具真的被执行了：第二次 LLM 调用的 messages 里带回了文件内容。
    second_messages = seen[1]
    assert len(second_messages) == 4  # system, user, assistant, user(tool_result)
    joined = "\n".join(m["content"] for m in second_messages)
    assert "先看这个说明" in joined
    # ledger 留痕
    events = list(env["ledger"].events("tool_call"))
    assert len(events) == 1
    assert events[0].data["tool_name"] == "read_file"
    assert events[0].data["success"] is True


async def test_no_tool_call_is_single_compatible_call(env):
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    agent = _make_agent(env, registry)
    queue = [_resp("```python file=a.py\ndef f():\n    return 1\n```\n")]

    async def fake_call(alias, messages, **kw):
        return queue.pop(0)

    agent._models.call = fake_call
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["ledger"].count("tool_call") == 0


async def test_registry_none_is_unchanged_behavior(env, tmp_path):
    agent = _make_agent(env, None)
    # 用真实 mock 夹具：tool_registry=None 时连一次额外 LLM 调用都不该发生。
    fx = tmp_path / "fx"
    (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    agent._models = ModelFactory(env["settings"], ledger=env["ledger"],
                                 fixtures_dir=fx)
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["store"].read("a.py").startswith("def f()")
    assert env["ledger"].count("tool_call") == 0


# ── 错误处理 ──────────────────────────────────────────────────────────

async def test_nonexistent_tool_returns_error_text(env):
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    agent = _make_agent(env, registry)

    reply_with_call = (
        '<tool_call>\n{"name": "no_such_tool", "args": {}}\n</tool_call>'
    )
    reply_final = "```python file=a.py\ndef f():\n    return 1\n```\n"
    queue = [_resp(reply_with_call), _resp(reply_final)]

    async def fake_call(alias, messages, **kw):
        return queue.pop(0)

    agent._models.call = fake_call
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    event = list(env["ledger"].events("tool_call"))[0]
    assert event.data["tool_name"] == "no_such_tool"
    assert event.data["success"] is False


async def test_unauthorized_tool_is_rejected(env):
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    registry.register(_SecretTool(name="secret_tool"))
    # 角色只放行 read_file，secret_tool 不可见。
    role = _role().model_copy(update={"allowed_tools": ["read_file"]})
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    agent = GenericAgent(role, models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         tool_registry=registry)

    reply_with_call = (
        '<tool_call>\n{"name": "secret_tool", "args": {}}\n</tool_call>'
    )
    reply_final = "```python file=a.py\ndef f():\n    return 1\n```\n"
    queue = [_resp(reply_with_call), _resp(reply_final)]

    async def fake_call(alias, messages, **kw):
        return queue.pop(0)

    agent._models.call = fake_call
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    event = list(env["ledger"].events("tool_call"))[0]
    assert event.data["success"] is False


# ── max_tool_calls 降级 ──────────────────────────────────────────────

async def test_max_tool_calls_degrades_with_warning(env):
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["proj"]))
    agent = _make_agent(env, registry, max_tool_calls=2)

    call1 = '<tool_call>\n{"name": "read_file", "args": {"path": "notes.txt"}}\n</tool_call>'
    # 最后一轮：既想再调工具，也给出最终产物 → 降级时产物仍落盘。
    call2 = (
        "```python file=a.py\ndef f():\n    return 1\n```\n"
        '<tool_call>\n{"name": "read_file", "args": {"path": "notes.txt"}}\n</tool_call>'
    )
    queue = [_resp(call1), _resp(call2)]

    async def fake_call(alias, messages, **kw):
        return queue.pop(0)

    agent._models.call = fake_call
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["ledger"].count("tool_loop_exhausted") == 1
    assert env["store"].read("a.py").startswith("def f()")
