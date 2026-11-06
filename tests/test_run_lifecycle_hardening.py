"""2026-11-04：run 生命周期完整性回归测试。

钉住本轮修复的确定性缺陷，每条都对应一次真实故障路径：

1. 非白名单异常不再让 ``run.json`` 永久停在 ``RUNNING``（兜底封版）；
2. 空板 / 缺面板的 resume 不再被判 ``COMPLETED`` 假成功（静默丢数据）；
3. ``_final_status`` 空板保护 + ``_finalize`` 幂等（终态不被二次改写）；
4. 删除守卫 fail-closed：读不出状态绝不删活 run；
5. 僵尸 RUNNING 有收敛出口，活 RUNNING 仍受保护（含 ``clean --protect-running``）；
6. API resume 端点的异常路径必须归还并发槽位，否则所有 run 永久 429。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.runtime.checkpoint import (
    STALE_RUNNING_AFTER,
    CheckpointStore,
    RunStatus,
    clean,
    delete_run,
    is_stale_running,
)
from agentcrew.task.task_board import TaskBoard


def _make_run(output_dir: Path, run_id: str,
              status: RunStatus | None = None) -> Path:
    """造一个最小 run 目录；``create`` 落盘的初始状态是 RUNNING。"""
    cs = CheckpointStore.create(
        output_dir, project_goal="g", config_snapshot={}, run_id=run_id)
    if status is not None:
        record = cs.load_record()
        record.status = status
        cs.save_record(record)
    return cs.run_dir


def _set_updated(run_dir: Path, when: datetime) -> None:
    """直接改盘上 ``updated_at``，模拟长时间无心跳。"""
    path = run_dir / "run.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = when.isoformat()
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """与 ``test_crew.py::env`` 同口径的离线 mock 配置。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    return Settings.load()


# ── 1. 兜底封版：任何异常都不得留下永久 RUNNING ──────────────────────────


@pytest.mark.asyncio
async def test_unexpected_exception_finalizes_run(settings, monkeypatch):
    """``_plan_phase`` 抛 ValueError（非白名单）→ 仍封版为 FAILED 且原异常不丢。"""
    async def boom(self, run):
        raise ValueError("计划卡点构造期失败")

    monkeypatch.setattr(Crew, "_plan_phase", boom)
    with pytest.raises(ValueError, match="构造期失败"):
        await Crew(settings).run("g")

    runs = CheckpointStore.list_runs(settings.output_dir)
    assert len(runs) == 1
    record = CheckpointStore(settings.output_dir / runs[0]).load_record()
    assert record.status is RunStatus.FAILED


