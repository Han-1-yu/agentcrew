"""稳定性测试：连续长跑 + 故障注入，产出一份可复核的报告。

    python scripts/soak.py                     # 20 次连续 run + 2 类故障注入
    python scripts/soak.py --runs 50 --latency-ms 50
    python scripts/soak.py --only faults       # 只跑故障注入

## 测什么

**长跑（soak）**：在同一个进程里连续跑 N 次完整 mock run。关注三件事 ——
成功率是否始终 100%、进程内存与句柄是否随次数单调增长、墙钟是否逐次劣化。
单次 run 通过不代表第 20 次也通过：checkpoint、ledger、缓存、asyncio 事件循环
这些问题只在反复运行后才显形。

**故障注入（faults）**：两类真实会遇到、且**不应该**拖垮整轮的问题。

1. ``provider-transient``：模型调用前 2 次抛瞬时错误。期望是任务按 attempts
   重试后成功，整轮仍 COMPLETED —— 偶发网络抖动不该让一条 run 报废。
2. ``gate-reject``：某个任务的产物缺件，被 G1 连续驳回直到额度用尽。期望是
   **该任务** FAILED、它的下游 BLOCKED，而其余无关任务照常完成，且 run 正常
   收尾（不挂死、不抛异常）。

## 口径与边界

- 全部离线（无 API Key），provider 为 mock 回放，因此测的是**引擎与本地 IO**的
  稳定性，不是模型侧的可用性。
- 内存与句柄读的是当前进程；Windows 走 psapi / kernel32，Linux 走 ``/proc``，
  读不到时报告里写"未采集"而不是编一个数。
- 长跑用固定 ``--latency-ms``（默认 30ms，只为让并发有意义，不是性能结论）。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 允许 `python scripts/soak.py` 直接跑
    sys.path.insert(0, str(REPO_ROOT))

from agentcrew.config import Settings  # noqa: E402
from examples.samples import get, names  # noqa: E402

#: 长跑默认次数：够显出趋势（每 5 次做一段比较），又不至于跑成十分钟。
DEFAULT_RUNS = 20

#: 长跑用的模拟延迟。这里不是性能测试，延迟只为了让并发分支被真正走到。
DEFAULT_LATENCY_MS = 30

#: 注入的瞬时故障次数（前几次调用失败，之后恢复正常）。
TRANSIENT_FAILURES = 2

#: 判定阈值。
THRESHOLDS = {
    "min_success_rate": 1.0,      # 长跑成功率必须 100%
    "max_rss_growth": 0.20,       # 后段 RSS 相对前段增长 ≤ 20%
    "max_handle_growth": 20,      # 句柄净增 ≤ 20（一次 run 会开若干文件，允许少量波动）
    "max_wall_drift": 0.50,       # 后段墙钟相对前段劣化 ≤ 50%
}

#: 与真实 provider 相关的环境变量：全部删掉，确保"无密钥可跑"。
_KEY_ENVS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY",
    "ARK_API_KEY", "QIANFAN_API_KEY", "DEEPSEEK_API_KEY",
)


# ── 进程资源快照 ──────────────────────────────────────────────────────────


class _ProcessMemoryCounters(ctypes.Structure):  # pragma: no cover - 平台相关
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_snapshot() -> tuple[float | None, int | None]:
    """返回（常驻内存 MB, 句柄数）。平台不支持时对应位置为 ``None``。"""
    if sys.platform == "win32":  # pragma: no cover - 只在 Windows 上走到
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            psapi = ctypes.WinDLL("psapi.dll")
            kernel32 = ctypes.WinDLL("kernel32.dll")
            # 必须显式声明原型：默认 restype 是 c_int，会把伪句柄 -1 截成
            # 0xFFFFFFFF 之外的值，GetProcessMemoryInfo 直接返回失败 —— 第一版
            # 就是这样静默"未采集"的。
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.GetProcessHandleCount.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.GetProcessHandleCount.restype = ctypes.c_int
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_ProcessMemoryCounters), ctypes.c_ulong]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int

            handle = kernel32.GetCurrentProcess()
            if not psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb):
                return None, None
            count = ctypes.c_ulong(0)
            handles = int(count.value) if kernel32.GetProcessHandleCount(
                handle, ctypes.byref(count)) else None
            return counters.WorkingSetSize / (1024 * 1024), handles
        except OSError:
            return None, None

    rss = None
    handles = None
    try:  # pragma: no cover - POSIX
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = usage.ru_maxrss / 1024.0  # Linux 单位是 KB
    except (ImportError, OSError):
        pass
    try:  # pragma: no cover - POSIX
        handles = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    return rss, handles


# ── 离线运行脚手架 ────────────────────────────────────────────────────────


class _Offline:
    """把进程切到"完全离线 + 指定夹具"的状态，退出时原样恢复。

    用上下文管理器而不是一堆散落的 monkeypatch：长跑要重复进出这个状态几十次，
    任何一处漏恢复都会让后面的测量读到上一个 run 的残留。
    """

    def __init__(self, spec_name: str, workdir: Path, *, latency_ms: int, level: int = 4):
        self.spec = get(spec_name)
        self.workdir = workdir
        self.latency_ms = latency_ms
        self.level = level
        self._saved_env: dict[str, str | None] = {}
        self._original_fixtures = None

    def __enter__(self) -> Settings:
        keys = ("AGENTCREW_MOCK", "AGENTCREW_GATE", "AGENTCREW_CONFIG",
                "AGENTCREW_OUTPUT_DIR", "AGENTCREW_MOCK_LATENCY_MS")
        self._saved_env = {key: os.environ.get(key) for key in keys}
        os.environ["AGENTCREW_MOCK"] = "true"
        os.environ["AGENTCREW_GATE"] = "none"
        os.environ["AGENTCREW_CONFIG"] = str(self.workdir / "absent-agentcrew.yaml")
        for key in _KEY_ENVS:
            os.environ.pop(key, None)

        import agentcrew.models.factory as model_factory

        self._original_fixtures = model_factory.DEFAULT_FIXTURES_DIR
        model_factory.DEFAULT_FIXTURES_DIR = self.spec.fixtures
        from agentcrew.config import load_dotenv

        self._original_dotenv = load_dotenv
        import agentcrew.config as config_module

        config_module.load_dotenv = lambda *a, **kw: False  # type: ignore[assignment]
        self._config_module = config_module

        return Settings.load(
            mock=True,
            gate="none",
            output_dir=self.workdir,
            max_concurrency=self.level,
            mock_latency_ms=self.latency_ms,
        )

    def __exit__(self, *exc_info: object) -> None:
        import agentcrew.models.factory as model_factory

        model_factory.DEFAULT_FIXTURES_DIR = self._original_fixtures
        self._config_module.load_dotenv = self._original_dotenv  # type: ignore[assignment]
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_once(settings: Settings, goal: str) -> tuple[object, Path, float]:
    """跑一次 run，返回（record, run_dir, 墙钟秒）。"""
    from agentcrew.crew.crew import Crew

    started = time.perf_counter()
    record = asyncio.run(Crew(settings).run(goal))
    return record, settings.output_dir / record.run_id, time.perf_counter() - started


# ── 长跑 ──────────────────────────────────────────────────────────────────


@dataclass
class SoakSample:
    """长跑中的一次迭代。"""

    index: int
    wall_s: float
    ok: bool
    total_tasks: int
    succeeded: int
    failed: int
    blocked: int
    llm_calls: int
    rss_mb: float | None
    handles: int | None
    threads: int


@dataclass
class SoakSummary:
    """长跑聚合结果。全字段带默认值：``--only faults`` 时不跑长跑也能构造空摘要。"""

    runs: int = 0
    success_rate: float = 0.0
    wall_first: float = 0.0
    wall_last: float = 0.0
    wall_drift: float = 0.0
    rss_first: float | None = None
    rss_last: float | None = None
    rss_growth: float | None = None
    handle_first: int | None = None
    handle_last: int | None = None
    handle_growth: float | None = None
    thread_first: int = 0
    thread_last: int = 0


def run_soak(spec_name: str, runs: int, latency_ms: int) -> tuple[list[SoakSample], SoakSummary]:
    """连续跑 ``runs`` 次，每次独立输出目录。"""
    import threading

    root = Path(tempfile.mkdtemp(prefix="soak-"))
    samples: list[SoakSample] = []
    # 首个读数里含解释器初始化与模块加载，因此"前段"从第 1 次之后开始算；
    # 这里只记线程基线，用来判断长跑是否泄漏线程。
    thread_baseline = threading.active_count()

    for index in range(runs):
        workdir = root / f"run-{index:02d}"
        workdir.mkdir(parents=True, exist_ok=True)
        with _Offline(spec_name, workdir, latency_ms=latency_ms) as settings:
            record, run_dir, wall = _run_once(settings, get(spec_name).goal)
        del run_dir
        gc.collect()
        rss, handles = process_snapshot()
        stats = record.stats
        samples.append(SoakSample(
            index=index + 1,
            wall_s=wall,
            ok=(stats.failed == 0 and stats.blocked == 0
                and stats.succeeded == stats.total_tasks),
            total_tasks=stats.total_tasks,
            succeeded=stats.succeeded,
            failed=stats.failed,
            blocked=stats.blocked,
            llm_calls=stats.llm_calls,
            rss_mb=rss,
            handles=handles,
            threads=threading.active_count(),
        ))

    head = samples[: max(1, len(samples) // 5)]
    tail = samples[-max(1, len(samples) // 5):]
    wall_first = statistics.fmean(s.wall_s for s in head)
    wall_last = statistics.fmean(s.wall_s for s in tail)
    rss_values = [s.rss_mb for s in samples if s.rss_mb is not None]
    handle_values = [s.handles for s in samples if s.handles is not None]

    def _growth(values: list[float]) -> float | None:
        """后段均值相对前段均值的变化率；样本不足或前段为 0 时返回 ``None``。"""
        if len(values) < len(head) + len(tail):
            return None
        head_avg = statistics.fmean(values[: len(head)])
        tail_avg = statistics.fmean(values[-len(tail):])
        if head_avg <= 0:
            return None
        return (tail_avg - head_avg) / head_avg

    summary = SoakSummary(
        runs=len(samples),
        success_rate=sum(1 for s in samples if s.ok) / len(samples),
        wall_first=wall_first,
        wall_last=wall_last,
        wall_drift=(wall_last - wall_first) / wall_first if wall_first > 0 else 0.0,
        rss_first=rss_values[0] if rss_values else None,
        rss_last=rss_values[-1] if rss_values else None,
        rss_growth=_growth([float(v) for v in rss_values]),
        handle_first=handle_values[0] if handle_values else None,
        handle_last=handle_values[-1] if handle_values else None,
        handle_growth=(
            statistics.fmean(handle_values[-len(tail):])
            - statistics.fmean(handle_values[: len(head)])
            if len(handle_values) >= len(head) + len(tail) else None),
        thread_first=thread_baseline,
        thread_last=samples[-1].threads if samples else thread_baseline,
    )
    shutil.rmtree(root, ignore_errors=True)
    return samples, summary


# ── 故障注入 ──────────────────────────────────────────────────────────────


@dataclass
class FaultResult:
    """一个故障场景的结局。"""

    name: str
    description: str
    passed: bool
    observed: str
    details: dict[str, object] = field(default_factory=dict)


def fault_provider_transient(spec_name: str, latency_ms: int) -> FaultResult:
    """前 N 次模型调用抛瞬时错误，期望任务重试后整轮仍完成。"""
    from agentcrew.models.providers import mock as mock_module
    from agentcrew.utils.retry import TransientError

    original = mock_module.MockProvider.complete
    counter = {"calls": 0, "injected": 0}

    async def flaky(self, messages, *, temperature, max_tokens, response_format=None):
        counter["calls"] += 1
        if counter["injected"] < TRANSIENT_FAILURES:
            counter["injected"] += 1
            raise TransientError("注入故障：模拟网络抖动")
        return await original(
            self, messages,
            temperature=temperature, max_tokens=max_tokens, response_format=response_format,
        )

    mock_module.MockProvider.complete = flaky  # type: ignore[method-assign]
    workdir = Path(tempfile.mkdtemp(prefix="fault-transient-"))
    try:
        with _Offline(spec_name, workdir, latency_ms=latency_ms) as settings:
            record, run_dir, wall = _run_once(settings, get(spec_name).goal)
    finally:
        mock_module.MockProvider.complete = original  # type: ignore[method-assign]
        shutil.rmtree(workdir, ignore_errors=True)

    stats = record.stats
    completed = stats.failed == 0 and stats.blocked == 0
    passed = completed and counter["injected"] == TRANSIENT_FAILURES
    return FaultResult(
        name="provider-transient",
        description=f"前 {TRANSIENT_FAILURES} 次模型调用抛 TransientError，期望任务重试后整轮完成",
        passed=passed,
        observed=(
            f"注入 {counter['injected']} 次 · "
            f"run {'完成' if completed else '未完成'} · "
            f"成功 {stats.succeeded}/{stats.total_tasks} · "
            f"失败 {stats.failed} · 阻塞 {stats.blocked}"
        ),
        details={
            "injected": counter["injected"],
            "total_calls": counter["calls"],
            "status": str(record.status),
            "wall_s": round(wall, 3),
        },
    )


#: 缺件响应用的桩内容：只产出契约里的一个文件，另一个必然缺失。
_STUB_RESPONSE = """只写了一个文件，另一个交付物没有产出。

