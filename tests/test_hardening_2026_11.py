"""2026-11 六层优化改造的回归测试。

钉住本轮定点改造的行为面，全部离线可跑：

1. RPM 滑动窗口超卖竞态修复（并发 ``_wait_window`` 窗口占用不超 ``max_rpm``）；
2. 复杂度校准器按 run 隔离（工厂实例注入优先于全局回退）；
3. ``TraceContext`` 追踪上下文 → ``llm_call`` 事件携带 ``task_id`` / ``attempt``；
4. ``WorkerOutcome.error_type`` 显式异常类型（审计不再依赖字符串反解析）；
5. ``setup_logging`` 的 JSON 结构化输出 + 脱敏协作；
6. trace 上下文的协程隔离与恢复。
"""

from __future__ import annotations

import asyncio
import logging
import types

import pytest

from agentcrew.config import Settings
from agentcrew.models.complexity import (
    ComplexityCalibrator,
    ComplexityLevel,
    evaluate,
    set_calibrator,
)
from agentcrew.models.factory import ModelFactory
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.utils.trace import (
    TraceContext,
    current_trace,
    reset_trace,
    set_trace,
)

# ── 夹具 ──────────────────────────────────────────────────────────────────

@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


def _task(**kwargs) -> Task:
    from agentcrew.artifacts.spec import ArtifactSpec

    defaults = dict(
        id="T-1", title="t", description="写代码", role="backend_dev",
        acceptance_criteria=["一", "二", "三"],
        deliverables=[ArtifactSpec(path="a.py", kind="python", description="d")],
    )
    defaults.update(kwargs)
    return Task.model_validate(defaults)


# ── 1. RPM 滑动窗口：并发不超卖 ───────────────────────────────────────────

class _RecordingList(list):
    """记录历史最大长度的列表（探测窗口占用峰值）。"""

    def __init__(self) -> None:
        super().__init__()
        self.max_len = 0

    def append(self, value: object) -> None:
        super().append(value)
        self.max_len = max(self.max_len, len(self))


async def test_rpm_window_concurrent_never_oversells(settings, monkeypatch):
    """N 个协程并发过窗：窗口占用峰值不得超过 max_rpm（修复前会超卖）。

    用可推进的假时钟 + 推进时钟的假 sleep：sleep(delay) 把时钟拨到最早时间戳
    滑出窗口的时刻，与真实 asyncio.sleep 的语义等价但瞬时完成。
    """
    f = ModelFactory(settings)
    clock = {"now": 100.0}

    def fake_monotonic() -> float:
        return clock["now"]

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        clock["now"] += delay  # sleep 到点 = 时钟推进
        await real_sleep(0)     # 让出事件循环，给其他协程入场机会

    monkeypatch.setattr(
        "agentcrew.models.factory.time", types.SimpleNamespace(monotonic=fake_monotonic))
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    recording = _RecordingList()
    f._rpm_timestamps["p"] = recording  # setdefault 命中预置的记录列表

    MAX_RPM = 2
    await asyncio.gather(*(f._wait_for_rpm("p", MAX_RPM) for _ in range(10)))

    # 修复前：多个协程基于同一份满窗快照 sleep 后同时入列，占用峰值可达
    # max_rpm + N-1；修复后「清窗→检查→入列」在 per-key 锁内原子完成。
    assert recording.max_len <= MAX_RPM, (
        f"窗口占用峰值 {recording.max_len} 超过 max_rpm={MAX_RPM}（超卖）")
    # 满窗等待者会先弹出过期时间戳再入列，故最终长度不代表总请求数；
    # 无超卖 + gather 无异常（全部协程走完）即是完整契约。


async def test_rpm_window_single_caller_behavior_unchanged(settings, monkeypatch):
    """单调用者路径与改造前一致：睡到最早时间戳滑出窗口、入列一次。"""
    f = ModelFactory(settings)
    clock = {"now": 10.0}

    def fake_monotonic() -> float:
        return clock["now"]

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        clock["now"] += delay
        await real_sleep(0)

    monkeypatch.setattr(
        "agentcrew.models.factory.time", types.SimpleNamespace(monotonic=fake_monotonic))
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    f._rpm_timestamps["p"] = [0.0]
    await f._wait_for_rpm("p", 1)
    # 0.0 + 60 - 10 = 50s 后窗口滑出，入列时刻为 60.0。
    assert f._rpm_timestamps["p"] == [60.0]


# ── 2. 复杂度校准器：工厂实例注入优先，全局回退兼容 ───────────────────────

@pytest.fixture(autouse=True)
def _reset_global_calibrator():
    set_calibrator(None)
    yield
    set_calibrator(None)


