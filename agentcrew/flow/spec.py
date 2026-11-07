"""R26：可视化编排的流程规格——YAML 双向、图校验、画布与引擎的同一份事实源。

设计要点：

1. **复用规划草案模型**：``tasks`` 直接用 :class:`~agentcrew.task.planner.TaskDraft`，
   与 PM 结构化输出同一套字段与边界（priority 1..9、deliverables 契约等）。
   画布导出的 YAML 因此天然与引擎兼容，不需要第二份 schema。
2. **图校验只报可读理由**：唯一 id、依赖存在、无环（Kahn）、至少一个任务。
   与门禁的「给出返工理由」一致——错误是给人看的，不是给栈看的。
3. **落盘独立于 run**：``<output>/_flows/<name>.yaml``，不进 run 目录、不碰
   MANIFEST；``clean`` 只扫 run 目录，不会误删。
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.task.planner import TaskDraft

#: 流程文件版本（将来结构变更时用它做迁移判断）。
FLOW_VERSION = 1

#: 流程目录名（相对 output_dir）。
FLOWS_DIR_NAME = "_flows"

#: 流程名允许的字符：ASCII 字母数字开头，后接字母数字/点/下划线/连字符。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class FlowSpec(BaseModel):
    """一份可编排的流程：任务节点集合（依赖即连线）。"""

    model_config = ConfigDict(extra="forbid")

    version: int = FLOW_VERSION
    name: str = "untitled"
    description: str = ""
    tasks: list[TaskDraft] = Field(default_factory=list)


class FlowError(ValueError):
    """流程文件读取 / 解析 / 命名不合法。"""


def parse_flow_yaml(text: str) -> FlowSpec:
    """YAML 文本 → ``FlowSpec``；结构错误转成可读 :class:`FlowError`。"""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FlowError(f"YAML 解析失败：{_short_yaml_error(exc)}") from exc
    if raw is None:
        raise FlowError("流程为空：至少要有一个任务节点")
    if not isinstance(raw, dict):
        raise FlowError("流程顶层必须是映射（version / name / tasks）")
    try:
        spec = FlowSpec(**raw)
    except ValidationError as exc:
        raise FlowError(f"流程结构不合法：{_short_pydantic_error(exc)}") from exc
    if spec.version != FLOW_VERSION:
        raise FlowError(
            f"不支持的流程版本：{spec.version}（当前 {FLOW_VERSION}）")
    return spec


def dump_flow_yaml(spec: FlowSpec) -> str:
    """``FlowSpec`` → YAML 文本（稳定排序、保留中文、不折行）。"""
    payload = spec.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=1000)


def _short_yaml_error(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc).strip().split("\n")[0]
    if mark is None:
        return str(problem)
    return f"line {mark.line + 1} column {mark.column + 1}: {problem}"


def _short_pydantic_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for item in exc.errors()[:5]:
        location = ".".join(str(part) for part in item.get("loc", ()))
        parts.append(f"{location or '<root>'}：{item.get('msg', '')}")
    if len(exc.errors()) > 5:
        parts.append(f"… 共 {len(exc.errors())} 处")
    return "；".join(parts)


def validate_flow(spec: FlowSpec) -> list[str]:
    """图级校验：返回中文错误列表（空列表 = 合法）。"""
    errors: list[str] = []
    if not spec.tasks:
        return ["流程里至少要有一个任务节点"]
    ids: list[str] = []
    seen: set[str] = set()
    for task in spec.tasks:
        if task.id in seen:
            errors.append(f"任务 id 重复：{task.id!r}")
        seen.add(task.id)
        ids.append(task.id)
    for task in spec.tasks:
        for dependency in task.dependencies:
            if dependency not in seen:
                errors.append(
                    f"任务 {task.id!r} 的依赖 {dependency!r} 不存在")
            if dependency == task.id:
                errors.append(f"任务 {task.id!r} 依赖了自己")
    cycle = _find_cycle(spec)
    if cycle:
        errors.append("依赖成环：" + " → ".join(cycle))
    if not errors and not any(not task.dependencies for task in spec.tasks):
        errors.append("流程没有入口任务（每个节点都声明了依赖）")
    return errors


def _find_cycle(spec: FlowSpec) -> list[str] | None:
    """Kahn 拓扑；有环时返回环上的一条路径（便于人直接看）。"""
    indegree = {task.id: 0 for task in spec.tasks}
    children: dict[str, list[str]] = {task.id: [] for task in spec.tasks}
    for task in spec.tasks:
        for dependency in task.dependencies:
            if dependency in indegree:
                indegree[task.id] += 1
                children[dependency].append(task.id)
    queue = deque(sorted(tid for tid, degree in indegree.items() if degree == 0))
    visited: list[str] = []
    while queue:
        current = queue.popleft()
        visited.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(visited) == len(indegree):
        return None
    remaining = {tid for tid, degree in indegree.items() if degree > 0}
    # 从剩余节点沿子边找回环
    path: list[str] = []
    node = sorted(remaining)[0]
    while node not in path:
        path.append(node)
        node = next((c for c in children[node] if c in remaining), node)
    return path[path.index(node):] + [node]


def topological_order(spec: FlowSpec) -> list[str]:
    """拓扑序（同层按 priority 再按 id，稳定可复现）。"""
    priority = {task.id: task.priority for task in spec.tasks}
    indegree = {task.id: 0 for task in spec.tasks}
    children: dict[str, list[str]] = {task.id: [] for task in spec.tasks}
    for task in spec.tasks:
        for dependency in task.dependencies:
            if dependency in indegree:
                indegree[task.id] += 1
                children[dependency].append(task.id)
    ready = sorted((tid for tid, degree in indegree.items() if degree == 0),
                   key=lambda tid: (priority.get(tid, 5), tid))
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=lambda tid: (priority.get(tid, 5), tid))
    return order


def graph_view(spec: FlowSpec) -> dict[str, Any]:
    """给画布 / 前端用的图视图：节点、边、拓扑序、错误。"""
    errors = validate_flow(spec)
    return {
        "name": spec.name,
        "version": spec.version,
        "description": spec.description,
        "nodes": [
            {
                "id": task.id, "title": task.title, "role": task.role,
                "priority": task.priority, "description": task.description,
                "acceptance_criteria": task.acceptance_criteria,
                "deliverables": [item.model_dump(mode="json")
                                 for item in task.deliverables],
                "dependencies": task.dependencies,
                "when_outcomes": task.when_outcomes,
                "brief": task.brief,
                "timeout_seconds": task.timeout_seconds,
            }
            for task in spec.tasks
        ],
        "edges": [
            {"from": dependency, "to": task.id,
             "condition": list(task.when_outcomes)}
            for task in spec.tasks for dependency in task.dependencies
        ],
        "order": topological_order(spec) if not errors else [],
        "errors": errors,
    }


# ── 落盘 ────────────────────────────────────────────────────────────────


def flows_dir(output_dir: Path) -> Path:
    return Path(output_dir) / FLOWS_DIR_NAME


def _validated_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise FlowError(
            "流程名只能用字母数字开头、后接字母数字/点/下划线/连字符（≤64 字符）")
    return name


def save_flow(output_dir: Path, spec: FlowSpec) -> Path:
    """保存流程；**先校验再落盘**（坏流程不写进目录，避免污染列表）。"""
    name = _validated_name(spec.name)
    errors = validate_flow(spec)
    if errors:
        raise FlowError("流程不合法：" + "；".join(errors))
    target = flows_dir(output_dir) / f"{name}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_flow_yaml(spec), encoding="utf-8")
    return target


def load_flow(output_dir: Path, name: str) -> FlowSpec:
    target = flows_dir(output_dir) / f"{_validated_name(name)}.yaml"
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise FlowError(f"流程不存在：{name}") from exc
    return parse_flow_yaml(text)


def list_flows(output_dir: Path) -> list[dict[str, Any]]:
    """列出已保存流程（名字、任务数、错误数、修改时间）。"""
    directory = flows_dir(output_dir)
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        entry: dict[str, Any] = {"name": path.stem, "tasks": 0, "errors": 0}
        try:
            spec = parse_flow_yaml(path.read_text(encoding="utf-8"))
            entry["tasks"] = len(spec.tasks)
            entry["errors"] = len(validate_flow(spec))
            entry["flow_name"] = spec.name
        except (OSError, FlowError) as exc:
            entry["errors"] = 1
            entry["error"] = str(exc)
        try:
            entry["updated_at"] = path.stat().st_mtime
        except OSError:
            entry["updated_at"] = 0
        items.append(entry)
    return items


def delete_flow(output_dir: Path, name: str) -> None:
    target = flows_dir(output_dir) / f"{_validated_name(name)}.yaml"
    try:
        target.unlink()
    except OSError as exc:
        raise FlowError(f"流程不存在：{name}") from exc


def sample_flow() -> FlowSpec:
    """画布「新建」用的示例流程（三节点 + 一条条件边）。"""
    return FlowSpec(name="sample", description="示例：需求 → 实现 → 交付",
                    tasks=[
        TaskDraft(
            id="T-001", title="需求文档", role="project_manager",
            description="输出 PRD，含验收标准",
            acceptance_criteria=["含范围内/外", "可验收"],
            deliverables=[ArtifactSpec(path="docs/PRD.md", kind="markdown",
                                      description="PRD",
                                      must_contain=["验收"])],
            priority=9, brief="先定范围",
        ),
        TaskDraft(
            id="T-002", title="后端实现", role="backend_dev",
            description="按 PRD 实现接口",
            acceptance_criteria=["接口可调用"],
            deliverables=[ArtifactSpec(path="backend/app/main.py", kind="python",
                                      description="服务入口",
                                      must_contain=["def "])],
            dependencies=["T-001"], priority=6,
        ),
        TaskDraft(
            id="T-003", title="交付说明", role="tech_writer",
            description="写 DELIVERY.md",
            acceptance_criteria=["含启动方式"],
            deliverables=[ArtifactSpec(path="DELIVERY.md", kind="markdown",
                                      description="交付说明",
                                      must_contain=["运行"])],
            dependencies=["T-001", "T-002"], priority=4,
            when_outcomes=["ready"],
        ),
    ])
