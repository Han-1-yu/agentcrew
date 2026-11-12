"""P2-9：Ledger 查询与分析 API（``/runs/{id}/ledger`` 与 ``/runs/{id}/stats``）。

测试模式与 ``test_api_server.py`` 一致：真实端口 + 裸 HTTP 请求；run 目录用
``CheckpointStore.create`` 在 ``tmp_path`` 下建好，再用写入侧的 ``Ledger`` 直接
往 ``ledger.jsonl`` 里 emit 事件（不断言 Crew 行为，只断言读路径）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentcrew.api.server import ApiServer
from agentcrew.runtime.checkpoint import CheckpointStore
from agentcrew.runtime.ledger import Ledger


async def _request(port: int, method: str, path: str) -> tuple[int, dict]:
    """发一个裸 HTTP 请求，返回 ``(状态码, 解析后的 JSON body)``。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
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


def _seed_run(tmp_path: Path, run_id: str = "R-1") -> CheckpointStore:
    """在 tmp_path 下建好一个 run 目录（含 run.json）。"""
    return CheckpointStore.create(
        tmp_path, run_id=run_id, project_goal="测试目标", config_snapshot={})


def _write_ledger(run_dir: Path, run_id: str, events: list[tuple[str, dict]]) -> None:
    """直接走写入侧 Ledger 往该 run 的 ledger.jsonl 里落事件。"""
    ledger = Ledger(run_dir / "ledger.jsonl", run_id=run_id)
    for event, data in events:
        ledger.emit(event, **data)


@pytest.fixture
async def server(tmp_path: Path):
    srv = ApiServer(port=0, output_dir=tmp_path)
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


