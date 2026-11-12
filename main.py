"""AgentCrew CLI 入口（spec §15.1）。

六个子命令：``run`` / ``resume`` / ``status`` / ``roles`` / ``clean`` / ``cancel``。
本模块只做三件事：解析参数、装配已提交的引擎组件、把结果翻译成退出码。所有业务
决策都在 ``Crew`` / ``Gatekeeper`` / ``CheckpointStore`` 里 —— 这里不复刻一份，也不
绕过任何一层。

设计约束：

- **退出码全部取自** :data:`agentcrew.crew.crew.EXIT_CODES`（spec §7.4）。本模块不
  重新定义数字，只做「结束方式 → 键」的映射。
- **``--gate`` 指定人工卡点但没有 TTY 时，显式降级为 ``none``** 并在 stderr 说明
  一句，随后用 ``NullPrompter`` 继续（spec §10.3：「非 TTY 自动降级为 none 并在
  日志中注明」）。绝不做「gate 仍写着 both、prompter 悄悄换成 NullPrompter」——
  那正是 Task 27 关掉的 fail-open，操作者会照着「有人工卡点」去规划。降级后的
  gate 会进配置快照与 ledger，不存在「声称有人工卡点、实际全自动」的错位。
- **Ctrl-C 是一等公民**（spec §13.4）：``Crew`` 已把 run 落盘为 ``INTERRUPTED``，
  这里只负责打印 ``resume <run_id>`` 并返回 130。第二次 Ctrl-C 由 :func:`main`
  与模块级入口兜住，不留 traceback。
- **输出编码**：Windows 重定向的 stdout 常是 GBK，而看板的状态符号（``✓`` / ``⟳``）
  在 GBK 里没有码位。入口把「编不出探测串」的 stdout/stderr 包一层替换流，重定向
  永远不会因 ``UnicodeEncodeError`` 崩掉。
- ``on_event`` 接到 :class:`agentcrew.runtime.board_view.BoardView`：``Scheduler``
  发的是 ``(事件名, 数据)``，看板要 ``LedgerEvent``，桥在 :class:`_BoardHook`。
  看板**按需创建** —— run_id 要到 ``Crew`` 建好 run 目录之后才知道。
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import math
import os
import signal
import sys
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import yaml
from rich.console import Console
from rich.text import Text

from agentcrew.cli.diff import (
    diff_runs,
)
from agentcrew.cli.diff import (
    print_diff as print_run_diff,
)
from agentcrew.cli.doctor import (
    DoctorCheck,
    DoctorReport,
    run_diagnostics,
)
from agentcrew.cli.doctor import (
    print_report as print_doctor_report,
)
from agentcrew.config import GateMode, Settings, redact_overrides
from agentcrew.crew.crew import (
    EXIT_CODES,
    Crew,
    PlanFailedError,
    RunCancelled,
    build_catalog,
)
from agentcrew.gate.cli_prompter import CliPrompter
from agentcrew.gate.deferred import DeferredPrompter
from agentcrew.gate.gatekeeper import NullPrompter, Prompter
from agentcrew.models.config import PROVIDER_PRESETS as PROVIDER_PRESETS_NAMES
from agentcrew.models.factory import ModelFactoryError
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.approvals import ApprovalError, ApprovalStore
from agentcrew.runtime.board_view import BoardView
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunRecord,
    RunStatus,
    clean,
    delete_run,
)
from agentcrew.runtime.ledger import Ledger, LedgerEvent
from agentcrew.runtime.lifecycle import ResumeLockError, reconcile_stale_runs
from agentcrew.utils.logger import setup_logging
from examples.todo_web_project import PROJECT_GOAL

#: 含人工卡点的 gate 模式（与 ``Gatekeeper`` 的 ``_PLAN_GATES`` / ``_DELIVERY_GATES`` 同源）。
_HUMAN_GATES: frozenset[str] = frozenset({"both", "plan", "delivery"})

#: 编码探测串：看板状态符号编不进 GBK，中文可以。
_ENCODING_PROBE = "✓⟳—·中文"

#: ``resume`` 时参与「结果可比性」比对的配置键（spec §13.3 第 3 步：防换模型后结果不可比）。
#: 不比对 ``output_dir`` / ``gate`` / ``log_level`` 之类的运行环境字段：它们变了
#: 不影响产物含义，比进去只会逼用户无脑加 ``--force``。
_COMPARABLE_KEYS = (
    "default_model",
    "role_bindings",
    "provider_overrides",
    "context_budget_tokens",
    "mock",
)


# ── 输出编码（GBK 重定向不崩）─────────────────────────────────────────────


class _EncodingSafeStream:
    """把目标流编码不出的字符替换为 ``?`` 的写包装。

    Windows 上 ``python main.py run > log.txt`` 的 stdout 是 GBK；rich 画看板时写的
    ``✓`` / ``⟳`` 会让 ``TextIOWrapper.write`` 抛 ``UnicodeEncodeError`` —— 进程在
    收尾打印时崩掉，比画面少几个符号严重得多。包装只改 ``write``：先按目标编码试编，
    编不出就整体 ``errors="replace"``；其余属性（``isatty`` / ``fileno`` / ``buffer``…）
    原样代理给底层流。
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    @property
    def encoding(self) -> str:
        """底层流的编码（rich 的 ``Console.encoding`` 读它）。"""
        return getattr(self._stream, "encoding", None) or "utf-8"

    def write(self, text: str) -> int:
        """按目标编码降级后写入；返回底层流报告的字符数。"""
        try:
            text.encode(self.encoding)
        except UnicodeEncodeError:
            text = text.encode(self.encoding, "replace").decode(
                self.encoding, "replace")
        return self._stream.write(text)

    def flush(self) -> None:
        """透传 flush。"""
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _install_encoding_safe_stdio() -> None:
    """把编不出探测串的 stdout/stderr 换成 UTF-8 或 :class:`_EncodingSafeStream`。

    两级处理，顺序不能反：

    1. **非 TTY（重定向到文件 / 管道 / CI）先尝试 ``reconfigure(encoding="utf-8")``**。
       重定向时目标编码默认取系统代码页（中文 Windows = GBK），而文件/管道本身
       并不限制编码——强制 UTF-8 能**完整保留**看板符号与中文。这一步不改变
       终端上的显示（TTY 分支不动）。
    2. 回退到 :class:`_EncodingSafeStream`（逐字符替换），保证不因编码异常崩进程。

    ``reconfigure`` 在 Python 3.7+ 的 ``TextIOWrapper`` 上有，但包装过的流（pytest
    的 capture、rich 的 console file）未必有——所以只做「有就用」，失败即跳过。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or isinstance(stream, _EncodingSafeStream):
            continue
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            _ENCODING_PROBE.encode(encoding)
            continue   # 目标编码够用，不动它
        except (UnicodeEncodeError, LookupError):
            pass
        if not getattr(stream, "isatty", lambda: False)():
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                    continue
                except (OSError, ValueError):
                    pass   # 流不支持重配置（已被包装）→ 落到字符替换
        setattr(sys, name, _EncodingSafeStream(stream))


# ── 参数解析 ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """六个子命令（spec §15.1）。

    ``run`` 的 ``--goal`` / ``--demo`` **不用** argparse 的互斥组：互斥组在两者
    同时给出时会以退出码 2 直接结束进程，而这里要求「都没给 / 都给」一律报错并
    返回退出码 3（:data:`EXIT_CODES` 的 ``config``）。
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AgentCrew：8 角色多 Agent 协作平台（规划 → 执行 → 三层门禁 → 交付）",
    )
    sub = parser.add_subparsers(
        dest="command",
        metavar="{run,resume,status,approvals,approve,roles,agents,switch,"
                "clean,cancel,delete,serve,doctor,diff,schedule}")

    run = sub.add_parser("run", help="执行一次完整 run（规划 → 执行 → 交付）")
    run.add_argument("--goal", metavar="TEXT", help="项目目标（与 --demo 二选一）")
    run.add_argument(
        "--demo", action="store_true",
        help="使用 examples/todo_web_project.py 的内置 Todo Web 目标（与 --goal 二选一）")
    run.add_argument(
        "--flow", metavar="NAME|FILE",
        help="R28：用流程定义起跑（output/_flows 里的名字，或 .yaml 文件路径）——"
             "跳过 PM 规划，直接按画布/文件里的任务表执行；缺省目标取流程描述")
    _add_run_flags(run)

    resume = sub.add_parser("resume", help="续跑一个中断或未完成的 run")
    resume.add_argument("run_id", help="run 目录名（见 status / clean 的输出）")
    resume.add_argument(
        "--force", action="store_true", help="配置快照与当前配置不一致时仍继续")
    _add_run_flags(resume)

    approvals = sub.add_parser(
        "approvals", help="R4：列出一个 run 的审批单（含待裁决）")
    approvals.add_argument("run_id", help="run 目录名")
    approvals.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")

    approve = sub.add_parser(
        "approve", help="R4：对审批单写裁决（可在另一个进程里执行）")
    approve.add_argument("run_id", help="run 目录名")
    approve.add_argument("request_id", help="审批单 id（见 approvals 输出）")
    approve.add_argument(
        "--decision", required=True,
        choices=("approve", "cancel", "save_exit", "accept", "abort", "rework",
                 "step", "run"),
        help="裁决动作；plan/delivery/step 卡点各只接受自己的子集")
    approve.add_argument("--task-ids", nargs="*", default=[], metavar="ID",
                         help="rework 时要打回的任务 id")
    approve.add_argument("--by", default="cli", metavar="NAME",
                         help="裁决人标识（写进审批单留痕）")
    approve.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")

    status = sub.add_parser("status", help="打印一个 run 的记录与任务表")
    status.add_argument("run_id")
    status.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")

    sub.add_parser("roles", help="列出 8 个内置角色（职责 / 边界 / 产出）")
    p_tools = sub.add_parser(
        "tools", help="R24：列出内置工具（名称 / 类别 / 入参 schema）")
    p_tools.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_tool = sub.add_parser(
        "tool", help="R24：单工具试跑（沙箱目录隔离，需工具执行已开启）")
    p_tool.add_argument("name", help="工具名（见 tools 命令）")
    p_tool.add_argument(
        "--args", default="{}", metavar="JSON",
        help='入参 JSON 对象，如 \'{"path": "a.txt"}\'')
    p_tool.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_tool.add_argument("--output-dir", metavar="DIR", help="试跑沙箱根目录")
    p_keys = sub.add_parser(
        "keys", help="R25：管理 API 密钥（多用户 RBAC：角色 / 作用域 / 归属）")
    keys_sub = p_keys.add_subparsers(dest="keys_action", required=True)
    p_keys_add = keys_sub.add_parser("add", help="新建密钥（明文只打印一次）")
    p_keys_add.add_argument("--name", required=True, help="身份名（审计与归属用）")
    p_keys_add.add_argument(
        "--role", default="operator",
        choices=("admin", "operator", "viewer", "custom"),
        help="角色（默认 operator；custom 需配 --scope）")
    p_keys_add.add_argument(
        "--scope", action="append", default=[], metavar="SCOPE",
        help="附加作用域，可多次：runs:read / runs:write / runs:admin / "
             "approvals:write / tools:read / tools:test / config:read / "
             "keys:read / keys:write")
    p_keys_add.add_argument(
        "--runs", default="", metavar="R1,R2",
        help="可见 run 白名单（逗号分隔）；缺省=只看自己创建的 run")
    p_keys_add.add_argument(
        "--expires", default=None, metavar="ISO8601",
        help="过期时间，如 2026-12-31T00:00:00")
    p_keys_add.add_argument("--note", default="", help="备注")
    p_keys_list = keys_sub.add_parser("list", help="列出密钥（不含哈希）")
    p_keys_list.add_argument("--json", action="store_true", help="输出 JSON")
    p_keys_revoke = keys_sub.add_parser("revoke", help="吊销密钥（id 或 name）")
    p_keys_revoke.add_argument("ident", help="密钥 id 或 name")
    for _keys_parser in (p_keys_add, p_keys_list, p_keys_revoke):
        _keys_parser.add_argument(
            "--output-dir", metavar="DIR", help="output 目录（定位 _auth/keys.json）")
    p_flow = sub.add_parser(
        "flow", help="R26：可视化编排——校验 / 列表 / 导出示例流程 YAML")
    flow_sub = p_flow.add_subparsers(dest="flow_action", required=True)
    p_flow_v = flow_sub.add_parser("validate", help="校验一个流程 YAML 文件")
    p_flow_v.add_argument("file", help="流程文件路径（- 表示 stdin）")
    p_flow_l = flow_sub.add_parser("list", help="列出已保存流程")
    p_flow_l.add_argument("--json", action="store_true", help="输出 JSON")
    flow_sub.add_parser("sample", help="打印示例流程 YAML（可重定向成文件）")
    p_recall = sub.add_parser(
        "recall", help="R27：按查询词检索历史经验（验证 BM25 / 向量混合排序）")
    p_recall.add_argument("query", help="查询词 / 任务描述")
    p_recall.add_argument("--role", default=None, help="只检索该角色的经验")
    p_recall.add_argument("--limit", type=int, default=5, help="返回条数（默认 5）")
    p_recall.add_argument("--json", action="store_true", help="输出 JSON")
    for _flow_parser in (p_flow_v, p_flow_l):
        _flow_parser.add_argument(
            "--output-dir", metavar="DIR", help="output 目录（定位 _flows/）")
    p_agents = sub.add_parser("agents", help="列出执行体档案与角色绑定（--json 供 Studio 读取）")
    p_agents.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_agents.add_argument("--yaml", action="store_true",
                          help="导出可复用的 agentcrew.yaml 片段（agents.profiles + roles 绑定）")
    p_switch = sub.add_parser("switch", help="给一个 run 写入执行体切换（下次 resume 生效）")
    p_switch.add_argument("run_id")
    p_switch.add_argument("role")
    p_switch.add_argument("agent", help="目标 agent profile（须已在 RunPlan 中）")
    p_switch.add_argument("--output-dir", default=None)
    p_switch.add_argument("--task", default="", metavar="TASK_ID",
                          help="只给这一个任务换（写 task_overrides.json）")

    # C5：文档 RAG——团队文档（knowledge/docs/）的索引管理与检索演示。
    p_docs = sub.add_parser(
        "docs", help="C5：团队文档 RAG——列出/检索 knowledge/docs/ 下的文档索引")
    docs_sub = p_docs.add_subparsers(dest="docs_action", required=True)
    p_docs_list = docs_sub.add_parser(
        "list", help="列出已索引的文档与块数（触发一次增量摄取）")
    p_docs_list.add_argument("--output-dir", default=None)
    p_docs_search = docs_sub.add_parser(
        "search", help="检索演示：按查询词返回最相关的文档片段")
    p_docs_search.add_argument("query", metavar="QUERY")
    p_docs_search.add_argument("--limit", type=int, default=4)
    p_docs_search.add_argument("--output-dir", default=None)

    p_schedule = sub.add_parser(
        "schedule", help="定时任务（Automations）：一次性 / 周期日程的增删查与到期触发")
    schedule_sub = p_schedule.add_subparsers(dest="schedule_action", required=True)
    p_sched_add = schedule_sub.add_parser("add", help="新建日程")
    p_sched_add.add_argument("--goal", required=True, metavar="TEXT", help="到期要跑的目标")
    p_sched_add.add_argument("--at", metavar="ISO8601", help="一次性：触发时间（如 2026-12-01T09:00:00）")
    p_sched_add.add_argument("--daily", metavar="HH:MM", help="周期：每天触发时间（如 09:00）")
    p_sched_add.add_argument("--weekdays", default="MO,TU,WE,TH,FR", metavar="MO,WE",
                             help="周期：命中星期（逗号分隔 MO..SU，默认工作日）")
    p_sched_add.add_argument("--mode", default="agent", choices=("agent", "plan", "ask"),
                             help="执行模式：agent=自动执行 / plan=计划审查 / ask=只读规划")
    p_sched_add.add_argument("--mock", action="store_true", help="用 mock provider 跑")
    p_sched_list = schedule_sub.add_parser("list", help="列出日程")
    p_sched_list.add_argument("--json", action="store_true", help="输出 JSON")
    p_sched_rm = schedule_sub.add_parser("remove", help="删除日程")
    p_sched_rm.add_argument("schedule_id", help="日程 id（见 list）")
    p_sched_tick = schedule_sub.add_parser("tick", help="到期检查 + 触发（幂等，可高频调用）")
    p_sched_tick.add_argument("--json", action="store_true", help="输出本轮触发 JSON")
    for _sched_parser in (p_sched_add, p_sched_list, p_sched_rm, p_sched_tick):
        _sched_parser.add_argument("--output-dir", metavar="DIR", help="output 目录（定位 _schedules/）")

    samples = sub.add_parser(
        "samples", help="列出预设 Crew 样例（串行 / 层级 / 混合并发）")
    samples.add_argument("name", nargs="?", help="只显示指定样例的详情")
    samples.add_argument("--json", action="store_true",
                         help="以 JSON 输出（便于脚本解析）")

    p_eval = sub.add_parser(
        "eval", help="P2-5：跑离线评测集（产物断言 + pass^k）")
    p_eval.add_argument("dataset", help="JSONL 数据集路径（一行一条 case）")
    p_eval.add_argument(
        "--repeat", type=int, default=1, metavar="K",
        help="每个 case 跑 K 次，用 pass^K 衡量稳定性（默认 1）")
    p_eval.add_argument("--json", action="store_true",
                        help="以 JSON 输出（便于 CI 解析）")
    p_eval.add_argument(
        "--output-dir", default=None, metavar="DIR",
        help="把每次 run 的目录留在这里便于排查（默认用临时目录并清理）")
    p_eval.add_argument(
        "--variant", action="append", default=None, metavar="NAME=FILE",
        help="R45：配置变体（可重复）。给了就进入 A/B 矩阵模式：同一批 case 分别用"
             "各变体跑一遍，第一个变体作基线。FILE 是 Settings 字段的 YAML/JSON"
             "（只写要覆盖的字段，如 agent_profiles / role_agents）")
    p_eval.add_argument(
        "--matrix-out", default=None, metavar="PATH",
        help="R45：把矩阵结果写成 JSON（默认不落盘，只在终端打印）")
    p_eval.add_argument(
        "--live", action="store_true",
        help="C11：走**真实模型**跑（默认 mock）。真实能力证据只能来自这里；"
             "建议配合 --max-cost 设硬上限，夜间限额跑")
    p_eval.add_argument(
        "--max-cost", type=float, metavar="USD",
        help="C11：本次评测的成本硬上限（真实通道必备护栏；超预算 run 会失败）")

    clean_cmd = sub.add_parser(
        "clean", help="保留最近 N 个 run，删除更旧的（默认 N=5）")
    clean_cmd.add_argument("--keep", type=int, default=5, metavar="N",
                           help="保留的 run 个数（默认 5）")
    clean_cmd.add_argument("--dry-run", action="store_true",
                           help="只列出将删除的 run，不真删")
    clean_cmd.add_argument(
        "--protect-running", action="store_true",
        help="跳过正在运行且有心跳的 run（生产环境建议开启；僵尸 run 仍可清理）")
    clean_cmd.add_argument("--output-dir", metavar="DIR")

    cancel = sub.add_parser(
        "cancel", help="给离线 run 打 cancelled 标记（下次 resume 直接收尾）")
    cancel.add_argument("run_id")
    cancel.add_argument("--output-dir", metavar="DIR")

    delete_cmd = sub.add_parser(
        "delete", help="删除单个 run（不可恢复；running 状态拒绝）")
    delete_cmd.add_argument("run_id", help="要删除的 run 目录名")
    delete_cmd.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")
    delete_cmd.add_argument("--force", action="store_true",
                            help="跳过确认提示（非交互环境/脚本用）")

    serve = sub.add_parser(
        "serve", help="启动 HTTP API + Web UI 面板（V2）")
    serve.add_argument(
        "--host", default="127.0.0.1", metavar="HOST",
        help="监听地址（默认只绑回环）。绑非回环地址时必须配置 API Key，"
             "否则拒绝启动")
    serve.add_argument("--port", type=int, default=8765, metavar="PORT")
    serve.add_argument(
        "--api-key", metavar="KEY",
        help="E-3：快速设置单个 API Key（等价 admin 角色；与 AGENTCREW_API_KEYS 合并）")

    doctor = sub.add_parser(
        "doctor", help="诊断配置 / 环境 / 依赖 / Provider 连通性，输出健康报告")
    doctor.add_argument("--config", metavar="PATH",
                        help="指定 agentcrew.yaml 路径（缺省读工作目录下的同名文件）")
    doctor.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")
    doctor.add_argument("--json", action="store_true",
                        help="以 JSON 输出报告（便于脚本解析）")

    diff = sub.add_parser(
        "diff", help="对比两个 run 的配置 / 任务 / 产物 / 事件统计差异")
    diff.add_argument("run_id_a", help="第一个 run ID（基线）")
    diff.add_argument("run_id_b", help="第二个 run ID")
    diff.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")
    diff.add_argument("--json", action="store_true",
                      help="以 JSON 输出差异（便于脚本解析）")

    providers = sub.add_parser(
        "providers", help="列出合并后的 provider 目录（内置 + 自定义）")
    providers.add_argument("name", nargs="?", help="只显示指定 provider 的详情")
    providers.add_argument("--category",
                           choices=("aggregator", "national", "international",
                                     "local", "specialized"),
                           help="按业务分类过滤（aggregator/national/international/local/specialized）")
    providers.add_argument("--search", metavar="KEYWORD",
                           help="按 name / search_aliases 子串搜索（大小写不敏感）")
    providers.add_argument("--json", action="store_true",
                           help="以 JSON 输出目录（便于脚本解析）")

    models = sub.add_parser(
        "models", help="按需拉取指定 provider 的模型列表")
    models.add_argument("provider", help="provider 名（如 openai / openrouter）")
    models.add_argument("--verbose", "-v", action="store_true",
                        help="显示端点候选尝试过程详情")
    models.add_argument("--json", action="store_true",
                        help="以 JSON 输出模型列表（便于脚本解析）")

    config = sub.add_parser("config", help="配置导入导出")
    config_sub = config.add_subparsers(dest="config_action")

    # C7：把 AgentCrew 的编排能力（派活/看进度/裁决审批）经 MCP 暴露给
    # Claude Desktop / Cursor 类客户端。stdio 传输；工具调用走本机 HTTP API，
    # RBAC 与审计由 API 侧执行——MCP 入口不产生第二条权限语义。
    p_mcp = sub.add_parser(
        "mcp-serve", help="C7：以 MCP server（stdio）暴露编排能力，"
                          "供 Claude Desktop / Cursor 等客户端调用")
    p_mcp.add_argument("--api-url", default="http://127.0.0.1:8765",
                       metavar="URL", help="AgentCrew API 地址（默认本机 8765）")
    p_mcp.add_argument("--api-key", default=None, metavar="KEY",
                       help="调用 API 用的密钥（默认读 AGENTCREW_MCP_API_KEY，"
                            "再退到 AGENTCREW_API_KEY）；密钥不落盘")
    export_p = config_sub.add_parser("export", help="导出当前合并配置为 YAML")
    export_p.add_argument("--output", "-o", metavar="FILE",
                          help="写入文件而非 stdout")
    import_p = config_sub.add_parser("import", help="从 YAML 文件导入配置")
    import_p.add_argument("file", help="要导入的 YAML 文件路径")

    return parser


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    """``run`` / ``resume`` 共用的配置与看板开关。"""
    parser.add_argument(
        "--sample", metavar="NAME",
        help="使用预设样例（见 `samples` 子命令）：同时设置目标与 mock 夹具，"
             "并自动开启 --mock。run 时与 --goal / --demo 互斥。")
    parser.add_argument("--mock", action="store_true",
                        help="强制 mock provider（覆盖所有角色绑定，无需 API Key）")
    # C4：随目标附带的图片（多模态输入）。可重复（最多 4 张）；单张 ≤2.6 MB，
    # png/jpg/webp/gif。是否真发给模型由 provider 的 supports_vision 能力位决定，
    # 不支持时降级为纯文本并落 vision_unsupported 事件（降级可见，不静默）。
    parser.add_argument(
        "--image", action="append", default=None, metavar="PATH",
        help="随目标附带图片（可重复，最多 4 张；单张 ≤2.6 MB，png/jpg/webp/gif）。"
             "仅在 provider 开启 capabilities.supports_vision 时真正生效")
    parser.add_argument("--auto", action="store_true",
                        help="等价于 --gate none（全自动，跳过人工闸门）")
    parser.add_argument("--plan-only", action="store_true",
                        help="Ask 模式：只生成计划并落盘（不派发执行），可用 resume 续跑；"
                             "与 --auto / --gate 互斥")
    parser.add_argument("--debug-step", action="store_true",
                        help="R20：单步断点——每个任务派发前挂起为 step 审批单，"
                             "用 approve --decision step|run|abort 逐个放行")
    parser.add_argument("--gate", choices=("both", "plan", "delivery", "none"),
                        help="人工闸门模式；含人工卡点但无 TTY 时降级为 none")
    parser.add_argument("--max-concurrency", type=int, metavar="N",
                        help="全局并发上限（含 LLM 调用）")
    parser.add_argument("--default-model", metavar="ALIAS",
                        help="默认模型别名，形如 provider:model")
    parser.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")
    parser.add_argument("--context-budget", type=int, metavar="N",
                        help="上下文预算（tokens）")
    parser.add_argument("--max-cost", type=float, metavar="USD",
                        help="累计 LLM 成本硬上限（USD），超限优雅收尾（P0-3）")
    parser.add_argument("--max-llm-calls", type=int, metavar="N",
                        help="累计 LLM 调用次数硬上限，超限优雅收尾（P0-3）")
    parser.add_argument(
        "--approval-mode", choices=("interactive", "deferred"),
        help="R4：人工卡点实现方式。interactive（默认）= TTY 同步问答；"
             "deferred = 审批单落盘后 run 挂起，用 approve 命令在另一个进程裁决再 resume")
    parser.add_argument("--no-live", action="store_true",
                        help="关闭实时看板，只保留结束报告（CI 友好）")


