"""run 报告渲染：把 ``RunRecord`` + ``TaskBoard`` 变成 spec §14.3 的中文文本。

``Crew`` 负责**产生**这些事实（统计、返工、评审裁决、耗时），本模块负责**措辞**。
分开的两个实际好处：

1. 报告是可断言的纯函数 —— 给定面板与记录，输出逐字确定，不需要跑一次 run；
2. ``Crew`` 的 1200 行里少掉一整块与编排无关的字符串拼接，调度逻辑读起来连续。

同一个仓库里 ``runtime/board_view.py``（终端实时看板）与
``gate/cli_prompter.py``（人工卡点摘要）也各有自己的时长/token/评审措辞 ——
那三处**刻意不同形**（``1h03m`` vs ``1h02m03s``、不同的前缀与细节粒度），
面向不同读者，因此不做合并；本模块只服务「run 结束报告」这一种输出。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agentcrew.runtime.checkpoint import RunRecord
from agentcrew.task.task import RunStats, Task, TaskStatus
from agentcrew.task.task_board import TaskBoard

__all__ = [
    "delivery_summary",
    "elapsed_line",
    "failure_line",
    "format_duration",
    "format_tokens",
    "review_line",
    "rework_line",
]


def delivery_summary(record: RunRecord, *, board: TaskBoard, artifacts_dir: Path) -> str:
    """渲染 §14.3 的中文运行报告文本（任务统计/返工/用量/通过率/耗时/产物）。

    ``artifacts_dir`` 由调用方给出绝对路径：报告是操作者最后看到的东西，
    打印相对路径等于让他自己去猜工作目录。
    """
    tasks = board.all()
    stats = record.stats
    lines = [
        f"━━━ AgentCrew 运行报告 · {record.run_id} ━━━",
        f"任务      {stats.total_tasks} 个   成功 {stats.succeeded} · "
        f"失败 {stats.failed} · 阻塞 {stats.blocked} · 跳过 {stats.skipped}",
        f"返工      {rework_line(tasks)}",
        f"LLM 调用  {stats.llm_calls} 次   "
        f"prompt {format_tokens(stats.prompt_tokens)} tok · "
        f"completion {format_tokens(stats.completion_tokens)} tok",
        f"估算成本  ${stats.estimated_cost_usd:.2f}",
        f"评审通过率 {review_line(tasks)}",
        f"耗时      {elapsed_line(record)}",
        f"产物      {artifacts_dir}",
    ]
    failed = [
        task
        for task in tasks
        if task.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
    ]
    if failed:
        lines.append("━━━ 失败任务 ━━━")
        lines.extend(failure_line(task) for task in failed)
    return "\n".join(lines)


def format_tokens(count: int) -> str:
    """token 数（spec §14.3 的 ``812k`` 量级写法）；千位以下给原值。"""
    if count >= 10_000:
        return f"{count / 1000:.0f}k"
    return str(count)


def format_duration(seconds: float) -> str:
    """耗时：``4m12s`` / ``1h02m03s`` / ``7s``。"""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def rework_line(tasks: Sequence[Task]) -> str:
    """返工总次数与逐任务明细（``3 次（T-006 ×2, T-009 ×1）``）。"""
    total = sum(task.rework_count for task in tasks)
    reworked = [task for task in tasks if task.rework_count > 0]
    if not reworked:
        return f"{total} 次"
    detail = ", ".join(f"{task.id} ×{task.rework_count}" for task in reworked)
    return f"{total} 次（{detail}）"


def review_line(tasks: Sequence[Task]) -> str:
    """评审通过率与各层驳回数；没有评审记录时不假装 100%。"""
    reviews = [review for task in tasks for review in task.reviews]
    if not reviews:
        return "—（无评审记录）"
    rejected = [review for review in reviews if review.verdict == "reject"]
    rate = round(100 * (len(reviews) - len(rejected)) / len(reviews))
    g1 = sum(1 for review in rejected if review.layer == "static")
    g2 = sum(1 for review in rejected if review.layer == "llm")
    human = sum(1 for review in rejected if review.layer == "human")
    return f"{rate}%（G1 拦截 {g1} / G2 驳回 {g2} / 人工驳回 {human}）"


def elapsed_line(record: RunRecord) -> str:
    """耗时：起止都取 ``record.stats``，缺失时回落到记录的创建/更新时间。"""
    stats: RunStats = record.stats
    start = stats.started_at or record.created_at
    end = stats.finished_at or record.updated_at
    return format_duration((end - start).total_seconds())


def failure_line(task: Task) -> str:
    """一行失败任务：``T-011 标题  FAILED  最后一次评审：…``。"""
    last = task.reviews[-1] if task.reviews else None
    if last is None:
        detail = "（无评审记录）"
    elif last.issues:
        detail = "；".join(last.issues)
    else:
        detail = "（无具体问题）"
    return f"{task.id} {task.title}  {task.status.value.upper()}  最后一次评审：{detail}"
