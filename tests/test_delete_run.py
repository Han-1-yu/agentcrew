"""P0-4：可删除会话（删除单个 run）。

覆盖：
- ``checkpoint.delete_run`` 核心逻辑（成功删除/释放字节数、running 拒绝、不存在、非法 run_id）
- API ``DELETE /runs/{id}?confirm=1`` 端点（成功/缺 confirm/running 409/不存在 404）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentcrew.api.server import ApiServer
from agentcrew.runtime.checkpoint import (
    CheckpointStore,
    RunStatus,
    delete_run,
)


# ── 辅助 ──────────────────────────────────────────────────────────────────


def _make_run(
    output_dir: Path,
    run_id: str,
    *,
    status: RunStatus = RunStatus.COMPLETED,
    artifact_content: str = "# test\n",
) -> Path:
    """创建一个最小 run 目录并设置状态，返回 run_dir。"""
    store = CheckpointStore.create(
        output_dir, project_goal=f"goal-{run_id}", config_snapshot={}, run_id=run_id,
    )
    record = store.load_record()
    record.status = status
    store.save_record(record)
    # 放一个产物文件，使释放字节数 > 0
    (store.artifacts_dir / "output.md").write_text(artifact_content, encoding="utf-8")
    return store.run_dir


# ── checkpoint.delete_run ─────────────────────────────────────────────────


class TestDeleteRun:
    def test_delete_completed_run_returns_freed_bytes(self, tmp_path):
        """已完成的 run 可删除，返回释放的字节数（含产物文件）。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        run_dir = _make_run(output_dir, "R-001", status=RunStatus.COMPLETED)
        expected_size = sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file())

        freed = delete_run(output_dir, "R-001")

        assert freed == expected_size
        assert freed > 0
        assert not run_dir.exists()

    def test_delete_failed_run(self, tmp_path):
        """FAILED 状态的 run 可删除。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        run_dir = _make_run(output_dir, "R-002", status=RunStatus.FAILED)
        delete_run(output_dir, "R-002")
        assert not run_dir.exists()

    def test_delete_cancelled_run(self, tmp_path):
        """CANCELLED 状态的 run 可删除。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        run_dir = _make_run(output_dir, "R-003", status=RunStatus.CANCELLED)
        delete_run(output_dir, "R-003")
        assert not run_dir.exists()

    def test_delete_interrupted_run(self, tmp_path):
        """INTERRUPTED 状态的 run 可删除（Ctrl-C 断点，不是活进程）。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        run_dir = _make_run(output_dir, "R-004", status=RunStatus.INTERRUPTED)
        delete_run(output_dir, "R-004")
        assert not run_dir.exists()

    def test_running_run_rejected(self, tmp_path):
        """RUNNING 状态拒绝删除，抛 RuntimeError（提示先 cancel）。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        run_dir = _make_run(output_dir, "R-005", status=RunStatus.RUNNING)
        with pytest.raises(RuntimeError, match="正在运行中"):
            delete_run(output_dir, "R-005")
        # 目录未被删除
        assert run_dir.exists()

    def test_nonexistent_run_raises_file_not_found(self, tmp_path):
        """不存在的 run_id 抛 FileNotFoundError。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            delete_run(output_dir, "does-not-exist")

    def test_invalid_run_id_rejected(self, tmp_path):
        """路径注入式 run_id（含 ../）被 _validated_run_id 拒绝。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        with pytest.raises(ValueError):
            delete_run(output_dir, "../escape")

    def test_delete_only_removes_target_run(self, tmp_path):
        """删除一个 run 不影响同目录下的其他 run。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_run(output_dir, "R-A", status=RunStatus.COMPLETED)
        run_b = _make_run(output_dir, "R-B", status=RunStatus.COMPLETED)
        delete_run(output_dir, "R-A")
        assert not (output_dir / "R-A").exists()
        assert run_b.exists()


# ── API DELETE /runs/{id} ─────────────────────────────────────────────────


class TestApiDeleteEndpoint:
    @pytest.mark.asyncio
    async def test_delete_success(self, tmp_path):
        """DELETE /runs/{id}?confirm=1 成功删除，返回 freed_bytes。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_run(output_dir, "R-API-1", status=RunStatus.COMPLETED,
                  artifact_content="x" * 100)
        server = ApiServer(port=0, output_dir=output_dir)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"DELETE /runs/R-API-1?confirm=1 HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            text = response.decode()
            assert "200 OK" in text
            assert '"deleted": "R-API-1"' in text
            assert "freed_bytes" in text
            assert not (output_dir / "R-API-1").exists()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_delete_without_confirm_returns_400(self, tmp_path):
        """缺少 ?confirm=1 返回 400，run 不被删除。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_run(output_dir, "R-API-2", status=RunStatus.COMPLETED)
        server = ApiServer(port=0, output_dir=output_dir)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"DELETE /runs/R-API-2 HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            text = response.decode()
            assert "400" in text
            assert "confirm" in text
            assert (output_dir / "R-API-2").exists()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_delete_running_returns_409(self, tmp_path):
        """RUNNING 状态的 run 通过 API 删除返回 409 Conflict。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _make_run(output_dir, "R-API-3", status=RunStatus.RUNNING)
        server = ApiServer(port=0, output_dir=output_dir)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"DELETE /runs/R-API-3?confirm=1 HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            text = response.decode()
            assert "409" in text
            assert (output_dir / "R-API-3").exists()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, tmp_path):
        """删除不存在的 run 返回 404。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        server = ApiServer(port=0, output_dir=output_dir)
        await server.start()
        try:
            port = server._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"DELETE /runs/no-such?confirm=1 HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            text = response.decode()
            assert "404" in text
        finally:
            await server.stop()
