"""Checkpoint 持久化与恢复（spec §13）。

``run.json``（:class:`RunRecord`）与 ``tasks.json``（``TaskBoard`` 全量快照）是
断点续跑的唯一事实源。两个文件都经
:func:`~agentcrew.utils.files.atomic_write_text` 落盘（同目录临时文件 +
``os.replace``）：目标文件任何时刻只可能是「旧的完整内容」或「新的完整内容」，
绝无半截 JSON —— 这正是「resume 绝不因半个文件重复执行」的依据。

恢复规则见 :func:`repair_statuses`：``RUNNING`` 任务因进程死亡必须重执行
（``RUNNING → READY``）；``REWORK`` **保持原状**（状态机没有 ``REWORK → READY``
边，而 ``TaskBoard.take_ready()`` 本就同时派发 ``READY`` 与 ``REWORK``，返工任务
无需修复即可续跑）；``REVIEW`` 保持（产物已完整落盘，只需重跑门禁）；
``attempts`` / ``rework_count`` **一律不重置**，额度在派发时即扣除（spec §6.2），
一次中断消耗一次尝试，从机制上杜绝「中断 → resume → 中断」的无限循环。

``schema_version`` 不匹配（含缺失）时 :meth:`CheckpointStore.load_record` 抛
:class:`SchemaMismatchError`：不认识的格式必须被上报，不做静默迁移。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.task.task import RunStats, Task, TaskStatus
from agentcrew.task.task_board import TaskBoard
from agentcrew.utils.files import (
    atomic_write_text,
    is_link_like,
    read_text,
    slugify_run_id,
)

#: ``run.json`` 的 schema 版本；读到不同值（含缺失）一律抛 SchemaMismatchError。
SCHEMA_VERSION = 1

#: ``create()`` 在 run 目录下建立的子目录。``artifacts`` 供 ArtifactStore 落产物，
#: ``context`` 供 ContextBus 的 decisions.jsonl，``review`` / ``diagnostics`` 供
#: 评审材料与停滞报告；它们必须在任何写入者之前就位。
_RUN_SUBDIRS = ("artifacts", "context", "review", "diagnostics")

#: P3-8：增量 checkpoint 的自动合并（compaction）阈值。``tasks.delta.jsonl`` 每追加
#: 这么多行就合并回 ``tasks.json`` 并清空 delta 文件——避免一个长 run 把 delta 无限
#: 撑大，也让 resume 不必重放几百行增量。
DELTA_COMPACTION_THRESHOLD = 50

#: 显式传入的 run_id 的允许字符集：与 ``slugify_run_id`` 的输出同族，不含任何分隔符。
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class SchemaMismatchError(ValueError):
    """``run.json`` 的 ``schema_version`` 与当前支持的版本不一致。

    不做静默迁移：不认识的格式宁可报错，让用户用匹配版本的 AgentCrew 读取。
    """

    def __init__(self, found: object, expected: int) -> None:
        super().__init__(
            f"run.json schema_version={found!r} 与当前支持的 {expected} 不一致；"
            "不做静默迁移，请用匹配版本的 AgentCrew 读取该 run"
        )
        self.found = found
        self.expected = expected


class RunStatus(str, Enum):
    """run 级状态（spec §13.4）。

    ``str`` 混入使 JSON 序列化为裸字符串；插值进 f-string 时必须取 ``.value``
    （``f"{RunStatus.RUNNING}"`` 会渲染成 ``"RunStatus.RUNNING"``）。
    """

    RUNNING = "running"
    INTERRUPTED = "interrupted"  # Ctrl-C 落盘后的可恢复断点（不是 CANCELLED）
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunRecord(BaseModel):
    """``run.json`` 的模型：run 元信息 + 汇总统计 + 配置快照。

    ``config_snapshot`` 在 run 创建时捕获（Task 29 传
    ``settings.model_dump(mode="json")``），resume 时用它对比当前配置是否被改动
    （spec §13.3 第 3 步）。时间字段为 naive 本地时间，与 ``Task`` 一致。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    schema_version: int = SCHEMA_VERSION
    project_goal: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    config_snapshot: dict[str, Any]
    stats: RunStats = Field(default_factory=RunStats)


def _dump_json(data: Any) -> str:
    """统一的 JSON 落盘格式：中文可读（``ensure_ascii=False``）、两空格缩进、
    结尾换行、不可直接序列化的值由 ``default=str`` 兜底。"""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n"


