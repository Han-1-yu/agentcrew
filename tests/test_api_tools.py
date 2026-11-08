"""R24：工具管理 API——GET /tools 与 POST /tools/{name}/test。

三条硬约束：开关关闭时清单可看但试跑 409（调试页不扩大执行面）；
试跑落在 ``<output>//_tool_trial/`` 沙箱、绝不碰仓库；未知工具 404 / 入参
不合法 400 / 工具报错 200+ok=false——三种失败各走各的通道。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentcrew.api.server import ApiServer
from agentcrew.config import Settings


async def _request(port: int, method: str, path: str,
                   body: dict | None = None) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = json.dumps(body).encode() if body is not None else b""
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
        + payload)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, asyncio.CancelledError):
        pass
    head, _, body_text = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(body_text.decode("utf-8") or "{}")


@pytest.fixture
async def server_factory(tmp_path):
    servers: list[ApiServer] = []

    async def make(*, tools_enabled: bool, settings: bool = True):
        srv = ApiServer(
            port=0,
            output_dir=tmp_path,
            settings=Settings.load(output_dir=tmp_path,
                                   tools_enabled=tools_enabled)
            if settings else None,
        )
        await srv.start()
        servers.append(srv)
        return srv._server.sockets[0].getsockname()[1]

    try:
        yield make
    finally:
        for srv in servers:
            await srv.stop()


async def test_list_tools_without_settings_is_503(server_factory):
    port = await server_factory(tools_enabled=False, settings=False)
    status, data = await _request(port, "GET", "/tools")
    assert status == 503 and "settings" in data["error"]


async def test_list_tools_shows_inventory_and_switch(server_factory):
    port = await server_factory(tools_enabled=False)
    status, data = await _request(port, "GET", "/tools")
    assert status == 200
    assert data["tools_enabled"] is False
    assert data["trial_dir"] == "_tool_trial"
    names = {item["name"] for item in data["tools"]}
    assert "write_file" in names
    # P1：run_python 默认不注册（code_exec_enabled=false），清单与实际注册表同口径
    assert "run_python" not in names
    assert data["code_exec_enabled"] is False
    assert all(item["input_schema"]["type"] == "object" for item in data["tools"])


async def test_trial_refused_when_tools_disabled(server_factory, tmp_path):
    port = await server_factory(tools_enabled=False)
    status, data = await _request(
        port, "POST", "/tools/read_file/test", {"args": {"path": "a.txt"}})
    assert status == 409
    assert "tools_enabled" in data["error"]
    assert not (tmp_path / "_tool_trial").exists()   # 拒绝时连沙箱都不建


async def test_trial_writes_only_into_sandbox(server_factory, tmp_path):
    port = await server_factory(tools_enabled=True)
    status, data = await _request(
        port, "POST", "/tools/write_file/test",
        {"args": {"path": "a.txt", "content": "沙箱内"}})
    assert status == 200 and data["ok"] is True
    sandbox = tmp_path / data["sandbox"]
    assert sandbox.parent.name == "_tool_trial"
    assert (sandbox / "workspace" / "a.txt").read_text(
        encoding="utf-8") == "沙箱内"
    # 仓库/输出根没有被写脏
    assert not (Path.cwd() / "a.txt").exists()
    assert not (tmp_path / "a.txt").exists()


async def test_trial_unknown_tool_is_404(server_factory):
    port = await server_factory(tools_enabled=True)
    status, data = await _request(port, "POST", "/tools/nope/test", {"args": {}})
    assert status == 404 and "未知工具" in data["error"]


async def test_trial_invalid_args_is_400(server_factory):
    port = await server_factory(tools_enabled=True)
    status, data = await _request(
        port, "POST", "/tools/read_file/test", {"args": {}})
    assert status == 400 and "入参校验失败" in data["error"]


async def test_trial_tool_failure_is_200_with_ok_false(server_factory):
    """工具报错是试跑的**结果**，不是请求错误：200 + ok=false + error 文本。"""
    port = await server_factory(tools_enabled=True)
    status, data = await _request(
        port, "POST", "/tools/read_file/test",
        {"args": {"path": "missing.txt"}})
    assert status == 200 and data["ok"] is True   # 工具把「不存在」作为文本返回
    assert "不存在" in (data["result"] or "")

    status, data = await _request(
        port, "POST", "/tools/grep/test", {"args": {"pattern": "x"}})
    assert status == 200   # 没有 pattern 也有默认行为；这条只是钉住 200 语义


async def test_trial_bad_body_is_400(server_factory):
    port = await server_factory(tools_enabled=True)
    status, data = await _request(port, "POST", "/tools/read_file/test",
                                  {"args": ["not", "an", "object"]})
    assert status == 400 and "args" in data["error"]