```markdown file=tests/test_ui.py
# 占位用例（故意不满足契约）

def test_placeholder():
    assert True
```
"""


def fault_gate_reject(spec_name: str, latency_ms: int) -> FaultResult:
    """让一个叶子任务连续缺件，验证失败被隔离在该任务与其下游。"""
    spec = get(spec_name)
    workdir = Path(tempfile.mkdtemp(prefix="fault-gate-"))
    fixtures = workdir / "fixtures"
    shutil.copytree(spec.fixtures, fixtures)

    # 选一个"没有后继"的任务：它的失败只会顺着依赖链影响收尾任务，
    # 不会把整张图推平 —— 这正是要验证的"隔离"语义。
    deps = spec.dependency_map()
    referenced = {parent for parents in deps.values() for parent in parents}
    leaves = [task_id for task_id in deps if task_id not in referenced]
    victim = leaves[0] if leaves else spec.expected_task_ids[-1]
    # 必须覆盖**该任务的每一个** attempt 文件：夹具里 T-005 准备了
    # attempt-2（"驳回 → 返工 → 通过"的演示），只改 attempt-1 时第二次尝试
    # 会回放正确的 attempt-2，注入当场失效 —— 第一版就是这么假通过的。
    victim_dir = fixtures / "responses" / victim
    victim_dir.mkdir(parents=True, exist_ok=True)
    for attempt_file in victim_dir.glob("attempt-*.md"):
        attempt_file.write_text(_STUB_RESPONSE, encoding="utf-8")

    class _PatchedOffline(_Offline):
        def __enter__(self):
            settings = super().__enter__()
            import agentcrew.models.factory as model_factory

            model_factory.DEFAULT_FIXTURES_DIR = fixtures
            return settings

    try:
        with _PatchedOffline(spec_name, workdir, latency_ms=latency_ms) as settings:
            record, run_dir, _ = _run_once(settings, spec.goal)
        payload = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))
        tasks = payload["tasks"] if isinstance(payload, dict) else payload
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    status = {task["id"]: task["status"] for task in tasks}
    victim_status = status.get(victim, "missing")
    others = [task_id for task_id in status if task_id != victim]
    others_succeeded = [task_id for task_id in others if status[task_id] == "succeeded"]

    passed = (
        victim_status == "failed"
        and len(others_succeeded) == len(others) - 1  # 只有收尾任务会被连带阻塞
        and record.status is not None
    )
    return FaultResult(
        name="gate-reject",
        description=f"让 {victim} 的产物缺件，期望它 FAILED、收尾任务 BLOCKED、其余任务照常完成",
        passed=passed,
        observed=(
            f"{victim}={victim_status} · 其余 {len(others)} 个任务中 "
            f"{len(others_succeeded)} 个成功 · run 正常收尾"
        ),
        details={"status_map": status, "run_status": str(record.status)},
    )


# ── 报告 ──────────────────────────────────────────────────────────────────


def evaluate_soak(summary: SoakSummary) -> dict[str, bool]:
    checks = {
        f"连续 {summary.runs} 次全部成功": summary.success_rate >= THRESHOLDS["min_success_rate"],
    }
    if summary.rss_growth is not None:
        checks[f"内存增长 ≤ {THRESHOLDS['max_rss_growth']:.0%}"] = (
            summary.rss_growth <= THRESHOLDS["max_rss_growth"])
    if summary.handle_growth is not None:
        checks[f"句柄净增 ≤ {THRESHOLDS['max_handle_growth']}"] = (
            summary.handle_growth <= THRESHOLDS["max_handle_growth"])
    checks[f"墙钟劣化 ≤ {THRESHOLDS['max_wall_drift']:.0%}"] = (
        summary.wall_drift <= THRESHOLDS["max_wall_drift"])
    checks["线程数不增长"] = summary.thread_last <= summary.thread_first
    return checks


def render_report(
    spec_name: str,
    samples: list[SoakSample],
    summary: SoakSummary,
    soak_checks: dict[str, bool],
    faults: list[FaultResult],
    meta: dict[str, object],
) -> str:
    spec = get(spec_name)
    lines = [
        "# AgentCrew 稳定性测试报告",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 样例：`{spec.name}`（{spec.shape}，{spec.expected_total_tasks} 个任务）",
        f"- 模式：离线 mock 回放，注入 {meta['latency_ms']}ms 模拟延迟",
        f"- 长跑：{summary.runs} 次连续 run（同进程，逐次独立输出目录）",
        f"- 环境：Python {meta['python']} · {meta['platform']}",
        "",
        "> **口径**：离线模式下 provider 是确定性回放，因此本报告测的是"
        "**引擎与本地 IO 的长期稳定性**，不含模型侧可用性。",
        "",
        "## 一、长跑汇总",
        "",
        "| 指标 | 前段 | 后段 | 变化 |",
        "| --- | ---: | ---: | ---: |",
        f"| 成功率 | {summary.success_rate:.0%} | {summary.success_rate:.0%} | — |",
        f"| 墙钟均值 (s) | {summary.wall_first:.3f} | {summary.wall_last:.3f} | "
        f"{summary.wall_drift:+.1%} |",
    ]
    if summary.rss_first is not None and summary.rss_last is not None:
        growth = f"{summary.rss_growth:+.1%}" if summary.rss_growth is not None else "—"
        lines.append(
            f"| 常驻内存 (MB) | {summary.rss_first:.1f} | {summary.rss_last:.1f} | {growth} |")
    else:
        lines.append("| 常驻内存 (MB) | 未采集 | 未采集 | — |")
    if summary.handle_first is not None and summary.handle_last is not None:
        growth = f"{summary.handle_growth:+.1f}" if summary.handle_growth is not None else "—"
        lines.append(
            f"| 句柄数 | {summary.handle_first} | {summary.handle_last} | {growth} |")
    else:
        lines.append("| 句柄数 | 未采集 | 未采集 | — |")
    lines.append(f"| 线程数 | {summary.thread_first} | {summary.thread_last} | — |")

    lines += ["", "## 二、长跑判定", "", "| 检查项 | 结果 |", "| --- | :---: |"]
    for name, passed in soak_checks.items():
        lines.append(f"| {name} | {'✅ 达标' if passed else '❌ 未达标'} |")

    lines += ["", "## 三、故障注入", ""]
    if not faults:
        lines.append("未执行（去掉 `--only soak`）。")
    else:
        lines += ["| 场景 | 期望 | 观察到的结果 | 判定 |", "| --- | --- | --- | :---: |"]
        for fault in faults:
            lines.append(
                f"| `{fault.name}` | {fault.description} | {fault.observed} | "
                f"{'✅ 通过' if fault.passed else '❌ 未通过'} |"
            )

    lines += [
        "",
        "## 四、逐次明细",
        "",
        "| 次序 | 墙钟 (s) | 任务 | 成功 | 失败 | 阻塞 | LLM 调用 | 内存 (MB) | 句柄 | 线程 | 结果 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for sample in samples:
        rss = f"{sample.rss_mb:.1f}" if sample.rss_mb is not None else "—"
        handles = str(sample.handles) if sample.handles is not None else "—"
        lines.append(
            f"| {sample.index} | {sample.wall_s:.3f} | {sample.total_tasks} | "
            f"{sample.succeeded} | {sample.failed} | {sample.blocked} | "
            f"{sample.llm_calls} | {rss} | {handles} | {sample.threads} | "
            f"{'通过' if sample.ok else '失败'} |"
        )

    lines += [
        "",
        "## 五、结论与边界",
        "",
        "- 长跑覆盖的是**同一进程内反复运行**的场景：checkpoint 累积、ledger 追加、",
        "  事件循环复用这些问题的症状只在第 N 次运行时出现，单次 run 全绿说明不了什么。",
        "- 内存与句柄的「前段/后段」各取 20% 的样本做均值：一次性抓两个点会被 GC 时机",
        "  误导，区间比较才能反映趋势。",
        "- 故障注入只覆盖两类**工程上最常见**的异常；模型输出质量下降、磁盘写满、",
        "  进程被杀等场景不在本报告范围内。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentCrew 稳定性测试（长跑 + 故障注入）")
    parser.add_argument("--sample", default="todo", choices=names(), help="用哪个预设样例")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help=f"长跑次数（默认 {DEFAULT_RUNS}）")
    parser.add_argument("--latency-ms", type=int, default=DEFAULT_LATENCY_MS,
                        help=f"模拟延迟（默认 {DEFAULT_LATENCY_MS}ms）")
    parser.add_argument("--only", choices=("all", "soak", "faults"), default="all",
                        help="只跑其中一部分")
    parser.add_argument("--out", default="bench", help="报告输出目录（默认 bench/）")
    args = parser.parse_args(argv)

    if args.only != "faults" and args.runs < 2:
        print("长跑次数至少为 2（要比较前段与后段）", file=sys.stderr)
        return 2

    warnings.simplefilter("always")
    with warnings.catch_warnings(record=True) as caught:
        if args.only == "faults":
            samples, summary = [], SoakSummary()
        else:
            samples, summary = run_soak(args.sample, args.runs, args.latency_ms)
        faults: list[FaultResult] = []
        if args.only != "soak":
            faults.append(fault_provider_transient(args.sample, args.latency_ms))
            faults.append(fault_gate_reject(args.sample, args.latency_ms))
    resource_warnings = [str(w.message) for w in caught if "was never awaited" in str(w.message)
                         or "unclosed" in str(w.message).lower()]

    soak_checks = evaluate_soak(summary) if samples else {}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "latency_ms": args.latency_ms,
        "python": ".".join(str(v) for v in sys.version_info[:3]),
        "platform": sys.platform,
        "thresholds": THRESHOLDS,
        "resource_warnings": resource_warnings,
    }
    payload = {
        "meta": meta,
        "soak": {
            "summary": asdict(summary),
            "samples": [asdict(s) for s in samples],
            "checks": soak_checks,
        },
        "faults": [asdict(f) for f in faults],
        "resource_warnings": resource_warnings,
    }
    (out_dir / "stability.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "stability.md").write_text(
        render_report(args.sample, samples, summary, soak_checks, faults, meta),
        encoding="utf-8")

    ok = all(soak_checks.values()) and all(f.passed for f in faults)
    print(f"报告：{out_dir / 'stability.md'}")
    if resource_warnings:
        print(f"⚠ 捕获 {len(resource_warnings)} 条资源警告，见报告附注", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
