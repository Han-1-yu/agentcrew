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
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from agentcrew.api import http as http_transport
from agentcrew.api.auth import APIKeyAuth, RateLimiter
from agentcrew.api.paths import PathRejected, canonical_target
from agentcrew.api.rbac import (
    ALL_SCOPES,
    SCOPE_RUNS_WRITE,
    SCOPE_WEBHOOK_TRIGGER,
    Principal,
    current_principal,
    read_owner,
    required_scope,
    set_current,
    write_owner,
)
from agentcrew.api.run_options import validate_images, validate_run_options
from agentcrew.models.config import PROVIDER_PRESETS as PROVIDER_PRESETS_NAMES
from agentcrew.utils.logger import redact

_logger = logging.getLogger("agentcrew.api")


def _validate_run_options(options: Mapping[str, Any]) -> str | None:
    """兼容别名：校验逻辑已收敛到 :mod:`agentcrew.api.run_options`（P1-11）。

    保留这个名字是因为库层与既有测试按它导入；实现只有一份，不再有
    「HTTP 端校验、其他入口放行」的第二套规则。
    """
    return validate_run_options(options)


def _parse_network(item: str) -> Any:
    """P0-4：把一个 IP/CIDR 文本解析成网段对象；坏值记日志并返回 ``None``。

    ``Settings`` 加载层已用 field_validator 拒绝坏值，这里只兜 ``model_copy``
    等绕过 pydantic 的路径——坏项跳过（fail-closed：宁可少信一个网段）。
    """
    import ipaddress

    try:
        return ipaddress.ip_network(item.strip(), strict=False)
    except ValueError:
        _logger.warning("trusted_proxies 项 %r 无法解析，已跳过", item)
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
    """发布/订阅事件总线：SSE 客户端订阅，Crew 的 on_event 发布。

    **按 run 分桶**（安全止血项）：一进程可以同时跑多个 run，而 ``/runs/{id}/
    events`` 是「看这一个 run 的实时状态」的接口。改造前 ``publish`` 广播给所有
    订阅者，A run 的 goal / 产物摘要 / 审批内容会推到 B run 的连接上——同进程
    内跨 run 的信息泄露，且订阅者越多泄露面越大。

    分桶规则：
    - 订阅时声明 ``run_id``：只收「同一 run」的事件，以及**没有** ``run_id`` 的
      事件（全局广播，如服务级通知）；
    - 订阅时不声明（``run_id=None``）：收全部（库层/测试用的通配订阅）。
    """

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue[str], str | None] = {}

    def publish(self, event: str, data: dict[str, Any]) -> None:
        """发布事件；只投给关心这个 run 的订阅者（非阻塞，慢客户端丢弃）。"""
        payload = http_transport.sse_frame(event, data)
        run_id = str(data.get("run_id") or "")
        for queue, want in list(self._subscribers.items()):
            if want is not None and run_id and want != run_id:
                continue   # 别的 run 的事件：不投递，连队列都不碰
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # 慢客户端丢弃，不阻塞发布者

    def subscribe(self, run_id: str | None = None) -> asyncio.Queue[str]:
        """订阅事件流（``run_id`` 为空 = 通配）；用完须调用 :meth:`unsubscribe`。"""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers[queue] = run_id or None
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.pop(queue, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)



#: 请求体补读超时（秒）：绝大多数请求一次 ``read`` 就凑满，只有跨 TCP 段到达
#: 或客户端少发时才进补读；短超时把「声明长度却不发包」收口为 400，不会死等。
_BODY_READ_TIMEOUT = 1.0


def _clamp_int(
    raw: str, *, default: int, minimum: int, maximum: int | None = None,
) -> int:
    """把查询参数里的整数读出来；非法 / 缺失用 ``default``，压到 ``[minimum, maximum]``。"""
    return http_transport.clamp_int(
        raw, default=default, minimum=minimum, maximum=maximum)


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
    # D-7：Prometheus 文本指标（与 /health 同一条认证链）
    ("GET", "/metrics"),
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
    ("GET", "/runs/{run_id}/agents"),
    ("POST", "/runs/{run_id}/tasks/{task_id}/agent"),
    ("GET", "/providers"),
    ("GET", "/providers/{name}/models"),
    ("POST", "/providers/{name}/test"),
    ("GET", "/tools"),
    ("POST", "/tools/{name}/test"),
    ("GET", "/keys"),
    ("POST", "/keys"),
    ("DELETE", "/keys/{key_id}"),
    ("GET", "/flows"),
    ("POST", "/flows"),
    ("GET", "/flows/{name}"),
    ("DELETE", "/flows/{name}"),
    ("POST", "/flows/validate"),
    # C8：入站 Webhook 触发（认证方式是 HMAC 签名，见 _webhook_trigger）
    ("POST", "/webhooks/trigger"),
)


#: C8：入站 Webhook 触发的签名时间戳容差（秒）。窗口内可重放一次的代价远小于
#: 引入 nonce 存储的复杂度——与出站 R12 的口径一致。
_WEBHOOK_TRIGGER_MAX_DRIFT = 300


