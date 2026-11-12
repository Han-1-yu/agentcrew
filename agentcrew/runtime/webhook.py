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

R15：持久化重放队列
-------------------

R12/R14 的致命边界是「进程退出时队列里没投出去的通知直接丢」。现在把每条
「事件 × 接收端」在**入队之前**就写成一个 JSON 文件（spool），投递成功才删除；
进程被杀、收尾超时、对端长时间不可用，文件都留在盘上，下一次启用 Webhook 的
run 启动时自动补投。

几个刻意约束：

* **密钥与 URL 都不落盘**。文件里只有 ``target_key = sha256(url)[:16]``，补投时
  用当前配置重新解析接收端；secret 永远来自内存里的 Settings。payload 落盘前
  按 ``secret_values()`` 递归脱敏，避免绕过 Ledger 已有的脱敏把密钥抄进 spool。
* **at-least-once**。多进程/多 run 同时补投同一文件时可能双投，接收方用既有的
  ``X-AgentCrew-Delivery`` 幂等（delivery 与 timestamp 跨重试、跨重放都不变）。
* **有终止条件**：记录超过 24 小时或累计重放 5 次即丢弃并记
  ``webhook_failed(replay_discarded)``，不让陈年事件无限重放。
* **不启用 spool 时行为与 R14 逐字一致**：``spool_dir=None`` 零文件、零扫描。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

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

#: B-7：``WebhookConfig`` 的兜底上界（``run options`` 入口另有同值校验）。
#: 每个 run 一个投递线程、``close()`` 只等 5 秒——超大值会把后台线程与连接
#: 占用放大成资源攻击面。
_MAX_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 5


class _NoRedirect(HTTPRedirectHandler):
    """让 urllib 遇到 3xx 直接报错，而不是悄悄请求另一个地址。

    即使接收端 URL 来自可信的服务端配置，重定向仍是放大面：公网端点被劫持后
    302 到任意地址，签名头会跟着发过去。webhook 接收端不需要重定向语义，
    3xx 一律按投递失败处理、走正常重试。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


#: 进程级共享 opener：不跟随重定向。
_OPENER = build_opener(_NoRedirect())

#: 语义上就是「出了事」的事件：``only_failures`` 的接收端一律收。
_FAILURE_EVENTS = frozenset({"error", "deadlock", "run_stalled", "budget_exhausted"})
#: ``payload["status"]`` 落在这些值时算「没跑成」。
_FAILURE_STATUSES = frozenset({"failed", "cancelled"})

#: R15：spool 目录名（位于 ``output_dir`` 下）。``CheckpointStore._run_dirs`` 只认
#: 含 ``run.json`` 的目录，所以 ``clean`` / ``delete_run`` 都不会误删它。
SPOOL_DIR_NAME = "_webhook_spool"
#: R15：待投记录的最长存活时间；超时丢弃，避免陈年事件无限重放。
SPOOL_MAX_AGE_SECONDS = 24 * 3600.0
#: R15：单条记录累计重放次数上限。
SPOOL_MAX_ATTEMPTS = 5
_SPOOL_SUFFIX = ".json"


def _is_failure(event: str, payload: dict[str, Any]) -> bool:
    """这条事件是不是「坏消息」。

    只看 ``status`` 字段与几个固定事件名，不做语义推断：``cancelled`` 也算——对
    接收方来说「没跑完」比「成功」更值得知道，取消只是原因不同。
    """
    if event in _FAILURE_EVENTS:
        return True
    return str(payload.get("status") or "").lower() in _FAILURE_STATUSES


@dataclass(frozen=True)
class WebhookTarget:
    """一个接收端。

    ``events`` 为空 = 不限事件；``only_failures`` 再叠一层「只推坏消息」——两者是
    **与**关系，这样「Slack 只收失败、内部系统全收」可以配成两个 target。
    """

    name: str = ""
    url: str = ""
    secret: str = ""
    events: tuple[str, ...] = ()
    only_failures: bool = False

    def matches(self, event: str, payload: dict[str, Any]) -> bool:
        if self.events and "*" not in self.events and event not in self.events:
            return False
        if self.only_failures and not _is_failure(event, payload):
            return False
        return True


@dataclass(frozen=True)
class WebhookConfig:
    """Webhook 配置：一个或多个接收端。

    ``targets`` 非空时以它为准；否则退回单接收端字段（``url`` / ``secret`` /
    ``events`` / ``only_failures``）——旧配置因此一字不用改。
    """

    url: str = ""
    secret: str = ""
    events: tuple[str, ...] = field(
        default=("run_started", "run_finished", "approval_requested",
                 "run_stalled", "deadlock")
    )
    timeout_seconds: float = 5.0
    max_retries: int = 2
    only_failures: bool = False
    targets: tuple[WebhookTarget, ...] = ()

    def __post_init__(self) -> None:
        """B-7 兜底：``run options`` 入口之外（手写 YAML / 程序化构造）也能拿到
        超大值的 ``timeout_seconds`` / ``max_retries``，这里 clamp 到安全上界。

        frozen dataclass 用 ``object.__setattr__``；clamp 而不是抛错——通知是
        辅助设施，配置笔误不该让整个 run 起不来（收不到通知这件事本身会以
        ``webhook_failed`` 留痕）。
        """
        if not (0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS):
            object.__setattr__(
                self, "timeout_seconds",
                min(max(float(self.timeout_seconds), 0.1), _MAX_TIMEOUT_SECONDS))
        if not (0 <= self.max_retries <= _MAX_RETRIES):
            object.__setattr__(
                self, "max_retries",
                min(max(int(self.max_retries), 0), _MAX_RETRIES))

    def resolved(self) -> tuple[WebhookTarget, ...]:
        """展开成实际要投递的接收端列表（已剔除没有 URL 的）。"""
        if self.targets:
            return tuple(t for t in self.targets if t.url)
        if self.url:
            return (WebhookTarget(
                name="default", url=self.url, secret=self.secret,
                events=self.events, only_failures=self.only_failures),)
        return ()

    @property
    def enabled(self) -> bool:
        """有任一可用接收端才算启用——全空时整个分发器零开销。"""
        return bool(self.resolved())

    def matches(self, event: str, payload: dict[str, Any] | None = None) -> bool:
        """是否有**任一**接收端想要这条事件（分发器据此决定是否入队）。"""
        data = payload or {}
        return any(target.matches(event, data) for target in self.resolved())


def _target_key(url: str) -> str:
    """接收端的落盘标识：只存 URL 的短哈希，不存 URL 本身。

    补投时按当前配置重新算一遍哈希来匹配；URL 改了就匹配不上，那条记录会被
    丢弃而不是发到新地址去。
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    """递归替换字符串里的密钥；只在写 spool 文件时用。"""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "***")
        return value
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, secrets) for item in value]
    return value


