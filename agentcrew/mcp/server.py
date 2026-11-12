"""把 AgentCrew 暴露为 MCP server（C7）：任何 MCP 客户端都能把它当一个「同事」调用。

**复用现有 API 层**是刻意的设计决定：本模块自己**不碰** Crew / 审批 / 产物——它把
每个工具调用翻译成一次对本机 HTTP API 的请求（``POST /runs``、
``GET /runs/{id}/status``、审批读写），因此：

- RBAC 由 API 侧执行：MCP 客户端拿到的 API Key 是什么角色，就只能做什么。
  「越权调用被拒并留痕」不需要在这里再造一遍——``security_events`` 与
  ``approval_decided`` 的审计已经存在；
- 语义与 Studio / SDK 完全同源，不会出现「MCP 入口能做、API 不能做」的分叉。

**传输**：stdio（换行分隔 JSON-RPC，与 :class:`~agentcrew.mcp.transports.StdioTransport`
同一帧格式——我们自己的客户端可以连自己）。streamable HTTP 传输**本轮不做**：
stdio 已覆盖「Claude Desktop / Cursor 类本机客户端」的主场景，HTTP 服务端要单独
处理鉴权与会话，等有真实远程调用方再立项（与路线文档「最小可用」的口径一致）。

**协议面**刻意做窄：``initialize`` / ``tools/list`` / ``tools/call`` / ``ping``
四个方法 + 一条 ``notifications/initialized`` 通知。不做 resources / prompts /
sampling——编排引擎的 MCP 面就是「给活、看进度、裁决审批」。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

__all__ = [
    "ApiBridge",
    "PROTOCOL_VERSION",
    "SERVER_INFO",
    "TOOL_DEFINITIONS",
    "handle_message",
    "run_stdio",
]

#: 与 ``agentcrew.mcp.transports`` 客户端声明同一协议版本（对齐即可，不做协商）。
PROTOCOL_VERSION = "2024-11-05"

SERVER_INFO: dict[str, Any] = {
    "name": "agentcrew",
    "version": "1.0.0",
}

#: 终态集合：``wait_for_run`` 停止轮询的条件（与 RunStatus 语义一致）。
_TERMINAL_STATUSES = frozenset({
    "COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED",
    "completed", "failed", "cancelled", "interrupted",
})

_MAX_POLL_SECONDS = 3600.0
_POLL_INTERVAL = 2.0


class ApiBridge:
    """MCP 工具 → HTTP API 的翻译层（同步、零第三方依赖）。

    ``request_fn`` 可注入替换（测试用）；默认用 urllib 打真实请求。
    非 2xx 响应**不抛异常**：返回 ``{"error": ..., "status": ...}`` 让工具调用
    以 ``isError`` 结果回给模型——MCP 客户端要的是可读错误，不是崩溃。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 30.0,
        request_fn: Callable[[str, str, dict[str, Any] | None], tuple[int, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._request_fn = request_fn

    # ---- HTTP ----

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        if self._request_fn is not None:
            return self._request_fn(method, path, payload)
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=data, method=method,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = resp.read(2 * 1024 * 1024)
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read(2 * 1024 * 1024)
            except OSError:
                body = b"{}"
        except (urllib.error.URLError, OSError) as exc:
            return 0, {"error": f"无法连接 AgentCrew API（{self.base_url}）：{exc}"}
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {"raw": body[:500].decode("utf-8", errors="replace")}
        return status, parsed

    # ---- 工具实现（每个都对应一条现有 API 路由）----

    def create_run(self, goal: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        status, data = self.request("POST", "/runs",
                                    {"goal": goal, "options": options or {}})
        if status == 202:
            return {"run_id": data.get("run_id"), "status": data.get("status")}
        return _error(status, data)

    def run_status(self, run_id: str) -> dict[str, Any]:
        status, data = self.request("GET", f"/runs/{run_id}/status")
        if status != 200:
            return _error(status, data)
        return {
            "run_id": data.get("run_id", run_id),
            "status": data.get("status") or data.get("run_status"),
            "goal": data.get("goal"),
            "progress": {
                key: data.get(key) for key in ("stats", "updated_at") if key in data
            },
        }

    def list_approvals(self, run_id: str) -> dict[str, Any]:
        status, data = self.request("GET", f"/runs/{run_id}/approvals")
        if status != 200:
            return _error(status, data)
        return data

    def approve(
        self, run_id: str, request_id: str, decision: dict[str, Any],
    ) -> dict[str, Any]:
        status, data = self.request(
            "POST", f"/runs/{run_id}/approvals",
            {"request_id": request_id, "decision": decision,
             "decided_by": "mcp"})
        if status == 200:
            return {"approved": True, "approval": data}
        return _error(status, data)

    def wait_for_run(
        self, run_id: str, timeout_seconds: float = 600.0,
    ) -> dict[str, Any]:
        """轮询到终态或超时（MCP 客户端大多是同步对话流，轮询比 SSE 简单可靠）。"""
        deadline = time.monotonic() + min(max(timeout_seconds, 1.0),
                                          _MAX_POLL_SECONDS)
        last: dict[str, Any] = {}
        while True:
            last = self.run_status(run_id)
            state = str(last.get("status") or "")
            if state in _TERMINAL_STATUSES or (
                    state == "AWAITING_APPROVAL" or state == "awaiting_approval"):
                return last
            if time.monotonic() >= deadline:
                return {**last, "timed_out": True,
                        "note": f"等待 {timeout_seconds:g}s 未到终态，可稍后再查"}
            time.sleep(_POLL_INTERVAL)


def _error(status: int, data: Any) -> dict[str, Any]:
    message = data.get("error") if isinstance(data, dict) else None
    return {"error": message or f"API 返回 {status}", "status": status}


# ── 工具清单（MCP tools/list 的 payload）────────────────────────────────

def _tool(
    name: str, description: str, properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object", "properties": properties, "required": required,
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool(
        "agentcrew_create_run",
        "给 AgentCrew 团队派一个目标，创建并启动一次多智能体 run，返回 run_id。",
        {
            "goal": {"type": "string", "description": "要完成的目标（自然语言）"},
            "options": {
                "type": "object",
                "description": "可选的 run 配置覆盖（同 POST /runs 的 options）",
            },
        },
        ["goal"],
    ),
    _tool(
        "agentcrew_run_status",
        "查询一次 run 的当前状态（running / awaiting_approval / completed / failed…）。",
        {"run_id": {"type": "string", "description": "run ID"}},
        ["run_id"],
    ),
    _tool(
        "agentcrew_list_approvals",
        "列出一次 run 的全部人工审批单（含待裁决的）。",
        {"run_id": {"type": "string", "description": "run ID"}},
        ["run_id"],
    ),
    _tool(
        "agentcrew_approve",
        "裁决一张审批单（approve / reject / rework 等，decision 形状同 HTTP API）。",
        {
            "run_id": {"type": "string", "description": "run ID"},
            "request_id": {"type": "string", "description": "审批单 ID"},
            "decision": {
                "type": "object",
                "description": '裁决内容，如 {"action": "approve"}',
            },
        },
        ["run_id", "request_id", "decision"],
    ),
    _tool(
        "agentcrew_wait_for_run",
        "阻塞等待一次 run 到终态（completed/failed/cancelled/interrupted）或超时。",
        {
            "run_id": {"type": "string", "description": "run ID"},
            "timeout_seconds": {
                "type": "number", "description": "最长等待秒数，默认 600",
            },
        },
        ["run_id"],
    ),
]


# ── JSON-RPC 处理（纯函数，便于单测）────────────────────────────────────

def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text[:20000]}],
    }
    if is_error:
        result["isError"] = True
    return result


