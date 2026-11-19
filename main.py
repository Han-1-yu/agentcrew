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
import contextlib
import ipaddress
import json
import os
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console

# ── 兼容再导出：历史调用点/测试仍从 main 取这些名字（R66f）──
from agentcrew.cli.catalog import _cmd_agents as _cmd_agents
from agentcrew.cli.catalog import _cmd_docs as _cmd_docs
from agentcrew.cli.catalog import _cmd_flow as _cmd_flow
from agentcrew.cli.catalog import _cmd_guide as _cmd_guide
from agentcrew.cli.catalog import _cmd_recall as _cmd_recall
from agentcrew.cli.catalog import _cmd_roles as _cmd_roles
from agentcrew.cli.catalog import _cmd_schedule as _cmd_schedule
from agentcrew.cli.catalog import _cmd_switch as _cmd_switch
from agentcrew.cli.catalog import _cmd_tool as _cmd_tool
from agentcrew.cli.catalog import _cmd_tools as _cmd_tools
from agentcrew.cli.common import _COMPARABLE_KEYS as _COMPARABLE_KEYS
from agentcrew.cli.common import _HUMAN_GATES as _HUMAN_GATES
from agentcrew.cli.common import _IMAGE_MAX_BYTES as _IMAGE_MAX_BYTES
from agentcrew.cli.common import _IMAGE_MIME_BY_SUFFIX as _IMAGE_MIME_BY_SUFFIX
from agentcrew.cli.common import _RESUME_FLAG_TEMPLATES as _RESUME_FLAG_TEMPLATES
from agentcrew.cli.common import _apply_sample as _apply_sample
from agentcrew.cli.common import _config_diff as _config_diff
from agentcrew.cli.common import _effective_gate as _effective_gate
from agentcrew.cli.common import _fail as _fail
from agentcrew.cli.common import _invalid_numbers as _invalid_numbers
from agentcrew.cli.common import _load_cli_images as _load_cli_images
from agentcrew.cli.common import _out as _out
from agentcrew.cli.common import _redacted as _redacted
from agentcrew.cli.common import _resolve_gate as _resolve_gate
from agentcrew.cli.common import _resume_hint as _resume_hint
from agentcrew.cli.common import _resume_hint_notes as _resume_hint_notes
from agentcrew.cli.common import _run_dir as _run_dir
from agentcrew.cli.common import _sample_names_for as _sample_names_for
from agentcrew.cli.common import _settings_from as _settings_from
from agentcrew.cli.common import _stdin_isatty as _stdin_isatty
from agentcrew.cli.config_cmd import _cmd_config as _cmd_config
from agentcrew.cli.config_cmd import _cmd_config_export as _cmd_config_export
from agentcrew.cli.config_cmd import _cmd_config_import as _cmd_config_import
from agentcrew.cli.diagnostics import _cmd_diff as _cmd_diff
from agentcrew.cli.diagnostics import _cmd_doctor as _cmd_doctor
from agentcrew.cli.eval_cmd import _cmd_eval as _cmd_eval
from agentcrew.cli.eval_cmd import _cmd_samples as _cmd_samples
from agentcrew.cli.eval_cmd import _eval_base_settings as _eval_base_settings
from agentcrew.cli.eval_cmd import _eval_matrix as _eval_matrix
from agentcrew.cli.parser import _add_run_flags as _add_run_flags
from agentcrew.cli.parser import build_parser as build_parser
from agentcrew.cli.providers import _cmd_models as _cmd_models
from agentcrew.cli.providers import _cmd_providers as _cmd_providers
from agentcrew.cli.run import _BoardHook as _BoardHook
from agentcrew.cli.run import _cmd_resume as _cmd_resume
from agentcrew.cli.run import _cmd_rewind as _cmd_rewind
from agentcrew.cli.run import _cmd_run as _cmd_run
from agentcrew.cli.run import _drive as _drive
from agentcrew.cli.run import _expire_overdue_approvals as _expire_overdue_approvals
from agentcrew.cli.run import _load_flow_definition as _load_flow_definition
from agentcrew.cli.run import _print_resume_hint as _print_resume_hint
from agentcrew.cli.run import _reconcile_on_startup as _reconcile_on_startup
from agentcrew.cli.run import _run_exit_code as _run_exit_code
from agentcrew.cli.status import _cmd_approvals as _cmd_approvals
from agentcrew.cli.status import _cmd_approve as _cmd_approve
from agentcrew.cli.status import _cmd_cancel as _cmd_cancel
from agentcrew.cli.status import _cmd_clean as _cmd_clean
from agentcrew.cli.status import _cmd_delete as _cmd_delete
from agentcrew.cli.status import _cmd_status as _cmd_status
from agentcrew.config import Settings
from agentcrew.crew.crew import EXIT_CODES
from agentcrew.crew.crew import Crew as Crew
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
)
from agentcrew.utils.logger import setup_logging

#: 编码探测串：看板状态符号编不进 GBK，中文可以。
_ENCODING_PROBE = "✓⟳—·中文"


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


#: 子命令 → 处理器（``build_parser`` 里注册的 dest 值）。
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


