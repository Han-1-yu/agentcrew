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

from agentcrew.knowledge.experience import Experience, ExperienceStore

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
from agentcrew.knowledge.experience import Experience, ExperienceStore

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
