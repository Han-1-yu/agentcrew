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


def test_crew_stream_sink_publishes_agent_stream_delta(monkeypatch, tmp_path):
    """Crew 把增量包成 agent_stream_delta 推给 on_event，含 run_id；同时落盘回溯。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", "ignored")
    events: list[tuple[str, dict]] = []
    crew = Crew(Settings.load(), on_event=lambda name, data: events.append((name, data)))
    sink = crew._make_stream_sink("R-42", tmp_path / "streams")
    sink("T-1", "backend_dev", "hello")
    sink("T-1", "backend_dev", " world")

    assert [name for name, _ in events] == ["agent_stream_delta", "agent_stream_delta"]
    first = events[0][1]
    assert first["run_id"] == "R-42"
    assert first["task_id"] == "T-1"
    assert first["role"] == "backend_dev"
    assert first["delta"] == "hello"
    # 正文落盘供刷新回溯
    persisted = (tmp_path / "streams" / "T-1.md").read_text(encoding="utf-8")
    assert persisted == "hello world"


def test_crew_stream_sink_persists_without_on_event(monkeypatch, tmp_path):
    """没有 on_event（CLI 直接跑）时正文仍落盘（回溯不依赖 SSE）；空 delta 不落盘。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", "ignored")
    crew = Crew(Settings.load())  # 不传 on_event
    sink = crew._make_stream_sink("R", tmp_path / "streams")
    sink("T-1", "r", "")          # 空正文不该写
    sink("T-1", "r", "x")         # 有正文 → 落盘
    assert (tmp_path / "streams" / "T-1.md").read_text(encoding="utf-8") == "x"


def test_crew_stream_sink_sanitizes_task_id(monkeypatch, tmp_path):
    """task_id 里的路径字符要消毒，防越界写盘。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", "ignored")
    crew = Crew(Settings.load())
    sink = crew._make_stream_sink("R", tmp_path / "streams")
    sink("../etc/passwd", "r", "boom")
    # 落盘文件应叫消毒后的名字，且不越界
    assert not (tmp_path / "etc").exists()
    assert (tmp_path / "streams" / ".._etc_passwd.md").read_text(encoding="utf-8") == "boom"


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


@pytest.mark.asyncio
async def test_detail_events_reach_on_event_via_ledger_observer(tmp_path, monkeypatch):
    """端到端：明细事件经 Ledger observer 转发到 on_event，带 run_id 且不重复。

    改造前 ``task_started`` / ``task_finished`` / ``artifact_written`` /
    ``plan_created`` 等只写 ledger、不进 on_event（SSE 收不到实时明细）。本轮
    Ledger 增加 observer，把这些明细同步推给 on_event。钉三件事：明细事件到达、
    每个都带 run_id（EventBus 按 run 分桶）、已走 ``_dispatch_event`` 的
    ``run_started`` 不重复推（恰好一次）。
    """
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
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

    names = [name for name, _ in events]
    # 明细事件经 observer 到达 on_event
    for expected in ("task_started", "task_finished", "artifact_written", "plan_created"):
        assert expected in names, f"{expected} 应经 observer 到达 on_event"
    # 已走 _dispatch_event 的事件不重复
    assert names.count("run_started") == 1
    # 明细事件带 run_id，EventBus 才能按 run 分桶
    started = [data for name, data in events if name == "task_started"]
    assert started and all(d.get("run_id") == record.run_id for d in started)
