"""R17：run 生命周期加固——心跳、僵尸 run 收敛、resume 进程锁。

此前只有 ``is_stale_running``（``run.json.updated_at`` + 24h）这一个被动判据，
用于 ``clean`` / ``delete_run`` 的删除守卫：进程被强杀后，run 会一直停在
``RUNNING``，``status`` / ``/runs`` 都显示「正在跑」，也没人阻止两个进程同时
``resume`` 同一个 run 写坏 checkpoint。

三件事各自解决一个边界：

1. **心跳**：活 run 由 daemon 线程每 30s 原子写一个独立文件。它不碰 Ledger
   （``Ledger.emit`` 无锁，后台线程写会与主线程交错），也不改 ``run.json``
   （避免与调度器的状态落盘竞争）。
2. **reconcile**：CLI ``run`` / ``serve`` 启动时扫一遍 ``output_dir``，把
   「停在 RUNNING 且心跳已过期」的 run 标记为 ``INTERRUPTED``（可恢复断点），
   并留一条 ``run_reconciled`` 审计事件。**没有心跳文件的老 run 仍走原来的
   24h 判据**，绝不因为「升级后第一次启动」就把它们全改成 interrupted。
3. **resume 锁**：``resume()`` 用 ``O_CREAT|O_EXCL`` 原子占锁，正常/异常路径都
   在 ``finally`` 释放。锁带 owner token，release 只删自己的那一份；进程被强杀
   留下的锁在没有新鲜心跳且超过阈值后可被接管。

跨平台说明：**不用 ``os.kill(pid, 0)`` 判存活**——Windows 上它会走
``TerminateProcess``，检查进程可能直接把进程杀掉。存活一律用心跳时间戳判定，
``pid`` 只写进文件供人工排查。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..utils.files import atomic_write_text, read_text
from .checkpoint import CheckpointStore, RunStatus, is_stale_running
from .ledger import Ledger

#: 心跳文件名（run 目录下，与 run.json 同级）。
HEARTBEAT_FILE = "heartbeat.json"
#: 活 run 写心跳的间隔。
HEARTBEAT_INTERVAL_SECONDS = 30.0
#: 心跳超过这个时长没刷新就认为进程已死。远大于任务级超时 / 停滞检测（300s），
#: 又足够短，让强杀遗留的 run 在下一次启动时就能被收敛。
LIVENESS_STALE_AFTER = timedelta(minutes=15)

#: resume 锁文件名。
LOCK_FILE = "resume.lock"
#: 锁的陈旧阈值；与心跳阈值同量级，避免「锁还新、心跳已死」的窗口。
LOCK_STALE_AFTER = timedelta(minutes=15)


class ResumeLockError(RuntimeError):
    """同一个 run 已有活进程在续跑，或锁文件无法接管。"""


def _heartbeat_path(run_dir: Path) -> Path:
    return Path(run_dir) / HEARTBEAT_FILE


def write_heartbeat(run_dir: Path, *, now: datetime | None = None) -> None:
    """原子写一帧心跳；失败不向上抛（心跳是辅助设施，不能让 run 挂掉）。"""
    payload = {
        "pid": os.getpid(),
        "updated_at": (now or datetime.now()).isoformat(),
    }
    try:
        atomic_write_text(
            _heartbeat_path(run_dir),
            json.dumps(payload, ensure_ascii=False),
        )
    except OSError:
        pass


def read_heartbeat(run_dir: Path) -> datetime | None:
    """读最近一帧心跳时间；文件缺失 / 损坏返回 ``None``（交给旧判据兜底）。"""
    try:
        data = json.loads(read_text(_heartbeat_path(run_dir)))
        if not isinstance(data, dict):
            return None
        raw = data.get("updated_at")
        if not isinstance(raw, str):
            return None
        return datetime.fromisoformat(raw)
    except (OSError, ValueError, TypeError):
        return None


def clear_heartbeat(run_dir: Path) -> None:
    """正常收尾时删掉心跳；文件已被别的进程删掉也算成功。"""
    try:
        _heartbeat_path(run_dir).unlink()
    except OSError:
        pass


class RunHeartbeat:
    """一次 run 的心跳线程；``start`` / ``stop`` 都幂等。"""

    def __init__(
        self, run_dir: Path, *, interval: float = HEARTBEAT_INTERVAL_SECONDS
    ) -> None:
        self._run_dir = Path(run_dir)
        self._interval = max(interval, 0.1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        # 先同步写首帧：拿到锁之后、线程起之前的那段窗口也必须有存活证据。
        write_heartbeat(self._run_dir)
        self._thread = threading.Thread(
            target=self._run, name="agentcrew-heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            write_heartbeat(self._run_dir)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        clear_heartbeat(self._run_dir)


def is_orphaned_running(run_dir: Path, *, now: datetime | None = None) -> bool:
    """这个 run 是不是「停在 RUNNING 但进程已经没了」。

    * 读不出 ``run.json`` / 不是 RUNNING（含 ``awaiting_approval``）→ False，
      与删除守卫同方向的 fail-closed。
    * 有心跳文件 → 只看心跳新鲜度（老 ``updated_at`` 不再重要，那正是心跳要
      解决的问题：长任务期间没有状态跃迁）。
    * 没有心跳文件（升级前的老 run）→ 回退 ``is_stale_running`` 的 24h 判据，
      保持既有语义不变。
    """
    moment = now or datetime.now()
    try:
        data = json.loads(read_text(Path(run_dir) / "run.json"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("status") != RunStatus.RUNNING.value:
        return False
    heartbeat = read_heartbeat(run_dir)
    if heartbeat is None:
        return is_stale_running(run_dir, now=moment)
    return moment - heartbeat > LIVENESS_STALE_AFTER


def reconcile_stale_runs(
    output_dir: Path, *, now: datetime | None = None, dry_run: bool = False,
) -> list[dict[str, Any]]:
    """把孤儿 RUNNING 标记成 ``INTERRUPTED``，返回逐 run 的结果。

    只改 ``run.json`` 的状态与 ``updated_at``，不动产物、不重放检查点、不删目录；
    标记后 ``resume`` 会按既有的 interrupted 路径修复任务状态并续跑。
    """
    moment = now or datetime.now()
    output_dir = Path(output_dir)
    results: list[dict[str, Any]] = []
    for run_id in CheckpointStore.list_runs(output_dir):
        run_dir = output_dir / run_id
        if not is_orphaned_running(run_dir, now=moment):
            continue
        heartbeat = read_heartbeat(run_dir)
        reason = ("heartbeat_expired" if heartbeat is not None
                  else "no_heartbeat_stale")
        entry: dict[str, Any] = {
            "run_id": run_id, "ok": True, "dry_run": dry_run, "reason": reason,
        }
        if dry_run:
            results.append(entry)
            continue
        try:
            store = CheckpointStore(run_dir)
            record = store.load_record()
            record.status = RunStatus.INTERRUPTED
            record.updated_at = moment
            store.save_record(record)
            Ledger(run_dir / "ledger.jsonl", run_id=run_id).emit(
                "run_reconciled",
                from_status=RunStatus.RUNNING.value,
                to_status=RunStatus.INTERRUPTED.value,
                reason=reason,
            )
        except (OSError, ValueError, TypeError) as exc:
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        results.append(entry)
    return results


class ResumeLock:
    """run 目录级的跨进程 resume 锁。

    ``O_CREAT|O_EXCL`` 是唯一可靠的原子占锁手段（``exists()`` + ``write()``
    两步之间有竞态）。锁内容只有 owner token / pid / 创建时间，**不写任何密钥**。
    """

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = Path(run_dir)
        self._path = self._run_dir / LOCK_FILE
        self._owner = ""
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self, *, now: datetime | None = None) -> None:
        """占锁；已有活锁时抛 :class:`ResumeLockError`。"""
        if self._held:
            raise ResumeLockError("锁已由本实例持有")
        moment = now or datetime.now()
        for _ in range(2):
            try:
                handle = os.open(
                    self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if not self._lock_is_stale(now=moment):
                    raise ResumeLockError(
                        f"run {self._run_dir.name!r} 正在被另一个进程续跑"
                        f"（锁：{self._path}）；请等待它结束，或确认进程已死后"
                        "等待锁过期重试"
                    ) from None
                try:
                    self._path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise ResumeLockError(
                        f"无法清理陈旧锁 {self._path}：{exc}") from exc
                continue
            except OSError as exc:
                raise ResumeLockError(
                    f"无法创建 resume 锁 {self._path}：{exc}") from exc
            owner = uuid.uuid4().hex
            payload = json.dumps({
                "owner": owner,
                "pid": os.getpid(),
                "created_at": moment.isoformat(),
            }, ensure_ascii=False)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    file.write(payload)
            except OSError as exc:
                try:
                    self._path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ResumeLockError(
                    f"无法写入 resume 锁 {self._path}：{exc}") from exc
            self._owner = owner
            self._held = True
            return
        raise ResumeLockError(f"无法接管 resume 锁 {self._path}")

    def _lock_is_stale(self, *, now: datetime) -> bool:
        """锁能否被接管：新鲜心跳一律不接管，其余看锁的年龄 / 可读性。"""
        heartbeat = read_heartbeat(self._run_dir)
        if heartbeat is not None and now - heartbeat <= LIVENESS_STALE_AFTER:
            return False
        try:
            data = json.loads(read_text(self._path))
            created = datetime.fromisoformat(data["created_at"])
        except (OSError, ValueError, TypeError, KeyError):
            # 锁文件坏了：没有新鲜心跳就没有活进程的证据，允许自愈接管。
            return heartbeat is None or now - heartbeat > LIVENESS_STALE_AFTER
        return now - created > LOCK_STALE_AFTER

    def release(self) -> None:
        """只删自己持有的锁；owner 不匹配时绝不替别人解锁。幂等。"""
        if not self._held:
            return
        try:
            data = json.loads(read_text(self._path))
            if data.get("owner") == self._owner:
                self._path.unlink()
        except (OSError, ValueError, TypeError):
            pass
        self._held = False
        self._owner = ""

    def __enter__(self) -> ResumeLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.release()
        return False
