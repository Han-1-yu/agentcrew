"""C2 剩余：``stream_call`` 的配额设防（预扣 / 归还配对）。

背景：R56 只补了「流被打断也要记账」；``stream_call`` 仍然**不检查配额**——
配额耗尽的别名照样能起流，预扣机制对并发流式调用完全空转。本文件钉住五件事：

1. 配额耗尽的别名起流前就被拦下（单别名时异常原样上抛）；
2. 多别名时按 ``model_failover`` 切换到备用，已 yield 的流不受影响；
3. 起流即预扣：同别名并发第二条流会被自己的预扣量拦住（这是设防的核心）；
4. 流完整消费完 → 实际用量入账、预扣归零（不重复计数）；
5. 流被打断 / 首块前失败 → 预扣同样归零，不泄漏。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.mock import set_purpose
from agentcrew.models.quota import QuotaExhaustedError
from agentcrew.runtime.ledger import Ledger
from agentcrew.utils.retry import TransientError

GOOD = "```python file=a.py\ndef f():\n    return 1\n```\n"
MESSAGES = [{"role": "user", "content": "hi"}]


def _write_fixture(root: Path, text: str, *, task_id: str = "T-1") -> None:
    directory = root / "responses" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "attempt-1.md").write_text(text, encoding="utf-8")


def _make(tmp_path: Path, *, quotas=None, failover=None) -> tuple[ModelFactory, Path]:
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    fixtures = tmp_path / "fx"
    _write_fixture(fixtures, GOOD)
    settings = Settings(
        mock=True,
        output_dir=tmp_path / "o",
        quotas=quotas or {},
        model_failover=failover or {},
    )
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=fixtures)
    return models, fixtures


def _bind(models: ModelFactory, alias: str) -> None:
    set_purpose(models.get(alias), "execute", task_id="T-1", attempt=1)


def _reserved(models: ModelFactory, alias: str, window: str = "daily"):
    return models._quota_tracker._get_reserved(alias, window)


def _usage(models: ModelFactory, alias: str, window: str = "daily"):
    return models._quota_tracker._get_usage(alias, window)


async def test_single_alias_quota_exhausted_blocks_before_first_chunk(tmp_path):
    models, _ = _make(
        tmp_path, quotas={"mock:primary": {"daily": {"max_calls": 0}}})
    alias = "mock:primary"
    _bind(models, alias)
    agen = models.stream_call(
        alias, MESSAGES, temperature=0, max_tokens=100,
        purpose="execute", role="backend_dev")
    with pytest.raises(QuotaExhaustedError):
        await agen.__anext__()
    await agen.aclose()


async def test_quota_exhausted_stream_fails_over_to_backup(tmp_path):
    models, _ = _make(
        tmp_path,
        quotas={"mock:primary": {"daily": {"max_calls": 0}},
                "mock:backup": {"daily": {"max_calls": 100}}},
        failover={"mock:primary": ["mock:backup"]},
    )
    _bind(models, "mock:primary")
    _bind(models, "mock:backup")
    parts = []
    async for chunk in models.stream_call(
        "mock:primary", MESSAGES, temperature=0, max_tokens=100,
        purpose="execute", role="backend_dev",
    ):
        parts.append(chunk.delta)
    assert "".join(parts).strip() != ""
    assert models.llm_calls == 1
    # 备用别名实际入账一次；primary 未预扣也未消耗
    assert _usage(models, "mock:backup").calls == 1
    assert _reserved(models, "mock:backup").calls == 0
    assert _usage(models, "mock:primary").calls == 0
    assert _reserved(models, "mock:primary").calls == 0


async def test_inflight_stream_reserves_quota_for_concurrent_call(tmp_path):
    """设防的核心：第一条流未消费完时预扣仍在，第二条流必须被拦。"""
    models, _ = _make(
        tmp_path, quotas={"mock:primary": {"daily": {"max_calls": 1}}})
    alias = "mock:primary"
    _bind(models, alias)

    first = models.stream_call(
        alias, MESSAGES, temperature=0, max_tokens=100,
        purpose="execute", role="backend_dev")
    await first.__anext__()  # 已起流：预扣 1 次占用配额
    assert _reserved(models, alias).calls == 1

    second = models.stream_call(
        alias, MESSAGES, temperature=0, max_tokens=100,
        purpose="execute", role="backend_dev")
    with pytest.raises(QuotaExhaustedError):
        await second.__anext__()
    await second.aclose()

    await first.aclose()  # 打断：部分用量入账、预扣归还
    assert _usage(models, alias).calls == 1
    assert _reserved(models, alias).calls == 0


async def test_complete_stream_settles_reservation_exactly_once(tmp_path):
    models, _ = _make(
        tmp_path, quotas={"mock:primary": {"daily": {"max_calls": 2}}})
    alias = "mock:primary"
    _bind(models, alias)

    async for _chunk in models.stream_call(
        alias, MESSAGES, temperature=0, max_tokens=100,
        purpose="execute", role="backend_dev",
    ):
        pass

    assert _usage(models, alias).calls == 1
    assert _reserved(models, alias).calls == 0
    assert _reserved(models, alias).tokens == 0
    # 只剩一次配额：第二次能过、第三次被拦（预扣没有把第一条流的估算吃掉）
    async for _chunk in models.stream_call(
        alias, MESSAGES, temperature=0, max_tokens=100,
        purpose="execute", role="backend_dev",
    ):
        pass
    with pytest.raises(QuotaExhaustedError):
        async for _chunk in models.stream_call(
            alias, MESSAGES, temperature=0, max_tokens=100,
            purpose="execute", role="backend_dev",
        ):
            pass


async def test_reservation_released_when_stream_fails_before_first_chunk(tmp_path):
    models, _ = _make(
        tmp_path, quotas={"mock:primary": {"daily": {"max_calls": 5}}})
    alias = "mock:primary"
    provider = models.get(alias)
    _bind(models, alias)

    async def _boom(*_args, **_kwargs):
        raise TransientError("upstream down")
        yield  # pragma: no cover —— 让它成为 async generator

    provider.stream_complete = _boom  # type: ignore[method-assign]

    with pytest.raises(TransientError):
        async for _chunk in models.stream_call(
            alias, MESSAGES, temperature=0, max_tokens=100,
            purpose="execute", role="backend_dev",
        ):
            pass

    assert _reserved(models, alias).calls == 0
    assert _usage(models, alias).calls == 0