# ── 入口 ──────────────────────────────────────────────────────────────────


def _setup_logging_early() -> None:
    """在分派任何子命令之前接线脱敏日志（2026-11 可观测性改造）。

    ``utils.logger.setup_logging`` 与 ``Settings.log_level`` 此前是「建好却从未
    被调用」的死基建——全库日志调用（ledger 坏行警告、MCP 传输警告等）落在
    root logger 的默认 WARNING 上且无格式。这里统一接线：

    - 配置加载失败时**静默跳过**：入口绝不因日志配置崩掉，子命令自己的错误
      路径（``_fail``）照常工作；
    - ``AGENTCREW_LOG_JSON=1`` 切换 JSON 结构化输出（便于日志系统检索），
      缺省纯文本，行为兼容。
    """
    try:
        settings = Settings.load()
    except (OSError, ValueError):
        return
    json_format = os.environ.get(
        "AGENTCREW_LOG_JSON", "").strip().lower() in ("1", "true", "yes")
    setup_logging(settings.log_level, settings.secret_values(),
                  json_format=json_format)


def main(argv: Sequence[str] | None = None) -> int:
    """解析并分派；返回进程退出码（spec §7.4 的那张表）。"""
    _install_encoding_safe_stdio()
    _setup_logging_early()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        _print_bare_invocation(parser)
        return EXIT_CODES["ok"]
    handler = _HANDLERS[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        # 第二次 Ctrl-C（第一次已在 _drive 里打印 resume 提示）：静默收尾，不留 traceback。
        print("已中断。", file=sys.stderr)
        return EXIT_CODES["cancelled"]


def _print_bare_invocation(parser: argparse.ArgumentParser) -> None:
    """无子命令时的默认入口：帮助 + 已注册角色数 + 最近 3 次 run 概览（spec §15.1）。

    这是 CLI 的默认发现路径（Task 34 的 README 指向它），所以不只打 help：角色数
    用 ``BUILTIN_ROLES``，最近 run 用 ``CheckpointStore.list_runs``（与 ``clean``
    同一份列表，new→old，取前三条）。配置坏了也照常返回帮助：这里的目标是「告诉
    用户有哪些工具」，不该因为一个坏 YAML 变成非零退出。
    """
    parser.print_help()
    console = Console()
    _out(
        console,
        f"已注册角色 {len(BUILTIN_ROLES)} 个"
        "（python main.py roles 可看职责 / 边界 / 产出）",
    )
    try:
        output_dir = Settings.load().output_dir
    except (OSError, ValueError) as exc:
        _out(console, f"最近 3 次 run：（配置不可用，略过：{exc}）")
        return
    runs = CheckpointStore.list_runs(output_dir)[:3]
    if not runs:
        _out(console, f"最近 3 次 run：无（{output_dir} 下还没有 run）")
        return
    _out(console, f"最近 3 次 run（{output_dir}）：")
    for run_id in runs:
        _out(console, f"  {run_id}")


# ── run / resume ──────────────────────────────────────────────────────────


def _expire_overdue_approvals(settings: Settings, *,
                              run_id: str | None = None) -> int:
    """R30：扫描审批单，把超窗口的标记为 ``expired``。

    只警告不改变 run 结局（仍是 interrupted）；每张过期单会在对应 run 的 ledger
    里留一条 ``approval_expired``——有接 Webhook 的部署据此告警。返回本次过期数量。
    扫描失败一律不阻断调用方（status / serve 启动是只读入口，不该被清理拖死）。

    ``run_id`` 给定时**只结算这一个 run**：``status`` 是查看单个 run 的命令，
    顺手清扫整目录会让它变成一个对别的 run 有副作用的「只读」命令（也曾让批量
    测试互相干扰）。全局清扫只在 ``serve`` 启动做一次。
    """
    from agentcrew.runtime.approvals import ApprovalStore
    from agentcrew.runtime.checkpoint import CheckpointStore
    from agentcrew.runtime.ledger import Ledger

    expired_total = 0
    if run_id is not None:
        run_ids = [run_id]
    else:
        try:
            run_ids = CheckpointStore.list_runs(settings.output_dir)
        except (OSError, ValueError):
            return 0
    for run_id in run_ids:
        run_dir = settings.output_dir / run_id
        try:
            store = ApprovalStore(run_dir)
            expired = store.expire_overdue()
        except (OSError, ValueError):
            continue
        if not expired:
            continue
        ledger = Ledger(run_dir / "ledger.jsonl", run_id=run_id)
        for request in expired:
            ledger.emit("approval_expired", request_id=request.id,
                        checkpoint=request.checkpoint,
                        expires_at=request.expires_at.isoformat()
                        if request.expires_at else None)
            expired_total += 1
        print(
            f"run {run_id}：{len(expired)} 张审批单已超时（"
            f"approval_timeout_seconds={settings.approval_timeout_seconds}），"
            "需要重新触发卡点或调大窗口后重跑",
            file=sys.stderr,
        )
    return expired_total


def _reconcile_on_startup(settings: Settings) -> None:
    """R17：启动时收敛孤儿 RUNNING；只警告，绝不因为清理失败阻断本次启动。

    库层直接调 ``Crew.run()`` 不走这里（程序化调用不该有隐性副作用），只有
    CLI ``run`` 与 ``serve`` 这两个进程入口做一次全目录扫描。
    """
    try:
        results = reconcile_stale_runs(settings.output_dir)
    except (OSError, ValueError) as exc:
        print(f"启动时 run 状态收敛跳过（{exc}）", file=sys.stderr)
        return
    for item in results:
        run_id = item["run_id"]
        if item.get("ok"):
            print(
                f"已把孤儿 run {run_id} 标记为 interrupted"
                f"（{item['reason']}），可用 resume 续跑",
                file=sys.stderr,
            )
        else:
            print(
                f"run {run_id} 状态收敛失败：{item.get('error')}",
                file=sys.stderr,
            )


#: CLI ``--image`` 的上限与白名单（与 API 侧 validate_images 同口径）。
_IMAGE_MAX_BYTES = 2_600_000
_IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}


