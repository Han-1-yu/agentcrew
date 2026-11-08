"""P2-4：工具级 guardrails —— 副作用前拦截 + 回灌前遮泄漏。

关键断言只有一条：**``block`` 的工具一次都没被执行**。所以测试里的工具都带
计数器（``hits``），而不是只看回灌文本长什么样——「文本说被拦了、其实已经写了
文件」是这类功能最容易出现的假绿。

另外覆盖：``warn`` 只留痕照常执行、``allow`` 是显式放行、``phase`` 分离、
``field`` 定位、``max_length``、输出相位拦泄漏、ledger 事件、配置加载期校验。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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
from agentcrew.tools import Tool, ToolRegistry
from agentcrew.tools.guard import ToolGuard, build_tool_guard

# ── 1. 规则引擎（纯单元）──────────────────────────────────────────────


def test_input_rule_blocks_by_pattern():
    guard = ToolGuard([{"name": "no-rm", "pattern": r"rm\s+-rf", "action": "block"}])
    hits = guard.check_input("run_python", {"code": "rm -rf /"})
    assert [(h.rule, h.action, h.phase) for h in hits] == [
        ("no-rm", "block", "input")]
    assert guard.has_blocking(hits) is True


def test_input_rule_only_applies_to_listed_tools():
    guard = ToolGuard([{"name": "no-rm", "tools": ["run_python"],
                        "pattern": "rm -rf"}])
    assert guard.check_input("read_file", {"path": "rm -rf"}) == []
    assert guard.check_input("run_python", {"code": "rm -rf"}) != []


def test_field_scoping_skips_when_field_absent():
    guard = ToolGuard([{"name": "big", "field": "content", "max_length": 10}])
    assert guard.check_input("write_file", {"path": "a.txt"}) == []
    assert guard.check_input("write_file", {"content": "x" * 11}) != []


def test_max_length_rule():
    """``field`` 限定后按该字段的长度判；不给 field 则按整个入参 JSON 的长度。"""
    guard = ToolGuard([{"name": "big", "field": "content", "max_length": 5,
                        "action": "warn"}])
    assert guard.check_input("write_file", {"content": "01234"}) == []
    hits = guard.check_input("write_file", {"content": "0123456"})
    assert hits[0].action == "warn" and "超过上限" in hits[0].reason

    # 不给 field：payload 是序列化后的整串，长度自然更长
    loose = ToolGuard([{"name": "big", "max_length": 5}])
    assert loose.check_input("write_file", {"content": "01234"}) != []


def test_allow_action_is_explicit_no_op():
    guard = ToolGuard([{"name": "doc", "pattern": "anything", "action": "allow"}])
    assert guard.check_input("read_file", {"path": "anything"}) == []


def test_phase_isolation():
    guard = ToolGuard([{"name": "leak", "phase": "output", "pattern": "sk-"}])
    assert guard.check_input("read_file", {"path": "sk-1"}) == []
    hits = guard.check_output("read_file", "key=sk-123")
    assert hits and hits[0].phase == "output"


def test_output_phase_uses_whole_text():
    guard = ToolGuard([{"name": "leak", "phase": "output",
                        "pattern": r"AKIA[0-9A-Z]{16}"}])
    assert guard.check_output("read_file", "AKIAIOSFODNN7EXAMPLE") != []
    assert guard.check_output("read_file", "普通内容") == []


def test_build_returns_none_for_empty_rules():
    assert build_tool_guard(None) is None
    assert build_tool_guard([]) is None
    assert build_tool_guard([{"name": "x", "pattern": "y"}]).rule_count == 1


@pytest.mark.parametrize("rule,fragment", [
    ({"pattern": "x"}, "缺少 name"),
    ({"name": "x"}, "必须给出 pattern 或 max_length"),
    ({"name": "x", "pattern": "("}, "不是合法正则"),
    ({"name": "x", "pattern": "y", "action": "deny"}, "必须是 block"),
    ({"name": "x", "pattern": "y", "phase": "before"}, "必须是 input"),
    ({"name": "x", "pattern": "y", "max_length": 0}, "必须是 >= 1"),
    ({"name": "x", "pattern": "y", "tools": "run_python"}, "必须是工具名数组"),
])
def test_config_validation_rejects_bad_rules(rule: dict[str, Any], fragment: str):
    with pytest.raises(ValueError) as exc:
        Settings.model_validate({"tool_guards": [rule]})
    assert fragment in str(exc.value)


def test_yaml_section_must_be_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "agentcrew.yaml"
    cfg.write_text("tool_guards:\n  name: x\n", encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(cfg))
    with pytest.raises(ValueError) as exc:
        Settings.load()
    assert "tool_guards" in str(exc.value)


# ── 2. 执行链集成：block 真的没执行 ────────────────────────────────────


class _CountingTool(Tool):
    """带调用计数的工具：断言「没执行」只能看它。"""

    name: str = "write_file"
    description: str = "写点东西"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }
    #: 调用次数（``block`` 断言就看它）
    hits: int = 0

    async def run(self, **kwargs: Any) -> str:
        self.hits += 1
        return f"已写入 {kwargs.get('content')}"


class _LeakTool(Tool):
    """返回「凭据」的工具：验证输出相位拦截。"""

    name: str = "leak_probe"
    description: str = "读环境"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> str:
        return "OPENAI_API_KEY=sk-live-SECRET"


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
    return dict(settings=settings, ledger=ledger, store=store, task=task,
                bus=bus, tmp_path=tmp_path)


def _role() -> RoleSpec:
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件",
                    system_prompt="你是后端工程师。")


def _resp(text: str) -> CompletionResult:
    return CompletionResult(text=text, provider="mock", model="m",
                            prompt_tokens=1, completion_tokens=10, latency_ms=0)


FINAL = "```python file=a.py\ndef f():\n    return 1\n```\n"


async def _drive(env, tools: list[Tool], guard: ToolGuard | None,
                 reply_first: str) -> tuple[GenericAgent, list[str]]:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    agent = GenericAgent(_role(), models=models, store=env["store"],
                        ledger=env["ledger"], project_goal="目标",
                        tool_registry=registry, tool_guard=guard)
    queue = [_resp(reply_first), _resp(FINAL)]
    seen: list[str] = []

    async def fake_call(alias, messages, **kw):
        seen.append("\n".join(m["content"] for m in messages[1:]))
        return queue.pop(0)

    agent._models.call = fake_call
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    return agent, seen


async def test_block_prevents_tool_execution(env):
    tool = _CountingTool()
    guard = ToolGuard([{"name": "no-secret", "tools": ["write_file"],
                        "pattern": "SECRET", "action": "block"}])
    reply = ('<tool_call>\n{"name": "write_file", "args": '
             '{"content": "SECRET"}}\n</tool_call>')
    _, seen = await _drive(env, [tool], guard, reply)

    assert tool.hits == 0, "被 block 的工具一次都不能执行"
    assert "安全规则拦截" in seen[1], "拦截原因要回灌给模型，让它自己改"
    event = list(env["ledger"].events("tool_guard_triggered"))
    assert len(event) == 1
    assert event[0].data["rule"] == "no-secret"
    assert event[0].data["phase"] == "input"
    assert event[0].data["action"] == "block"
    # 工具调用本身也留痕（success=False）
    assert list(env["ledger"].events("tool_call"))[-1].data["success"] is False


async def test_warn_allows_execution_but_leaves_trace(env):
    tool = _CountingTool()
    guard = ToolGuard([{"name": "suspicious", "tools": ["write_file"],
                        "pattern": "SECRET", "action": "warn"}])
    reply = ('<tool_call>\n{"name": "write_file", "args": '
             '{"content": "SECRET"}}\n</tool_call>')
    await _drive(env, [tool], guard, reply)

    assert tool.hits == 1, "warn 只留痕，不拦执行"
    assert list(env["ledger"].events("tool_call"))[-1].data["success"] is True
    assert list(env["ledger"].events("tool_guard_triggered"))


async def test_output_phase_blocks_leak_from_being_fed_back(env):
    tool = _LeakTool()
    guard = ToolGuard([{"name": "no-credential", "phase": "output",
                        "pattern": r"sk-[A-Za-z0-9-]+", "action": "block"}])
    reply = '<tool_call>\n{"name": "leak_probe", "args": {}}\n</tool_call>'
    _, seen = await _drive(env, [tool], guard, reply)

    assert "sk-live-SECRET" not in seen[1], "泄漏内容绝不能回灌给模型"
    assert "输出被安全规则拦截" in seen[1]
    assert list(env["ledger"].events("tool_guard_triggered"))[0].data["phase"] == "output"


async def test_no_guard_keeps_original_behaviour(env):
    """没配规则：工具照常执行，且不产生任何 guard 事件。"""
    tool = _CountingTool()
    reply = ('<tool_call>\n{"name": "write_file", "args": '
             '{"content": "SECRET"}}\n</tool_call>')
    await _drive(env, [tool], None, reply)
    assert tool.hits == 1
    assert list(env["ledger"].events("tool_guard_triggered")) == []
