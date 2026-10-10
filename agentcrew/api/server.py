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
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from agentcrew.api.auth import APIKeyAuth, RateLimiter
from agentcrew.models.config import PROVIDER_PRESETS as PROVIDER_PRESETS_NAMES


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
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
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


def _clamp_int(raw: str, *, default: int, minimum: int) -> int:
    """把查询参数里的整数读出来；非法 / 缺失用 ``default``，并压到 ``minimum`` 以上。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


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
            request_line = await reader.readline()
            if not request_line:
                return
            method, path, _ = request_line.decode("utf-8", errors="replace").split(" ", 2)
            # 读请求头，存成「小写键 → 值」的字典（E-3 认证要查 Authorization /
            # X-API-Key，限流要查 X-Forwarded-For）；顺带记住 Content-Length（无
            # body 的 POST 不能再 read() 阻塞等客户端 —— 连接还开着等响应，会和
            # 服务端互相死等）。
            headers: dict[str, str] = {}
            content_length = 0
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, sep, raw_value = line.partition(b":")
                if not sep:
                    continue
                key = name.strip().decode("ascii", errors="replace").lower()
                value = raw_value.strip().decode("latin-1", errors="replace")
                headers[key] = value
                if key == "content-length":
                    try:
                        content_length = max(0, int(value))
                    except ValueError:
                        content_length = 0
            # E-3 中间件链（路由分发前）：先限流后认证。任一拦截直接回响应并
            # return，不再读 body / 不再分发——被拒请求不该再触发任何业务逻辑。
            if await self._apply_middleware(path, headers, writer):
                return
            if method == "GET":
                await self._handle_get(path, writer)
            elif method in ("POST", "PUT"):
                # 按声明长度读，但不苛求凑满：客户端少发（含调试用的坏 JSON）
                # 都在 JSON 解析处收成 400，而不是在这里死等字节。
                body = (
                    await reader.read(content_length)
                    if content_length > 0 else b"")
                if method == "POST":
                    await self._handle_post(path, body, writer)
                else:
                    await self._handle_put(path, body, writer)
            elif method == "DELETE":
                await self._handle_delete(path, writer)
            else:
                await self._send_json(writer, 405, {"error": "Method Not Allowed"})
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

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
        """客户端 IP：优先 ``X-Forwarded-For`` 首地址（反代后真实客户端），否则
        取 socket peer 地址；都取不到时记 ``unknown``。"""
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        peername = writer.get_extra_info("peername")
        if peername:
            return str(peername[0])
        return "unknown"

    def _record_security(self, event: str, data: dict[str, Any]) -> None:
        """追加一条服务端安全审计事件（内存；与 Ledger 事件同形：event + data）。"""
        self.security_events.append({"event": event, "data": dict(data)})

    async def _handle_get(self, path: str, writer: asyncio.StreamWriter) -> None:
        # 先按 urlsplit 拆开路径与查询串：带 ``?event=...`` 的请求不能再用
        # ``path.endswith("/status")`` 这类裸匹配（P2-9 的 ledger/stats 端点
        # 自带查询参数）。无查询串时 ``parsed.path`` 与历史行为逐字节一致。
        parsed = urlsplit(path)
        p = parsed.path
        query = parse_qs(parsed.query)
        if p == "/" or p == "/index.html":
            from agentcrew.api.dashboard import DASHBOARD_HTML
            payload = DASHBOARD_HTML.encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"\r\n" + payload
            )
            await writer.drain()
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
                if run is None:
                    await self._send_json(writer, 404, {"error": "run not found"})
                    return
                await self._send_json(writer, 200, {
                    "run_id": run.run_id,
                    "goal": run.goal,
                    "status": run.status,
                    "task_count": len(run.tasks),
                    "tasks": run.tasks,
                })
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
        await self._send_json(writer, 404, {"error": "Not Found"})

    async def _handle_post(
        self, path: str, body: bytes, writer: asyncio.StreamWriter,
    ) -> None:
        if path == "/runs":
            await self._create_run(body, writer)
            return
        parsed = urlsplit(path)
        segments = [unquote(s) for s in parsed.path.split("/") if s]
        # A4：POST /providers/{name}/test
        if len(segments) == 3 and segments[0] == "providers" and segments[2] == "test":
            await self._provider_test(segments[1], writer)
            return
        if len(segments) == 3 and segments[0] == "runs":
            run_id, action = segments[1], segments[2]
            if action == "cancel":
                await self._cancel_run(run_id, writer)
                return
            if action == "resume":
                await self._resume_run(run_id, writer)
                return
            if action == "events":
                try:
                    data = json.loads(body.decode("utf-8")) if body else {}
                except json.JSONDecodeError:
                    await self._send_json(writer, 400, {"error": "invalid JSON"})
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
        except json.JSONDecodeError:
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
        crew = self._build_crew(goal, options)
        if crew is None:
            await self._send_json(
                writer, 503,
                {"error": "server 未配置 crew_factory/settings，无法创建 run"},
            )
            return
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            self._run_crew(crew, lambda: crew.run(goal), started))
        try:
            run_id = await asyncio.wait_for(started, timeout=30)
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

        try:
            settings = (
                self._settings.model_copy(update=dict(options))
                if options else self._settings)
        except Exception:
            settings = self._settings
        # API 服务器无人值守：人工卡点会在 await prompter 上永久挂起。
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
            if started.done() and started.exception() is None:
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
        if run_id in self.active_crews:
            await self._send_json(
                writer, 409, {"error": "run 已在运行中"})
            return
        if self._settings is None:
            await self._send_json(
                writer, 503, {"error": "server 未配置 settings，无法 resume"})
            return
        from agentcrew.crew.crew import Crew

        try:
            crew = Crew.resume_from(self._settings, run_id, on_event=self.on_event)
        except (OSError, ValueError) as exc:
            await self._send_json(
                writer, 404, {"error": f"无法恢复 run {run_id!r}：{exc}"})
            return
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            self._run_crew(crew, crew.resume, started))
        try:
            resumed_id = await asyncio.wait_for(started, timeout=30)
        except Exception as exc:
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
            events = reader.query_events(
                run_id, event=event, limit=limit, offset=offset)
            total = reader.count_events(run_id, event=event)
        except ValueError as exc:
            await self._send_json(writer, 400, {"error": str(exc)})
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
            stats = reader.event_stats(run_id)
        except ValueError as exc:
            await self._send_json(writer, 400, {"error": str(exc)})
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
        except json.JSONDecodeError:
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
        except (OSError, ValueError) as exc:
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
        self, name: str, writer: asyncio.StreamWriter,
    ) -> None:
        """POST /providers/{name}/test —— 按需测速，发 max_tokens=1 的 ping。"""
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
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        await writer.drain()
        queue = self.bus.subscribe()
        try:
            # 发送初始连接事件
            writer.write(f"event: connected\ndata: {json.dumps({'run_id': run_id})}\n\n".encode())
            await writer.drain()
            async for payload in self._iter_queue(queue):
                writer.write(payload.encode())
                await writer.drain()
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
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        reason = {
            200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
            404: "Not Found", 405: "Method Not Allowed", 429: "Too Many Requests",
        }.get(status, "OK")
        header_lines = (
            f"HTTP/1.1 {status} {reason}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode()
        )
        for name, value in (extra_headers or {}).items():
            header_lines += f"{name}: {value}\r\n".encode()
        writer.write(header_lines + b"\r\n" + payload)
        await writer.drain()
