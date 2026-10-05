"""原生异步指数退避重试（spec §17.6）。

不引入 tenacity 等第三方重试库，用标准库实现：指数增长、带上限、带抖动，
并尊重服务端 ``Retry-After``（由调用方通过 ``retry_after`` 注入读取函数）。
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class TransientError(Exception):
    """可重试的瞬时故障：网络超时、连接错误、HTTP 429/500/502/503/504。"""


class PermanentError(Exception):
    """不可重试的错误：HTTP 400/401/403/404/422 等配置或鉴权问题。"""


def compute_delay(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter: float,
) -> float:
    """计算第 ``attempt`` 次重试前的等待秒数（``attempt`` 从 1 起）。

    先指数增长并按 ``max_delay`` 截断，再乘 ``[1 - jitter, 1 + jitter]`` 的
    抖动因子；抖动在截断之后施加，结果向下取 0 保证非负。
    """
    delay = min(max_delay, base_delay * 2 ** (attempt - 1))
    if jitter:
        delay *= 1 + random.uniform(-jitter, jitter)
    return max(0.0, float(delay))


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.3,
    retry_on: tuple[type[Exception], ...] = (TransientError,),
    on_retry: Callable[[int, Exception, float], None] | None = None,
    retry_after: Callable[[BaseException], float | None] | None = None,
) -> T:
    """调用 ``fn``，捕获 ``retry_on`` 中的异常并按退避策略重试。

    ``attempts`` 是总尝试次数（含首次）；``attempts=3`` 最多调用 ``fn`` 3 次、
    最多重试 2 次。不匹配 ``retry_on`` 的异常立即向上抛出。每次**即将重试前**
    调用 ``on_retry(attempt_index, exc, delay)``，``attempt_index`` 从 1 起。

    若提供了 ``retry_after`` 且其返回非 ``None``，该值（服务端 ``Retry-After``）
    覆盖本轮计算出的退避时长。``base_delay=0`` 是即时模式（测试用）：引擎完全不
    等待，连 ``retry_after`` 的覆盖值也不 sleep，但传给 ``on_retry`` 的仍是覆盖值。
    """
    if attempts < 1:
        raise ValueError(f"attempts 必须 >= 1，收到 {attempts}")
    for attempt_index in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:
            if attempt_index >= attempts:
                raise
            delay = compute_delay(
                attempt_index,
                base_delay=base_delay,
                max_delay=max_delay,
                jitter=jitter,
            )
            if retry_after is not None:
                override = retry_after(exc)
                if override is not None:
                    delay = override
            if on_retry is not None:
                on_retry(attempt_index, exc, delay)
            # base_delay=0 表示不等待；否则只在本轮确实需要等待时才 sleep。
            if base_delay > 0 and delay > 0:
                await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
