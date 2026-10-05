"""Checkpoint 持久化与恢复的行为测试（spec §13，Task 15）。

覆盖：目录布局与 run_id 格式、run.json / tasks.json 往返、schema_version 校验、
「写入中途进程被杀」的原子性（Review Focus #5）、resume 状态修复规则，以及
clean 的删除资格与删除边界。
"""

import json
import os
import re
import subprocess
from datetime import datetime

import pytest

import agentcrew.runtime.checkpoint as checkpoint
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunRecord,
    RunStatus,
    SchemaMismatchError,
    clean,
    repair_statuses,
)
from agentcrew.task.task import Task
from agentcrew.task.task import TaskStatus as S
from agentcrew.task.task_board import TaskBoard


def _make_dir_link(link, target):
    """建一个指向目录的链接：Windows 用 junction（mklink /J，无需提权），其余用 symlink。"""
    if os.name == "nt":
        return subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        ).returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return True


def mk(tid, deps=(), status=S.PENDING):
    return Task(id=tid, title=tid, description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=[], dependencies=list(deps),
                status=status)


def mk_record(goal="g", *, status=RunStatus.RUNNING, config=None):
    now = datetime.now()
    return RunRecord(run_id="20261005-120000-abcd", project_goal=goal,
                     status=status, created_at=now, updated_at=now,
                     config_snapshot={} if config is None else config)


@pytest.fixture
def cs(tmp_path):
    return CheckpointStore.create(tmp_path / "output",
                                  project_goal="build a todo app",
                                  config_snapshot={"max_concurrency": 4},
                                  run_id="20261005-120000-abcd")


def test_create_makes_directories(cs):
    assert cs.run_dir.is_dir() and cs.artifacts_dir.is_dir()


def test_create_makes_full_layout_and_initial_record(cs):
    """裁定 4：四个子目录全部就位；create 同时落第一条 run.json。

    ``list_runs`` / ``clean`` 只认「目录且含 run.json」的条目，create 不写
    run.json 的话，本文件里 clean 的用例根本看不到刚建的 run。
    """
    for name in ("artifacts", "context", "review", "diagnostics"):
        assert (cs.run_dir / name).is_dir(), name
    assert (cs.run_dir / "run.json").is_file()
    rec = cs.load_record()
    assert rec.run_id == "20261005-120000-abcd"
    assert rec.status is RunStatus.RUNNING
    assert rec.project_goal == "build a todo app"
    assert rec.config_snapshot == {"max_concurrency": 4}


def test_constructor_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        CheckpointStore(tmp_path / "does-not-exist")


def test_run_id_format_when_generated(tmp_path):
    c = CheckpointStore.create(tmp_path / "o", project_goal="g", config_snapshot={})
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", c.run_dir.name)


def test_create_rejects_unsafe_run_id(tmp_path):
    """命令行/显式传入的 run_id 不得逃出 output_dir（Task 32 从 argv 取值）。"""
    o = tmp_path / "o"
    for bad in ("../x", "..", ".", "a/b", "a\\b", "C:x", "a b", ""):
        with pytest.raises(ValueError):
            CheckpointStore.create(o, project_goal="g", config_snapshot={},
                                   run_id=bad)
    assert not (tmp_path / "x").exists()         # 穿越尝试未在 output_dir 外留下东西
    assert not (tmp_path / "run.json").exists()
    assert not o.exists()                        # 全部被拒，output_dir 都未创建


def test_create_refuses_existing_run_id_without_damage(tmp_path):
    """同名 run_id 已存在时拒绝覆盖：可能对应一个活着的 run，记录不得被重置。"""
    o = tmp_path / "o"
    cs = CheckpointStore.create(o, project_goal="original", config_snapshot={"a": 1},
                                run_id="20260101-000000-aaaa")
    before = (cs.run_dir / "run.json").read_bytes()
    with pytest.raises(FileExistsError):
        CheckpointStore.create(o, project_goal="hijack", config_snapshot={},
                               run_id="20260101-000000-aaaa")
    assert (cs.run_dir / "run.json").read_bytes() == before     # 原内容逐字节未动
    rec = cs.load_record()
    assert rec.project_goal == "original" and rec.config_snapshot == {"a": 1}


