"""原生异步退避重试的行为测试（spec §17.6）。"""

import pytest

from agentcrew.utils.retry import (
    PermanentError,
    TransientError,
    compute_delay,
    retry_async,
)


class RetryAfterError(TransientError):
    """带 ``Retry-After`` 值的可重试错误。"""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


async def test_returns_immediately_on_success():
    calls = []

    async def fn():
        calls.append(1)
        return "ok"

    assert await retry_async(fn, base_delay=0) == "ok"
    assert len(calls) == 1


async def test_retries_transient_then_succeeds():
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("boom")
        return "ok"

    assert await retry_async(fn, attempts=4, base_delay=0) == "ok"
    assert len(calls) == 3


async def test_exhausts_attempts_and_reraises():
    calls = []

    async def fn():
        calls.append(1)
        raise TransientError("always")

    with pytest.raises(TransientError):
        await retry_async(fn, attempts=3, base_delay=0)
    assert len(calls) == 3


async def test_permanent_error_is_not_retried():
    calls = []

    async def fn():
        calls.append(1)
        raise PermanentError("nope")

    with pytest.raises(PermanentError):
        await retry_async(fn, attempts=5, base_delay=0)
    assert len(calls) == 1


async def test_on_retry_reports_attempt_and_delay():
    seen = []

    async def fn():
        raise TransientError("x")

    with pytest.raises(TransientError):
        await retry_async(
            fn,
            attempts=3,
            base_delay=0,
            on_retry=lambda n, e, d: seen.append((n, type(e).__name__)),
        )
    assert seen == [(1, "TransientError"), (2, "TransientError")]


def test_delay_is_capped_and_jittered():
    assert compute_delay(1, base_delay=1.0, max_delay=30.0, jitter=0.0) == 1.0
    assert compute_delay(10, base_delay=1.0, max_delay=30.0, jitter=0.0) == 30.0
    d = compute_delay(3, base_delay=1.0, max_delay=30.0, jitter=0.3)
    assert 2.8 <= d <= 5.2


def test_delay_is_never_negative(monkeypatch):
    # 极端抖动因子（|multiplier| > 1 且为负）下结果被夹到 0。
    monkeypatch.setattr("agentcrew.utils.retry.random.uniform", lambda a, b: -2.0)
    assert compute_delay(1, base_delay=1.0, max_delay=30.0, jitter=1.5) == 0.0


async def test_retry_after_overrides_computed_backoff():
    delays = []

    async def fn():
        raise RetryAfterError("429", 12.0)

    with pytest.raises(RetryAfterError):
        await retry_async(
            fn,
            attempts=2,
            base_delay=0,
            jitter=0,
            retry_after=lambda e: getattr(e, "retry_after", None),
            on_retry=lambda n, e, d: delays.append(d),
        )
    assert delays == [12.0]


async def test_retry_after_returning_none_falls_back_to_backoff():
    delays = []

    async def fn():
        raise TransientError("boom")

    with pytest.raises(TransientError):
        await retry_async(
            fn,
            attempts=2,
            base_delay=0,
            jitter=0,
            retry_after=lambda e: None,
            on_retry=lambda n, e, d: delays.append(d),
        )
    assert delays == [0.0]
