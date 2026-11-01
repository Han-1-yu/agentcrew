"""基准脚本的自测：图谱上限与并发还原。

这两个函数是 `scripts/bench.py` 与 `scripts/soak.py` 的结论来源，一旦算错，
报告里的"加速比""达效率"就全是错的 —— 但它们不产出业务行为，所以容易被忽略。
这里用构造数据把它们钉住，不跑真实 run（几毫秒完成）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import bench  # noqa: E402


# ── 图谱理论上限 ──────────────────────────────────────────────────────────


def test_linear_graph_ceiling_is_one() -> None:
    """严格串行图：关键路径就是全部任务，加速比上限恰好 1。"""
    deps = {"T-001": (), "T-002": ("T-001",), "T-003": ("T-002",)}
    spans = {"T-001": 100, "T-002": 100, "T-003": 100}
    ceiling, width = bench.graph_ceiling(deps, spans)

    assert ceiling == pytest.approx(1.0)
    assert width == 1


def test_fan_out_ceiling_matches_critical_path() -> None:
    """一个根 + 四个并行子任务：上限 = 5/2 = 2.5，宽度 = 4。"""
    deps = {
        "T-001": (),
        "T-002": ("T-001",),
        "T-003": ("T-001",),
        "T-004": ("T-001",),
        "T-005": ("T-001",),
    }
    spans = {task_id: 100 for task_id in deps}
    ceiling, width = bench.graph_ceiling(deps, spans)

    assert ceiling == pytest.approx(2.5)
    assert width == 4


def test_final_task_joins_all_leaves() -> None:
    """收尾任务不在 plan 里，但必须挂在所有叶子之后，否则上限会被高估。"""
    deps = {"T-001": (), "T-002": ("T-001",), "T-003": ("T-001",)}
    spans = {"T-001": 100, "T-002": 100, "T-003": 100, "T-FINAL": 100}
    with_final, _ = bench.graph_ceiling(deps, spans)
    without_final, _ = bench.graph_ceiling(deps, {k: v for k, v in spans.items()
                                                  if k != "T-FINAL"})

    assert with_final < without_final, "把收尾任务算进来后上限必须下降"
    assert with_final == pytest.approx(4 / 3)


def test_uneven_durations_use_weights_not_counts() -> None:
    """关键路径要看耗时权重：一个慢任务比三个快任务更能决定上限。"""
    deps = {"T-001": (), "T-002": ("T-001",), "T-003": ("T-001",)}
    spans = {"T-001": 100, "T-002": 900, "T-003": 100}
    ceiling, _ = bench.graph_ceiling(deps, spans)

    # 总和 1100，关键路径 = T-001 + T-002 = 1000
    assert ceiling == pytest.approx(1100 / 1000)


def test_ceiling_of_empty_graph_is_zero() -> None:
    assert bench.graph_ceiling({"T-001": ()}, {}) == (0.0, 0)


# ── 并发还原 ──────────────────────────────────────────────────────────────


def _write_ledger(path: Path, events: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8")
    return path


def test_concurrency_profile_detects_overlap(tmp_path: Path) -> None:
    """三个任务里两个重叠：峰值应为 2，平均并发落在 1 与 2 之间。"""
    events = [
        {"ts": "2026-01-01 00:00:00.000000", "event": "run_started", "data": {}},
        {"ts": "2026-01-01 00:00:01.000000", "event": "task_started",
         "data": {"task_id": "T-001"}},
        {"ts": "2026-01-01 00:00:02.000000", "event": "task_started",
         "data": {"task_id": "T-002"}},
        {"ts": "2026-01-01 00:00:03.000000", "event": "task_finished",
         "data": {"task_id": "T-001"}},
        {"ts": "2026-01-01 00:00:04.000000", "event": "task_finished",
         "data": {"task_id": "T-002"}},
        {"ts": "2026-01-01 00:00:05.000000", "event": "task_started",
         "data": {"task_id": "T-003"}},
        {"ts": "2026-01-01 00:00:06.000000", "event": "task_finished",
         "data": {"task_id": "T-003"}},
    ]
    plan_s, task_s, peak, mean, spans = bench.concurrency_profile(
        _write_ledger(tmp_path / "ledger.jsonl", events))

    assert plan_s == pytest.approx(1.0)
    assert task_s == pytest.approx(5.0)
    assert peak == 2
    # 重叠段 2 秒、间隙 1 秒，恰好抵平：平均并发 = 1.0（峰值 2 不代表平均 2 ——
    # 任务之间的闸门与落盘是真空闲，这正是报告里强调"平均恒小于峰值"的原因）
    assert mean == pytest.approx(1.0)
    assert mean <= peak
    assert spans == {"T-001": 2000, "T-002": 2000, "T-003": 1000}


def test_concurrency_profile_is_serial_when_no_overlap(tmp_path: Path) -> None:
    events = [
        {"ts": "2026-01-01 00:00:00.000000", "event": "run_started", "data": {}},
        {"ts": "2026-01-01 00:00:00.100000", "event": "task_started",
         "data": {"task_id": "T-001"}},
        {"ts": "2026-01-01 00:00:01.100000", "event": "task_finished",
         "data": {"task_id": "T-001"}},
        {"ts": "2026-01-01 00:00:01.200000", "event": "task_started",
         "data": {"task_id": "T-002"}},
        {"ts": "2026-01-01 00:00:02.200000", "event": "task_finished",
         "data": {"task_id": "T-002"}},
    ]
    _, _, peak, mean, _ = bench.concurrency_profile(
        _write_ledger(tmp_path / "ledger.jsonl", events))

    assert peak == 1
    assert mean < 1.0  # 任务之间有间隙，平均并发必然小于峰值


def test_concurrency_profile_tolerates_broken_ledger(tmp_path: Path) -> None:
    """ledger 缺失或事件不全时必须返回全零，而不是抛异常打断整轮基准。"""
    missing = bench.concurrency_profile(tmp_path / "nope.jsonl")
    assert missing == (0.0, 0.0, 0, 0.0, {})

    partial = _write_ledger(tmp_path / "partial.jsonl", [
        {"ts": "2026-01-01 00:00:00.000000", "event": "run_started", "data": {}},
        {"ts": "2026-01-01 00:00:01.000000", "event": "task_started",
         "data": {"task_id": "T-001"}},
    ])
    assert bench.concurrency_profile(partial) == (0.0, 0.0, 0, 0.0, {})


# ── 判定口径 ──────────────────────────────────────────────────────────────


def test_serial_peak_must_be_exactly_one() -> None:
    """并发档位给 1 时峰值必须是 1：出现 2 就说明调度器忽略了配置。"""
    summaries = [
        bench.LevelSummary(
            level=1, runs=1, success_rate=1.0, wall_mean=10, wall_p50=10, wall_p95=10,
            throughput=1, wall_min=10, task_min=8, plan_phase=1, task_phase=8,
            peak_concurrency=2, mean_concurrency=1.2, llm_calls=10, cost_usd=0,
            speedup=1.0, task_speedup=1.0, efficiency=0.5,
        ),
    ]
    checks = bench.evaluate(summaries)

    assert checks["并发 1 峰值并发 = 1（没有偷偷并行）"] is False


def test_efficiency_is_capped_at_one() -> None:
    """实测超过理论上限只可能是基准被噪声拖慢，不能报出 300% 的达效率。"""
    samples = [
        bench.RunSample(
            level=1, repeat=0, wall_s=5, total_tasks=2, succeeded=2, failed=0,
            blocked=0, llm_calls=2, prompt_tokens=0, completion_tokens=0, cost_usd=0,
            plan_phase_s=1, task_phase_s=4, peak_concurrency=1, mean_concurrency=1,
            task_spans_ms={"T-001": 100, "T-002": 100},
        ),
    ]
    summaries = [
        bench.LevelSummary(
            level=1, runs=1, success_rate=1.0, wall_mean=5, wall_p50=5, wall_p95=5,
            throughput=1, wall_min=5, task_min=4, plan_phase=1, task_phase=4,
            peak_concurrency=1, mean_concurrency=1, llm_calls=2, cost_usd=0,
            speedup=1.0, task_speedup=10.0,  # 人为放大的加速比
        ),
    ]
    bench._fill_ceiling(summaries, samples, "linear")

    assert summaries[0].efficiency == 1.0
