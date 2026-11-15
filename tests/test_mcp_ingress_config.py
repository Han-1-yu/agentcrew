"""C9：``mcp_servers`` 入站配置契约——``command`` 字符串 + ``args``、``env`` 合并。

修的是一个「文档写了却不生效」的真 bug：``from_config`` 此前只接受 ``command`` 列表，
``args`` 被静默丢弃；仓库自带的浏览器模板（``command: npx`` + ``args``）按原样抄进去
会 100% 启动失败。这里钉住四种形状，避免再次退化。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from agentcrew.mcp.client import MCPClient
from agentcrew.mcp.transports import StdioTransport

REPO = Path(__file__).resolve().parents[1]


def test_command_string_plus_args():
    client = MCPClient.from_config({
        "command": "npx", "args": ["-y", "@playwright/mcp@latest"]})
    assert isinstance(client._transport, StdioTransport)
    assert client._transport._command == [
        "npx", "-y", "@playwright/mcp@latest"]


def test_command_list_plus_args_appends():
    client = MCPClient.from_config({
        "command": ["python", "-m", "srv"], "args": ["--flag"]})
    assert client._transport._command == [
        "python", "-m", "srv", "--flag"]


def test_env_merges_process_environment(monkeypatch):
    """整体替换会丢掉 PATH / SYSTEMROOT，Windows 上子进程直接起不来。"""
    monkeypatch.setenv("AGENTCREW_MCP_TEST_MARKER", "keep")
    client = MCPClient.from_config({
        "command": ["python"], "env": {"FOO": "bar"}})
    env = client._transport._env
    assert env is not None and env["FOO"] == "bar"
    assert env.get("PATH") or env.get("Path")
    if os.name == "nt":
        assert env.get("SYSTEMROOT") or env.get("SystemRoot")
    assert env["AGENTCREW_MCP_TEST_MARKER"] == "keep"


def test_browser_example_shape_really_works():
    """示例文件本身就是回归用例：它写的是 command: npx + args。"""
    data = yaml.safe_load(
        (REPO / "examples" / "agentcrew.browser.example.yaml").read_text(
            encoding="utf-8"))
    raw = data["mcp_servers"]["playwright"]
    client = MCPClient.from_config(raw)
    assert client._transport._command[0] == "npx"
    assert "@playwright/mcp@latest" in client._transport._command
