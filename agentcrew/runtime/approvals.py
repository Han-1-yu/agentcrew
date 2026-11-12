"""持久化人工审批请求（R4：durable human-in-the-loop）。

此前「人工闸门」只是一次 **同步 ``input()``**：``CliPrompter`` 在 TTY 里问一句、
阻塞整个协程栈直到回答；非 TTY 直接降级为全自动，HTTP API 更是硬编码
``gate="none"``。也就是说决策权根本交不出去，run 也没法「等几天再继续」。

本模块把人工决策变成**一等公民的可持久化实体**：

- 一条审批请求是 ``<run_dir>/approvals.jsonl`` 里的若干行事件（``create`` →
  ``decide`` → ``consume``），追加写、读取时按 id 归并。与 ledger / decisions
  同款「写入严、读取宽」，进程在任何一行前后被杀都不会毁掉整份历史。
- 三个状态：``pending``（等人）→ ``decided``（人已裁决、尚未被引擎消费）→
  ``consumed``（引擎已据此继续）。``consume`` 这一步是幂等恢复的关键：没有它，
  每次 resume 都会把同一张审批单重复执行一遍。
- 引擎侧（:class:`~agentcrew.gate.deferred.DeferredPrompter`）看到 ``pending``
  就抛 :class:`ApprovalPending` 挂起；看到 ``decided`` 就消费并转成
  ``PlanDecision`` / ``DeliveryDecision`` 继续执行。
- 人这一侧（CLI / HTTP API / Studio）只需要 ``decide``，不需要碰引擎状态。

分层：位于 ``runtime`` 层，只依赖 ``utils.files`` 与 pydantic，不 import
``gate`` / ``crew`` / ``api``（``test_architecture.py`` 的分层护栏）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.utils.files import read_text

from ..utils.clock import utc_now

_logger = logging.getLogger(__name__)

#: 审批单文件名（相对 ``<run_dir>``）。
APPROVAL_FILE = "approvals.jsonl"

#: 各卡点允许的人工裁决。收紧在存储层，外部通道乱传不会污染引擎。
PLAN_ACTIONS = frozenset(("approve", "cancel", "save_exit"))
DELIVERY_ACTIONS = frozenset(("accept", "abort", "rework"))
#: R20 单步断点：``step`` 放行断点上的那个任务；``run`` 解除断点一路跑到终局；
#: ``abort`` 取消整场 run（语义与 plan 卡点的 ``cancel`` 相同）。
STEP_ACTIONS = frozenset(("step", "run", "abort"))

ApprovalCheckpoint = Literal["plan", "delivery", "step"]
ApprovalStatus = Literal["pending", "decided", "consumed", "expired"]


class ApprovalError(ValueError):
    """审批单不存在 / 状态不允许 / 裁决形状非法。"""


class ApprovalPending(RuntimeError):
    """引擎侧挂起信号：审批单已创建但仍未决，run 应落 ``awaiting_approval``。

    这是**内部控制流信号**，不是失败：``Crew._start`` 显式捕获它并走
    ``_suspend``，绝不能落到「兜底封版为 FAILED」的异常分支。
    """

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__(
            f"审批请求 {request.id}（{request.checkpoint}）等待人工裁决"
        )
        self.request = request


class ApprovalRequest(BaseModel):
    """一张审批单归并后的状态。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str = ""
    checkpoint: ApprovalCheckpoint
    status: ApprovalStatus = "pending"
    context: dict[str, Any] = Field(default_factory=dict)
    """给审批人看的上下文（任务 id 列表、目标摘要等），不含密钥。"""
    decision: dict[str, Any] = Field(default_factory=dict)
    """人工裁决载荷：``{"action": ..., "task_ids": [...]}``。"""
    decided_by: str = ""
    created_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    expires_at: datetime | None = None
    """R30：超过这个时刻仍未裁决 → 视为过期（``status="expired"``）。

    ``None`` = 不设超时（``approval_timeout_seconds=0`` 时的默认行为），
    与 R30 之前逐字一致。
    """
    expired_at: datetime | None = None


