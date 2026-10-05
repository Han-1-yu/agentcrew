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
        5. 编码后字节数对 ``spec.max_bytes``/``spec.min_bytes`` 校验；
        6. ``mkdir(parents=True, exist_ok=True)`` 后原子写；
        7. 更新内存记录并（有 ledger 时）发 ``artifact_written`` 事件。
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
        if len(data) < spec.min_bytes:
            raise ValueError(
                f"产物 {normalized} 大小 {len(data)} 字节低于下限 {spec.min_bytes} 字节"
            )

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

    def file_tree(self) -> list[str]:
        """``root`` 下全部文件的 POSIX 相对路径（截断惯例见 ``files.tree``）。"""
        return tree(self._root)
