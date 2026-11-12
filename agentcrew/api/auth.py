"""REST API 认证与速率限制中间件（E-3）。

两个可选、默认关闭的保护层，挂在 :class:`~agentcrew.api.server.ApiServer` 的
路由分发之前：

- :class:`APIKeyAuth` —— 校验 ``Authorization: Bearer <key>`` 或
  ``X-API-Key: <key>``。有效 key 列表为空时 :attr:`APIKeyAuth.enabled` 为假，
  中间件整体放行（向后兼容：不传 = 不认证）。
- :class:`RateLimiter` —— 按客户端 IP 的滑动窗口计数。``max_requests <= 0``
  时 :attr:`RateLimiter.enabled` 为假，中间件整体放行。

设计约束：

- **key 绝不外泄**：本模块只判断「成功 / 失败」，不把 key 值带进日志、异常
  消息或 ledger 事件（服务端记录的是 ``api_auth_failed`` 这个事件名与
  client_ip/path，不含任何凭据）。
- **零依赖**：滑动窗口用 ``dict[str, list[datetime]]`` 原地维护，不引入
  额外存储；窗口外的时间戳在每次 :meth:`RateLimiter.check` 时惰性清理。
- **可测时钟**：时间取自 :func:`datetime.now`，测试用小窗口 + 直接调用
  :meth:`RateLimiter.check` 即可验证窗口行为，无需 sleep 真实秒数。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta


class AuthError(Exception):
    """请求缺少有效 API Key（服务端据此回 ``401 Unauthorized``）。

    异常消息只描述「为什么被拒」（缺失 / 无效），绝不回显客户端发来的凭据。
    """


class RateLimitError(Exception):
    """客户端在滑动窗口内的请求数超限（服务端据此回 ``429 Too Many Requests``）。"""


class APIKeyAuth:
    """校验请求头里携带的 API Key 是否落在有效列表内。

    接受两种等价的投递方式（与大多数网关 / SDK 约定一致）：

    - ``Authorization: Bearer <key>``
    - ``X-API-Key: <key>``

    构造时去掉空白、过滤空串；有效列表为空时 :attr:`enabled` 为假，
    :meth:`authenticate` 一律返回 ``True``（不启用 = 不拦截）。
    """

    def __init__(self, api_keys: list[str]) -> None:
        # 冻结为 tuple：之后调用方的列表变更不影响本实例（与 Ledger.secrets
        # 同一思路——凭据集合不该在构造后被悄悄改动）。
        self._keys: tuple[str, ...] = tuple(
            key.strip() for key in api_keys if key and key.strip()
        )

    @property
    def enabled(self) -> bool:
        """是否启用认证（有效 key 列表非空时启用）。"""
        return bool(self._keys)

    def authenticate(self, request_headers: dict[str, str]) -> bool:
        """校验请求头；``enabled`` 为假时恒为 ``True``。

        ``request_headers`` 的键按小写存储（服务端解析请求头时统一小写化），
        因此这里用小写键查询，不受客户端大小写影响。
        """
        if not self.enabled:
            return True
        presented = self._extract_key(request_headers)
        if presented is None:
            return False
        # B-6：恒定时间比较。此前是 `presented in self._keys`——Python 的
        # `in` 在命中即返回，正确前缀越长的 key 越快通过，理论上可被时序测量
        # 逐字节猜出。这里对所有 key 都完整比较一遍、**不短路**。
        # 用 bytes 比较：``compare_digest`` 对 str 只支持 ASCII，中文 key 会抛
        # TypeError；编码后比较对任意 Unicode 都成立。
        presented_bytes = presented.encode("utf-8")
        matched = False
        for key in self._keys:
            if secrets.compare_digest(presented_bytes, key.encode("utf-8")):
                matched = True
        return matched

    @staticmethod
    def _extract_key(headers: dict[str, str]) -> str | None:
        """从 ``Authorization: Bearer`` 或 ``X-API-Key`` 取出 key；都没有返回 None。"""
        authorization = headers.get("authorization", "")
        if authorization[:7].lower() == "bearer ":
            token = authorization[7:].strip()
            if token:
                return token
        x_api_key = headers.get("x-api-key", "")
        if x_api_key.strip():
            return x_api_key.strip()
        return None


class RateLimiter:
    """按客户端 IP 的滑动窗口速率限制。

    窗口内（最近 ``window_seconds`` 秒）的请求数达到 ``max_requests`` 即拒绝
    后续请求，直到最早的时间戳滑出窗口。每次 :meth:`check` 惰性清理该 IP 的
    过期时间戳——不跑定时任务，内存里只为「真的来过的 IP」留记录。
    """

    def __init__(self, max_requests: int = 100, window_seconds: int = 60) -> None:
        self._max_requests = max_requests
        self._window = timedelta(seconds=window_seconds)
        self._hits: dict[str, list[datetime]] = {}

    @property
    def enabled(self) -> bool:
        """是否启用限流（``max_requests > 0`` 时启用）。"""
        return self._max_requests > 0

    @property
    def window_seconds(self) -> int:
        """窗口时长（秒）；服务端据此写 ``429`` 响应的 ``Retry-After``。"""
        return int(self._window.total_seconds())

    def check(self, client_ip: str) -> tuple[bool, int]:
        """记录一次来自 ``client_ip`` 的请求，返回 ``(是否允许, 剩余次数)``。

        - 允许：把本次时间戳计入，剩余 = 上限 - 当前窗口内计数。
        - 拒绝：达到上限，不记入本次时间戳，剩余次数为 0（调用方据此写
          ``Retry-After``）。
        - :attr:`enabled` 为假时恒返回 ``(True, max_requests)``，且不记账。
        """
        if not self.enabled:
            return True, self._max_requests
        now = datetime.now()
        cutoff = now - self._window
        hits = [ts for ts in self._hits.get(client_ip, ()) if ts > cutoff]
        if len(hits) >= self._max_requests:
            # 拒绝不记账：否则被拒的攻击请求会一直占着窗口、让 Retry-After
            # 永远等不到窗口滑动。
            self._hits[client_ip] = hits
            return False, 0
        hits.append(now)
        self._hits[client_ip] = hits
        return True, self._max_requests - len(hits)
