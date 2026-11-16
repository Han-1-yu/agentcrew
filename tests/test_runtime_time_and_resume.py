"""R64：真实运行里暴露的四个缺陷（时间显示 / 审批单崩溃 / 续跑提示 / 门禁轮次回放）。

四条都不是「跑不通」，而是**跑起来之后说错话或给错下一步**——这类问题单测覆盖
不到，因为没人会去断言「CLI 打出来的时间戳是哪一时刻」。证据来自 2026-11-16 晚
的真机实跑：

1. ``main.py approvals <run>`` 在审批单带 ``expires_at`` 时**整条命令崩**
   ``TypeError: can't subtract offset-naive and offset-aware datetimes``
   （R30 的超时是 aware UTC，CLI 却拿 ``datetime.now()` 去减）。
2. ``status`` / ``delete`` 把 aware UTC 当本地时间念：run 目录名是
   ``20261116-192125``，同一屏的「创建」却写 ``11:21:25``——东八区差 8 小时。
3. ``cancel`` 往 ``run.json.updated_at`` 写 naive 本地时间（D-14 后该字段是
   aware UTC），于是同一文件里「创建 UTC、更新本地」，且任何
   ``updated_at - created_at`` 的读者直接抛异常。
4. ``rewind`` 结尾打印的「下一步：python main.py resume <id>」对 ``--sample``
   起出来的 run **必然失败**（配置快照 mock=true，resume 拒绝）；照着它敲还会
   掉进更深的坑——只补 ``--mock`` 能让比对通过，但夹具仍是默认目录，回放出来
   的产物属于另一个项目。
5. 样例 ``revision`` 宣称「首轮复核不通过（演示返工）」，实跑却是 5/5 通过、
   0 返工：门禁评审的 mock 回放 ``attempt`` 恒为 0，夹具里那条 ``"T-003@1"``
   永远命不中，也就没有任何离线路径能演示「驳回 → 返工 → 通过」。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import main as cli
from agentcrew.models.config import parse_alias
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.runtime.approvals import ApprovalStore
from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.utils.clock import format_local, utc_now

ROOT = Path(__file__).resolve().parents[1]


def _fake_run_dir(output_dir: Path, run_id: str, *, goal: str = "g") -> Path:
    store = CheckpointStore.create(
        output_dir, project_goal=goal, config_snapshot={}, run_id=run_id)
    return store.run_dir


# ── 1. 审批单带超时不再崩 ────────────────────────────────────────────────


def test_approvals_with_expiry_prints_remaining(tmp_path, monkeypatch, capsys):
    """``expires_at`` 是 aware UTC：CLI 必须按 aware 相减，而不是裸 ``now()``。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    run_dir = _fake_run_dir(tmp_path / "o", "20260101-000000-ap01")
    ApprovalStore(run_dir).create("plan", timeout_seconds=600)

    assert cli.main(["approvals", "20260101-000000-ap01"]) == 0
    out = capsys.readouterr().out
    assert "AP-" in out and "剩余" in out


def test_approvals_with_legacy_naive_expiry_tolerated(tmp_path, monkeypatch, capsys):
    """D-14 之前的旧审批单是 naive 本地时间：按本机时区补齐，不许抛。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    run_dir = _fake_run_dir(tmp_path / "o", "20260101-000000-ap02")
    store = ApprovalStore(run_dir)
    record = store.create("plan").model_dump(mode="json")
    legacy = utc_now().astimezone().replace(tzinfo=None) + timedelta(seconds=600)
    record["expires_at"] = legacy.isoformat()
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"op": "create", **record}, ensure_ascii=False) + "\n")

    assert cli.main(["approvals", "20260101-000000-ap02"]) == 0
    assert "剩余" in capsys.readouterr().out


# ── 2. status / delete 的时间按本机时区显示 ──────────────────────────────


def test_status_shows_local_time_for_utc_stamp(tmp_path, monkeypatch, capsys):
    """同一屏上目录名（本地）与「创建」（本应是本地）不能再差 8 小时。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    run_dir = _fake_run_dir(tmp_path / "o", "20260101-000000-tt01")
    store = CheckpointStore(run_dir)
    record = store.load_record()
    expected = format_local(record.created_at)
    assert cli.main(["status", "20260101-000000-tt01"]) == 0
    out = capsys.readouterr().out
    assert expected in out
    # 反证：裸打印 UTC 会得到另一个串（除非本机就是 UTC，此时两者相同）
    naive = record.created_at.strftime("%Y-%m-%d %H:%M:%S")
    if naive != expected:
        assert naive not in out


# ── 3. cancel 写 aware UTC ───────────────────────────────────────────────


