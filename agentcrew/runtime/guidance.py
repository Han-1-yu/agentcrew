"""C10：run 中途指引的持久化存储（切片一 + 切片二）。

一场 run 开跑后，人此前只能看或取消，不能补一句约束。本模块把「人话」变成
``<run_dir>/guidance.jsonl`` 里的可审计记录：引擎在**下一次任务派发前**读取并吸收，
不打断在跑任务、不改调度顺序、不新增调度分支。

四个设计决定（与 ``approvals.py`` 同族，不是新花样）：

1. **追加写 + 归并读**。一行一条 ``{"op": ...}``：``add`` 落一条指引，``consume``
   落一次吸收，``withdraw`` 落一次撤回，``snapshot`` 是历史压缩后的基线。
   读取时按 id 归并。为什么不是「一行一条记录、吸收时改 ``consumed_at``」：
   改字段要重写整个文件，多终端/API 并发写会互相覆盖；追加写 + ``O_APPEND`` 才是
   「进程在任何一行前后被杀都不会毁掉整份历史」。对外视图仍是 spec 的
   ``id / ts / author / text / consumed_at``，切片二追加 ``target`` / ``expires_at`` /
   ``withdrawn_at``。
2. **at-most-once**。``consume`` 只对仍未被吸收的 id 追加行，已消费再调返回 0；
   resume 后 ``consumed_at`` 已在盘上，不会二次注入。撤回、过期与已消费的 id
   一律不再参与吸收。
3. **密钥不进盘**。写入前过 :func:`agentcrew.utils.logger.redact`，词表由调用方
   从 ``Settings.secret_values()`` 传入——``runtime`` 层不 import ``config``
   （``test_architecture.py`` 的分层护栏）。
4. **压缩只在 quiescent 时做**。``compact()`` 用临时文件 + ``os.replace`` 重写，
   而追加写方每次都重新 ``open(..., "a")``；两者同时发生会把并发写丢到旧 inode。
   因此物理压缩由控制面在 **run 终态**才允许调用（见 ``_compact_guidance``），
   运行中的长会话靠 ``snapshot`` 语义归并、不重写文件。

失败语义：坏行跳过（与 ``Ledger`` 同口径）、写失败抛 ``OSError`` 由调用方降级，
本模块不吞异常——「指引失败不能弄停 run」是调用方的职责，不是存储层的。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentcrew.utils.clock import utc_now
from agentcrew.utils.files import read_text
from agentcrew.utils.logger import redact

_logger = logging.getLogger(__name__)

#: 指引文件名（相对 ``<run_dir>``）。
GUIDANCE_FILE = "guidance.jsonl"

#: 存储层单条文本硬上限（字符）。配置上限 ``max_guidance_chars`` 不得超过它。
MAX_TEXT_CHARS = 2000

#: 空文本/纯空白的拒绝文案（CLI 与 API 共用同一句，避免两处口径漂移）。
EMPTY_TEXT_ERROR = "指引文本不能为空"

#: 压缩时保留的最近「已终结」记录条数（消费 / 撤回 / 过期）。
COMPACT_KEEP_RECENT = 20

#: 终态封版时自动压缩的行数阈值。低于它时重写文件的收益不抵一次磁盘 IO；
#: 超过它说明这场 run 有过反复指引/撤回，值得在收尾时归并成一行 snapshot。
AUTO_COMPACT_LINE_THRESHOLD = 50

#: 定向任务 id 的长度上限（只是防止把一整段提示词塞进 target）。
MAX_TARGET_CHARS = 120


def _parse_ts(value: str | None) -> datetime | None:
    """把落盘的 ISO 时间戳解析成 aware UTC；坏值返回 ``None``（容错，不抛）。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_expired(item: GuidanceItem, now: datetime) -> bool:
    deadline = _parse_ts(item.expires_at)
    return deadline is not None and deadline <= now


def _is_active(item: GuidanceItem, now: datetime) -> bool:
    """是否仍可被吸收：未消费、未撤回、未过期。"""
    return (
        item.consumed_at is None
        and item.withdrawn_at is None
        and not _is_expired(item, now)
    )


