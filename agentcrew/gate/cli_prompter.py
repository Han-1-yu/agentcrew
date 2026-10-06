"""交互式 G3 人工闸门（spec §10.3）：两个卡点的终端实现。

计划卡点：打印任务 DAG 表 + 角色分布，循环提示
``[a] 批准执行 / [e] 编辑后执行 / [s] 保存退出 / [q] 取消``（空输入默认批准）。
交付卡点：打印产物清单 + 统计 + 评审通过率，提示
``[a] 接受交付 / [r] 打回指定任务返工 / [q] 放弃``。

设计约束：

- **IO 全部可注入**。``input_fn`` / ``edit_fn`` / ``console`` 都是构造参数，
  测试用它们喂脚本答案，绝不碰真实 stdin；生产路径由 ``main.py`` 只在 TTY 上
  构造本类。
- **``e`` 的裁决动作是 ``edit``，不是 ``approve``**。``Crew._plan_phase`` 只在
  ``PlanDecision.action == "edit"`` 时用 ``decision.tasks`` 重建待执行面板
  （见 ``tests/test_crew.py::test_plan_gate_edit_uses_edited_tasks``）。返回
  ``approve`` 会把编辑结果**静默丢弃** —— 用户以为改过了，引擎照原计划执行。
- **校验失败不退出循环**。``edit_tasks_in_editor`` 把 JSON 解析错误、字段不合法、
  产物路径冲突统一收敛成 :class:`PlanValidationError`，``ask_plan`` 打印
  ``errors`` 后重新提示；一次手滑不该让整次 run 崩掉，也不该让用户重新再来。
- **交付正文从 ``tasks.json`` 尽力读回**（``RunRecord`` 只有统计）。读不到就只显示
  统计 —— 这是展示路径，绝不因为快照缺失/损坏而抛。
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from agentcrew.gate.gatekeeper import DeliveryDecision, PlanDecision
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.checkpoint import CheckpointStore, RunRecord
from agentcrew.task.planner import (
    FINAL_TASK_ID,
    PlanOutput,
    PlanValidationError,
    to_tasks,
    validate_plan,
    with_final_task,
)
from agentcrew.task.task import Task

#: 读一行输入的注入点（默认 ``input``；测试传脚本函数）。
InputFn = Callable[[str], str]

#: 编辑拆解的注入点：收到当前任务列表，返回编辑并校验后的列表。
EditFn = Callable[[Sequence[Task]], list[Task]]

_PLAN_MENU = "[a] 批准执行 / [e] 编辑后执行 / [s] 保存退出 / [q] 取消"
_PLAN_CHOICES = "a/e/s/q"
_DELIVERY_MENU = "[a] 接受交付 / [r] 打回指定任务返工 / [q] 放弃"
_DELIVERY_CHOICES = "a/r/q"


class CliPrompter:
    """G3 两个卡点的交互实现（``Prompter`` 协议的结构实现）。

    ``input_fn`` 缺省是内建 ``input``（TTY 交互），测试一律注入；
    ``edit_fn`` 缺省是 :func:`edit_tasks_in_editor`（开 ``$EDITOR``）。
    ``catalog`` 是本 run 的角色目录：缺省编辑路径用它校验，自定义角色因此
    不会被「角色未注册」误拒（不传则回落到内置八角色）。

    ``crew``（P1-6）注入后，两个卡点菜单多一项 ``[m] 切换模型``：计划/交付卡点
    上即可切默认模型或角色绑定。默认 ``None`` —— 老调用方（无 crew 上下文）
    行为完全不变，也不显示该项。
    """

    def __init__(
        self,
        *,
        input_fn: InputFn = input,
        edit_fn: EditFn | None = None,
        console: Console | None = None,
        catalog: RoleCatalog | None = None,
        crew: Any | None = None,
    ) -> None:
        self._input = input_fn
        self._edit: EditFn = (
            functools.partial(edit_tasks_in_editor, catalog=catalog)
            if edit_fn is None
            else edit_fn
        )
        self._console = Console() if console is None else console
        self._crew = crew

    # ── 卡点一：计划 ─────────────────────────────────────────────────────

    async def ask_plan(self, tasks: Sequence[Task]) -> PlanDecision:
        """展示拆解并询问是否执行；``e`` 走编辑器后重校验（失败则重新提示）。

        EOF（Ctrl-D / Ctrl-Z）等同 ``[q] 取消``：结束输入是普通操作，不该用 traceback
        结束整次 run —— 它走与选 ``q`` 完全相同的收尾路径。
        注入了 ``crew`` 时菜单多 ``[m] 切换模型``（P1-6）。
        """
        planned = list(tasks)
        self._print_plan(planned)
        while True:
            menu, choices = self._plan_menu()
            choice = self._choose(menu, choices, default="a", eof_label="取消")
            if choice == "a":
                return PlanDecision(action="approve", tasks=planned)
            if choice == "e":
                try:
                    edited = list(self._edit(planned))
                except PlanValidationError as exc:
                    self._print_errors(exc.errors)
                    continue
                return PlanDecision(action="edit", tasks=edited)
            if choice == "m":
                await self._switch_model()
                continue
            if choice == "s":
                return PlanDecision(action="save_exit")
            return PlanDecision(action="cancel")

    def _plan_menu(self) -> tuple[str, str]:
        """计划卡点菜单；注入了 crew 时多 ``[m] 切换模型``。"""
        if self._crew is not None:
            return (
                "[a] 批准执行 / [e] 编辑后执行 / [m] 切换模型 / [s] 保存退出 / [q] 取消",
                "a/e/m/s/q",
            )
        return (_PLAN_MENU, _PLAN_CHOICES)

    # ── 卡点二：交付 ─────────────────────────────────────────────────────

    async def ask_delivery(self, record: RunRecord | None) -> DeliveryDecision:
        """展示交付并询问是否接受；``r`` 收集任务 id，空列表重新提示。

        EOF 在任一提示处都等同 ``[q] 放弃``（含任务 id 子提示）。
        注入了 ``crew`` 时菜单多 ``[m] 切换模型``（P1-6）。
        """
        self._print_delivery(record)
        while True:
            menu, choices = self._delivery_menu()
            choice = self._choose(menu, choices, default="a", eof_label="放弃")
            if choice == "a":
                return DeliveryDecision(action="accept")
            if choice == "q":
                return DeliveryDecision(action="abort")
            if choice == "m":
                await self._switch_model()
                continue
            task_ids = self._ask_task_ids()
            if task_ids is None:
                return DeliveryDecision(action="abort")
            if task_ids:
                return DeliveryDecision(action="rework", task_ids=task_ids)
            self._console.print(Text("没有解析到任务 id，请重新选择。"), soft_wrap=True)

    def _delivery_menu(self) -> tuple[str, str]:
        """交付卡点菜单；注入了 crew 时多 ``[m] 切换模型``。"""
        if self._crew is not None:
            return (
                "[a] 接受交付 / [r] 打回指定任务返工 / [m] 切换模型 / [q] 放弃",
                "a/r/m/q",
            )
        return (_DELIVERY_MENU, _DELIVERY_CHOICES)

    async def _switch_model(self) -> None:
        """``[m] 切换模型``：展示当前绑定 → 问新别名（+可选角色）→ 调 crew。

        任何一步 EOF / 空输入都安全退回菜单，不影响 run 主流程。
        """
        crew = self._crew
        if crew is None:
            return
        try:
            bindings = crew.current_model_bindings()
        except RuntimeError as exc:
            self._console.print(Text(f"当前还读不到模型绑定：{exc}"), soft_wrap=True)
            return
        self._console.print(
            Text(f"当前默认模型：{bindings['default']}"), soft_wrap=True)
        for role, alias in bindings.get("role_bindings", {}).items():
            self._console.print(
                Text(f"  角色 {role} → {alias}"), soft_wrap=True)
        try:
            alias = self._input("新模型别名（如 openai:gpt-4o-mini，留空取消）> ").strip()
        except EOFError:
            return
        if not alias:
            return
        try:
            role_raw = self._input("角色绑定（留空=切换默认模型）> ").strip()
        except EOFError:
            role_raw = ""
        try:
            result = crew.switch_model(alias, role=role_raw or None)
        except Exception as exc:
            self._console.print(Text(f"切换失败：{exc}"), soft_wrap=True)
            return
        self._console.print(
            Text(f"已切换：默认 {result['default']}；"
                 f"角色绑定 {result['role_bindings']}"),
            soft_wrap=True)

    # ── 提示 ─────────────────────────────────────────────────────────────

    def _choose(self, menu: str, choices: str, *, default: str, eof_label: str) -> str:
        """循环提示直到拿到 ``choices`` 中的选项；空输入取 ``default``，EOF 取 ``q``。

        ``eof_label`` 只用于把「按哪个选项处理」说清楚（计划卡点是取消、交付卡点是
        放弃）——``q`` 在两个菜单里都是收尾项，所以返回同一个字符。
        """
        while True:
            try:
                raw = self._input(f"{menu} > ").strip().lower()
            except EOFError:
                self._console.print(
                    Text(f"输入已结束（Ctrl-D），按「{eof_label}」处理。"), soft_wrap=True)
                return "q"
            if not raw:
                return default
            if raw in choices:
                return raw
            self._console.print(
                Text(f"请输入 {'/'.join(choices)} 之一。"), soft_wrap=True)

    def _ask_task_ids(self) -> list[str] | None:
        """读一行逗号分隔的任务 id；去空白、丢空段；EOF 返回 ``None``（按放弃收尾）。"""
        try:
            raw = self._input("要返工的任务 id（逗号分隔）> ")
        except EOFError:
            self._console.print(
                Text("输入已结束（Ctrl-D），按「放弃」处理。"), soft_wrap=True)
            return None
        return [part.strip() for part in raw.split(",") if part.strip()]

    def _print_errors(self, errors: Sequence[str]) -> None:
        """打印编辑结果的校验错误（每行一条），供用户改完再来。"""
        self._console.print(Text("编辑后的拆解未通过校验："), soft_wrap=True)
        for error in errors:
            self._console.print(Text(f"  - {error}"), soft_wrap=True)

    def _print_plan(self, tasks: Sequence[Task]) -> None:
        """任务 DAG 表（id / 标题 / 角色 / 依赖 / 产物数 / 优先级）+ 角色分布。

        每列 ``overflow="fold"``：窄终端里 rich 的默认 ``ellipsis`` 会把放不下的
        单元格截成 ``T-…``，而人正要照着这张表批准执行 —— 折行可以，丢内容不行。
        """
        table = Table(
            box=box.SIMPLE_HEAD, header_style="bold", title="任务拆解 · G3 计划卡点")
        for title in ("ID", "标题", "角色", "依赖", "产物数", "优先级"):
            table.add_column(title, overflow="fold")
        for task in tasks:
            table.add_row(
                Text(task.id),
                Text(task.title),
                Text(task.role),
                Text("、".join(task.dependencies) or "—"),
                Text(str(len(task.deliverables))),
                Text(str(task.priority)),
            )
        self._console.print(table)
        self._console.print(Text(_role_line(tasks)), soft_wrap=True)

    def _print_delivery(self, record: RunRecord | None) -> None:
        """产物清单 + 统计 + 评审通过率（快照读不到时只显示 run 统计）。"""
        if record is not None:
            stats = record.stats
            self._console.print(
                Text(
                    f"任务 {stats.total_tasks} 个   成功 {stats.succeeded} · "
                    f"失败 {stats.failed} · 阻塞 {stats.blocked} · "
                    f"跳过 {stats.skipped}"
                ),
                soft_wrap=True,
            )
            self._console.print(
                Text(
                    f"LLM 调用 {stats.llm_calls} 次   "
                    f"估算成本 ${stats.estimated_cost_usd:.2f}"
                ),
                soft_wrap=True,
            )
        tasks = _load_tasks(record)
        if tasks is None:
            self._console.print(Text("（未找到任务快照，仅显示 run 统计）"), soft_wrap=True)
            return
        self._console.print(Text(_artifacts_line(tasks)), soft_wrap=True)
        self._console.print(Text(_review_line(tasks)), soft_wrap=True)


# ── 编辑器路径 ────────────────────────────────────────────────────────────


def edit_tasks_in_editor(
    tasks: Sequence[Task],
    *,
    editor: str | None = None,
    catalog: RoleCatalog | None = None,
) -> list[Task]:
    """把拆解写进临时 JSON，交给编辑器改，读回后重走 §7.2 校验。

    落盘的是 ``PlanOutput`` 形状（只含 PM 能决定的字段）——``TaskDraft``
    是 ``extra="forbid"``，把运行期字段（status/attempts/reviews…）写进去会被
    解析直接拒掉。``T-FINAL`` 不写进文件：它是平台追加的保留任务，
    :func:`with_final_task` 会按编辑后的依赖关系重建。

    ``catalog`` 是本次 run 的角色目录：校验编辑结果时用它判定「角色已注册」，
    自定义角色（``roles.d`` / ``register_role``）因此可以在编辑后继续使用；
    不传则回落到内置八角色（直接调用本函数的旧路径）。

    任何失败（编辑器起不来、退出码非 0、文件被删/被改成非 UTF-8、JSON 坏了、
    字段不合法、路径冲突、临时文件清不掉）都收敛成 :class:`PlanValidationError`，
    调用方据此打印错误并重新提示，不会让整次 run 崩掉 —— 「所有失败都重新提示」
    包括**读回**这一步，不只是校验那一步。
    """
    payload = json.dumps(
        {"tasks": [_draft(task) for task in tasks if task.id != FINAL_TASK_ID]},
        ensure_ascii=False,
        indent=2,
    )
    command = _editor_command(editor)
    try:
        raw = _edit_via_editor(command, payload)
    except PlanValidationError:
        raise
    except OSError as exc:
        # TemporaryDirectory 的清理在 Windows 上会因编辑器未放手而抛 PermissionError。
        raise PlanValidationError([f"编辑器临时文件无法清理：{exc}"]) from exc
    try:
        plan = PlanOutput.model_validate_json(raw)
    except ValueError as exc:  # JSONDecodeError 与 pydantic ValidationError 都是 ValueError
        raise PlanValidationError([f"编辑后的拆解无法解析：{exc}"]) from exc
    validated = with_final_task(to_tasks(plan))
    validate_plan(validated, catalog if catalog is not None else RoleCatalog(BUILTIN_ROLES))
    return validated


def _edit_via_editor(command: str, payload: str) -> str:
    """写临时文件 → 起编辑器 → 读回文本；失败一律转成 :class:`PlanValidationError`。"""
    with tempfile.TemporaryDirectory(prefix="agentcrew-plan-") as tmp:
        path = Path(tmp) / "plan.json"
        path.write_text(payload, encoding="utf-8")
        try:
            result = subprocess.run([command, str(path)], check=False)
        except OSError as exc:
            raise PlanValidationError(
                [f"无法启动编辑器 {command!r}：{exc}（可用 $AGENTCREW_EDITOR / $EDITOR 指定）"]
            ) from exc
        if result.returncode != 0:
            raise PlanValidationError(
                [f"编辑器 {command!r} 退出码 {result.returncode}，本次编辑作废"]
            )
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PlanValidationError(
                [f"无法读取编辑结果（文件被删除或不是 UTF-8 文本）：{exc}"]
            ) from exc


def _editor_command(editor: str | None) -> str:
    """编辑器可执行文件：参数 → ``$AGENTCREW_EDITOR`` → ``$EDITOR`` → 平台默认。

    只接受**单个可执行文件名**（不加参数拆分）：``$EDITOR`` 里带参数（``code -w``）
    在 Windows 上无法可靠拆分，宁可让它以「找不到可执行文件」明确失败。
    """
    if editor:
        return editor
    for name in ("AGENTCREW_EDITOR", "EDITOR"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return "notepad" if sys.platform == "win32" else "vi"


def _draft(task: Task) -> dict[str, Any]:
    """``Task`` → ``TaskDraft`` 形状的 dict（只含 PM 能决定的字段）。"""
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "acceptance_criteria": list(task.acceptance_criteria),
        "deliverables": [
            artifact.model_dump(mode="json") for artifact in task.deliverables
        ],
        "dependencies": list(task.dependencies),
        "priority": task.priority,
        "brief": task.brief,
    }


# ── 展示辅助 ──────────────────────────────────────────────────────────────


def _load_tasks(record: RunRecord | None) -> list[Task] | None:
    """从 run 目录尽力读回任务快照；读不到返回 ``None``（展示路径绝不抛）。

    ``config_snapshot["output_dir"]`` 是 run 创建时的绝对路径，据此定位
    ``tasks.json``。文件缺失、JSON 损坏、schema 不符都只是「没得展示」。
    """
    if record is None:
        return None
    output_dir = record.config_snapshot.get("output_dir")
    if not isinstance(output_dir, str):
        return None
    try:
        store = CheckpointStore(Path(output_dir) / record.run_id)
        return store.load_board().all()
    except (OSError, ValueError):
        return None


def _role_line(tasks: Sequence[Task]) -> str:
    """任务数 + 角色分布（``backend_dev 3 · qa_engineer 1``）。"""
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.role] = counts.get(task.role, 0) + 1
    detail = " · ".join(f"{role} {count}" for role, count in sorted(counts.items()))
    return f"共 {len(tasks)} 个任务；角色分布：{detail or '（无）'}"


def _artifacts_line(tasks: Sequence[Task]) -> str:
    """产物清单：实际落盘的文件（``TaskResult.artifacts``）去重保序。"""
    paths = dict.fromkeys(
        artifact.path
        for task in tasks
        if task.result is not None
        for artifact in task.result.artifacts
    )
    if not paths:
        return "产物清单：空"
    body = "".join(f"\n  {path}" for path in paths)
    return f"产物清单（{len(paths)} 个）：{body}"


def _review_line(tasks: Sequence[Task]) -> str:
    """评审通过率与各层驳回数；没有评审记录时不假装 100%。"""
    reviews = [review for task in tasks for review in task.reviews]
    if not reviews:
        return "评审通过率：—（无评审记录）"
    rejected = [review for review in reviews if review.verdict == "reject"]
    rate = round(100 * (len(reviews) - len(rejected)) / len(reviews))
    g1 = sum(1 for review in rejected if review.layer == "static")
    g2 = sum(1 for review in rejected if review.layer == "llm")
    return f"评审通过率：{rate}%（G1 拦截 {g1} / G2 驳回 {g2}）"