def test_cancel_writes_aware_utc(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    run_dir = _fake_run_dir(tmp_path / "o", "20260101-000000-cc01")
    assert cli.main(["cancel", "20260101-000000-cc01"]) == 0
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "cancelled"
    updated = record["updated_at"]
    assert updated.endswith("Z") or "+00:00" in updated, (
        f"updated_at 必须是 aware UTC，实际 {updated!r}")
    created = record["created_at"]
    # 两个字段同一口径，才能直接相减
    assert created.endswith("Z") or "+00:00" in created


# ── 4. 续跑提示给的是能跑通的命令 ────────────────────────────────────────


def _hint(goal: str, snapshot_mock: bool) -> str:
    from agentcrew.config import Settings
    from agentcrew.runtime.checkpoint import RunRecord

    record = RunRecord(
        run_id="20260101-000000-hh01",
        project_goal=goal,
        status=RunStatus.INTERRUPTED,
        created_at=utc_now(),
        updated_at=utc_now(),
        config_snapshot={"mock": snapshot_mock},
    )
    return cli._resume_hint(record, Settings.load())


def test_resume_hint_restores_sample_fixtures():
    """样例 run：必须提示 ``--sample <名>``，只给 ``--mock`` 会回放出别的项目。"""
    from examples.samples import SAMPLES

    spec = SAMPLES["todo"]
    hint = _hint(spec.goal, True)
    assert hint.startswith("python main.py resume 20260101-000000-hh01")
    assert f"--sample {spec.name}" in hint
    # ``--sample`` 自带 mock，重复给反而不合法
    assert "--mock" not in hint


def test_resume_hint_notes_flag_ambiguous_sample_goal():
    """``linear`` / ``revision`` 共享同一句目标：说法必须承认分不清，而不是装懂。"""
    from agentcrew.config import Settings
    from agentcrew.runtime.checkpoint import RunRecord
    from examples.samples import SAMPLES

    record = RunRecord(
        run_id="20260101-000000-hh02",
        project_goal=SAMPLES["revision"].goal,
        status=RunStatus.INTERRUPTED,
        created_at=utc_now(),
        updated_at=utc_now(),
        config_snapshot={"mock": True},
    )
    notes = cli._resume_hint_notes(record)
    assert len(notes) == 1
    assert "linear" in notes[0] and "revision" in notes[0]
    # 命令本身仍然可跑（取第一个候选）
    assert "--sample linear" in cli._resume_hint(record, Settings.load())


def test_resume_hint_falls_back_to_mock_flag():
    hint = _hint("一个与任何样例都不同的目标", True)
    assert "--mock" in hint and "--sample" not in hint


def test_resume_hint_is_bare_for_real_runs():
    """真实 provider 的 run 不需要任何开关：提示不该无中生有。"""
    assert _hint("一个与任何样例都不同的目标", False) == (
        "python main.py resume 20260101-000000-hh01")


def test_resume_refusal_points_at_runnable_command(tmp_path, monkeypatch, capsys):
    """拒绝文案必须给出补开关后的完整命令，而不只是「加 --force」。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.chdir(ROOT)
    assert cli.main(["run", "--sample", "linear", "--no-live"]) == 0
    rid = next(
        d for d in (tmp_path / "o").iterdir()
        if d.is_dir() and (d / "run.json").exists()).name
    capsys.readouterr()

    # 不带任何开关续跑：配置比对必然不过（快照 mock=true，当前 mock=false）
    monkeypatch.setattr(cli, "_stdin_isatty", lambda: False)
    assert cli.main(["resume", rid]) == cli.EXIT_CODES["config"]
    err = capsys.readouterr().err
    assert "--sample linear" in err, err
    assert f"python main.py resume {rid}" in err


# ── 5. 门禁评审的轮次回放（样例「首轮驳回」真能发生）────────────────────


def _mock_with_reviews(tmp_path: Path, reviews: dict) -> MockProvider:
    fixtures = tmp_path / "fx"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "reviews.json").write_text(
        json.dumps(reviews, ensure_ascii=False), encoding="utf-8")
    return MockProvider(parse_alias("mock:mock-model"), fixtures_dir=fixtures)


def _replay_text(provider: MockProvider) -> str:
    return provider._review_text(provider.current_replay())


def test_gate_review_replay_uses_round_key(tmp_path):
    reviews = {
        "T-1@r1": {"score": 0.3, "judgements": [], "suggestions": []},
        "T-1@r2": {"score": 0.9, "judgements": [], "suggestions": []},
        "T-1": {"score": 0.66, "judgements": [], "suggestions": []},
    }
    provider = _mock_with_reviews(tmp_path, reviews)

    set_purpose(provider, "review", task_id="T-1", attempt=0, round_no=1)
    assert "0.3" in _replay_text(provider)
    set_purpose(provider, "review", task_id="T-1", attempt=0, round_no=2)
    assert "0.9" in _replay_text(provider)


def test_gate_review_replay_without_round_keeps_old_behaviour(tmp_path):
    """没有 ``@r<n>`` 键时行为与引入前逐字一致（老夹具不受影响）。"""
    provider = _mock_with_reviews(tmp_path, {
        "T-1": {"score": 0.66, "judgements": [], "suggestions": []}})
    set_purpose(provider, "review", task_id="T-1", attempt=0)
    assert "0.66" in _replay_text(provider)


def test_revision_sample_really_reworks(tmp_path, monkeypatch):
    """样例自称「首轮复核不通过」：断言它真的驳回一次并返工成功。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.chdir(ROOT)
    assert cli.main(["run", "--sample", "revision", "--no-live"]) == 0
    run_dir = next(
        d for d in (tmp_path / "o").iterdir()
        if d.is_dir() and (d / "run.json").exists())
    tasks = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    by_id = {task["id"]: task for task in tasks}
    assert by_id["T-003"]["rework_count"] == 1, "样例承诺的首轮驳回没有发生"
    assert by_id["T-003"]["status"] == "succeeded"
    events = [
        json.loads(line) for line in
        (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    rejects = [
        e for e in events
        if e.get("event") == "gate_result"
        and (e.get("data") or {}).get("verdict") == "reject"
    ]
    assert len(rejects) == 1, f"应当恰好驳回一次，实际 {len(rejects)}"
