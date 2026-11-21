"""R3 新增内置工具：受控写文件 + 只读网页抓取（SSRF 防护）。

覆盖：写路径越界/链接/大小/目录边界、写与产物目录隔离；URL 词法校验全部拒绝
分支、DNS 私网判定、域名白名单、正文截断、注入 fetcher 的离线执行、异常收敛。
全部测试零网络（``fetcher`` 注入），符合 hermetic 口径。
"""

from __future__ import annotations

import http.client
import http.server
import ipaddress
import socket
import threading
from pathlib import Path
from urllib.error import HTTPError

import pytest

import agentcrew.tools.network as network_mod
from agentcrew.tools.builtins import WriteFileTool
from agentcrew.tools.network import (
    MAX_MAX_CHARS,
    WebFetchTool,
    _is_forbidden_ip,
    _NoRedirect,
    _pick_public_ip,
    validate_fetch_url,
)
from agentcrew.tools.schema import validate_tool_args
from agentcrew.utils.files import read_text

# ── WriteFileTool ──────────────────────────────────────────────────────

@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "run" / "workspace"
    root.mkdir(parents=True)
    return root


async def test_write_file_then_read_back(workspace: Path):
    tool = WriteFileTool(workdir=workspace)
    result = await tool.run(path="notes/api.md", content="# API\n中文也能写")
    assert "已写入" in result
    assert read_text(workspace / "notes" / "api.md") == "# API\n中文也能写"


@pytest.mark.parametrize("path", [
    "../outside.txt", "a/../../outside.txt", "/etc/passwd", "C:/tmp/x.txt",
    "a\\..\\..\\outside.txt", "CON.txt", "a/  ",
])
async def test_write_file_rejects_unsafe_paths(workspace: Path, path: str):
    tool = WriteFileTool(workdir=workspace)
    result = await tool.run(path=path, content="leak")
    assert result.startswith("错误："), (path, result)
    assert not (workspace.parent / "outside.txt").exists()


async def test_write_file_rejects_root_dir_and_existing_dir(workspace: Path):
    tool = WriteFileTool(workdir=workspace)
    (workspace / "sub").mkdir()
    assert "错误" in await tool.run(path=".", content="x")
    assert "错误" in await tool.run(path="sub", content="x")


async def test_write_file_rejects_bad_args(workspace: Path):
    tool = WriteFileTool(workdir=workspace, max_bytes=16)
    assert "缺少必填参数 path" in await tool.run(path="", content="x")
    assert "content 必须是字符串" in await tool.run(path="a.txt", content={"a": 1})
    assert "超过上限" in await tool.run(path="a.txt", content="x" * 40)
    assert not (workspace / "a.txt").exists()


async def test_write_file_rejects_path_through_link(workspace: Path, monkeypatch):
    """写侧逐段符号链接检查：不能跟随链接把内容写到工作区之外。"""
    outside = workspace.parent / "outside.txt"
    (workspace / "link.txt").write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(
        "agentcrew.tools.builtins.is_link_like",
        lambda path: path.name == "link.txt",
    )
    tool = WriteFileTool(workdir=workspace)
    result = await tool.run(path="link.txt", content="escaped")
    assert result.startswith("错误：")
    assert (workspace / "link.txt").read_text(encoding="utf-8") == "placeholder"
    assert not outside.exists()


async def test_write_file_schema_declares_required_args(workspace: Path):
    tool = WriteFileTool(workdir=workspace)
    errors = validate_tool_args(tool.input_schema, {"path": "a.txt"})
    assert any("缺少必填参数 content" in e for e in errors)
    assert validate_tool_args(tool.input_schema, {
        "path": "a.txt", "content": "ok"}) == []


# ── WebFetchTool：词法层 SSRF ──────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://example.com/_x",
    "ftp://example.com/x",
    "http://localhost/x",
    "http://service.internal/x",
    "http://box.local/x",
    "http://127.0.0.1/x",
    "http://10.0.0.5/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://192.168.1.1/x",
    "http://[::1]/x",
    "http://example.com:8080/x",
    "http://user:pass@example.com/x",
    "http://example.com/a b",
    "http://example.com:abc/x",
    "",
])
async def test_web_fetch_rejects_unsafe_urls(url: str):
    assert validate_fetch_url(url) is not None
    tool = WebFetchTool()
    result = await tool.run(url=url)
    assert result.startswith("错误："), (url, result)


