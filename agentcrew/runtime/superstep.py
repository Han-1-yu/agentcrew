"""P2-2：super-step 标记 + pending tool writes（崩溃可恢复的「半步状态」）。

**问题**：现有 checkpoint 只在「事件落地后」写（``tasks.json`` / ``run.json`` /
``tasks.delta.jsonl``）。进程在两种时刻被强杀，就会留下**没有任何记录**的半步行
——「一批任务已派发、还没收尾」与「工具已开始执行、结果还没回灌」。前者由 R17 的
``run_reconciled`` 兜住（RUNNING → INTERRUPTED 后 resume 重派），后者**完全不可
见**：重跑一次任务时没人知道上一次已经执行过 ``write_file`` / ``run_python``，
排查时只能猜。

**做法**（只借语义，不引外部 runtime）：

1. ``superstep.json``：每轮调度开始写一条「我在第 N 轮、在跑哪些任务」，本轮结束
   改写为 ``idle``。原子写（临时文件 + ``os.replace``），任何时刻只有「旧的完整」
   或「新的完整」。
2. **pending writes 从 ledger 推导**，不另建一套状态：工具调用前记
   ``tool_call_started``，执行完记 ``tool_call``（既有事件，新增 ``call_id`` /
   ``fence_index`` 两个关联字段）。每次启动/续跑扫一遍 ledger，**有 started 没有
   配对的 finished** 就是被中断的那次工具调用。

为什么从 ledger 推导而不是单独存：ledger 已经是「写入严、读取宽」的追加日志
（逐行 flush + fsync、崩溃截断的尾行读取端容忍），再存一份 pending 状态就多了
一个可能与 ledger 不一致的事实源。工具调用必须留痕这一点既有实现已经保证，
本模块只是把「已经留下的一半」读出来。

**不做什么**：不做工具结果的**重放**（把上次的结果喂回去跳过执行）。重放要求
工具幂等，而 ``write_file`` / ``run_python`` 的副作用不可回滚；宁可如实标注
「这次调用被中断，任务将重跑」，也不假装能无缝续上。方案 §4.2 的结论一致：
先本地 SQLite/JSONL 达成 pending 可见与恢复，不引外部 durable 服务。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..utils.files import atomic_write_text, read_text
from .ledger import Ledger

#: super-step 状态文件名（run 目录下，与 ``run.json`` 同级）。
SUPERSTEP_FILE = "superstep.json"
SUPERSTEP_SCHEMA_VERSION = 1

#: 一次恢复最多汇报多少条被中断的工具调用（防止病态 ledger 把事件撑爆）。
MAX_PENDING_REPORTED = 20


class SuperStepState(BaseModel):
    """``superstep.json`` 的内容：本轮调度的半步状态。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SUPERSTEP_SCHEMA_VERSION
    index: int = 0
    """第几轮（从 1 开始；0 = 还没开始调度）。"""

    phase: str = "idle"
    """``dispatch``（正在派发/等待本批收尾）或 ``idle``（没有在飞的任务）。"""

    running_tasks: list[str] = Field(default_factory=list)
    """本轮开始时处于派发在跑的 task id（崩溃点最可能落在它们中间）。"""

    started_at: str = ""
    updated_at: str = ""