@dataclass(frozen=True)
class PendingDelivery:
    """一条「事件 × 接收端」的待投记录（可跨进程存活）。

    ``delivery`` / ``timestamp`` 在首次入队时就固定下来，重试与跨进程重放都不变——
    接收方靠它们做幂等与重放校验。
    """

    event: str
    payload: dict[str, Any]
    delivery: str
    timestamp: str
    target_key: str
    created_at: float
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "event": self.event,
            "payload": self.payload,
            "delivery": self.delivery,
            "timestamp": self.timestamp,
            "target_key": self.target_key,
            "created_at": self.created_at,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PendingDelivery | None:
        """坏形状 / 旧版本一律返回 ``None``，由调用方丢弃文件。"""
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        if not isinstance(data.get("payload"), dict):
            return None
        if not all(isinstance(data.get(key), str) and data[key]
                   for key in ("event", "delivery", "timestamp", "target_key")):
            return None
        try:
            created_at = float(data["created_at"])
            attempts = int(data.get("attempts", 0))
        except (KeyError, TypeError, ValueError):
            return None
        if attempts < 0:
            return None
        return cls(
            event=data["event"], payload=data["payload"],
            delivery=data["delivery"], timestamp=data["timestamp"],
            target_key=data["target_key"], created_at=created_at,
            attempts=attempts,
        )


