"""P2-10：Dashboard 操作化（取消 / 删除 / 模型切换 / Ledger / 统计）。

两层断言：
1. 静态检查 ``DASHBOARD_HTML`` 包含操作按钮、确认流程、标签页与 JS 调用；
2. 经真实服务器走一遍前端会调用的端点（cancel / delete / model / ledger），
   保证 UI 按钮背后的 API 行为正确。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentcrew.api.dashboard import DASHBOARD_HTML
from agentcrew.api.server import ApiServer
from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.runtime.ledger import Ledger


# ── 静态：HTML / JS 结构 ────────────────────────────────────────────────


class TestDashboardHtml:
    def test_serves_run_list_and_refresh(self):
        assert "loadRuns" in DASHBOARD_HTML
        assert "运行列表" in DASHBOARD_HTML
        assert "/runs" in DASHBOARD_HTML

    def test_cancel_button_only_for_running_runs(self):
        assert "cancelRun" in DASHBOARD_HTML
        assert "/cancel" in DASHBOARD_HTML
        # 取消按钮只在运行中渲染（条件分支在模板字符串里）
        assert "status === \"running\"" in DASHBOARD_HTML or \
            "status === 'running'" in DASHBOARD_HTML

    def test_delete_button_requires_double_confirm(self):
        assert "deleteRunById" in DASHBOARD_HTML
        assert "?confirm=1" in DASHBOARD_HTML
        assert "二次确认" in DASHBOARD_HTML

    def test_ledger_tab_fetches_events(self):
        assert "loadLedger" in DASHBOARD_HTML
        assert "/ledger" in DASHBOARD_HTML
        assert "事件日志" in DASHBOARD_HTML
        assert "ledger-event-filter" in DASHBOARD_HTML

    def test_stats_tab_fetches_analysis(self):
        assert "loadStats" in DASHBOARD_HTML
        assert "/stats" in DASHBOARD_HTML
        assert "统计" in DASHBOARD_HTML

    def test_model_switcher_present(self):
        assert "switchModel" in DASHBOARD_HTML
        assert "/model" in DASHBOARD_HTML


# ── 动态：前端按钮背后的端点 ───────────────────────────────────────────


class _ModelFakeCrew:
    """duck-type 运行中 crew：支持 cancel / switch_model / current_model_bindings。"""

    def __init__(self, run_id: str = "model-run-1") -> None:
        self.run_id = run_id
        self.cancelled = False
        self._bindings = {"default": "openai:gpt-4o-mini", "role_bindings": {}}

    async def run(self, goal: str) -> None:
        await asyncio.sleep(0)

    def cancel(self) -> None:
        self.cancelled = True

    def switch_model(self, alias: str, role: str | None = None) -> dict:
        self._bindings = {"default": alias, "role_bindings": {role: alias} if role else {}}
        return self._bindings

    def current_model_bindings(self) -> dict:
        return self._bindings


async def _request(
    port: int, method: str, path: str, body: dict | None = None
) -> tuple[int, dict]:
    payload = json.dumps(body).encode() if body is not None else b""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if body is not None:
        request += f"Content-Length: {len(payload)}\r\n"
    request += "\r\n"
    writer.write(request.encode() + payload)
    await writer.drain()
    raw = await reader.read(65536)
    writer.close()
    await writer.wait_closed()
    text = raw.decode("utf-8", errors="replace")
    head, _, body_text = text.partition("\r\n\r\n")
    status = int(head.split("\r\n")[0].split(" ")[1])
    try:
        return status, json.loads(body_text)
    except json.JSONDecodeError:
        return status, {}


@pytest.fixture
async def server(tmp_path: Path):
    srv = ApiServer(port=0, output_dir=tmp_path)
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


class TestOpsEndpoints:
    async def test_cancel_running_run(self, server: ApiServer):
        crew = _ModelFakeCrew()
        server.active_crews["R-ops-1"] = crew
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "POST", "/runs/R-ops-1/cancel")
        assert status == 202
        assert crew.cancelled is True

    async def test_delete_run_with_confirm(self, server: ApiServer, tmp_path: Path):
        store = CheckpointStore.create(
            tmp_path, run_id="R-del-1", project_goal="g", config_snapshot={})
        # create 落盘的状态是 running，delete_run 会拒绝（409）；改成 completed
        record = store.load_record()
        record.status = RunStatus.COMPLETED
        store.save_record(record)
        port = server._server.sockets[0].getsockname()[1]
        # 不带 confirm 拒绝
        status, _ = await _request(port, "DELETE", "/runs/R-del-1")
        assert status == 400
        # 带 confirm 删除
        status, data = await _request(port, "DELETE", "/runs/R-del-1?confirm=1")
        assert status == 200
        assert data["deleted"] == "R-del-1"
        # 删完后 ledger 端点 404
        status, _ = await _request(port, "GET", "/runs/R-del-1/ledger")
        assert status == 404

    async def test_switch_model_endpoint(self, server: ApiServer):
        server.active_crews["R-model-1"] = _ModelFakeCrew()
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(
            port, "PUT", "/runs/R-model-1/model",
            {"alias": "deepseek:deepseek-chat", "role": "qa_engineer"})
        assert status == 200
        assert data["default"] == "deepseek:deepseek-chat"

    async def test_ledger_and_stats_endpoints_feed_tabs(
        self, server: ApiServer, tmp_path: Path
    ):
        store = CheckpointStore.create(
            tmp_path, run_id="R-ui-1", project_goal="g", config_snapshot={})
        Ledger(store.run_dir / "ledger.jsonl", run_id="R-ui-1").emit(
            "llm_call", prompt_tokens=10, completion_tokens=5,
            estimated_cost_usd=0.001)
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs/R-ui-1/ledger")
        assert status == 200
        assert data["total"] == 1
        status, data = await _request(port, "GET", "/runs/R-ui-1/stats")
        assert status == 200
        assert data["llm"]["calls"] == 1