class PendingToolCall(BaseModel):
    """一条「开始了但没结束」的工具调用（从 ledger 推导出来的 pending write）。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = ""
    role: str = ""
    tool_name: str = ""
    call_id: str = ""
    started_at: str = ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _pending_key(data: dict[str, Any]) -> tuple[str, str]:
    """tool_call_started 与 tool_call 的配对键。

    优先用 Provider 给的 ``call_id``（原生 function calling 一定有）；围栏路径
    没有 id，退到 ``task_id + fence_index``（同一批里的第几个调用）；老事件两者
    都没有时退到 ``task_id + tool_name``——同一任务同一工具连调两次会误配成一条，
    这种情况只会让 pending **少数**，不会凭空多报。
    """
    task_id = str(data.get("task_id") or "")
    call_id = str(data.get("call_id") or "")
    if call_id:
        return task_id, f"id:{call_id}"
    if data.get("fence_index") is not None:
        return task_id, f"fence:{data.get('fence_index')}"
    return task_id, f"tool:{data.get('tool_name') or ''}"


class SuperStepJournal:
    """绑定到一个 run 目录的 super-step 读写器（I/O 之外不含业务判断）。"""

    def __init__(self, run_dir: Path, ledger: Ledger | None = None) -> None:
        self.run_dir = Path(run_dir)
        self._ledger = ledger

    @property
    def path(self) -> Path:
        return self.run_dir / SUPERSTEP_FILE

    # ── 读写 ────────────────────────────────────────────────────────────

    def load(self) -> SuperStepState | None:
        """读当前状态；文件不存在 / 内容坏掉一律返回 ``None``（不抛异常）。

        崩溃可能把文件写成半截（虽然用了原子替换，但手工编辑 / 磁盘坏道仍在），
        这种情况下「当作没有状态」比让 resume 直接失败要好——恢复逻辑本身不该
        成为新的故障点。
        """
        try:
            raw = read_text(self.path)
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            return SuperStepState.model_validate_json(raw)
        except (ValueError, TypeError):
            return None

    def save(self, state: SuperStepState) -> None:
        """原子写（临时文件 + ``os.replace``）：任何时刻只有旧的或新的完整内容。"""
        state.updated_at = _now()
        atomic_write_text(
            self.path,
            state.model_dump_json(indent=2) + "\n",
        )

    def begin(self, *, running: Iterable[str] = ()) -> SuperStepState:
        """标记「第 N 轮开始」：index 自增，记下本轮在跑的任务。"""
        previous = self.load()
        state = SuperStepState(
            index=(previous.index + 1) if previous else 1,
            phase="dispatch",
            running_tasks=[str(item) for item in running],
            started_at=_now(),
        )
        self.save(state)
        if self._ledger is not None:
            self._ledger.emit(
                "superstep_begin", index=state.index,
                running=list(state.running_tasks))
        return state

    def end(self) -> SuperStepState:
        """标记本轮结束（没有在飞的任务）；未开始过则是一个空 ``idle``。"""
        previous = self.load()
        state = SuperStepState(
            index=previous.index if previous else 0,
            phase="idle",
            running_tasks=[],
            started_at=previous.started_at if previous else "",
        )
        self.save(state)
        return state

    # ── pending writes ──────────────────────────────────────────────────

    def pending_tool_calls(self, *, limit: int = MAX_PENDING_REPORTED) -> list[PendingToolCall]:
        """扫 ledger 找「开始了但没结束」的工具调用（最旧优先，去重）。"""
        if self._ledger is None:
            return []
        started: dict[tuple[str, str], PendingToolCall] = {}
        finished: set[tuple[str, str]] = set()
        for event in self._ledger.events():
            if event.event == "tool_call_started":
                key = _pending_key(event.data)
                started.setdefault(key, PendingToolCall(
                    task_id=str(event.data.get("task_id") or ""),
                    role=str(event.data.get("role") or ""),
                    tool_name=str(event.data.get("tool_name") or ""),
                    call_id=str(event.data.get("call_id") or ""),
                    started_at=event.ts.isoformat(timespec="seconds"),
                ))
            elif event.event == "tool_call":
                finished.add(_pending_key(event.data))
        pending = [call for key, call in started.items() if key not in finished]
        return pending[:limit]

    def recover(self) -> dict[str, Any]:
        """给启动 / 续跑用的一页摘要：上一轮停在哪 + 哪些工具调用被打断。

        纯读取，不写文件、不发事件——调用方（Crew）决定怎么留痕与怎么处置。
        """
        state = self.load()
        pending = self.pending_tool_calls()
        return {
            "last_index": state.index if state else 0,
            "last_phase": state.phase if state else "unknown",
            "running_tasks": list(state.running_tasks) if state else [],
            "pending_tool_calls": [call.model_dump(mode="json") for call in pending],
        }

    def reset(self) -> None:
        """清空标记（``clean`` 场景外的显式重置；正常收尾不需要它）。"""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