class WebhookSpool:
    """待投队列的落盘实现：一条记录一个文件，成功即删。

    为什么不用单个 JSONL 追加文件：删除「已投递」的那一行需要重写整个文件，
    多 run 并发时会互相覆盖。一记录一文件后，``os.replace`` 写、``unlink`` 删
    都是原子操作，天然容忍多进程。
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        secrets: tuple[str, ...] = (),
        max_age_seconds: float = SPOOL_MAX_AGE_SECONDS,
        max_attempts: int = SPOOL_MAX_ATTEMPTS,
    ) -> None:
        self.directory = Path(directory)
        # 太短的串会把正常文本误伤成 ***；Ledger 的脱敏也只关心真正的密钥。
        self._secrets = tuple(s for s in secrets if isinstance(s, str) and len(s) >= 4)
        self._max_age = max_age_seconds
        self._max_attempts = max_attempts

    def add(self, record: PendingDelivery) -> Path | None:
        """写一条新记录；失败返回 ``None``（落盘是增强，不能让 run 挂掉）。"""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = int(record.created_at * 1_000_000)
            path = self.directory / (
                f"{stamp}-{record.delivery[:8]}-{record.target_key}"
                f"-{uuid.uuid4().hex[:6]}{_SPOOL_SUFFIX}")
            self._write(path, record)
            return path
        except (OSError, TypeError, ValueError):
            return None

    def _write(self, path: Path, record: PendingDelivery) -> None:
        data = _redact_value(record.to_dict(), self._secrets)
        text = json.dumps(data, ensure_ascii=False)
        tmp = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def remove(self, path: Path) -> None:
        """投递成功后删除；文件已被别的进程删掉也是成功。"""
        try:
            path.unlink()
        except OSError:
            pass

    def retry(self, path: Path, record: PendingDelivery) -> None:
        """一次投递失败：累加 attempts 写回，超限则丢弃。"""
        next_record = PendingDelivery(
            event=record.event, payload=record.payload,
            delivery=record.delivery, timestamp=record.timestamp,
            target_key=record.target_key, created_at=record.created_at,
            attempts=record.attempts + 1,
        )
        if next_record.attempts > self._max_attempts:
            self.remove(path)
            return
        try:
            self._write(path, next_record)
        except (OSError, TypeError, ValueError):
            pass

    def load(self) -> tuple[list[tuple[Path, PendingDelivery]], list[tuple[str, str]]]:
        """扫出待补投记录；顺手清理坏文件、过期记录与超次数的记录。

        返回 ``(待投列表, 被丢弃的 (delivery, 原因) 列表)``。
        """
        pending: list[tuple[Path, PendingDelivery]] = []
        discarded: list[tuple[str, str]] = []
        try:
            paths = sorted(self.directory.glob(f"*{_SPOOL_SUFFIX}"))
        except OSError:
            return pending, discarded
        now = time.time()
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.remove(path)
                discarded.append(("", "corrupt"))
                continue
            record = PendingDelivery.from_dict(raw)
            if record is None:
                self.remove(path)
                discarded.append(("", "bad_shape"))
                continue
            if now - record.created_at > self._max_age:
                self.remove(path)
                discarded.append((record.delivery, "expired"))
                continue
            if record.attempts > self._max_attempts:
                self.remove(path)
                discarded.append((record.delivery, "max_attempts"))
                continue
            pending.append((path, record))
        return pending, discarded


@dataclass(frozen=True)
class _QueueItem:
    """队列里的一个投递单元：一条记录 + 它此刻对应的接收端。"""

    record: PendingDelivery
    target: WebhookTarget
    path: Path | None = None
    replayed: bool = False


class WebhookDispatcher:
    """事件 → POST 的后台投递器。

    ``sink`` 是投递结果的出口（形如 ``ledger.emit``），只在**主线程**被调用，
    因此 ``sink`` 自身不需要考虑线程安全。``spool_dir`` 给定时启用 R15 的
    持久化重放；不给定时行为与 R14 逐字一致。
    """

    def __init__(
        self,
        config: WebhookConfig,
        *,
        sink: Callable[[str, dict[str, Any]], None] | None = None,
        spool_dir: Path | str | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self._config = config
        self._sink = sink
        self._queue: queue.Queue[_QueueItem | None] = queue.Queue()
        self._results: queue.Queue[dict[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._spool = (
            WebhookSpool(spool_dir, secrets=tuple(secrets))
            if spool_dir is not None else None
        )
        #: 便于测试与收尾汇总的计数（只在主线程读）。
        self.delivered = 0
        self.failed = 0
        if config.enabled:
            self._thread = threading.Thread(
                target=self._run, name="agentcrew-webhook", daemon=True)
            self._thread.start()
            self._replay_pending()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def spool_enabled(self) -> bool:
        return self._spool is not None

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        """把一条事件交给后台投递；未启用 / 不订阅此事件时什么都不做。

        先 drain 上一批投递结果——这样「上一次失败」能在下一个事件点上及时落账，
        而不必等到 run 收尾。R15 起在**入队前**先落盘：即使进程立刻被杀，
        未投出的通知也留在 spool 里等下次补投。
        """
        self._drain()
        if self._closed or not self._config.enabled:
            return
        data = dict(payload or {})
        # 入队前先按接收端过滤：没有任何接收端想要这条事件时，不必占用队列。
        if not self._config.matches(event, data):
            return
        delivery = uuid.uuid4().hex
        # 时间戳只取一次：body 里写它、签名也用它。两处各取一次会在跨秒时
        # 对不上，接收方校验 body.timestamp == header.timestamp 就会失败。
        timestamp = str(int(time.time()))
        created_at = time.time()
        for target in self._config.resolved():
            if not target.matches(event, data):
                continue
            record = PendingDelivery(
                event=event, payload=data, delivery=delivery,
                timestamp=timestamp, target_key=_target_key(target.url),
                created_at=created_at,
            )
            # delivery id 在所有接收端之间共享（它是「这次事件」的标识）；
            # 每个接收端独立重试、独立记一笔账。
            path = self._spool.add(record) if self._spool is not None else None
            self._queue.put(_QueueItem(record, target, path))

    def close(self, *, timeout: float = DEFAULT_CLOSE_TIMEOUT) -> None:
        """停止接收新事件、等队列排空（最多 ``timeout`` 秒），并落最后一批结果。

        超时不是错误：记一条 ``shutdown_timeout`` 就放手。R15 起超时的部分不会
        丢——记录已在 spool 里，下次启动会补投。
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

    # ---- 持久化重放（R15）----

    def _replay_pending(self) -> None:
        """把上次没投出去的记录重新放进本进程的投递队列。

        用 ``target_key`` 而非名字/URL 匹配：配置里改了地址就当作「不再是同一个
        接收端」，丢弃记录而不是把旧事件发到新地址。spool 未启用时零开销。
        """
        if self._spool is None or not self._config.enabled:
            return
        pending, discarded = self._spool.load()
        targets = {_target_key(target.url): target
                   for target in self._config.resolved()}
        for delivery, reason in discarded:
            self._record({
                "ok": False, "webhook_event": "*", "delivery": delivery,
                "attempts": 0, "error": f"replay_discarded: {reason}",
                "replayed": True,
            })
        for path, record in pending:
            target = targets.get(record.target_key)
            if target is None:
                self._spool.remove(path)
                self._record({
                    "ok": False, "webhook_event": record.event,
                    "delivery": record.delivery, "attempts": record.attempts,
                    "error": "replay_target_missing", "replayed": True,
                })
                continue
            self._queue.put(_QueueItem(record, target, path, replayed=True))

    # ---- 后台线程 ----

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._deliver_one(item)

    def _deliver_one(self, item: _QueueItem) -> None:
        """投给一个接收端，重试 ``max_retries`` 次；成功删 spool，失败留给下次。"""
        record, target = item.record, item.target
        label = _target_label(target)
        body = self._encode(record.event, record.payload, record.timestamp)
        last_error = ""
        for attempt in range(self._config.max_retries + 1):
            ok, detail = self._post(
                target, record.event, record.delivery, record.timestamp, body)
            if ok:
                result: dict[str, Any] = {
                    "ok": True, "webhook_event": record.event,
                    "target": label, "delivery": record.delivery,
                    "attempts": attempt + 1,
                }
                if item.replayed:
                    result["replayed"] = True
                self._record(result)
                if item.path is not None and self._spool is not None:
                    self._spool.remove(item.path)
                return
            last_error = detail
            if attempt < self._config.max_retries:
                time.sleep(min(0.2 * (2 ** attempt), _MAX_BACKOFF_SECONDS))
        result = {
            "ok": False, "webhook_event": record.event, "target": label,
            "delivery": record.delivery,
            "attempts": self._config.max_retries + 1,
            "error": last_error,
        }
        if item.replayed:
            result["replayed"] = True
        self._record(result)
        if item.path is not None and self._spool is not None:
            self._spool.retry(item.path, record)

    def _encode(self, event: str, payload: dict[str, Any], timestamp: str) -> bytes:
        """组装 POST body：事件名 + 时间戳 + 原始 payload。"""
        envelope = {
            "event": event,
            "timestamp": int(timestamp),
            "data": payload,
        }
        return json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    def _post(
        self,
        target: WebhookTarget,
        event: str,
        delivery: str,
        timestamp: str,
        body: bytes,
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
        # 每个接收端用自己的密钥：多接收端下共用一个 secret 等于把凭证发给所有人。
        if target.secret:
            headers[SIGNATURE_HEADER] = _sign(
                target.secret, timestamp, body)
        request = Request(
            target.url, data=body, headers=headers, method="POST")
        try:
            # 不跟随重定向（_NoRedirect）：3xx 视为失败进正常重试，
            # 避免「可信公网端点 302 → 任意地址」的放大面。
            with _OPENER.open(
                    request, timeout=self._config.timeout_seconds) as response:
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


def _target_label(target: WebhookTarget) -> str:
    """接收端在台账里的名字：优先显式 ``name``，否则用 ``scheme://host``。

    绝不写完整 URL——查询串里可能有 token，落进 ledger 就是泄露。
    """
    if target.name:
        return target.name
    try:
        parts = urlsplit(target.url)
    except ValueError:
        return "target"
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else "target"