@pytest.mark.parametrize("url", [
    "https://example.com/docs",
    "http://example.com:80/a?b=1",
    "https://example.com:443/a#frag",
])
def test_web_fetch_allows_public_urls(url: str):
    assert validate_fetch_url(url) is None


def test_forbidden_ip_predicate_covers_reserved_ranges():
    assert _is_forbidden_ip(ipaddress.ip_address("127.0.0.1"))
    assert _is_forbidden_ip(ipaddress.ip_address("169.254.169.254"))
    assert _is_forbidden_ip(ipaddress.ip_address("::1"))
    assert _is_forbidden_ip(ipaddress.ip_address("0.0.0.0"))
    assert not _is_forbidden_ip(ipaddress.ip_address("93.184.216.34"))


def test_redirect_handler_refuses_to_follow():
    assert _NoRedirect().redirect_request(None, None, 302, "m", {}, "http://x") is None


# ── WebFetchTool：执行层（全部离线） ───────────────────────────────────

async def test_web_fetch_uses_injected_fetcher_and_reports_url():
    seen: list[tuple[str, float, int]] = []

    def fetcher(url: str, timeout: float, max_bytes: int) -> str:
        seen.append((url, timeout, max_bytes))
        return "# 文档\n正文"

    tool = WebFetchTool(fetcher=fetcher, timeout_secs=3.5, max_response_bytes=1024)
    result = await tool.run(url="https://example.com/docs")
    assert result.startswith("URL: https://example.com/docs")
    assert "# 文档" in result
    assert seen == [("https://example.com/docs", 3.5, 1024)]


async def test_web_fetch_clips_body_to_max_chars():
    tool = WebFetchTool(fetcher=lambda url, timeout, max_bytes: "x" * 200)
    result = await tool.run(url="https://example.com", max_chars=10)
    assert "x" * 10 in result
    assert "x" * 11 not in result
    assert "已截断到 10 字符" in result


@pytest.mark.parametrize("value", [0, -1, MAX_MAX_CHARS + 1, "10", True, 1.5])
async def test_web_fetch_rejects_bad_max_chars(value):
    tool = WebFetchTool(fetcher=lambda url, timeout, max_bytes: "ok")
    assert (await tool.run(url="https://example.com", max_chars=value)).startswith("错误：")


async def test_web_fetch_host_allowlist():
    tool = WebFetchTool(
        fetcher=lambda url, timeout, max_bytes: "ok",
        allowed_hosts=["docs.example.com"],
    )
    assert (await tool.run(url="https://evil.example.com")).startswith("错误：")
    assert "ok" in await tool.run(url="https://docs.example.com/x")


async def test_web_fetch_converges_fetcher_exception():
    def broken(url: str, timeout: float, max_bytes: int) -> str:
        raise RuntimeError("上游炸了")

    tool = WebFetchTool(fetcher=broken)
    result = await tool.run(url="https://example.com")
    assert result.startswith("错误：抓取失败：")
    assert "上游炸了" in result


async def test_web_fetch_rejects_non_text_fetcher_result():
    tool = WebFetchTool(fetcher=lambda url, timeout, max_bytes: 123)  # type: ignore[arg-type,return-value]
    assert "没有返回文本" in await tool.run(url="https://example.com")


def test_web_fetch_schema_rejects_bad_args():
    tool = WebFetchTool()
    assert validate_tool_args(tool.input_schema, {}) != []
    assert validate_tool_args(tool.input_schema, {
        "url": "https://example.com", "max_chars": MAX_MAX_CHARS + 1,
    }) != []
    assert validate_tool_args(tool.input_schema, {
        "url": "https://example.com", "max_chars": 100,
    }) == []


# ── P3-1：DNS rebinding 修复——按校验通过的 IP 直连 ────────────────────────
# 旧实现的窗口：getaddrinfo 校验全部公网后交给 urllib 再解析一次 DNS，
# 两次解析之间域名可以被切回私网（TOCTOU）。新实现校验与建连用同一份解析结果。

#: 4xx 回测服务器端口（模块级起一次，供 HTTPError 文案用例连接）。
_HTTP_ERR_SERVER: http.server.HTTPServer | None = None
#: 回测服务器最近一次请求的要素（Host / path）。
_LAST_REQUEST: dict[str, str] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    """回测服务器：记录请求要素并返回固定正文；/missing 返回 404。"""

    def do_GET(self):  # noqa: N802 —— http.server 命名约定
        _LAST_REQUEST["host"] = self.headers.get("Host", "")
        _LAST_REQUEST["path"] = self.path
        if self.path.startswith("/missing"):
            self.send_error(404, "Not Found")
            return
        body = b"regression-ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # pragma: no cover - 静默回测日志
        pass