# ── 2. 空板 resume：绝不假成功 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_empty_board_fails_loud(settings):
    """规划期被强杀的 run 没有任务面板：resume 必须报错并封版 FAILED。"""
    run_dir = _make_run(settings.output_dir, "R-empty")
    (run_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8")

    crew = Crew.resume_from(settings, "R-empty")
    with pytest.raises(ValueError, match="没有可续跑的任务面板"):
        await crew.resume()

    assert CheckpointStore(run_dir).load_record().status is RunStatus.FAILED


def test_final_status_empty_board_is_failed():
    """双保险：即便绕过入口校验，空板也不能判 COMPLETED。"""
    crew = object.__new__(Crew)
    assert crew._final_status(TaskBoard([])) is RunStatus.FAILED


# ── 3. _finalize 幂等 ────────────────────────────────────────────────────


def test_finalize_is_idempotent(monkeypatch):
    """二次封版零副作用：不改写终态、不重复 emit、不碰已关闭的 ledger。"""
    calls: list[str] = []
    monkeypatch.setattr(Crew, "_sync_stats", lambda self, run, **kw: None)
    monkeypatch.setattr(Crew, "_exit_code", lambda self, status: 0)
    now = datetime.now()
    record = SimpleNamespace(
        run_id="R", project_goal="g", status=None, updated_at=now,
        created_at=now, stats=SimpleNamespace(started_at=None),
    )
    run = SimpleNamespace(
        record=record,
        checkpoint=SimpleNamespace(
            save_record=lambda r: calls.append("record"),
            save_tasks=lambda b: calls.append("tasks")),
        store=SimpleNamespace(write_manifest=lambda: calls.append("manifest")),
        ledger=SimpleNamespace(
            emit=lambda *a, **kw: calls.append("emit"),
            close=lambda: calls.append("close")),
        models=SimpleNamespace(total_cost_usd=0.0, llm_calls=0),
        goal="g",
    )
    crew = object.__new__(Crew)
    crew._finalized = False
    crew._board = None
    crew._on_event = None

    first = crew._finalize(run, RunStatus.COMPLETED)
    assert {"record", "emit", "close"} <= set(calls)

    calls.clear()
    second = crew._finalize(run, RunStatus.INTERRUPTED)
    assert calls == [], "二次封版不得再写盘 / 再发 run_finished"
    assert record.status is RunStatus.COMPLETED, "终态不得被二次改写"
    assert first is second is record


# ── 4/5. 删除守卫 fail-closed 与僵尸 run 收敛 ─────────────────────────────


def test_delete_run_refuses_unreadable_run_json(tmp_path):
    """run.json 损坏 / 被独占读不出来时，必须按最危险的情况拒绝删除。"""
    output = tmp_path / "o"
    output.mkdir()
    run_dir = _make_run(output, "R-broken")
    (run_dir / "run.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="正在运行中"):
        delete_run(output, "R-broken")
    assert run_dir.exists()


def test_delete_run_allows_stale_running(tmp_path):
    """强杀遗留、超过 TTL 无心跳的 RUNNING 可以清理（历史死 run 的出口）。"""
    output = tmp_path / "o"
    output.mkdir()
    run_dir = _make_run(output, "R-stale", RunStatus.RUNNING)
    _set_updated(run_dir,
                 datetime.now() - STALE_RUNNING_AFTER - timedelta(hours=1))

    assert is_stale_running(run_dir)
    delete_run(output, "R-stale")
    assert not run_dir.exists()


def test_is_stale_running_boundaries(tmp_path):
    """新鲜 RUNNING / 已终结 run / 读不出的 run 都不算僵尸。"""
    output = tmp_path / "o"
    output.mkdir()
    live = _make_run(output, "R-live", RunStatus.RUNNING)
    assert not is_stale_running(live)

    done = _make_run(output, "R-done", RunStatus.COMPLETED)
    _set_updated(done, datetime.now() - timedelta(days=10))
    assert not is_stale_running(done)

    broken = _make_run(output, "R-bad")
    (broken / "run.json").write_text("{}", encoding="utf-8")
    assert not is_stale_running(broken)


def test_clean_protect_running_skips_live_but_clears_stale(tmp_path):
    """``protect_running=True``：活 run 跳过、僵尸 RUNNING 与已终结 run 照常清理。"""
    output = tmp_path / "o"
    output.mkdir()
    _make_run(output, "20260101-000000-aaaa", RunStatus.COMPLETED)
    live = _make_run(output, "20260102-000000-bbbb", RunStatus.RUNNING)
    stale = _make_run(output, "20260103-000000-cccc", RunStatus.RUNNING)
    _set_updated(stale, datetime.now() - timedelta(days=3))

    removed = clean(output, keep=0, protect_running=True)
    assert {p.name for p in removed} == {
        "20260101-000000-aaaa", "20260103-000000-cccc"}
    assert live.exists()


def test_clean_default_contract_unchanged(tmp_path):
    """库层默认值仍是 spec §13.5 的既有语义（不破坏调用方契约）。"""
    output = tmp_path / "o"
    output.mkdir()
    for rid in ("20260101-000000-aaaa", "20260102-000000-bbbb",
                "20260103-000000-cccc"):
        _make_run(output, rid)
    assert len(clean(output, keep=1)) == 2


# ── 6. API resume 并发槽位 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_endpoint_releases_slot_on_unexpected_error(
        settings, monkeypatch):
    """``resume_from`` 抛非 (OSError, ValueError) 异常时槽位必须归还。

    否则 ``_run_crew`` 根本没被创建，计数永久 +1；攒够上限后所有 run 请求
    都会返回 429，且没有任何自愈路径。
    """
    from agentcrew.api.server import ApiServer

    def boom(cls, *args, **kwargs):
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(Crew, "resume_from", classmethod(boom))
    server = ApiServer(port=0, output_dir=settings.output_dir,
                       settings=settings)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /runs/R-1/resume HTTP/1.1\r\n"
            b"Host: localhost\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        response = await reader.read(4096)
        writer.close()
        await writer.wait_closed()

        assert b"500" in response
        assert server._active_run_count == 0
    finally:
        await server.stop()
