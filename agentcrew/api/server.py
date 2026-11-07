"""HTTP API + SSE 实时事件流（V2 第 5 项）。

零依赖实现：用 ``asyncio.start_server`` + 手写 HTTP/1.1 解析，不引入
aiohttp/fastapi。实时推送用 SSE（Server-Sent Events）而非 WebSocket——
SSE 是标准 HTTP 长连接，单向推送足够覆盖「运行状态实时更新」场景，
且无需实现 WebSocket 握手/帧协议。事件源与 Ledger 同构（``on_event`` 回调）。

端点：
- ``GET /health`` → 健康检查
- ``POST /runs`` → 创建并后台启动一个 run（body: ``{"goal": ..., "options": {...}}``）
- ``GET /runs`` → 列出全部 run（盘上记录 + 内存中运行中的实时状态）
- ``POST /runs/{run_id}/cancel`` → 软取消运行中的 run（202）
- ``POST /runs/{run_id}/resume`` → 从 checkpoint 恢复 run 并后台执行（202）
- ``GET /runs/{run_id}/status`` → 运行状态（任务统计）
- ``GET /runs/{run_id}/ledger?event=&limit=&offset=`` → Ledger 事件分页查询（P2-9）
- ``GET /runs/{run_id}/stats`` → 事件分布 / LLM 用量 / 任务成功率（P2-9）
- ``GET /runs/{run_id}/events`` → SSE 事件流
- ``POST /runs/{run_id}/events`` → 注入测试事件（仅测试/调试用）

P1-5 的 run 生命周期：``POST /runs`` 经 ``crew_factory`` 建 Crew、挂 ``on_event``
后用 ``asyncio.create_task`` 后台跑；``run_started`` 事件一到就把 ``run_id`` 回给
HTTP 响应并登记进 :attr:`ApiServer.active_crews`，任务结束时摘除。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from agentcrew.api import http as http_transport
from agentcrew.api.auth import APIKeyAuth, RateLimiter
from agentcrew.models.config import PROVIDER_PRESETS as PROVIDER_PRESETS_NAMES


def _validate_run_options(options: Mapping[str, Any]) -> str | None:
    """``POST /runs`` 的 ``options`` 白名单 + 逐字段类型/范围校验，返回错误文案。

    ``Settings.model_copy(update=...)`` 既不校验也不过滤：任意键都会被塞进
    ``Settings.__dict__``，``{"mock": "false"}`` 里的字符串还会被当成 truthy
    的开关（客户端想关 mock、实际却强制开了）。此前唯一的兜底是 ``except
    Exception: settings = self._settings`` —— 失败被静默吞掉、请求照样回 202，
    用户以为 options 生效了。这里在入口挡住，值仍由 Settings 的既有语义解释。

    白名单按「可安全按 run 覆盖且不会绕过服务端边界」取最小集合；``gate``
    恒被 ``_build_crew`` 覆写为 none，``output_dir`` / ``api_keys`` 等属于
    服务端配置，均不开放。
    """
    allowed = {
        "mock", "default_model", "max_concurrency", "context_budget_tokens",
        "max_cost_usd", "max_llm_calls", "model_tiers", "gate_config",
        # R3：两个默认关闭的能力开关，允许按 run 显式打开。
        "tools_enabled", "entity_memory_enabled",
        # R4：允许按 run 打开「审批单挂起」模式。
        "approval_mode",
        # R5：允许按 run 打开跨角色咨询并收紧配额。
        "peer_dialogue_enabled", "max_peer_messages",
        # R6：咨询沉淀与动态委派治理。
        "peer_consult_capture_enabled", "delegation_mode",
        "max_delegation_depth", "max_dynamic_tasks",
        # R7：主管审核；R8：委派拒绝原因回灌次数；R9：覆盖范围与主管角色。
        "supervisor_review_enabled", "max_subtask_corrections",
        "supervisor_scope", "supervisor_role",
        # R10：主管驳回后换人做。
        "role_fallback", "max_supervisor_reassignments",
        # R12：出站 Webhook（密钥不经 HTTP 接口传，只认配置文件/环境变量）。
        "webhook_url", "webhook_events", "webhook_timeout_seconds",
        "webhook_max_retries",
    }
    unknown = sorted(set(options) - allowed)
    if unknown:
        return ("options 含不支持的键：" + "、".join(unknown)
                + "；可用：" + "、".join(sorted(allowed)))
    for key, value in options.items():
        if value is None:
            if key not in {"default_model", "max_cost_usd", "max_llm_calls"}:
                return f"options.{key} 不能为 null"
            continue
        # R3：两个能力开关与 mock 同为布尔开关，共用一条类型校验。
        if key in ("mock", "tools_enabled", "entity_memory_enabled",
                   "peer_dialogue_enabled", "peer_consult_capture_enabled",
                   "supervisor_review_enabled"):
            if not isinstance(value, bool):
                return f"options.{key} 必须是布尔值"
        elif key == "delegation_mode":
            if value not in ("any", "manager_only"):
                return "options.delegation_mode 必须是 any 或 manager_only"
        elif key == "supervisor_scope":
            if value not in ("delegated", "all"):
                return "options.supervisor_scope 必须是 delegated 或 all"
        elif key == "supervisor_role":
            if not isinstance(value, str) or not value.strip():
                return "options.supervisor_role 必须是非空字符串"
        elif key == "role_fallback":
            if not isinstance(value, dict):
                return "options.role_fallback 必须是对象（角色 → 备选角色数组）"
            for role, candidates in value.items():
                if isinstance(candidates, str) or not isinstance(candidates, list):
                    return f"options.role_fallback.{role} 必须是角色 key 数组"
        elif key in ("max_delegation_depth", "max_dynamic_tasks",
                     "max_subtask_corrections",
                     "max_supervisor_reassignments", "webhook_max_retries"):
            if isinstance(value, bool) or not isinstance(value, int):
                return f"options.{key} 必须是整数"
            if value < 0:
                return f"options.{key} 必须 >= 0"
        elif key == "approval_mode":
            if value not in ("interactive", "deferred"):
                return "options.approval_mode 必须是 interactive 或 deferred"
        elif key == "webhook_url":
            if not isinstance(value, str) or not value.strip():
                return "options.webhook_url 必须是非空字符串"
            if not value.startswith(("http://", "https://")):
                return "options.webhook_url 必须是 http/https 地址"
            if "@" in value.split("//", 1)[-1].split("/", 1)[0]:
                return "options.webhook_url 不能带用户名密码"
        elif key == "webhook_events":
            if isinstance(value, str) or not isinstance(value, list):
                return "options.webhook_events 必须是事件名数组"
            if not value:
                return "options.webhook_events 不能为空数组（要全推请用 [\"*\"]）"
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    return "options.webhook_events 的每一项都必须是非空字符串"
        elif key == "max_peer_messages":
            if isinstance(value, bool) or not isinstance(value, int):
                return "options.max_peer_messages 必须是整数"
            if value < 0:
                return "options.max_peer_messages 必须 >= 0"
        elif key == "default_model":
            if not isinstance(value, str) or not value.strip():
                return "options.default_model 必须是非空字符串"
        elif key in ("max_concurrency", "context_budget_tokens", "max_llm_calls"):
            if isinstance(value, bool) or not isinstance(value, int):
                return f"options.{key} 必须是整数"
            lower = 0 if key == "max_llm_calls" else 1
            if value < lower:
                return f"options.{key} 必须 >= {lower}"
        elif key == "webhook_timeout_seconds":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "options.webhook_timeout_seconds 必须是数字"
            if value <= 0:
                return "options.webhook_timeout_seconds 必须 > 0"
        elif key == "max_cost_usd":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "options.max_cost_usd 必须是数值"
            if not math.isfinite(value) or value < 0:
                return "options.max_cost_usd 必须是 >= 0 的有限数"
        elif not isinstance(value, dict):
            return f"options.{key} 必须是对象"
    return None


@dataclass
class RunState:
    """一个运行的内存状态快照（供 API 查询）。"""

    run_id: str
    goal: str = ""
    status: str = "pending"  # pending | running | completed | failed
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """发布/订阅事件总线：SSE 客户端订阅，Crew 的 on_event 发布。"""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    def publish(self, event: str, data: dict[str, Any]) -> None:
        """发布事件到所有订阅者（非阻塞）。"""
        payload = http_transport.sse_frame(event, data)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # 慢客户端丢弃，不阻塞发布者

    def subscribe(self) -> asyncio.Queue[str]:
        """订阅事件流，返回一个队列；用完须调用 :meth:`unsubscribe`。"""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


#: 请求体补读超时（秒）：绝大多数请求一次 ``read`` 就凑满，只有跨 TCP 段到达
#: 或客户端少发时才进补读；短超时把「声明长度却不发包」收口为 400，不会死等。
_BODY_READ_TIMEOUT = 1.0


def _clamp_int(raw: str, *, default: int, minimum: int) -> int:
    """把查询参数里的整数读出来；非法 / 缺失用 ``default``，并压到 ``minimum`` 以上。"""
    return http_transport.clamp_int(raw, default=default, minimum=minimum)


def _env_int(name: str, default: int) -> int:
    """读一个整型环境变量；缺失 / 非法一律回 ``default``（配置容错）。"""
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


#: API 路由清单 ``(方法, 路径模板)``——对外契约的**唯一事实源**。
#: 新增 / 删除路由必须同步改这里与 ``artifacts/contracts/openapi.yaml``；
#: ``tests/test_api_contract.py`` 做双向比对，并在真服务上逐条探测每个条目
#: 真的接得起来（未匹配路由统一回 ``404 {"error": "Not Found"}``，探测断言
#: 以「响应不是哨兵」判定路由存在，与各 handler 的具体错误形状解耦）。
API_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/runs"),
    ("POST", "/runs"),
    ("DELETE", "/runs/{run_id}"),
    ("GET", "/runs/{run_id}/status"),
    ("GET", "/runs/{run_id}/approvals"),
    ("POST", "/runs/{run_id}/approvals"),
    ("GET", "/runs/{run_id}/ledger"),
    ("GET", "/runs/{run_id}/stats"),
    ("POST", "/runs/{run_id}/cancel"),
    ("POST", "/runs/{run_id}/resume"),
    ("GET", "/runs/{run_id}/model"),
    ("PUT", "/runs/{run_id}/model"),
    ("GET", "/runs/{run_id}/events"),
    ("POST", "/runs/{run_id}/events"),
    ("POST", "/runs/{run_id}/agents"),
    ("POST", "/runs/{run_id}/tasks/{task_id}/agent"),
    ("GET", "/providers"),
    ("GET", "/providers/{name}/models"),
    ("POST", "/providers/{name}/test"),
)


class ApiServer:
    """轻量 async HTTP 服务器 + SSE 事件流。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        event_bus: EventBus | None = None,
        output_dir: Path | None = None,
        settings: Any | None = None,
        crew_factory: Callable[[str, Mapping[str, Any]], Any] | None = None,
        auth: APIKeyAuth | None = None,
        rate_limiter: RateLimiter | None = None,
        model_factory: Any | None = None,
    ) -> None:
        """``settings`` / ``crew_factory`` 只服务于 P1-5 的创建/恢复端点。

        ``crew_factory(goal, options) -> crew`` 负责构造一个带 ``on_event`` 回调的
        run 驱动对象（真实是 :class:`~agentcrew.crew.crew.Crew`，测试注入 mock）；
        缺省时用 ``settings`` 现场构造一个 ``gate="none"`` 的 Crew。``settings``
        为 ``None`` 且未给工厂时，创建/恢复端点返回 503。

        ``auth`` / ``rate_limiter``（E-3）是两个可选保护层，默认 ``None`` =
        不挂中间件，行为与改造前逐字节一致。启用后在路由分发前依次做：先按
        客户端 IP 限流（超限 429），再校验 API Key（缺失 / 无效 401）。被拒请求
        写进 :attr:`security_events`（只记事件名与 client_ip/path/remaining，
        **绝不记 key 值**）。
        """
        self.host = host
        self.port = port
        self.bus = event_bus or EventBus()
        self.runs: dict[str, RunState] = {}
        self._server: asyncio.AbstractServer | None = None
        self._output_dir = output_dir
        self._settings = settings
        self._crew_factory = crew_factory
        #: E-3：可选的 API Key 认证 / IP 速率限制（None = 关闭，向后兼容）。
        self._auth = auth
        self._rate_limiter = rate_limiter
        #: E-3：服务端安全审计事件（``api_auth_failed`` / ``api_rate_limited``）。
        #: 中间件在路由分发被拒时追加；与 run 级 Ledger 同源同形（事件名 + data），
        #: 但落在内存——这些事件发生在任何 run 被识别之前，没有 run 目录可写。
        self.security_events: list[dict[str, Any]] = []
        #: 运行中的 Crew 注册表（run_id → crew 实例）；任务结束由 runner 摘除。
        self.active_crews: dict[str, Any] = {}
        #: 后台任务句柄（run_id → asyncio.Task），便于优雅收尾。
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        #: A5：可选 ModelFactory，用于 /providers 健康查询与 /test 测速。
        self._model_factory = model_factory
        #: A5：模型发现结果缓存（provider → (timestamp, models) 或 (timestamp, error)）。
        self._discovery_cache: dict[str, tuple[float, Any]] = {}
        # 2026-11 并发加固：同时存活的 run 数上限。每个 crew 自带 LLM 信号量，
        # 但 N 个并发 run = N × max_concurrency 的 LLM 并发与无界的内存/句柄；
        # 超限时创建/resume 端点直接 429。环境变量可调，默认 8。
        self._max_concurrent_runs = max(
            1, _env_int("AGENTCREW_MAX_CONCURRENT_RUNS", 8))
        self._active_run_count = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> ApiServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def on_event(self, event: str, data: dict[str, Any]) -> None:
        """Crew 的 on_event 回调入口：推送到 SSE 客户端并更新内存状态。"""
        self.bus.publish(event, data)
        run_id = data.get("run_id", "")
        if run_id and run_id not in self.runs:
            self.runs[run_id] = RunState(run_id=run_id)
        if event == "task_transition":
            task_id = data.get("task_id", "")
            if task_id:
                self.runs[run_id].tasks[task_id] = {
                    "status": data.get("to_status", ""),
                    "role": data.get("role", ""),
                }
        elif event == "run_started":
            self.runs[run_id].status = "running"
            goal = data.get("goal")
            if isinstance(goal, str) and goal:
                self.runs[run_id].goal = goal
        elif event == "run_paused":
            # R4：审批挂起不是终局，不能落到 run_finished 的 else→failed。
            self.runs[run_id].status = str(
                data.get("status") or "awaiting_approval")
        elif event == "run_completed":
            self.runs[run_id].status = "completed"
        elif event == "run_failed":
            self.runs[run_id].status = "failed"
        elif event == "run_finished":
            # P1-5：Crew 封版统一发这一个事件（含 completed / cancelled /
            # interrupted / failed），按状态落到徽章。
            status = data.get("status", "")
            if status == "completed":
                self.runs[run_id].status = "completed"
            elif status in ("cancelled", "interrupted"):
                self.runs[run_id].status = status
            else:
                self.runs[run_id].status = "failed"

    # ── HTTP 处理 ────────────────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await http_transport.read_request(reader)
            if request is None:
                return
            # E-3 中间件链（路由分发前）：先限流后认证。任一拦截直接回响应并
            # return，不再读 body / 不再分发——被拒请求不该再触发任何业务逻辑。
            if await self._apply_middleware(request.path, request.headers, writer):
                return
            if request.method == "GET":
                await self._handle_get(request.path, writer)
            elif request.method in ("POST", "PUT"):
                length = request.content_length
                if length > http_transport.MAX_BODY_BYTES:
                    await self._send_json(
                        writer, 413,
                        {"error": f"请求体过大（上限 "
                                  f"{http_transport.MAX_BODY_BYTES} 字节）"})
                    return
                # StreamReader.read(n) 只返回当前 buffer 里已有的字节，合法大 body
                # 跨 TCP 段到达时会被截断成半截 JSON（误报 400）。先读一次，不足
                # 再按剩余长度补读；少发 / 不发包由短超时收口，交给 JSON 解析 400。
                body = await reader.read(length) if length > 0 else b""
                if length > 0 and len(body) < length:
                    try:
                        rest = await asyncio.wait_for(
                            reader.readexactly(length - len(body)),
                            timeout=_BODY_READ_TIMEOUT)
                    except (asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
                        rest = getattr(exc, "partial", b"")
                    body += rest
                if request.method == "POST":
                    await self._handle_post(request.path, body, writer)
                else:
                    await self._handle_put(request.path, body, writer)
            elif request.method == "DELETE":
                await self._handle_delete(request.path, writer)
            else:
                await self._send_json(writer, 405, {"error": "Method Not Allowed"})
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:
            # 兜底：任何未预料异常都必须给客户端一个 HTTP 响应。此前 OSError 会
            # 穿透所有 except，finally 只关连接 → 客户端拿到 0 字节、服务端无记录。
            try:
                await self._send_json(
                    writer, 500,
                    {"error": f"服务端内部错误：{type(exc).__name__}: {exc}"})
            except (ConnectionError, OSError):
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    # ── E-3：认证 / 速率限制中间件 ──────────────────────────────────────────

    async def _apply_middleware(
        self,
        path: str,
        headers: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> bool:
        """路由分发前的保护层；返回 ``True`` 表示已拦截（调用方停止分发）。

        顺序固定为「先限流、后认证」：被刷爆的客户端不该再消耗认证比对，也不该
        通过 401 探知端点是否存在。两者都写 :attr:`security_events`——事件名与
        data 只含 client_ip / path / remaining，**绝不回显客户端发来的 key**
        （key 只参与布尔比对，不进任何持久化结构）。
        """
        client_ip = self._client_ip(headers, writer)
        # 1) 速率限制（基于 IP 的滑动窗口）
        limiter = self._rate_limiter
        if limiter is not None and limiter.enabled:
            allowed, remaining = limiter.check(client_ip)
            if not allowed:
                self._record_security("api_rate_limited", {
                    "client_ip": client_ip, "path": path, "remaining": remaining,
                })
                await self._send_json(
                    writer, 429,
                    {"error": "Too Many Requests"},
                    extra_headers={"Retry-After": str(limiter.window_seconds)},
                )
                return True
        # 2) API Key 认证（Bearer / X-API-Key）
        auth = self._auth
        if auth is not None and auth.enabled:
            if not auth.authenticate(headers):
                self._record_security("api_auth_failed", {
                    "client_ip": client_ip, "path": path,
                })
                await self._send_json(
                    writer, 401,
                    {"error": "Unauthorized"},
                    extra_headers={"WWW-Authenticate": 'Bearer realm="agentcrew"'},
                )
                return True
        return False

    @staticmethod
    def _client_ip(
        headers: dict[str, str], writer: asyncio.StreamWriter
    ) -> str:
        """客户端 IP：优先 ``X-Forwarded-For`` 首地址，否则 socket peer 地址。"""
        return http_transport.client_ip(headers, writer)

    def _record_security(self, event: str, data: dict[str, Any]) -> None:
        """追加一条服务端安全审计事件（内存；与 Ledger 事件同形：event + data）。"""
        self.security_events.append({"event": event, "data": dict(data)})

    async def _handle_get(self, path: str, writer: asyncio.StreamWriter) -> None:
        # 先按 urlsplit 拆开路径与查询串：带 ``?event=...`` 的请求不能再用
        # ``path.endswith("/status")`` 这类裸匹配（P2-9 的 ledger/stats 端点
        # 自带查询参数）。无查询串时 ``parsed.path`` 与历史行为逐字节一致。
        parsed = urlsplit(path)
        # 统一去掉路径尾斜杠：``/runs/`` 与 ``/runs`` 是同一个资源，此前只有
        # POST /runs 用裸字符串比较，带尾斜杠或查询串会直接 404。
        p = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if p == "/" or p == "/index.html":
            # 看板页面按需 import：dashboard 是纯字符串大常量，只有访问根路径时
            # 才需要它，别让每个 API 请求都背着它。
            from agentcrew.api.dashboard import DASHBOARD_HTML
            await http_transport.send_html(writer, DASHBOARD_HTML)
            return
        if p == "/health":
            await self._send_json(writer, 200, {"status": "ok"})
            return
        if p == "/runs":
            await self._list_runs(writer)
            return
        segments = [unquote(s) for s in p.split("/") if s]
        # A5：/providers 目录与 /providers/{name}/models
        if segments and segments[0] == "providers":
            await self._handle_providers_get(segments, writer)
            return
        if len(segments) == 3 and segments[0] == "runs":
            run_id, action = segments[1], segments[2]
            if action == "events":
                await self._handle_sse(run_id, writer)
                return
            if action == "status":
                run = self.runs.get(run_id)
                if run is not None:
                    await self._send_json(writer, 200, {
                        "run_id": run.run_id,
                        "goal": run.goal,
                        "status": run.status,
                        "task_count": len(run.tasks),
                        "tasks": run.tasks,
                    })
                    return
                # 内存表只覆盖本进程启动过的 run；服务重启后 /runs、/ledger、
                # /stats 都能从磁盘读，唯独 /status 回 404，看板点开历史 run
                # 永远停在 pending。这里按同一份 run.json / tasks.json 回落。
                disk = self._disk_status(run_id)
                if disk is None:
                    await self._send_json(writer, 404, {"error": "run not found"})
                    return
                await self._send_json(writer, 200, disk)
                return
            if action == "model":
                await self._get_model(run_id, writer)
                return
            if action == "ledger":
                await self._get_ledger(run_id, query, writer)
                return
            if action == "stats":
                await self._get_stats(run_id, writer)
                return
            if action == "approvals":
                await self._get_approvals(run_id, writer)
                return
        await self._send_json(writer, 404, {"error": "Not Found"})

    def _disk_status(self, run_id: str) -> dict[str, Any] | None:
        """从盘上 ``run.json`` / ``tasks.json`` 还原最小状态（服务重启后的 /status）。

        run_id 先过 ``_validated_run_id``：这个端点是读路径，但拼路径的方式与
        ``DELETE`` / ``ledger`` 完全一样，不能留一条绕过边界的口子。规划阶段就
        失败的 run 没有 ``tasks.json``，任务表按空处理而不是整个 404。
        """
        if self._output_dir is None:
            return None
        from agentcrew.runtime.checkpoint import CheckpointStore, _validated_run_id

        try:
            store = CheckpointStore(self._output_dir / _validated_run_id(run_id))
            record = store.load_record()
        except (OSError, ValueError):
            return None
        tasks: dict[str, dict[str, Any]] = {}
        try:
            raw = json.loads((store.run_dir / "tasks.json").read_text(encoding="utf-8"))
            entries = raw.get("tasks") or [] if isinstance(raw, dict) else []
            for task in entries:
                if isinstance(task, dict) and task.get("id"):
                    tasks[str(task["id"])] = task
        except (OSError, ValueError):
            pass
        return {
            "run_id": record.run_id or run_id,
            "goal": record.project_goal,
            "status": record.status.value,
            "task_count": len(tasks),
            "tasks": tasks,
            "source": "disk",
        }

    async def _get_approvals(
        self, run_id: str, writer: asyncio.StreamWriter
    ) -> None:
        """R4：列出一个 run 的全部审批单（服务重启后仍从盘上可读）。"""
        if self._output_dir is None:
            await self._send_json(writer, 503, {"error": "output_dir 未配置"})
            return
        from agentcrew.runtime.approvals import ApprovalStore
        from agentcrew.runtime.checkpoint import _validated_run_id

        try:
            rid = _validated_run_id(run_id)
        except ValueError:
            await self._send_json(writer, 400, {"error": "非法 run_id"})
            return
        store = ApprovalStore(self._output_dir / rid)
        await self._send_json(writer, 200, {
            "run_id": rid,
            "approvals": [
                item.model_dump(mode="json") for item in store.all()
            ],
        })

    async def _decide_approval(
        self, run_id: str, body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        """R4：在 HTTP 通道写裁决；引擎侧 resume 后消费这张单继续。"""
        if self._output_dir is None:
            await self._send_json(writer, 503, {"error": "output_dir 未配置"})
            return
        from agentcrew.runtime.approvals import ApprovalError, ApprovalStore
        from agentcrew.runtime.checkpoint import _validated_run_id

        try:
            rid = _validated_run_id(run_id)
            data = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            await self._send_json(writer, 400, {"error": "非法 run_id 或 JSON"})
            return
        if not isinstance(data, dict):
            await self._send_json(
                writer, 400, {"error": "body 必须是 JSON 对象"})
            return
        request_id = data.get("request_id")
        decision = data.get("decision")
        decided_by = data.get("decided_by", "api")
        if (not isinstance(request_id, str) or not request_id
                or not isinstance(decision, dict)
                or not isinstance(decided_by, str)):
            await self._send_json(writer, 400, {
                "error": "需要 request_id(str) / decision(object) / decided_by(str)"
            })
            return
        store = ApprovalStore(self._output_dir / rid)
        if store.get(request_id) is None:
            await self._send_json(
                writer, 404, {"error": f"审批单不存在：{request_id}"})
            return
        try:
            updated = store.decide(
                request_id, decision, decided_by=decided_by[:80])
        except ApprovalError as exc:
            await self._send_json(writer, 409, {"error": str(exc)})
            return
        self.on_event("approval_decided", {
            "run_id": rid,
            "approval_id": request_id,
            "checkpoint": updated.checkpoint,
            "action": updated.decision.get("action", ""),
        })
        await self._send_json(writer, 200, updated.model_dump(mode="json"))

    async def _handle_post(
        self, path: str, body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        parsed = urlsplit(path)
        p = parsed.path.rstrip("/") or "/"
        if p == "/runs":
            await self._create_run(body, writer)
            return
        segments = [unquote(s) for s in p.split("/") if s]
        # A4：POST /providers/{name}/test（V5 P2：支持 ?mode=stream 流式测速）
        if len(segments) == 3 and segments[0] == "providers" and segments[2] == "test":
            query = parse_qs(parsed.query)
            mode = query.get("mode", ["completion"])[0]
            await self._provider_test(segments[1], writer, mode=mode)
            return
        # 任务级覆盖：POST /runs/{id}/tasks/{task_id}/agent
        if (len(segments) == 5 and segments[0] == "runs"
                and segments[2] == "tasks" and segments[4] == "agent"):
            await self._task_agent(segments[1], segments[3], body, writer)
            return
        if len(segments) == 3 and segments[0] == "runs":
            run_id, action = segments[1], segments[2]
            if action == "cancel":
                await self._cancel_run(run_id, writer)
                return
            if action == "resume":
                await self._resume_run(run_id, writer)
                return
            if action == "approvals":
                await self._decide_approval(run_id, body, writer)
                return
            if action == "agents":
                await self._switch_agent(run_id, body, writer)
                return
            if action == "events":
                try:
                    data = json.loads(body.decode("utf-8")) if body else {}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    await self._send_json(writer, 400, {"error": "invalid JSON"})
                    return
                if not isinstance(data, dict):
                    await self._send_json(
                        writer, 400, {"error": "body 必须是 JSON 对象"})
                    return
                event = data.pop("_event", "custom")
                data.setdefault("run_id", run_id)
                self.on_event(event, data)
                await self._send_json(writer, 202, {"accepted": True})
                return
        await self._send_json(writer, 404, {"error": "Not Found"})

    # ── P1-5：run 生命周期 ────────────────────────────────────────────────

    async def _create_run(self, body: bytes, writer: asyncio.StreamWriter) -> None:
        """``POST /runs``：校验 body → 建 Crew → 后台跑 → 回 run_id。"""
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self._send_json(writer, 400, {"error": "invalid JSON"})
            return
        if not isinstance(data, dict):
            await self._send_json(writer, 400, {"error": "body 必须是 JSON 对象"})
            return
        goal = data.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            await self._send_json(writer, 400, {"error": "goal 不能为空"})
            return
        options = data.get("options") or {}
        if not isinstance(options, Mapping):
            await self._send_json(writer, 400, {"error": "options 必须是对象"})
            return
        option_error = _validate_run_options(options)
        if option_error is not None:
            await self._send_json(writer, 400, {"error": option_error})
            return
        try:
            crew = self._build_crew(goal, options)
        except (ValueError, TypeError) as exc:
            await self._send_json(
                writer, 400, {"error": f"options 无法应用：{exc}"})
            return
        if crew is None:
            await self._send_json(
                writer, 503,
                {"error": "server 未配置 crew_factory/settings，无法创建 run"},
            )
            return
        # 2026-11 并发加固：并发 run 数上限（检查与占用之间无 await，原子）。
        if self._active_run_count >= self._max_concurrent_runs:
            await self._send_json(
                writer, 429,
                {"error": (
                    f"并发 run 数已达上限（{self._max_concurrent_runs}）；"
                    "请等待在跑 run 结束，或调大环境变量 "
                    "AGENTCREW_MAX_CONCURRENT_RUNS")},
            )
            return
        self._active_run_count += 1
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            self._run_crew(crew, lambda: crew.run(goal), started))
        try:
            run_id = await asyncio.wait_for(started, timeout=30)
        except asyncio.CancelledError:
            # handler 被取消时 CancelledError 不属于 Exception，若不显式收口，
            # 后台 run 会继续跑且永远不进 active_crews/_run_tasks 注册表（幽灵 run）。
            task.cancel()
            raise
        except asyncio.TimeoutError:
            task.cancel()
            await self._send_json(
                writer, 504, {"error": "run 启动超时（未收到 run_started）"})
            return
        except Exception as exc:  # crew.run 在首个事件前就炸了
            task.cancel()
            await self._send_json(
                writer, 500, {"error": f"run 启动失败：{type(exc).__name__}: {exc}"})
            return
        self.active_crews[run_id] = crew
        self._run_tasks[run_id] = task
        await self._send_json(
            writer, 202, {"run_id": run_id, "status": "running"})

    def _build_crew(
        self, goal: str, options: Mapping[str, Any]
    ) -> Any | None:
        """经 ``crew_factory`` 建 Crew；缺省用 ``settings`` 现场造一个无人值守实例。"""
        if self._crew_factory is not None:
            return self._crew_factory(goal, options)
        if self._settings is None:
            return None
        from agentcrew.crew.crew import Crew

        # options 已在 _create_run 过白名单与类型校验；这里不再用 except Exception
        # 静默吞掉装配失败（那会让请求回 202 而用户以为配置生效了）。
        settings = (
            self._settings.model_copy(update=dict(options))
            if options else self._settings)
        # R4：deferred 模式把「问人」外包给审批单，run 挂起而不是等 stdin。
        # 这是 API 通道上第一次真正允许人工卡点存在。
        if settings.approval_mode == "deferred":
            from agentcrew.gate.deferred import DeferredPrompter
            gate = (
                settings.gate
                if settings.gate in ("both", "plan", "delivery") else "both"
            )
            settings = settings.model_copy(update={"gate": gate})
            return Crew(settings, on_event=self.on_event,
                        prompter=DeferredPrompter())
        # 其余情况：API 服务器无人值守，人工卡点会在 await prompter 上永久挂起。
        settings = settings.model_copy(update={"gate": "none"})
        return Crew(settings, on_event=self.on_event)

    async def _run_crew(
        self,
        crew: Any,
        coro_factory: Callable[[], Any],
        started: asyncio.Future[str],
    ) -> None:
        """后台跑一个 crew：拦截 ``run_started`` 回传 run_id，结束时摘除注册表。

        ``run_started`` 之前就抛错（如配置错误、规划即崩）时把异常挂进
        ``started``，创建端点立刻收到失败而不是等满 30s 超时；``run_started``
        之后的失败由 crew 自己的 ``run_finished(failed)`` 事件推给 SSE，这里
        不再重抛（后台任务异常无人消费只会刷 warning）。
        """
        self._wire_events(crew, started)
        try:
            await coro_factory()
        except asyncio.CancelledError:
            if not started.done():
                started.cancel()
            raise
        except Exception as exc:
            if not started.done():
                try:
                    started.set_exception(exc)
                except Exception:
                    pass
        finally:
            # 2026-11 并发加固：无论结局如何都释放并发 run 槽位（创建端点在
            # ``create_task`` 前同步占用）。coroutine 已被调度后才可能进入本
            # 函数，因此 ``finally`` 必然执行——槽位不会泄漏。
            self._active_run_count -= 1
            # started 被 wait_for 超时取消后，对已取消的 Future 调 .exception()
            # 会抛 CancelledError，使 finally 当场中断、注册表摘除被跳过。
            if (started.done() and not started.cancelled()
                    and started.exception() is None):
                run_id = started.result()
                self.active_crews.pop(run_id, None)
                self._run_tasks.pop(run_id, None)

    def _wire_events(
        self, crew: Any, started: asyncio.Future[str]
    ) -> None:
        """把 crew 的 ``on_event`` 包一层：``run_started`` 到来时解出 run_id。

        crew 的真实 on_event（本服务器的 ``self.on_event``）照常透传；mock crew
        没有该属性时静默跳过 —— 工厂本应把 on_event 接上。
        """
        original = getattr(crew, "_on_event", None)

        def forward(event: str, payload: dict[str, Any]) -> None:
            if event == "run_started" and not started.done():
                run_id = payload.get("run_id")
                if isinstance(run_id, str) and run_id:
                    started.set_result(run_id)
            if original is not None:
                original(event, payload)

        try:
            crew._on_event = forward
        except Exception:
            pass

    async def _switch_agent(
        self, run_id: str, body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        """``POST /runs/{id}/agents``：热切换某个角色的执行体。

        运行中的 run 立刻生效（下一次派发）；不在跑就写 ``binding_overrides.json``，
        由下一次 resume 接管。两种结果都如实回报 ``applied``，不假装成功。

        body 两种形态：

        - ``{"role": "...", "agent": "backend_fast", "reason": "..."}`` —— 切到指定执行体；
        - ``{"role": "...", "reset": true}`` —— 撤销该角色的运行中覆盖（回到配置）。

        ``reset`` 是幂等的：没切过也返回 200，``changed=false``。
        """
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self._send_json(writer, 400, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            await self._send_json(writer, 400, {"error": "body 必须是 JSON 对象"})
            return
        if "reset" in payload and not isinstance(payload["reset"], bool):
            # bool("false") is True：字符串会变成「撤销覆盖」，与客户端意图相反
            await self._send_json(writer, 400, {"error": "reset 必须是布尔值"})
            return
        role = str(payload.get("role") or "")
        reset = bool(payload.get("reset"))
        agent = str(payload.get("agent") or "")
        if not role or (not reset and not agent):
            await self._send_json(
                writer, 400, {"error": "需要 role，以及 agent 或 reset:true"})
            return
        crew = self.active_crews.get(run_id)
        if crew is not None:
            try:
                if reset:
                    members, changed = crew.reset_agent(role)
                else:
                    members = crew.switch_agent(
                        role, agent, reason=str(payload.get("reason") or "api"))
                    changed = True
            except (ValueError, RuntimeError) as exc:
                await self._send_json(writer, 400, {"error": str(exc)})
                return
            await self._send_json(writer, 200, {
                "applied": True, "effective": "next-dispatch",
                "run_id": run_id, "role": role,
                "reset": reset, "changed": changed,
                "members": [m.profile for m in members],
            })
            return
        await self._send_json(writer, 409, {
            "applied": False, "run_id": run_id,
            "error": "该 run 未在运行中；请用 CLI `switch` 写入后 resume",
        })

    def _task_role(self, run_id: str, task_id: str) -> str:
        """查任务所属角色（API 调用方可以省略 role）。

        优先问正在跑的 Crew 要**板上真实任务**；内存 RunState 只记录
        ``task_transition``，在有的路径下不一定覆盖到每个任务。
        """
        crew = self.active_crews.get(run_id)
        if crew is not None and hasattr(crew, "task_role"):
            role = crew.task_role(task_id)
            if role:
                return role
        run = self.runs.get(run_id)
        task = (run.tasks or {}).get(task_id) if run is not None else None
        return str((task or {}).get("role") or "")

    async def _task_agent(
        self, run_id: str, task_id: str, body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        """``POST /runs/{id}/tasks/{task_id}/agent``：只给这一个任务换执行体。

        与角色级切换互不干扰：任务级覆盖在派发时优先（``agent_for_task``），
        影响该任务的每次 attempt 与返工，别的任务照旧走角色绑定。

        body: ``{"agent": "backend_fast"}`` 或 ``{"reset": true}``；
        ``role`` 可省略——能从 run 的任务状态里查到就自动补。
        """
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self._send_json(writer, 400, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            await self._send_json(writer, 400, {"error": "body 必须是 JSON 对象"})
            return
        if "reset" in payload and not isinstance(payload["reset"], bool):
            await self._send_json(writer, 400, {"error": "reset 必须是布尔值"})
            return
        reset = bool(payload.get("reset"))
        agent = str(payload.get("agent") or "")
        if not reset and not agent:
            await self._send_json(
                writer, 400, {"error": "需要 agent，或 reset:true"})
            return
        crew = self.active_crews.get(run_id)
        if crew is None:
            await self._send_json(writer, 409, {
                "applied": False, "run_id": run_id, "task_id": task_id,
                "error": "该 run 未在运行中；请用 CLI switch --task 写入后 resume",
            })
            return
        role = str(payload.get("role") or "") or self._task_role(run_id, task_id)
        if not role:
            await self._send_json(writer, 400, {
                "error": f"查不到任务 {task_id} 的角色，请在 body 里带 role"})
            return
        try:
            if reset:
                previous, changed = crew.reset_task_agent(task_id)
            else:
                crew.set_task_agent(
                    task_id, agent, role=role,
                    reason=str(payload.get("reason") or "api"))
                previous, changed = agent, True
        except (ValueError, RuntimeError) as exc:
            await self._send_json(writer, 400, {"error": str(exc)})
            return
        await self._send_json(writer, 200, {
            "applied": True, "effective": "next-dispatch", "scope": "task",
            "run_id": run_id, "task_id": task_id, "role": role,
            "reset": reset, "changed": changed, "agent": previous,
        })

    async def _cancel_run(self, run_id: str, writer: asyncio.StreamWriter) -> None:
        """``POST /runs/{id}/cancel``：软取消运行中的 run（202）。"""
        crew = self.active_crews.get(run_id)
        if crew is None:
            await self._send_json(
                writer, 404,
                {"error": f"run {run_id!r} 不在运行中（可先用 POST /runs/{run_id}/resume 恢复）"},
            )
            return
        cancel = getattr(crew, "cancel", None)
        if cancel is None:
            await self._send_json(
                writer, 409, {"error": "该 run 驱动对象不支持取消"})
            return
        cancel()
        await self._send_json(
            writer, 202, {"run_id": run_id, "cancelled": True})

    async def _resume_run(self, run_id: str, writer: asyncio.StreamWriter) -> None:
        """``POST /runs/{id}/resume``：从 checkpoint 恢复 run 并后台执行（202）。"""
        # resume 会 load_board → repair_statuses → 写盘，是**写**路径。CLI 的
        # resume 走 _run_dir() 收口、DELETE 走 _validated_run_id()，这里此前是
        # 唯一一条直接把 run_id 拼进 output_dir 的路径。
        from agentcrew.runtime.checkpoint import _validated_run_id
        from agentcrew.runtime.lifecycle import ResumeLockError

        try:
            _validated_run_id(run_id)
        except ValueError as exc:
            await self._send_json(writer, 400, {"error": str(exc)})
            return
        if run_id in self.active_crews:
            await self._send_json(
                writer, 409, {"error": "run 已在运行中"})
            return
        if self._settings is None:
            await self._send_json(
                writer, 503, {"error": "server 未配置 settings，无法 resume"})
            return
        # 2026-11 并发加固：resume 与 create 共享同一份并发 run 槽位。
        if self._active_run_count >= self._max_concurrent_runs:
            await self._send_json(
                writer, 429,
                {"error": (
                    f"并发 run 数已达上限（{self._max_concurrent_runs}）；"
                    "请等待在跑 run 结束，或调大环境变量 "
                    "AGENTCREW_MAX_CONCURRENT_RUNS")},
            )
            return
        self._active_run_count += 1
        from agentcrew.crew.crew import Crew

        try:
            crew = Crew.resume_from(self._settings, run_id, on_event=self.on_event)
        except (OSError, ValueError) as exc:
            self._active_run_count -= 1  # 未进入后台任务，立即归还槽位
            await self._send_json(
                writer, 404, {"error": f"无法恢复 run {run_id!r}：{exc}"})
            return
        except Exception as exc:
            # 槽位已占用：resume_from 对畸形 tasks.json 抛的 AttributeError /
            # TypeError 等不在上面两类里。不在这里归还，_run_crew 根本没被创建，
            # 槽位会永久泄漏，累计到上限后所有 run 请求永久 429。
            self._active_run_count -= 1
            await self._send_json(
                writer, 500, {"error": f"无法恢复 run {run_id!r}："
                                      f"{type(exc).__name__}: {exc}"})
            return
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            self._run_crew(crew, crew.resume, started))
        try:
            resumed_id = await asyncio.wait_for(started, timeout=30)
        except asyncio.CancelledError:
            task.cancel()
            raise
        except ResumeLockError as exc:
            # R17：别的进程正在续跑这个 run；与「run 已在运行中」同语义。
            task.cancel()
            await self._send_json(writer, 409, {"error": str(exc)})
            return
        except Exception as exc:  # resume 启动失败
            task.cancel()
            await self._send_json(
                writer, 500, {"error": f"resume 启动失败：{type(exc).__name__}: {exc}"})
            return
        self.active_crews[resumed_id] = crew
        self._run_tasks[resumed_id] = task
        await self._send_json(
            writer, 202, {"run_id": resumed_id, "status": "running"})

    async def _list_runs(self, writer: asyncio.StreamWriter) -> None:
        """``GET /runs``：盘上 run.json 列表 + 内存中运行中状态叠加。"""
        from agentcrew.runtime.checkpoint import CheckpointStore

        runs: list[dict[str, Any]] = []
        if self._output_dir is not None and self._output_dir.is_dir():
            for rid in CheckpointStore.list_runs(self._output_dir):
                try:
                    record = CheckpointStore(
                        self._output_dir / rid).load_record()
                except (OSError, ValueError):
                    continue
                runs.append({
                    "run_id": rid,
                    "goal": record.project_goal,
                    "status": record.status.value,
                    "created_at": record.created_at.isoformat(),
                    "updated_at": record.updated_at.isoformat(),
                })
        # 运行中的 crew 以实时状态覆盖盘上记录（run 可能已被取消/收尾）。
        for rid in self.active_crews:
            existing = next((r for r in runs if r["run_id"] == rid), None)
            if existing is not None:
                existing["status"] = "running"
                existing["active"] = True
            else:
                runs.append({
                    "run_id": rid, "goal": "", "status": "running",
                    "created_at": "", "active": True,
                })
        runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        await self._send_json(writer, 200, {"runs": runs})

    # ── P2-9：Ledger 查询与分析 ─────────────────────────────────────────────

    async def _get_ledger(
        self,
        run_id: str,
        query: dict[str, list[str]],
        writer: asyncio.StreamWriter,
    ) -> None:
        """``GET /runs/{id}/ledger?event=&limit=&offset=``：分页查询事件。

        返回 ``{"events": [...], "total": <过滤后的事件总数>}``。run 目录不存在
        返回 404；run 存在但从未 emit（空 ledger.jsonl）返回 200 空列表——
        「没有事件」是诚实答案，不是错误。
        """
        if self._output_dir is None:
            await self._send_json(writer, 500, {"error": "server 未配置 output_dir"})
            return
        from agentcrew.runtime.ledger import LedgerReader

        reader = LedgerReader(self._output_dir)
        try:
            if not reader.run_exists(run_id):
                await self._send_json(
                    writer, 404, {"error": f"run {run_id!r} 不存在"})
                return
            event = query.get("event", [None])[0] or None
            limit = _clamp_int(query.get("limit", ["100"])[0], default=100, minimum=1)
            offset = _clamp_int(query.get("offset", ["0"])[0], default=0, minimum=0)
            # 2026-11 事件循环保护：ledger 查询会物化/遍历整个 JSONL 文件
            # （同步 IO），下放线程池，避免大 run 的读请求卡住事件循环。
            # 串行而非 gather：gather 在其中一个 to_thread 抛异常时不取消兄弟
            # 任务，那个线程会继续把整份 JSONL 读进内存再丢弃。两次只读遍历
            # 同一文件，串行开销可忽略，且异常能当场收敛。
            events = await asyncio.to_thread(
                reader.query_events,
                run_id, event=event, limit=limit, offset=offset)
            total = await asyncio.to_thread(
                reader.count_events, run_id, event=event)
        except ValueError as exc:
            await self._send_json(writer, 400, {"error": str(exc)})
            return
        except OSError as exc:
            await self._send_json(
                writer, 500, {"error": f"ledger 读取失败：{exc}"})
            return
        await self._send_json(
            writer, 200, {"events": events, "total": total})

    async def _get_stats(
        self, run_id: str, writer: asyncio.StreamWriter
    ) -> None:
        """``GET /runs/{id}/stats``：事件分布 / LLM 用量 / 任务成功率 / 工具调用。

        空 ledger 返回各项为零的统计对象（200），只有 run 目录不存在才 404。
        """
        if self._output_dir is None:
            await self._send_json(writer, 500, {"error": "server 未配置 output_dir"})
            return
        from agentcrew.runtime.ledger import LedgerReader

        reader = LedgerReader(self._output_dir)
        try:
            if not reader.run_exists(run_id):
                await self._send_json(
                    writer, 404, {"error": f"run {run_id!r} 不存在"})
                return
            # 2026-11 事件循环保护：统计会全量遍历 ledger，同上下放线程池。
            stats = await asyncio.to_thread(reader.event_stats, run_id)
        except ValueError as exc:
            await self._send_json(writer, 400, {"error": str(exc)})
            return
        except OSError as exc:
            await self._send_json(
                writer, 500, {"error": f"ledger 统计失败：{exc}"})
            return
        await self._send_json(writer, 200, {"run_id": run_id, **stats})

    # ── P1-6：运行时切换模型 ────────────────────────────────────────────────

    async def _handle_put(
        self, path: str, body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        parsed = urlsplit(path)
        segments = [unquote(s) for s in parsed.path.split("/") if s]
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "model":
            await self._put_model(segments[1], body, writer)
            return
        await self._send_json(writer, 404, {"error": "Not Found"})

    async def _get_model(self, run_id: str, writer: asyncio.StreamWriter) -> None:
        """``GET /runs/{id}/model``：当前模型绑定（仅运行中的 run 可读）。"""
        crew = self.active_crews.get(run_id)
        if crew is None:
            await self._send_json(
                writer, 404,
                {"error": f"run {run_id!r} 不在运行中（只能切换运行中的 run）"})
            return
        try:
            bindings = crew.current_model_bindings()
        except RuntimeError as exc:
            await self._send_json(writer, 409, {"error": str(exc)})
            return
        await self._send_json(writer, 200, bindings)

    async def _put_model(
        self, run_id: str, body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        """``PUT /runs/{id}/model``：body ``{"alias": ..., "role": ...}`` 切换模型。"""
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self._send_json(writer, 400, {"error": "invalid JSON"})
            return
        if not isinstance(data, dict):
            await self._send_json(writer, 400, {"error": "body 必须是 JSON 对象"})
            return
        alias = data.get("alias")
        if not isinstance(alias, str) or not alias.strip():
            await self._send_json(writer, 400, {"error": "alias 不能为空"})
            return
        role = data.get("role")
        if role is not None and not isinstance(role, str):
            await self._send_json(writer, 400, {"error": "role 必须是字符串"})
            return
        crew = self.active_crews.get(run_id)
        if crew is None:
            await self._send_json(
                writer, 409,
                {"error": f"run {run_id!r} 不在运行中（只能切换运行中的 run）"})
            return
        try:
            bindings = crew.switch_model(alias, role=role or None)
        except Exception as exc:
            await self._send_json(
                writer, 400, {"error": f"模型切换失败：{exc}"})
            return
        await self._send_json(writer, 200, bindings)

    async def _handle_delete(self, path: str, writer: asyncio.StreamWriter) -> None:
        """``DELETE /runs/{run_id}?confirm=1``：删除单个 run（P0-4）。

        必须带 ``?confirm=1`` 否则返回 400（防误删）。``running`` 状态由
        ``delete_run`` 内部拒绝（返回 409）。删除成功返回释放字节数。
        """
        parsed = urlsplit(path)
        segments = [unquote(s) for s in parsed.path.split("/") if s]
        if len(segments) != 2 or segments[0] != "runs":
            await self._send_json(writer, 404, {"error": "Not Found"})
            return
        run_id = segments[1]
        query = parse_qs(parsed.query)
        if query.get("confirm", ["0"])[0] != "1":
            await self._send_json(writer, 400, {
                "error": "删除需显式确认：追加 ?confirm=1",
            })
            return
        if self._output_dir is None:
            await self._send_json(writer, 500, {"error": "server 未配置 output_dir"})
            return
        from agentcrew.runtime.checkpoint import delete_run
        try:
            freed = delete_run(self._output_dir, run_id)
        except FileNotFoundError:
            await self._send_json(writer, 404, {"error": f"run {run_id!r} 不存在"})
            return
        except RuntimeError as exc:
            # running 状态拒绝删除 → 409 Conflict
            await self._send_json(writer, 409, {"error": str(exc)})
            return
        except ValueError as exc:
            # 与 /ledger、/stats 同一口径：非法 run_id 是客户端错误（400），
            # 不是服务端故障（此前归 500）。
            await self._send_json(writer, 400, {"error": str(exc)})
            return
        except OSError as exc:
            await self._send_json(writer, 500, {"error": str(exc)})
            return
        # 从内存注册表中移除（如果存在）
        self.runs.pop(run_id, None)
        await self._send_json(writer, 200, {
            "deleted": run_id,
            "freed_bytes": freed,
        })

    # ── A4/A5：Provider 目录、模型发现与测速 ────────────────────────────────

    async def _handle_providers_get(
        self, segments: list[str], writer: asyncio.StreamWriter,
    ) -> None:
        """GET /providers 或 GET /providers/{name}/models。"""
        from agentcrew.models.config import build_directory

        if self._settings is None:
            await self._send_json(writer, 503, {"error": "server 未配置 settings"})
            return

        directory = build_directory(
            custom=self._settings.custom_providers,
            overrides=self._settings.provider_overrides,
        )
        builtin_names = set(PROVIDER_PRESETS_NAMES)

        if len(segments) == 1:
            # GET /providers —— 返回完整目录
            entries = []
            import os
            for name in sorted(directory):
                preset = directory[name]
                env_name = preset.api_key_env or ""
                key_configured = bool(env_name) and bool(
                    os.environ.get(env_name, "").strip())
                # 健康状态：从 factory 的 _health 缓存里找该 provider 的任意 alias
                health = self._provider_health(name)
                entries.append({
                    "name": name,
                    "kind": "builtin" if name in builtin_names else "custom",
                    "implementation": preset.implementation,
                    "base_url": preset.base_url,
                    "api_key_env": env_name,
                    "key_configured": key_configured,
                    "capabilities": preset.capabilities.model_dump(),
                    "suggested_models": list(preset.suggested_models),
                    "health": health,
                    # ── V5 P1：新增元数据字段（向后兼容，仅增字段）─────────
                    "category": preset.category,
                    "family": preset.family,
                    "search_aliases": list(preset.search_aliases),
                    "endpoint_candidates": (
                        list(preset.endpoint_candidates)
                        if preset.endpoint_candidates is not None else None
                    ),
                    "api_key_url": preset.api_key_url,
                    # ── V6 P1：价格/窗口/并发/RPM 元数据（向后兼容，仅增字段）─
                    "input_price": preset.input_price,
                    "output_price": preset.output_price,
                    "context_window": preset.context_window,
                    "max_output_tokens": preset.max_output_tokens,
                    "fallback_models": list(preset.fallback_models),
                    "max_concurrency": preset.max_concurrency,
                    "max_rpm": preset.max_rpm,
                })
            await self._send_json(writer, 200, {"providers": entries})
            return

        if len(segments) == 3 and segments[2] == "models":
            # GET /providers/{name}/models —— 实时拉取模型列表（带 60s 缓存）
            name = segments[1]
            if name not in directory:
                await self._send_json(
                    writer, 404, {"error": f"未知 provider：{name!r}"})
                return
            await self._discover_models(name, directory[name], writer)
            return

        await self._send_json(writer, 404, {"error": "Not Found"})

    def _provider_health(self, provider: str) -> dict[str, Any] | None:
        """从 factory 的健康缓存里取该 provider 任意 alias 的最新记录。"""
        factory = self._model_factory
        if factory is None:
            return None
        # health 按 alias 存，找以 f"{provider}:" 开头的最新记录
        best: Any = None
        for alias, rec in getattr(factory, "_health", {}).items():
            if not alias.startswith(f"{provider}:"):
                continue
            if best is None or rec.last_check > best.last_check:
                best = rec
        if best is None:
            return None
        return {
            "status": best.status,
            "latency_ms": best.latency_ms,
            "last_check": best.last_check.isoformat(),
            "error": best.error,
        }

    async def _discover_models(
        self, name: str, preset: Any, writer: asyncio.StreamWriter,
    ) -> None:
        """实时拉取模型列表，带 60s 进程内缓存。"""
        import time

        from agentcrew.models.config import parse_alias
        from agentcrew.models.discovery import ModelDiscoveryError, fetch_models

        settings = self._settings
        if settings is None:
            await self._send_json(
                writer, 503, {"error": "server 未配置 settings"})
            return

        # 检查缓存
        cached = self._discovery_cache.get(name)
        if cached is not None:
            _ts, cached_payload = cached
            if time.monotonic() - _ts < 60:
                await self._send_json(writer, 200, cached_payload)
                return

        # 构造临时 binding
        dummy_model = preset.suggested_models[0] if preset.suggested_models else "test"
        binding = parse_alias(
            f"{name}:{dummy_model}",
            overrides=settings.provider_overrides,
            custom=settings.custom_providers,
        )
        api_key = settings.api_key_for(name) or ""
        try:
            models = await fetch_models(binding, api_key=api_key)
        except ModelDiscoveryError as exc:
            # 错误不进缓存：瞬时网络抖动/鉴权问题应允许下一次请求立即重试，
            # 而不是把上次的失败结果钉住 60s（用户点「刷新模型」应是实时的）。
            result_payload: dict[str, Any] = {
                "provider": name,
                "error": {"kind": exc.kind, "message": str(exc)},
            }
            await self._send_json(writer, 200, result_payload)
            return
        result_payload = {"provider": name, "models": models}
        # 只有成功结果才缓存（模型列表不常变；错误实时透传）。
        self._discovery_cache[name] = (time.monotonic(), result_payload)
        await self._send_json(writer, 200, result_payload)

    async def _provider_test(
        self,
        name: str,
        writer: asyncio.StreamWriter,
        *,
        mode: str = "completion",
    ) -> None:
        """POST /providers/{name}/test —— 按需测速，发 max_tokens=1 的 ping。

        ``mode=completion``（默认）：走普通补全探测，返回 ``{ok, latency_ms, error}``；
        ``mode=stream``：走 SSE 流式探测，返回额外 ``stream_ok`` 字段。
        """
        from agentcrew.models.config import build_directory

        settings = self._settings
        if settings is None:
            await self._send_json(
                writer, 503, {"error": "server 未配置 settings"})
            return
        factory = self._model_factory
        if factory is None:
            await self._send_json(
                writer, 503,
                {"error": "server 未配置 model_factory，无法测速"})
            return
        directory = build_directory(
            custom=settings.custom_providers,
            overrides=settings.provider_overrides,
        )
        preset = directory.get(name)
        if preset is None:
            await self._send_json(writer, 404, {"error": f"未知 provider：{name!r}"})
            return
        # 测速用模型：优先预设的 suggested_models[0]；若为空则从 default_model
        # 解析该 provider 的模型；再不行才用占位名（此时 provider 通常不可达，
        # 错误信息会如实反映，不会误报"连通"）。
        dummy_model = preset.suggested_models[0] if preset.suggested_models else None
        if dummy_model is None and settings.default_model:
            try:
                from agentcrew.models.config import parse_alias
                _parsed = parse_alias(
                    settings.default_model,
                    overrides=settings.provider_overrides,
                    custom=settings.custom_providers,
                )
                if _parsed.provider == name:
                    dummy_model = _parsed.model
            except Exception:
                pass
        if dummy_model is None:
            dummy_model = "ping"  # 最后兜底：比 "test" 更不可能是真实模型名，错误更清晰
        alias = f"{name}:{dummy_model}"
        if mode == "stream":
            # V5 P2：SSE 流式测速 —— 发 stream=true 请求，验证第一个 data 帧。
            try:
                record = await factory.health_check_stream(alias)
            except Exception as exc:
                await self._send_json(writer, 200, {
                    "ok": False, "stream_ok": False, "latency_ms": None,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                return
            await self._send_json(writer, 200, {
                "ok": bool(record.stream_ok),
                "stream_ok": record.stream_ok,
                "latency_ms": record.latency_ms,
                "error": record.error,
            })
            return
        # 默认：普通补全探测（保持原有返回字段）
        try:
            record = await factory.health_check(alias)
        except Exception as exc:
            await self._send_json(writer, 200, {
                "ok": False, "latency_ms": None,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return
        await self._send_json(writer, 200, {
            "ok": record.status != "unavailable",
            "latency_ms": record.latency_ms,
            "error": record.error,
        })

    async def _handle_sse(self, run_id: str, writer: asyncio.StreamWriter) -> None:
        await http_transport.sse_headers(writer)
        queue = self.bus.subscribe()
        try:
            # 发送初始连接事件
            await http_transport.write_sse(
                writer, http_transport.sse_frame("connected", {"run_id": run_id}))
            async for payload in self._iter_queue(queue):
                await http_transport.write_sse(writer, payload)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.bus.unsubscribe(queue)

    @staticmethod
    async def _iter_queue(queue: asyncio.Queue[str]) -> AsyncIterator[str]:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=30)
                yield item
            except asyncio.TimeoutError:
                # SSE 心跳注释行，防止代理超时
                yield ": ping\n\n"

    @staticmethod
    async def _send_json(
        writer: asyncio.StreamWriter,
        status: int,
        body: dict[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        """写一条 JSON 响应（实现见 :func:`agentcrew.api.http.send_json`）。"""
        await http_transport.send_json(
            writer, status, body, extra_headers=extra_headers)
