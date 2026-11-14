"""C10 切片一：run 中途指引的持久化存储。

一场 run 开跑后，人此前只能看或取消，不能补一句约束。本模块把「人话」变成
``<run_dir>/guidance.jsonl`` 里的可审计记录：引擎在**下一次任务派发前**读取并吸收，
不打断在跑任务、不改调度顺序、不新增调度分支。

三个设计决定（与 ``approvals.py`` 同族，不是新花样）：

1. **追加写 + 归并读**。一行一条 ``{"op": ...}``：``add`` 落一条指引，``consume``
   落一次吸收。读取时按 id 归并。为什么不是「一行一条记录、吸收时改 ``consumed_at``」：
   改字段要重写整个文件，多终端/API 并发写会互相覆盖；追加写 + ``O_APPEND`` 才是
   「进程在任何一行前后被杀都不会毁掉整份历史」。对外视图仍是 spec 的
   ``id / ts / author / text / consumed_at``。
2. **at-most-once**。``consume`` 只对仍未被吸收的 id 追加行，已消费再调返回 0；
   resume 后 ``consumed_at`` 已在盘上，不会二次注入。
3. **密钥不进盘**。写入前过 :func:`agentcrew.utils.logger.redact`，词表由调用方
   从 ``Settings.secret_values()`` 传入——``runtime`` 层不 import ``config``
   （``test_architecture.py`` 的分层护栏）。

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


@dataclass(frozen=True)
class GuidanceItem:
    """一条指引的归并视图（含消费状态）。"""

    id: str
    ts: str
    author: str
    text: str
    consumed_at: str | None = None
    #: 本条在**本次读取**中被 ``max_chars`` 截断（不落盘，仅用于事件留痕）。
    truncated: bool = False


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

    def append(self, text: str, author: str = "") -> GuidanceItem:
        """追加一条指引并返回它。空文本 / 纯空白直接拒绝。"""
        cleaned = redact((text or "").strip(), self._secrets)
        if not cleaned.strip():
            raise ValueError(EMPTY_TEXT_ERROR)
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
        )
        with self._lock:
            self._append({
                "op": "add",
                "id": item.id,
                "ts": item.ts,
                "author": item.author,
                "text": item.text,
            })
        if was_truncated:
            _logger.warning("指引超过存储上限 %d 字符，已截断 %s", MAX_TEXT_CHARS, item.id)
        return item

    def consume(self, ids: Sequence[str]) -> int:
        """把仍待吸收的 ``ids`` 标记为已吸收，返回**本次真正消费**的条数。

        幂等：已消费的 id 不再追加；重复调用返回 0。未知 id 忽略（不抛异常，
        避免一条脏数据卡住整场 run）。
        """
        wanted = {item for item in ids if item}
        if not wanted:
            return 0
        with self._lock:
            pending_ids = {
                item.id for item in self._load() if item.consumed_at is None
            }
            hit = [item for item in wanted if item in pending_ids]
            if not hit:
                return 0
            consumed_at = utc_now().isoformat()
            for item_id in hit:
                self._append({"op": "consume", "id": item_id,
                              "consumed_at": consumed_at})
            return len(hit)

    # ---- 读 ----

    def pending(self, *, limit: int, max_chars: int) -> list[GuidanceItem]:
        """待吸收的指引（落盘顺序，最多 ``limit`` 条，单条截到 ``max_chars``）。"""
        limit = max(1, int(limit))
        max_chars = max(1, int(max_chars))
        with self._lock:
            items = [item for item in self._load() if item.consumed_at is None]
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
            ))
        return result

    def pending_count(self) -> int:
        """待吸收条数（``status`` 与 ``GET`` 用）。"""
        return sum(1 for item in self.items() if item.consumed_at is None)

    def items(self) -> list[GuidanceItem]:
        """全部指引（含已消费，按落盘顺序），供 ``GET`` 与审计读取。"""
        with self._lock:
            return self._load()

    # ---- 内部 ----

    def _append(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
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
            item_id = payload.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            op = payload.get("op")
            if op == "add":
                text = payload.get("text")
                if not isinstance(text, str):
                    continue
                merged[item_id] = GuidanceItem(
                    id=item_id,
                    ts=str(payload.get("ts") or ""),
                    author=str(payload.get("author") or ""),
                    text=text,
                )
                order.append(item_id)
            elif op == "consume" and item_id in merged:
                current = merged[item_id]
                # 第一条 consume 胜出：重复消费行不改时间戳，审计保持确定。
                if current.consumed_at is None:
                    merged[item_id] = GuidanceItem(
                        id=current.id,
                        ts=current.ts,
                        author=current.author,
                        text=current.text,
                        consumed_at=str(payload.get("consumed_at") or ""),
                    )
        return [merged[item_id] for item_id in order]