def _validate_decision(checkpoint: str, decision: Any) -> dict[str, Any]:
    """校验外部写入的裁决形状；报错不回显整个 payload。"""
    if not isinstance(decision, dict):
        raise ApprovalError("decision 必须是 JSON 对象")
    action = decision.get("action")
    if checkpoint == "plan":
        allowed = PLAN_ACTIONS
    elif checkpoint == "delivery":
        allowed = DELIVERY_ACTIONS
    else:
        allowed = STEP_ACTIONS
    if action not in allowed:
        raise ApprovalError(
            f"{checkpoint} 卡点的 action 必须是 {'、'.join(sorted(allowed))} 之一"
        )
    if action == "rework":
        ids = decision.get("task_ids")
        if not isinstance(ids, list) or not ids or not all(
            isinstance(item, str) and item for item in ids
        ):
            raise ApprovalError("rework 必须带非空的 task_ids 字符串数组")
    # B-22：只保留契约字段。此前 ``return dict(decision)`` 把调用方塞进来的
    # 任意键原样落盘并在 ``GET /approvals`` 回显——未定义字段会污染审计记录，
    # 也能借道给前端/日志注入任意内容。下游只消费 ``action`` 与 ``task_ids``。
    clean: dict[str, Any] = {"action": action}
    if action == "rework":
        clean["task_ids"] = list(decision["task_ids"])
    return clean