def _validated_run_id(run_id: str) -> str:
    """校验显式传入的 ``run_id``；非法即抛 ``ValueError``，绝不拼接进路径。

    run_id 会被直接 join 到 ``output_dir`` 之下，且 Task 32 的 ``resume`` /
    ``status`` 子命令从命令行取值——不校验时 ``"../x"`` 会把 run 写到
    ``output_dir`` 之外（``list_runs`` / ``clean`` 都看不见它），空串、盘符、
    反斜杠同理。只放行 ``[A-Za-z0-9._-]+``，并显式拒绝 ``.`` / ``..``（字符集
    本身允许这两个名字，但它们是路径穿越片段）。``slugify_run_id`` 生成的
    ``YYYYMMDD-HHMMSS-<4hex>`` 天然在集内。
    """
    if run_id in (".", "..") or _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError(
            f"非法 run_id: {run_id!r}；只允许 [A-Za-z0-9._-]+ 且不得为 '.' / '..'"
        )
    return run_id


def _run_dirs(output_dir: Path) -> list[Path]:
    """``output_dir`` 下「是目录且含 ``run.json``」的条目；其余一律不算 run。"""
    if not output_dir.is_dir():
        return []
    return [
        child
        for child in output_dir.iterdir()
        if child.is_dir() and (child / "run.json").is_file()
    ]