def _cmd_worker(args: argparse.Namespace) -> int:
    """C14：分布式执行 worker——从共享队列领 run，可多进程/多机水平扩展。

    队列是文件系统目录（见 :mod:`agentcrew.runtime.queue`），所以「多机」只需要
    把 ``--queue-dir`` 与 ``--output-dir`` 放在同一块共享卷上（NFS 等），不需要
    Redis / RabbitMQ。``--once`` 用于烟测与 CI（处理完当前队列即退出）。
    """
    import asyncio
    from pathlib import Path

    from agentcrew.runtime.queue import RunQueue, make_worker_id

    overrides: dict[str, Any] = {}
    if args.mock:
        overrides["mock"] = True
    if args.output_dir:
        overrides["output_dir"] = Path(args.output_dir)
    try:
        settings = Settings.load(**overrides)
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    # worker 是无人值守进程：gate 含人工卡点时不可能有人来批，绝不能在后台
    # 静默换成 NullPrompter（那是「闸门看起来还在、实际放行」）。要么显式
    # --auto（等价 gate=none），要么把配置改成 plan/delivery。
    if "both" in str(settings.gate):
        if not args.auto:
            return _fail(
                "worker 无人值守，但配置 gate='both' 含人工卡点："
                "请加 --auto（等价 gate=none），或改成 gate=plan/delivery")
        settings = settings.model_copy(update={"gate": "none"})
    queue = RunQueue(
        Path(args.queue_dir) if args.queue_dir
        else settings.output_dir / "_queue",
        lease_seconds=args.lease_seconds,
    )
    return asyncio.run(_worker_loop(settings, queue, args, make_worker_id()))


async def _worker_loop(settings: Any, queue: Any, args: argparse.Namespace,
                       worker_id: str) -> int:
    """领取 → 跑 → 收尾的主循环；失败只记在队列项上，不拖死 worker。"""
    import asyncio

    from agentcrew.crew.crew import Crew

    sem = asyncio.Semaphore(max(1, args.concurrency))
    running: set[asyncio.Task[Any]] = set()

    async def handle(item: Any) -> None:
        crew = Crew(settings)
        task = asyncio.create_task(crew.run(item.goal, run_id=item.run_id))
        # 心跳与「等任务结束」分开：早先直接 sleep(lease/3)，任务跑完还要等满
        # 20 秒才收尾，队列吞吐被拖低四倍。这里等的是 task 本身，心跳只在
        # 超时未完成时做。
        interval = max(1.0, min(args.lease_seconds / 3, 5.0))
        lease_lost = False
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if done:
                    break
                if queue.cancel_requested(item.run_id):
                    crew.cancel()
                if not queue.heartbeat(item.run_id, worker_id):
                    # 租约被别人接管（或已过期被回收）：立刻停，别写坏别人的 run。
                    # P3-2：收尾权也随租约一起没了——下面的 complete/fail 会被
                    # 队列拒绝（claim 已不存在/已换主），所以这里不写任何结果。
                    lease_lost = True
                    crew.cancel()
                    break
            record = await task
            if lease_lost:
                print(f"[{worker_id}] {item.run_id} 租约已失，放弃收尾"
                      "（结果由新租约持有者写入）", file=sys.stderr)
                return
            status = str(getattr(record.status, "value", record.status))
            queue.complete(item.run_id, status=status, worker_id=worker_id)
        except asyncio.CancelledError:
            crew.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 —— 单个 run 失败不能拖死 worker
            if lease_lost:
                return
            from agentcrew.runtime.queue import QueueError

            try:
                queue.fail(
                    item.run_id, error=f"{type(exc).__name__}: {exc}",
                    worker_id=worker_id)
            except QueueError as queue_exc:
                # 收尾时租约已失（心跳循环之后才被回收）：不覆盖新租约持有者的
                # 结果，只留一行可诊断的痕迹。
                print(f"[{worker_id}] {item.run_id} 收尾失败：{queue_exc}"
                      f"（run 自身错误：{type(exc).__name__}: {exc}）",
                      file=sys.stderr)

    while True:
        await sem.acquire()
        item = queue.claim(worker_id)
        if item is None:
            sem.release()
            if args.once and not running:
                break
            await asyncio.sleep(max(0.2, args.poll_seconds))
            continue
        task = asyncio.create_task(handle(item))
        running.add(task)

        def _finished(done: asyncio.Task[Any]) -> None:
            running.discard(done)
            sem.release()

        task.add_done_callback(_finished)
    if running:
        await asyncio.gather(*running, return_exceptions=True)
    return 0


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
        queue = None
        if getattr(args, "queue_dir", None):
            from agentcrew.runtime.queue import RunQueue

            queue = RunQueue(
                Path(args.queue_dir), lease_seconds=args.lease_seconds)
            print(f"C14 队列模式：API 只入队，worker 消费 {queue.root}")
        server = ApiServer(
            host=args.host, port=args.port, output_dir=settings.output_dir,
            settings=settings,
            auth=auth, rate_limiter=rate_limiter,
            model_factory=model_factory, queue=queue,
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
                # 非主线程等场景：只保留 KeyboardInterrupt 兜底。
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(_sig, _request_stop)
        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass
        finally:
            await server.stop()
        return EXIT_CODES["ok"]

    return asyncio.run(_serve())


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



_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "run": _cmd_run,
    "resume": _cmd_resume,
    "rewind": _cmd_rewind,
    "status": _cmd_status,
    "approvals": _cmd_approvals,
    "approve": _cmd_approve,
    "guide": _cmd_guide,
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
    "worker": _cmd_worker,
    "doctor": _cmd_doctor,
    "diff": _cmd_diff,
    "providers": _cmd_providers,
    "models": _cmd_models,
    "config": _cmd_config,
    "schedule": _cmd_schedule,
    "docs": _cmd_docs,
    "mcp-serve": _cmd_mcp_serve,
}



if __name__ == "__main__":  # pragma: no cover - 进程入口
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # 第二次 Ctrl-C 落在任何地方都不留 traceback（spec §13.4）。
        sys.exit(EXIT_CODES["cancelled"])