def handle_message(
    bridge: ApiBridge, msg: dict[str, Any],
) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 消息；通知（无 id）返回 ``None``，请求返回响应对象。

    任何处理期异常都收敛成 JSON-RPC error 响应：MCP server 崩了，客户端拿到的
    是「工具坏了」而不是「管道断了」。
    """
    method = str(msg.get("method") or "")
    msg_id = msg.get("id")
    if msg_id is None:
        return None  # notification：不回帧
    try:
        result, error = _dispatch(bridge, method, msg.get("params") or {})
        return {"jsonrpc": "2.0", "id": msg_id,
                **({"result": result} if error is None else {"error": error})}
    except Exception as exc:  # noqa: BLE001 —— 崩溃必须变成可见的协议错误
        return {"jsonrpc": "2.0", "id": msg_id, "error": {
            "code": -32603, "message": f"internal error: {type(exc).__name__}: {exc}",
        }}


def _dispatch(
    bridge: ApiBridge, method: str, params: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }, None
    if method == "ping":
        return {}, None
    if method == "tools/list":
        return {"tools": TOOL_DEFINITIONS}, None
    if method == "tools/call":
        return _call_tool(bridge, params)
    return None, {"code": -32601, "message": f"未知方法：{method}"}


def _call_tool(
    bridge: ApiBridge, params: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    name = str(params.get("name") or "")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    known = {tool["name"] for tool in TOOL_DEFINITIONS}
    if name not in known:
        return None, {
            "code": -32602,
            "message": f"未知工具：{name}（可用：{', '.join(sorted(known))}）",
        }
    try:
        if name == "agentcrew_create_run":
            goal = args.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                return _text_result("错误：goal 必须是非空字符串", is_error=True), None
            options = args.get("options") or {}
            data = bridge.create_run(goal.strip(), options if isinstance(options, dict) else {})
        elif name == "agentcrew_run_status":
            run_id = str(args.get("run_id") or "")
            if not run_id:
                return _text_result("错误：run_id 必填", is_error=True), None
            data = bridge.run_status(run_id)
        elif name == "agentcrew_list_approvals":
            run_id = str(args.get("run_id") or "")
            if not run_id:
                return _text_result("错误：run_id 必填", is_error=True), None
            data = bridge.list_approvals(run_id)
        elif name == "agentcrew_approve":
            run_id = str(args.get("run_id") or "")
            request_id = str(args.get("request_id") or "")
            decision = args.get("decision")
            if not run_id or not request_id or not isinstance(decision, dict):
                return _text_result(
                    "错误：run_id / request_id / decision(object) 必填",
                    is_error=True), None
            data = bridge.approve(run_id, request_id, decision)
        elif name == "agentcrew_wait_for_run":
            run_id = str(args.get("run_id") or "")
            if not run_id:
                return _text_result("错误：run_id 必填", is_error=True), None
            timeout = args.get("timeout_seconds", 600.0)
            timeout = float(timeout) if isinstance(timeout, (int, float)) else 600.0
            data = bridge.wait_for_run(run_id, timeout)
        else:  # pragma: no cover - known 集合已保证
            return _text_result(f"未知工具：{name}", is_error=True), None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        data = {"error": f"{type(exc).__name__}: {exc}"}
    text = json.dumps(data, ensure_ascii=False, indent=2)
    is_error = isinstance(data, dict) and bool(data.get("error"))
    return _text_result(text, is_error=is_error), None


# ── stdio 主循环 ────────────────────────────────────────────────────────

def run_stdio(
    bridge: ApiBridge,
    reader: Any,
    writer: Any,
) -> int:
    """换行分隔 JSON-RPC 的同步主循环。

    ``reader`` / ``writer`` 是文件类对象（生产传 ``sys.stdin`` / ``sys.stdout``，
    测试传 ``io.StringIO``），便于在 Windows 上运行（connect_read_pipe 不可用）。
    返回进程退出码：EOF 正常退出 = 0。
    """
    for line in reader:
        line = line.strip()
        if not line:
            continue
        # try/except 不支持 elif；分支统一收敛成一个可选响应（通知回 None）。
        response: dict[str, Any] | None
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600,
                                      "message": "请求必须是 JSON 对象"}}
            else:
                response = handle_message(bridge, msg)
        except json.JSONDecodeError:
            # 坏帧按 JSON-RPC 规范回 parse error（id=null）。
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700,
                                  "message": "解析错误：不是合法 JSON"}}
        if response is not None:
            writer.write(json.dumps(response, ensure_ascii=False) + "\n")
            writer.flush()
    return 0
