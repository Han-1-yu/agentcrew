"""C14：文件系统任务队列——多 worker 横向扩展的最小原语。

**为什么不是 Redis / RabbitMQ。** 项目的红线是「离线可跑、样例不依赖网络」。
横向扩展真正需要的只有两件事：**跨进程互斥的领取**、**崩溃后的租约回收**。
这两件事在 POSIX / NTFS 的文件语义上都能做到，代价只是一个共享目录——NFS 挂上
就能跨机，不需要额外的中间件与运维。

目录布局（``root`` 通常是共享卷上的 ``<output_dir>/_queue``）::

    pending/<run_id>.json     待领取（按 mtime FIFO）
    claims/<run_id>.json      领取租约（O_EXCL 创建，天然互斥）
    running/<run_id>.json     正在跑（worker_id / lease_until）
    done/<run_id>.json        已完成（status / finished_at）
    failed/<run_id>.json      失败（error）
    cancel/<run_id>           取消标记（worker 轮询）

三条不变量：

1. **领取是原子的**：``claims/<id>.json`` 用 ``O_CREAT|O_EXCL`` 创建，两个 worker
   同时抢同一个任务只有一个成功。不依赖 ``rename`` 的覆盖语义——POSIX 会静默
   覆盖、Windows 会报错，正是跨平台 bug 的温床。
2. **崩溃可回收**：worker 定期续租；租约过期且 ``running`` 还在，任务被移回
   ``pending``。语义是**至少一次**，所以 ``Crew.run`` 必须能按同一个 run_id 重跑
   （它写 checkpoint，重跑会覆盖终态记录——接受这个代价，比丢任务好）。
3. **状态可读**：``done`` / ``failed`` 是终态快照，API 与运维脚本直接读文件，
   不需要连队列进程。

边界：队列不负责去重（同一 goal 可入队多次）、不做优先级、不做延迟重试。
需要这些时再加目录（如 ``delayed/``），不要把语义塞进 ``pending``。
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["QueueError", "QueuedRun", "RunQueue", "make_worker_id"]


class QueueError(RuntimeError):
    """队列操作失败（重复入队 / 租约不归你 / 文件损坏）。"""


@dataclass
class QueuedRun:
    """一条队列任务；``options`` 原样交给 API 的 ``_build_crew``。"""

    run_id: str
    goal: str
    options: dict[str, Any] = field(default_factory=dict)
    images: list[dict[str, str]] | None = None
    enqueued_at: float = 0.0
    worker_id: str = ""
    lease_until: float = 0.0
    status: str = "pending"
    error: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> QueuedRun:
        return cls(
            run_id=str(data.get("run_id") or ""),
            goal=str(data.get("goal") or ""),
            options=dict(data.get("options") or {}),
            images=data.get("images") or None,
            enqueued_at=float(data.get("enqueued_at") or 0.0),
            worker_id=str(data.get("worker_id") or ""),
            lease_until=float(data.get("lease_until") or 0.0),
            status=str(data.get("status") or "pending"),
            error=str(data.get("error") or ""),
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def make_worker_id() -> str:
    """worker 身份：主机 + pid + 随机后缀。跨机可读，进程重启不撞。"""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """同目录临时文件 + ``os.replace``：读方永远看到完整 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class RunQueue:
    """文件系统队列。线程/进程安全靠 ``O_EXCL`` 与原子替换，不靠内存锁。"""

    def __init__(self, root: Path, *, lease_seconds: float = 60.0) -> None:
        self.root = Path(root)
        self.lease_seconds = max(5.0, float(lease_seconds))
        for name in ("pending", "claims", "running", "done", "failed", "cancel"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    # ── 入队 ──────────────────────────────────────────────────────────────

    def enqueue(
        self,
        *,
        goal: str,
        options: dict[str, Any] | None = None,
        images: list[dict[str, str]] | None = None,
        run_id: str | None = None,
    ) -> QueuedRun:
        if not goal.strip():
            raise QueueError("goal 不能为空")
        run_id = run_id or (
            f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}")
        if any((self.root / name / f"{run_id}.json").exists()
               for name in ("pending", "running", "done", "failed")):
            raise QueueError(f"run_id 已存在：{run_id}")
        item = QueuedRun(
            run_id=run_id, goal=goal.strip(), options=dict(options or {}),
            images=images or None, enqueued_at=time.time())
        _write_json(self.root / "pending" / f"{run_id}.json", item.to_json())
        return item

    # ── 领取 / 续租 / 收尾 ────────────────────────────────────────────────

    def claim(self, worker_id: str) -> QueuedRun | None:
        """原子领取最早的一条；没有可领的返回 ``None``。"""
        self.requeue_stale()

        def _mtime(path: Path) -> float:
            # 并发下文件可能刚被别的 worker 移走；stat 失败不能把整轮 claim 打崩。
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        pending = sorted((self.root / "pending").glob("*.json"), key=_mtime)
        for path in pending:
            run_id = path.stem
            claim_path = self.root / "claims" / f"{run_id}.json"
            try:
                fd = os.open(
                    claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue          # 别人先抢到了
            except OSError:
                continue          # 目录暂不可写（NFS 抖动）：下一轮再试
            try:
                os.write(fd, json.dumps({
                    "worker_id": worker_id,
                    "lease_until": time.time() + self.lease_seconds,
                }).encode("utf-8"))
            finally:
                os.close(fd)
            data = _read_json(path)
            if data is None:      # 已被 requeue/取消移走
                claim_path.unlink(missing_ok=True)
                continue
            item = QueuedRun.from_json(data)
            item.worker_id = worker_id
            item.lease_until = time.time() + self.lease_seconds
            item.status = "running"
            _write_json(self.root / "running" / f"{run_id}.json", item.to_json())
            path.unlink(missing_ok=True)
            return item
        return None

    def heartbeat(self, run_id: str, worker_id: str) -> bool:
        """续租；租约已不属于该 worker 时返回 ``False``（它该停下了）。"""
        claim_path = self.root / "claims" / f"{run_id}.json"
        claim = _read_json(claim_path)
        if claim is None or str(claim.get("worker_id") or "") != worker_id:
            return False
        claim["lease_until"] = time.time() + self.lease_seconds
        _write_json(claim_path, claim)
        running_path = self.root / "running" / f"{run_id}.json"
        data = _read_json(running_path)
        if data is not None:
            data["lease_until"] = claim["lease_until"]
            data["worker_id"] = worker_id
            _write_json(running_path, data)
        return True

    def complete(self, run_id: str, *, status: str, worker_id: str) -> None:
        self._finish(run_id, target="done", status=status, worker_id=worker_id)

    def fail(self, run_id: str, *, error: str, worker_id: str) -> None:
        self._finish(
            run_id, target="failed", status="failed", worker_id=worker_id,
            error=error[:2000])

    def _finish(
        self, run_id: str, *, target: str, status: str, worker_id: str,
        error: str = "",
    ) -> None:
        claim_path = self.root / "claims" / f"{run_id}.json"
        claim = _read_json(claim_path)
        if claim is not None and str(claim.get("worker_id") or "") != worker_id:
            raise QueueError(f"租约不属于 {worker_id}：{run_id}")
        running_path = self.root / "running" / f"{run_id}.json"
        data = _read_json(running_path) or _read_json(
            self.root / "pending" / f"{run_id}.json")
        if data is None:
            raise QueueError(f"队列里没有该任务：{run_id}")
        item = QueuedRun.from_json(data)
        item.status = status
        item.worker_id = worker_id
        item.error = error
        payload = item.to_json()
        payload["finished_at"] = time.time()
        _write_json(self.root / target / f"{run_id}.json", payload)
        running_path.unlink(missing_ok=True)
        (self.root / "pending" / f"{run_id}.json").unlink(missing_ok=True)
        claim_path.unlink(missing_ok=True)
        (self.root / "cancel" / run_id).unlink(missing_ok=True)

    # ── 租约回收 ──────────────────────────────────────────────────────────

    def requeue_stale(self) -> int:
        """把租约过期的 ``running`` 移回 ``pending``；返回回收条数。"""
        now = time.time()
        count = 0
        for claim_path in (self.root / "claims").glob("*.json"):
            claim = _read_json(claim_path)
            if claim is None:
                # 可能是另一个 worker 刚 O_EXCL 创建、还没来得及写内容的空文件。
                # 立刻删会把它的 claim 偷走 —— 两个 worker 就会同时领到同一个
                # 任务（4 线程抢 12 个任务的测试里表现为 13 条）。只清理「存在
                # 时间已超过一个租约」的坏文件。
                try:
                    age = now - claim_path.stat().st_mtime
                except OSError:
                    continue
                if age > self.lease_seconds:
                    claim_path.unlink(missing_ok=True)
                continue
            if float(claim.get("lease_until") or 0.0) > now:
                continue
            run_id = claim_path.stem
            running_path = self.root / "running" / f"{run_id}.json"
            pending_path = self.root / "pending" / f"{run_id}.json"
            if running_path.is_file():
                data = _read_json(running_path) or {}
                data["status"] = "pending"
                data["worker_id"] = ""
                data["lease_until"] = 0.0
                _write_json(pending_path, data)
                running_path.unlink(missing_ok=True)
                count += 1
            claim_path.unlink(missing_ok=True)
        return count

    # ── 取消 ──────────────────────────────────────────────────────────────

    def request_cancel(self, run_id: str) -> None:
        marker = self.root / "cancel" / run_id
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")

    def cancel_requested(self, run_id: str) -> bool:
        return (self.root / "cancel" / run_id).is_file()

    def clear_cancel(self, run_id: str) -> None:
        (self.root / "cancel" / run_id).unlink(missing_ok=True)

    # ── 查询 ──────────────────────────────────────────────────────────────

    def get(self, run_id: str) -> QueuedRun | None:
        """按状态优先级读一条任务（running → pending → done → failed）。"""
        for name in ("running", "pending", "done", "failed"):
            data = _read_json(self.root / name / f"{run_id}.json")
            if data is not None:
                item = QueuedRun.from_json(data)
                item.status = name if item.status in ("", "pending") else item.status
                return item
        return None

    def stats(self) -> dict[str, int]:
        counts = {}
        for name in ("pending", "running", "done", "failed"):
            counts[name] = sum(
                1 for _ in (self.root / name).glob("*.json"))
        counts["claims"] = sum(
            1 for _ in (self.root / "claims").glob("*.json"))
        return counts
