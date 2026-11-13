"""C2：流式调用被中途打断时的部分用量记账。

背景：``stream_call`` 原本**只在流完整消费完**记账。调用方 break / 取消 / 已
yield 后抛错时，已吐出的块是确定的 token 消耗，但计数、成本、配额全都不落——
取消后重跑会让预算计数悄悄偏低，可能无限逼近实际上限却永不触发。

本文件钉住四件事：

1. 正常消费完 → 一条普通 ``llm_call``（无 ``partial`` 键）；
2. 中途打断 → 一条 ``llm_call(partial=True)``，token/成本/调用数都入账；
3. 部分用量进入 ``max_llm_calls`` 计数 → 下一次调用被 ``_guard_run_cost`` 拦下；
4. 打断时记账本身不抛 ``BudgetExhaustedError``（异常正传播，不能掩盖原原因）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.models.errors import BudgetExhaustedError
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.mock import set_purpose
from agentcrew.runtime.ledger import Ledger

GOOD = "```python file=a.py\ndef f():\n    return 1\n```\n"


def _write_fixture(root: Path, text: str, *, task_id: str = "T-1") -> None:
    directory = root / "responses" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "attempt-1.md").write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    fixtures = tmp_path / "fx"
    _write_fixture(fixtures, GOOD)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=fixtures)
    alias = settings.default_model
    set_purpose(models.get(alias), "execute", task_id="T-1", attempt=1)
    return dict(models=models, alias=alias, ledger=ledger, settings=settings)


def _llm_events(ledger: Ledger) -> list:
    return list(ledger.events("llm_call"))


async def test_normal_stream_emits_one_non_partial_event(env):
    models, alias, ledger = env["models"], env["alias"], env["ledger"]
    before = models.llm_calls
    async for _chunk in models.stream_call(
        alias, [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=100, purpose="execute", role="backend_dev",
    ):
        pass
    events = _llm_events(ledger)
    assert models.llm_calls == before + 1
    assert len(events) == 1
    assert "partial" not in events[0].data


async def test_aborted_stream_records_partial_usage(env):
    models, alias, ledger = env["models"], env["alias"], env["ledger"]
    before_calls = models.llm_calls
    before_tokens = models.total_completion_tokens
    agen = models.stream_call(
        alias, [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=100, purpose="execute", role="backend_dev",
    )
    await agen.__anext__()  # 拿到一块就放弃（等价于用户取消 / 上层 break）
    await agen.aclose()     # 显式关闭：触发 finally 的部分记账
    events = _llm_events(ledger)
    assert len(events) == 1
    assert events[0].data.get("partial") is True
    assert events[0].data["completion_tokens"] > 0
    assert models.llm_calls == before_calls + 1
    assert models.total_completion_tokens > before_tokens


async def test_partial_usage_counts_toward_max_llm_calls(tmp_path, monkeypatch):
    """打断也算一次调用：``max_llm_calls=1`` 时第二次调用必须被拦。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_MAX_LLM_CALLS", "1")
    settings = Settings.load()
    assert settings.max_llm_calls == 1
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    fixtures = tmp_path / "fx"
    _write_fixture(fixtures, GOOD)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=fixtures)
    alias = settings.default_model
    set_purpose(models.get(alias), "execute", task_id="T-1", attempt=1)

    agen = models.stream_call(
        alias, [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=100, purpose="execute", role="backend_dev",
    )
    await agen.__anext__()
    await agen.aclose()
    assert models.llm_calls == 1

    with pytest.raises(BudgetExhaustedError):
        async for _chunk in models.stream_call(
            alias, [{"role": "user", "content": "hi"}],
            temperature=0, max_tokens=100, purpose="execute", role="backend_dev",
        ):
            pass


async def test_partial_accounting_does_not_mask_original_error(tmp_path, monkeypatch):
    """打断记账过程中预算检查失败，也不能把原异常换成 BudgetExhaustedError。"""

    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    fixtures = tmp_path / "fx"
    _write_fixture(fixtures, GOOD)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=fixtures)
    alias = settings.default_model
    set_purpose(models.get(alias), "execute", task_id="T-1", attempt=1)

    # 让「记账时触发的软阈值」直接炸掉——部分记账必须吞掉它，原异常照常出去。
    def _boom() -> None:
        raise RuntimeError("budget warning exploded")

    models._maybe_emit_budget_warning = _boom  # type: ignore[method-assign]

    class _CustomError(RuntimeError):
        pass

    agen = models.stream_call(
        alias, [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=100, purpose="execute", role="backend_dev",
    )
    with pytest.raises(_CustomError):
        await agen.__anext__()
        raise _CustomError("上层放弃")
    # 显式关闭触发 finally 的部分记账（不依赖 GC 时机）；记账里的 warning
    # 已经炸掉，但必须被吞掉、不影响这里的收尾。
    await agen.aclose()

    # 部分用量依然入账（记账先于软阈值）
    events = _llm_events(ledger)
    assert len(events) == 1 and events[0].data.get("partial") is True
