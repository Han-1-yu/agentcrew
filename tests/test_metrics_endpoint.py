"""D-7：``/metrics`` Prometheus 端点。

三条断言：文本格式可被 Prometheus 解析（``# TYPE`` + 样本行）、必需指标存在、
计数随请求增长。端点与 ``/health`` 同一条认证链，因此这里不加认证中间件，
只验证内容本身。
"""

from __future__ import annotations

import asyncio
import re

import pytest

from agentcrew.api.server import ApiServer


async def _get(server: ApiServer, path: str) -> tuple[str, str]:
    """发一次 GET，返回 (状态行, body)。"""
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        raw = await reader.read(65536)
    finally:
        writer.close()
        await writer.wait_closed()
    text = raw.decode("utf-8", "replace")
    head, _, body = text.partition("\r\n\r\n")
    return head, body


@pytest.fixture
async def server():
    s = ApiServer(port=0)
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


async def test_metrics_returns_prometheus_text(server):
    head, body = await _get(server, "/metrics")
    assert "200" in head.split("\r\n")[0]
    assert "text/plain" in head
    assert "version=0.0.4" in head
    # 每个非注释行都必须是「指标名 [标签] 值」
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        assert re.match(r"^[a-z][a-zA-Z0-9_]*(\{[^}]*\})? -?[0-9.]", line), line


async def test_metrics_has_required_series(server):
    _, body = await _get(server, "/metrics")
    assert "# TYPE agentcrew_up gauge" in body
    assert "agentcrew_up 1" in body
    assert "agentcrew_process_start_time_seconds " in body
    assert "agentcrew_runs_in_memory " in body
    assert "# TYPE agentcrew_http_requests_total counter" in body


async def test_metrics_counts_requests(server):
    """每发一次 JSON 请求，对应状态码计数 +1。"""
    await _get(server, "/health")
    _, before = await _get(server, "/metrics")
    first = int(re.search(r'agentcrew_http_requests_total\{status="200"\} (\d+)',
                          before).group(1))
    await _get(server, "/health")
    _, after = await _get(server, "/metrics")
    second = int(re.search(r'agentcrew_http_requests_total\{status="200"\} (\d+)',
                           after).group(1))
    assert second > first


async def test_metrics_reflects_run_status(server):
    from agentcrew.api.server import RunState

    server.runs["R-1"] = RunState(run_id="R-1", status="running")
    _, body = await _get(server, "/metrics")
    assert 'agentcrew_runs_by_status{status="running"} 1' in body
    assert "agentcrew_runs_in_memory 1" in body


async def test_metrics_is_read_only(server):
    """连续抓取不应改变 run 状态或产生副作用。"""
    _, first = await _get(server, "/metrics")
    _, second = await _get(server, "/metrics")
    # 除了请求计数，其它样本应逐字相同
    strip = lambda text: [ln for ln in text.splitlines()  # noqa: E731
                          if not ln.startswith("agentcrew_http_requests_total")]
    assert strip(first) == strip(second)
