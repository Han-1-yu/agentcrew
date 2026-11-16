"""C14：经验库 / 实体库在并发写下的完整性。

8 worker 实测（`bench/queue-2026-11-16.md`）暴露过一次：多个进程同时 append
`experiences.jsonl`，读方撞上半行 → `Experience` 校验失败 → run 直接崩。这里钉住
两层修复：写侧（文件锁 + 单次 write）与读侧（坏行跳过）。
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from agentcrew.knowledge import jsonl as jsonl_mod
from agentcrew.knowledge.experience import Experience, ExperienceStore
from agentcrew.knowledge.jsonl import append_line, read_lines

REPO = Path(__file__).resolve().parents[1]


def _experience(index: int) -> Experience:
    return Experience(
        id=f"E-{index}",
        task_description="写一个 slugify 函数",
        role="backend",
        solution_summary="用正则替换非字母数字",
        artifacts=["src/slugify.py"],
        key_decisions=["保持幂等"],
        success=True,
        run_id=f"R-{index}",
        created_at=datetime.now(timezone.utc),
    )


def test_concurrent_thread_appends_are_intact(tmp_path):
    store = ExperienceStore(tmp_path)

    def write(worker: int) -> None:
        for index in range(20):
            store.add(_experience(worker * 100 + index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(8)))
    items = store.all()
    assert len(items) == 160
    assert len({item.id for item in items}) == 160


def test_truncated_line_is_skipped_without_crashing(tmp_path):
    store = ExperienceStore(tmp_path)
    store.add(_experience(1))
    # 模拟「另一个进程正写到一半」：尾部半行没有配对的大括号。
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write('{"id": "E-half", "task_description": "半行')
    items = store.all()
    assert [item.id for item in items] == ["E-1"]


def test_multi_process_appends_are_intact():
    """4 个独立进程并发写同一个库：锁 + 单次写必须让 120 条全部可读。

    用仓库 ``output/`` 下的临时目录而不是 pytest 的 ``tmp_path``：Windows 上
    后者位于用户 Temp，子进程打开 ``*.lock`` 会稳定报 PermissionError（实测），
    与代码无关，会把这层保护测成假红。
    """
    import shutil
    import tempfile

    base = REPO / "output"
    base.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix="ac-jsonl-", dir=base))
    code = """
import sys
from datetime import datetime, timezone
from pathlib import Path
from agentcrew.knowledge import jsonl as jsonl_mod
from agentcrew.knowledge.experience import Experience, ExperienceStore
from agentcrew.knowledge.jsonl import append_line, read_lines

store = ExperienceStore(Path(sys.argv[1]))
tag = sys.argv[2]
for index in range(30):
    store.add(Experience(
        id=f"{tag}-{index}", task_description="并发写", role="dev",
        solution_summary="s", artifacts=[], key_decisions=[], success=True,
        run_id="r", created_at=datetime.now(timezone.utc),
    ))
"""
    try:
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(tmp_path), f"p{n}"], cwd=REPO)
            for n in range(4)
        ]
        assert all(proc.wait(timeout=120) == 0 for proc in procs)
        items = ExperienceStore(tmp_path).all()
        assert len(items) == 120
        assert len({item.id for item in items}) == 120
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_lock_file_open_race_is_retried(tmp_path, monkeypatch):
    """4 进程抢建同一个锁文件时 Windows 会抛 PermissionError —— 必须重试。

    实跑复现（R64）：4 个进程并发写 ``experiences.jsonl``，约一半概率有一个进程
    **在第一行**就 ``PermissionError: [Errno 13]`` 崩掉，整个 run 陪葬。根因不是
    「没拿到锁」（那条路径本来就有重试），而是 ``os.open(O_CREAT)`` 本身失败，
    发生在进锁之前。
    """
    target = tmp_path / "e.jsonl"
    real_open = jsonl_mod.os.open
    attempts = {"lock": 0}

    def flaky(path, flags, mode=0o777, **kwargs):  # noqa: ANN001
        if str(path).endswith(".lock"):
            attempts["lock"] += 1
            if attempts["lock"] <= 2:
                raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(jsonl_mod.os, "open", flaky)
    append_line(target, '{"id": "E-1"}')
    assert attempts["lock"] == 3, "前两次失败应当被重试，第三次成功"
    assert [line for line in read_lines(target)] == ['{"id": "E-1"}']


def test_lock_file_open_permanently_denied_degrades(tmp_path, monkeypatch, caplog):
    """一直打不开锁文件时：退回无锁单次写 + 警告，绝不把 run 崩掉。"""
    target = tmp_path / "e.jsonl"
    real_open = jsonl_mod.os.open

    def denied(path, flags, mode=0o777, **kwargs):  # noqa: ANN001
        if str(path).endswith(".lock"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, mode, **kwargs)

    class _FastClock:
        """让 2 秒的重试窗口在测试里瞬间过去（只替换 jsonl 模块里的 time）。"""

        def __init__(self) -> None:
            self._now = 0.0

        def monotonic(self) -> float:
            self._now += 1.0
            return self._now

        @staticmethod
        def sleep(_seconds: float) -> None:
            return None

    monkeypatch.setattr(jsonl_mod.os, "open", denied)
    monkeypatch.setattr(jsonl_mod, "time", _FastClock())
    with caplog.at_level("WARNING", logger="agentcrew.knowledge.jsonl"):
        append_line(target, '{"id": "E-2"}')

    assert [line for line in read_lines(target)] == ['{"id": "E-2"}']
    assert any("退回无锁追加" in record.message for record in caplog.records)
