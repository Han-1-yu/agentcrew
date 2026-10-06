"""P3-3 流式输出测试。

覆盖：

- ``MockProvider.stream_complete`` 按块 yield，拼接全文 == ``complete()`` 文本；
- ``ModelFactory.stream_call`` 走流式路径且流结束后正确记账（llm_call/token）；
- ``BaseAgent(stream=True)`` 用流式调用产出产物，并对每个增量块记
  ``agent_stream_chunk`` 事件（含 task_id / delta_length）；
- 流式与非流式产出的产物文本逐字节一致；
- ``stream=False``（默认）不产生任何 ``agent_stream_chunk`` 事件（向后兼容）。
"""

from pathlib import Path

import pytest

from agentcrew.agents.generic import GenericAgent
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.mock import set_purpose
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard


def role():
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件", system_prompt="你是后端工程师。")


@pytest.fixture
def env(tmp_path, monkeypatch):
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
    return dict(settings=s, ledger=lg, store=store, spec=spec, task=task,
                board=board, bus=bus, tmp_path=tmp_path)


def write_fixture(fx: Path, text: str, *, attempt: int = 1, task_id: str = "T-1") -> None:
    directory = fx / "responses" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"attempt-{attempt}.md").write_text(text, encoding="utf-8")


GOOD_A = "```python file=a.py\ndef f():\n    return 1\n```\n"


# ── Provider 层：分块 yield 且拼接全文一致 ──

async def test_mock_stream_chunks_concatenate_to_complete(env, tmp_path):
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    provider = models.get(env["settings"].default_model)
    set_purpose(provider, "execute", task_id="T-1", attempt=1)
    msgs = [{"role": "user", "content": "hi"}]

    complete = await provider.complete(
        msgs, temperature=0, max_tokens=100)
    chunks = [chunk async for chunk in provider.stream_complete(
        msgs, temperature=0, max_tokens=100)]

    assert len(chunks) > 1                       # 确实分了多块
    assert "".join(chunk.delta for chunk in chunks) == complete.text
    assert chunks[-1].finish_reason == "stop"
    assert all(chunk.finish_reason is None for chunk in chunks[:-1])


# ── 工厂层：stream_call 逐块 yield 且流结束后记账 ──

async def test_factory_stream_call_yields_chunks_and_accounts(env, tmp_path):
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    alias = env["settings"].default_model
    set_purpose(models.get(alias), "execute", task_id="T-1", attempt=1)

    before_calls = models.llm_calls
    before_completion = models.total_completion_tokens
    deltas: list[str] = []
    async for chunk in models.stream_call(
        alias,
        [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=100,
        purpose="execute", role="backend_dev",
    ):
        deltas.append(chunk.delta)

    assert len(deltas) > 1
    assert "".join(deltas) == GOOD_A
    # 流结束后才记账：调用数 +1、completion token 入账、llm_call 事件落 ledger。
    assert models.llm_calls == before_calls + 1
    assert models.total_completion_tokens > before_completion
    llm_events = list(env["ledger"].events("llm_call"))
    assert llm_events[-1].data["purpose"] == "execute"
    assert llm_events[-1].data["completion_tokens"] > 0


# ── Agent 层：stream=True 产出产物并实时留痕 ──

async def test_agent_stream_writes_artifact_and_emits_chunk_events(env, tmp_path):
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标", stream=True)
    result = await agent.execute(env["task"], env["bus"].build(env["task"]))

    # 产物照常落盘（流式收集的全文走完了原有的 parse/validate/commit）
    assert result.task_id == "T-1"
    assert env["store"].read("a.py").startswith("def f()")

    chunk_events = list(env["ledger"].events("agent_stream_chunk"))
    assert len(chunk_events) > 1
    assert chunk_events[0].data["task_id"] == "T-1"
    assert chunk_events[0].data["role"] == "backend_dev"
    assert chunk_events[0].data["delta_length"] > 0


async def test_stream_and_non_stream_produce_same_artifact(env, tmp_path):
    """流式收集的完整文本与非流式一致：两次执行产物逐字节相同。"""
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)

    async def run_one(stream: bool) -> str:
        store = ArtifactStore(
            tmp_path / f"o_{stream}" / "R" / "artifacts",
            run_id="R", ledger=env["ledger"])
        board = TaskBoard([env["task"]])
        bus = ContextBus("目标", board, store)
        models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
        agent = GenericAgent(role(), models=models, store=store,
                             ledger=env["ledger"], project_goal="目标", stream=stream)
        await agent.execute(env["task"], bus.build(env["task"]))
        return store.read("a.py")

    text_stream = await run_one(True)
    text_plain = await run_one(False)
    assert text_stream == text_plain


# ── 向后兼容：默认不流式时不留 chunk 事件 ──

async def test_no_chunk_events_when_stream_disabled(env, tmp_path):
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标")  # stream 默认 False
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["store"].read("a.py").startswith("def f()")
    assert env["ledger"].count("agent_stream_chunk") == 0