class ApprovalStore:
    """``<run_dir>/approvals.jsonl`` 的追加写 / 归并读。"""

    def __init__(self, run_dir: Path) -> None:
        self._path = Path(run_dir) / APPROVAL_FILE
        # B-18：裁决是「读 pending → 追加 decide」的两步操作，中间必须串行。
        # asyncio 单线程下这两步之间没有 await 看似安全，但 CLI 线程、
        # serve 的线程池、测试里的并发调用都会走到同一个 store 实例：两个裁决
        # 都能通过 ``status == "pending"`` 检查，归并时后写覆盖先写，
        # 「一张单只能被裁决一次」只在串行下成立。
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _append(self, op: str, **data: Any) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"op": op, **data}, ensure_ascii=False, default=str
        ) + "\n"
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

    def create(
        self,
        checkpoint: ApprovalCheckpoint,
        *,
        context: dict[str, Any] | None = None,
        run_id: str = "",
        timeout_seconds: int = 0,
    ) -> ApprovalRequest:
        """创建一张 ``pending`` 审批单并落盘。

        R30：``timeout_seconds > 0`` 时写入 ``expires_at``——由
        :meth:`expire_overdue` 在 status / serve 启动时判定过期。
        """
        # D-14：created_at / expires_at 统一 aware UTC，与 decided_at 同口径。
        now = utc_now()
        expires_at = (
            now + timedelta(seconds=int(timeout_seconds))
            if int(timeout_seconds or 0) > 0 else None
        )
        request = ApprovalRequest(
            id="AP-" + uuid4().hex[:12],
            run_id=run_id or self._path.parent.name,
            checkpoint=checkpoint,
            context=context or {},
            created_at=now,
            expires_at=expires_at,
        )
        self._append("create", **request.model_dump(mode="json"))
        return request

    def decide(
        self, request_id: str, decision: dict[str, Any], *, decided_by: str = ""
    ) -> ApprovalRequest:
        """对一张 ``pending`` 审批单写入裁决（幂等：已决 / 已消费一律拒绝）。"""
        # B-18：整个「读 → 校验 → 追加 → 复核」序列在同一把锁里完成。
        with self._lock:
            request = self.get(request_id)
            if request is None:
                raise ApprovalError(f"审批单不存在：{request_id}")
            # R30：过期排在通用状态检查之前——「已过期」要给出可操作提示，
            # 而不是一句「已 expired」把人晾在那儿。
            if request.status == "expired" or self.is_expired(request):
                raise ApprovalError(
                    f"审批单 {request_id} 已过期（{request.expires_at}），"
                    "请先重新触发该卡点或调大 approval_timeout_seconds"
                )
            if request.status != "pending":
                raise ApprovalError(
                    f"审批单 {request_id} 已 {request.status}，不能重复裁决"
                )
            payload = _validate_decision(request.checkpoint, decision)
            decided_at = utc_now()
            self._append(
                "decide",
                id=request_id,
                decision=payload,
                decided_by=str(decided_by)[:80],
                decided_at=decided_at.isoformat(),
            )
            decided = self.get(request_id)
            assert decided is not None  # 刚写入的行一定能读回
            # B-18：进程锁管不到另一个进程（serve + CLI 同时操作同一 run）。
            # 落盘后复核「生效的是不是我这一条」：不是就明确报冲突，而不是
            # 让调用方以为自己的裁决生效了。
            if decided.decided_at != decided_at:
                raise ApprovalError(
                    f"审批单 {request_id} 已被另一个进程抢先裁决"
                    f"（{decided.decided_by or '未知'}），本次裁决未生效；"
                    "请重新读取审批单状态"
                )
            return decided

    def consume(self, request_id: str) -> ApprovalRequest:
        """标记裁决已被引擎消费；后续 resume 不会再重复执行这张单。"""
        # B-18：与 decide 同一把锁——读状态 + 追加 consume 也是两步操作，
        # 并发下会重复追加同一条消费记录（虽幂等，但审计里会出现两次）。
        with self._lock:
            return self._consume_locked(request_id)

    def _consume_locked(self, request_id: str) -> ApprovalRequest:
        request = self.get(request_id)
        if request is None:
            raise ApprovalError(f"审批单不存在：{request_id}")
        if request.status == "consumed":
            return request
        if request.status != "decided":
            raise ApprovalError(
                f"审批单 {request_id} 尚未裁决，不能消费"
            )
        self._append(
            "consume", id=request_id, consumed_at=datetime.now().isoformat()
        )
        consumed = self.get(request_id)
        assert consumed is not None
        return consumed

    def all(self) -> list[ApprovalRequest]:
        """读取全部审批单（按创建时间升序）；文件不存在 = 空库，坏行跳过。"""
        if not self._path.is_file():
            return []
        merged: dict[str, ApprovalRequest] = {}
        order: list[str] = []
        try:
            lines = read_text(self._path).splitlines()
        except OSError as exc:
            _logger.warning("审批单文件无法读取，按空库处理 %s: %s", self._path, exc)
            return []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                _logger.warning("跳过无法解析的审批单行 %s", self._path)
                continue
            if not isinstance(payload, dict):
                continue
            op = payload.get("op")
            request_id = payload.get("id")
            if not isinstance(request_id, str):
                continue
            try:
                if op == "create":
                    # create 行带一个额外的 "op" 键，模型是 extra="forbid"，
                    # 读回时必须先摘掉它。
                    body = {k: v for k, v in payload.items() if k != "op"}
                    merged[request_id] = ApprovalRequest.model_validate(body)
                    order.append(request_id)
                elif op == "decide" and request_id in merged:
                    # B-18：并发/重复裁决时**第一条胜出**。此前后写覆盖先写，
                    # 两个矛盾裁决都被接受且以最后一条为准。这里已 decided 的
                    # 单忽略后续 decide 行（consume 同理），保证裁决是确定的。
                    current = merged[request_id]
                    if current.status == "decided":
                        _logger.warning(
                            "审批单 %s 重复裁决，保留第一条（后写 %s 忽略）",
                            request_id, payload.get("decided_by") or "")
                        continue
                    merged[request_id] = current.model_copy(update={
                        "status": "decided",
                        "decision": payload.get("decision") or {},
                        "decided_by": str(payload.get("decided_by") or ""),
                        "decided_at": datetime.fromisoformat(payload["decided_at"])
                        if payload.get("decided_at") else None,
                    })
                elif op == "expire" and request_id in merged:
                    merged[request_id] = merged[request_id].model_copy(update={
                        "status": "expired",
                        "expired_at": datetime.fromisoformat(payload["expired_at"])
                        if payload.get("expired_at") else None,
                    })
                elif op == "consume" and request_id in merged:
                    current = merged[request_id]
                    if current.status == "consumed":
                        continue
                    merged[request_id] = current.model_copy(update={
                        "status": "consumed",
                        "consumed_at": datetime.fromisoformat(payload["consumed_at"])
                        if payload.get("consumed_at") else None,
                    })
            except (ValueError, TypeError, KeyError) as exc:
                _logger.warning("跳过结构未知的审批单行 %s: %s", self._path, exc)
                continue
        return [merged[key] for key in order if key in merged]

    def get(self, request_id: str) -> ApprovalRequest | None:
        return next(
            (item for item in self.all() if item.id == request_id), None
        )

    def pending(self) -> list[ApprovalRequest]:
        """所有仍在等人的审批单。"""
        return [item for item in self.all() if item.status == "pending"]

    def is_expired(self, request: ApprovalRequest,
                   *, now: datetime | None = None) -> bool:
        """这张单是否已过等待窗口（``expires_at`` 为空 = 永不超时）。

        D-14：``expires_at`` 新写入是 aware UTC，存量是 naive 本地时间——
        直接比较会 ``TypeError``。两边都归一成 aware 再比，两种数据都能判。
        """
        if request.expires_at is None or request.status != "pending":
            return False
        moment = now or utc_now()
        if moment.tzinfo is None:
            moment = moment.astimezone()
        expiry = request.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.astimezone()
        return moment >= expiry

    def expire_overdue(self, *, now: datetime | None = None) -> list[ApprovalRequest]:
        """把**已过期且仍 pending** 的单标记为 ``expired``，返回本次过期的单。

        调用点：``status`` 查询与 ``serve`` 启动 reconcile。过期不改 run 结局——
        run 仍是 ``interrupted``，只是审批人再裁决会收到「已过期」而不是静默
        接受一张早已失去时效的裁决。
        """
        moment = now or utc_now()
        expired: list[ApprovalRequest] = []
        for request in self.pending():
            if not self.is_expired(request, now=moment):
                continue
            self._append("expire", id=request.id,
                         expired_at=moment.isoformat())
            refreshed = self.get(request.id)
            if refreshed is not None:
                expired.append(refreshed)
        return expired

    def open_for(self, checkpoint: ApprovalCheckpoint) -> ApprovalRequest | None:
        """该卡点上最新一张**尚未被引擎消费**的审批单（pending 或 decided）。

        ``ask_*`` 用它决定：已有裁决 → 消费并继续；仍 pending → 复用同一张单
        再次挂起（不重复创建）；都没有 → 新建一张。
        """
        candidates = [
            item for item in self.all()
            if item.checkpoint == checkpoint and item.status != "consumed"
        ]
        return candidates[-1] if candidates else None

    def awaiting_checkpoint(self) -> ApprovalCheckpoint | None:
        """整场 run 当前挂在哪个卡点上；没有未消费审批单时返回 ``None``。

        ``Crew.resume_from`` 用它决定 resume 要重入计划卡点还是交付卡点。
        """
        candidates = [
            item for item in self.all() if item.status != "consumed"
        ]
        return candidates[-1].checkpoint if candidates else None

    def consumed_steps(self) -> tuple[frozenset[str], str | None]:
        """R20：汇总已消费的 step 断点裁决。

        返回 ``(已放行的 task_id 集合, 终局指令)``。``task_id`` 取自各审批单的
        ``context``（``decide`` 只存 action，任务 id 在建单时写进 context）；
        终局指令是已消费裁决里出现过的 ``"run"`` / ``"abort"``（两者都是
        「不再逐个断点」语义），一个都没有时为 ``None``。``status != "consumed"``
        的单不算——裁决必须先被引擎消费才生效，与 plan/delivery 同一条纪律。
        """
        task_ids: set[str] = set()
        final: str | None = None
        for item in self.all():
            if item.checkpoint != "step" or item.status != "consumed":
                continue
            task_id = item.context.get("task_id")
            if isinstance(task_id, str) and task_id:
                task_ids.add(task_id)
            action = item.decision.get("action")
            if action in ("run", "abort"):
                final = str(action)
        return frozenset(task_ids), final
