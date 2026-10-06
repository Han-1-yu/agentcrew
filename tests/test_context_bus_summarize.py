"""ContextBus 的 LLM 摘要预取集成测试（V2 第 1 项）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.context.context_bus import ContextBus
from agentcrew.context.summarizer import LLMSummarizer
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskResult
from agentcrew.task.task_board import TaskBoard


def _make_result(task: Task, summary: str) -> TaskResult:
    now = datetime.now()
    return TaskResult(
        task_id=task.id, role=task.role, summary=summary,
        artifacts=[], notes="",
        started_at=now, finished_at=now + timedelta(seconds=1), duration_ms=1000,
    )


@pytest.fixture
def bus_env(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "artifacts", run_id="R", ledger=lg)
    upstream = Task(
        id="T-UP", title="上游", description="做上游", role="architect",
        acceptance_criteria=["完成"], deliverables=[ArtifactSpec(path="up.md", kind="markdown", description="d")],
    )
    upstream.result = _make_result(upstream, "这是一段非常长的摘要。" * 200)
    downstream = Task(
        id="T-DOWN", title="下游", description="做下游", role="backend_dev",
        acceptance_criteria=["完成"], deliverables=[ArtifactSpec(path="down.py", kind="python", description="d")],
        dependencies=["T-UP"],
    )
    board = TaskBoard([upstream, downstream])
    return store, board, upstream, downstream


@pytest.mark.asyncio
async def test_prefetch_summaries_calls_summarizer_for_oversized(bus_env):
    store, board, upstream, downstream = bus_env
    calls: list[str] = []

    async def fake_fn(text: str, max_tokens: int) -> str:
        calls.append(text)
        return "压缩后的摘要"

    bus = ContextBus("目标", board, store, summarizer=LLMSummarizer(fake_fn))
    count = await bus.prefetch_summaries(downstream)
    assert count == 1
    assert len(calls) == 1
    assert upstream.id in bus._summary_cache
    assert bus._summary_cache[upstream.id] == "压缩后的摘要"


@pytest.mark.asyncio
async def test_prefetch_summaries_skips_when_fits(bus_env):
    store, board, upstream, downstream = bus_env
    upstream.result = _make_result(upstream, "短摘要")
    calls: list[str] = []

    async def fake_fn(text: str, max_tokens: int) -> str:
        calls.append(text)
        return "不应调用"

    bus = ContextBus("目标", board, store, summarizer=LLMSummarizer(fake_fn))
    count = await bus.prefetch_summaries(downstream)
    assert count == 0
    assert calls == []


@pytest.mark.asyncio
async def test_prefetch_summaries_uses_cache_in_build(bus_env):
    store, board, upstream, downstream = bus_env

    async def fake_fn(text: str, max_tokens: int) -> str:
        return "LLM压缩摘要"

    bus = ContextBus("目标", board, store, summarizer=LLMSummarizer(fake_fn))
    await bus.prefetch_summaries(downstream)
    pack = bus.build(downstream)
    assert len(pack.upstream) == 1
    assert pack.upstream[0].summary == "LLM压缩摘要"


@pytest.mark.asyncio
async def test_prefetch_summaries_idempotent(bus_env):
    store, board, upstream, downstream = bus_env
    call_count = 0

    async def fake_fn(text: str, max_tokens: int) -> str:
        nonlocal call_count
        call_count += 1
        return "摘要"

    bus = ContextBus("目标", board, store, summarizer=LLMSummarizer(fake_fn))
    await bus.prefetch_summaries(downstream)
    await bus.prefetch_summaries(downstream)  # 第二次应命中缓存
    assert call_count == 1


def test_build_without_summarizer_still_works(bus_env):
    """未配置 summarizer 时走 NullSummarizer，build 行为不变。"""
    store, board, upstream, downstream = bus_env
    bus = ContextBus("目标", board, store)
    pack = bus.build(downstream)
    assert len(pack.upstream) == 1
    # 长 summary 会被截断，但不会崩
    assert len(pack.upstream[0].summary) < len(upstream.result.summary)
