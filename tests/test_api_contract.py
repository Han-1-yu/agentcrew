"""API 契约守卫：``API_ROUTES`` ↔ ``openapi.yaml`` 双向比对 + 真服务逐条探测。

R16 之前 ``openapi.yaml`` 一直缺 ``/providers`` 族与执行体切换端点——SDK 只封装
了契约里有的 17 个方法，缺口静默存在了四轮。本模块把漂移钉死：

1. **清单 ↔ 契约**：``server.API_ROUTES``（唯一事实源）与 ``openapi.yaml`` 的
   ``paths``（方法 × 路径）必须完全一致——契约漏写、多写都当场炸。
2. **哨兵钉死**：未匹配路由统一回 ``404 {"error": "Not Found"}``——探测断言
   依赖这个哨兵，先验证它稳定。
3. **逐条探测**：在 ``ApiServer(port=0)`` 上对每个 ``(方法, 模板)`` 发真实请求，
   断言响应**不是**哨兵——清单里的每条路由都真的接得起来（探测用不存在的
   run_id / provider 名与空 body，只触发 400/404/503 参数错误，不产生副作用；
   SSE 端点只读响应头即断开）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from agentcrew.api.server import API_ROUTES, ApiServer

_OPENAPI = (
    Path(__file__).resolve().parents[1] / "artifacts" / "contracts" / "openapi.yaml"
)

#: 哨兵：任何未匹配路由的固定响应（形状在 server 的 404 兜底里）。
_NOT_FOUND = "Not Found"

_PROBE_PATH_VARS = {
    "{run_id}": "R-contract-probe",
    "{task_id}": "T-contract-probe",
    "{name}": "__no_such_provider__",
}

# ── 清单 ↔ 契约（纯文件比对，不起服务） ────────────────────────────────────


def _openapi_operations() -> set[tuple[str, str]]:
    """openapi.yaml 里的 ``(方法, 路径)`` 集合（方法统一大写）。"""
    doc = yaml.safe_load(_OPENAPI.read_text(encoding="utf-8"))
    ops: set[tuple[str, str]] = set()
    for path, item in (doc.get("paths") or {}).items():
        for method in ("get", "post", "put", "delete", "patch"):
            if method in (item or {}):
                ops.add((method.upper(), path))
    return ops


def test_route_inventory_matches_openapi():
    """``API_ROUTES`` 与 openapi.yaml 的 paths 必须双向一致。"""
    documented = _openapi_operations()
    inventory = set(API_ROUTES)
    assert inventory == documented, (
        "API 路由清单与 openapi.yaml 漂移："
        f"清单有而契约缺 {sorted(inventory - documented)}；"
        f"契约有而清单缺 {sorted(documented - inventory)}"
    )


# ── 真服务探测 ─────────────────────────────────────────────────────────────


async def _request(
    method: str,
    path: str,
    *,
    port: int,
    body: bytes | None = None,
    headers_only: bool = False,
) -> tuple[int, str]:
    """发一条 ``Connection: close`` 请求，回 ``(状态码, 文本)``。

    ``headers_only=True`` 用于 SSE：读到响应头即断开，不等事件流。
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = body or b""
    writer.write(
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: close\r\n\r\n".encode() + payload
    )
    await writer.drain()
    raw = await asyncio.wait_for(
        reader.readuntil(b"\r\n\r\n"), timeout=5)
    status_line = raw.split(b"\r\n", 1)[0].decode()
    status = int(status_line.split()[1])
    if headers_only:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, asyncio.CancelledError):
            pass
        return status, status_line
    text = (await reader.read(65536)).decode("utf-8", errors="replace")
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, asyncio.CancelledError):
        pass
    return status, text


def _probe_path(template: str) -> str:
    """路径模板 → 可请求的具体路径（用必然不存在的资源名）。"""
    path = template
    for var, value in _PROBE_PATH_VARS.items():
        path = path.replace(var, value)
    return path


def _is_sentinel(status: int, text: str) -> bool:
    """未匹配路由的哨兵响应：``404`` 且 body 是裸 ``{"error": "Not Found"}``。"""
    return status == 404 and f'"{_NOT_FOUND}"' in text


@pytest.fixture
async def server():
    srv = ApiServer(port=0)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        yield srv, port
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_unmatched_route_returns_stable_sentinel(server):
    """哨兵先钉死：探测断言全部建立在「未匹配 = 这一个形状」上。"""
    status, text = await _request(
        "GET", "/__definitely_not_a_route__", port=server[1])
    assert status == 404 and _is_sentinel(status, text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,template",
    sorted(API_ROUTES),
    ids=[f"{m}_{t.replace('/', '_')}" for m, t in sorted(API_ROUTES)],
)
async def test_every_documented_route_responds(server, method, template):
    """清单里每条路由都必须真的接得起来：响应不是未匹配哨兵。

    探测载荷全部是「必然不存在的资源 + 空 body」：只触发参数/资源校验错误
    （400/404/503），不启动 run、不改状态、不发 LLM 调用。SSE 端点读响应头
    即断开，再经 bus 发一条事件让处理器「写失败」退出——否则它阻塞在 30s
    心跳队列上，Python 3.12 的 ``wait_closed()`` 等在途连接会把 teardown 拖到 60s。
    """
    srv, port = server
    path = _probe_path(template)
    sse = template.endswith("/events") and method == "GET"
    status, text = await _request(
        method, path, port=port,
        body=b"" if method in ("POST", "PUT", "DELETE") else None,
        headers_only=sse,
    )
    if sse:
        # 客户端已关。单个事件不够：处理器的第一次写会进 OS 缓冲「成功」，
        # 然后阻塞在下一个 30s 队列等待上；连发多条让它不阻塞地连续消费，
        # 很快撞上对端 RST（ConnectionError）走 finally 退出。
        for _ in range(5):
            srv.bus.publish("__probe_unblock__", {"run_id": "R-contract-probe"})
    assert not _is_sentinel(status, text), (
        f"{method} {path} 落到了未匹配哨兵：路由清单声称存在，"
        "但 server 的分发逻辑里没有对应分支（API_ROUTES 与 _handle_* 漂移）"
    )


@pytest.mark.asyncio
async def test_method_mismatch_is_not_a_route_match(server):
    """405 语义抽查：GET 不了 DELETE 专用的资源（与「兜底」注释呼应）。"""
    status, text = await _request(
        "GET", "/runs/R-contract-probe/cancel", port=server[1])
    # cancel 只有 POST；GET 走到兜底（哨兵）或 405 都算「没把 GET 当路由」
    assert _is_sentinel(status, text) or status == 405
