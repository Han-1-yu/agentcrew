"""AgentCrew HTTP API 的零依赖同步客户端（R16）。

设计取舍：

* **零新增依赖**：只用标准库 ``urllib``。SDK 是给外部系统用的，不该把
  ``openai`` / ``pydantic`` / ``rich`` 这一串服务端依赖拖进调用方环境。
* **不 import ``agentcrew``**：SDK 只认 HTTP 契约
  （``artifacts/contracts/openapi.yaml``），服务端与客户端可以各自升级；
  也避免把项目内部包的分层约束传染给调用方。
* **不做隐式重试**：服务端已有 Provider 级重试与故障转移，客户端再叠一层会把
  「一次 POST /runs」变成可能创建多个 run 的动作。429 只把 ``Retry-After``
  交给调用方，由调用方决定何时重试。
* **SSE 是惰性生成器**：``iter_events`` 不缓存整条事件流，长连接断开时由
  ``finally`` 关闭响应。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import (
    APIError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    RateLimitError,
    ResponseError,
    TransportError,
    WaitTimeoutError,
)

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
#: SSE 读取超时的下限（秒）。服务端每 30s 发一个 ``: ping`` 注释行做 keep-alive，
#: 客户端的 socket 超时若与心跳间隔同为 30s，空闲期就会在心跳到达前刚好断开
#: （B-20）。取 75s = 心跳的 2.5 倍，留出调度抖动与网络延迟余量。
SSE_MIN_TIMEOUT_SECONDS = 75.0
#: run 的终态；``awaiting_approval`` 是非终态挂起，不在此列。
TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"})


@dataclass(frozen=True)
class Event:
    """一条 SSE 事件；``data`` 恒为 dict（解析失败时包成 ``{"raw": ...}``）。"""

    name: str
    data: dict[str, Any]


def _message_from(raw: bytes | str, status: int) -> str:
    """从错误响应里取人类可读消息；不是 JSON 就回退原文。"""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return text.strip()[:500] or f"HTTP {status}"
    if isinstance(obj, dict):
        for key in ("error", "message", "detail"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    return text.strip()[:500] or f"HTTP {status}"


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _api_error(status: int, raw: bytes, headers: Mapping[str, str]) -> APIError:
    """按状态码挑异常类型；未知 4xx/5xx 落到通用 ``APIError``。"""
    message = _message_from(raw, status)
    kwargs: dict[str, Any] = {
        "status_code": status,
        "retry_after": _retry_after(headers),
    }
    if status == 401:
        return AuthenticationError(message, **kwargs)
    if status == 404:
        return NotFoundError(message, **kwargs)
    if status == 409:
        return ConflictError(message, **kwargs)
    if status == 429:
        return RateLimitError(message, **kwargs)
    return APIError(message, **kwargs)


def _event_data(text: str) -> dict[str, Any]:
    """SSE 的 data 行：合法 JSON 对象直接用，其余包起来不丢信息。"""
    try:
        obj = json.loads(text)
    except ValueError:
        return {"raw": text}
    if isinstance(obj, dict):
        return obj
    return {"value": obj}


class AgentCrewClient:
    """同步客户端；每个实例只存配置，不持有连接。

    ``api_key`` 给定时所有请求（含 ``/health``）都会带
    ``Authorization: Bearer <key>``——服务端的认证中间件覆盖全部路由。
    ``headers`` 用于追加自定义头（如反代要求的租户标识）。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._extra_headers = dict(headers or {})
        self._closed = False

    def __enter__(self) -> AgentCrewClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request_timeout(
        self, timeout: float | None, *, stream: bool = False
    ) -> float | None:
        """把调用级 ``timeout`` 归一成 socket 超时（B-20）。

        - ``None`` → 用 client 默认值；流式接口再叠加 :data:`SSE_MIN_TIMEOUT_SECONDS`
          下限（服务端每 30s 发心跳，socket 超时若同为 30s，空闲期会在心跳到达前
          刚好断开）；
        - ``<= 0`` → ``None``，即**不设超时**。此前 ``timeout or self.timeout``
          会把显式 ``0`` 静默替换成默认值，调用方没有办法表达「不要超时」。
        """
        if timeout is not None and timeout <= 0:
            return None
        base = self.timeout if timeout is None else timeout
        if stream:
            return max(SSE_MIN_TIMEOUT_SECONDS, base * 2)
        return base

    def close(self) -> None:
        """标记不再使用。urllib 没有连接池，这里没有需要释放的句柄。"""
        self._closed = True

    # ---- 底层 ----

    @staticmethod
    def _run_path(run_id: str, suffix: str = "") -> str:
        # run_id 由服务端校验；客户端仍做转义，避免把 '/' 或 '?' 拼进路径。
        return f"/runs/{urllib.parse.quote(str(run_id), safe='')}{suffix}"

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            pairs = [(key, value) for key, value in params.items()
                     if value is not None]
            if pairs:
                url += "?" + urllib.parse.urlencode(pairs)
        return url

    def _headers(self, *, has_body: bool, accept: str) -> dict[str, str]:
        result = {
            "Accept": accept,
            "User-Agent": "agentcrew-sdk/1",
            **self._extra_headers,
        }
        if self.api_key:
            result["Authorization"] = f"Bearer {self.api_key}"
        if has_body:
            result["Content-Type"] = "application/json"
        return result

    def _open(
        self, method: str, path: str, *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        accept: str = "application/json",
    ) -> Any:
        """发一次请求，返回 ``(status, raw_bytes, headers)``。"""
        if self._closed:
            raise TransportError("client 已关闭")
        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None else None
        )
        request = urllib.request.Request(
            self._url(path, params),
            data=data,
            headers=self._headers(has_body=data is not None, accept=accept),
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._request_timeout(timeout)
            ) as response:
                return (getattr(response, "status", 200), response.read(),
                        dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            headers = dict(exc.headers.items()) if exc.headers else {}
            return exc.code, raw, headers
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"{method} {path} 失败：{exc}") from exc

    def _request(
        self, method: str, path: str, *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        status, raw, headers = self._open(method, path, body=body, params=params)
        if not 200 <= status < 300:
            raise _api_error(status, raw, headers)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ResponseError(
                f"{method} {path} 返回了非 JSON 响应", status_code=status
            ) from exc

    def request(
        self, method: str, path: str, *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """调用契约里存在、但 SDK 尚未封装语义方法的端点（逃生口）。

        例如 ``client.request("GET", "/providers")``。返回值是解码后的 JSON，
        错误仍按 :func:`_api_error` 的映射抛出。
        """
        return self._request(method, path, body=body, params=params)

    # ---- 系统 / run 列表 ----

    def health(self) -> dict[str, Any]:
        """``GET /health`` → ``{"status": "ok"}``。启用认证时也要带 key。"""
        return self._request("GET", "/health")

    def list_runs(self) -> list[dict[str, Any]]:
        """``GET /runs`` → run 摘要列表（无分页，一次返回全部）。"""
        return self._request("GET", "/runs").get("runs", [])

    def create_run(
        self, goal: str, *, options: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """``POST /runs`` → ``{"run_id": ...}``。

        ``options`` 是服务端白名单里的按 run 覆盖项（``mock`` /
        ``default_model`` / ``max_concurrency`` …）；密钥类配置不接受。
        """
        payload: dict[str, Any] = {"goal": goal}
        if options:
            payload["options"] = dict(options)
        return self._request("POST", "/runs", body=payload)

    # ---- 只读查询 ----

    def get_status(self, run_id: str) -> dict[str, Any]:
        """``GET /runs/{id}/status``：内存实时快照。服务重启后可能 404。"""
        return self._request("GET", self._run_path(run_id, "/status"))

    def get_ledger(
        self, run_id: str, *, event: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        """``GET /runs/{id}/ledger`` → ``{"events": [...], "total": N}``。"""
        return self._request(
            "GET", self._run_path(run_id, "/ledger"),
            params={"event": event, "limit": limit, "offset": offset})

    def iter_ledger(
        self, run_id: str, *, event: str | None = None, page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """按 offset/limit 自动翻完整个 ledger，逐条产出事件。"""
        offset = 0
        while True:
            page = self.get_ledger(
                run_id, event=event, limit=page_size, offset=offset)
            events = page.get("events") or []
            yield from events
            total = page.get("total")
            offset += len(events)
            if not events or (isinstance(total, int) and offset >= total):
                return

    def get_stats(self, run_id: str) -> dict[str, Any]:
        """``GET /runs/{id}/stats``：事件分布 / LLM 用量 / 成功率。"""
        return self._request("GET", self._run_path(run_id, "/stats"))

    def get_approvals(self, run_id: str) -> list[dict[str, Any]]:
        """``GET /runs/{id}/approvals``：持久化审批单（服务重启后仍可读）。"""
        result = self._request("GET", self._run_path(run_id, "/approvals"))
        return result.get("approvals", [])

    def get_model(self, run_id: str) -> dict[str, Any]:
        """``GET /runs/{id}/model``：仅运行中的 run 可读。"""
        return self._request("GET", self._run_path(run_id, "/model"))

    # ---- 生命周期动作 ----

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """``POST /runs/{id}/cancel``：软取消，可重复调用。"""
        return self._request("POST", self._run_path(run_id, "/cancel"))

    def resume_run(self, run_id: str) -> dict[str, Any]:
        """``POST /runs/{id}/resume``：从 checkpoint 后台续跑。"""
        return self._request("POST", self._run_path(run_id, "/resume"))

    def delete_run(self, run_id: str, *, confirm: bool = True) -> dict[str, Any]:
        """``DELETE /runs/{id}?confirm=1``：物理删除，不可恢复。"""
        return self._request(
            "DELETE", self._run_path(run_id),
            params={"confirm": "1"} if confirm else None)

    def decide_approval(
        self,
        run_id: str,
        request_id: str,
        action: str,
        *,
        task_ids: list[str] | None = None,
        decided_by: str = "sdk",
    ) -> dict[str, Any]:
        """``POST /runs/{id}/approvals``：写审批裁决（可跨进程）。

        ``action`` 由卡点决定：plan 用 approve / cancel / save_exit，
        delivery 用 accept / abort / rework（rework 需给 ``task_ids``）。
        """
        decision: dict[str, Any] = {"action": action}
        if task_ids is not None:
            decision["task_ids"] = list(task_ids)
        return self._request(
            "POST", self._run_path(run_id, "/approvals"),
            body={"request_id": request_id, "decision": decision,
                  "decided_by": decided_by})

    def set_model(
        self, run_id: str, alias: str, *, role: str | None = None
    ) -> dict[str, Any]:
        """``PUT /runs/{id}/model``：切默认模型，或指定角色的绑定。"""
        body: dict[str, Any] = {"alias": alias}
        if role is not None:
            body["role"] = role
        return self._request(
            "PUT", self._run_path(run_id, "/model"), body=body)

    def inject_event(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        *,
        event: str | None = None,
    ) -> dict[str, Any]:
        """``POST /runs/{id}/events``：注入一条测试事件给 SSE 订阅者。"""
        body = dict(payload)
        if event is not None:
            body["_event"] = event
        return self._request(
            "POST", self._run_path(run_id, "/events"), body=body)

    # ---- SSE ----

    def iter_events(
        self, run_id: str, *, timeout: float | None = None
    ) -> Iterator[Event]:
        """订阅 ``GET /runs/{id}/events``，逐条产出 :class:`Event`。

        首帧是服务端的 ``connected``；``: ping`` 心跳注释行被跳过。
        生成器被关闭时响应随之关闭，长连接不会泄漏。
        """
        if self._closed:
            raise TransportError("client 已关闭")
        request = urllib.request.Request(
            self._url(self._run_path(run_id, "/events")),
            headers=self._headers(has_body=False,
                                  accept="text/event-stream"),
            method="GET",
        )
        try:
            response = urllib.request.urlopen(
                request, timeout=self._request_timeout(timeout, stream=True))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            headers = dict(exc.headers.items()) if exc.headers else {}
            raise _api_error(exc.code, raw, headers) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"订阅事件流失败：{exc}") from exc

        name = ""
        data_lines: list[str] = []
        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith(":"):       # 心跳 / 注释
                    continue
                if line == "":
                    if name or data_lines:
                        yield Event(name or "message",
                                    _event_data("\n".join(data_lines)))
                    name, data_lines = "", []
                    continue
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if name or data_lines:
                yield Event(name or "message", _event_data("\n".join(data_lines)))

    # ---- 便利方法 ----

    def wait_for_run(
        self,
        run_id: str,
        *,
        poll_interval: float = 1.0,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """轮询 ``get_status`` 直到 run 进入终态，返回最后一次快照。

        ``awaiting_approval`` 是**非终态**：需要人工裁决时会一直等到超时，
        调用方应改用 ``get_approvals`` / ``decide_approval`` + ``resume_run``。
        """
        deadline = time.monotonic() + timeout
        while True:
            status = self.get_status(run_id)
            if str(status.get("status", "")).lower() in TERMINAL_STATUSES:
                return status
            if time.monotonic() >= deadline:
                raise WaitTimeoutError(
                    f"run {run_id} 在 {timeout}s 内未进入终态"
                    f"（当前 {status.get('status')!r}）",
                    payload=status)
            time.sleep(max(poll_interval, 0.0))
