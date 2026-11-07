"""AgentCrew 官方 Python SDK：零依赖的 HTTP / SSE 同步客户端。

契约来源：``artifacts/contracts/openapi.yaml``。SDK 不 import 服务端代码，
服务端与客户端可独立升级。
"""

from .client import (
    DEFAULT_BASE_URL,
    TERMINAL_STATUSES,
    AgentCrewClient,
    Event,
)
from .errors import (
    AgentCrewError,
    APIError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    RateLimitError,
    ResponseError,
    TransportError,
    WaitTimeoutError,
)

__version__ = "1.0.0"

__all__ = [
    "DEFAULT_BASE_URL",
    "TERMINAL_STATUSES",
    "AgentCrewClient",
    "AgentCrewError",
    "APIError",
    "AuthenticationError",
    "ConflictError",
    "Event",
    "NotFoundError",
    "RateLimitError",
    "ResponseError",
    "TransportError",
    "WaitTimeoutError",
]