class TestCalibratorInjection:
    def _factory(self, tmp_path, calibrator=None, **kwargs) -> ModelFactory:
        s = Settings.load().model_copy(
            update={"default_model": "openai:default",
                    "model_tiers": {"low": "openai:cheap",
                                    "medium": "openai:mid",
                                    "high": "openai:powerful"},
                    **kwargs})
        return ModelFactory(
            s, ledger=Ledger(tmp_path / "l.jsonl", run_id="R"),
            calibrator=calibrator)

    def test_injected_calibrator_takes_priority(self, tmp_path):
        """工厂实例校准器在场时优先使用，不受全局单例影响。"""
        task = _task(role="backend_dev")
        injected = ComplexityCalibrator()
        for _ in range(3):  # 连续失败 3 次才触发上调（MEDIUM → HIGH）
            injected.record(task, success=False, attempts=3)
        factory = self._factory(tmp_path, calibrator=injected)
        global_cal = ComplexityCalibrator()               # 全局空：不变调
        set_calibrator(global_cal)
        assert factory.alias_for_task(task) == "openai:powerful"

    def test_falls_back_to_global_when_not_injected(self, tmp_path):
        """未注入实例时回退读全局（直连工厂 + set_calibrator 的旧用法兼容）。"""
        factory = self._factory(tmp_path)
        task = _task(role="backend_dev")
        cal = ComplexityCalibrator()
        for _ in range(3):
            cal.record(task, success=False, attempts=3)
        set_calibrator(cal)
        assert factory.alias_for_task(task) == "openai:powerful"

    def test_concurrent_runs_do_not_pollute_each_other(self):
        """两个 Crew 的校准器互相独立（修复点：原全局单例被后者全量替换）。"""
        from agentcrew.crew.crew import Crew

        s = Settings.load().model_copy(update={"gate": "none"})
        crew_a = Crew(s)
        crew_b = Crew(s)
        assert crew_a._calibrator is not crew_b._calibrator
        task = _task(role="backend_dev")
        for _ in range(3):
            crew_a._calibrator.record(task, success=False, attempts=3)
        # run B 的校准器不受 run A 的失败记录影响（修复前 B 构造时已把 A 清空）。
        assert crew_b._calibrator.calibrate(task) == evaluate(task)
        assert crew_a._calibrator.calibrate(task) == ComplexityLevel.HIGH


# ── 3. TraceContext → llm_call 事件归因 ──────────────────────────────────

class _CapturingSink:
    """捕获 emit 的事件（EventSink 协议的最小实现）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **data) -> None:
        self.events.append((event, data))


class TestTraceAccounting:
    def _factory(self, tmp_path) -> ModelFactory:
        s = Settings.load()
        return ModelFactory(s, ledger=_CapturingSink())

    def test_llm_call_carries_task_id_when_trace_set(self, tmp_path):
        from agentcrew.models.providers.base import CompletionResult

        factory = self._factory(tmp_path)
        token = set_trace(TraceContext(task_id="T-9", role="dev", attempt=2))
        try:
            factory._account(
                CompletionResult(
                    provider="mock", model="m", text="",
                    prompt_tokens=10, completion_tokens=5, latency_ms=7),
                purpose="execute", role="dev")
        finally:
            reset_trace(token)
        event, data = factory._ledger.events[-1]
        assert event == "llm_call"
        assert data["task_id"] == "T-9"
        assert data["attempt"] == 2

    def test_llm_call_unchanged_without_trace(self, tmp_path):
        """追踪未设置时事件无 task_id/attempt 字段（形状与改造前一致）。"""
        from agentcrew.models.providers.base import CompletionResult

        factory = self._factory(tmp_path)
        factory._account(
            CompletionResult(
                provider="mock", model="m", text="",
                prompt_tokens=10, completion_tokens=5, latency_ms=7),
            purpose="execute", role="dev")
        _, data = factory._ledger.events[-1]
        assert "task_id" not in data
        assert "attempt" not in data


# ── 4. WorkerOutcome.error_type ──────────────────────────────────────────

def test_error_outcome_carries_error_type():
    from agentcrew.crew.scheduler import _error_outcome

    outcome = _error_outcome("T-1", ValueError(" bad: input"))
    assert outcome.error_type == "ValueError"
    assert outcome.error.startswith("ValueError:")


def test_emit_error_prefers_explicit_type(tmp_path):
    from agentcrew.crew.scheduler import Scheduler

    class _BoardStub:
        def get(self, task_id):  # pragma: no cover - _emit_error 不触板
            raise AssertionError("不应触板")

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    sch = Scheduler.__new__(Scheduler)
    sch._ledger = ledger
    sch._emit_error("agent.execute", "T-1", "SomeError: boom",
                    error_type="SomeError")
    event = next(ledger.events("error"))
    assert event.data["exc_type"] == "SomeError"


# ── 5. setup_logging 的 JSON 输出 ────────────────────────────────────────

def test_setup_logging_json_format(tmp_path, capsys):
    from agentcrew.utils.logger import setup_logging

    setup_logging("INFO", ["supersecret"], json_format=True)
    logging.getLogger("probe").warning("hello sk-abc123456")
    err = capsys.readouterr().err
    payload = __import__("json").loads(err.strip().splitlines()[-1])
    assert payload["level"] == "WARNING"
    assert "supersecret" not in err
    assert "***" in payload["message"]


def test_setup_logging_text_format_default(tmp_path, capsys):
    from agentcrew.utils.logger import setup_logging

    setup_logging("INFO", [])
    logging.getLogger("probe2").warning("plain message")
    err = capsys.readouterr().err.strip().splitlines()[-1]
    # 纯文本模式保持改造前的裸消息输出（无级别前缀），既有消费方不受影响。
    assert err == "plain message"
    assert not err.lstrip().startswith("{")


# ── 6. trace 上下文：协程隔离与恢复 ──────────────────────────────────────

async def test_trace_context_isolated_across_tasks():
    """contextvar 按协程隔离：并发任务的 trace 互不串扰。"""
    seen: dict[str, str | None] = {}

    async def worker(name: str, task_id: str) -> None:
        token = set_trace(TraceContext(task_id=task_id))
        try:
            await asyncio.sleep(0)
            seen[name] = current_trace().task_id  # type: ignore[union-attr]
        finally:
            reset_trace(token)

    await asyncio.gather(
        worker("a", "T-A"), worker("b", "T-B"))
    assert seen == {"a": "T-A", "b": "T-B"}
    assert current_trace() is None
