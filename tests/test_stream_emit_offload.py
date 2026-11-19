"""R66i（EXP-1）：逐 chunk 审计落盘的自适应下沉机制。

裁决背景（scripts/exp1_stream_emit.py，.exp1-r66*.json 原始样本）：
- 无条件 to_thread 下沉：并发 4 流 ACCEPT（墙钟 −65%、滞后 p95 27→10ms），
  但单流 REJECT（dispatch 开销 +10.9% > 噪声 1.9%）。
- 自适应（并发流 >1 才下沉）：单流 REMEASURE（无回归，走原同步路径），
  并发 4 流 ACCEPT。mutant（丢一帧 / 丢观察者回调）均被 oracle 击中。

本文件钉生产接线的四件事：单流走 emit、并发流走 write_event+notify、
异常后计数归零、两条路径的 ledger 行（去 ts）逐字一致。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentcrew.agents import base_agent as ba
from agentcrew.agents.generic import GenericAgent
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import StreamChunk
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task


def _role() -> RoleSpec:
    return RoleSpec(
        key="backend_dev", title="b", goal="g", backstory="b",
        responsibilities=["w"], boundaries=[], output_contract="py",
        system_prompt="x",
    )


def _task(tid: str = "T-1") -> Task:
    return Task(
        id=tid, title="t", description="d", role="backend_dev",
        acceptance_criteria=["a"], deliverables=[],
    )


def _agent(tmp_path: Path, monkeypatch, name: str, chunks: int) -> GenericAgent:
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / f"{name}.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts", run_id="R", ledger=ledger)
    models = ModelFactory(settings, ledger=ledger)
    agent = GenericAgent(
        _role(), models=models, store=store, ledger=ledger,
        project_goal="g", stream=True,
    )

    async def fake_stream(alias, messages, **kw):
        for i in range(chunks):
            await asyncio.sleep(0)  # 让出循环：并发消费者得以交叠
            yield StreamChunk(delta=f"块{i}", finish_reason="stop" if i == chunks - 1 else None)

    agent._models.stream_call = fake_stream
    return agent


def _spy(ledger: Ledger) -> dict[str, list]:
    calls: dict[str, list] = {"emit": [], "write_event": [], "notify": []}
    for meth in calls:
        orig = getattr(ledger, meth)

        def wrapper(*a, _m=meth, _o=orig, **kw):
            calls[_m].append((a, kw))
            return _o(*a, **kw)

        setattr(ledger, meth, wrapper)
    return calls


def _rows(path: Path) -> list[dict]:
    rows = [json.loads(ln) for ln in path.read_text("utf-8").splitlines() if ln]
    for row in rows:
        row.pop("ts", None)
    return rows


@pytest.mark.asyncio
async def test_single_consumer_stays_sync(tmp_path: Path, monkeypatch):
    """单流：逐帧走原 emit（同步路径），不碰 write_event；计数归零。"""
    agent = _agent(tmp_path, monkeypatch, "solo", 5)
    calls = _spy(agent._ledger)
    await agent._invoke_model(_task(), [{"role": "user", "content": "x"}], "mock:model")
    assert len(calls["emit"]) == 5
    # emit 内部委托 write_event+notify（拆分后的组合）：单流下所有落盘都经 emit
    assert len(calls["write_event"]) == 5
    assert len(calls["notify"]) == 5
    assert ba._ACTIVE_STREAM_CONSUMERS == 0


@pytest.mark.asyncio
async def test_concurrent_consumers_offload(tmp_path: Path, monkeypatch):
    """并发流：交叠后走 write_event+notify；审计总帧数不丢；计数归零。"""
    a1 = _agent(tmp_path, monkeypatch, "a", 6)
    a2 = _agent(tmp_path, monkeypatch, "b", 6)
    c1, c2 = _spy(a1._ledger), _spy(a2._ledger)
    await asyncio.gather(
        a1._invoke_model(_task("T-1"), [{"role": "user", "content": "x"}], "mock:model"),
        a2._invoke_model(_task("T-2"), [{"role": "user", "content": "x"}], "mock:model"),
    )
    # 首帧可能赶在第二个消费者启动前（同步），交叠后必须绕过 emit 直接下沉：
    # 直落帧数 = write_event 总数 − 经 emit 委托的帧数
    assert len(c1["write_event"]) - len(c1["emit"]) > 0
    assert len(c2["write_event"]) - len(c2["emit"]) > 0
    assert len(c1["notify"]) == len(c1["write_event"])
    assert len(c2["notify"]) == len(c2["write_event"])
    assert len(c1["write_event"]) == 6
    assert len(c2["write_event"]) == 6
    assert len(_rows(tmp_path / "a.jsonl")) == 6
    assert len(_rows(tmp_path / "b.jsonl")) == 6
    assert ba._ACTIVE_STREAM_CONSUMERS == 0


@pytest.mark.asyncio
async def test_counter_restored_on_stream_error(tmp_path: Path, monkeypatch):
    """流中途抛错：计数必须归零（finally），已消费帧的审计仍在盘上。"""
    agent = _agent(tmp_path, monkeypatch, "err", 4)

    async def broken_stream(alias, messages, **kw):
        yield StreamChunk(delta="第一帧")
        raise RuntimeError("provider 断流")

    agent._models.stream_call = broken_stream
    with pytest.raises(RuntimeError, match="断流"):
        await agent._invoke_model(_task(), [{"role": "user", "content": "x"}], "mock:model")
    assert ba._ACTIVE_STREAM_CONSUMERS == 0
    assert len(_rows(tmp_path / "err.jsonl")) == 1


@pytest.mark.asyncio
async def test_both_paths_write_identical_rows(tmp_path: Path, monkeypatch):
    """语义 oracle：同帧序列经同步/下沉两路径，ledger 行（去 ts）逐字一致。"""
    solo = _agent(tmp_path, monkeypatch, "solo", 6)
    await solo._invoke_model(_task("T-1"), [{"role": "user", "content": "x"}], "mock:model")
    pair_a = _agent(tmp_path, monkeypatch, "pa", 6)
    pair_b = _agent(tmp_path, monkeypatch, "pb", 6)
    ca = _spy(pair_a._ledger)
    await asyncio.gather(
        pair_a._invoke_model(_task("T-1"), [{"role": "user", "content": "x"}], "mock:model"),
        pair_b._invoke_model(_task("T-2"), [{"role": "user", "content": "x"}], "mock:model"),
    )
    assert len(ca["write_event"]) - len(ca["emit"]) > 0  # 确保真的交叠下沉过
    assert _rows(tmp_path / "solo.jsonl") == _rows(tmp_path / "pa.jsonl")