def test_save_and_load_record_roundtrip(cs):
    rec = RunRecord(run_id="20261005-120000-abcd", project_goal="g",
                    status=RunStatus.RUNNING,
                    created_at=__import__("datetime").datetime.now(),
                    updated_at=__import__("datetime").datetime.now(),
                    config_snapshot={"a": 1})
    cs.save_record(rec)
    assert cs.load_record().project_goal == "g"


def test_save_and_load_board_roundtrip(cs):
    b = TaskBoard([mk("A"), mk("B", ["A"])])
    b.transition("A", S.READY)
    b.transition("A", S.RUNNING)
    cs.save_tasks(b)
    assert cs.load_board().get("A").status is S.RUNNING


def test_schema_mismatch_raises(cs, tmp_path):
    cs.save_record(RunRecord(run_id="20261005-120000-abcd", project_goal="g",
                             status=RunStatus.RUNNING,
                             created_at=__import__("datetime").datetime.now(),
                             updated_at=__import__("datetime").datetime.now(),
                             config_snapshot={}))
    p = cs.run_dir / "run.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["schema_version"] = 99
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(SchemaMismatchError):
        cs.load_record()


def test_schema_version_missing_is_mismatch(cs):
    """缺失的 schema_version 同样是「不认识的格式」，必须报错而非猜默认值。"""
    p = cs.run_dir / "run.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    del d["schema_version"]
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(SchemaMismatchError):
        cs.load_record()


# ── Review Focus #5：中断恰好发生在写入过程中 ──
def test_interrupted_write_never_leaves_partial_json(cs, monkeypatch):
    """模拟写 run.json 时进程被杀死：目标文件必须仍是旧的完整内容。"""
    rec = RunRecord(run_id="20261005-120000-abcd", project_goal="old",
                    status=RunStatus.RUNNING,
                    created_at=__import__("datetime").datetime.now(),
                    updated_at=__import__("datetime").datetime.now(),
                    config_snapshot={})
    cs.save_record(rec)
    before = (cs.run_dir / "run.json").read_text(encoding="utf-8")

    import agentcrew.utils.files as files
    real_replace = files.os.replace
    def boom(src, dst):
        raise KeyboardInterrupt("模拟中断")
    monkeypatch.setattr(files.os, "replace", boom)
    rec2 = rec.model_copy(update={"project_goal": "new"})
    with pytest.raises(KeyboardInterrupt):
        cs.save_record(rec2)
    monkeypatch.setattr(files.os, "replace", real_replace)

    after = (cs.run_dir / "run.json").read_text(encoding="utf-8")
    assert after == before                       # 旧内容完好
    json.loads(after)                            # 仍是合法 JSON
    assert cs.load_record().project_goal == "old"


def test_interrupted_tasks_write_keeps_previous_content(cs, monkeypatch):
    """tasks.json 与 run.json 同等对待：中断不得留下半截任务快照。"""
    b = TaskBoard([mk("A")])
    cs.save_tasks(b)
    before = (cs.run_dir / "tasks.json").read_text(encoding="utf-8")

    import agentcrew.utils.files as files
    real_replace = files.os.replace
    monkeypatch.setattr(files.os, "replace",
                        lambda src, dst: (_ for _ in ()).throw(KeyboardInterrupt()))
    b.transition("A", S.READY)
    with pytest.raises(KeyboardInterrupt):
        cs.save_tasks(b)
    monkeypatch.setattr(files.os, "replace", real_replace)

    after = (cs.run_dir / "tasks.json").read_text(encoding="utf-8")
    assert after == before
    assert cs.load_board().get("A").status is S.PENDING


