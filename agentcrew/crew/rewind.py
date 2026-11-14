"""R62：run 时间旅行（回滚到任意任务并重跑）。

主流 Agent 产品（Claude Code 的 ``/rewind``、LangGraph 的 time travel、Devin 的
replan）都允许「不改整条历史，只从某一步重来」。AgentCrew 此前只有 ``resume``：
它把**中断**的 run 接着跑完，失败任务仍然失败、已成功的产物无法在保留上游成果
的前提下重做 —— 想改一个中间任务的交付，只能整个 run 从头再跑一遍（重花全部
token）。

本模块补的正是这一层：给定 run 与目标任务，把「该任务 + 全部传递下游 + 交付
任务 ``T-FINAL``」重置为 ``PENDING``、把它们产出的产物先快照再移除、run 状态
置回 ``interrupted``；随后用既有的 ``resume`` 从该点重跑。上游已成功的任务与
其产物原样保留。

四条不变量：

1. **不改状态机**：终态没有回 ``PENDING`` 的合法边，与 ``reset_failed`` 一样
   显式绕过，并补一条 ``reason="rewind"`` 的 ``StatusChange`` 让绕过可见。
2. **先快照后删除**：旧产物一律先复制到 ``history/rewind-NNN/``；快照没写完
   之前绝不碰 checkpoint，失败时同步清掉半成品快照目录。
3. **不猜下游**：影响集 = 依赖图传递后代 ∪ ``parent_id`` 子任务（运行时动态
   委派的子任务不进依赖图，只挂父指针），再并上交付任务。
4. **不吃旧增量**：``tasks.delta.jsonl`` 里可能还留着回滚前的任务快照，
   ``resume`` 会把它叠加回 ``tasks.json`` 之上、把刚重置的状态又盖回去 ——
   所以动手前先 ``apply_deltas()`` 合并并清空。

``rewind`` 是**维护操作**，不触发任何模型调用、不写 webhook、不动审批单；
它的产物是一份可审的 ``rewind.json`` 与一条 ``run_rewound`` 审计事件。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.runtime.checkpoint import CheckpointStore, RunRecord, RunStatus
from agentcrew.runtime.ledger import Ledger
from agentcrew.runtime.lifecycle import ResumeLock
from agentcrew.task.planner import FINAL_TASK_ID
from agentcrew.task.task_board import TaskBoard
from agentcrew.utils.clock import utc_now
from agentcrew.utils.files import atomic_write_text

#: run 目录下的快照子目录名。与 ``artifacts`` / ``streams`` 平级，互不覆盖。
REWIND_DIR = "history"

#: 快照目录名前缀；序号在 run 内单调递增，便于 ``ls`` 与人工恢复。
_SNAPSHOT_PREFIX = "rewind-"

#: 允许回滚的 run 状态。``running`` 与 ``awaiting_approval`` 被排除：前者可能
#: 有活进程在写同一份 checkpoint，后者应先裁决审批单再谈重做。
REWINDABLE_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.INTERRUPTED,
        RunStatus.CANCELLED,
        RunStatus.PLANNED,
    }
)


@dataclass(frozen=True)
class RewindResult:
    """一次回滚的结果摘要（CLI / API / 测试共用同一份形状）。"""

    run_id: str
    target: str
    affected: tuple[str, ...]
    snapshot_dir: str | None
    discarded_artifacts: int
    status_before: str
    status_after: str
    instruction: str = ""
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        """机器可读视图（``--json`` 与 API 响应体共用）。"""
        return {
            "run_id": self.run_id,
            "target": self.target,
            "affected": list(self.affected),
            "snapshot_dir": self.snapshot_dir,
            "discarded_artifacts": self.discarded_artifacts,
            "status_before": self.status_before,
            "status_after": self.status_after,
            "instruction": self.instruction,
            "dry_run": self.dry_run,
        }


def affected_task_ids(board: TaskBoard, target: str) -> tuple[str, ...]:
    """回滚 ``target`` 时必须一并重做的任务 id（含目标本身，按 id 升序）。

    传递闭包覆盖两种下游：依赖边（规划期的 DAG）与 ``parent_id``（运行时动态
    委派出来的子任务 —— 它们不进 ``TaskGraph``，只认父指针）。交付任务
    ``T-FINAL`` 无条件纳入：它汇总全部产物，上游一变它必然过期。

    未知 id 抛 ``ValueError`` 并列出板上可选任务 —— 静默回滚一个不存在的任务
    是最坏的失败模式（用户以为重做了，其实什么都没发生）。
    """
    ids = set(board.ids())
    if target not in ids:
        raise ValueError(f"任务 {target!r} 不在 run 的任务板上；可选：{', '.join(board.ids())}")
    dependents: dict[str, list[str]] = {task_id: [] for task_id in ids}
    for task in board.all():
        for dep in task.dependencies:
            dependents.setdefault(dep, []).append(task.id)

    seen: set[str] = set()
    stack = [target]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(dependents.get(current, ()))
        stack.extend(child.id for child in board.children_of(current))
    if FINAL_TASK_ID in ids:
        seen.add(FINAL_TASK_ID)
    return tuple(sorted(seen))


def rewind_run(
    run_dir: Path,
    *,
    target: str,
    instruction: str = "",
    settings: Settings | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RewindResult:
    """把 ``run_dir`` 回滚到 ``target`` 之前的状态，返回结果摘要。

    ``dry_run=True`` 只读盘、只算影响集与待移除产物数量，**不写任何文件**
    （连快照目录都不建）。``instruction`` 非空时写进目标任务的任务契约
    （``description`` 追加一段带时间戳的指令、``acceptance_criteria`` 追加一条
    「补充要求」），让执行体照着做、也让 G2 评审有据可依。

    失败模式：状态不允许、任务不存在、快照写不完都在**动手前**抛；快照阶段
    失败会清掉半成品目录，此时 checkpoint 一个字都没动。删除阶段的 ``OSError``
    如实抛出、不吞掉——已删的文件仍在快照里，可人工恢复；任务状态与 ``run.json``
    此时尚未改写，重试一次即可。
    """
    run_dir = Path(run_dir)
    checkpoint = CheckpointStore(run_dir)
    record = checkpoint.load_record()
    _guard_rewindable(record)
    status_before = record.status.value

    lock = ResumeLock(run_dir)
    lock.acquire()
    try:
        if not dry_run:
            # 先合并任务增量：不清空 delta 的话，resume 会把回滚前的任务快照
            # 重新叠回 tasks.json，重置当场失效。
            checkpoint.apply_deltas()
        board = checkpoint.load_board()
        affected = affected_task_ids(board, target)
        store = ArtifactStore(checkpoint.artifacts_dir, run_id=record.run_id)
        store.load_manifest()
        discarded_count = sum(len(store.by_task(task_id)) for task_id in affected)

        if dry_run:
            return RewindResult(
                run_id=record.run_id,
                target=target,
                affected=affected,
                snapshot_dir=None,
                discarded_artifacts=discarded_count,
                status_before=status_before,
                status_after=status_before,
                instruction=instruction.strip(),
                dry_run=True,
            )

        moment = now or datetime.now()
        snapshot_dir, entries = _snapshot(
            run_dir,
            store,
            affected,
            target=target,
            moment=moment,
            record=record,
            board=board,
            instruction=instruction.strip(),
        )
        discarded = 0
        for task_id in affected:
            discarded += store.discard_task(task_id)

        _reset_tasks(
            board,
            affected,
            target=target,
            instruction=instruction.strip(),
            moment=moment,
        )
        checkpoint.save_tasks(board)

        record.status = RunStatus.INTERRUPTED
        record.updated_at = utc_now()
        stats = board.stats()
        record.stats = record.stats.model_copy(
            update={
                "total_tasks": stats.total_tasks,
                "succeeded": stats.succeeded,
                "failed": stats.failed,
                "blocked": stats.blocked,
                "skipped": stats.skipped,
            }
        )
        checkpoint.save_record(record)

        _emit(
            checkpoint,
            settings,
            run_id=record.run_id,
            target=target,
            affected=affected,
            snapshot_dir=snapshot_dir,
            discarded=discarded,
            entries=entries,
        )
        return RewindResult(
            run_id=record.run_id,
            target=target,
            affected=affected,
            snapshot_dir=str(snapshot_dir),
            discarded_artifacts=discarded,
            status_before=status_before,
            status_after=RunStatus.INTERRUPTED.value,
            instruction=instruction.strip(),
        )
    finally:
        lock.release()


# ── 内部步骤 ──────────────────────────────────────────────────────────────


def _guard_rewindable(record: RunRecord) -> None:
    """状态守卫：只允许对已停下来的 run 回滚，错误文案给出下一步动作。"""
    if record.status is RunStatus.RUNNING:
        raise ValueError(
            f"run {record.run_id!r} 正在运行（或心跳新鲜），拒绝回滚；"
            "请先 cancel，或等它结束/心跳过期后重试"
        )
    if record.status is RunStatus.AWAITING_APPROVAL:
        raise ValueError(
            f"run {record.run_id!r} 挂在人工审批上；请先 approvals/approve 裁决，再回滚"
        )
    if record.status not in REWINDABLE_STATUSES:
        raise ValueError(f"run {record.run_id!r} 的状态 {record.status.value!r} 不支持回滚")


def _next_snapshot_dir(run_dir: Path) -> Path:
    """下一个快照目录 ``history/rewind-NNN``（NNN 从 001 起单调递增）。"""
    root = run_dir / REWIND_DIR
    existing = (
        sorted(
            int(path.name[len(_SNAPSHOT_PREFIX) :])
            for path in root.glob(f"{_SNAPSHOT_PREFIX}[0-9][0-9][0-9]")
            if path.name[len(_SNAPSHOT_PREFIX) :].isdigit()
        )
        if root.is_dir()
        else []
    )
    seq = (existing[-1] + 1) if existing else 1
    return root / f"{_SNAPSHOT_PREFIX}{seq:03d}"


def _snapshot(
    run_dir: Path,
    store: ArtifactStore,
    affected: tuple[str, ...],
    *,
    target: str,
    moment: datetime,
    record: RunRecord,
    board: TaskBoard,
    instruction: str,
) -> tuple[Path, list[dict[str, Any]]]:
    """把受影响任务的旧产物复制到 ``history/rewind-NNN/`` 并写 ``rewind.json``。

    返回 ``(快照目录, 产物条目列表)``。任何一步失败都清掉半成品目录并原样
    抛出 —— 调用方此时还没有改动 checkpoint，回滚等于没发生。
    """
    snapshot_dir = _next_snapshot_dir(run_dir)
    artifacts_dir = snapshot_dir / "artifacts"
    affected_set = set(affected)
    entries: list[dict[str, Any]] = []
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        for task_id in affected:
            for artifact in store.by_task(task_id):
                source = store.resolve(artifact.path)
                destination = artifacts_dir / artifact.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                entries.append(
                    {
                        "task": task_id,
                        "path": artifact.path,
                        "kind": artifact.kind,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                        "snapshot": (Path("artifacts") / artifact.path).as_posix(),
                    }
                )
        payload = {
            "run_id": record.run_id,
            "target": target,
            "affected": list(affected),
            "instruction": instruction,
            "created_at": moment.isoformat(),
            "status_before": record.status.value,
            "artifacts": entries,
            "tasks": {
                task.id: task.model_dump(mode="json")
                for task in board.all()
                if task.id in affected_set
            },
        }
        atomic_write_text(
            snapshot_dir / "rewind.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
    except BaseException:
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        raise
    return snapshot_dir, entries


def _reset_tasks(
    board: TaskBoard,
    affected: tuple[str, ...],
    *,
    target: str,
    instruction: str,
    moment: datetime,
) -> None:
    """把受影响任务逐个重置为 ``PENDING``；目标任务的契约吸收新指令。"""
    description: str | None = None
    criteria: list[str] | None = None
    if instruction:
        target_task = board.get(target)
        stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
        description = f"{target_task.description}\n\n[回滚重做指令 {stamp}]\n{instruction}"
        criteria = [*target_task.acceptance_criteria, f"补充要求：{instruction}"]

    for task_id in affected:
        if task_id == target:
            board.reset_for_rewind(
                task_id,
                description=description,
                acceptance_criteria=criteria,
            )
        else:
            board.reset_for_rewind(task_id)


def _emit(
    checkpoint: CheckpointStore,
    settings: Settings | None,
    *,
    run_id: str,
    target: str,
    affected: tuple[str, ...],
    snapshot_dir: Path,
    discarded: int,
    entries: list[dict[str, Any]],
) -> None:
    """追加一条 ``run_rewound`` 审计事件（密钥脱敏沿用 ``Settings`` 词表）。"""
    ledger = Ledger(
        checkpoint.run_dir / "ledger.jsonl",
        run_id=run_id,
        secrets=settings.secret_values() if settings is not None else (),
    )
    try:
        ledger.emit(
            "run_rewound",
            target=target,
            affected=list(affected),
            snapshot=snapshot_dir.relative_to(checkpoint.run_dir).as_posix(),
            discarded_artifacts=discarded,
            snapshotted_artifacts=len(entries),
        )
    finally:
        ledger.close()
