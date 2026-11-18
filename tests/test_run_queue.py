"""C14：文件系统任务队列——原子领取、租约回收、取消标记。

这些用例全部离线、无网络：队列的安全性是文件语义（``O_EXCL`` + 原子替换），
所以测试也直接打文件系统，不 mock。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentcrew.runtime.queue import QueueError, RunQueue, make_worker_id


def _queue(tmp_path: Path, **kwargs) -> RunQueue:
    return RunQueue(tmp_path / "_queue", lease_seconds=kwargs.pop("lease_seconds", 60.0), **kwargs)


def test_enqueue_and_claim_in_fifo_order(tmp_path):
    queue = _queue(tmp_path)
    first = queue.enqueue(goal="第一个", run_id="R-1")
    queue.enqueue(goal="第二个", run_id="R-2")
    assert first.status == "pending"

    claimed = queue.claim("w1")
    assert claimed is not None and claimed.run_id == "R-1"
    assert claimed.status == "running"
    assert queue.claim("w1").run_id == "R-2"
    assert queue.claim("w1") is None


def test_two_workers_never_claim_same_run(tmp_path):
    """O_EXCL 是唯一的互斥来源；并发抢同一批任务不能出现重复。"""
    queue = _queue(tmp_path)
    for index in range(12):
        queue.enqueue(goal=f"任务 {index}", run_id=f"R-{index}")

    def drain(worker: str) -> list[str]:
        seen: list[str] = []
        while (item := queue.claim(worker)) is not None:
            seen.append(item.run_id)
        return seen

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(drain, ["w1", "w2", "w3", "w4"]))
    claimed = [run_id for group in results for run_id in group]
    assert len(claimed) == 12
    assert len(set(claimed)) == 12


def test_stale_lease_is_requeued(tmp_path):
    queue = _queue(tmp_path, lease_seconds=5)
    queue.enqueue(goal="会崩的任务", run_id="R-1")
    item = queue.claim("w-dead")
    assert item is not None

    claim_path = queue.root / "claims" / "R-1.json"
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    claim["lease_until"] = time.time() - 1      # 模拟 worker 崩了
    claim_path.write_text(json.dumps(claim), encoding="utf-8")

    assert queue.requeue_stale() == 1
    assert (queue.root / "pending" / "R-1.json").is_file()
    assert not (queue.root / "running" / "R-1.json").exists()
    again = queue.claim("w-alive")
    assert again is not None and again.run_id == "R-1"


def test_heartbeat_extends_and_rejects_wrong_worker(tmp_path):
    queue = _queue(tmp_path, lease_seconds=30)
    queue.enqueue(goal="x", run_id="R-1")
    assert queue.claim("w1") is not None
    before = json.loads(
        (queue.root / "claims" / "R-1.json").read_text(encoding="utf-8"))
    time.sleep(0.01)
    assert queue.heartbeat("R-1", "w1") is True
    after = json.loads(
        (queue.root / "claims" / "R-1.json").read_text(encoding="utf-8"))
    assert after["lease_until"] > before["lease_until"]
    assert queue.heartbeat("R-1", "w2") is False


def test_complete_moves_to_done_and_clears_claim(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue(goal="x", run_id="R-1")
    queue.claim("w1")
    queue.request_cancel("R-1")
    queue.complete("R-1", status="completed", worker_id="w1")
    done = json.loads(
        (queue.root / "done" / "R-1.json").read_text(encoding="utf-8"))
    assert done["status"] == "completed"
    assert "finished_at" in done
    assert not (queue.root / "claims" / "R-1.json").exists()
    assert not (queue.root / "cancel" / "R-1").exists()


def test_fail_records_error(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue(goal="x", run_id="R-1")
    queue.claim("w1")
    queue.fail("R-1", error="RuntimeError: boom", worker_id="w1")
    failed = json.loads(
        (queue.root / "failed" / "R-1.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert "boom" in failed["error"]


def test_finish_by_other_worker_is_rejected(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue(goal="x", run_id="R-1")
    queue.claim("w1")
    with pytest.raises(QueueError):
        queue.complete("R-1", status="completed", worker_id="w2")


def test_finish_without_claim_is_rejected(tmp_path):
    """P3-2：claim 不存在 = 租约已失，不能再写 done/failed。

    旧实现在 ``claim is None`` 时直接放行：租约被 ``requeue_stale`` 回收后，
    旧 worker 仍会写 done 并删掉 pending，与新 worker 双写同一 run。
    """
    queue = _queue(tmp_path)
    queue.enqueue(goal="x", run_id="R-1")
    queue.claim("w1")
    (queue.root / "claims" / "R-1.json").unlink()      # 模拟被回收
    with pytest.raises(QueueError):
        queue.complete("R-1", status="completed", worker_id="w1")
    assert not (queue.root / "done" / "R-1.json").exists()


def test_old_worker_cannot_overwrite_after_requeue(tmp_path):
    """回收 + 新 worker 接管后，旧 worker 的收尾必须被拒，结果归新 worker。"""
    queue = _queue(tmp_path, lease_seconds=1)
    queue.enqueue(goal="慢慢跑", run_id="R-1")
    queue.claim("w-old")
    # 让租约过期并把任务放回 pending
    claim_path = queue.root / "claims" / "R-1.json"
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    claim["lease_until"] = time.time() - 1
    claim_path.write_text(json.dumps(claim), encoding="utf-8")
    assert queue.requeue_stale() == 1
    assert queue.claim("w-new") is not None

    with pytest.raises(QueueError):
        queue.complete("R-1", status="completed", worker_id="w-old")
    # 新 worker 仍能正常收尾（claim 归它）
    queue.fail("R-1", error="RuntimeError: 新 worker 的结果", worker_id="w-new")
    failed = json.loads(
        (queue.root / "failed" / "R-1.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["worker_id"] == "w-new"


def test_duplicate_run_id_is_rejected(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue(goal="x", run_id="R-1")
    with pytest.raises(QueueError):
        queue.enqueue(goal="y", run_id="R-1")


def test_cancel_marker_roundtrip(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue(goal="x", run_id="R-1")
    assert queue.cancel_requested("R-1") is False
    queue.request_cancel("R-1")
    assert queue.cancel_requested("R-1") is True
    queue.clear_cancel("R-1")
    assert queue.cancel_requested("R-1") is False


def test_get_and_stats(tmp_path):
    queue = _queue(tmp_path)
    queue.enqueue(goal="a", run_id="R-1")
    queue.enqueue(goal="b", run_id="R-2")
    queue.claim("w1")
    assert queue.get("R-1").status == "running"
    assert queue.get("R-2").status == "pending"
    assert queue.get("R-none") is None
    stats = queue.stats()
    assert stats["pending"] == 1 and stats["running"] == 1


def test_worker_id_is_unique_per_process_call():
    assert make_worker_id() != make_worker_id()
