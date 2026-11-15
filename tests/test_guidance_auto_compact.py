"""C10 切片三：run 终态封版后自动压缩指引历史。

钉住四条：没开过指引的 run 零副作用 · 小历史不动 · 大历史压成一行且保留活跃项 ·
压缩失败绝不影响封版。这里直接钉 ``Crew._maybe_compact_guidance`` 这个唯一接线点，
不跑完整 run（那会把「压缩」和调度、门禁、模型调用混在一起）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.runtime.guidance import (
    AUTO_COMPACT_LINE_THRESHOLD,
    GUIDANCE_FILE,
    GuidanceStore,
)
from agentcrew.runtime.ledger import Ledger


def mk_run(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="R-auto-compact")
    return SimpleNamespace(
        checkpoint=SimpleNamespace(run_dir=tmp_path), ledger=ledger), ledger


def line_count(tmp_path) -> int:
    return sum(1 for line in (tmp_path / GUIDANCE_FILE).read_text(
        encoding="utf-8").splitlines() if line.strip())


def test_incomplete_run_shape_never_breaks_finalize(tmp_path):
    """全量回归抓到过：封版夹具的 checkpoint 没有 run_dir，压缩不能抛回去。

    库层与测试里的 run 对象形状不总是完整；自动压缩是辅助设施，拿不到
    run_dir 就当「这场 run 没开过指引」，绝不能让 _finalize 二次封版。
    """
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="R-shape")
    for run in (
        SimpleNamespace(checkpoint=SimpleNamespace(), ledger=ledger),
        SimpleNamespace(ledger=ledger),
    ):
        Crew(Settings())._maybe_compact_guidance(run)
    assert not (tmp_path / GUIDANCE_FILE).exists()
    assert not list(ledger.events("guidance_compacted"))


def test_no_guidance_file_is_zero_side_effect(tmp_path):
    run, ledger = mk_run(tmp_path)
    Crew(Settings())._maybe_compact_guidance(run)
    assert not (tmp_path / GUIDANCE_FILE).exists()
    assert not list(ledger.events("guidance_compacted"))


def test_small_history_is_left_alone(tmp_path):
    run, ledger = mk_run(tmp_path)
    store = GuidanceStore(tmp_path)
    store.append("第一条")
    store.append("第二条")
    before = (tmp_path / GUIDANCE_FILE).read_text(encoding="utf-8")
    Crew(Settings())._maybe_compact_guidance(run)
    assert (tmp_path / GUIDANCE_FILE).read_text(encoding="utf-8") == before
    assert not list(ledger.events("guidance_compacted"))


def test_large_history_is_compacted_and_active_items_survive(tmp_path):
    run, ledger = mk_run(tmp_path)
    store = GuidanceStore(tmp_path)
    items = [store.append(f"第 {index} 条") for index in range(30)]
    for item in items[:28]:
        store.consume([item.id])
    assert line_count(tmp_path) > AUTO_COMPACT_LINE_THRESHOLD

    Crew(Settings())._maybe_compact_guidance(run)

    assert line_count(tmp_path) == 1
    assert [item.text for item in GuidanceStore(tmp_path).pending(
        limit=10, max_chars=400)] == ["第 28 条", "第 29 条"]
    events = list(ledger.events("guidance_compacted"))
    assert len(events) == 1
    assert events[0].data["auto"] is True
    assert events[0].data["after_lines"] == 1
    assert events[0].data["kept"] > 0


def test_second_call_is_a_noop(tmp_path):
    """封版幂等 + 阈值双重保证：压缩过一次后不能反复 emit / 反复重写。"""
    run, ledger = mk_run(tmp_path)
    store = GuidanceStore(tmp_path)
    items = [store.append(f"第 {index} 条") for index in range(30)]
    for item in items[:28]:
        store.consume([item.id])
    crew = Crew(Settings())
    crew._maybe_compact_guidance(run)
    crew._maybe_compact_guidance(run)
    assert len(list(ledger.events("guidance_compacted"))) == 1
    assert line_count(tmp_path) == 1


def test_compact_failure_never_escapes(tmp_path, monkeypatch):
    run, ledger = mk_run(tmp_path)
    store = GuidanceStore(tmp_path)
    items = [store.append(f"第 {index} 条") for index in range(30)]
    for item in items[:28]:
        store.consume([item.id])
    before = (tmp_path / GUIDANCE_FILE).read_text(encoding="utf-8")

    def boom(self):  # noqa: ANN001
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(GuidanceStore, "compact", boom)
    Crew(Settings())._maybe_compact_guidance(run)   # 不抛
    assert (tmp_path / GUIDANCE_FILE).read_text(encoding="utf-8") == before
    assert not list(ledger.events("guidance_compacted"))


def test_snapshot_keeps_slice_two_fields_after_auto_compact(tmp_path):
    run, _ = mk_run(tmp_path)
    store = GuidanceStore(tmp_path)
    store.append("定向", target="T-002")
    for index in range(30):
        item = store.append(f"填充 {index}")
        store.consume([item.id])
    Crew(Settings())._maybe_compact_guidance(run)
    raw = json.loads((tmp_path / GUIDANCE_FILE).read_text(
        encoding="utf-8").splitlines()[0])
    assert raw["op"] == "snapshot"
    assert any(entry.get("target") == "T-002" for entry in raw["items"])