def _load_cli_images(paths: list[str] | None) -> tuple[list[dict], str | None]:
    """读取 ``--image`` 指定的文件并转成 Crew 的 images 形状。

    返回 ``(images, 错误文案)``；错误文案非 None 时调用方 ``_fail``。
    """
    import base64 as _base64

    if not paths:
        return [], None
    if len(paths) > 4:
        return [], "--image 最多 4 张"
    images: list[dict] = []
    for raw_path in paths:
        path = Path(raw_path)
        mime = _IMAGE_MIME_BY_SUFFIX.get(path.suffix.lower())
        if mime is None:
            return [], (
                f"--image {raw_path}：不支持的格式 {path.suffix!r}"
                "（可用 png / jpg / webp / gif）")
        try:
            data = path.read_bytes()
        except OSError as exc:
            return [], f"--image {raw_path}：读取失败（{exc}）"
        if len(data) > _IMAGE_MAX_BYTES:
            return [], (f"--image {raw_path}：单张上限 2.6 MB，"
                        f"实际 {len(data) / 1e6:.1f} MB")
        images.append({
            "mime": mime,
            "data": _base64.b64encode(data).decode("ascii"),
        })
    return images, None


def _cmd_run(args: argparse.Namespace) -> int:
    """``run --goal TEXT | --demo | --sample NAME``：三选一，否则退出码 3。"""
    chosen = [
        flag for flag, given in (
            ("--goal", args.goal), ("--demo", args.demo), ("--sample", args.sample))
        if given
    ]
    if len(chosen) > 1:
        return _fail(f"run：{' 与 '.join(chosen)} 互斥，只能给一个。")
    sample_goal, sample_error = _apply_sample(args)
    if sample_error is not None:
        return _fail(sample_error)
    if args.demo:
        goal: str | None = PROJECT_GOAL
    elif sample_goal is not None:
        goal = sample_goal
    else:
        goal = args.goal
    flow_arg = getattr(args, "flow", None)
    if not goal and not flow_arg:
        return _fail("run：必须给 --goal / --demo / --sample / --flow 之一。")
    invalid = _invalid_numbers(args)
    if invalid is not None:
        return _fail(invalid)
    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:  # pydantic ValidationError / YAML 解析错误
        return _fail(f"配置错误：{exc}")
    _reconcile_on_startup(settings)
    try:
        # 目录只建一次：CliPrompter 的编辑校验与 Crew 的调度必须看到同一批角色，
        # 否则编辑后的计划会被「角色未注册」误拒（roles.d 的自定义角色）。
        catalog = build_catalog(settings)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    console = Console()
    gate, prompter, downgraded = _resolve_gate(
        settings.gate, interactive=_stdin_isatty(), console=console, catalog=catalog,
        approval_mode=settings.approval_mode)
    if downgraded:
        print(
            "非 TTY（管道 / CI）：--gate 含人工卡点但无法交互，已显式降级为 "
            "gate=none（不会向任何人提问）。",
            file=sys.stderr,
        )
    settings = settings.model_copy(update={"gate": gate})
    preset_tasks = None
    if flow_arg:
        try:
            flow_spec = _load_flow_definition(settings, flow_arg)
        except ValueError as exc:
            return _fail(f"run --flow：{exc}")
        preset_tasks = flow_spec.tasks
        if not goal:
            goal = flow_spec.description or f"流程 {flow_spec.name}"
        _out(console, f"按流程 {flow_spec.name} 起跑：{len(preset_tasks)} 个任务"
                      "（跳过 PM 规划）")
    assert goal is not None
    hook = _BoardHook(goal, enabled=not args.no_live, console=console)
    images, image_error = _load_cli_images(getattr(args, "image", None))
    if image_error:
        return _fail(image_error)
    crew = Crew(settings, catalog=catalog, prompter=prompter, on_event=hook,
                preset_tasks=preset_tasks, images=images)
    hook.bind(crew)
    return _drive(crew, lambda: crew.run(goal), hook=hook, console=console)


def _load_flow_definition(settings: Settings, source: str):
    """R28：``--flow`` 参数 → ``FlowSpec``（先按文件，再按流程库名字）。

    文件支持 ``.yaml`` / ``.yml``；否则当流程名去 ``<output>/_flows/<name>.yaml``
    找。两条路径都不通时抛 ``ValueError``（CLI 映射退出码 3）。图不合法同样抛
    ``ValueError``——**不带着坏流程起跑**，错误在开跑前就要看得见。
    """
    from agentcrew.flow.spec import (
        FlowError,
        load_flow,
        parse_flow_yaml,
        validate_flow,
    )

    try:
        candidate = Path(source)
        if candidate.suffix in (".yaml", ".yml") and candidate.is_file():
            spec = parse_flow_yaml(candidate.read_text(encoding="utf-8"))
        else:
            spec = load_flow(settings.output_dir, source)
    except (OSError, FlowError) as exc:
        raise ValueError(str(exc)) from exc
    errors = validate_flow(spec)
    if errors:
        raise ValueError("流程不合法：" + "；".join(errors))
    return spec


def _cmd_resume(args: argparse.Namespace) -> int:
    """``resume <run_id>``：先比对配置快照，不一致时要求 ``--force``（spec §13.3）。"""
    invalid = _invalid_numbers(args)
    if invalid is not None:
        return _fail(invalid)
    # 续跑样例时必须挂回同一套夹具，否则「已完成的任务能读到、未完成的读不到」。
    # 目标不参与：resume 的目标永远取 record.project_goal。
    _, sample_error = _apply_sample(args)
    if sample_error is not None:
        return _fail(sample_error)
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
        record = CheckpointStore(run_dir).load_record()
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    differences = _config_diff(record.config_snapshot, settings)
    if differences and not args.force:
        print("配置已变动，resume 的结果与首次运行不可比：", file=sys.stderr)
        for line in differences:
            print(f"  {line}", file=sys.stderr)
        print("确认无误请加 --force 重试。", file=sys.stderr)
        return EXIT_CODES["config"]
    try:
        # 与 run 同一条目录：续跑时的计划编辑校验也要认得自定义角色。
        catalog = build_catalog(settings)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    console = Console()
    gate, prompter, downgraded = _resolve_gate(
        settings.gate, interactive=_stdin_isatty(), console=console, catalog=catalog,
        approval_mode=settings.approval_mode)
    if downgraded:
        print(
            "非 TTY（管道 / CI）：--gate 含人工卡点但无法交互，已显式降级为 "
            "gate=none（不会向任何人提问）。",
            file=sys.stderr,
        )
    settings = settings.model_copy(update={"gate": gate})
    hook = _BoardHook(record.project_goal, enabled=not args.no_live, console=console)
    try:
        crew = Crew.resume_from(
            settings, args.run_id, prompter=prompter, on_event=hook, catalog=catalog)
    except (OSError, ValueError) as exc:
        return _fail(f"无法恢复 run {args.run_id}：{exc}")
    hook.bind(crew)
    try:
        return _drive(crew, crew.resume, hook=hook, console=console)
    except ResumeLockError as exc:
        # R17：两个进程同时续跑同一个 run —— 给可读提示，不打印 traceback。
        hook.stop()
        return _fail(f"resume 冲突：{exc}")
    except (OSError, ValueError) as exc:
        # resume 的用户级失败模式是「run 目录与配置/清单不一致」（复制来的 run、
        # 手工改过的 run.json、被清掉一半的 artifacts/）：这类问题要在完整链路
        # 里才暴露（如 ArtifactStore.load_manifest 的 run_id 校验），以配置错误
        # 退出并给出可读消息，好过让用户对着 traceback 猜。
        hook.stop()
        return _fail(f"resume 失败（run 目录与清单/配置不一致）：{exc}")