class TestLedgerEndpoint:
    async def test_returns_events_list(self, server: ApiServer, tmp_path: Path):
        store = _seed_run(tmp_path)
        _write_ledger(store.run_dir, "R-1", [
            ("run_started", {"goal": "g"}),
            ("task_started", {"task_id": "T-1"}),
            ("llm_call", {"prompt_tokens": 10, "completion_tokens": 5}),
        ])
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs/R-1/ledger")
        assert status == 200
        assert data["total"] == 3
        assert len(data["events"]) == 3
        first = data["events"][0]
        assert first["event"] == "run_started"
        assert first["run_id"] == "R-1"
        assert first["data"] == {"goal": "g"}
        assert "ts" in first

    async def test_event_filter(self, server: ApiServer, tmp_path: Path):
        store = _seed_run(tmp_path)
        _write_ledger(store.run_dir, "R-1", [
            ("llm_call", {"prompt_tokens": 1}),
            ("task_started", {"task_id": "T-1"}),
            ("llm_call", {"prompt_tokens": 2}),
            ("task_finished", {"task_id": "T-1", "duration_ms": 9}),
        ])
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs/R-1/ledger?event=llm_call")
        assert status == 200
        assert data["total"] == 2
        assert [e["event"] for e in data["events"]] == ["llm_call", "llm_call"]

    async def test_limit_and_offset_pagination(
        self, server: ApiServer, tmp_path: Path
    ):
        store = _seed_run(tmp_path)
        _write_ledger(store.run_dir, "R-1", [
            (f"evt-{i}", {"i": i}) for i in range(5)
        ])
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs/R-1/ledger?limit=2")
        assert status == 200
        assert data["total"] == 5
        assert [e["data"]["i"] for e in data["events"]] == [0, 1]
        status, data = await _request(
            port, "GET", "/runs/R-1/ledger?limit=2&offset=2")
        assert [e["data"]["i"] for e in data["events"]] == [2, 3]
        status, data = await _request(
            port, "GET", "/runs/R-1/ledger?limit=2&offset=4")
        assert [e["data"]["i"] for e in data["events"]] == [4]

    async def test_unknown_run_returns_404(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, _ = await _request(port, "GET", "/runs/nope/ledger")
        assert status == 404

    async def test_empty_ledger_returns_empty_list(
        self, server: ApiServer, tmp_path: Path
    ):
        _seed_run(tmp_path)  # 建了 run 目录但从不 emit
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs/R-1/ledger")
        assert status == 200
        assert data["events"] == []
        assert data["total"] == 0

    async def test_suspicious_run_id_does_not_escape_output_dir(
        self, server: ApiServer
    ):
        port = server._server.sockets[0].getsockname()[1]
        # 路径越界：404（不存在）而不是读到 output_dir 之外的文件
        status, _ = await _request(port, "GET", "/runs/..%2Fetc/ledger")
        assert status in (400, 404)


class TestStatsEndpoint:
    async def test_stats_aggregates_llm_tasks_tools(
        self, server: ApiServer, tmp_path: Path
    ):
        store = _seed_run(tmp_path)
        _write_ledger(store.run_dir, "R-1", [
            ("llm_call", {"prompt_tokens": 100, "completion_tokens": 50,
                          "estimated_cost_usd": 0.01}),
            ("llm_call", {"prompt_tokens": 200, "completion_tokens": 60,
                          "estimated_cost_usd": 0.02}),
            ("task_started", {"task_id": "T-1"}),
            ("task_started", {"task_id": "T-2"}),
            ("task_started", {"task_id": "T-3"}),
            ("task_finished", {"task_id": "T-1", "duration_ms": 100}),
            ("task_finished", {"task_id": "T-2", "duration_ms": 300}),
            ("tool_call", {"tool_name": "read_file", "success": True}),
            ("tool_call", {"tool_name": "run_python", "success": False}),
            ("gate_result", {"verdict": "approve"}),
        ])
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs/R-1/stats")
        assert status == 200
        assert data["event_counts"] == {
            "llm_call": 2, "task_started": 3, "task_finished": 2,
            "tool_call": 2, "gate_result": 1,
        }
        assert data["llm"] == {
            "calls": 2, "prompt_tokens": 300, "completion_tokens": 110,
            "total_tokens": 410, "cost_usd": pytest.approx(0.03),
            # 这两个事件没有 profile 字段（历史/schema 外写入），归入 unknown
            # 而不是丢弃——否则 by_profile 之和会小于总量。
            "by_profile": {
                "unknown": {
                    "calls": 2, "prompt_tokens": 300, "completion_tokens": 110,
                    "total_tokens": 410, "cost_usd": pytest.approx(0.03),
                },
            },
        }
        assert data["tasks"]["started"] == 3
        assert data["tasks"]["finished"] == 2
        assert data["tasks"]["success_rate"] == pytest.approx(0.6667)
        assert data["tasks"]["avg_duration_ms"] == pytest.approx(200.0)
        assert data["tools"] == {"calls": 2, "success": 1, "failed": 1}

    async def test_stats_attributes_llm_usage_to_agent_profile(
        self, server: ApiServer, tmp_path: Path
    ):
        """多执行体角色：每个 agent profile 的调用数与成本必须各算各的。"""
        store = _seed_run(tmp_path)
        _write_ledger(store.run_dir, "R-1", [
            ("llm_call", {"profile": "backend_solid", "prompt_tokens": 100,
                          "completion_tokens": 50, "estimated_cost_usd": 0.01}),
            ("llm_call", {"profile": "backend_fast", "prompt_tokens": 10,
                          "completion_tokens": 5, "estimated_cost_usd": 0.0001}),
            ("llm_call", {"profile": "backend_fast", "prompt_tokens": 10,
                          "completion_tokens": 5, "estimated_cost_usd": 0.0001}),
        ])
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs/R-1/stats")
        assert status == 200
        by_profile = data["llm"]["by_profile"]
        assert set(by_profile) == {"backend_solid", "backend_fast"}
        assert by_profile["backend_solid"]["calls"] == 1
        assert by_profile["backend_solid"]["cost_usd"] == pytest.approx(0.01)
        assert by_profile["backend_fast"]["calls"] == 2
        assert by_profile["backend_fast"]["total_tokens"] == 30
        assert by_profile["backend_fast"]["cost_usd"] == pytest.approx(0.0002)
        # 分项之和必须等于总量，账要平。
        assert sum(b["calls"] for b in by_profile.values()) == data["llm"]["calls"]
        assert (sum(b["cost_usd"] for b in by_profile.values())
                == pytest.approx(data["llm"]["cost_usd"]))

    async def test_stats_empty_ledger_zeroed(
        self, server: ApiServer, tmp_path: Path
    ):
        _seed_run(tmp_path)
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs/R-1/stats")
        assert status == 200
        assert data["llm"]["calls"] == 0
        assert data["tasks"]["success_rate"] is None
        assert data["tasks"]["avg_duration_ms"] is None
        assert data["tools"] == {"calls": 0, "success": 0, "failed": 0}
        assert data["event_counts"] == {}

    async def test_stats_unknown_run_returns_404(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, _ = await _request(port, "GET", "/runs/nope/stats")
        assert status == 404


class TestAgentsEndpoint:
    """``GET /runs/{id}/agents``：读**解析后**的执行体计划，而非配置声称。"""

    async def test_returns_last_resolved_plan(
        self, server: ApiServer, tmp_path: Path
    ):
        store = _seed_run(tmp_path)
        _write_ledger(store.run_dir, "R-1", [
            ("run_plan_resolved", {"agents": [
                {"role": "backend_dev", "profile": "old", "alias": "a:1",
                 "duty": "primary", "tools": ["*"], "path_scope": [], "weight": 1.0},
            ]}),
            ("run_plan_resolved", {"agents": [
                {"role": "backend_dev", "profile": "backend_solid",
                 "alias": "anthropic:claude-sonnet-5", "duty": "primary",
                 "tools": ["*"], "path_scope": ["backend/"], "weight": 1.0},
                {"role": "backend_dev", "profile": "backend_fast",
                 "alias": "deepseek:deepseek-chat", "duty": "review",
                 "tools": ["read_file"], "path_scope": [], "weight": 1.0},
            ]}),
        ])
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs/R-1/agents")
        assert status == 200
        assert data["source"] == "run_plan_resolved"
        profiles = {a["profile"]: a for a in data["agents"]}
        assert set(profiles) == {"backend_solid", "backend_fast"}
        assert profiles["backend_solid"]["alias"] == "anthropic:claude-sonnet-5"
        assert profiles["backend_fast"]["duty"] == "review"

    async def test_legacy_run_without_plan_is_explicitly_empty(
        self, server: ApiServer, tmp_path: Path
    ):
        store = _seed_run(tmp_path)
        _write_ledger(store.run_dir, "R-1", [("run_started", {"goal": "g"})])
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _request(port, "GET", "/runs/R-1/agents")
        assert status == 200
        assert data["agents"] == []
        assert data["source"] == "none", "没有记录就说没有，不猜配置"

    async def test_unknown_run_returns_404(self, server: ApiServer):
        port = server._server.sockets[0].getsockname()[1]
        status, _ = await _request(port, "GET", "/runs/nope/agents")
        assert status == 404
