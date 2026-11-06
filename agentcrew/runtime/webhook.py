"""R12：出站 Webhook——把 run 的关键节点推给外部系统。

这套编排器此前没有任何对外通知手段：run 结束、审批挂起、任务失败，外部系统一概
不知道，只能人盯着终端。Webhook 是让它能被别的系统用起来的第一块。

三个刻意的设计：

1. **投递在后台线程**。``on_event`` 是同步回调，可能在事件循环里被调用；在这里
   直接发 HTTP 会把整个 run 卡在一个网络超时上。所以事件进 ``queue.Queue``，
   由 daemon 线程消费。
2. **失败不拖垮 run**。重试次数有上限、退避有上限；run 结束时最多等 5 秒排空队列，
   排不完就记 ``webhook_failed(shutdown_timeout)`` 放手——通知是辅助设施，不能
   让主流程等它。
3. **不碰 Ledger**。``Ledger.emit`` 没有锁（每次 open/append/fsync），从后台线程
   写会与主线程交错。所以投递结果先进 ``_results`` 队列，由主线程在下次
   ``emit`` 或 ``close`` 时 drain 出来，再交给 ``sink``（即 Crew 的 ledger）。

**不做内网 IP 黑名单**：与 ``tools/network.py`` 的 ``web_fetch`` 不同，这里是运维
显式配置的目标，企业内网服务恰恰是最常见的接收方。``web_fetch`` 要防 SSRF，是因为
它的目标由模型决定。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

#: 接收方可据此分发，不必解析 body 才知道事件类型。
EVENT_HEADER = "X-AgentCrew-Event"
#: 每次投递的唯一 id（重试时**不变**），接收方用它做幂等。
DELIVERY_HEADER = "X-AgentCrew-Delivery"
#: 签名时间戳（Unix 秒）；接收方比对它即可拒绝过期重放。
TIMESTAMP_HEADER = "X-AgentCrew-Timestamp"
#: ``sha256=<hex>``，对 ``<timestamp>.<body>`` 求 HMAC。
SIGNATURE_HEADER = "X-AgentCrew-Signature"

#: run 结束时等待队列排空的上限：通知不该让收尾多等。
DEFAULT_CLOSE_TIMEOUT = 5.0

#: 重试退避的上限；到顶后不再增长，避免 2^n 把收尾拖长。
_MAX_BACKOFF_SECONDS = 2.0


@dataclass(frozen=True)
class WebhookConfig:
    """一份 Webhook 目标配置（值来自 :class:`agentcrew.config.Settings`）。

    ``events`` 含 ``"*"`` 时全推。``max_retries=0`` 表示不重试（仍会投递一次）。
    """

    url: str = ""
    secret: str = ""
    events: tuple[str, ...] = field(
        default=("run_started", "run_finished", "approval_requested",
                 "run_stalled", "deadlock")
    )
    timeout_seconds: float = 5.0
    max_retries: int = 2

    @property
    def enabled(self) -> bool:
        """只有配了 URL 才算启用——空 URL 下整个分发器是零开销的。"""
        return bool(self.url)

    def matches(self, event: str) -> bool:
        return "*" in self.events or event in self.events


class WebhookDispatcher:
    """事件 → POST 的后台投递器。

    ``sink`` 是投递结果的出口（形如 ``ledger.emit``），只在**主线程**被调用，
    因此 ``sink`` 自身不需要考虑线程安全。
    """

    def __init__(
        self,
        config: WebhookConfig,
        *,
        sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._config = config
        self._sink = sink
        self._queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        self._results: queue.Queue[dict[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._closed = False
        #: 便于测试与收尾汇总的计数（只在主线程读）。
        self.delivered = 0
        self.failed = 0
        if config.enabled:
            self._thread = threading.Thread(
                target=self._run, name="agentcrew-webhook", daemon=True)
            self._thread.start()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        """把一条事件交给后台投递；未启用 / 不订阅此事件时什么都不做。

        先 drain 上一批投递结果——这样「上一次失败」能在下一个事件点上及时落账，
        而不必等到 run 收尾。
        """
        self._drain()
        if self._closed or not self._config.enabled:
            return
        if not self._config.matches(event):
            return
        self._queue.put((event, dict(payload or {})))

    def close(self, *, timeout: float = DEFAULT_CLOSE_TIMEOUT) -> None:
        """停止接收新事件、等队列排空（最多 ``timeout`` 秒），并落最后一批结果。

        超时不是错误：记一条 ``shutdown_timeout`` 就放手。宁可少一次通知，
        也不让收尾卡在网络抖动上。
        """
        if self._closed:
            return
        self._closed = True
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout)
        if self._thread.is_alive():
            self.failed += 1
            self._record({
                "ok": False,
                "webhook_event": "*",
                "attempts": 0,
                "error": "shutdown_timeout",
            })
        self._drain()

    # ---- 后台线程 ----

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            event, payload = item
            self._deliver(event, payload)

    def _deliver(self, event: str, payload: dict[str, Any]) -> None:
        """投递一条事件；重试 ``max_retries`` 次后放弃。

        ``delivery`` id 在重试之间保持不变——接收方可以据此去重，避免「第一次
        其实收到了、只是响应慢」被当成失败而重复处理。
        """
        delivery = uuid.uuid4().hex
        # 时间戳只取一次：body 里写它、签名也用它。两处各取一次会在跨秒时
        # 对不上，接收方校验 body.timestamp == header.timestamp 就会失败。
        timestamp = str(int(time.time()))
        body = self._encode(event, payload, timestamp)
        last_error = ""
        for attempt in range(self._config.max_retries + 1):
            ok, detail = self._post(event, delivery, timestamp, body)
            if ok:
                self._record({"ok": True, "webhook_event": event,
                              "delivery": delivery, "attempts": attempt + 1})
                return
            last_error = detail
            if attempt < self._config.max_retries:
                time.sleep(min(0.2 * (2 ** attempt), _MAX_BACKOFF_SECONDS))
        self._record({"ok": False, "webhook_event": event, "delivery": delivery,
                      "attempts": self._config.max_retries + 1,
                      "error": last_error})

    def _encode(self, event: str, payload: dict[str, Any], timestamp: str) -> bytes:
        """组装 POST body：事件名 + 时间戳 + 原始 payload。"""
        envelope = {
            "event": event,
            "timestamp": int(timestamp),
            "data": payload,
        }
        return json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    def _post(
        self, event: str, delivery: str, timestamp: str, body: bytes
    ) -> tuple[bool, str]:
        """发一次 POST；返回 ``(是否 2xx, 失败说明)``。

        HTTPError 与网络异常分别捕获：前者说明对端收到了但拒绝（4xx/5xx，重试
        通常无意义却也无害），后者是连不上/超时（重试有意义）。两者都只返回说明，
        绝不向上抛——投递失败不该让 run 看到异常。
        """
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "agentcrew-webhook/1",
            EVENT_HEADER: event,
            DELIVERY_HEADER: delivery,
            TIMESTAMP_HEADER: timestamp,
        }
        if self._config.secret:
            headers[SIGNATURE_HEADER] = _sign(
                self._config.secret, timestamp, body)
        request = Request(
            self._config.url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self._config.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if 200 <= status < 300:
                    return True, ""
                return False, f"HTTP {status}"
        except HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except (URLError, OSError, ValueError) as exc:
            return False, f"{type(exc).__name__}: {exc}"[:200]

    # ---- 结果回传（只在主线程执行）----

    def _record(self, result: dict[str, Any]) -> None:
        """收一条投递结果进队列（由主线程 drain 后交给 sink）。

        **键名是 ``webhook_event`` 而不是 ``event``**：sink 通常是
        ``ledger.emit``，它的第一个位置参数就叫 ``event``，用同名键展开会
        撞成 ``TypeError: got multiple values for argument 'event'``——这会把
        一次通知的记账失败变成整个 run 的失败。
        """
        if result.get("ok"):
            self.delivered += 1
        else:
            self.failed += 1
        self._results.put(result)

    def _drain(self) -> None:
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                return
            if self._sink is None:
                continue
            name = "webhook_delivered" if result["ok"] else "webhook_failed"
            self._sink(name, result)


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    """``sha256=<hex>``，签名对象是 ``<timestamp>.<body>``。

    把时间戳纳入签名，接收方才能拒绝「很久以前截获、现在重放」的请求；只签 body
    的话，同一个 body 的旧请求可以无限重放。
    """
    message = timestamp.encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    secret: str, timestamp: str, body: bytes, signature: str
) -> bool:
    """接收方侧的校验实现（供示例与测试使用；本项目不发请求过去）。

    用 :func:`hmac.compare_digest` 做常量时间比较，避免按字节比较泄露签名前缀。
    """
    if not secret or not signature:
        return False
    return hmac.compare_digest(_sign(secret, timestamp, body), signature)

