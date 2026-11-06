"""只读网页抓取工具（R3）：标准库实现 + SSRF 防护，零第三方依赖。

同类框架普遍自带 ``web_fetch`` / ``web_search``。本模块只做**只读抓取**：
``web_search`` 依赖外部搜索 API 与密钥，与「密钥绝不落盘」和离线可测的口径冲突，
本轮不引入（见 ``PROJECT-STATE.md`` §7）。

安全边界（每一条都有对应测试）：

- 只接受 ``http`` / ``https``，端口只允许 80 / 443（不给内网端口探测留口子）；
- 拒绝内嵌凭据、``localhost`` / ``*.internal`` / ``*.local`` 与字面私网 IP；
- **真实请求前**解析 DNS，任一解析结果落在私网 / 回环 / 链路本地 / 保留段即拒绝
  （防 DNS rebinding 与元数据服务 ``169.254.169.254``）；
- **不跟随重定向** —— 否则一个公网 URL 可以 302 到内网地址绕过上面所有检查；
- 只接收文本类 Content-Type，响应体硬上限 256 KiB，超时默认 10 秒；
- ``fetcher`` 可注入同步函数，让单测完全离线（hermetic），也便于接入自定义代理。

与 :class:`~agentcrew.tools.builtins.ReadFileTool` 一样，本工具位于 ``tools`` 层，
不 import ``runtime`` / ``agents`` / ``crew`` 等上层（架构护栏）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import Field

from agentcrew.tools.tool import Tool

_ALLOWED_SCHEMES = frozenset(("http", "https"))
_ALLOWED_PORTS = frozenset((80, 443))
_FORBIDDEN_HOST_SUFFIXES = (".localhost", ".internal", ".local", ".localdomain")
_TEXT_CONTENT_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/x-www-form-urlencoded",
)
_MAX_RESPONSE_BYTES = 256 * 1024
#: 回灌给模型的默认正文长度与硬上限（字符）。
DEFAULT_MAX_CHARS = 8000
MAX_MAX_CHARS = 50000


class _NoRedirect(HTTPRedirectHandler):
    """让 urllib 遇到 3xx 直接报错，而不是偷偷请求另一个（可能是内网的）地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_fetch_url(url: str) -> str | None:
    """词法层 SSRF 校验；返回中文错误文本，``None`` 表示通过（DNS 层另查）。

    这是纯函数：不发网络请求、不解析域名，因此单测可以直接覆盖全部拒绝分支。
    """
    if not isinstance(url, str) or not url.strip():
        return "错误：url 必须是非空字符串"
    if any(char.isspace() or ord(char) < 0x20 for char in url):
        return "错误：url 含空白或控制字符"
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return f"错误：url 无法解析：{exc}"
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return (
            f"错误：只允许 http/https，收到 {parts.scheme!r}"
            "（file://、gopher://、data: 等一律拒绝）"
        )
    if parts.username or parts.password:
        return "错误：url 不允许内嵌用户名或密码"
    host = parts.hostname
    if not host:
        return "错误：url 缺少主机名"
    lowered = host.lower()
    if (
        lowered in ("localhost", "localhost.localdomain")
        or lowered.endswith(_FORBIDDEN_HOST_SUFFIXES)
    ):
        return f"错误：拒绝本地主机名 {host!r}"
    try:
        port = parts.port
    except ValueError:
        return "错误：url 端口非法"
    if port is not None and port not in _ALLOWED_PORTS:
        return f"错误：只允许 80/443 端口，收到 {port}"
    try:
        literal_ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass  # 域名：真实请求前再做 DNS 解析检查
    else:
        if _is_forbidden_ip(literal_ip):
            return f"错误：拒绝非公网 IP {host!r}"
    return None


def _http_get_text(url: str, timeout: float, max_bytes: int) -> str:
    """同步抓取（在 ``asyncio.to_thread`` 里跑，不阻塞事件循环）。

    DNS 解析结果逐个检查，且用不跟随重定向的 opener；任何失败都抛异常，由
    :meth:`WebFetchTool.run` 收敛成错误文本回灌，绝不冒泡断掉 Agent。
    """
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    host = parts.hostname or ""
    for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_forbidden_ip(ip):
            raise ValueError(f"目标解析到非公网地址 {ip}，已拒绝")

    request = Request(
        url,
        headers={
            "User-Agent": "AgentCrew-WebFetch/1.0",
            "Accept": "text/*, application/json, application/xml;q=0.8",
        },
    )
    opener = build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        content_type = (
            response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        )
        allowed = content_type.startswith(_TEXT_CONTENT_PREFIXES) or (
            content_type.endswith("+json") or content_type.endswith("+xml")
        )
        if content_type and not allowed:
            raise ValueError(f"响应不是文本类型（{content_type}），已拒绝")
        data = response.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode("utf-8", errors="replace")
    if truncated:
        text += f"\n…（响应超过 {max_bytes} 字节上限，已截断）"
    return text


class WebFetchTool(Tool):
    """抓取一个公网 URL 的文本内容（只读、限量、防 SSRF）。"""

    name: str = "web_fetch"
    description: str = (
        "抓取一个公网 http/https 页面的文本内容并返回（只读，不跟随重定向，"
        "正文默认截断到 8000 字符）。用于查阅公开文档；内网地址一律拒绝。"
    )
    input_schema: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "url": {
                "type": "string", "minLength": 1, "maxLength": 2000,
                "description": "公网 http/https URL",
            },
            "max_chars": {
                "type": "integer", "minimum": 1, "maximum": MAX_MAX_CHARS,
                "description": "返回正文的最大字符数，默认 8000",
            },
        },
        "required": ["url"],
    })
    timeout_secs: float = 10.0
    max_response_bytes: int = _MAX_RESPONSE_BYTES
    #: 可选的域名白名单：非空时只允许精确匹配（大小写不敏感）的主机。
    allowed_hosts: list[str] = Field(default_factory=list)
    #: 测试/自定义代理注入点：同步 ``(url, timeout, max_bytes) -> str``。
    fetcher: Callable[[str, float, int], str] | None = None

    async def run(
        self, url: str = "", max_chars: int | None = None, **kwargs: Any
    ) -> str:
        error = validate_fetch_url(url)
        if error:
            return error
        if self.allowed_hosts:
            host = (urlsplit(url).hostname or "").lower()
            allowed = {item.lower() for item in self.allowed_hosts}
            if host not in allowed:
                return f"错误：主机 {host!r} 不在允许的域名白名单内"

        if max_chars is None:
            limit = DEFAULT_MAX_CHARS
        else:
            if isinstance(max_chars, bool) or not isinstance(max_chars, int):
                return "错误：max_chars 必须是整数"
            if not 1 <= max_chars <= MAX_MAX_CHARS:
                return f"错误：max_chars 必须落在 1~{MAX_MAX_CHARS} 之间"
            limit = max_chars

        fetch = self.fetcher or _http_get_text
        try:
            text = await asyncio.to_thread(
                fetch, url, self.timeout_secs, self.max_response_bytes
            )
        except (HTTPError, URLError, OSError, ValueError, TimeoutError) as exc:
            return f"错误：抓取失败：{exc}"
        except Exception as exc:  # 注入的 fetcher 可能抛任意异常，一律收敛
            return f"错误：抓取失败：{type(exc).__name__}: {exc}"
        if not isinstance(text, str):
            return "错误：抓取器没有返回文本"
        clipped = text[:limit]
        suffix = "" if len(text) <= limit else f"\n…（已截断到 {limit} 字符）"
        return f"URL: {url}\n\n{clipped}{suffix}"