def test_leftover_temp_files_are_cleaned(cs, monkeypatch):
    import agentcrew.utils.files as files
    monkeypatch.setattr(files.os, "replace",
                        lambda s, d: (_ for _ in ()).throw(KeyboardInterrupt()))
    rec = RunRecord(run_id="20261005-120000-abcd", project_goal="g",
                    status=RunStatus.RUNNING,
                    created_at=__import__("datetime").datetime.now(),
                    updated_at=__import__("datetime").datetime.now(),
                    config_snapshot={})
    with pytest.raises(KeyboardInterrupt):
        cs.save_record(rec)
    assert list(cs.run_dir.glob("**/.*tmp*")) == []


def test_repair_running_becomes_ready_and_keeps_counts(cs):
    b = TaskBoard([mk("A", status=S.READY)])
    b.bump_attempt("A")                                # 派发时扣额度（spec §6.2）
    b.transition("A", S.RUNNING)
    n = repair_statuses(b)
    assert n == 1 and b.get("A").status is S.READY
    assert b.get("A").attempts == 1                    # 不重置


def test_repair_rework_stays_rework(cs):
    """裁定 2：维持 REWORK 不动 —— 状态机没有 REWORK → READY 边。

    ``TaskBoard.take_ready()`` 同时派发 READY 与 REWORK，返工任务无需任何修复
    即可续跑；若这里调用 ``transition`` 会抛 ``IllegalTransitionError``。
    """
    b = TaskBoard([mk("A", status=S.READY)])
    b.transition("A", S.RUNNING)
    b.transition("A", S.REVIEW)
    b.transition("A", S.REWORK)
    n = repair_statuses(b)
    assert n == 0
    assert b.get("A").status is S.REWORK
    # 历史里不得出现任何额外的跃迁（尤其 REWORK → READY）
    assert [c.to_status for c in b.get("A").status_history] == [
        S.RUNNING, S.REVIEW, S.REWORK]


def test_repair_review_stays_review(cs):
    b = TaskBoard([mk("A", status=S.READY)])
    b.transition("A", S.RUNNING)
    b.transition("A", S.REVIEW)
    repair_statuses(b)
    assert b.get("A").status is S.REVIEW


def test_repair_leaves_terminal_alone(cs):
    b = TaskBoard([mk("A", status=S.SUCCEEDED), mk("B", status=S.FAILED)])
    assert repair_statuses(b) == 0


def test_repair_all_ten_statuses_only_running_moves(cs):
    """十种状态各一：只有 RUNNING 被修复，计数为 1，其余九种原样。"""
    board = TaskBoard([
        mk("P", status=S.PENDING),
        mk("R", status=S.READY),
        mk("X", status=S.RUNNING),
        mk("V", status=S.REVIEW),
        mk("W", status=S.REWORK),
        mk("S", status=S.SUCCEEDED),
        mk("F", status=S.FAILED),
        mk("B", status=S.BLOCKED),
        mk("C", status=S.CANCELLED),
        mk("K", status=S.SKIPPED),
    ])
    histories = {t.id: list(t.status_history) for t in board.all()}
    attempts = {t.id: t.attempts for t in board.all()}

    n = repair_statuses(board)

    assert n == 1
    assert board.get("X").status is S.READY
    assert board.get("X").status_history[-1].from_status is S.RUNNING
    assert board.get("X").status_history[-1].to_status is S.READY
    assert board.get("X").status_history[-1].reason == "resume-repair"
    for tid, expected in (("P", S.PENDING), ("R", S.READY), ("V", S.REVIEW),
                          ("W", S.REWORK), ("S", S.SUCCEEDED), ("F", S.FAILED),
                          ("B", S.BLOCKED), ("C", S.CANCELLED), ("K", S.SKIPPED)):
        assert board.get(tid).status is expected, tid
        assert board.get(tid).status_history == histories[tid], tid
        assert board.get(tid).attempts == attempts[tid], tid


def test_repair_does_not_reset_rework_count(cs):
    b = TaskBoard([mk("A", status=S.READY)])
    b.transition("A", S.RUNNING)
    b.transition("A", S.REVIEW)
    b.bump_rework("A")
    b.transition("A", S.REWORK)
    repair_statuses(b)
    assert b.get("A").rework_count == 1


