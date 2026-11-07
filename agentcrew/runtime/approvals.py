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
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.utils.files import read_text

_logger = logging.getLogger(__name__)

#: 审批单文件名（相对 ``<run_dir>``）。
APPROVAL_FILE = "approvals.jsonl"

#: 两个卡点各自允许的人工裁决。收紧在存储层，外部通道乱传不会污染引擎。
PLAN_ACTIONS = frozenset(("approve", "cancel", "save_exit"))
DELIVERY_ACTIONS = frozenset(("accept", "abort", "rework"))

ApprovalCheckpoint = Literal["plan", "delivery"]
ApprovalStatus = Literal["pending", "decided", "consumed"]


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


def _validate_decision(checkpoint: str, decision: Any) -> dict[str, Any]:
    """校验外部写入的裁决形状；报错不回显整个 payload。"""
    if not isinstance(decision, dict):
        raise ApprovalError("decision 必须是 JSON 对象")
    action = decision.get("action")
    allowed = PLAN_ACTIONS if checkpoint == "plan" else DELIVERY_ACTIONS
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
    return dict(decision)


class ApprovalStore:
    """``<run_dir>/approvals.jsonl`` 的追加写 / 归并读。"""

    def __init__(self, run_dir: Path) -> None:
        self._path = Path(run_dir) / APPROVAL_FILE

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
    ) -> ApprovalRequest:
        """创建一张 ``pending`` 审批单并落盘。"""
        request = ApprovalRequest(
            id="AP-" + uuid4().hex[:12],
            run_id=run_id or self._path.parent.name,
            checkpoint=checkpoint,
            context=context or {},
            created_at=datetime.now(),
        )
        self._append("create", **request.model_dump(mode="json"))
        return request

    def decide(
        self, request_id: str, decision: dict[str, Any], *, decided_by: str = ""
    ) -> ApprovalRequest:
        """对一张 ``pending`` 审批单写入裁决（幂等：已决 / 已消费一律拒绝）。"""
        request = self.get(request_id)
        if request is None:
            raise ApprovalError(f"审批单不存在：{request_id}")
        if request.status != "pending":
            raise ApprovalError(
                f"审批单 {request_id} 已 {request.status}，不能重复裁决"
            )
        payload = _validate_decision(request.checkpoint, decision)
        now = datetime.now().isoformat()
        self._append(
            "decide",
            id=request_id,
            decision=payload,
            decided_by=str(decided_by)[:80],
            decided_at=now,
        )
        decided = self.get(request_id)
        assert decided is not None  # 刚写入的行一定能读回
        return decided

    def consume(self, request_id: str) -> ApprovalRequest:
        """标记裁决已被引擎消费；后续 resume 不会再重复执行这张单。"""
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
                    merged[request_id] = merged[request_id].model_copy(update={
                        "status": "decided",
                        "decision": payload.get("decision") or {},
                        "decided_by": str(payload.get("decided_by") or ""),
                        "decided_at": datetime.fromisoformat(payload["decided_at"])
                        if payload.get("decided_at") else None,
                    })
                elif op == "consume" and request_id in merged:
                    merged[request_id] = merged[request_id].model_copy(update={
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