def _drive(
    crew: Crew,
    run_coro: Callable[[], Coroutine[Any, Any, RunRecord]],
    *,
    hook: _BoardHook,
    console: Console,
) -> int:
    """执行一次 ``run`` / ``resume``，把结束方式翻译成退出码（spec §7.4）。"""
    try:
        record = asyncio.run(run_coro())
    except PlanFailedError as exc:
        hook.stop()
        print("拆解连续未通过校验（退出码 2）：", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_CODES["plan"]
    except ModelFactoryError as exc:
        # ContextBudgetError 是 ModelFactoryError 的子类，一并在这里映射为配置错误。
        hook.stop()
        return _fail(f"配置错误：{exc}")
    except RunCancelled as exc:
        hook.stop()
        print(str(exc))
        _print_resume_hint(crew)
        return EXIT_CODES["cancelled"]
    except KeyboardInterrupt:
        # Crew 已把 checkpoint 落盘为 INTERRUPTED（spec §13.4）；这里只报断点。
        hook.stop()
        _print_resume_hint(crew, prefix="运行被 Ctrl-C 中断，断点已保存")
        return EXIT_CODES["cancelled"]
    hook.stop()
    if record.status is RunStatus.PLANNED:
        console.print(
            Text(f"计划已生成（未执行）：任务已落盘。可用 resume {record.run_id} 续跑执行。"),
            soft_wrap=True)
        return _run_exit_code(crew, record)
    console.print(Text(crew.delivery_summary(record)), soft_wrap=True)
    return _run_exit_code(crew, record)


def _run_exit_code(crew: Crew, record: RunRecord) -> int:
    """结束状态 → 退出码；数字全部取自 :data:`EXIT_CODES`。

    判定顺序与 ``Crew._exit_code``（``run_finished`` 事件里 ``exit_code`` 的来源）
    保持同一口径：停滞 → 终局状态 → 失败。状态先于统计，是因为**两条路径必须给出
    同一个数**：一个带失败任务、随后又被取消的 run，审计记录写 130，进程就不能退
    1。``COMPLETED`` 蕴含「无失败」，由 ``Crew._final_status`` 保证（有 failed /
    blocked / skipped 或未终结时它返回 ``FAILED``）。
    """
    if crew.stalled:
        return EXIT_CODES["stalled"]
    if record.status is RunStatus.COMPLETED:
        return EXIT_CODES["ok"]
    if record.status is RunStatus.PLANNED:
        return EXIT_CODES["ok"]
    if record.status in (RunStatus.CANCELLED, RunStatus.INTERRUPTED):
        return EXIT_CODES["cancelled"]
    # R4：审批挂起是正常中间态，与 Crew._exit_code 同一口径（复用 ok=0，
    # 不改 EXIT_CODES 字典——test_crew.py 对它做精确相等断言）。
    if record.status is RunStatus.AWAITING_APPROVAL:
        return EXIT_CODES["ok"]
    return EXIT_CODES["failed"]


def _print_resume_hint(crew: Crew, *, prefix: str = "可续跑本次 run") -> None:
    """打印 ``resume <run_id>`` 命令；已取消的 run 改为明说「无需 resume」。

    计划卡点选 ``q``（cancel）与 ``s``（save_exit）都抛 ``RunCancelled``，但落盘
    状态不同（``CANCELLED`` / ``INTERRUPTED``）——只有后者值得续跑，对前者喊
    ``resume`` 只会让人白跑一次。run 目录还没建出来时只报断点。
    """
    checkpoint = crew.checkpoint
    if checkpoint is None:
        print(f"{prefix}（run 目录尚未生成）", file=sys.stderr)
        return
    try:
        status = checkpoint.load_record().status
    except (OSError, ValueError):
        status = None
    if status is RunStatus.CANCELLED:
        print(f"{prefix}：本次 run 已取消（cancelled），无需 resume", file=sys.stderr)
        return
    print(f"{prefix}：python main.py resume {checkpoint.run_dir.name}", file=sys.stderr)


# ── status / roles / clean / cancel ───────────────────────────────────────


def _cmd_status(args: argparse.Namespace) -> int:
    """打印 ``run.json`` 记录 + ``tasks.json`` 任务表 + ledger 统计。

    ledger 读取端本来就宽容（文件不存在 = 零事件，见 ``Ledger.events``），这里
    直接读、不预先判存在。
    """
    try:
        settings = _settings_from(args)
        # R30：只结算本次查看的 run——status 不该顺手改别的 run。
        _expire_overdue_approvals(settings, run_id=args.run_id)
        run_dir = _run_dir(settings, args.run_id)
        checkpoint = CheckpointStore(run_dir)
        record = checkpoint.load_record()
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    console = Console()
    ledger = Ledger(
        run_dir / "ledger.jsonl", run_id=record.run_id,
        secrets=settings.secret_values())
    _out(console, f"━━━ AgentCrew run · {record.run_id} ━━━")
    _out(console, f"目标    {record.project_goal}")
    _out(console, f"状态    {record.status.value}")
    _out(
        console,
        f"创建    {record.created_at:%Y-%m-%d %H:%M:%S}"
        f"    更新    {record.updated_at:%Y-%m-%d %H:%M:%S}",
    )
    _out(
        console,
        f"ledger  {sum(1 for _ in ledger.events())} 条事件"
        f"    llm_call {ledger.count('llm_call')} 次",
    )
    board = None
    try:
        board = checkpoint.load_board()
    except (OSError, ValueError) as exc:
        _out(console, f"任务    （tasks.json 不可读：{exc}）")
    if board is not None:
        _out(console, Crew(settings).delivery_summary(record))
        view = BoardView(record.run_id, record.project_goal,
                         enabled=False, console=console)
        console.print(view.render(board, record))
    # R4：审批挂起的 run 在 status 里就能看到待办，不用先翻 ledger。
    approvals = ApprovalStore(run_dir).all()
    if approvals:
        pending = sum(1 for item in approvals if item.status == "pending")
        _out(
            console,
            f"审批单  {len(approvals)} 条    待裁决 {pending} 条"
            + (f"    （python main.py approvals {record.run_id}）" if pending else ""),
        )
    # R19：主管建议不参与裁决，但必须「有处可去」——否则它们只埋在 gate_result
    # 里没人看。status 是查看 run 的第一入口，在这里汇总最近几条。
    suggestion_events = list(ledger.events("supervisor_suggestions"))
    if suggestion_events:
        total = sum(
            len(event.data.get("suggestions") or [])
            for event in suggestion_events
        )
        task_count = len({
            event.data.get("task_id") for event in suggestion_events})
        _out(console, f"主管建议  {total} 条（{task_count} 个任务，不阻塞）")
        shown = 0
        for event in reversed(suggestion_events):
            data = event.data
            for text in (data.get("suggestions") or []):
                if shown >= 5:
                    _out(console, "  … 其余建议见 ledger 的 supervisor_suggestions 事件")
                    break
                _out(
                    console,
                    f"  · {data.get('task_id', '?')} "
                    f"[{data.get('supervisor', '?')} · 第{data.get('round', '?')}轮 · "
                    f"{data.get('verdict', '?')}] {text}",
                )
                shown += 1
            if shown >= 5:
                break
    return EXIT_CODES["ok"]


def _cmd_approvals(args: argparse.Namespace) -> int:
    """R4：列出一个 run 的全部审批单（跨进程可读）。"""
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    store = ApprovalStore(run_dir)
    store.expire_overdue()          # R30：先结算超时，展示的才是真实状态
    requests = store.all()
    if not requests:
        print(f"run {args.run_id} 没有审批单")
        return EXIT_CODES["ok"]
    console = Console()
    _out(console, f"━━━ 审批单 · {args.run_id} ━━━")
    for item in requests:
        action = item.decision.get("action") or "-"
        suffix = ""
        if item.status == "pending" and item.expires_at is not None:
            remaining = int((item.expires_at - datetime.now()).total_seconds())
            suffix = (f"  · 剩余 {remaining}s" if remaining > 0
                      else "  · 已超时（下次 status 会标记 expired）")
        elif item.status == "expired" and item.expires_at is not None:
            suffix = f"  · 超时于 {item.expires_at.isoformat(timespec='seconds')}"
        _out(
            console,
            f"{item.id}  {item.checkpoint:<9} {item.status:<9} "
            f"action={action:<10} by={item.decided_by or '-'}{suffix}",
        )
    return EXIT_CODES["ok"]


def _cmd_approve(args: argparse.Namespace) -> int:
    """R4：在**另一个进程**里对审批单写裁决；之后用 ``resume`` 从卡点继续。"""
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    decision: dict[str, Any] = {"action": args.decision}
    if args.decision == "rework":
        decision["task_ids"] = list(args.task_ids or [])
    try:
        updated = ApprovalStore(run_dir).decide(
            args.request_id, decision, decided_by=args.by
        )
    except ApprovalError as exc:
        return _fail(f"裁决失败：{exc}")
    print(f"已裁决 {updated.id}（{updated.checkpoint}）：{updated.decision}")
    print(f"用 python main.py resume {args.run_id} 从卡点继续")
    return EXIT_CODES["ok"]


def _cmd_roles(args: argparse.Namespace) -> int:
    """列出 8 个内置角色：key / 角色 / 职责 / 边界 / 产出（Task 34 的 README 指向这里）。

    逐角色分块，而不是一张五列表：80 列的终端里五列表会把「职责/边界/产出」挤到
    每列十几个字，rich 还会把放不下的 key 截成 ``project_mana…`` —— 本命令存在的
    意义就是让人拿到完整的 key 与契约。每一行都走 ``soft_wrap``：内容只换行、不截断。
    """
    del args  # 无参数：角色表来自 BUILTIN_ROLES
    console = Console()
    from agentcrew.agents.plan import binding_for
    from agentcrew.config import Settings
    try:
        settings = Settings.load()
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    for spec in BUILTIN_ROLES:
        _out(console, f"[{spec.key}] {spec.title} —— {spec.goal}")
        _out(console, f"  职责  {'；'.join(spec.responsibilities)}")
        _out(console, f"  边界  {'；'.join(spec.boundaries)}")
        _out(console, f"  产出  {spec.output_contract}")
        binding = binding_for(spec, settings)
        if binding.agents:
            chain = " + ".join(f"{b.ref}:{b.duty.value}" for b in binding.agents)
            mode = binding.aggregation.mode if binding.aggregation else "single"
            _out(console, f"  执行体  {chain}（汇总：{mode}）")
        _out(console, "")
    _out(
        console,
        f"共 {len(BUILTIN_ROLES)} 个内置角色；用 --gate plan 可在执行前人工确认拆解。",
    )
    return EXIT_CODES["ok"]


def _cmd_docs(args: argparse.Namespace) -> int:
    """C5：文档 RAG 的管理入口——``docs list`` / ``docs search``。

    ``list`` 触发一次增量摄取并打印「每篇文档的块数」——这是「我的手册被
    索引了吗」的直接答案。``search`` 是检索演示：run 内注入用的同一套
    BM25 + 混合打分，给用户一个「放对目录了没有」的快速验证手段。
    """
    import asyncio as _asyncio

    from agentcrew.config import Settings as _Settings
    from agentcrew.knowledge.docs import DocStore
    from agentcrew.knowledge.embeddings import build_embedder
    from agentcrew.models.embedding import embedding_target

    settings = _Settings.load()
    if getattr(args, "output_dir", None):
        settings = settings.model_copy(
            update={"output_dir": Path(args.output_dir)})
    embedder = build_embedder(
        settings, output_dir=settings.output_dir,
        target=embedding_target(settings))
    store = DocStore(
        settings.output_dir / "knowledge" / "docs", embedder=embedder)
    result = _asyncio.run(store.ingest())
    if getattr(args, "docs_action", "") == "list":
        console = Console()
        _out(console, f"文档根目录：{store.docs_dir}")
        _out(console, f"已索引 {result.docs} 篇 / {result.chunks} 块"
                      f"（本次重建 {len(result.rebuilt)}、复用 {len(result.skipped)}）")
        by_doc: dict[str, int] = {}
        for chunk in store._chunks:  # noqa: SLF001 —— 同仓管理命令直接读
            by_doc[chunk.doc] = by_doc.get(chunk.doc, 0) + 1
        for name, count in sorted(by_doc.items()):
            _out(console, f"  {name} × {count} 块")
        return EXIT_CODES["ok"]
    query = args.query
    # query_sync 是同步方法；上面 asyncio.run 只包了 ingest（异步摄取）。
    hits = store.query_sync(
        query, limit=max(1, int(args.limit)),
        vector_weight=settings.docs_rag_vector_weight)
    console = Console()
    if not hits:
        _out(console, f"没有命中（查询：{query!r}）。检查文档是否已放进 "
                      f"{store.docs_dir} 且主题相关。")
        return EXIT_CODES["ok"]
    _out(console, f"查询：{query!r} · 命中 {len(hits)} 条：")
    for rank, hit in enumerate(hits, start=1):
        _out(console, f"{rank}. [{hit.score:.3f}] {hit.chunk.doc}"
                      f" · {hit.chunk.heading}")
        _out(console, f"   {hit.chunk.text[:160]}…")
    return EXIT_CODES["ok"]

def _cmd_mcp_serve(args: argparse.Namespace) -> int:
    """C7：以 MCP server（stdio）暴露编排能力；工具调用走本机 HTTP API。

    密钥来源（按序）：``--api-key`` → ``AGENTCREW_MCP_API_KEY`` →
    ``AGENTCREW_API_KEY``。没有密钥也允许启动（服务端未开认证时可用），
    但开认证的服务会拒绝每个调用——所以这里在 stderr 上提示一次。
    """
    import os
    import sys as _sys

    from agentcrew.mcp.server import ApiBridge, run_stdio

    api_key = (
        args.api_key
        or os.environ.get("AGENTCREW_MCP_API_KEY", "").strip()
        or os.environ.get("AGENTCREW_API_KEY", "").strip()
    )
    if not api_key:
        print(
            "提示：未配置 API 密钥（--api-key 或 AGENTCREW_MCP_API_KEY）。"
            "若服务端开启了认证，所有调用都会被 401 拒绝。",
            file=_sys.stderr,
        )
    bridge = ApiBridge(base_url=args.api_url, api_key=api_key)
    return run_stdio(bridge, _sys.stdin, _sys.stdout)


def _cmd_tools(args: argparse.Namespace) -> int:
    """R24：列出内置工具（名称 / 类别 / 必填入参）。管理页与 API 共用同一份元数据。"""
    from agentcrew.tools.lab import describe_tools

    # P1：run_python 只在 code_exec_enabled 打开时进清单（与 run 内注册表同口径）。
    settings = Settings.load()
    tools = describe_tools(
        code_exec=settings.code_exec_enabled,
        search_provider=settings.search_provider)
    if args.json:
        print(json.dumps({"tools": tools}, ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]
    console = Console()
    for item in tools:
        required = (item["input_schema"] or {}).get("required") or []
        params = "、".join(required) if required else "无必填"
        _out(console, f"[{item['name']}] {item['kind']} —— 必填：{params}")
        _out(console, f"  {item['description']}")
    _out(
        console,
        "单工具试跑：python main.py tool <名称> --args '{\"path\": \"a.txt\"}'"
        "（需先开启工具执行：AGENTCREW_TOOLS_ENABLED=true）",
    )
    return EXIT_CODES["ok"]


def _cmd_tool(args: argparse.Namespace) -> int:
    """R24：单工具试跑——沙箱目录隔离，与 Agent 调用同一条入参校验。

    退出码：``ok``=0；工具运行时失败=1（试跑结果本身，不是 CLI 错误）；
    未开工具执行 / 入参非法 / 未知工具=3（配置类，与其他 ``_fail`` 一致）。
    """
    from agentcrew.config import Settings
    from agentcrew.tools.lab import ToolLabError, run_trial

    try:
        settings = (
            Settings.load(output_dir=Path(args.output_dir))
            if args.output_dir else Settings.load()
        )
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    if not settings.tools_enabled:
        return _fail(
            "工具执行未开启：设置 AGENTCREW_TOOLS_ENABLED=true（或 YAML "
            "tools_enabled: true）后重试；调试页不扩大执行面"
        )
    try:
        payload = json.loads(args.args)
    except json.JSONDecodeError as exc:
        return _fail(f"--args 不是合法 JSON：{exc}")
    if not isinstance(payload, dict):
        return _fail("--args 必须是 JSON 对象")
    from agentcrew.tools.lab import build_backend_from_config

    try:
        backend = build_backend_from_config(
            kind=settings.sandbox_backend,
            image=settings.sandbox_image,
            memory_mb=settings.sandbox_memory_mb,
            cpus=settings.sandbox_cpus,
            allow_network=settings.sandbox_allow_network,
        )
    except Exception as exc:  # noqa: BLE001 —— 配置类错误统一退 3
        return _fail(f"执行后端不可用：{exc}")
    try:
        result = asyncio.run(
            run_trial(args.name, payload, output_dir=settings.output_dir,
                      code_exec=settings.code_exec_enabled,
                      search_provider=settings.search_provider,
                      backend=backend))
    except ToolLabError as exc:
        return _fail(str(exc))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        console = Console()
        state = "成功" if result["ok"] else "失败"
        _out(
            console,
            f"{result['tool']}  {state}  {result['duration_ms']}ms  "
            f"沙箱 {result['sandbox']}",
        )
        if result["result"]:
            _out(console, result["result"])
        if result["error"]:
            _out(console, f"错误：{result['error']}")
    return EXIT_CODES["ok"] if result["ok"] else EXIT_CODES["failed"]


def _cmd_keys(args: argparse.Namespace) -> int:
    """R25：API 密钥管理（add / list / revoke），落盘 ``<output>/_auth/keys.json``。

    明文只在 ``add`` 的输出里出现一次——之后服务端只有 scrypt 哈希。
    """
    from agentcrew.api.rbac import ROLE_SCOPES, default_key_store

    try:
        settings = (
            Settings.load(output_dir=Path(args.output_dir))
            if getattr(args, "output_dir", None) else Settings.load()
        )
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    store = default_key_store(settings.output_dir)
    action = args.keys_action
    console = Console()

    if action == "add":
        run_ids = [item.strip() for item in (args.runs or "").split(",")
                   if item.strip()]
        try:
            record, plaintext = store.add(
                name=args.name, role=args.role, scopes=args.scope,
                run_ids=run_ids, expires_at=args.expires, note=args.note)
        except ValueError as exc:
            return _fail(f"keys add 失败：{exc}")
        _out(console, f"已创建密钥 [{record.name}] 角色 {record.role}（{record.id}）")
        if record.run_ids:
            _out(console, f"  可见 run：{', '.join(record.run_ids)}")
        if record.expires_at:
            _out(console, f"  过期时间：{record.expires_at}")
        _out(console, f"  密钥（只显示这一次）：{plaintext}")
        _out(console, f"  密钥库：{store.path}")
        return EXIT_CODES["ok"]

    if action == "list":
        records = store.load()
        if getattr(args, "json", False):
            payload = [record.model_dump(mode="json", exclude={
                "key_hash", "salt"}) for record in records]
            print(json.dumps({"keys": payload}, ensure_ascii=False, indent=2))
            return EXIT_CODES["ok"]
        if not records:
            _out(console, f"密钥库为空：{store.path}")
            _out(console, "新建：python main.py keys add --name alice --role operator")
            return EXIT_CODES["ok"]
        for record in records:
            state = "已吊销" if record.revoked_at else "有效"
            _out(
                console,
                f"[{record.id}] {record.name}  {record.role}  {state}  "
                f"建 {record.created_at}  用 {record.last_used_at or '—'}",
            )
            scopes = sorted(ROLE_SCOPES.get(record.role, set()) | set(record.scopes))
            _out(console, f"  作用域 {' '.join(scopes)}")
            if record.run_ids:
                _out(console, f"  可见 run {', '.join(record.run_ids)}")
            if record.expires_at:
                _out(console, f"  过期 {record.expires_at}")
        _out(console, f"（{len(records)} 条 · {store.path}）")
        return EXIT_CODES["ok"]

    try:
        record = store.revoke(args.ident)
    except ValueError as exc:
        return _fail(f"keys revoke 失败：{exc}")
    _out(console, f"已吊销 [{record.name}]（{record.id}）")
    return EXIT_CODES["ok"]


def _cmd_flow(args: argparse.Namespace) -> int:
    """R26：流程 YAML 的校验 / 列表 / 示例输出。

    退出码：``ok``=0；流程不合法=2（``plan``——能改的东西，不是配置错误）；
    文件读不了=3。
    """
    from agentcrew.flow.spec import (
        FlowError,
        dump_flow_yaml,
        list_flows,
        parse_flow_yaml,
        sample_flow,
        validate_flow,
    )

    action = args.flow_action
    console = Console()
    if action == "sample":
        _out(console, dump_flow_yaml(sample_flow()))
        return EXIT_CODES["ok"]

    try:
        settings = (
            Settings.load(output_dir=Path(args.output_dir))
            if getattr(args, "output_dir", None) else Settings.load()
        )
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")

    if action == "list":
        items = list_flows(settings.output_dir)
        if getattr(args, "json", False):
            print(json.dumps({"flows": items}, ensure_ascii=False, indent=2))
            return EXIT_CODES["ok"]
        if not items:
            _out(console, f"没有已保存的流程：{settings.output_dir / '_flows'}")
            _out(console, "示例：python main.py flow sample > my-flow.yaml")
            return EXIT_CODES["ok"]
        for item in items:
            state = "合法" if item["errors"] == 0 else f"{item['errors']} 处错误"
            _out(console, f"[{item['name']}] {item['tasks']} 个任务 · {state}")
            if item.get("error"):
                _out(console, f"  {item['error']}")
        return EXIT_CODES["ok"]

    # validate
    try:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(
            encoding="utf-8")
    except OSError as exc:
        return _fail(f"读不到流程文件：{exc}")
    try:
        spec = parse_flow_yaml(text)
    except FlowError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CODES["plan"]
    errors = validate_flow(spec)
    if errors:
        _out(console, f"流程不合法（{len(errors)} 处）：{spec.name}")
        for error in errors:
            _out(console, f"  - {error}")
        return EXIT_CODES["plan"]
    _out(console, f"流程合法：{spec.name} · {len(spec.tasks)} 个任务")
    return EXIT_CODES["ok"]


def _cmd_schedule(args: argparse.Namespace) -> int:
    """定时任务（Automations）：一次性 / 周期日程的增删查与到期触发。

    对齐 WorkBuddy 的 Automations：``add`` 注册日程，``tick`` 由宿主（Studio
    主进程定时器 / 系统 cron）幂等调用、到期后起 ``run`` 子进程。不引入常驻
    daemon —— 谁负责「到点叫 tick」由部署方决定，CLI 本身零后台。
    """
    from agentcrew.cli.schedule import (
        add_schedule,
        load_schedules,
        remove_schedule,
        tick,
    )

    try:
        settings = (
            Settings.load(output_dir=Path(args.output_dir))
            if getattr(args, "output_dir", None) else Settings.load()
        )
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")

    action = args.schedule_action
    console = Console()

    if action == "add":
        kind = "recurring" if args.daily else "once"
        if kind == "once" and not args.at:
            return _fail("一次性日程需要 --at（ISO8601）")
        if kind == "recurring" and not args.daily:
            return _fail("周期日程需要 --daily HH:MM")
        weekdays = [w.strip().upper() for w in args.weekdays.split(",") if w.strip()]
        try:
            sched = add_schedule(
                settings.output_dir,
                goal=args.goal,
                mock=args.mock,
                mode=args.mode,
                kind=kind,
                at=args.at if kind == "once" else None,
                weekdays=weekdays if kind == "recurring" else [],
                time=args.daily if kind == "recurring" else None,
            )
        except ValueError as exc:
            return _fail(str(exc))
        _out(console, f"已注册日程 {sched.id}（{sched.kind}/{sched.mode}），"
                      f"下次触发 {sched.next_run}")
        _out(console, f"  goal: {sched.goal}")
        return EXIT_CODES["ok"]

    if action == "list":
        schedules = load_schedules(settings.output_dir)
        if args.json:
            print(json.dumps(
                {"schedules": [s.model_dump(mode="json") for s in schedules]},
                ensure_ascii=False, indent=2))
            return EXIT_CODES["ok"]
        if not schedules:
            _out(console, f"没有日程：{settings.output_dir / '_schedules'}")
            _out(console, "示例：python main.py schedule add --goal '写周报' "
                          "--daily 09:00 --mode ask")
            return EXIT_CODES["ok"]
        for s in schedules:
            state = "启用" if s.enabled else "已触发/停用"
            when = s.next_run or "—"
            _out(console, f"[{s.id}] {state} · {s.kind}/{s.mode} · 下次 {when}")
            _out(console, f"  {s.goal}")
        return EXIT_CODES["ok"]

    if action == "remove":
        ok = remove_schedule(settings.output_dir, args.schedule_id)
        if not ok:
            return _fail(f"找不到日程：{args.schedule_id}")
        _out(console, f"已删除日程 {args.schedule_id}")
        return EXIT_CODES["ok"]

    # tick
    fired = tick(settings.output_dir)
    if args.json:
        print(json.dumps({"fired": fired}, ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]
    if not fired:
        _out(console, "没有到期日程。")
    else:
        _out(console, f"本轮触发 {len(fired)} 个日程：")
        for item in fired:
            _out(console, f"  [{item['id']}] {item['kind']}/{item['mode']} — {item['goal']}")
    return EXIT_CODES["ok"]


def _cmd_recall(args: argparse.Namespace) -> int:
    """R27：检索历史经验，打印排序结果——手工核对 BM25 / 混合打分的入口。

    ``embedding_enabled`` 时用与 run 内相同的混合打分（同一 ``build_embedder``
    与权重）；未启用或端点失败时自动退回纯 BM25，并在输出里写明用了哪条通道。
    """
    from agentcrew.knowledge.embeddings import EmbeddingError, build_embedder
    from agentcrew.knowledge.experience import ExperienceStore

    try:
        settings = Settings.load()
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    store = ExperienceStore(settings.output_dir)
    experiences = store.all()
    if not experiences:
        _out(Console(), f"经验库为空：{settings.output_dir / 'knowledge'}")
        return EXIT_CODES["ok"]

    doc_vectors = query_vector = None
    channel = "bm25"
    from agentcrew.models.embedding import embedding_target

    embedder = build_embedder(settings, output_dir=settings.output_dir,
                              target=embedding_target(settings))
    if embedder is not None:
        try:
            doc_vectors = asyncio.run(
                embedder.embed([store.haystack(exp) for exp in experiences]))
            query_vector = asyncio.run(embedder.embed([args.query]))[0]
            channel = embedder.name
        except EmbeddingError as exc:
            print(f"embedding 降级为 BM25：{exc}", file=sys.stderr)

    matches = store.query(
        args.query, role=args.role, limit=max(1, args.limit),
        doc_vectors=doc_vectors, query_vector=query_vector,
        vector_weight=settings.retrieval_vector_weight)
    if args.json:
        print(json.dumps({
            "channel": channel, "query": args.query,
            "results": [{"task": exp.task_description, "role": exp.role,
                         "solution": exp.solution_summary} for exp in matches],
        }, ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]
    console = Console()
    _out(console, f"检索「{args.query}」（通道 {channel} · 权重 "
                  f"{settings.retrieval_vector_weight}）：{len(matches)} 条")
    for index, exp in enumerate(matches, start=1):
        _out(console, f"{index}. [{exp.role}] {exp.task_description}")
        _out(console, f"   {exp.solution_summary}")
    return EXIT_CODES["ok"]


def _cmd_switch(args: argparse.Namespace) -> int:
    """把「某角色用哪个执行体」写进 run 目录，resume 时自动恢复。

    进程外的 CLI 改不了正在跑的进程：这里落盘的是**下一次 resume** 的绑定。
    进程内切换由 ``Crew.switch_agent`` 提供（效果相同，且立即对下一次派发生效）。
    """
    import json

    from agentcrew.agents.plan import resolve_plan
    from agentcrew.config import Settings
    from agentcrew.crew.crew import build_catalog
    from agentcrew.roles.binding import Duty

    try:
        settings = Settings.load()
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    if args.output_dir:
        settings = settings.model_copy(update={"output_dir": Path(args.output_dir).resolve()})
    # 与 status / resume / cancel / delete / diff 同一条边界：switch 会往 run 目录
    # 写两个 JSON，run_id 必须先收进 output_dir 内，否则 ``../`` 会写到别处。
    try:
        run_dir = _run_dir(settings, args.run_id)
    except ValueError as exc:
        return _fail(f"非法 run_id：{exc}")
    if not run_dir.is_dir():
        print(f"run 目录不存在：{run_dir}", file=sys.stderr)
        return EXIT_CODES["config"]

    catalog = build_catalog(settings)
    plan = resolve_plan(settings, catalog, args.run_id)
    members = plan.for_role(args.role)
    target = next((a for a in members if a.profile == args.agent), None)
    if target is None and args.agent in settings.agent_profiles:
        # 与引擎一致：允许切到"已定义但当前没绑"的执行体
        target = settings.agent_profiles[args.agent]
    if target is None:
        options = ", ".join(sorted(set(list(settings.agent_profiles) +
                                       [a.profile for a in members]))) or "无"
        print(f"角色 {args.role} 没有可用的 agent：{args.agent}（可选：{options}）",
              file=sys.stderr)
        return EXIT_CODES["config"]

    # 覆盖文件损坏时**不能**当成空对象再写回：那会静默抹掉用户已有的全部覆盖，
    # 且没有任何提示。fail loud，让用户先修复或自行删除。
    def _read_overrides(path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"覆盖文件损坏：{path}（{exc}）；请修复或删除后再 switch") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"覆盖文件格式错误：{path} 必须是 JSON 对象")
        return payload

    if args.task:
        # 任务级覆盖：只影响这一次任务的派发，与角色绑定互不干扰
        path = run_dir / "task_overrides.json"
        try:
            payload = _read_overrides(path)
        except ValueError as exc:
            return _fail(str(exc))
        payload[args.task] = {"role": args.role, "profile": args.agent}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {path}：{args.task}（{args.role}）→ {args.agent}（下次 resume 生效）")
        return EXIT_CODES["ok"]

    path = run_dir / "binding_overrides.json"
    try:
        payload = _read_overrides(path)
    except ValueError as exc:
        return _fail(str(exc))
    kept = [a for a in members
            if a.duty is not Duty.PRIMARY and a.profile != args.agent]
    payload[args.role] = {
        "agents": [{"ref": a.profile, "duty": a.duty.value, "weight": a.weight} for a in kept]
                  + [{"ref": args.agent, "duty": "primary", "weight": 1.0}],
        "aggregation": ({"mode": "primary_with_review", "quorum": 1}
                        if any(a.duty is Duty.REVIEW for a in kept) else None),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {path}：{args.role} → {args.agent}（下次 resume 生效）")
    return EXIT_CODES["ok"]


def _cmd_agents(args: argparse.Namespace) -> int:
    """列出 agent 档案与角色绑定；``--json`` 给 Studio 的绑定矩阵用。

    数据来自「Settings + RoleCatalog + RunPlan」三层，和真正跑任务时的解析
    完全同源——UI 看到的编排必须就是执行时会用的编排。
    """
    from agentcrew.agents.plan import binding_for, resolve_plan
    from agentcrew.config import Settings
    from agentcrew.crew.crew import build_catalog

    try:
        settings = Settings.load()
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    catalog = build_catalog(settings)
    plan = resolve_plan(settings, catalog, "preview")

    roles = []
    for spec in catalog.all():
        binding = binding_for(spec, settings)
        roles.append({
            "role": spec.key,
            "title": spec.title,
            "agents": [{"ref": b.ref, "duty": b.duty.value, "weight": b.weight}
                       for b in binding.agents],
            "aggregation": (binding.aggregation.model_dump(mode="json")
                            if binding.aggregation else None),
            "resolved": [a.model_dump(mode="json") for a in plan.for_role(spec.key)],
        })
    payload = {
        "profiles": [p.model_dump(mode="json") for p in settings.agent_profiles.values()],
        "roles": roles,
    }
    if args.json:
        import json as _json
        print(_json.dumps(payload, ensure_ascii=False))
        return EXIT_CODES["ok"]

    # 导出：生成可直接粘贴进 agentcrew.yaml 的 agents/roles 片段（复用 yaml 依赖）
    if getattr(args, "yaml", False):
        import yaml as _yaml
        export = {
            "agents": {
                "profiles": {
                    key: {k: v for k, v in p.model_dump(mode="json").items() if k != "key" and v not in (None, "", [])}
                    for key, p in settings.agent_profiles.items()
                }
            }
        }
        if settings.role_agents:
            export["roles"] = {
                role: {k: v for k, v in ra.model_dump(mode="json", exclude_none=True).items() if k != "aggregation" or v is not None}
                for role, ra in settings.role_agents.items()
            }
        print(_yaml.safe_dump(export, allow_unicode=True, sort_keys=False))
        return EXIT_CODES["ok"]

    console = Console()
    profiles = payload["profiles"]
    _out(console, f"执行体档案 {len(profiles)} 个：" +
         ("、".join(p["key"] for p in profiles) if profiles else "（未配置）"))
    for item in roles:
        if not item["agents"]:
            continue
        chain = " + ".join(f"{a['ref']}:{a['duty']}" for a in item["agents"])
        mode = (item["aggregation"] or {}).get("mode", "single")
        _out(console, f"  {item['role']:<18} {chain}（汇总：{mode}）")
    return EXIT_CODES["ok"]


def _cmd_eval(args: argparse.Namespace) -> int:
    """``eval``：跑离线评测集，用产物断言 + ``pass^k`` 给出可复现的结论。

    退出码：全部 case 的 K 次运行都通过 → 0；有任一 case 没过 → 1（与「测试失败」
    同一口径）；数据集本身不合法 / 运行环境错误 → 3（配置类）。

    刻意**不**做「部分通过也算过」：评测的价值就在于是非题。想只看趋势就加
    ``--json`` 自己算，别让工具替人下结论。
    """
    from agentcrew.evals import DatasetError, load_dataset, run_dataset

    try:
        cases = load_dataset(args.dataset)
    except DatasetError as exc:
        return _fail(f"数据集不合法：{exc}")
    repeat = max(1, int(getattr(args, "repeat", 1) or 1))
    output_dir = getattr(args, "output_dir", None)
    live = bool(getattr(args, "live", False))
    base = _eval_base_settings(args)
    if isinstance(base, str):  # 参数非法，已经是可展示的错误文案
        return _fail(base)
    variants_raw = getattr(args, "variant", None)
    if variants_raw:
        return _eval_matrix(args, cases, repeat, output_dir, variants_raw, base)
    try:
        report = asyncio.run(run_dataset(
            cases, repeat=repeat, base_settings=base, live=live,
            output_dir=Path(output_dir) if output_dir else None))
    except Exception as exc:  # noqa: BLE001 —— 评测跑不起来是环境/配置问题
        return _fail(f"评测执行失败：{type(exc).__name__}: {exc}")

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        console = Console()
        _out(console, f"评测集 {args.dataset} · {len(cases)} 条 case · "
                      f"每条跑 {repeat} 次")
        grouped = report.by_case()
        for case_id, runs in grouped.items():
            bad = [run for run in runs if not run.ok]
            # ``_out`` 走 ``Text``（rich 的标记会被当字面量），所以这里用纯文本
            # 而不是 [green] 标记——宁可朴素，也不要控制台里出现 "[/]"。
            mark = "PASS" if not bad else "FAIL"
            _out(console, f"  {mark} {case_id}  "
                          f"{len(runs) - len(bad)}/{len(runs)} 次通过")
            for run in bad[:1]:
                for failure in run.failures:
                    _out(console, f"       - {failure.kind}: {failure.detail}")
                if run.error:
                    _out(console, f"       - 运行错误：{run.error}")
        _out(console, "")
        _out(console, f"pass@1 = {report.pass_at_1:.2%} · "
                      f"pass^{repeat} = {report.pass_pow_k:.2%}")
    return EXIT_CODES["ok"] if report.pass_pow_k >= 1.0 else EXIT_CODES["failed"]


def _eval_base_settings(args: argparse.Namespace):
    """C11：评测的基线 Settings——``--live`` 切真实通道、``--max-cost`` 设硬上限。

    返回 ``Settings``，或在参数非法时返回错误文案字符串（调用方 ``_fail``）。
    预算校验刻意放在这里：``nan`` / ``inf`` 与成本比较恒为 False，等于没设上限。
    """
    from agentcrew.config import Settings

    settings = Settings.load()
    max_cost = getattr(args, "max_cost", None)
    if max_cost is not None:
        if isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)):
            return "--max-cost 必须是数值"
        if not math.isfinite(float(max_cost)) or float(max_cost) < 0:
            return "--max-cost 必须是 >= 0 的有限数"
        settings = settings.model_copy(update={"max_cost_usd": float(max_cost)})
    if getattr(args, "live", False):
        settings = settings.model_copy(update={"mock": False})
    return settings


def _eval_matrix(
    args: argparse.Namespace, cases: list, repeat: int,
    output_dir: str | None, variants_raw: list[str], base_settings=None,
) -> int:
    """``eval --variant``：同一批 case × 多套配置，给出可对比的数字。

    退出码与单配置一致：**基线**变体的 pass^k 全过才算 0。其余变体只作对照，
    不参与判定——否则「换便宜档掉了一个点」会让整条命令失败，人就学会忽略它。
    """
    from agentcrew.evals import load_variant_file, run_matrix

    variants: list[tuple[str, dict]] = []
    for item in variants_raw:
        name, sep, path = str(item).partition("=")
        if not sep or not name.strip() or not path.strip():
            return _fail(f"--variant 需要 NAME=FILE 形式：{item!r}")
        try:
            variants.append((name.strip(), load_variant_file(path.strip())))
        except ValueError as exc:
            return _fail(f"变体不合法：{exc}")
    try:
        matrix = asyncio.run(run_matrix(
            variants, cases, repeat=repeat, base_settings=base_settings,
            live=bool(getattr(args, "live", False)),
            output_dir=Path(output_dir) if output_dir else None))
    except ValueError as exc:
        return _fail(f"矩阵评测无法开始：{exc}")
    except Exception as exc:  # noqa: BLE001 —— 跑不起来属环境/配置问题
        return _fail(f"矩阵评测执行失败：{type(exc).__name__}: {exc}")

    data = matrix.to_dict()
    out_path = getattr(args, "matrix_out", None)
    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        console = Console()
        _out(console, f"配置矩阵 · {len(cases)} 条 case × {len(variants)} 个变体 · "
                      f"每条跑 {repeat} 次")
        for item in data["variants"]:
            metrics, delta = item["metrics"], item["delta_vs_baseline"]
            tail = ""
            if delta:
                tail = (
                    f"   Δpass^k {delta['pass_pow_k']:+.2f}"
                    f" · Δcalls {delta['llm_calls']:+.0f}"
                    f" · Δcost ${delta['cost_usd']:+.4f}"
                    f" · Δ修订 {delta['revise_rounds']:+.0f}")
            _out(console, f"  {item['name']:<14} pass^k={metrics['pass_pow_k']:.2%}"
                          f"  calls={metrics['llm_calls']}"
                          f"  cost=${metrics['cost_usd']:.4f}"
                          f"  修订={metrics['revise_rounds']}{tail}")
        if out_path:
            _out(console, f"\n矩阵已写入 {out_path}")
    baseline = matrix.baseline
    return (EXIT_CODES["ok"]
            if baseline is not None and baseline.report.pass_pow_k >= 1.0
            else EXIT_CODES["failed"])


def _cmd_samples(args: argparse.Namespace) -> int:
    """``samples [name]``：列出预设 Crew 样例。

    每个样例一行：名字 / 形状 / 任务数 / 目标摘要，随后给出可直接复制的运行命令。
    逐样例分块而不是大表格 —— 目标句很长，表格会把它压成一行省略号。
    """
    from examples.samples import SAMPLES, get, names

    if args.name:
        try:
            specs = [get(args.name)]
        except KeyError as exc:
            return _fail(str(exc).strip("'\""))
    else:
        specs = [SAMPLES[name] for name in names()]

    if args.json:
        payload = [
            {
                "name": spec.name,
                "title": spec.title,
                "shape": spec.shape,
                "goal": spec.goal,
                "tasks": spec.expected_total_tasks,
                "expected_task_ids": list(spec.expected_task_ids),
                "fixtures": str(spec.fixtures),
            }
            for spec in specs
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]

    console = Console()
    for spec in specs:
        _out(console, f"[{spec.name}] {spec.title} · {spec.shape} · {spec.expected_total_tasks} 个任务")
        _out(console, f"  目标  {spec.goal}")
        _out(console, f"  运行  python main.py run --sample {spec.name} --no-live")
        _out(console, "")
    _out(console, f"共 {len(SAMPLES)} 个样例；全部离线可跑（自动开启 --mock，无需任何 API Key）。")
    return EXIT_CODES["ok"]


def _cmd_clean(args: argparse.Namespace) -> int:
    """按 run_id 倒序保留最近 ``--keep`` 个 run，删除其余（spec §13.5）。

    ``--dry-run`` 只列不删（:func:`agentcrew.runtime.checkpoint.clean` 的真跑与
    干跑共用同一套边界校验）。``--protect-running`` 显式开启活 run 保护：
    库层 ``clean`` 默认不保护以维持 spec §13.5 的既有契约，生产批量清理时
    由 CLI 开关决定是否跳过「有心跳的 running」，僵尸 RUNNING 始终可清理。
    """
    if args.keep < 0:
        return _fail("clean：--keep 必须 >= 0。")
    try:
        settings = _settings_from(args)
        removed = clean(settings.output_dir, keep=args.keep,
                        dry_run=args.dry_run,
                        protect_running=getattr(args, "protect_running", False))
    except (OSError, RuntimeError, ValueError) as exc:
        return _fail(f"clean 失败：{exc}")
    console = Console()
    action = "[dry-run] 将删除" if args.dry_run else "已删除"
    _out(
        console,
        f"{action} {len(removed)} 个 run（保留最近 {args.keep} 个）：{settings.output_dir}",
    )
    for path in removed:
        _out(console, f"  {path.name}")
    if not removed:
        _out(console, "没有需要清理的 run。")
    return EXIT_CODES["ok"]


def _cmd_cancel(args: argparse.Namespace) -> int:
    """给离线 run 置 ``cancelled`` 标记：下次 ``resume`` 直接以 CANCELLED 收尾（spec §13.4）。"""
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
        checkpoint = CheckpointStore(run_dir)
        record = checkpoint.load_record()
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    if record.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
        _out(Console(), f"run {record.run_id} 已结束（{record.status.value}），无需取消。")
        return EXIT_CODES["ok"]
    record.status = RunStatus.CANCELLED
    record.updated_at = datetime.now()
    checkpoint.save_record(record)
    _out(
        Console(),
        f"已把 run {record.run_id} 标记为 cancelled；下次 resume 将直接以 CANCELLED 收尾。",
    )
    return EXIT_CODES["ok"]


def _cmd_delete(args: argparse.Namespace) -> int:
    """删除单个 run 目录（P0-4，不可恢复）。

    打印 run 概要（目标/状态/时间/产物大小），TTY 下询问确认；``running`` 状态
    拒绝删除（退出码 3，提示先 cancel）；``--force`` 跳过确认。删除后打印释放字节数。
    """
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
        checkpoint = CheckpointStore(run_dir)
        record = checkpoint.load_record()
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")

    # running 状态拒绝删除（退出码 3 = config 类错误，与 spec 退出码表一致）
    if record.status is RunStatus.RUNNING:
        return _fail(
            f"run {record.run_id!r} 正在运行中，拒绝删除。"
            f"请先 cancel：python main.py cancel {record.run_id}",
        )

    console = Console()
    artifacts_dir = run_dir / "artifacts"
    artifact_size = sum(
        f.stat().st_size for f in artifacts_dir.rglob("*") if f.is_file()
    ) if artifacts_dir.is_dir() else 0
    _out(console, f"━━━ 将删除 run · {record.run_id} ━━━")
    _out(console, f"目标    {record.project_goal}")
    _out(console, f"状态    {record.status.value}")
    _out(
        console,
        f"创建    {record.created_at:%Y-%m-%d %H:%M:%S}"
        f"    更新    {record.updated_at:%Y-%m-%d %H:%M:%S}",
    )
    _out(console, f"产物    {artifact_size:,} 字节    目录    {run_dir}")

    if not args.force and sys.stdin.isatty():
        try:
            answer = input("确认删除？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            _out(console, "已取消删除。")
            return EXIT_CODES["ok"]

    try:
        freed = delete_run(settings.output_dir, record.run_id)
    except (OSError, RuntimeError, ValueError) as exc:
        return _fail(f"删除失败：{exc}")
    _out(console, f"已删除 run {record.run_id}，释放 {freed:,} 字节。")
    return EXIT_CODES["ok"]


def _is_loopback_host(host: str) -> bool:
    """``--host`` 是否只绑回环地址（决定「无密钥能否启动」）。

    空串 / 通配地址（``0.0.0.0`` / ``::``）**不算**回环：asyncio 会把它们解释成
    「监听所有网卡」，那正是最需要认证的形态。主机名按字面判断（``localhost``），
    不做 DNS 解析——解析结果会随环境变化，安全判定不能依赖它。
    """
    name = (host or "").strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not name:
        return False
    if name in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _cmd_serve(args: argparse.Namespace) -> int:
    """启动 HTTP API + SSE 事件流服务器（V2 第 5 项；E-3 可选认证/限流）。"""
    import asyncio

    from agentcrew.api.auth import RateLimiter
    from agentcrew.api.server import ApiServer

    # 端口由 argparse 保证是 int，但不保证落在合法区间：-1 / 70000 会让
    # asyncio.start_server 抛 OverflowError，端口被占用抛 OSError，两者此前都以
    # 裸 traceback 结束（serve 是最常用的启动命令）。0 虽能绑定随机端口，打印的
    # 地址却是 :0，同样不可用。统一在启动前给出「配置错误」而非栈崩。
    if not 1 <= args.port <= 65535:
        return _fail(f"serve：--port 必须是 1–65535，实际 {args.port}。")

    async def _serve() -> int:
        try:
            settings = _settings_from(args)
        except (OSError, ValueError) as exc:
            return _fail(f"serve：配置错误：{exc}")
        _reconcile_on_startup(settings)
        _expire_overdue_approvals(settings)   # R30：启动时结算超时审批单
        # P0：认证默认开启（``api_auth_enabled`` 默认 True），且 fail-closed。
        #
        # 判定只看两件事：「有没有密钥」与「要不要认证」，不去猜「是不是显式设置的」：
        #   1) 有 legacy/api_keys 或 RBAC 密钥库里有活跃密钥 → 认证必开；
        #   2) 没有密钥，且配置要求认证（默认状态）→ **退 3**，回环地址也一样：
        #      否则「我明明开了认证」和「忘了配密钥」在日志里长得一样，还顺手
        #      起了一个匿名服务（一条 `AGENTCREW_API_AUTH_ENABLED=1` 的用例会
        #      把进程挂死在启动后的等待上，就是这么暴露出来的）；
        #   3) 没有密钥，且显式关掉认证（``api_auth_enabled=false``）→ 只允许绑
        #      回环地址并打警告；绑非回环仍然退 3（关认证不能顺手对外）。
        api_keys = list(settings.api_keys)
        cli_key = getattr(args, "api_key", None)
        if cli_key:
            api_keys.append(cli_key)
        # R25：密钥库里有活跃密钥就启用多密钥认证（fail-closed）；legacy
        # ``api_keys`` 仍等价于 admin——单密钥部署不需要改任何配置。
        from agentcrew.api.rbac import MultiKeyAuth, default_key_store

        store = default_key_store(settings.output_dir)
        has_rbac_keys = any(
            record.revoked_at is None for record in store.load())
        loopback = _is_loopback_host(args.host)
        if api_keys or has_rbac_keys:
            auth = MultiKeyAuth(store, legacy_keys=api_keys, required=True)
        elif settings.api_auth_enabled:
            return _fail(
                "serve：已启用 API 认证但没有配置任何密钥，拒绝以匿名方式启动。\n"
                "  任选其一：\n"
                "    1) python main.py keys add --name admin --role admin"
                "（推荐，明文只打印一次）；\n"
                "    2) 配置 agentcrew.yaml 的 api_keys 或环境变量 AGENTCREW_API_KEYS；\n"
                "    3) serve --api-key <KEY> 临时加一个 admin 密钥。\n"
                "  仅本机开发且明确不要认证时，显式设 api_auth_enabled=false"
                "（或 AGENTCREW_API_AUTH_ENABLED=false）。")
        elif not loopback:
            return _fail(
                f"serve：已显式关闭 API 认证，却要绑非回环地址 {args.host}；"
                "拒绝以匿名方式对外提供服务。\n"
                "  只在本机开发时用 --host 127.0.0.1；对外必须先配密钥"
                "（keys add / AGENTCREW_API_KEYS）。")
        else:
            auth = None
            print(
                "  ! 警告：已显式关闭认证且未配置任何 API Key，服务以**匿名**方式运行；"
                f"仅因绑定回环地址（{args.host}）才放行。",
                file=sys.stderr,
            )
        if auth is not None:
            source = (f"RBAC 密钥库 {store.path}" if has_rbac_keys
                      else "legacy api_keys")
            print(f"  auth                      已启用（{source}）")
        if (settings.api_rate_limit_enabled
                and settings.api_rate_limit_max > 0
                and settings.api_rate_limit_window < 1):
            return _fail("serve：api_rate_limit_window 必须 >= 1（否则限流静默失效）。")
        rate_limiter = (
            RateLimiter(
                max_requests=settings.api_rate_limit_max,
                window_seconds=settings.api_rate_limit_window,
            )
            if settings.api_rate_limit_enabled
            else None
        )
        # A4/A5：构造 ModelFactory 供 /providers 健康查询与 /test 测速。
        from agentcrew.models.factory import ModelFactory
        model_factory = ModelFactory(settings)
        server = ApiServer(
            host=args.host, port=args.port, output_dir=settings.output_dir,
            settings=settings,
            auth=auth, rate_limiter=rate_limiter,
            model_factory=model_factory,
        )
        try:
            await server.start()
        except (OSError, OverflowError, ValueError) as exc:
            return _fail(f"serve 启动失败（{args.host}:{args.port}）：{exc}")
        print(f"AgentCrew API server listening on http://{args.host}:{args.port}")
        print(f"  Web UI: http://{args.host}:{args.port}/")
        print("  GET  /health              健康检查")
        print("  GET  /runs/{id}/status    运行状态")
        print("  GET  /runs/{id}/events    SSE 事件流")
        print("  POST /runs/{id}/events    注入事件（调试）")
        if auth is not None:
            print("  认证：已启用 API Key（Bearer / X-API-Key）")
        if rate_limiter is not None:
            print(
                f"  限流：每 IP {settings.api_rate_limit_max} 次 / "
                f"{settings.api_rate_limit_window}s")
        print("Ctrl-C / SIGTERM 停止。")
        # D-1：优雅停机。此前只捕获 KeyboardInterrupt（SIGINT），而 systemd /
        # `docker stop` 发的是 SIGTERM——Python 默认直接终止进程，`server.stop()`
        # 不执行：在途 run 不收尾、ledger 未 flush、SSE 连接硬断。
        # Windows 的 Proactor 事件循环不支持 add_signal_handler，回退到
        # signal.signal（主线程可用）；两者都失败时保留 KeyboardInterrupt 兜底。
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _request_stop(*_args: object) -> None:
            loop.call_soon_threadsafe(stop_event.set)

        for _sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(_sig, stop_event.set)
            except (NotImplementedError, RuntimeError, ValueError):
                try:
                    signal.signal(_sig, _request_stop)
                except (ValueError, OSError):
                    pass  # 非主线程等场景：只保留 KeyboardInterrupt 兜底
        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass
        finally:
            await server.stop()
        return EXIT_CODES["ok"]

    return asyncio.run(_serve())


def _cmd_doctor(args: argparse.Namespace) -> int:
    """``doctor``：跑环境/配置/依赖/连通性检查，打印健康报告。

    退出码：全部 pass 或仅有 warn → 0；有任何 fail → 3（``config``）。配置本身
    加载失败时也退 3，并在报告里给一条 ``config: fail``，而不是抛 traceback。
    """
    if getattr(args, "config", None):
        # 配置文件路径走 AGENTCREW_CONFIG 环境变量（config.py 的唯一读取点）。
        os.environ["AGENTCREW_CONFIG"] = args.config
    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:
        report = DoctorReport(
            ok=False,
            checks=[DoctorCheck(
                name="config", status="fail", message=f"配置加载失败：{exc}")],
        )
        if args.json:
            print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
        else:
            print_doctor_report(report)
        return EXIT_CODES["config"]
    report = asyncio.run(run_diagnostics(settings))
    if args.json:
        print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
    else:
        print_doctor_report(report)
    return EXIT_CODES["ok"] if report.ok else EXIT_CODES["config"]


def _cmd_diff(args: argparse.Namespace) -> int:
    """``diff <run_a> <run_b>``：对比两个 run 的配置/任务/产物/事件差异。

    退出码：两个 run 都存在 → 0（diff 是信息性命令，有差异也不报错）；任一 run
    不存在 → 1（``failed``）。``--json`` 输出结构化差异供脚本解析。
    """
    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CODES["config"]

    def _load(run_id: str) -> tuple[CheckpointStore, RunRecord]:
        run_dir = _run_dir(settings, run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run 不存在: {run_id}")
        store = CheckpointStore(run_dir)
        return store, store.load_record()

    try:
        store_a, record_a = _load(args.run_id_a)
        store_b, record_b = _load(args.run_id_b)
    except (OSError, ValueError) as exc:
        print(f"无法读取 run：{exc}", file=sys.stderr)
        return EXIT_CODES["failed"]

    diff = diff_runs(record_a, record_b, store_a, store_b)
    if args.json:
        print(json.dumps(diff.model_dump(), ensure_ascii=False, indent=2))
    else:
        print_run_diff(diff)
    return EXIT_CODES["ok"]


def _cmd_providers(args: argparse.Namespace) -> int:
    """``providers [name]``：列出合并后的 provider 目录（内置 + 自定义）。

    显示每个 provider 的 kind（builtin/custom）、implementation、base_url、
    api_key_env（环境变量名，不显示值）、能力位摘要、建议模型、category、
    api_key_url。``--json`` 输出结构化 JSON；``name`` 参数只显示单个 provider。
    ``--category`` / ``--search`` 可组合（AND）过滤。
    """
    import os

    from agentcrew.models.config import build_directory

    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")

    directory = build_directory(
        custom=settings.custom_providers,
        overrides=settings.provider_overrides,
    )
    builtin_names = set(PROVIDER_PRESETS_NAMES)

    def _entry(name: str, preset) -> dict[str, Any]:
        env_name = preset.api_key_env or ""
        key_configured = bool(env_name) and bool(os.environ.get(env_name, "").strip())
        return {
            "name": name,
            "kind": "builtin" if name in builtin_names else "custom",
            "implementation": preset.implementation,
            "base_url": preset.base_url,
            "api_key_env": env_name,
            "key_configured": key_configured,
            "capabilities": preset.capabilities.model_dump(),
            "suggested_models": list(preset.suggested_models),
            "command": preset.command,
            "category": preset.category,
            "family": preset.family,
            "search_aliases": list(preset.search_aliases),
            "endpoint_candidates": list(preset.endpoint_candidates) if preset.endpoint_candidates else None,
            "api_key_url": preset.api_key_url,
        }

    if args.name:
        if args.name not in directory:
            return _fail(f"未知 provider：{args.name!r}")
        entries = [_entry(args.name, directory[args.name])]
    else:
        entries = [_entry(n, directory[n]) for n in sorted(directory)]

    # ── 过滤：--category 与 --search 可组合（AND）─────────────────────────
    category = getattr(args, "category", None)
    keyword = getattr(args, "search", None)
    if category or keyword:
        kw = keyword.lower().strip() if keyword else ""
        filtered: list[dict[str, Any]] = []
        for e in entries:
            if category and e["category"] != category:
                continue
            if kw:
                haystack = [e["name"].lower()] + [a.lower() for a in e["search_aliases"]]
                if not any(kw in h for h in haystack):
                    continue
            filtered.append(e)
        entries = filtered

    if args.json:
        print(json.dumps(
            entries if args.name else {"providers": entries},
            ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]

    console = Console()
    if not entries:
        print("没有匹配的 provider（试试放宽 --category / --search 条件）。",
              flush=True)
        return EXIT_CODES["ok"]
    for e in entries:
        cat_tag = f"  [{e['category']}]" if e["category"] else ""
        _out(console, f"[{e['kind']}] {e['name']}  ({e['implementation']}){cat_tag}")
        _out(console, f"  base_url       {e['base_url']}")
        _out(console, f"  api_key_env    {e['api_key_env'] or '(无)'}"
                       + ("  [已配置]" if e["key_configured"] else "  [未配置]"))
        caps = e["capabilities"]
        caps_summary = (
            f"json_schema={caps['supports_json_schema']} "
            f"json_object={caps['supports_json_object']} "
            f"tools={caps['supports_tool_calls']}")
        _out(console, f"  capabilities   {caps_summary}")
        if e["suggested_models"]:
            _out(console, f"  suggested      {', '.join(e['suggested_models'])}")
        if e["endpoint_candidates"]:
            n = len(e["endpoint_candidates"])
            _out(console, f"  endpoints      {n} 端点候选")
        if e["api_key_url"]:
            _out(console, f"  api_key_url    {e['api_key_url']}")
        _out(console, "")
    return EXIT_CODES["ok"]


def _cmd_models(args: argparse.Namespace) -> int:
    """``models <provider>``：按需拉取该 provider 的 /models 端点。

    错误分类清晰（network/auth/not_found/parse/timeout），不栈崩溃。
    ``--verbose`` 显示端点候选尝试过程；``--json`` 附带 endpoints_tried /
    successful_endpoint 元数据。
    """
    from agentcrew.models.config import build_directory, parse_alias
    from agentcrew.models.discovery import (
        ModelDiscoveryError,
        candidate_urls,
        fetch_models,
    )

    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")

    directory = build_directory(
        custom=settings.custom_providers,
        overrides=settings.provider_overrides,
    )
    preset = directory.get(args.provider)
    if preset is None:
        return _fail(f"未知 provider：{args.provider!r}")

    # 构造一个临时 binding：model 名用 suggested_models 第一个或 "test"。
    dummy_model = preset.suggested_models[0] if preset.suggested_models else "test"
    binding = parse_alias(
        f"{args.provider}:{dummy_model}",
        overrides=settings.provider_overrides,
        custom=settings.custom_providers,
    )
    api_key = settings.api_key_for(args.provider) or ""

    # 预计算端点候选列表（用于展示 / JSON 元数据）。
    endpoints_tried = candidate_urls(binding)
    verbose = getattr(args, "verbose", False)

    try:
        models = asyncio.run(fetch_models(binding, api_key=api_key))
    except ModelDiscoveryError as exc:
        payload: dict[str, Any] = {"provider": args.provider, "error": {
            "kind": exc.kind, "message": str(exc)}}
        if endpoints_tried:
            payload["endpoints_tried"] = endpoints_tried
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"[{exc.kind}] {args.provider} 模型发现失败：{exc}",
                file=sys.stderr)
        return EXIT_CODES["config"]

    # fetch_models 按候选顺序尝试、第一个成功即返回；主端点即首条候选。
    successful_endpoint = endpoints_tried[0] if endpoints_tried else None

    if args.json:
        print(json.dumps(
            {"provider": args.provider,
             "models": models,
             "endpoints_tried": endpoints_tried,
             "successful_endpoint": successful_endpoint},
            ensure_ascii=False, indent=2))
    else:
        console = Console()
        if verbose and endpoints_tried:
            _out(console, f"provider {args.provider}：{len(endpoints_tried)} 个端点候选")
            for i, url in enumerate(endpoints_tried, 1):
                _out(console, f"  [{i}] {url}")
            _out(console, f"  → 成功端点：{successful_endpoint}")
        _out(console, f"provider {args.provider}：{len(models)} 个模型")
        for m in models:
            _out(console, f"  {m}")
    return EXIT_CODES["ok"]


def _cmd_config(args: argparse.Namespace) -> int:
    """``config export`` / ``config import <file>``：配置导入导出（A6）。"""
    if getattr(args, "config_action", None) == "export":
        return _cmd_config_export(args)
    if getattr(args, "config_action", None) == "import":
        return _cmd_config_import(args)
    print("用法：python main.py config export|import ...", file=sys.stderr)
    return EXIT_CODES["config"]


def _cmd_config_export(args: argparse.Namespace) -> int:
    """导出当前合并配置为 YAML（密钥字段已脱敏）。"""
    try:
        settings = _settings_from(args)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    snap = settings.config_snapshot()
    # 只导出可再导入的段，不导出 output_dir / log_level 等运行环境字段。
    export_doc: dict[str, Any] = {
        "config_version": 1,
        "models": {
            "default": snap.get("default_model"),
            "role_bindings": snap.get("role_bindings") or {},
            "providers": snap.get("provider_overrides") or {},
            "custom_providers": snap.get("custom_providers") or {},
        },
        "mcp_servers": snap.get("mcp_servers") or {},
        "gate_config": snap.get("gate_config") or {},
        "tool_permissions": snap.get("tool_permissions") or {},
        # P2-4：guardrails 属于安全配置，导出/导入必须带上——否则
        # `config export` 会静默丢掉整套拦截规则，是最不该出现的「丢配置」。
        "tool_guards": snap.get("tool_guards") or [],
        # R10：换人映射只在非空时导出（默认空映射不写回 yaml）。
        "role_fallback": snap.get("role_fallback") or {},
    }
    # R3：两个能力开关只在显式开启时导出 —— 默认配置的导出文档与改造前一致，
    # 也不会把「关」当成用户配置写回 agentcrew.yaml。
    for key in ("tools_enabled", "entity_memory_enabled", "peer_dialogue_enabled",
                "peer_consult_capture_enabled", "supervisor_review_enabled"):
        if snap.get(key):
            export_doc[key] = True
    if snap.get("max_peer_messages") not in (None, 8):
        export_doc["max_peer_messages"] = int(snap["max_peer_messages"])
    # R6：委派治理只在偏离默认值时导出，默认配置的导出文档保持不变。
    if snap.get("delegation_mode") and snap["delegation_mode"] != "any":
        export_doc["delegation_mode"] = snap["delegation_mode"]
    for key, default in (("max_delegation_depth", 2), ("max_dynamic_tasks", 20),
                         ("max_subtask_corrections", 1),
                         ("max_supervisor_reassignments", 1)):
        if snap.get(key) not in (None, default):
            export_doc[key] = int(snap[key])
    # R9：主管审核的覆盖范围与主管角色，同样只在偏离默认值时导出。
    if snap.get("supervisor_scope") and snap["supervisor_scope"] != "delegated":
        export_doc["supervisor_scope"] = snap["supervisor_scope"]
    if (snap.get("supervisor_role")
            and snap["supervisor_role"] != "project_manager"):
        export_doc["supervisor_role"] = snap["supervisor_role"]
    # R14：多接收端优先——配了它就不再导出单接收端那组，避免两者同时出现让人
    # 以为会推两遍。**每条 target 的 secret 一律剔除**，同 webhook_secret 的处理。
    if snap.get("webhooks"):
        export_doc["webhooks"] = [
            {key: value for key, value in target.items() if key != "secret"}
            for target in snap["webhooks"]
        ]
    # R12：Webhook 只在配了 URL 时导出整组；密钥**永不导出**（它会随文件流传）。
    elif snap.get("webhook_url"):
        export_doc["webhook_url"] = snap["webhook_url"]
        export_doc["webhook_events"] = list(snap.get("webhook_events") or [])
        if snap.get("webhook_timeout_seconds") not in (None, 5.0):
            export_doc["webhook_timeout_seconds"] = float(
                snap["webhook_timeout_seconds"])
        if snap.get("webhook_max_retries") not in (None, 2):
            export_doc["webhook_max_retries"] = int(snap["webhook_max_retries"])
    # R18：限流三参数补齐配置通道，只在偏离默认时导出（默认导出文档保持不变）。
    if snap.get("api_rate_limit_enabled"):
        export_doc["api_rate_limit_enabled"] = True
    if snap.get("api_rate_limit_max") not in (None, 100):
        export_doc["api_rate_limit_max"] = int(snap["api_rate_limit_max"])
    if snap.get("api_rate_limit_window") not in (None, 60):
        export_doc["api_rate_limit_window"] = int(snap["api_rate_limit_window"])
    if snap.get("approval_mode") and snap["approval_mode"] != "interactive":
        export_doc["approval_mode"] = snap["approval_mode"]
    # R20：单步断点只在开启时导出（默认导出文档保持不变）。
    if snap.get("debug_step"):
        export_doc["debug_step"] = True
    # R51：C1/C2 的执行与搜索配置同样只在偏离默认值时导出——否则 `config export`
    # 会静默丢掉「代码在哪跑」这类关键安全设置（同 R18 / P2-4 的教训）。
    for key, default in (
        ("sandbox_backend", "local"),
        ("sandbox_image", "python:3.12-slim"),
        ("sandbox_memory_mb", 512),
        ("sandbox_cpus", 1.0),
        ("sandbox_allow_network", False),
        ("search_provider", ""),
        ("search_max_results", 5),
        ("search_timeout_seconds", 15.0),
    ):
        value = snap.get(key)
        if value not in (None, default):
            export_doc[key] = value
    # 去掉空段
    export_doc["models"] = {
        k: v for k, v in export_doc["models"].items() if v
    }
    for key in ("mcp_servers", "gate_config", "tool_permissions",
                "tool_guards", "role_fallback"):
        if not export_doc[key]:
            del export_doc[key]
    text = yaml.safe_dump(export_doc, allow_unicode=True, sort_keys=False)
    if getattr(args, "output", None):
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"已导出到 {args.output}")
    else:
        print(text, end="")
    return EXIT_CODES["ok"]


def _cmd_config_import(args: argparse.Namespace) -> int:
    """从 YAML 文件导入配置：校验 → 备份 → 合并写入 agentcrew.yaml。"""
    import re

    src = Path(args.file)
    if not src.is_file():
        return _fail(f"导入文件不存在：{src}")
    try:
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return _fail(f"无法读取导入文件：{exc}")
    if not isinstance(data, dict):
        return _fail("导入文件必须是 YAML 映射")

    # 密钥字段作为值出现 → 拒绝（密钥只从环境变量读）。
    secret_re = re.compile(
        r"key|token|secret|password|passwd|credential|auth", re.IGNORECASE)
    # 环境变量名约定：字母/下划线开头，后跟字母/数字/下划线。用于识别
    # 「把真实密钥误填进 api_key_env」——那样会被原样写进 agentcrew.yaml
    # （明文落盘红线），且永不生效（os.environ.get(<密钥>) 必为空）。
    env_name_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def _check_secrets(obj: Any, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k) == "api_key_env":
                    # api_key_env 存的是环境变量「名」。空串合法（免钥 provider）；
                    # 非空则必须长得像环境变量名，否则判定为用户把密钥值误填到了
                    # 这里——拒绝并说明，绝不回显这个疑似密钥的值。
                    if (isinstance(v, str) and v.strip()
                            and env_name_re.match(v) is None):
                        raise ValueError(
                            f"导入文件 {path}.{k} 的值不像环境变量名："
                            "api_key_env 应填环境变量名（如 MYRELAY_KEY），"
                            "不要直接粘贴密钥值；密钥请放进环境变量，绝不写进配置文件。")
                    continue
                if secret_re.search(str(k)) and not isinstance(v, (dict, list)):
                    raise ValueError(
                        f"导入文件 {path}.{k} 疑似密钥字段："
                        "API Key 只从环境变量读取，请改用 api_key_env 指定环境变量名。")
                _check_secrets(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _check_secrets(item, f"{path}[{i}]")

    try:
        _check_secrets(data)
    except ValueError as exc:
        return _fail(str(exc))

    # 校验：用 Settings.load 验证（临时设 AGENTCREW_CONFIG 指向导入文件）。
    import os
    tmp_env = os.environ.get("AGENTCREW_CONFIG")
    os.environ["AGENTCREW_CONFIG"] = str(src)
    try:
        Settings.load()
    except (OSError, ValueError) as exc:
        return _fail(f"导入文件校验失败：{exc}")
    finally:
        if tmp_env is not None:
            os.environ["AGENTCREW_CONFIG"] = tmp_env
        else:
            os.environ.pop("AGENTCREW_CONFIG", None)

    # 合并写入现有 agentcrew.yaml（不是整体覆盖）。
    target = Path(os.environ.get("AGENTCREW_CONFIG") or "agentcrew.yaml")
    if not target.is_absolute():
        target = Path.cwd() / target
    existing: dict = {}
    if target.is_file():
        try:
            existing = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            existing = {}
    # 备份
    bak = target.with_suffix(".yaml.bak")
    if target.is_file():
        bak.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    # 合并 models 段
    existing_models = existing.get("models") or {}
    incoming_models = data.get("models") or {}
    for key in ("default", "role_bindings", "providers", "custom_providers"):
        if key in incoming_models:
            existing_models[key] = incoming_models[key]
    existing["models"] = existing_models
    # 合并顶层段
    for key in (
        "mcp_servers", "gate_config", "tool_permissions", "tool_guards",
        "tools_enabled", "entity_memory_enabled",  # R3 顶层能力开关
        "approval_mode",                          # R4 人工审批模式
        "peer_dialogue_enabled", "max_peer_messages",  # R5 跨角色咨询
        "peer_consult_capture_enabled",               # R6 咨询沉淀
        "delegation_mode", "max_delegation_depth", "max_dynamic_tasks",  # R6 委派治理
        "supervisor_review_enabled",                  # R7 主管审核
        "max_subtask_corrections",                    # R8 拒绝原因回灌
        "supervisor_scope", "supervisor_role",        # R9 跨任务层级
        "role_fallback", "max_supervisor_reassignments",  # R10 自动重派
        "webhook_url", "webhook_secret", "webhook_events",  # R12 出站 Webhook
        "webhook_timeout_seconds", "webhook_max_retries",
        "webhooks",                                   # R14 多接收端
        "api_rate_limit_enabled", "api_rate_limit_max",  # R18 限流配置通道
        "api_rate_limit_window",
        "debug_step",                                 # R20 单步断点
        "sandbox_backend", "sandbox_image",           # R51 C2 执行后端
        "sandbox_memory_mb", "sandbox_cpus", "sandbox_allow_network",
        "search_provider", "search_max_results",      # R51 C1 联网搜索
        "search_timeout_seconds",
    ):
        if key in data:
            existing[key] = data[key]
    if "config_version" in data:
        existing["config_version"] = data["config_version"]
    target.write_text(
        yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    print(f"已导入 {src} → {target}（备份在 {bak}）")
    return EXIT_CODES["ok"]


#: 子命令 → 处理器（``build_parser`` 里注册的 dest 值）。
_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "run": _cmd_run,
    "resume": _cmd_resume,
    "status": _cmd_status,
    "approvals": _cmd_approvals,
    "approve": _cmd_approve,
    "roles": _cmd_roles,
    "tools": _cmd_tools,
    "tool": _cmd_tool,
    "keys": _cmd_keys,
    "flow": _cmd_flow,
    "recall": _cmd_recall,
    "agents": _cmd_agents,
    "switch": _cmd_switch,
    "samples": _cmd_samples,
    "eval": _cmd_eval,
    "clean": _cmd_clean,
    "cancel": _cmd_cancel,
    "delete": _cmd_delete,
    "serve": _cmd_serve,
    "doctor": _cmd_doctor,
    "diff": _cmd_diff,
    "providers": _cmd_providers,
    "models": _cmd_models,
    "config": _cmd_config,
    "schedule": _cmd_schedule,
    "docs": _cmd_docs,
    "mcp-serve": _cmd_mcp_serve,
}


# ── 配置、闸门与辅助 ──────────────────────────────────────────────────────


def _apply_sample(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """按 ``--sample`` 切换 mock 夹具并返回样例目标。

    样例是一份**离线回放**：目标与 ``plan_todo.json`` / ``responses/`` 强绑定，
    用真实 provider 跑就跑成了另一个项目。因此这里顺手把 ``mock`` 打开，
    而不是让用户先跑一次才发现「任务与夹具对不上」。

    返回 ``(goal, error)``：未给 ``--sample`` 时是 ``(None, None)``；
    给了但登记表里没有（或缺夹具）时是 ``(None, 错误文案)``，由调用方转成退出码 3。
    """
    name = getattr(args, "sample", None)
    if not name:
        return None, None
    try:
        from examples.samples import activate
    except ImportError as exc:  # 发行包可能不含 examples/，此时样例不可用
        return None, f"--sample 需要仓库内的 examples/samples：{exc}"
    try:
        spec = activate(name)
    except (KeyError, FileNotFoundError) as exc:
        # KeyError 的 str() 带外层引号，这里统一剥掉，和 `samples` 的错误口径一致
        return None, f"样例不可用：{str(exc).strip(chr(39) + chr(34))}"
    args.mock = True
    return spec.goal, None


def _settings_from(args: argparse.Namespace) -> Settings:
    """把 CLI 覆盖合并进五级配置（spec §17.1）。

    只写「命令行真的给了」的键：``store_true`` 的 ``False`` 缺省不能进 overrides，
    否则会把 ``AGENTCREW_MOCK=true`` 之类的环境变量反过来盖掉。
    """
    overrides: dict[str, Any] = {}
    if getattr(args, "mock", False):
        overrides["mock"] = True
    if getattr(args, "plan_only", False):
        overrides["plan_only"] = True
    if getattr(args, "debug_step", False):
        overrides["debug_step"] = True   # R20：store_true 的 False 缺省不进 overrides
    gate = _effective_gate(
        getattr(args, "gate", None), auto=getattr(args, "auto", False))
    if gate is not None:
        overrides["gate"] = gate
    if getattr(args, "max_concurrency", None) is not None:
        overrides["max_concurrency"] = args.max_concurrency
    if getattr(args, "default_model", None) is not None:
        overrides["default_model"] = args.default_model
    if getattr(args, "context_budget", None) is not None:
        overrides["context_budget_tokens"] = args.context_budget
    if getattr(args, "max_cost", None) is not None:
        overrides["max_cost_usd"] = args.max_cost
    if getattr(args, "max_llm_calls", None) is not None:
        overrides["max_llm_calls"] = args.max_llm_calls
    if getattr(args, "output_dir", None):
        overrides["output_dir"] = Path(args.output_dir)
    if getattr(args, "approval_mode", None):
        overrides["approval_mode"] = args.approval_mode
    return Settings.load(**overrides)


def _effective_gate(gate: GateMode | None, *, auto: bool) -> GateMode | None:
    """``--gate`` / ``--auto`` → 传给配置层的 gate 覆盖值（``None`` = 不覆盖）。

    ``--auto`` 与 ``--gate`` 同时给出时 ``--auto`` 胜出：它本身就是「等价于
    ``--gate none``」的缩写，两者写出不同的值属于自相矛盾的调用。
    """
    if auto:
        return "none"
    return gate


def _resolve_gate(
    gate: GateMode,
    *,
    interactive: bool,
    console: Console | None = None,
    catalog: RoleCatalog | None = None,
    approval_mode: str = "interactive",
) -> tuple[GateMode, Prompter, bool]:
    """gate 模式 → ``(生效的 gate, prompter, 是否降级)``。

    人工卡点 + 无 TTY 时**显式降级**为 ``none`` 并返回 ``downgraded=True``，由调用方
    打一行说明。本函数绝不返回「gate 仍含人工卡点 + NullPrompter」的组合：那正是
    Task 27 的构造期守卫要拦的错位（``Gatekeeper`` 会为此抛 ``ValueError``，因为
    「声称有人工卡点、实际全自动放行」比没有闸门更危险）。

    ``catalog`` 透传给 :class:`CliPrompter`：计划卡点的 ``[e] 编辑后执行`` 要用
    它校验编辑结果，自定义角色才不会被误判为未注册。
    """
    # R4：deferred 模式把「问人」外包给审批单，不需要 TTY，也不降级为全自动。
    # run 会在卡点挂起，人用 `approve` 在另一个进程裁决后 `resume` 继续。
    if approval_mode == "deferred" and gate in _HUMAN_GATES:
        return gate, DeferredPrompter(), False
    if gate not in _HUMAN_GATES:
        return gate, NullPrompter(), False
    if interactive:
        return gate, CliPrompter(console=console, catalog=catalog), False
    return "none", NullPrompter(), True


def _stdin_isatty() -> bool:
    """stdin 是否真终端；取不到（pytest / 重定向 / 无 stdin）一律当非 TTY。"""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _invalid_numbers(args: argparse.Namespace) -> str | None:
    """数值开关的校验（argparse 只管类型，不管取值）；非法时返回错误消息。

    ``--max-cost`` 特别要挡 ``nan`` / ``inf``：预算判定是
    ``total_cost_usd > max_cost_usd``，与 NaN 比较恒为 False，配了上限等于没配；
    inf 同理。负值 / 负调用上限会让第一次 LLM 调用就以「预算耗尽」失败，用户
    看不出是自己把上限写成了负数。引擎侧负上限的既有语义由测试钉住，这里只
    在 CLI 入口拦住非法输入。
    """
    cmd = getattr(args, "command", "run")
    concurrency = getattr(args, "max_concurrency", None)
    if concurrency is not None and concurrency < 1:
        return f"{cmd}：--max-concurrency 必须 >= 1。"
    budget = getattr(args, "context_budget", None)
    if budget is not None and budget < 1:
        return f"{cmd}：--context-budget 必须 >= 1。"
    cost = getattr(args, "max_cost", None)
    if cost is not None and (not math.isfinite(cost) or cost < 0):
        return f"{cmd}：--max-cost 必须是 >= 0 的有限数。"
    calls = getattr(args, "max_llm_calls", None)
    if calls is not None and calls < 0:
        return f"{cmd}：--max-llm-calls 必须 >= 0。"
    return None


def _config_diff(snapshot: dict[str, Any], settings: Settings) -> list[str]:
    """配置快照 vs 当前配置的差异行（空列表 = 可比，spec §13.3 第 3 步）。

    两侧都过 :func:`~agentcrew.config.redact_overrides`：这条差异行会**打印到
    stderr**（还常常被重定向进 CI 日志），而 ``provider_overrides`` 是自由形状
    的用户输入 —— 旧 run.json 里可能留着本次修复之前写下的明文密钥。
    """
    current = settings.config_snapshot()
    return [
        f"{key}: {_redacted(snapshot.get(key))!r} -> "
        f"{_redacted(current.get(key))!r}"
        for key in _COMPARABLE_KEYS
        if _redacted(snapshot.get(key)) != _redacted(current.get(key))
    ]


def _redacted(value: Any) -> Any:
    """映射值先抹掉疑似密钥再比较/打印；其余类型原样返回。"""
    return redact_overrides(value) if isinstance(value, dict) else value


def _run_dir(settings: Settings, run_id: str) -> Path:
    """定位 run 目录，并拦住任何会拼出 ``output_dir`` 之外的名字。

    ``CheckpointStore.__init__`` 只检查「目录存在」，不校验名字；``cancel`` 会往
    ``run.json`` 写，所以路径必须在进 I/O 之前就收进 output_dir 内（与
    ``checkpoint._assert_deletable`` 同一条边界）。
    """
    root = settings.output_dir.resolve()
    resolved = (settings.output_dir / run_id).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"非法 run_id: {run_id!r}（必须位于 {root} 之下）")
    return resolved


def _out(console: Console, text: str) -> None:
    """打印一行长文本；``soft_wrap`` 关掉 rich 的折行，路径 / run_id 不被截断。"""
    console.print(Text(text), soft_wrap=True)


def _fail(message: str) -> int:
    """错误一律走 stderr + 配置类退出码（spec §7.4 的「配置错误」）。"""
    print(message, file=sys.stderr)
    return EXIT_CODES["config"]


# ── 看板桥 ────────────────────────────────────────────────────────────────


class _BoardHook:
    """``Scheduler.on_event`` 的 ``(事件名, 数据)`` → :class:`BoardView` 的桥。

    看板**按需创建**：``BoardView`` 构造时要 run_id，而 run_id 只有 ``Crew`` 建好
    run 目录后才知道。第一条事件到达时（执行阶段开始）run 目录必然已就绪。
    ``--no-live`` 时 ``enabled=False``：看板不产生输出，但仍更新内存状态。
    """

    def __init__(self, goal: str, *, enabled: bool, console: Console) -> None:
        self._goal = goal
        self._enabled = enabled
        self._console = console
        self._crew: Crew | None = None
        self._view: BoardView | None = None
        self._run_id = ""

    def bind(self, crew: Crew) -> None:
        """绑定所属 ``Crew``（构造桥时 Crew 还没建好，只能事后绑定）。"""
        self._crew = crew

    def __call__(self, event: str, data: dict[str, Any]) -> None:
        view = self._ensure_view()
        view.on_event(
            LedgerEvent(ts=datetime.now(), run_id=self._run_id, event=event, data=data))

    def stop(self) -> None:
        """停止实时看板（幂等；看板从未创建时是空操作）。"""
        if self._view is not None:
            self._view.stop()

    def _ensure_view(self) -> BoardView:
        """第一条事件到达时创建并启动看板。"""
        if self._view is None:
            checkpoint = self._crew.checkpoint if self._crew is not None else None
            self._run_id = checkpoint.run_dir.name if checkpoint is not None else ""
            self._view = BoardView(
                self._run_id, self._goal, enabled=self._enabled, console=self._console)
            self._view.start()
        return self._view


if __name__ == "__main__":  # pragma: no cover - 进程入口
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # 第二次 Ctrl-C 落在任何地方都不留 traceback（spec §13.4）。
        sys.exit(EXIT_CODES["cancelled"])