@dataclass(frozen=True)
class GuidanceItem:
    """一条指引的归并视图（含消费 / 撤回 / 定向 / 过期状态）。"""

    id: str
    ts: str
    author: str
    text: str
    consumed_at: str | None = None
    #: 本条在**本次读取**中被 ``max_chars`` 截断（不落盘，仅用于事件留痕）。
    truncated: bool = False
    #: C10 切片二：只喂给这个任务；``None`` = 下一个派发的任务（切片一语义）。
    target: str | None = None
    #: C10 切片二：ISO UTC 过期时刻；到点后不再吸收（读时判定，不重写文件）。
    expires_at: str | None = None
    #: C10 切片二：撤回时刻；只能撤「还没被吸收」的指引。
    withdrawn_at: str | None = None

    def status(self, now: datetime | None = None) -> str:
        """对外展示用的状态（优先级：撤回 > 已吸收 > 过期 > 待吸收）。"""
        if self.withdrawn_at is not None:
            return "withdrawn"
        if self.consumed_at is not None:
            return "consumed"
        if _is_expired(self, now or utc_now()):
            return "expired"
        return "pending"


def _item_payload(item: GuidanceItem) -> dict[str, Any]:
    """``snapshot`` 行里的完整记录（不含只该在本次读取里出现的 ``truncated``）。"""
    return {
        "id": item.id,
        "ts": item.ts,
        "author": item.author,
        "text": item.text,
        "consumed_at": item.consumed_at,
        "target": item.target,
        "expires_at": item.expires_at,
        "withdrawn_at": item.withdrawn_at,
    }


def _item_from_payload(payload: dict[str, Any]) -> GuidanceItem | None:
    item_id = payload.get("id")
    text = payload.get("text")
    if not isinstance(item_id, str) or not item_id or not isinstance(text, str):
        return None

    def optional_str(key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) and value else None

    return GuidanceItem(
        id=item_id,
        ts=str(payload.get("ts") or ""),
        author=str(payload.get("author") or ""),
        text=text,
        consumed_at=optional_str("consumed_at"),
        target=optional_str("target"),
        expires_at=optional_str("expires_at"),
        withdrawn_at=optional_str("withdrawn_at"),
    )


