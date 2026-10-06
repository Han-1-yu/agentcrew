"""``diff`` 子命令：对比两个 run 的配置 / 任务 / 产物 / 事件统计差异。

设计要点：

- **只读**：本命令不写任何 run 目录，只从 ``run.json`` / ``tasks.json`` /
  ``artifacts/`` / ``ledger.jsonl`` 读事实。run 不存在是调用方（``main.py``）的
  报错路径，本模块假定两个 :class:`RunRecord` 与两个 :class:`CheckpointStore`
  都已成功加载。
- **只报告差异**：每一段（config / task / artifact / event / exit）都只在「两侧
  真的不同」时填字段；完全一致的 run 产出一份全空的 diff —— 测试据此断言
  「相同 run 无差异」。
- **信息性命令**：diff 本身永远退出 0（哪怕差异巨大）；run 不存在由 CLI 层退 1。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentcrew.runtime.checkpoint import CheckpointStore, RunRecord
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task

#: 参与配置对比的关键字段（config_snapshot 里的键）。
#: 不含 output_dir / log_level 这类「运行环境」字段：它们变了不影响产物含义，
#: 比进去只会让 diff 噪声盖过信号。
_CONFIG_KEYS: tuple[str, ...] = (
    "default_model",
    "context_budget_tokens",
    "max_concurrency",
    "gate",
    "mock",
    "max_cost_usd",
    "max_llm_calls",
    "role_bindings",
    "model_tiers",
    "gate_config",
)

#: 参与退出/状态对比的统计字段（``RunStats`` 的标量字段）。
_STAT_FIELDS: tuple[str, ...] = (
    "total_tasks",
    "succeeded",
    "failed",
    "blocked",
    "skipped",
    "llm_calls",
    "prompt_tokens",
    "completion_tokens",
    "estimated_cost_usd",
)


class RunDiff(BaseModel):
    """两个 run 的结构化差异；各段仅在不同时填，相同段为空 dict。"""

    model_config = ConfigDict(extra="forbid")

    run_a: str
    run_b: str
    config_diff: dict[str, Any]  # 字段名 → [a值, b值]
    task_diff: dict[str, Any]  # 数量 / 状态分布 / 新增删除任务
    artifact_diff: dict[str, Any]  # added / removed / modified
    event_diff: dict[str, Any]  # 事件计数 / LLM 用量差异
    exit_diff: dict[str, Any]  # 状态 / 统计差异


# ── 各段差异计算（纯函数）─────────────────────────────────────────────────


def _config_diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """config_snapshot 里关键字段的逐字段差异。"""
    out: dict[str, Any] = {}
    for key in _CONFIG_KEYS:
        va = a.get(key)
        vb = b.get(key)
        if va != vb:
            out[key] = [va, vb]
    return out


def _status_distribution(tasks: dict[str, Task]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for task in tasks.values():
        status = task.status.value
        dist[status] = dist.get(status, 0) + 1
    return dist


def _task_diff(
    tasks_a: dict[str, Task], tasks_b: dict[str, Task]
) -> dict[str, Any]:
    """任务数量 / 状态分布 / 任务集合差异。"""
    ids_a = set(tasks_a)
    ids_b = set(tasks_b)
    added = sorted(ids_b - ids_a)
    removed = sorted(ids_a - ids_b)
    da = _status_distribution(tasks_a)
    db = _status_distribution(tasks_b)
    status_diff: dict[str, list[int]] = {}
    for status in set(da) | set(db):
        ca, cb = da.get(status, 0), db.get(status, 0)
        if ca != cb:
            status_diff[status] = [ca, cb]
    out: dict[str, Any] = {}
    if len(tasks_a) != len(tasks_b):
        out["count"] = [len(tasks_a), len(tasks_b)]
    if added:
        out["added"] = added
    if removed:
        out["removed"] = removed
    if status_diff:
        out["status_distribution"] = status_diff
    return out


def _artifact_index(artifacts_dir: Path) -> dict[str, int]:
    """产物相对路径 → 字节数；目录不存在视为空。"""
    out: dict[str, int] = {}
    if not artifacts_dir.is_dir():
        return out
    for path in artifacts_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(artifacts_dir).as_posix()
        try:
            out[rel] = path.stat().st_size
        except OSError:
            out[rel] = -1
    return out


def _artifact_index_diff(
    a: dict[str, int], b: dict[str, int]
) -> dict[str, Any]:
    """按路径与大小比对：added（仅 b 有）/ removed（仅 a 有）/ modified（大小变）。"""
    added = [
        {"path": path, "size": b[path]} for path in sorted(set(b) - set(a))
    ]
    removed = [
        {"path": path, "size": a[path]} for path in sorted(set(a) - set(b))
    ]
    modified = [
        {"path": path, "size_a": a[path], "size_b": b[path]}
        for path in sorted(set(a) & set(b))
        if a[path] != b[path]
    ]
    out: dict[str, Any] = {}
    if added:
        out["added"] = added
    if removed:
        out["removed"] = removed
    if modified:
        out["modified"] = modified
    return out


def _event_stats(run_dir: Path, run_id: str) -> dict[str, Any]:
    """从 ledger.jsonl 聚合：各事件计数、llm_call 次数与成本。"""
    ledger = Ledger(run_dir / "ledger.jsonl", run_id=run_id)
    counts: dict[str, int] = {}
    llm_calls = 0
    cost = 0.0
    for event in ledger.events():
        counts[event.event] = counts.get(event.event, 0) + 1
        if event.event == "llm_call":
            llm_calls += 1
            raw = event.data.get("estimated_cost_usd", 0)
            try:
                cost += float(raw or 0)
            except (TypeError, ValueError):
                pass
    return {"counts": counts, "llm_calls": llm_calls, "cost": round(cost, 6)}


def _event_diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """事件计数差异 + LLM 用量聚合差异。"""
    out: dict[str, Any] = {}
    counts_a, counts_b = a["counts"], b["counts"]
    per_event: dict[str, list[int]] = {}
    for name in set(counts_a) | set(counts_b):
        ca, cb = counts_a.get(name, 0), counts_b.get(name, 0)
        if ca != cb:
            per_event[name] = [ca, cb]
    if per_event:
        out["event_counts"] = per_event
    if a["llm_calls"] != b["llm_calls"]:
        out["llm_calls"] = [a["llm_calls"], b["llm_calls"]]
    if a["cost"] != b["cost"]:
        out["cost_usd"] = [a["cost"], b["cost"]]
    return out


def _exit_diff(a: RunRecord, b: RunRecord) -> dict[str, Any]:
    """最终状态与汇总统计差异。"""
    out: dict[str, Any] = {}
    if a.status != b.status:
        out["status"] = [a.status.value, b.status.value]
    stat_diff: dict[str, list[Any]] = {}
    for field in _STAT_FIELDS:
        va = getattr(a.stats, field, None)
        vb = getattr(b.stats, field, None)
        if va != vb:
            stat_diff[field] = [va, vb]
    if stat_diff:
        out["stats"] = stat_diff
    return out


# ── 入口 ───────────────────────────────────────────────────────────────────


def diff_runs(
    run_a: RunRecord,
    run_b: RunRecord,
    store_a: CheckpointStore,
    store_b: CheckpointStore,
) -> RunDiff:
    """对比两个 run，返回结构化差异。

    ``tasks.json`` 读不到（规划就失败的 run）按空板处理，不抛 —— 与
    :meth:`CheckpointStore.load_with_deltas` 的宽容语义一致。
    """
    config_diff = _config_diff(run_a.config_snapshot, run_b.config_snapshot)

    try:
        tasks_a = store_a.load_with_deltas()
    except (OSError, ValueError):
        tasks_a = {}
    try:
        tasks_b = store_b.load_with_deltas()
    except (OSError, ValueError):
        tasks_b = {}
    task_diff = _task_diff(tasks_a, tasks_b)

    artifact_diff = _artifact_index_diff(
        _artifact_index(store_a.artifacts_dir),
        _artifact_index(store_b.artifacts_dir),
    )

    event_diff = _event_diff(
        _event_stats(store_a.run_dir, run_a.run_id),
        _event_stats(store_b.run_dir, run_b.run_id),
    )

    exit_diff = _exit_diff(run_a, run_b)

    return RunDiff(
        run_a=run_a.run_id,
        run_b=run_b.run_id,
        config_diff=config_diff,
        task_diff=task_diff,
        artifact_diff=artifact_diff,
        event_diff=event_diff,
        exit_diff=exit_diff,
    )


def print_diff(diff: RunDiff) -> None:
    """把差异格式化打印到终端（``+`` 仅 b 有 / ``-`` 仅 a 有 / ``~`` 修改）。"""
    print(f"━━━ diff: {diff.run_a} → {diff.run_b} ━━━")
    identical = not any(
        [diff.config_diff, diff.task_diff, diff.artifact_diff,
         diff.event_diff, diff.exit_diff]
    )
    if identical:
        print("（两个 run 完全一致，无差异）")
        return

    if diff.config_diff:
        print("配置差异:")
        for field, values in diff.config_diff.items():
            print(f"  {field}: {values[0]!r} → {values[1]!r}")

    if diff.task_diff:
        print("任务差异:")
        if "count" in diff.task_diff:
            ca, cb = diff.task_diff["count"]
            print(f"  任务数: {ca} → {cb}")
        for task_id in diff.task_diff.get("added", []):
            print(f"  + {task_id}")
        for task_id in diff.task_diff.get("removed", []):
            print(f"  - {task_id}")
        for status, values in diff.task_diff.get("status_distribution", {}).items():
            print(f"  状态[{status}]: {values[0]} → {values[1]}")

    if diff.artifact_diff:
        print("产物差异:")
        for item in diff.artifact_diff.get("added", []):
            print(f"  + {item['path']}（{item['size']} bytes）")
        for item in diff.artifact_diff.get("removed", []):
            print(f"  - {item['path']}（{item['size']} bytes）")
        for item in diff.artifact_diff.get("modified", []):
            print(f"  ~ {item['path']}（{item['size_a']} → {item['size_b']} bytes）")

    if diff.event_diff:
        print("事件统计差异:")
        for name, values in diff.event_diff.get("event_counts", {}).items():
            print(f"  {name}: {values[0]} → {values[1]}")
        if "llm_calls" in diff.event_diff:
            ca, cb = diff.event_diff["llm_calls"]
            print(f"  llm_call 合计: {ca} → {cb}")
        if "cost_usd" in diff.event_diff:
            ca, cb = diff.event_diff["cost_usd"]
            print(f"  成本 USD: {ca} → {cb}")

    if diff.exit_diff:
        print("退出/状态差异:")
        if "status" in diff.exit_diff:
            sa, sb = diff.exit_diff["status"]
            print(f"  状态: {sa} → {sb}")
        for field, values in diff.exit_diff.get("stats", {}).items():
            print(f"  {field}: {values[0]} → {values[1]}")