def _ensure_err_server() -> int:
    """懒起 404 回测服务器，返回端口（进程内复用）。"""
    global _HTTP_ERR_SERVER
    if _HTTP_ERR_SERVER is None:
        _HTTP_ERR_SERVER = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=_HTTP_ERR_SERVER.serve_forever, daemon=True).start()
    assert _HTTP_ERR_SERVER is not None
    return _HTTP_ERR_SERVER.server_address[1]


def _connect_local(sock_cls, timeout):
    """直连本地 404 回测服务器（绕过被替换的 getaddrinfo）。"""
    sock = sock_cls()
    sock.settimeout(timeout)
    sock.connect(("127.0.0.1", _ensure_err_server()))
    return sock



def _addrinfo(ip: str, port: int = 80):
    """构造 getaddrinfo 形状的单条记录。"""
    version = 6 if ":" in ip else 4
    return (socket.AF_INET6 if version == 6 else socket.AF_INET,
            socket.SOCK_STREAM, 6, "", (ip, port))


def test_pick_public_ip_rejects_any_private_record():
    with pytest.raises(ValueError, match="非公网"):
        _pick_public_ip([_addrinfo("93.184.216.34"), _addrinfo("192.168.1.5")])


def test_pick_public_ip_rejects_empty_resolution():
    with pytest.raises(ValueError, match="没有可用的解析结果"):
        _pick_public_ip([])


def test_pick_public_ip_returns_first_allowed():
    ip = _pick_public_ip([_addrinfo("93.184.216.34"), _addrinfo("8.8.8.8")])
    assert ip == "93.184.216.34"


def test_http_get_text_rejects_before_connecting(monkeypatch):
    """解析出私网 IP 时必须在建连之前拒绝——create_connection 被碰即测试失败。"""
    def forbidden(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("私网解析结果不应触发任何建连")
    monkeypatch.setattr(network_mod.socket, "getaddrinfo",
                        lambda *a, **k: [_addrinfo("169.254.169.254", 80)])
    monkeypatch.setattr(network_mod.socket, "create_connection", forbidden)
    with pytest.raises(ValueError, match="非公网"):
        network_mod._http_get_text("http://metadata.example/latest/", 5.0, 1024)


def test_http_get_text_connects_to_validated_ip(monkeypatch):
    """hermetic 端到端：解析结果指向公网 IP，但真实连接被路由到本地测试服务器。"""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        real_socket = socket.socket

        def routed_create(address, timeout=None, **kwargs):
            # address 是校验后的公网 IP；真正连到本地回测服务器（hermetic）。
            # 注意不能用 socket.create_connection：它内部还会走 getaddrinfo，
            # 而 getaddrinfo 已被本用例替换，会把回环地址解析成假公网 IP。
            _LAST_REQUEST["dialed"] = f"{address[0]}:{address[1]}"
            sock = real_socket()
            sock.settimeout(timeout)
            sock.connect(("127.0.0.1", server.server_address[1]))
            return sock

        monkeypatch.setattr(network_mod.socket, "getaddrinfo",
                            lambda *a, **k: [_addrinfo("93.184.216.34", 80)])
        monkeypatch.setattr(network_mod.socket, "create_connection", routed_create)
        text = network_mod._http_get_text("http://example.com/docs?a=1", 5.0, 4096)
        # Host 头仍是原域名（虚拟主机口径），连接确实按校验过的 IP 直连。
        assert _LAST_REQUEST["host"] == "example.com"
        assert _LAST_REQUEST["dialed"].startswith("93.184.216.34:")
        assert _LAST_REQUEST["path"] == "/docs?a=1"
        assert "regression-ok" in text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_get_text_converts_http_error_like_old_opener(monkeypatch):
    """4xx 仍以 HTTPError 抛出（错误文案与旧 urllib 路径一致）。"""
    real_socket = socket.socket
    monkeypatch.setattr(network_mod.socket, "getaddrinfo",
                        lambda *a, **k: [_addrinfo("93.184.216.34", 80)])
    monkeypatch.setattr(network_mod.socket, "create_connection",
                        lambda address, timeout=None, **k: _connect_local(
                            real_socket, timeout))
    with pytest.raises(HTTPError):
        network_mod._http_get_text("http://example.com/missing", 5.0, 1024)