class GuidanceStore:
    """``<run_dir>/guidance.jsonl`` 的追加写 / 归并读。"""

    def __init__(self, run_dir: Path, *, secrets: Sequence[str] = ()) -> None:
        self._path = Path(run_dir) / GUIDANCE_FILE
        self._secrets = tuple(item for item in secrets if item)
        # 与 ``approvals.py`` 同款进程内锁：读 pending → 追加 consume 是两步操作，
        # 并发下两个任务会同时看到同一条 pending，两边都注入。
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # ---- 写 ----

    def append(
        self,
        text: str,
        author: str = "",
        *,
        target: str | None = None,
        expires_at: str | None = None,
    ) -> GuidanceItem:
        """追加一条指引并返回它。空文本 / 纯空白、坏过期时间直接拒绝。

        ``target`` 为空串等价于不定向；``expires_at`` 必须是可解析的 ISO 时间戳，
        且不早于当前时刻（过了期的指引当场就废，没有意义）。
        """
        cleaned = redact((text or "").strip(), self._secrets)
        if not cleaned.strip():
            raise ValueError(EMPTY_TEXT_ERROR)
        cleaned_target = str(target).strip()[:MAX_TARGET_CHARS] if target else None
        cleaned_target = cleaned_target or None
        deadline: str | None = None
        if expires_at:
            parsed = _parse_ts(str(expires_at))
            if parsed is None:
                raise ValueError("expires_at 必须是 ISO 8601 时间戳")
            if parsed <= utc_now():
                raise ValueError("expires_at 必须晚于当前时刻")
            deadline = parsed.isoformat()
        was_truncated = len(cleaned) > MAX_TEXT_CHARS
        if was_truncated:
            cleaned = cleaned[: MAX_TEXT_CHARS - 1].rstrip() + "…"
        now = utc_now()
        item = GuidanceItem(
            # 时间戳 + 随机后缀：spec 的「时间戳-序号」是为了唯一，跨进程写时
            # 纯序号会撞（两个终端同一秒各写一条），随机后缀才真的唯一。
            id=f"GD-{now:%Y%m%dT%H%M%S}-{uuid4().hex[:6]}",
            ts=now.isoformat(),
            author=str(author or "").strip()[:80],
            text=cleaned,
            target=cleaned_target,
            expires_at=deadline,
        )
        with self._lock:
            self._append({"op": "add", **_item_payload(item)})
        if was_truncated:
            _logger.warning("指引超过存储上限 %d 字符，已截断 %s", MAX_TEXT_CHARS, item.id)
        return item

    def consume(self, ids: Sequence[str]) -> int:
        """把仍待吸收的 ``ids`` 标记为已吸收，返回**本次真正消费**的条数。

        幂等：已消费 / 已撤回 / 已过期 / 未知 id 都不再追加；重复调用返回 0。
        """
        wanted = {item for item in ids if item}
        if not wanted:
            return 0
        with self._lock:
            now = utc_now()
            pending_ids = {
                item.id for item in self._load() if _is_active(item, now)
            }
            hit = [item for item in wanted if item in pending_ids]
            if not hit:
                return 0
            consumed_at = now.isoformat()
            for item_id in hit:
                self._append({"op": "consume", "id": item_id,
                              "consumed_at": consumed_at})
            return len(hit)

    def withdraw(self, item_id: str) -> bool:
        """撤回一条**尚未被吸收**的指引；已吸收 / 已过期 / 不存在都返回 ``False``。

        撤回是追加一行而不是删记录：审计要能看见「曾经说过、后来撤了」。
        """
        if not item_id:
            return False
        with self._lock:
            now = utc_now()
            for item in self._load():
                if item.id == item_id and _is_active(item, now):
                    self._append({"op": "withdraw", "id": item_id,
                                  "withdrawn_at": now.isoformat()})
                    return True
        return False

    def compact(self, *, keep_recent: int = COMPACT_KEEP_RECENT) -> dict[str, int]:
        """把归并后的状态压成一行 ``snapshot``（物理压缩，返回前后行数与保留数）。

        **并发边界**：本方法用 ``os.replace`` 重写文件，只应由控制面在 run 终态
        （确认没有其他进程/线程还在追加）时调用；运行中的 run 请只用追加 op。
        保留全部「仍可能被吸收」的指引 + 最近 ``keep_recent`` 条已终结记录。
        """
        keep_recent = max(0, int(keep_recent))
        with self._lock:
            items = self._load()
            before_lines = self._line_count()
            now = utc_now()
            active = [item for item in items if _is_active(item, now)]
            finished = [item for item in items if not _is_active(item, now)]
            finished.sort(key=lambda item: item.ts, reverse=True)
            kept = active + list(reversed(finished[:keep_recent]))
            # 保持原有落盘顺序，压缩后 GET 的时间线不跳序。
            order = {item.id: index for index, item in enumerate(items)}
            kept.sort(key=lambda item: order[item.id])
            payload = {"op": "snapshot",
                       "items": [_item_payload(item) for item in kept]}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".compact-tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, self._path)
            self._chmod()
            return {
                "before_lines": before_lines,
                "after_lines": self._line_count(),
                "kept": len(kept),
                "removed": len(items) - len(kept),
            }

    # ---- 读 ----

    def pending(
        self,
        *,
        limit: int,
        max_chars: int,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> list[GuidanceItem]:
        """待吸收的指引（落盘顺序，最多 ``limit`` 条，单条截到 ``max_chars``）。

        ``task_id`` 非空时只返回「未定向 + 定向到该任务」的条目；``None`` 表示
        不过滤（``pending_count`` 与切片一的调用口径）。撤回 / 过期项被排除，
        但**不重写文件**——它们留在审计里，只是不再占位、不再被吸收。
        """
        limit = max(1, int(limit))
        max_chars = max(1, int(max_chars))
        moment = now or utc_now()
        with self._lock:
            items = [item for item in self._load() if _is_active(item, moment)]
        if task_id is not None:
            items = [item for item in items
                     if item.target is None or item.target == task_id]
        selected = items[:limit]
        result: list[GuidanceItem] = []
        for item in selected:
            if len(item.text) <= max_chars:
                result.append(item)
                continue
            result.append(GuidanceItem(
                id=item.id,
                ts=item.ts,
                author=item.author,
                text=item.text[: max_chars - 1].rstrip() + "…",
                consumed_at=item.consumed_at,
                truncated=True,
                target=item.target,
                expires_at=item.expires_at,
                withdrawn_at=item.withdrawn_at,
            ))
        return result

    def expired(self, *, now: datetime | None = None) -> list[GuidanceItem]:
        """已过期的指引（供引擎补 ``guidance_expired`` 事件，不落盘、不改状态）。"""
        moment = now or utc_now()
        with self._lock:
            return [
                item for item in self._load()
                if item.consumed_at is None
                and item.withdrawn_at is None
                and _is_expired(item, moment)
            ]

    def pending_count(self) -> int:
        """仍可能被吸收的条数（排除已撤回 / 已过期；``status`` 与配额用）。"""
        return sum(1 for _ in self.pending(limit=10**6, max_chars=MAX_TEXT_CHARS))

    def items(self) -> list[GuidanceItem]:
        """全部指引（含已消费 / 已撤回 / 已过期，按落盘顺序），供 ``GET`` 与审计。"""
        with self._lock:
            return self._load()

    # ---- 内部 ----

    def _line_count(self) -> int:
        if not self._path.is_file():
            return 0
        try:
            return sum(1 for line in read_text(self._path).splitlines() if line.strip())
        except OSError:
            return 0

    def _append(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
        self._chmod()

    def _chmod(self) -> None:
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            # Windows 上只落地只读位；权限收紧是尽力而为，不是语义。
            pass

    def _load(self) -> list[GuidanceItem]:
        if not self._path.is_file():
            return []
        try:
            lines = read_text(self._path).splitlines()
        except OSError as exc:
            _logger.warning("指引文件无法读取，按空库处理 %s: %s", self._path, exc)
            return []
        merged: dict[str, GuidanceItem] = {}
        order: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                _logger.warning("跳过无法解析的指引行 %s", self._path)
                continue
            if not isinstance(payload, dict):
                continue
            op = payload.get("op")
            if op == "snapshot":
                # 压缩基线：此前所有行都已归并进 items，从这里重新起算。
                merged, order = {}, []
                snapshot = payload.get("items")
                if not isinstance(snapshot, list):
                    continue
                for raw in snapshot:
                    if not isinstance(raw, dict):
                        continue
                    item = _item_from_payload(raw)
                    if item is not None:
                        merged[item.id] = item
                        order.append(item.id)
                continue
            item_id = payload.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            if op == "add":
                item = _item_from_payload(payload)
                if item is None:
                    continue
                merged[item_id] = item
                order.append(item_id)
            elif op == "consume" and item_id in merged:
                current = merged[item_id]
                # 第一条 consume 胜出：重复消费行不改时间戳，审计保持确定。
                if current.consumed_at is None:
                    merged[item_id] = GuidanceItem(
                        id=current.id, ts=current.ts, author=current.author,
                        text=current.text,
                        consumed_at=str(payload.get("consumed_at") or ""),
                        target=current.target, expires_at=current.expires_at,
                        withdrawn_at=current.withdrawn_at,
                    )
            elif op == "withdraw" and item_id in merged:
                current = merged[item_id]
                if current.withdrawn_at is None:
                    merged[item_id] = GuidanceItem(
                        id=current.id, ts=current.ts, author=current.author,
                        text=current.text, consumed_at=current.consumed_at,
                        target=current.target, expires_at=current.expires_at,
                        withdrawn_at=str(payload.get("withdrawn_at") or ""),
                    )
        return [merged[item_id] for item_id in order]
