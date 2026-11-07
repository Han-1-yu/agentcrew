"""AgentCrew SDK 的异常层次。

分两类：**连不上**（``TransportError``）与**连上了但服务端拒绝**
（``APIError`` 及其子类）。调用方通常只想区分这两类，所以子类都保留
``status_code`` / ``payload`` / ``retry_after``，不必再解析原始响应。

错误响应统一是 ``{"error": "<人类可读消息>"}``，见
``artifacts/contracts/openapi.yaml``；本模块不假设有 code/message/details
三段式，也不把响应体里的凭据回显到异常之外。
"""

from __future__ import annotations

from typing import Any


class AgentCrewError(Exception):
    """SDK 所有异常的基类。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}
        self.retry_after = retry_after


class TransportError(AgentCrewError):
    """网络不可达、DNS 失败、超时或连接被断开。"""


class APIError(AgentCrewError):
    """服务端返回了非 2xx；``status_code`` 一定有值。"""


class AuthenticationError(APIError):
    """401：没带 / 带错 API Key。"""


class NotFoundError(APIError):
    """404：run 或审批单不存在。"""


class ConflictError(APIError):
    """409：状态冲突（run 正在跑、审批单已裁决等）。"""


class RateLimitError(APIError):
    """429：按 IP 限流或并发 run 数达上限；``retry_after`` 可能来自响应头。"""


class WaitTimeoutError(AgentCrewError):
    """``wait_for_run`` 等到超时仍未进入终态。"""


class ResponseError(AgentCrewError):
    """2xx 但响应体不是合法 JSON / 结构不符合契约。"""
