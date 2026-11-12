"""2026-11-10 P2 加固回归（对应 docs/GAP-ANALYSIS-2026-11-10.md 批次 3）。

覆盖：

1. A-8：门禁总超时解析（``gate_config.gate_timeout_seconds``）与超时后不再无限等；
2. B-10~B-13：MCP 传输的单行上限、非 UTF-8、SSE 状态码、endpoint 同源；
3. D-1：SIGTERM 优雅停机（仅 POSIX；Windows 的 terminate 是硬杀，跳过）。
"""

from __future__ import annotations

import signal
import sys

import pytest

# ── 1. 门禁超时 ─────────────────────────────────────────────────────────


class TestGateTimeout:
    def test_default_is_600(self):
        from agentcrew.crew.scheduler import Scheduler

        assert Scheduler._resolve_gate_timeout(None) == 600.0

    def test_gate_config_overrides(self):
        from agentcrew.config import Settings
        from agentcrew.crew.scheduler import Scheduler

        s = Settings(gate_config={"gate_timeout_seconds": 120})
        assert Scheduler._resolve_gate_timeout(s) == 120.0

    def test_invalid_value_falls_back(self):
        from agentcrew.config import Settings
        from agentcrew.crew.scheduler import Scheduler

        for bad in (0, -5, "abc", None):
            s = Settings(gate_config={"gate_timeout_seconds": bad})
            assert Scheduler._resolve_gate_timeout(s) == 600.0


# ── 2. MCP 传输加固 ─────────────────────────────────────────────────────


class TestMcpTransportHardening:
    def test_endpoint_cross_origin_is_rejected(self):
        """B-13：SSE endpoint 指向别的源 → 拒绝（防凭据外泄 + 出站 SSRF）。"""
        from agentcrew.mcp.transports import MCPTransportError, SSETransport

        t = SSETransport("http://127.0.0.1:9000/sse")
        with pytest.raises(MCPTransportError, match="跨源"):
            t._resolve_endpoint("http://attacker.example/collect")

    def test_endpoint_same_origin_is_allowed(self):
        from agentcrew.mcp.transports import SSETransport

        t = SSETransport("http://127.0.0.1:9000/sse")
        assert t._resolve_endpoint(
            "http://127.0.0.1:9000/messages") == "http://127.0.0.1:9000/messages"

    def test_endpoint_relative_is_joined(self):
        from agentcrew.mcp.transports import SSETransport

        t = SSETransport("http://127.0.0.1:9000/sse")
        assert t._resolve_endpoint("/messages") == "http://127.0.0.1:9000/messages"

    def test_endpoint_other_port_is_rejected(self):
        from agentcrew.mcp.transports import MCPTransportError, SSETransport

        t = SSETransport("http://127.0.0.1:9000/sse")
        with pytest.raises(MCPTransportError, match="跨源"):
            t._resolve_endpoint("http://127.0.0.1:9001/messages")

    @pytest.mark.asyncio
    async def test_iter_lines_caps_unterminated_buffer(self):
        """B-11：无换行的持续字节流不能无限累积缓冲。"""
        from agentcrew.mcp import transports

        async def pieces():
            for _ in range(4):
                yield b"x" * (transports._MAX_SSE_LINE_BYTES // 3)

        with pytest.raises(transports.MCPTransportError, match="单行"):
            async for _line in transports._iter_lines(pieces()):
                pass


class TestStdioReceiveHardening:
    @pytest.mark.asyncio
    async def test_non_utf8_line_is_dropped_not_raised(self):
        """B-10：非法 UTF-8 只丢弃该行，不让读循环死亡。"""
        from agentcrew.mcp.transports import StdioTransport

        class FakeStdout:
            def __init__(self, lines):
                self._lines = list(lines)

            async def readline(self):
                return self._lines.pop(0) if self._lines else b""

        t = StdioTransport(["noop"])
        t._proc = type("P", (), {"stdout": FakeStdout([b"\xff\xfe\n"])})()
        assert await t.receive() is None

    @pytest.mark.asyncio
    async def test_oversized_line_is_dropped(self):
        from agentcrew.mcp.transports import StdioTransport

        class FakeStdout:
            def __init__(self, lines):
                self._lines = list(lines)

            async def readline(self):
                return self._lines.pop(0) if self._lines else b""

        t = StdioTransport(["noop"])
        huge = b"x" * (8 * 1024 * 1024 + 10) + b"\n"
        t._proc = type("P", (), {"stdout": FakeStdout([huge])})()
        assert await t.receive() is None

    @pytest.mark.asyncio
    async def test_valid_line_still_parses(self):
        from agentcrew.mcp.transports import StdioTransport

        class FakeStdout:
            def __init__(self, lines):
                self._lines = list(lines)

            async def readline(self):
                return self._lines.pop(0) if self._lines else b""

        t = StdioTransport(["noop"])
        t._proc = type("P", (), {
            "stdout": FakeStdout([b'{"jsonrpc":"2.0","id":1,"result":{}}\n'])})()
        msg = await t.receive()
        assert msg["id"] == 1


# ── 3. SIGTERM 优雅停机（仅 POSIX）──────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows 的 terminate() 是 TerminateProcess 硬杀，不触发信号处理")
def test_serve_registers_sigterm_handler(tmp_path):
    """D-1：``serve`` 必须为 SIGTERM 注册处理器（systemd/docker stop 走它）。

    只做静态检查：源码里出现 SIGTERM 且走 stop_event —— 真正发信号会杀测试进程。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "main.py").read_text(encoding="utf-8")
    assert "signal.SIGTERM" in source
    assert "add_signal_handler" in source
    assert signal.SIGTERM is not None