class ApiServer:
    """轻量 async HTTP 服务器 + SSE 事件流。"""

    #: B-9：内存中 run 状态镜像的容量上限。
    #:
    #: 这个注册表只为「本进程见过哪些 run 的状态」服务，唯一的清理途径是
    #: ``DELETE /runs/{id}``。长期运行的常驻服务会不断见到新 run_id（尤其配合
    #: 定时任务），没有上界就是一条慢性内存泄漏。超限后不再建新镜像——事件仍会
    #: 经 EventBus 推给 SSE 订阅者，只是 ``/runs`` 列表少一条。
    MAX_TRACKED_RUNS: int = 500

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        event_bus: EventBus | None = None,
        output_dir: Path | None = None,
        settings: Any | None = None,
        crew_factory: Callable[..., Any] | None = None,
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
        #: D-7：最小可观测性状态。``_status_counts`` 按 HTTP 状态码累计请求数，
        #: ``_started_at`` 用于 ``agentcrew_process_start_time_seconds``。
        #: 只做计数（O(1)、无锁），不引入 Prometheus 客户端依赖。
        self._status_counts: dict[int, int] = {}
        self._started_at = time.time()
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
        # P0-4：预解析可信代理网段（每请求只做 ``in`` 判断，不重复解析 CIDR）。
        # 坏值跳过并落日志——Settings 加载层已校验，这里只兜 model_copy 旁路。
        self._trusted_networks: tuple[Any, ...] = tuple(
            net for net in (
                _parse_network(item)
                for item in getattr(settings, "trusted_proxies", ()) or ()
            ) if net is not None
        )

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
        run_id = str(data.get("run_id") or "")
        # B-9：run_id 为空时立刻返回。此前只有「建表」那段判了空，后面的
        # `task_transition` 分支直接用 ``self.runs[""]`` → KeyError → 500，
        # 且异常文本会回显给客户端。
        if not run_id:
            return
        state = self.runs.get(run_id)
        if state is None:
            # B-9：注册表上界。此前唯一清理途径是 DELETE，长期运行的服务会被
            # 无数 run_id 撑大内存。超限后不再建内存镜像（事件仍已通过 bus
            # 推给订阅者），并留一条 warning 便于运维发现。
            if len(self.runs) >= self.MAX_TRACKED_RUNS:
                _logger.warning(
                    "内存中的 run 注册表已达上限 %d，忽略 run %s 的状态镜像；"
                    "请用 DELETE /runs/{id} 清理不再关心的 run",
                    self.MAX_TRACKED_RUNS, run_id)
                return
            state = self.runs[run_id] = RunState(run_id=run_id)
        if event == "task_transition":
            task_id = data.get("task_id", "")
            if task_id:
                state.tasks[task_id] = {
                    "status": data.get("to_status", ""),
                    "role": data.get("role", ""),
                }
        elif event == "run_started":
            state.status = "running"
            goal = data.get("goal")
            if isinstance(goal, str) and goal:
                state.goal = goal
        elif event == "run_paused":
            # R4：审批挂起不是终局，不能落到 run_finished 的 else→failed。
            state.status = str(
                data.get("status") or "awaiting_approval")
        elif event == "run_completed":
            state.status = "completed"
        elif event == "run_failed":
            state.status = "failed"
        elif event == "run_finished":
            # P1-5：Crew 封版统一发这一个事件（含 completed / cancelled /
            # interrupted / failed / planned），按状态落到徽章。
            status = data.get("status", "")
            if status == "completed":
                state.status = "completed"
            elif status in ("cancelled", "interrupted", "planned"):
                state.status = status
            else:
                state.status = "failed"

    # ── HTTP 处理 ────────────────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await http_transport.read_request(reader)
            if request is None:
                return
            # P0：**只在这里解码一次**。规范路径同时喂给授权中间件与路由分发，
            # 两侧看到同一个字符串，`/%6beys` 这类「授权端匹配不上、分发端解成
            # /keys」的编码绕过就没有生存空间（见 agentcrew/api/paths.py）。
            try:
                path, query = canonical_target(request.path)
            except PathRejected as exc:
                await self._send_json(
                    writer, 400, {"error": f"非法请求路径：{exc}"})
                return
            # E-3 中间件链（路由分发前）：先限流后认证。任一拦截直接回响应并
            # return，不再读 body / 不再分发——被拒请求不该再触发任何业务逻辑。
            if await self._apply_middleware(
                    request.method, path, request.headers, writer):
                return
            query_params = parse_qs(query)
            if request.method == "GET":
                await self._handle_get(path, query_params, writer)
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
                    await self._handle_post(path, query_params, body, writer,
                                            headers=request.headers)
                else:
                    await self._handle_put(path, query_params, body, writer)
            elif request.method == "DELETE":
                await self._handle_delete(path, query_params, writer)
            else:
                await self._send_json(writer, 405, {"error": "Method Not Allowed"})
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:
            # 兜底：任何未预料异常都必须给客户端一个 HTTP 响应。此前 OSError 会
            # 穿透所有 except，finally 只关连接 → 客户端拿到 0 字节、服务端无记录。
            # P0-5：详情（路径/配置/密钥片段）只进日志，响应体固定文案 + 类型名。
            _logger.error("unhandled api error on %s: %s",
                          request.path if request else "-",
                          self._internal_detail(exc))
            try:
                await self._send_json(
                    writer, 500, self._internal_error_body(exc))
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
        method: str,
        path: str,
        headers: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> bool:
        """路由分发前的保护层；返回 ``True`` 表示已拦截（调用方停止分发）。

        顺序固定为「先限流、后认证、再授权」：被刷爆的客户端不该再消耗认证比对，
        被拒的调用也不该触达业务逻辑。三者都写 :attr:`security_events`——事件名与
        data 只含 client_ip / path / remaining / 主体名与作用域，**绝不回显客户端
        发来的 key**（key 只参与哈希比对，不进任何持久化结构）。

        R25：认证成功后解析出主体（admin 角色 / 密钥身份 / legacy 单密钥），写进
        ContextVar 供 handler 读；随后按 :func:`required_scope` 校验作用域，缺权限
        回 403（不是 401——身份是有效的，只是没被授权）。
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
        # C8：入站 Webhook 触发有两条认证路径——
        #   ① 携带 HMAC 签名头（X-AgentCrew-Signature）→ 免 API Key，进 handler
        #      做签名校验（需要先读 body，只能在 handler 里做；失败 401 留痕）；
        #   ② 没有签名头 → 按普通 API Key 路由走（无 key 同样 401，与全站
        #      「每条路由无凭证必须 401」的不变量保持一致，不给探测者任何额外信息）。
        if (method == "POST" and path == "/webhooks/trigger"
                and headers.get("x-agentcrew-signature")):
            # 标记为「待 HMAC 验证」：一个没有作用域的占位主体，让 handler 能
            # 区分「认证已通过」与「签名待验」，不会被 anonymous-admin 兜底骗过。
            set_current(Principal(name="webhook-pending", role="webhook",
                                  scopes=frozenset()))
            return False
        # 2) API Key 认证（Bearer / X-API-Key）+ 3) 作用域授权
        auth = self._auth
        principal: Any = None
        if auth is not None and auth.enabled:
            resolver = getattr(auth, "principal", None)
            if resolver is not None:
                principal = resolver(headers)
            else:
                principal = (
                    Principal(name="legacy-key", role="admin",
                              scopes=ALL_SCOPES, key_id="legacy")
                    if auth.authenticate(headers) else None
                )
            if principal is None:
                self._record_security("api_auth_failed", {
                    "client_ip": client_ip, "path": path,
                })
                await self._send_json(
                    writer, 401,
                    {"error": "Unauthorized"},
                    extra_headers={"WWW-Authenticate": 'Bearer realm="agentcrew"'},
                )
                return True
            scope = required_scope(method, path)
            if not principal.allows(scope):
                self._record_security("api_forbidden", {
                    "client_ip": client_ip, "path": path,
                    "principal": principal.name, "role": principal.role,
                    "required_scope": scope,
                })
                await self._send_json(
                    writer, 403,
                    {"error": f"权限不足：需要作用域 {scope}（当前角色 "
                              f"{principal.role} 不具备）"},
                )
                return True
        set_current(principal)
        return False

    # ── R25：run 可见性守卫 ──────────────────────────────────────────────

    def _run_owner_name(self, run_id: str) -> str | None:
        """run 目录里的所有者名；非法 run_id / 无标记一律 ``None``。"""
        if self._output_dir is None:
            return None
        from agentcrew.runtime.checkpoint import _validated_run_id

        try:
            rid = _validated_run_id(run_id)   # 防路径穿越，与各 handler 同一校验
        except ValueError:
            return None
        return read_owner(self._output_dir / rid)

    async def _deny_if_run_hidden(
        self, run_id: str, writer: asyncio.StreamWriter
    ) -> bool:
        """非 admin 只能碰自己创建的 / 白名单里的 run；不可见回 **404**。

        用 404 而不是 403：403 会泄露「这个 run 存在但不属于你」，等于让人枚举
        别人的 run_id。返回 ``True`` 表示已拒绝（调用方 return）。
        """
        principal = current_principal()
        if principal.can_see_run(self._run_owner_name(run_id)):
            return False
        self._record_security("api_forbidden", {
            "path": f"/runs/{run_id}", "principal": principal.name,
            "role": principal.role, "required_scope": "run-visibility",
        })
        await self._send_json(writer, 404, {"error": "run not found"})
        return True

    def _client_ip(
        self, headers: dict[str, str], writer: asyncio.StreamWriter
    ) -> str:
        """客户端 IP（P0-4）：XFF 只在 peer 属于可信代理网段时才被解析。

        可信网段来自 ``Settings.trusted_proxies``（``AGENTCREW_TRUSTED_PROXIES``，
        逗号分隔的 IP/CIDR）；空 = 完全不信 XFF，限流/审计只用 socket peer。
        """
        return http_transport.client_ip(
            headers, writer, trusted_proxies=self._trusted_networks)

    def _record_security(self, event: str, data: dict[str, Any]) -> None:
        """追加一条服务端安全审计事件（内存；与 Ledger 事件同形：event + data）。"""
        self.security_events.append({"event": event, "data": dict(data)})

    def _internal_detail(self, exc: BaseException) -> str:
        """P0-5：未预料异常的**服务端侧**详情——密钥脱敏 + 截断，只写日志。"""
        try:
            values = self._settings.secret_values() if self._settings else []
            text = redact(
                f"{type(exc).__name__}: {exc}", values)
        except Exception:  # noqa: BLE001 —— 脱敏自身失败也不能挡住记日志
            text = type(exc).__name__
        return text[:500]

    def _internal_error_body(self, exc: BaseException) -> dict[str, str]:
        """P0-5：500 响应体——固定文案 + 异常类型名（不含任何变量内容）。

        ``str(exc)`` 里常带绝对路径、配置片段、密钥错误文本（SDK 会把该文案
        原样抛给调用方），一律不进响应；完整详情走 :meth:`_internal_detail`
        落服务端日志。
        """
        return {"error": f"服务端内部错误（{type(exc).__name__}）"}

    async def _handle_get(
        self, path: str, query: dict[str, list[str]], writer: asyncio.StreamWriter,
    ) -> None:
        # ``path`` 已由 _handle_connection 规范化（解码 + 去尾斜杠 + 折叠斜杠），
        # 与授权中间件用的是同一个字符串；这里再 unquote 一次就等于制造第二种
        # 解释，正是路径绕过的成因。查询串单独传入，不参与路径语义。
        p = path
        if p == "/" or p == "/index.html":
            # 看板页面按需 import：dashboard 是纯字符串大常量，只有访问根路径时
            # 才需要它，别让每个 API 请求都背着它。
            from agentcrew.api.dashboard import DASHBOARD_HTML
            await http_transport.send_html(writer, DASHBOARD_HTML)
            return
        if p == "/health":
            await self._send_json(writer, 200, {"status": "ok"})
            return
        if p == "/metrics":
            # D-7：Prometheus 文本格式。走与 /health 相同的认证链（默认开启时
            # 抓取端必须带 key），避免把运行态信息暴露在匿名端口上。
            await self._handle_metrics(writer)
            return
        if p == "/runs":
            await self._list_runs(writer)
            return
        if p == "/tools":
            await self._list_tools(writer)
            return
        if p == "/keys":
            await self._list_keys(writer)
            return
        if p == "/flows":
            await self._list_flows(writer)
            return
        if p.startswith("/flows/"):
            await self._get_flow(p[len("/flows/"):], writer)
            return
        segments = [s for s in p.split("/") if s]
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
                if await self._deny_if_run_hidden(run_id, writer):
                    return
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
            if action == "agents":
                await self._get_agents(run_id, writer)
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
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
        # R25：审计身份取主体名（真实密钥身份），不再信任 body 的自由文本；
        # 未启用 RBAC（anonymous）或 legacy 单密钥时才回退到 body。
        principal = current_principal()
        if principal.name not in ("anonymous", "legacy-key"):
            decided_by = principal.name
        else:
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
        self, path: str, query: dict[str, list[str]], body: bytes,
        writer: asyncio.StreamWriter, headers: dict[str, str] | None = None,
    ) -> None:
        # ``path`` 已是规范路径（见 _handle_get 的说明），不再二次 unquote。
        p = path
        if p == "/webhooks/trigger":
            await self._webhook_trigger(headers or {}, body, writer)
            return
        if p == "/runs":
            await self._create_run(body, writer)
            return
        segments = [s for s in p.split("/") if s]
        # A4：POST /providers/{name}/test（V5 P2：支持 ?mode=stream 流式测速）
        if len(segments) == 3 and segments[0] == "providers" and segments[2] == "test":
            mode = query.get("mode", ["completion"])[0]
            await self._provider_test(segments[1], writer, mode=mode)
            return
        # R24：POST /tools/{name}/test —— 单工具试跑（沙箱隔离）
        if len(segments) == 3 and segments[0] == "tools" and segments[2] == "test":
            await self._tool_test(segments[1], body, writer)
            return
        # R25：POST /keys —— 新建密钥（明文只回这一次）
        if p == "/keys":
            await self._create_key(body, writer)
            return
        # R26：POST /flows（保存）与 /flows/validate（只校验不落盘）
        if p == "/flows":
            await self._save_flow(body, writer)
            return
        if p == "/flows/validate":
            await self._validate_flow(body, writer)
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
                # P0-2：与其余 run 级端点同一道归属校验（此前是唯一漏掉的
                # 一个）——不校验的话，任意 ``runs:write`` key 可以向别人的
                # run 注入事件：on_event 会改写 self.runs[run_id] 的状态并经
                # EventBus 推给该 run 的全部 SSE 订阅者。
                if await self._deny_if_run_hidden(run_id, writer):
                    return
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
                # P0-3 第一层：入口就拒绝非法事件名（sse_frame 里还有第二层
                # 降级兜底），带换行的名字可伪造任意 SSE 帧。
                if not http_transport.valid_event_name(event):
                    await self._send_json(
                        writer, 400,
                        {"error": "_event 必须是 1-64 位字母/数字/./_/- 的事件名"})
                    return
                # 强制覆盖（不能用 setdefault）：body 自带的 run_id 不允许
                # 把事件记到/推到另一个 run 名下。
                data["run_id"] = run_id
                self.on_event(event, data)
                await self._send_json(writer, 202, {"accepted": True})
                return
        await self._send_json(writer, 404, {"error": "Not Found"})

    # ── P1-5：run 生命周期 ────────────────────────────────────────────────

    async def _create_run(
        self, body: bytes, writer: asyncio.StreamWriter,
        *, trigger_source: str | None = None,
    ) -> None:
        """``POST /runs``：校验 body → 建 Crew → 后台跑 → 回 run_id。

        ``trigger_source``（C8）非空时，在 run ledger 里落一条 ``run_triggered``
        ——「这次 run 是谁发起的」要能从审计里直接读出来，而不是靠猜。
        """
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
        # C4：可选的随 run 图片（多模态输入）。校验失败回 400——
        # 带着坏图片起跑，模型只会看到半截 base64。
        images = data.get("images") or []
        try:
            images = validate_images(images)
        except ValueError as exc:
            await self._send_json(
                writer, 400, {"error": f"images 不合法：{exc}"})
            return
        try:
            crew = self._build_crew(goal, options, images)
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
            # R25：记录 run 归属（key_id + 身份名），非 admin 只能看自己的 run。
            write_owner(self._output_dir / run_id, current_principal()) \
                if self._output_dir is not None else None
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
            _logger.error("run start failed: %s", self._internal_detail(exc))
            await self._send_json(
                writer, 500,
                {"error": f"run 启动失败（{type(exc).__name__}）"})
            return
        self.active_crews[run_id] = crew
        self._run_tasks[run_id] = task
        if trigger_source and self._output_dir is not None:
            self._emit_run_triggered(run_id, trigger_source)
        await self._send_json(
            writer, 202, {"run_id": run_id, "status": "running"})

    async def _webhook_trigger(
        self, headers: dict[str, str], body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        """``POST /webhooks/trigger``（C8）：HMAC 签名认证 + 触发 run。

        认证语义复用 R12 出站签名（``X-AgentCrew-Timestamp`` +
        ``X-AgentCrew-Signature: sha256=<hex>``，签 ``<timestamp>.<body>``）：
        时间戳偏离超过 300 秒拒绝（防重放），比较用常量时间。成功后以
        ``webhook`` 主体（只有 ``runs:write``）创建 run，来源进 ledger。
        """
        from agentcrew.runtime.webhook import verify_signature

        secret = ""
        if self._settings is not None:
            secret = str(getattr(self._settings, "webhook_trigger_secret", "")
                         or "")
        if not secret:
            await self._send_json(writer, 503, {
                "error": "入站触发未启用：请配置 webhook_trigger_secret"
                         "（或 AGENTCREW_WEBHOOK_TRIGGER_SECRET）",
            })
            return
        # 已持有效 API Key 且具备触发作用域（或认证整体关闭）→ 不要求 HMAC：
        # 这条请求已经过了与全站一致的认证链，签名是给「不持 key 的外部系统」
        # 用的另一条路径，不该变成对 key 调用方的额外负担。
        if not current_principal().allows(SCOPE_WEBHOOK_TRIGGER):
            timestamp = headers.get("x-agentcrew-timestamp", "")
            signature = headers.get("x-agentcrew-signature", "")
            try:
                drift = abs(time.time() - int(timestamp))
            except ValueError:
                drift = None
            if drift is None or drift > _WEBHOOK_TRIGGER_MAX_DRIFT:
                self._record_security("webhook_signature_invalid", {
                    "path": "/webhooks/trigger", "reason": "stale_timestamp"})
                await self._send_json(
                    writer, 401, {"error": "签名时间戳缺失或已过期（防重放）"})
                return
            if not verify_signature(secret, timestamp, body, signature):
                self._record_security("webhook_signature_invalid", {
                    "path": "/webhooks/trigger", "reason": "bad_signature"})
                await self._send_json(writer, 401, {"error": "签名校验失败"})
                return
            # 签名即身份：这个主体只有 runs:write，不能看别人的 run、不能动审批。
            set_current(Principal(
                name="webhook", role="webhook",
                scopes=frozenset({SCOPE_RUNS_WRITE})))
        await self._create_run(body, writer, trigger_source="webhook")

    def _emit_run_triggered(self, run_id: str, source: str) -> None:
        """把「本次 run 由什么触发」写进 run ledger（C8；失败不影响 run）。"""
        root = self._output_dir
        if root is None:  # 调用方已保证非空；这里只为类型收窄
            return
        try:
            from agentcrew.runtime.ledger import Ledger

            # run 目录在真实 run 里由 Crew 创建；这里兜底建一次，保证审计事件
            # 不会因为目录尚不存在而被吞掉（吞掉等于触发来源不可审计）。
            ledger_path = root / run_id / "ledger.jsonl"
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            Ledger(ledger_path, run_id=run_id).emit(
                "run_triggered", source=source)
        except (OSError, RuntimeError) as exc:
            _logger.warning("run_triggered 落账失败 run=%s: %s", run_id, exc)

    def _build_crew(
        self, goal: str, options: Mapping[str, Any],
        images: list[dict[str, str]] | None = None,
    ) -> Any | None:
        """经 ``crew_factory`` 建 Crew；缺省用 ``settings`` 现场造一个无人值守实例。

        ``images``（C4）：随 run 附带的图片，交给 Crew 下发给每个 Agent；
        自定义 ``crew_factory`` 优先尝试三参签名，旧的两参替身照常兼容。
        """
        if self._crew_factory is not None:
            try:
                return self._crew_factory(goal, options, images)
            except TypeError:
                return self._crew_factory(goal, options)
        if self._settings is None:
            return None
        from agentcrew.crew.crew import Crew

        # R28：flow / flow_name 不是 Settings 字段，先摘出来再 model_copy
        # （否则 Settings 的 extra="forbid" 会把它们当非法配置炸掉）。
        preset_tasks = None
        flow_options = {key: options[key]
                        for key in ("flow", "flow_name") if key in options}
        if flow_options:
            preset_tasks = self._preset_tasks_from_options(flow_options)
        effective = {key: value for key, value in options.items()
                     if key not in ("flow", "flow_name")}
        # options 已在 _create_run 过白名单与类型校验；这里不再用 except Exception
        # 静默吞掉装配失败（那会让请求回 202 而用户以为配置生效了）。
        settings = (
            self._settings.model_copy(update=dict(effective))
            if effective else self._settings)
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
                        prompter=DeferredPrompter(),
                        preset_tasks=preset_tasks, images=images)
        # 其余情况：API 服务器无人值守，人工卡点会在 await prompter 上永久挂起。
        settings = settings.model_copy(update={"gate": "none"})
        return Crew(settings, on_event=self.on_event, preset_tasks=preset_tasks,
                    images=images)

    def _preset_tasks_from_options(
        self, flow_options: Mapping[str, Any]
    ) -> list[Any]:
        """R28：``options.flow`` / ``flow_name`` → 任务草案列表。

        图不合法直接抛 ``ValueError``（``_create_run`` 会映射成 400）——**不带着
        坏流程起跑**；这也是画布「起跑」按钮的错误来源。
        """
        from agentcrew.flow.spec import (
            FlowError,
            FlowSpec,
            load_flow,
            validate_flow,
        )

        if "flow" in flow_options:
            try:
                spec = FlowSpec(**dict(flow_options["flow"]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"options.flow 结构不合法：{exc}") from exc
        else:
            if self._output_dir is None:
                raise ValueError("output_dir 未配置，无法按名字载入流程")
            try:
                spec = load_flow(self._output_dir,
                                 str(flow_options["flow_name"]))
            except FlowError as exc:
                raise ValueError(str(exc)) from exc
        errors = validate_flow(spec)
        if errors:
            raise ValueError("流程不合法：" + "；".join(errors))
        return list(spec.tasks)

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

    async def _get_agents(self, run_id: str, writer: asyncio.StreamWriter) -> None:
        """``GET /runs/{id}/agents``：读本次 run **解析后**的执行体编排计划。

        数据取自 ledger 的 ``run_plan_resolved`` 事件（启动期解析完即落盘），
        所以运行中与历史 run 都能读，而且回答的是「这次实际解析成了什么」
        （别名已定、工具已收窄），不是「配置里声称是什么」。配合
        ``/stats`` 的 ``llm.by_profile``，可以完整回答两个问题：
        哪个 agent 用了哪个模型，以及那个 agent 花了多少钱。

        早于该事件的历史 run 返回空列表并标注 ``source=none``——不拿配置层面
        的猜测冒充实际运行事实。
        """
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
            total = await asyncio.to_thread(
                reader.count_events, run_id, event="run_plan_resolved")
            if total == 0:
                await self._send_json(writer, 200, {
                    "run_id": run_id, "agents": [], "source": "none"})
                return
            # 取最后一条：同一个 run 可能 resume 多次，后一次的计划才是当前的。
            page = await asyncio.to_thread(
                reader.query_events, run_id,
                event="run_plan_resolved", limit=1, offset=total - 1)
        except ValueError as exc:
            await self._send_json(writer, 400, {"error": str(exc)})
            return
        except OSError as exc:
            # P0-5：OSError 文本带绝对路径，不进响应；详情落日志。
            _logger.error("ledger read failed for %s: %s",
                          run_id, self._internal_detail(exc))
            await self._send_json(
                writer, 500, {"error": "ledger 读取失败（OSError）"})
            return
        data = (page[0].get("data") or {}) if page else {}
        await self._send_json(writer, 200, {
            "run_id": run_id,
            "agents": data.get("agents") or [],
            "source": "run_plan_resolved",
        })

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
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
            _logger.error("resume %s failed: %s",
                          run_id, self._internal_detail(exc))
            await self._send_json(
                writer, 500,
                {"error": f"无法恢复 run {run_id!r}"
                          f"（{type(exc).__name__}）"})
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
            _logger.error("resume launch failed: %s", self._internal_detail(exc))
            await self._send_json(
                writer, 500,
                {"error": f"resume 启动失败（{type(exc).__name__}）"})
            return
        self.active_crews[resumed_id] = crew
        self._run_tasks[resumed_id] = task
        await self._send_json(
            writer, 202, {"run_id": resumed_id, "status": "running"})

    # ── R26：可视化编排（流程 YAML 双向 / 图校验 / 落盘）─────────────────

    async def _list_flows(self, writer: asyncio.StreamWriter) -> None:
        """``GET /flows``：已保存流程清单（名字 / 任务数 / 错误数）。"""
        if self._output_dir is None:
            await self._send_json(writer, 503, {"error": "output_dir 未配置"})
            return
        from agentcrew.flow.spec import list_flows

        await self._send_json(writer, 200, {"flows": list_flows(self._output_dir)})

    async def _get_flow(self, name: str, writer: asyncio.StreamWriter) -> None:
        """``GET /flows/{name}``：返回原始 YAML + 图视图（节点/边/拓扑序/错误）。"""
        if self._output_dir is None:
            await self._send_json(writer, 503, {"error": "output_dir 未配置"})
            return
        from agentcrew.flow.spec import (
            FlowError,
            dump_flow_yaml,
            graph_view,
            load_flow,
        )

        try:
            spec = load_flow(self._output_dir, name)
        except FlowError as exc:
            await self._send_json(writer, 404, {"error": str(exc)})
            return
        await self._send_json(writer, 200, {
            "yaml": dump_flow_yaml(spec), "view": graph_view(spec)})

    @staticmethod
    def _flow_yaml_from_body(body: bytes) -> tuple[str | None, str | None]:
        """从 ``{"yaml": "..."}`` 或 ``{"flow": {...}}`` 取 YAML 文本。"""
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, "invalid JSON"
        if not isinstance(data, dict):
            return None, "body 必须是 JSON 对象"
        raw = data.get("yaml")
        if isinstance(raw, str) and raw.strip():
            return raw, None
        flow = data.get("flow")
        if isinstance(flow, dict):
            from agentcrew.flow.spec import FlowError, FlowSpec, dump_flow_yaml

            try:
                return dump_flow_yaml(FlowSpec(**flow)), None
            except (FlowError, ValueError, TypeError) as exc:
                return None, f"flow 结构不合法：{exc}"
        return None, "缺少 yaml 字段（字符串）或 flow 字段（对象）"

    async def _validate_flow(
        self, body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        """``POST /flows/validate``：只校验不落盘（画布每次编辑后调）。"""
        from agentcrew.flow.spec import FlowError, graph_view, parse_flow_yaml

        text, error = self._flow_yaml_from_body(body)
        if text is None:
            await self._send_json(writer, 400, {"error": error})
            return
        try:
            spec = parse_flow_yaml(text)
        except FlowError as exc:
            await self._send_json(writer, 200, {
                "ok": False, "errors": [str(exc)], "view": None, "yaml": ""})
            return
        from agentcrew.flow.spec import dump_flow_yaml

        view = graph_view(spec)
        await self._send_json(writer, 200, {
            "ok": not view["errors"], "errors": view["errors"], "view": view,
            # 回显规范化 YAML：画布「导出 / 导入」都走这条通道，
            # 前端不需要再引一个 YAML 库（也就不会与后端序列化不一致）。
            "yaml": dump_flow_yaml(spec)})

    async def _save_flow(
        self, body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        """``POST /flows``：校验通过后落盘 ``<output>/_flows/<name>.yaml``。"""
        if self._output_dir is None:
            await self._send_json(writer, 503, {"error": "output_dir 未配置"})
            return
        from agentcrew.flow.spec import (
            FlowError,
            graph_view,
            parse_flow_yaml,
            save_flow,
        )

        text, error = self._flow_yaml_from_body(body)
        if text is None:
            await self._send_json(writer, 400, {"error": error})
            return
        try:
            spec = parse_flow_yaml(text)
            path = save_flow(self._output_dir, spec)
        except FlowError as exc:
            await self._send_json(writer, 400, {"error": str(exc)})
            return
        await self._send_json(writer, 201, {
            "saved": path.name, "view": graph_view(spec)})

    async def _delete_flow(
        self, name: str, writer: asyncio.StreamWriter
    ) -> None:
        """``DELETE /flows/{name}``：删除已保存流程。"""
        if self._output_dir is None:
            await self._send_json(writer, 503, {"error": "output_dir 未配置"})
            return
        from agentcrew.flow.spec import FlowError, delete_flow

        try:
            delete_flow(self._output_dir, name)
        except FlowError as exc:
            await self._send_json(writer, 404, {"error": str(exc)})
            return
        await self._send_json(writer, 200, {"deleted": name})

    # ── R25：密钥管理（RBAC）──────────────────────────────────────────────

    def _key_store(self) -> Any:
        """当前认证器背后的密钥库；未启用多密钥时 ``None``。"""
        return getattr(self._auth, "store", None)

    async def _list_keys(self, writer: asyncio.StreamWriter) -> None:
        """``GET /keys``：密钥清单（**不含哈希与盐**）+ 角色/作用域说明。"""
        store = self._key_store()
        if store is None:
            await self._send_json(writer, 503, {
                "error": "未启用多密钥：配置密钥库（python main.py keys add ...）后可用"})
            return
        from agentcrew.api.rbac import ROLE_SCOPES, describe_scopes

        keys = []
        for record in store.load():
            data = record.model_dump(mode="json")
            data.pop("key_hash", None)   # 哈希与盐永不出服务端
            data.pop("salt", None)
            keys.append(data)
        await self._send_json(writer, 200, {
            "keys": keys,
            "roles": {role: sorted(scopes) for role, scopes in ROLE_SCOPES.items()},
            "scopes": describe_scopes(),
        })

    async def _create_key(
        self, body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        """``POST /keys``：新建密钥，返回明文（**仅此一次**）。"""
        store = self._key_store()
        if store is None:
            await self._send_json(writer, 503, {
                "error": "未启用多密钥：配置密钥库后可用"})
            return
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self._send_json(writer, 400, {"error": "invalid JSON"})
            return
        if not isinstance(data, dict):
            await self._send_json(writer, 400, {"error": "body 必须是 JSON 对象"})
            return
        try:
            record, plaintext = store.add(
                name=str(data.get("name") or ""),
                role=str(data.get("role") or "operator"),
                scopes=[str(s) for s in (data.get("scopes") or [])],
                run_ids=[str(r) for r in (data.get("run_ids") or [])],
                expires_at=(str(data["expires_at"])
                            if data.get("expires_at") else None),
                note=str(data.get("note") or ""),
            )
        except ValueError as exc:
            await self._send_json(writer, 400, {"error": str(exc)})
            return
        payload = record.model_dump(mode="json")
        payload.pop("key_hash", None)
        payload.pop("salt", None)
        self._record_security("api_key_created", {
            "principal": current_principal().name, "key_id": record.id,
            "name": record.name, "role": record.role,
        })
        await self._send_json(writer, 201, {
            "key": plaintext,
            "record": payload,
            "notice": "密钥明文只显示这一次，请立即保存；服务端只存哈希",
        })

    async def _revoke_key(
        self, ident: str, writer: asyncio.StreamWriter
    ) -> None:
        """``DELETE /keys/{id|name}``：吊销密钥（幂等，已吊销重复调用仍 200）。"""
        store = self._key_store()
        if store is None:
            await self._send_json(writer, 503, {
                "error": "未启用多密钥：配置密钥库后可用"})
            return
        try:
            record = store.revoke(ident)
        except ValueError as exc:
            await self._send_json(writer, 404, {"error": str(exc)})
            return
        self._record_security("api_key_revoked", {
            "principal": current_principal().name,
            "key_id": record.id, "name": record.name,
        })
        await self._send_json(writer, 200, {
            "revoked": record.model_dump(mode="json", exclude={
                "key_hash", "salt"}),
        })

    async def _list_tools(self, writer: asyncio.StreamWriter) -> None:
        """``GET /tools``：R24 工具清单（管理页 / CLI 共用同一份元数据）。

        元数据读取不建目录、不执行工具；``tools_enabled`` 一并返回，管理页据此
        决定试跑表单是否可用（关闭时后端对试跑回 409）。
        """
        if self._settings is None:
            await self._send_json(writer, 503, {"error": "server 未配置 settings"})
            return
        from agentcrew.tools.lab import TRIAL_DIR_NAME, describe_tools

        await self._send_json(writer, 200, {
            "tools_enabled": bool(self._settings.tools_enabled),
            # P1：run_python 是否可用由 code_exec_enabled 单独控制（默认关），
            # 管理页据此决定是否渲染它——清单与实际注册表必须同一口径。
            "code_exec_enabled": bool(self._settings.code_exec_enabled),
            # C2：代码到底在哪跑（local / docker），管理页要能显示——
            # 「有沙箱」和「没沙箱」是两个完全不同的安全承诺。
            "sandbox_backend": self._settings.sandbox_backend,
            # C1：空串 = web_search 未启用（清单里也不会有它）。
            "search_provider": self._settings.search_provider,
            "trial_dir": TRIAL_DIR_NAME,
            "tools": describe_tools(
                code_exec=bool(self._settings.code_exec_enabled),
                search_provider=self._settings.search_provider),
        })

    async def _tool_test(
        self, name: str, body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        """``POST /tools/{name}/test``：单工具试跑（沙箱隔离，R24）。

        受两个开关保护：``tools_enabled`` 未开回 409（调试页不得扩大执行面）；
        入参非法回 400、未知工具回 404。工具运行时失败仍是 200 + ``ok=false``
        ——「工具报错」是试跑的**结果**，不是请求错误。
        """
        if self._settings is None:
            await self._send_json(writer, 503, {"error": "server 未配置 settings"})
            return
        if not self._settings.tools_enabled:
            await self._send_json(writer, 409, {
                "error": "工具执行未开启：设置 tools_enabled=true（或 "
                         "AGENTCREW_TOOLS_ENABLED=true）后可用",
            })
            return
        if self._output_dir is None:
            await self._send_json(writer, 503, {"error": "output_dir 未配置"})
            return
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self._send_json(writer, 400, {"error": "invalid JSON"})
            return
        if not isinstance(data, dict):
            await self._send_json(writer, 400, {"error": "body 必须是 JSON 对象"})
            return
        args = data.get("args", {})
        if not isinstance(args, dict):
            await self._send_json(writer, 400, {"error": "args 必须是 JSON 对象"})
            return
        from agentcrew.tools.lab import ToolLabError, run_trial

        try:
            from agentcrew.tools.lab import build_backend_from_config
            backend = build_backend_from_config(
                kind=self._settings.sandbox_backend,
                image=self._settings.sandbox_image,
                memory_mb=self._settings.sandbox_memory_mb,
                cpus=self._settings.sandbox_cpus,
                allow_network=self._settings.sandbox_allow_network,
            )
        except Exception as exc:  # noqa: BLE001 —— 装配失败是配置问题，回 409
            await self._send_json(writer, 409, {
                "error": f"执行后端不可用：{exc}",
            })
            return
        try:
            result = await run_trial(
                name, args, output_dir=self._output_dir,
                code_exec=bool(self._settings.code_exec_enabled),
                search_provider=self._settings.search_provider,
                backend=backend)
        except ToolLabError as exc:
            status = 404 if str(exc).startswith("未知工具") else 400
            await self._send_json(writer, status, {"error": str(exc)})
            return
        await self._send_json(writer, 200, result)

    async def _list_runs(self, writer: asyncio.StreamWriter) -> None:
        """``GET /runs``：盘上 run.json 列表 + 内存中运行中状态叠加。

        R25：按主体可见性过滤——非 admin 只看到自己创建的（或 ``run_ids``
        白名单里的）run；无主 run（CLI 建的）只有 admin 看得到。过滤发生在
        叠内存状态**之前**，避免通过「运行中」条目泄露别人的 run_id。
        """
        from agentcrew.runtime.checkpoint import CheckpointStore

        principal = current_principal()
        runs: list[dict[str, Any]] = []
        if self._output_dir is not None and self._output_dir.is_dir():
            for rid in CheckpointStore.list_runs(self._output_dir):
                if not principal.can_see_run(self._run_owner_name(rid)):
                    continue
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
            if not principal.can_see_run(self._run_owner_name(rid)):
                continue
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
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
            # B-8：limit 设上界（1000），offset 无上界但 query_events 流式跳过
            # 不会物化整份文件。
            limit = _clamp_int(
                query.get("limit", ["100"])[0], default=100, minimum=1,
                maximum=1000)
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
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
        self, path: str, query: dict[str, list[str]], body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        segments = [s for s in path.split("/") if s]
        if len(segments) == 3 and segments[0] == "runs" and segments[2] == "model":
            await self._put_model(segments[1], body, writer)
            return
        await self._send_json(writer, 404, {"error": "Not Found"})

    async def _get_model(self, run_id: str, writer: asyncio.StreamWriter) -> None:
        """``GET /runs/{id}/model``：当前模型绑定（仅运行中的 run 可读）。"""
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
        if await self._deny_if_run_hidden(run_id, writer):
            return
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

    async def _handle_delete(
        self, path: str, query: dict[str, list[str]], writer: asyncio.StreamWriter,
    ) -> None:
        """``DELETE /runs/{run_id}?confirm=1``：删除单个 run（P0-4）。

        必须带 ``?confirm=1`` 否则返回 400（防误删）。``running`` 状态由
        ``delete_run`` 内部拒绝（返回 409）。删除成功返回释放字节数。
        """
        segments = [s for s in path.split("/") if s]
        if len(segments) == 2 and segments[0] == "keys":
            await self._revoke_key(segments[1], writer)
            return
        if len(segments) == 2 and segments[0] == "flows":
            await self._delete_flow(segments[1], writer)
            return
        if len(segments) != 2 or segments[0] != "runs":
            await self._send_json(writer, 404, {"error": "Not Found"})
            return
        run_id = segments[1]
        if await self._deny_if_run_hidden(run_id, writer):
            return
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
                    # P0-5：诊断端点保留详情（运维要看连接/认证失败原因），
                    # 但密钥脱敏 + 截断，防止 provider 异常文本带出敏感值。
                    "error": self._internal_detail(exc),
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
                "error": self._internal_detail(exc),
            })
            return
        await self._send_json(writer, 200, {
            "ok": record.status != "unavailable",
            "latency_ms": record.latency_ms,
            "error": record.error,
        })

    async def _handle_metrics(self, writer: asyncio.StreamWriter) -> None:
        """D-7：Prometheus 文本格式的运行时指标（只读、无副作用、不扫盘）。

        刻意只暴露**进程内可即时算出**的量：run 数与状态分布来自内存表，
        请求数来自 ``_send_json`` 的单点埋点。不统计磁盘用量/产物数——那需要
        扫 ``output_dir``，抓取间隔一密集就会把服务拖慢，而这类指标更适合由
        ``main.py status`` / 外部 exporter 离线产生。
        """
        try:
            from importlib.metadata import version as _pkg_version
            build = _pkg_version("agentcrew")
        except Exception:  # noqa: BLE001 —— 未安装为包时不该让 /metrics 500
            build = "unknown"

        by_status: dict[str, int] = {}
        for run in list(self.runs.values()):
            by_status[run.status] = by_status.get(run.status, 0) + 1

        lines = [
            "# HELP agentcrew_up 服务存活（1=up）",
            "# TYPE agentcrew_up gauge",
            "agentcrew_up 1",
            "# HELP agentcrew_build_info 构建版本信息",
            "# TYPE agentcrew_build_info gauge",
            f'agentcrew_build_info{{version="{build}"}} 1',
            "# HELP agentcrew_process_start_time_seconds 进程启动时间（Unix 秒）",
            "# TYPE agentcrew_process_start_time_seconds gauge",
            f"agentcrew_process_start_time_seconds {self._started_at:.3f}",
            "# HELP agentcrew_runs_in_memory 内存中跟踪的 run 数",
            "# TYPE agentcrew_runs_in_memory gauge",
            f"agentcrew_runs_in_memory {len(self.runs)}",
            "# HELP agentcrew_runs_by_status 按状态统计的 run 数",
            "# TYPE agentcrew_runs_by_status gauge",
        ]
        if by_status:
            for status in sorted(by_status):
                lines.append(
                    f'agentcrew_runs_by_status{{status="{status}"}} {by_status[status]}')
        else:
            lines.append('agentcrew_runs_by_status{status="none"} 0')
        lines += [
            "# HELP agentcrew_http_requests_total 按状态码累计的 HTTP 响应数",
            "# TYPE agentcrew_http_requests_total counter",
        ]
        if self._status_counts:
            for code in sorted(self._status_counts):
                lines.append(
                    f'agentcrew_http_requests_total{{status="{code}"}}'
                    f" {self._status_counts[code]}")
        else:
            lines.append('agentcrew_http_requests_total{status="none"} 0')
        await http_transport.send_text(
            writer, "\n".join(lines) + "\n",
            content_type="text/plain; version=0.0.4; charset=utf-8")

    async def _handle_sse(self, run_id: str, writer: asyncio.StreamWriter) -> None:
        if await self._deny_if_run_hidden(run_id, writer):
            return
        await http_transport.sse_headers(writer)
        # P0：只订阅这个 run 的事件桶，别的 run 的事件不进这条连接（见 EventBus）。
        queue = self.bus.subscribe(run_id)
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

    async def _send_json(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: dict[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        """写一条 JSON 响应（实现见 :func:`agentcrew.api.http.send_json`）。

        D-7 起是实例方法（原先为 ``@staticmethod``）：仅为了累计状态码计数。
        所有调用点都走 ``self._send_json(...)``，签名对调用方无变化。
        """
        # D-7：按状态码累计（也是 /metrics 的唯一埋点，覆盖所有 JSON 响应）。
        self._status_counts[status] = self._status_counts.get(status, 0) + 1
        await http_transport.send_json(
            writer, status, body, extra_headers=extra_headers)
