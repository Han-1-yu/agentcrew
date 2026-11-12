"""全链路追踪上下文（contextvars 实现；零依赖，任何分层都可安全 import）。

审计（2026-11）发现的观测断点：``llm_call`` 事件只有 ``role`` 级弱关联——同一
角色并发跑多个任务时，token / 成本 / 延迟无法归因到具体任务。本模块用
:mod:`contextvars` 提供一个随协程绑定的轻量追踪上下文：

- **写入点**：:meth:`BaseAgent.execute` 进入时 :func:`set_trace`，退出时
  :func:`reset_trace`（finally 保证）——协程内的所有 LLM 调用、工具调用天然
  携带任务标识；
- **读取点**：:class:`~agentcrew.models.factory.ModelFactory` 的 ``llm_call``
  记账路径经 :func:`current_trace` 读取，把 ``task_id`` / ``attempt`` 补进事件；
- **隔离性**：contextvar 按协程（task）隔离，并发任务互不串扰；未设置时
  :func:`current_trace` 返回 ``None``，工厂行为与改造前完全一致（向后兼容）。

设计约束：纯内存、零 IO、零锁——它只是「当前协程正在为哪个任务工作」的声明，
不是分布式追踪系统；跨进程/跨 run 的关联仍由 run_id 承担。
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class TraceContext:
    """当前协程正在执行的任务标识（只读快照）。

    ``attempt`` 取自 ``task.attempts``，与 ``task_started`` / ``task_finished``
    事件同源同义，便于把一次 LLM 调用精确对齐到某一次执行尝试。

    ``profile`` 是**执行体**（``AgentProfile.key``）标识。角色可以绑多个执行体、
    各自走不同模型，此时 ``role`` 只能回答「哪个岗位在花钱」，``profile`` 才能
    回答「哪个模型档在花钱」——两者缺一不可，否则多执行体的成本无法归因。
    单执行体角色下它在语义上等于 ``default``，事件里仍只在有值时出现。
    """

    task_id: str | None = None
    role: str | None = None
    attempt: int | None = None
    profile: str | None = None


_trace_var: contextvars.ContextVar[TraceContext | None] = (
    contextvars.ContextVar("agentcrew_trace", default=None))


def current_trace() -> TraceContext | None:
    """返回当前协程的追踪上下文；未设置时返回 ``None``。"""
    return _trace_var.get()


def set_trace(trace: TraceContext) -> contextvars.Token[TraceContext | None]:
    """设置追踪上下文并返回 token（配对 :func:`reset_trace` 使用）。"""
    return _trace_var.set(trace)


def reset_trace(token: contextvars.Token[TraceContext | None]) -> None:
    """恢复到 :func:`set_trace` 之前的状态（``finally`` 中调用）。"""
    _trace_var.reset(token)
