"""产物安全落盘与清单（spec §9.2/§9.3）。

``ArtifactStore`` 是**唯一**把模型产出写进磁盘的代码：``commit`` 先过
:func:`~agentcrew.utils.files.safe_relpath`、与任务契约比对、确认目标仍在
``root`` 内且不经过任何链接（符号链接或 Windows 重解析点），再原子写入。所有
校验都在 ``mkdir`` 之前完成 —— 任何一步失败都不得留下文件、目录或内存记录。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from agentcrew.artifacts.spec import ArtifactRecord, ArtifactSpec, Manifest
from agentcrew.task.task import Task
from agentcrew.utils.files import (
    UnsafePathError,
    atomic_write_text,
    is_link_like,
    read_text,
    safe_relpath,
    sha256_bytes,
    tree,
)


class ArtifactConflictError(RuntimeError):
    """同一路径已由**其他任务**写入（spec §9.2）。

    拆解期已保证同一 run 内产物路径两两不冲突，此处是落盘前的防御性二次检查：
    真出现冲突说明契约漂移或模型越权，应判失败而不是静默覆盖别人的产物。
    """

    def __init__(self, path: str, existing_task: str, new_task: str) -> None:
        super().__init__(
            f"产物 {path} 已由任务 {existing_task} 写入，任务 {new_task} 不得覆盖"
        )
        self.path = path
        self.existing_task = existing_task
        self.new_task = new_task


class ArtifactTooLargeError(ValueError):
    """编码后字节数超过契约的 ``max_bytes``。"""

    def __init__(self, path: str, size: int, limit: int) -> None:
        super().__init__(f"产物 {path} 大小 {size} 字节超过上限 {limit} 字节")
        self.path = path
        self.size = size
        self.limit = limit


class UndeclaredArtifactError(ValueError):
    """路径不在任务契约 ``deliverables`` 里（spec §9.1）。"""

    def __init__(self, path: str, task_id: str) -> None:
        super().__init__(f"任务 {task_id} 未声明产物 {path}，拒绝落盘")
        self.path = path
        self.task_id = task_id


class LedgerLike(Protocol):
    """Task 14 ``Ledger`` 的结构化子集；用 ``Protocol`` 避免反向依赖运行时包。"""

    def emit(self, event: str, **data: Any) -> None:
        """记录一次事件。"""
        ...


class ArtifactStore:
    """一次 run 的产物仓库；``root`` 即 ``artifacts/`` 目录本身。"""

    def __init__(
        self, root: Path, *, run_id: str, ledger: LedgerLike | None = None
    ) -> None:
        self._root = Path(root)
        self._run_id = run_id
        self._ledger = ledger
        self._records: list[ArtifactRecord] = []

    @property
    def root(self) -> Path:
        """产物根目录（``artifacts/`` 本身）。"""
        return self._root

    def resolve(self, rel: str) -> Path:
        """把相对路径解析为 ``root`` 下的绝对路径；不安全即抛 ``UnsafePathError``。

        三道闸门（spec §9.3）：``safe_relpath`` 校验并规范化；``resolve()`` 之后
        目标必须仍在 ``root`` 内；从 ``root`` 到目标的**每一段**都不得是符号链接或
        Windows 重解析点（junction/mount point —— ``is_symlink()`` 对它们返回
        False）。尚不存在的片段直接放行：它由 ``commit`` 现场创建，不可能是链接。
        """
        normalized = safe_relpath(rel)
        root = self._root.resolve()
        candidate = (root / normalized).resolve()
        if not candidate.is_relative_to(root):
            raise UnsafePathError(f"路径逃逸产物根目录: {rel!r}")
        current = root
        for part in normalized.parts:
            current = current / part
            try:
                linked = is_link_like(current)
            except OSError:
                continue  # 尚不存在或中途消失：不是链接，交给写入步骤定夺
            if linked:
                raise UnsafePathError(
                    f"路径经过符号链接/重解析点，拒绝访问: {rel!r}（{part!r} 处）"
                )
        return candidate

    def commit(
        self, rel: str, content: str, *, spec: ArtifactSpec, task: Task
    ) -> ArtifactRecord:
        """校验并原子写入一个产物，返回落盘记录。

        顺序（**先校验后写盘**，任何一步失败都不留文件、目录或记录）：

        1. :func:`~agentcrew.utils.files.safe_relpath` 规范化 ``rel``；
        2. 规范化结果必须等于 ``spec.path``（后者构造时已规范化），否则
           :class:`UndeclaredArtifactError` —— 模型写 ``./a.py``/``a\\b.py``
           不算越界；
        3. :meth:`resolve` 的根目录包含性与链接检查；
        4. 同路径已由**其他任务**写入 → :class:`ArtifactConflictError`；同一任务
           返工重写则覆盖原记录（spec §9.2）；
        5. 编码后字节数对 ``spec.max_bytes`` 校验（→ :class:`ArtifactTooLargeError`）；
        6. ``mkdir(parents=True, exist_ok=True)`` 后原子写；
        7. 更新内存记录并（有 ledger 时）发 ``artifact_written`` 事件。

        ``spec.min_bytes`` **故意不在这里校验**：它是「欠尺寸 = 质量缺陷」的判据，
        归 G1 静态门禁，好让产物先落盘、由 G1 出 ``fix_hints`` 走返工通道；若在
        ``commit`` 抛错，Agent 会把它当执行失败烧掉一次 attempt。``max_bytes`` 则是
        「防模型跑飞」的资源上限（spec §5.2），字节不该落盘，故留在写入前。
        """
        normalized = safe_relpath(rel).as_posix()
        if normalized != spec.path:
            raise UndeclaredArtifactError(normalized, task.id)

        target = self.resolve(normalized)

        index = next(
            (i for i, r in enumerate(self._records) if r.path == normalized), None
        )
        existing = None if index is None else self._records[index]
        if existing is not None and existing.produced_by_task != task.id:
            raise ArtifactConflictError(
                normalized, existing.produced_by_task, task.id
            )

        data = content.encode("utf-8")
        if len(data) > spec.max_bytes:
            raise ArtifactTooLargeError(normalized, len(data), spec.max_bytes)
        # 只有上限在这里：min_bytes 是 G1 的质量判据，见 docstring。

        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, content)

        record = ArtifactRecord(
            path=normalized,
            kind=spec.kind,
            size_bytes=len(data),
            sha256=sha256_bytes(data),
            produced_by_task=task.id,
            produced_by_role=task.role,
            written_at=datetime.now(),
        )
        if index is None:
            self._records.append(record)
        else:
            # 返工重写：原地更新，不追加重复条目（spec §9.2）
            self._records[index] = record
        if self._ledger is not None:
            self._ledger.emit(
                "artifact_written",
                task_id=task.id,
                path=normalized,
                size_bytes=record.size_bytes,
                sha256=record.sha256,
            )
        return record

    def read(self, rel: str, *, max_bytes: int | None = None) -> str:
        """读取产物（UTF-8）；``max_bytes`` 给定时按字节截断，用于上下文节选。

        截断可能劈开多字节字符，末尾的残字节以 ``U+FFFD`` 收尾 —— 节选只进提示词，
        不参与契约校验。
        """
        text = read_text(self.resolve(rel))
        if max_bytes is None:
            return text
        data = text.encode("utf-8")
        if len(data) <= max_bytes:
            return text
        return data[:max_bytes].decode("utf-8", errors="replace")

    def exists(self, rel: str) -> bool:
        """``root`` 下是否已有该文件；不安全路径照抛 ``UnsafePathError``。"""
        return self.resolve(rel).is_file()

    def records(self) -> list[ArtifactRecord]:
        """已落盘记录的副本，按写入顺序。"""
        return list(self._records)

    def by_task(self, task_id: str) -> list[ArtifactRecord]:
        """某个任务产出的记录（G1 用它对照契约）。"""
        return [r for r in self._records if r.produced_by_task == task_id]

    def discard_task(self, task_id: str, *, delete_files: bool = True) -> int:
        """丢弃某个任务的全部产物：删记录（必要时连文件），返回清除的记录数。

        供调度器在派发返工任务**之前**调用（Task 13 的 carry-forward）：本类没有
        删除 API 时，模型一次多产出的文件会一直挂在 :meth:`by_task` 上，此后每一次
        返工都产出正确的产物集，却仍被 G1 的「契约外产物」检查驳回 —— 任务烧完
        ``max_reworks`` 后 ``FAILED``，一个多余文件就把任务判死且无路可回。

        删除是安全的：``commit`` 的冲突规则已保证同一路径只属于一个任务，被丢弃
        的记录与文件不可能属于其他任务。

        **失败原子性的方向是「记录跟着文件走」**：先删文件，成功（或文件本就不
        存在）才删记录；删文件失败（如 Windows 上的占用/权限）时**保留**该记录。
        真正的危险状态是「文件已删、记录还在」—— G1 以记录为准，会拿着记录去
        :meth:`read` 一个不存在的文件并抛 ``FileNotFoundError``。这里保证任何
        时刻都不存在指向已删除文件的记录；删不掉的文件连同记录留在原地，由下一次
        返工派发再试。

        ``delete_files=False`` 只清记录、保留文件（记录驱动的检查即刻不再看到它们，
        文件留给人工处置）。
        """
        kept: list[ArtifactRecord] = []
        removed = 0
        for record in self._records:
            if record.produced_by_task != task_id:
                kept.append(record)
                continue
            if delete_files and not self._delete_file(record.path):
                kept.append(record)  # 文件删不掉：记录必须继续指向真实存在的文件
                continue
            removed += 1
        self._records = kept
        return removed

    def _delete_file(self, rel: str) -> bool:
        """删除一个产物文件；已不存在视为成功，删除失败返回 ``False``（不抛）。

        路径来自本类写入的记录，构造时已过 :func:`safe_relpath`，因此
        :meth:`resolve` 的校验不会失败。
        """
        try:
            self.resolve(rel).unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def write_manifest(self) -> Path:
        """把当前记录原子写成 ``root/MANIFEST.json``，返回清单路径。"""
        manifest = Manifest(
            run_id=self._run_id,
            generated_at=datetime.now(),
            entries=list(self._records),
        )
        payload = json.dumps(
            manifest.model_dump(mode="json"), ensure_ascii=False, indent=2
        )
        path = self._root / "MANIFEST.json"
        atomic_write_text(path, payload + "\n")
        return path

    def load_manifest(self, path: Path | None = None) -> int:
        """从 ``MANIFEST.json`` 重建内存记录，返回载入条数（resume 用）。

        ``path`` 缺省为 ``root/MANIFEST.json``。文件不存在返回 0：封版之前被中断
        的 run 本就没有已确认的清单（``write_manifest`` 只在 run 收尾时调用）。

        为什么必须有这个入口：进程重启后内存记录是空的，而**盘上的文件还在**。
        冲突检测（``commit`` 的跨任务同路径拒绝）与 ``discard_task``（返工前清理
        旧产物）都以记录为准 —— 不重建记录，spec §9.2 的跨 resume 保护会静默
        失效：另一个任务可以覆盖别人的产物，返工也清不掉自己上次多写的文件。

        记录**整体替换**（幂等），不合并：清单是磁盘产物的权威快照，合并会留下
        指向已删除文件的幽灵记录（``discard_task`` 正是删文件又删记录的）。
        ``run_id`` 不一致的清单直接报错 —— 载入他次 run 的记录比不载入更危险。
        """
        path = self._root / "MANIFEST.json" if path is None else Path(path)
        if not path.is_file():
            return 0
        manifest = Manifest.model_validate(json.loads(read_text(path)))
        if manifest.run_id != self._run_id:
            raise ValueError(
                f"MANIFEST.run_id={manifest.run_id!r} 与本次 run_id="
                f"{self._run_id!r} 不一致，拒绝载入：{path}"
            )
        self._records = list(manifest.entries)
        return len(self._records)

    def file_tree(self) -> list[str]:
        """``root`` 下全部文件的 POSIX 相对路径（截断惯例见 ``files.tree``）。"""
        return tree(self._root)