def test_repair_empty_board_is_zero(cs):
    assert repair_statuses(TaskBoard([])) == 0


def test_artifacts_survive_resume(cs):
    cs.artifacts_dir.joinpath("keep.txt").write_text("x", encoding="utf-8")
    assert (cs.run_dir / "artifacts" / "keep.txt").read_text(encoding="utf-8") == "x"


def test_save_persists_most_recent_record_and_board(cs):
    cs.save_record(mk_record("g"))
    cs.save_tasks(TaskBoard([mk("A")]))
    (cs.run_dir / "run.json").unlink()
    (cs.run_dir / "tasks.json").unlink()
    cs.save()                                    # 便捷：同时补写两个文件
    assert cs.load_record().project_goal == "g"
    assert cs.load_board().get("A").status is S.PENDING


def test_save_accepts_explicit_record_and_board(cs):
    cs.save(mk_record("explicit"), TaskBoard([mk("A", status=S.READY)]))
    assert cs.load_record().project_goal == "explicit"
    assert cs.load_board().get("A").status is S.READY


def test_save_without_record_or_board_raises(cs):
    fresh = CheckpointStore(cs.run_dir)          # 未 save/load 过任何对象
    with pytest.raises(RuntimeError):
        fresh.save()


def test_list_runs_sorted_desc(tmp_path):
    for rid in ("20260101-000000-aaaa", "20260102-000000-bbbb"):
        CheckpointStore.create(tmp_path / "o", project_goal="g",
                               config_snapshot={}, run_id=rid)
    assert CheckpointStore.list_runs(tmp_path / "o") == [
        "20260102-000000-bbbb", "20260101-000000-aaaa"]


def test_load_latest_picks_newest_run(tmp_path):
    o = tmp_path / "o"
    for rid in ("20260102-000000-bbbb", "20260101-000000-aaaa",
                "20260103-000000-cccc"):
        CheckpointStore.create(o, project_goal=rid, config_snapshot={}, run_id=rid)
    latest = CheckpointStore.load_latest(o)
    assert latest.run_dir.name == "20260103-000000-cccc"
    assert latest.load_record().project_goal == "20260103-000000-cccc"


def test_load_latest_without_runs_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        CheckpointStore.load_latest(tmp_path / "nope")


def test_clean_keeps_n_most_recent(tmp_path):
    o = tmp_path / "o"
    for rid in ("20260101-000000-aaaa", "20260102-000000-bbbb",
                "20260103-000000-cccc"):
        CheckpointStore.create(o, project_goal="g", config_snapshot={}, run_id=rid)
    removed = clean(o, keep=1)
    assert len(removed) == 2
    assert CheckpointStore.list_runs(o) == ["20260103-000000-cccc"]


def test_clean_dry_run_removes_nothing(tmp_path):
    o = tmp_path / "o"
    for rid in ("20260101-000000-aaaa", "20260102-000000-bbbb"):
        CheckpointStore.create(o, project_goal="g", config_snapshot={}, run_id=rid)
    removed = clean(o, keep=0, dry_run=True)
    assert len(removed) == 2 and len(CheckpointStore.list_runs(o)) == 2


def test_clean_skips_dirs_without_run_json(tmp_path):
    o = tmp_path / "o"
    o.mkdir()
    (o / "random-dir").mkdir()
    (o / "not-a-run.txt").write_text("x", encoding="utf-8")
    assert clean(o, keep=0) == []
    assert (o / "random-dir").is_dir() and (o / "not-a-run.txt").is_file()


def test_clean_never_deletes_outside_output_dir(tmp_path):
    """删除循环的边界：output_dir 的兄弟目录（同样含 run.json）必须毫发无损。"""
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "run.json").write_text("{}", encoding="utf-8")
    o = tmp_path / "o"
    CheckpointStore.create(o, project_goal="g", config_snapshot={},
                           run_id="20260101-000000-aaaa")
    removed = clean(o, keep=0)
    assert [p.name for p in removed] == ["20260101-000000-aaaa"]
    assert decoy.is_dir() and (decoy / "run.json").is_file()