class CheckpointStore:
    """绑定到一个 ``run_dir`` 的 checkpoint 读写器。

    实例只做 I/O；``_record`` / ``_board`` 仅记住最近一次**成功**读写的对象，
    让无参 :meth:`save` 能一次补写两者（写失败时不更新，见
    :meth:`save_record`）。目录不存在时构造即抛 ``FileNotFoundError``——对死
    run 的 resume 应在加载阶段就明确失败。
    """

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = Path(run_dir)
        if not self._run_dir.is_dir():
            raise FileNotFoundError(f"run 目录不存在: {self._run_dir}")
        self._record: RunRecord | None = None
        self._board: TaskBoard | None = None

    # ---- 路径 ----

    @property
    def run_dir(self) -> Path:
        """run 根目录（``output/<run_id>/``）。"""
        return self._run_dir

    @property
    def artifacts_dir(self) -> Path:
        """产物根目录；Task 29 用它构造 ``ArtifactStore``。"""
        return self._run_dir / "artifacts"

    @property
    def _record_path(self) -> Path:
        return self._run_dir / "run.json"

    @property
    def _tasks_path(self) -> Path:
        return self._run_dir / "tasks.json"

    @property
    def _delta_path(self) -> Path:
        """P3-8：增量任务变更日志（JSONL），与 ``tasks.json`` 同目录。"""
        return self._run_dir / "tasks.delta.jsonl"

    # ---- 创建与查找 ----

    @classmethod
    def create(
        cls,
        output_dir: Path,
        *,
        project_goal: str,
        config_snapshot: dict[str, Any],
        run_id: str | None = None,
    ) -> CheckpointStore:
        """建立完整 run 布局并落第一条 ``run.json``，返回绑定该目录的 store。

        ``run_id`` 缺省用 :func:`slugify_run_id` 生成（``YYYYMMDD-HHMMSS-<4hex>``）；
        显式传入时先过 :func:`_validated_run_id`（拒绝 ``../x``、分隔符等会把 run
        写到 ``output_dir`` 之外的名字）。子目录 ``artifacts/`` / ``context/`` /
        ``review/`` / ``diagnostics/`` 一并建好。初始记录**落盘**而非只留内存：
        ``list_runs`` / ``clean`` 只认含 ``run.json`` 的目录，不写盘的话刚建的 run
        对它们不存在。

        目标目录已有 ``run.json`` 时抛 ``FileExistsError``：同名 run_id 可能对应
        一个**活着**的 run，覆盖会重置它的状态与统计——宁可要求调用方换 id。
        """
        now = datetime.now()
        if run_id is None:
            run_id = slugify_run_id(now)
        else:
            run_id = _validated_run_id(run_id)
        run_dir = Path(output_dir) / run_id
        if (run_dir / "run.json").is_file():
            raise FileExistsError(
                f"run 已存在，拒绝覆盖: {run_dir}；请换一个 run_id 或先移走旧 run"
            )
        run_dir.mkdir(parents=True, exist_ok=True)
        for name in _RUN_SUBDIRS:
            (run_dir / name).mkdir(parents=True, exist_ok=True)
        store = cls(run_dir)
        store.save_record(
            RunRecord(
                run_id=run_dir.name,
                project_goal=project_goal,
                status=RunStatus.RUNNING,
                created_at=now,
                updated_at=now,
                config_snapshot=config_snapshot,
            )
        )
        return store

    @staticmethod
    def list_runs(output_dir: Path) -> list[str]:
        """``output_dir`` 下全部 run_id，按目录名降序（即创建时间从新到旧）。

        只认含 ``run.json`` 的目录；``output_dir`` 不存在时返回空列表（读取宽、
        写入严：列举不该因为目录还没建就报错）。
        """
        return sorted((d.name for d in _run_dirs(Path(output_dir))), reverse=True)

    @classmethod
    def load_latest(cls, output_dir: Path) -> CheckpointStore:
        """绑定最近一个 run；一个都没有时抛 ``FileNotFoundError``。"""
        runs = cls.list_runs(output_dir)
        if not runs:
            raise FileNotFoundError(f"{output_dir} 下没有可恢复的 run")
        return cls(Path(output_dir) / runs[0])

    # ---- 写 ----

    def save_record(self, record: RunRecord) -> None:
        """原子写 ``run.json``。

        写失败（含 ``KeyboardInterrupt``）时目标文件保持旧内容，且不在内存中
        假装已保存 —— 跟踪引用只在写成功后才更新。
        """
        payload = _dump_json(record.model_dump(mode="json"))
        atomic_write_text(self._record_path, payload)
        self._record = record

    def save_tasks(self, board: TaskBoard) -> None:
        """原子写 ``tasks.json``（``{"tasks": [...全量快照...]}``）。"""
        payload = _dump_json({"tasks": board.snapshot()})
        atomic_write_text(self._tasks_path, payload)
        self._board = board

    def save(
        self,
        record: RunRecord | None = None,
        board: TaskBoard | None = None,
    ) -> None:
        """便捷：一次写盘 record 与 board。

        省略参数时用最近一次 ``save_record`` / ``save_tasks``（或 ``load_record``
        / ``load_board``）交给本 store 的对象；任一缺失即抛 ``RuntimeError`` ——
        静默少写一个文件会把「同时存两者」变成半个 checkpoint。
        """
        record = self._record if record is None else record
        board = self._board if board is None else board
        if record is None or board is None:
            raise RuntimeError(
                "save() 缺少 record 或 board：请显式传入，或先 save/load 过一次"
            )
        self.save_record(record)
        self.save_tasks(board)

    # ---- 读 ----

    def load_record(self) -> RunRecord:
        """读 ``run.json``；``schema_version`` 不匹配（含缺失）抛异常。"""
        data = json.loads(read_text(self._record_path))
        version = data.get("schema_version") if isinstance(data, dict) else None
        if version != SCHEMA_VERSION:
            raise SchemaMismatchError(version, SCHEMA_VERSION)
        record = RunRecord.model_validate(data)
        self._record = record
        return record

    def load_board(self) -> TaskBoard:
        """读 ``tasks.json`` 重建 ``TaskBoard``（含 reviews / status_history）。

        P3-8：若 ``tasks.delta.jsonl`` 存在（有未落盘合并的增量），先把它应用到
        内存快照上再建面板——resume 自动拿到最新状态，无需调用方区分。没有 delta
        文件时行为与改造前逐字一致（向后兼容）。
        """
        tasks = self._read_base_tasks()
        tasks = self._apply_deltas(tasks)
        board = TaskBoard.from_snapshot(tasks)
        self._board = board
        return board

    # ---- P3-8：增量 checkpoint ----

    def save_task_delta(self, task: Task) -> None:
        """增量保存单个任务的最新状态（追加到 ``tasks.delta.jsonl``）。

        替代「每次任务变化都全量重写 tasks.json」：一行 JSONL = 一个任务的快照，
        远小于整板。达到 :data:`DELTA_COMPACTION_THRESHOLD` 行时自动合并回
        ``tasks.json``（compaction）。写格式::

            {"op": "update", "task_id": "T-1", "changes": {<任务完整 dict>}}

        （``changes`` 存任务完整 dict，apply 即整体替换——不做字段级 diff，足够简单
        且不会漏状态。）
        """
        record = {
            "op": "update",
            "task_id": task.id,
            "changes": task.model_dump(mode="json"),
        }
        self._run_dir.mkdir(parents=True, exist_ok=True)
        with open(self._delta_path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(record, ensure_ascii=False, default=str) + "\n"
            )
        if self._delta_line_count() >= DELTA_COMPACTION_THRESHOLD:
            self.apply_deltas()

    def apply_deltas(self) -> int:
        """把 ``tasks.delta.jsonl`` 合并进 ``tasks.json``（compaction）。

        合并后清空 delta 文件，并同步内存里的 board。无 delta 文件时是 no-op，
        返回 0。返回实际应用的增量条数。
        """
        if not self._delta_path.exists():
            return 0
        tasks = self._apply_deltas(self._read_base_tasks())
        atomic_write_text(self._tasks_path, _dump_json({"tasks": tasks}))
        applied = self._delta_line_count()
        self._delta_path.unlink()
        board = TaskBoard.from_snapshot(tasks)
        self._board = board
        return applied

    def load_with_deltas(self) -> dict[str, Task]:
        """读 ``tasks.json`` + 应用全部未合并 delta，返回 ``{task_id: Task}``。

        不回写盘（纯读合并）——需要落盘合并时调 :meth:`apply_deltas`。无 delta
        文件时等价于读全量 ``tasks.json``。
        """
        tasks = self._apply_deltas(self._read_base_tasks())
        return {data["id"]: Task.model_validate(data) for data in tasks}

    def _read_base_tasks(self) -> list[dict]:
        """读全量 ``tasks.json`` 的任务列表（不存在视为空板）。"""
        if not self._tasks_path.exists():
            return []
        data = json.loads(read_text(self._tasks_path))
        return data.get("tasks", [])

    def _apply_deltas(self, base_tasks: list[dict]) -> list[dict]:
        """把 ``tasks.delta.jsonl`` 的每一行增量叠到 ``base_tasks`` 上。

        读宽容：坏行跳过。``update`` 整体替换该任务；``add`` 插入新任务。保留
        任务首次出现的顺序（base 顺序在前，新增追加在后），让合并后的 tasks.json
        稳定可读。
        """
        by_id: dict[str, dict] = {}
        order: list[str] = []
        for task in base_tasks:
            if task["id"] not in by_id:
                order.append(task["id"])
            by_id[task["id"]] = task
        if not self._delta_path.exists():
            return [by_id[k] for k in order]
        with open(self._delta_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                op = record.get("op")
                if op == "update":
                    task_id = record["task_id"]
                    by_id[task_id] = record["changes"]
                    if task_id not in order:
                        order.append(task_id)
                elif op == "add":
                    task = record["task"]
                    by_id[task["id"]] = task
                    order.append(task["id"])
        return [by_id[k] for k in order]

    def _delta_line_count(self) -> int:
        """``tasks.delta.jsonl`` 的非空行数（无文件返回 0）。"""
        if not self._delta_path.exists():
            return 0
        return sum(
            1
            for line in read_text(self._delta_path).splitlines()
            if line.strip()
        )


def repair_statuses(board: TaskBoard) -> int:
    """resume 时的状态修复（spec §13.3），返回修复条数。

    进程已死，内存态不可信。逐状态规则：

    - ``RUNNING → READY``：中断发生在执行中，产物可能只写了一半，必须重新执行
      （该边在 18 条合法跃迁内）。
    - ``REWORK`` **保持不动**：状态机没有 ``REWORK → READY`` 边（Task 6 锁定的
      18 条边），而 ``TaskBoard.take_ready()`` 本就同时派发 ``READY`` 与
      ``REWORK``，返工任务无需任何修复即可续跑；spec §13.3 的「REWORK → READY」
      意图由派发通道等价实现。若在此调用 ``transition`` 会抛
      ``IllegalTransitionError``。
    - ``REVIEW`` 保持：产物已完整落盘，只需重跑门禁，远比重跑 Agent 便宜。
      重跑门禁的**执行者**是 ``Scheduler``：它会把停在 ``REVIEW`` 且没有 worker
      的任务补派发给 ``Gatekeeper``（``Scheduler._regate_ids``）。只保留状态是
      不够的 —— ``take_ready`` 只派发 ``READY`` / ``REWORK``，重启后没有任何
      worker 认领它，任务永远不终结、下游全部卡死（停滞，退出码 4）。
    - ``PENDING`` / ``READY`` / ``BLOCKED`` 保持：由正常调度重算。
    - 终态（``SUCCEEDED`` / ``FAILED`` / ``CANCELLED`` / ``SKIPPED``）不动。

    ``attempts`` / ``rework_count`` **一律不重置**：额度在派发时即扣除
    （spec §6.2），一次中断就消耗一次尝试，这从机制上杜绝「中断 → resume →
    中断」的无限循环。
    """
    repaired = 0
    for task in board.all():
        if task.status is TaskStatus.RUNNING:
            board.transition(task.id, TaskStatus.READY, reason="resume-repair")
            repaired += 1
    return repaired


def _assert_deletable(target: Path, root: Path) -> None:
    """删除前的边界检查；任何可疑目标抛 ``RuntimeError``（绝不静默跳过）。

    用显式检查而不是 ``assert``（``python -O`` 会剥掉 assert）。``shutil.rmtree``
    在 Windows 上会跟进 junction 并在目标里删文件（``os.path.islink`` 不认
    junction），故链接样式的条目必须在 rmtree 之前拦住；resolve 后的严格下界
    检查则兜住任何把目标拼出 ``output_dir`` 之外的情况。
    """
    if is_link_like(target):
        raise RuntimeError(f"拒绝删除链接样式的 run 目录: {target}")
    resolved = target.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise RuntimeError(f"拒绝删除 output_dir 之外的路径: {target} -> {resolved}")


def clean(output_dir: Path, *, keep: int, dry_run: bool = False) -> list[Path]:
    """按 run_id 倒序保留最近 ``keep`` 个 run，删除其余（spec §13.5）。

    只处理「目录且含 ``run.json``」的条目：散落文件、外来目录、缺 ``run.json``
    的目录原样保留。run_id（``YYYYMMDD-HHMMSS-<4hex>``）按目录名降序即按创建
    时间从新到旧；``keep`` 为负抛 ``ValueError``；``dry_run`` 只返回将被删除的
    路径，不落任何删除（返回的是原目录路径，按从新到旧排列）。

    删除循环的失败模式是删掉用户的数据，故边界格外保守，且**先整组校验、再整组
    删除**：``runs[keep:]`` 里任何一个目标链接样式（符号链接 / Windows junction /
    其他重解析点）或 resolve 后不严格位于 ``output_dir`` 之下，整次 clean 就抛
    ``RuntimeError`` 且**一个都不删**——调用方拿到的语义是「什么都没删」，而不是
    「删了一半然后抛异常」。``dry_run`` 同样跑校验：干跑要能预告真跑会拒绝什么。
    """
    if keep < 0:
        raise ValueError(f"keep 必须 >= 0：{keep}")
    output_dir = Path(output_dir)
    root = output_dir.resolve()
    runs = sorted(_run_dirs(output_dir), key=lambda path: path.name, reverse=True)
    targets = runs[keep:]
    for target in targets:
        _assert_deletable(target, root)
    removed: list[Path] = []
    for target in targets:
        if not dry_run:
            shutil.rmtree(target)
        removed.append(target)
    return removed


def delete_run(output_dir: Path, run_id: str) -> int:
    """删除单个 run 目录，返回释放的字节数（P0-4）。

    与 :func:`clean` 共享 ``_assert_deletable`` 边界校验（链接样式 / output_dir
    之外一律拒绝）。``running`` 状态的 run 拒绝删除（提示先 cancel），因为删一个
    活着的 run 等于在进程写盘中途拔电源 —— 产物可能只写了一半，且进程下次
    checkpoint 会因目录消失而崩。非 running 状态（completed / failed / cancelled /
    interrupted）均可删除。

    删除前先统计目录总大小（``shutil.rmtree`` 不返回释放量），用于 CLI 打印
    「释放 N 字节」。run_id 必须通过 :func:`_validated_run_id` 校验（拒绝
    ``../x`` 等路径注入）。run 不存在时抛 ``FileNotFoundError``。
    """
    output_dir = Path(output_dir)
    run_id = _validated_run_id(run_id)
    run_dir = output_dir / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run 不存在: {run_dir}")
    record_path = run_dir / "run.json"
    if record_path.is_file():
        try:
            data = json.loads(read_text(record_path))
            status = data.get("status") if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError):
            status = None
        if status == RunStatus.RUNNING.value:
            raise RuntimeError(
                f"run {run_id!r} 正在运行中，拒绝删除：请先 cancel 该 run"
                f"（python main.py cancel {run_id}）")
    _assert_deletable(run_dir, output_dir.resolve())
    freed = sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file())
    shutil.rmtree(run_dir)
    return freed
