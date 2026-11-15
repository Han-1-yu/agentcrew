"""C14：``serve --queue-dir`` 的队列模式——入队、状态回落、取消标记。

API 不再本地跑 run，只落一条 pending；状态查询在 ``run.json`` 出现前按队列
状态回答（``queued``），避免「刚拿 202 就 404」。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest

from agentcrew.api.server import ApiServer
from agentcrew.runtime.queue import RunQueue


def _request_sync(port: int, method: str, path: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


async def _request(port, method, path, payload=None):
    return await asyncio.to_thread(_request_sync, port, method, path, payload)


@pytest.fixture
async def queued_server(tmp_path):
    queue = RunQueue(tmp_path / "_queue")
    server = ApiServer(port=0, output_dir=tmp_path, queue=queue)
    await server.start()
    try:
        yield server._server.sockets[0].getsockname()[1], queue, tmp_path
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_create_run_enqueues_instead_of_executing(queued_server):
    port, queue, _ = queued_server
    status, data = await _request(port, "POST", "/runs", {
        "goal": "队列模式目标", "options": {"mock": True}})
    assert status == 202
    assert data["queued"] is True and data["status"] == "queued"
    run_id = data["run_id"]
    pending = json.loads(
        (queue.root / "pending" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert pending["goal"] == "队列模式目标"
    assert pending["options"] == {"mock": True}


@pytest.mark.asyncio
async def test_status_falls_back_to_queue_before_run_json(queued_server):
    port, _, _ = queued_server
    _, created = await _request(port, "POST", "/runs", {"goal": "还没开始跑"})
    run_id = created["run_id"]
    status, data = await _request(port, "GET", f"/runs/{run_id}/status")
    assert status == 200
    assert data["status"] == "queued"
    assert data["from_queue"] is True
    assert data["task_count"] == 0


@pytest.mark.asyncio
async def test_cancel_writes_marker_for_queued_run(queued_server):
    port, queue, _ = queued_server
    _, created = await _request(port, "POST", "/runs", {"goal": "要被取消"})
    run_id = created["run_id"]
    status, data = await _request(port, "POST", f"/runs/{run_id}/cancel", {})
    assert status == 202
    assert data["queued"] is True
    assert queue.cancel_requested(run_id) is True


@pytest.mark.asyncio
async def test_completed_queue_item_reports_terminal_status(queued_server):
    port, queue, _ = queued_server
    _, created = await _request(port, "POST", "/runs", {"goal": "直接标完成"})
    run_id = created["run_id"]
    item = queue.claim("w-test")
    assert item is not None and item.run_id == run_id
    queue.complete(run_id, status="completed", worker_id="w-test")

    status, data = await _request(port, "GET", f"/runs/{run_id}/status")
    assert status == 200
    assert data["status"] == "completed"
    assert data["from_queue"] is True
