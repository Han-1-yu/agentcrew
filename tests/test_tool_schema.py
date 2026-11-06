"""R3：工具入参 JSON Schema 强校验（纯函数 + ReAct 接线）。

覆盖：空/自由形状 schema 向后兼容、必填与类型、字符串长度与正则、数值范围与
enum/const、additionalProperties、items、错误条数上限、schema_hint 形状提示；
以及 Agent 侧「非法入参不执行工具、错误带正确形状回灌、ledger 诚实留痕」。

参考：CrewAI ``lib/crewai/src/crewai/tools/base_tool.py::_validate_kwargs``。
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
from agentcrew.tools import ReadFileTool, Tool, ToolRegistry
from agentcrew.tools.schema import (
    MAX_ERRORS,
    schema_hint,
    validate_tool_args,
)


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "pattern": r"^[\w./-]+$"},
            "count": {"type": "integer", "minimum": 1, "maximum": 10},
            "ratio": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
            "mode": {"type": "string", "enum": ["fast", "solid"]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["path"],
        "additionalProperties": False,
    }


# ── 纯函数层 ───────────────────────────────────────────────────────────

def test_empty_and_freeform_schemas_are_passed_through():
    """空 schema / 非 dict / 非 object / 无 properties 一律放行（MCP 兼容）。"""
    assert validate_tool_args({}, {"anything": 1}) == []
    assert validate_tool_args(None, {"anything": 1}) == []
    assert validate_tool_args("not-a-schema", {"anything": 1}) == []
    assert validate_tool_args({"type": "string"}, {"anything": 1}) == []
    assert validate_tool_args({"type": "object"}, {"anything": 1}) == []
    assert schema_hint({}) == ""


def test_args_must_be_a_mapping():
    errors = validate_tool_args(_schema(), ["path"])
    assert errors == ["工具入参必须是 JSON 对象（键 → 值映射）"]
    assert validate_tool_args(_schema(), None)  # None 归一成 {} 后仍会报缺必填
    assert "缺少必填参数 path" in validate_tool_args(_schema(), None)


def test_valid_args_pass():
    assert validate_tool_args(_schema(), {
        "path": "src/app.py", "count": 3, "ratio": 0.5,
        "mode": "fast", "tags": ["a", "b"],
    }) == []


def test_missing_required_and_unknown_property():
    errors = validate_tool_args(_schema(), {"nope": 1})
    assert any("缺少必填参数 path" in e for e in errors)
    assert any("未知参数 nope" in e for e in errors)


def test_type_mismatch_reports_actual_type():
    errors = validate_tool_args(_schema(), {"path": 123, "count": True})
    assert any("path 应为 string" in e for e in errors)
    # bool 不是 JSON Schema 的 integer
    assert any("count 应为 integer" in e for e in errors)


def test_string_length_pattern_and_enum():
    errors = validate_tool_args(_schema(), {
        "path": "", "mode": "turbo", "ratio": 1.5,
    })
    assert any("长度不能小于 1" in e for e in errors)
    assert any("不匹配格式" in e for e in errors)
    assert any("必须是以下取值之一" in e for e in errors)
    assert any("必须小于 1" in e for e in errors)  # exclusiveMaximum


def test_numeric_bounds_and_exclusive_bounds():
    errors = validate_tool_args(_schema(), {"path": "a.txt", "count": 99, "ratio": 0})
    assert any("count 不能大于 10" in e for e in errors)
    assert any("ratio 必须大于 0" in e for e in errors)


def test_const_and_array_items():
    schema = {
        "type": "object",
        "properties": {
            "version": {"type": "integer", "const": 1},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }
    errors = validate_tool_args(schema, {"version": 2, "tags": ["ok", 7]})
    assert any("必须等于 1" in e for e in errors)
    assert any("tags[1] 应为 string" in e for e in errors)


def test_broken_regex_in_schema_does_not_reject_everything():
    schema = {"type": "object", "properties": {"x": {"type": "string", "pattern": "["}}}
    assert validate_tool_args(schema, {"x": "abc"}) == []


def test_nan_number_is_rejected():
    schema = {"type": "object", "properties": {"x": {"type": "number"}}}
    assert any("应为 number" in e for e in validate_tool_args(schema, {"x": float("nan")}))


def test_errors_are_bounded():
    schema = {
        "type": "object",
        "properties": {f"f{i}": {"type": "integer"} for i in range(30)},
        "required": [f"f{i}" for i in range(30)],
    }
    assert len(validate_tool_args(schema, {})) == MAX_ERRORS


def test_unknown_keyword_is_ignored():
    """未实现的 JSON Schema 关键字不能反过来拒绝合法入参。"""
    schema = {"type": "object", "properties": {"x": {"type": "string", "$ref": "#/x"}}}
    assert validate_tool_args(schema, {"x": "ok"}) == []


def test_schema_hint_renders_shape_and_bounds():
    hint = schema_hint({
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "timeout": {"type": "number", "minimum": 0, "maximum": 60},
        },
        "required": ["path"],
    })
    assert "必填 path(string)" in hint
    assert "可选 timeout(number" in hint
    assert "0~60" in hint


# ── Agent ReAct 接线 ───────────────────────────────────────────────────

class _SpyTool(Tool):
    """记录自己是否真的被 run 过，用于证明「校验失败 = 绝不执行」。"""

    name: str = "spy"
    description: str = "记录调用"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    called: int = 0

    async def run(self, path: str = "", **kwargs: Any) -> str:
        self.called += 1
        return f"读取了 {path}"


class _NoSchemaTool(Tool):
    """空 schema 的自定义工具：必须照常执行（向后兼容）。"""

    name: str = "legacy"
    description: str = "老工具"
    called: int = 0

    async def run(self, **kwargs: Any) -> str:
        self.called += 1
        return "legacy-ok"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts", run_id="R", ledger=ledger)
    spec = ArtifactSpec(path="a.py", kind="python", description="d",
                        must_contain=["def f"])
    task = Task(id="T-1", title="t", description="写 a.py", role="backend_dev",
                acceptance_criteria=["可运行"], deliverables=[spec])
    board = TaskBoard([task])
    bus = ContextBus("目标", board, store)
    return dict(settings=settings, ledger=ledger, store=store, task=task, bus=bus,
                tmp_path=tmp_path)


def _role() -> RoleSpec:
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件", system_prompt="你是后端工程师。")


def _resp(text: str) -> CompletionResult:
    return CompletionResult(text=text, provider="mock", model="m",
                            prompt_tokens=1, completion_tokens=10, latency_ms=0)


_FINAL = "```python file=a.py\ndef f():\n    return 1\n```\n"


async def _drive(env, registry: ToolRegistry, call_json: str):
    agent = GenericAgent(_role(), models=ModelFactory(env["settings"],
                         ledger=env["ledger"]), store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         tool_registry=registry)
    queue = [_resp(f"<tool_call>\n{call_json}\n</tool_call>"), _resp(_FINAL)]
    seen: list[list[dict]] = []

    async def fake_call(alias, messages, **kw):
        seen.append([dict(m) for m in messages])
        return queue.pop(0)

    agent._models.call = fake_call
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    return seen


async def test_invalid_args_never_run_tool_and_feedback_contains_hint(env):
    tool = _SpyTool()
    registry = ToolRegistry()
    registry.register(tool)
    seen = await _drive(env, registry, '{"name": "spy", "args": {"path": 7}}')

    assert tool.called == 0, "校验失败的工具绝不能真正执行"
    replay = seen[1][-1]["content"]
    assert "spy" in replay
    assert "不符合声明的 JSON Schema" in replay
    assert "path 应为 string" in replay
    assert "必填 path(string)" in replay, "错误文本要带上正确形状供模型自我纠正"

    events = list(env["ledger"].events("tool_call"))
    assert len(events) == 1 and events[0].data["success"] is False
    invalid = list(env["ledger"].events("tool_schema_invalid"))
    assert len(invalid) == 1
    assert invalid[0].data["tool_name"] == "spy"
    assert invalid[0].data["task_id"] == "T-1"


async def test_valid_args_still_execute_normally(env):
    tool = _SpyTool()
    registry = ToolRegistry()
    registry.register(tool)
    await _drive(env, registry, '{"name": "spy", "args": {"path": "notes.txt"}}')

    assert tool.called == 1
    assert list(env["ledger"].events("tool_call"))[0].data["success"] is True
    assert env["ledger"].count("tool_schema_invalid") == 0


async def test_empty_schema_tool_bypasses_validation(env):
    tool = _NoSchemaTool()
    registry = ToolRegistry()
    registry.register(tool)
    await _drive(env, registry, '{"name": "legacy", "args": {"weird": [1, 2]}}')
    assert tool.called == 1
    assert env["ledger"].count("tool_schema_invalid") == 0


async def test_read_file_schema_rejects_wrong_type_in_real_tool(env):
    """真实内置工具也要过校验：``path`` 给数组时不能把异常冒泡断掉 Agent。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=env["tmp_path"]))
    seen = await _drive(
        env, registry,
        '{"name": "read_file", "args": {"path": ["../etc/passwd"]}}',
    )
    assert "path 应为 string" in seen[1][-1]["content"]
    assert list(env["ledger"].events("tool_call"))[0].data["success"] is False
    assert list(env["ledger"].events("tool_schema_invalid"))[0].data[
        "tool_name"] == "read_file"
