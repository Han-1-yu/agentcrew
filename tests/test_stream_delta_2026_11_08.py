"""P3-3 → UI：流式增量正文从 Agent 一路推到 SSE（看板 / ``curl -N``）。

流式之前只把 ``agent_stream_chunk``（``delta_length``）写进 ledger——正文不落审计
（避免膨胀），但看板也看不到实时正文。本轮补一条**瞬态**通道：

``BaseAgent(stream_sink=...)`` → ``Crew._make_stream_sink`` → ``on_event
("agent_stream_delta")`` → SSE 总线 → 看板。正文只走 SSE，不落 ledger、不触发
Webhook（几千个块不该打爆审计与接收端）。

钉四件事：增量内容逐块到 sink、Crew 把它包成 agent_stream_delta 推给 on_event、
无 on_event 时是零开销 no-op、看板确实有渲染入口。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.agents.generic import GenericAgent
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import StreamChunk
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task

ROOT = Path(__file__).resolve().parents[1]

DEMO_FIXTURES_DIR = ROOT / "examples" / "fixtures"
DEMO_GOAL = "开发一个 Todo List Web 应用：用户可在浏览器中增删改查待办事项。"


def _role() -> RoleSpec:
    return RoleSpec(
        key="backend_dev", title="b", goal="g", backstory="b",
        responsibilities=["w"], boundaries=[], output_contract="py",
        system_prompt="x",
    )


@pytest.mark.asyncio
async def test_agent_stream_sink_collects_deltas(tmp_path: Path, monkeypatch):
    """逐块正文要原样流到 sink；拼接结果与最终产物文本一致。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts", run_id="R", ledger=ledger)
    models = ModelFactory(settings, ledger=ledger)

    captured: list[tuple[str, str, str]] = []
    agent = GenericAgent(
        _role(), models=models, store=store, ledger=ledger,
        project_goal="g", stream=True,
        stream_sink=lambda tid, role, delta: captured.append((tid, role, delta)),
    )
    chunks = [
        StreamChunk(delta="hello "),
        StreamChunk(delta="world", finish_reason="stop"),
    ]

    async def fake_stream(alias, messages, **kw):
        for chunk in chunks:
            yield chunk

    agent._models.stream_call = fake_stream
    task = Task(
        id="T-1", title="t", description="d", role="backend_dev",
        acceptance_criteria=["a"], deliverables=[],
    )
    result = await agent._invoke_model(task, [{"role": "user", "content": "x"}], "mock:model")

    assert result.text == "hello world"
    assert "".join(delta for _, _, delta in captured) == "hello world"
    assert captured[0][0] == "T-1" and captured[0][1] == "backend_dev"


def test_crew_stream_sink_publishes_agent_stream_delta(monkeypatch):
    """Crew 把增量包成 agent_stream_delta 推给 on_event，含 run_id。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", "ignored")
    events: list[tuple[str, dict]] = []
    crew = Crew(Settings.load(), on_event=lambda name, data: events.append((name, data)))
    sink = crew._make_stream_sink("R-42")
    sink("T-1", "backend_dev", "hello")
    sink("T-1", "backend_dev", " world")

    assert [name for name, _ in events] == ["agent_stream_delta", "agent_stream_delta"]
    first = events[0][1]
    assert first["run_id"] == "R-42"
    assert first["task_id"] == "T-1"
    assert first["role"] == "backend_dev"
    assert first["delta"] == "hello"


def test_crew_stream_sink_is_noop_without_on_event(monkeypatch):
    """没有 on_event（CLI 直接跑）时 sink 是零开销 no-op，空 delta 也不推。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", "ignored")
    crew = Crew(Settings.load())  # 不传 on_event
    sink = crew._make_stream_sink("R")
    sink("T-1", "r", "")          # 空正文不该推
    sink("T-1", "r", "x")         # 有正文但无观察者，静默丢弃


def test_dashboard_has_stream_panel_and_handler():
    """看板要有流式面板 + agent_stream_delta 处理器（否则正文推了也没人看）。"""
    src = (ROOT / "agentcrew" / "api" / "dashboard.py").read_text(encoding="utf-8")
    assert 'id="stream-output"' in src
    assert "agent_stream_delta" in src
    assert "function onStreamDelta" in src


@pytest.mark.asyncio
async def test_stream_deltas_reach_on_event_end_to_end(tmp_path, monkeypatch):
    """端到端：mock + stream 跑一次完整 run，``agent_stream_delta`` 带着正文到 on_event。

    这是全链路最关键的集成断言：Crew → AgentFactory → Agent → sink → on_event。
    前面几条单测只证明了各段，这条证明它们真能串起来、且正文是连续的增量文本。
    """
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_STREAM", "true")
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_CONFIG", str(tmp_path / "absent.yaml"))
    from agentcrew.models import factory as model_factory
    monkeypatch.setattr(model_factory, "DEFAULT_FIXTURES_DIR", DEMO_FIXTURES_DIR)

    events: list[tuple[str, dict]] = []
    settings = Settings.load()
    record = await Crew(
        settings, on_event=lambda name, data: events.append((name, data)),
    ).run(DEMO_GOAL)

    deltas = [data for name, data in events if name == "agent_stream_delta"]
    assert deltas, "流式 run 里 on_event 应收到 agent_stream_delta 事件"
    joined = "".join(data["delta"] for data in deltas)
    assert len(joined) > 20, "增量正文不该是空壳"
    assert all(data["run_id"] == record.run_id for data in deltas)
    assert {data["task_id"] for data in deltas}
