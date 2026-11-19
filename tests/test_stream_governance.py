"""R66b：流式治理链与 ``call`` 对齐的回归钉。

修复前的分叉（本文件逐条钉住，任何一条回退都会失败）：

1. ``stream_call`` 绕过 ``_maybe_apply_downgrade``——全局预算降级触发后，
   流式（Agent 主输出路径）仍走原贵模型；
2. R44 执行体成本感知路由（headroom shift）同样被流式绕过；
3. ``model_failover`` 的权重格式在流式下静默失效（只取别名丢权重、
   不落 ``llm_routed``）；
4. 流式不参与会话亲和（``session_id`` 参数都不存在）。

同时保住两条**刻意保留**的流式红线：首块吐出后不再故障转移；
打断流的部分用量记账与预扣归还在 ``aclose()`` 返回时即已完成。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.models.agent_budget import bind_budget, note_call, release_budget
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import StreamChunk
from agentcrew.models.providers.mock import set_purpose
from agentcrew.runtime.ledger import Ledger
from agentcrew.utils.retry import TransientError

GOOD = "```python file=a.py\ndef f():\n    return 1\n```\n"
MESSAGES = [{"role": "user", "content": "hi"}]


def _write_fixture(root: Path, text: str, *, task_id: str = "T-1") -> None:
    directory = root / "responses" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "attempt-1.md").write_text(text, encoding="utf-8")


def _make(tmp_path: Path, *, failover=None, **settings_kw) -> tuple[ModelFactory, Ledger]:
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    fixtures = tmp_path / "fx"
    _write_fixture(fixtures, GOOD)
    # 给足配额：配额窗口存在时 quota_tracker 才建用量槽，_usage_calls 才有读数。
    quotas = {
        alias: {"daily": {"max_calls": 100}}
        for alias in ("mock:primary", "mock:cheap", "mock:backup")
    }
    settings = Settings(
        mock=True, output_dir=tmp_path / "o",
        model_failover=failover or {}, quotas=quotas, **settings_kw)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=fixtures)
    return models, ledger


def _bind(models: ModelFactory, alias: str) -> None:
    set_purpose(models.get(alias), "execute", task_id="T-1", attempt=1)


async def _drain(models: ModelFactory, alias: str, **kw) -> str:
    return "".join([
        chunk.delta async for chunk in models.stream_call(
            alias, MESSAGES, temperature=0, max_tokens=100,
            purpose="execute", role="backend_dev", **kw)
    ])


def _usage_calls(models: ModelFactory, alias: str) -> int:
    return models._quota_tracker._get_usage(alias, "daily").calls


async def test_stream_honors_global_budget_downgrade(tmp_path):
    """预算降级已触发时，流式必须改走便宜别名（修复前走原别名）。"""
    models, ledger = _make(tmp_path)
    _bind(models, "mock:primary")
    _bind(models, "mock:cheap")
    models._budget_downgrade_to = "mock:cheap"

    text = await _drain(models, "mock:primary")

    assert text.strip() != ""
    assert _usage_calls(models, "mock:cheap") == 1
    assert _usage_calls(models, "mock:primary") == 0
    calls = list(ledger.events("llm_call"))
    assert len(calls) == 1
    assert calls[0].data["model"] == "cheap"


async def test_stream_honors_cost_aware_headroom_shift(tmp_path, monkeypatch):
    """R44：执行体额度吃紧时，流式同样提前改走便宜备别名并留痕。"""
    models, ledger = _make(tmp_path, cost_aware_routing=True)
    _bind(models, "mock:primary")
    _bind(models, "mock:cheap")
    monkeypatch.setattr(
        models, "_find_cheaper_fallback",
        lambda alias, binding=None: "mock:cheap")
    token = bind_budget("backend_fast", None, None, run_ceiling_usd=1.0)
    try:
        note_call(0.99)
        await _drain(models, "mock:primary")
    finally:
        release_budget(token)

    assert _usage_calls(models, "mock:cheap") == 1
    assert _usage_calls(models, "mock:primary") == 0
    shifts = list(ledger.events("routing_budget_shift"))
    assert len(shifts) == 1
    assert shifts[0].data["to_alias"] == "mock:cheap"


async def test_stream_weighted_failover_respects_weights(tmp_path, monkeypatch):
    """权重格式在流式下生效：走权重池、落 llm_routed、失败按剩余候选重选。"""
    models, ledger = _make(
        tmp_path,
        failover={"mock:primary": [
            {"alias": "mock:primary", "weight": 1.0},
            {"alias": "mock:backup", "weight": 1.0},
        ]})
    _bind(models, "mock:primary")
    _bind(models, "mock:backup")
    # 确定性选择：永远取剩余候选的第一个（保留权重池机制本身）。
    monkeypatch.setattr(
        models, "_select_weighted", lambda pool: (pool[0].alias, "test"))

    async def _boom(*_args, **_kwargs):
        raise TransientError("upstream down")
        yield  # pragma: no cover —— 让它成为 async generator

    monkeypatch.setattr(models.get("mock:primary"), "stream_complete", _boom)

    text = await _drain(models, "mock:primary")

    assert text.strip() != ""
    assert _usage_calls(models, "mock:backup") == 1
    assert _usage_calls(models, "mock:primary") == 0
    routed = list(ledger.events("llm_routed"))
    assert routed, "权重路由必须落 llm_routed（修复前流式完全绕过权重池）"
    failovers = list(ledger.events("llm_failover"))
    assert len(failovers) == 1
    assert failovers[0].data["from_alias"] == "mock:primary"
    assert failovers[0].data["to_alias"] == "mock:backup"


async def test_stream_no_failover_after_first_chunk(tmp_path, monkeypatch):
    """红线：首块已吐出后失败必须原样上抛，不转移、不产生两半来源的文本。"""
    models, ledger = _make(
        tmp_path, failover={"mock:primary": ["mock:backup"]})
    _bind(models, "mock:primary")
    _bind(models, "mock:backup")

    async def _half(*_args, **_kwargs):
        yield StreamChunk(delta="part", finish_reason=None)
        raise TransientError("mid-stream down")

    monkeypatch.setattr(models.get("mock:primary"), "stream_complete", _half)

    parts: list[str] = []
    with pytest.raises(TransientError):
        async for chunk in models.stream_call(
            "mock:primary", MESSAGES, temperature=0, max_tokens=100,
            purpose="execute", role="backend_dev"):
            parts.append(chunk.delta)  # noqa: PERF401 —— 中途必须能抛异常，不能改推导式

    assert parts == ["part"]
    assert list(ledger.events("llm_failover")) == []
    assert _usage_calls(models, "mock:backup") == 0
    # 打断流的部分用量入账、预扣归还（异常传播路径）。
    assert _usage_calls(models, "mock:primary") == 1
    assert models._quota_tracker._get_reserved("mock:primary", "daily").calls == 0


async def test_stream_remembers_session_affinity(tmp_path):
    """有序计划下流式成功也记录会话亲和（修复前 stream_call 无 session_id）。"""
    models, _ = _make(
        tmp_path, failover={"mock:primary": ["mock:backup"]},
        affinity_mode="session")
    _bind(models, "mock:primary")
    _bind(models, "mock:backup")

    await _drain(models, "mock:primary", session_id="s1")

    picked = models._affinity_tracker.resolve(
        "mock:backup", "s1", candidates=["mock:primary", "mock:backup"])
    assert picked == "mock:primary"


async def test_stream_aclose_settles_quota_immediately(tmp_path):
    """aclose() 返回时部分记账与预扣归还必须已完成（嵌套生成器即时收尾）。"""
    models, _ = _make(tmp_path)
    _bind(models, "mock:primary")

    agen = models.stream_call(
        "mock:primary", MESSAGES, temperature=0, max_tokens=100,
        purpose="execute", role="backend_dev")
    await agen.__anext__()
    assert models._quota_tracker._get_reserved("mock:primary", "daily").calls == 1
    await agen.aclose()
    assert _usage_calls(models, "mock:primary") == 1
    assert models._quota_tracker._get_reserved("mock:primary", "daily").calls == 0