def test_clean_refuses_linked_dir(tmp_path):
    """指向 output_dir 之外的目录链接不得被 rmtree 跟进。

    Windows 用 junction 实跑（mklink /J 无需提权）：``os.path.islink`` 对 junction
    返回 False，而 ``shutil.rmtree`` 会跟进去删目标里的文件——最危险的一类。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run.json").write_text("{}", encoding="utf-8")
    o = tmp_path / "o"
    o.mkdir()
    link = o / "20260101-000000-aaaa"
    if not _make_dir_link(link, outside):
        pytest.skip("当前环境不允许创建目录链接")
    try:
        with pytest.raises(RuntimeError):
            clean(o, keep=0)
        assert (outside / "run.json").is_file()
    finally:
        try:
            os.rmdir(link)                  # 摘掉 junction/链接本身，不跟进目标
        except OSError:
            link.unlink(missing_ok=True)
    assert not link.exists()


def test_clean_refuses_link_like_without_calling_rmtree(tmp_path, monkeypatch):
    """守卫的平台无关版本：``is_link_like`` 为真时必须在任何 rmtree 之前拒绝。

    不依赖创建链接的权限——上面的 junction 用例在无权限环境会 skip，这条保证
    守卫在 CI 上真的被执行过。
    """
    o = tmp_path / "o"
    for rid in ("20260101-000000-aaaa", "20260102-000000-bbbb"):
        CheckpointStore.create(o, project_goal="g", config_snapshot={}, run_id=rid)
    monkeypatch.setattr(checkpoint, "is_link_like", lambda path: True)
    calls = []
    monkeypatch.setattr(checkpoint.shutil, "rmtree", lambda path: calls.append(path))
    with pytest.raises(RuntimeError):
        clean(o, keep=0)
    assert calls == []                              # rmtree 一次都没被调用
    assert len(CheckpointStore.list_runs(o)) == 2   # 一个 run 都没删


def test_clean_partial_refusal_deletes_nothing(tmp_path, monkeypatch):
    """先整组校验、再整组删除：排序最后的目标被拒时，前面的也不能先被删。"""
    o = tmp_path / "o"
    for rid in ("20260101-000000-aaaa", "20260102-000000-bbbb",
                "20260103-000000-cccc"):
        CheckpointStore.create(o, project_goal="g", config_snapshot={}, run_id=rid)
    oldest = "20260101-000000-aaaa"      # 降序删除顺序里排在最后
    monkeypatch.setattr(checkpoint, "is_link_like",
                        lambda path: path.name == oldest)
    calls = []
    monkeypatch.setattr(checkpoint.shutil, "rmtree", lambda path: calls.append(path))
    with pytest.raises(RuntimeError):
        clean(o, keep=0)
    assert calls == []                   # 旧实现会先删掉两个更新的，本断言钉住两阶段
    assert CheckpointStore.list_runs(o) == [
        "20260103-000000-cccc", "20260102-000000-bbbb", "20260101-000000-aaaa"]


def test_clean_validation_runs_in_dry_run_too(tmp_path, monkeypatch):
    """dry_run 同样跑校验：干跑必须能预告真跑会拒绝什么。"""
    o = tmp_path / "o"
    CheckpointStore.create(o, project_goal="g", config_snapshot={},
                           run_id="20260101-000000-aaaa")
    monkeypatch.setattr(checkpoint, "is_link_like", lambda path: True)
    with pytest.raises(RuntimeError):
        clean(o, keep=0, dry_run=True)
    assert len(CheckpointStore.list_runs(o)) == 1


def test_clean_rejects_negative_keep(tmp_path):
    o = tmp_path / "o"
    o.mkdir()
    with pytest.raises(ValueError):
        clean(o, keep=-1)
