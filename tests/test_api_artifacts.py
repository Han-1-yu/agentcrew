"""C9：``GET /runs/{id}/artifacts``——产物清单与单文件读取。

这是「派活 → 交付」闭环的最后一米：MCP 外部客户端此前只能派活、看状态、审批，
拿不回产物。路由只读、只走 ``runs:read``，路径经 ``safe_relpath`` 三道闸门。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

from agentcrew.api.rbac import required_scope
from agentcrew.api.server import ApiServer
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.runtime.checkpoint import CheckpointStore
from agentcrew.task.task import Task, TaskStatus


def _get_sync(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


async def _get(port: int, path: str) -> tuple[int, dict]:
    # urllib 是同步的：直接在 async 测试里调会阻塞 event loop，同进程的 API
    # 服务器永远读不到请求，最后表现为 5s 超时（不是路由没生效）。
    return await asyncio.to_thread(_get_sync, port, path)


@pytest.fixture
def artifact_run(tmp_path):
    CheckpointStore.create(
        tmp_path, project_goal="产物读取", config_snapshot={}, run_id="R-art")
    store = ArtifactStore(tmp_path / "R-art" / "artifacts", run_id="R-art")
    store.commit(
        "docs/PRD.md",
        "# PRD\n需求内容\n",
        spec=ArtifactSpec(
            path="docs/PRD.md", kind="markdown", description="PRD"),
        task=Task(
            id="T-001", title="需求", description="d", role="product_manager",
            acceptance_criteria=["a"], deliverables=[],
            status=TaskStatus.SUCCEEDED),
    )
    store.write_manifest()
    return tmp_path


@pytest.fixture
async def port(artifact_run):
    server = ApiServer(port=0, output_dir=artifact_run)
    await server.start()
    try:
        yield server._server.sockets[0].getsockname()[1]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_lists_manifest(port):
    status, data = await _get(port, "/runs/R-art/artifacts")
    assert status == 200
    assert data["count"] == 1
    item = data["artifacts"][0]
    assert item["path"] == "docs/PRD.md"
    assert item["kind"] == "markdown"
    assert item["task_id"] == "T-001"
    assert item["size_bytes"] > 0


@pytest.mark.asyncio
async def test_reads_single_text_artifact(port):
    status, data = await _get(port, "/runs/R-art/artifacts?path=docs/PRD.md")
    assert status == 200
    assert "# PRD" in data["content"]
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_truncates_when_max_bytes_is_small(port):
    status, data = await _get(
        port, "/runs/R-art/artifacts?path=docs/PRD.md&max_bytes=3")
    assert status == 200
    assert data["truncated"] is True
    assert len(data["content"].encode("utf-8")) <= 3


@pytest.mark.asyncio
async def test_unsafe_path_is_rejected(port):
    status, _ = await _get(
        port, "/runs/R-art/artifacts?path=" + quote("../../etc/passwd"))
    assert status == 400


@pytest.mark.asyncio
async def test_missing_file_and_run(port):
    assert (await _get(port, "/runs/R-art/artifacts?path=nope.md"))[0] == 404
    assert (await _get(port, "/runs/R-none/artifacts"))[0] == 404


def test_route_requires_runs_read():
    assert required_scope("GET", "/runs/R-art/artifacts") == "runs:read"
