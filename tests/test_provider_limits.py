"""V6 P0.5：per-provider 并发信号量与 RPM 滑动窗口限速测试。

覆盖：
- 不同 provider 使用独立信号量（互不阻塞）；
- 同一 provider 复用同一信号量实例（缓存命中）；
- max_concurrency=None 时回退到全局 settings.max_concurrency；
- 超过 RPM 上限时按滑动窗口等待；
- max_rpm=None 时不限制、不入时间戳。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agentcrew.config import Settings
from agentcrew.models.factory import ModelFactory


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


# ── 1. per-provider 信号量独立性 ─────────────────────────────────────────

def test_per_provider_semaphore_independent(settings):
    """不同 provider 名应得到不同的信号量实例。"""
    f = ModelFactory(settings)
    s1 = f._get_provider_semaphore("openai", 2)
    s2 = f._get_provider_semaphore("anthropic", 3)
    assert s1 is not s2, "不同 provider 必须用独立信号量"


def test_per_provider_semaphore_cached(settings):
    """同一 provider 重复请求应返回缓存的同一实例。"""
    f = ModelFactory(settings)
    s1 = f._get_provider_semaphore("openai", 2)
    s2 = f._get_provider_semaphore("openai", 2)
    assert s1 is s2


def test_per_provider_semaphore_uses_preset_capacity(settings):
    """preset 配置了 max_concurrency 时，信号量容量取该值。"""
    f = ModelFactory(settings)
    sem = f._get_provider_semaphore("openai", 5)
    # asyncio.Semaphore._value 是初始可用许可数（3.10+ 公开可用的内部属性）。
    assert sem._value == 5


def test_per_provider_semaphore_falls_back_to_global(settings):
    """max_concurrency=None 时回退到全局 settings.max_concurrency。"""
    settings = settings.model_copy(update={"max_concurrency": 7})
    f = ModelFactory(settings)
    sem = f._get_provider_semaphore("someprovider", None)
    assert sem._value == 7


# ── 2. RPM 滑动窗口 ───────────────────────────────────────────────────────

async def test_rpm_limit_waits(settings, monkeypatch):
    """窗口内已达 max_rpm 时，应 sleep 到最早时间戳滑出 60s 窗口。"""
    f = ModelFactory(settings)
    # 预置一个时间戳在 t=0，max_rpm=1 → 第二次调用必须等 60s - 当前时刻。
    f._rpm_timestamps["p"] = [0.0]

    now = 10.0
    monkeypatch.setattr(time, "monotonic", lambda: now)

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await f._wait_for_rpm("p", 1)
    # 最早时间戳 0.0 + 60 - 当前 10 = 50s
    assert slept == [pytest.approx(50.0, abs=1e-6)]
    # 本次调用结束后应把当前时刻入列
    assert f._rpm_timestamps["p"][-1] == 10.0


async def test_no_rpm_limit_when_none(settings, monkeypatch):
    """max_rpm=None 时不等待、也不入时间戳。"""
    f = ModelFactory(settings)
    f._rpm_timestamps["p"] = [0.0, 1.0, 2.0]

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await f._wait_for_rpm("p", None)
    assert slept == []
    # 时间戳列表不应被新增
    assert f._rpm_timestamps["p"] == [0.0, 1.0, 2.0]


async def test_rpm_cleans_old_timestamps(settings, monkeypatch):
    """超过 60s 的旧时间戳应被清理，不占用新窗口配额。"""
    f = ModelFactory(settings)
    # 旧时间戳在 t=0，当前 t=100（窗口外 40s）→ 应被清理。
    f._rpm_timestamps["p"] = [0.0]
    now = 100.0
    monkeypatch.setattr(time, "monotonic", lambda: now)

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # max_rpm=1：清理后窗口内 0 条，不应 sleep。
    await f._wait_for_rpm("p", 1)
    assert slept == []
    # 旧时间戳已被清掉，只剩当前时刻入列。
    assert f._rpm_timestamps["p"] == [100.0]


# ── 3. 端到端：call() 路径实际走 per-provider 信号量 ─────────────────────

async def test_call_uses_per_provider_semaphore(settings, monkeypatch):
    """call() 路径应从 _provider_semaphores 取锁，而不是全局 _llm_semaphore。"""
    from agentcrew.models.providers.mock import MockProvider, set_purpose

    settings = settings.model_copy(update={"max_concurrency": 2})
    f = ModelFactory(settings, ledger=None)
    p = f.get("mock:m")
    assert isinstance(p, MockProvider)
    set_purpose(p, "execute", role="r", task_id="T-1", attempt=1)

    await f.call(
        "mock:m", [{"role": "user", "content": "hi"}],
        temperature=0, max_tokens=10, purpose="execute", role="r")

    # 调用后应在 _provider_semaphores 里为 "mock" 建了一把锁。
    assert "mock" in f._provider_semaphores
    # 容量应等于全局 max_concurrency（mock 预设未配置 max_concurrency）。
    assert f._provider_semaphores["mock"]._value == 2
