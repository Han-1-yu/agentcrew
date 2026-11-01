"""会话亲和性追踪（V7-P3，借鉴 magpie internal/gateway/affinity.go）。

当配置了多 key / 多模型池时，让同一会话的对话保持在同一个别名上，利用厂商的
prompt 缓存（如 OpenAI 的 prompt cache、Anthropic 的 cache_control）降低延迟和成本。

设计要点：

- **三种模式**：
  - ``session``：整个会话固定一个别名，直到该别名不可用才切换；
  - ``turn``：每轮对话可重新选择（等价于关闭亲和性，但保留追踪能力）；
  - ``off``（默认）：完全关闭，行为与改造前一致。
- **健康感知**：亲和性别名不可用时自动回退到正常路由策略，不阻塞调用。
- **持久化**：亲和性映射持久化到 JSON，跨 run 保留（可选）。
- **零侵入**：``off`` 模式下所有方法为恒等操作，性能开销可忽略。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 亲和性模式
AFFINITY_OFF = "off"
AFFINITY_SESSION = "session"
AFFINITY_TURN = "turn"

VALID_MODES = (AFFINITY_OFF, AFFINITY_SESSION, AFFINITY_TURN)

#: 厂商 prompt 缓存失效时间（借鉴 magpie cacheCold = 5 分钟）。
#: 超过此时间未使用的亲和性记录视为缓存已冷，不再强制保持。
CACHE_COLD_SECONDS = 5 * 60

#: 最多保留的亲和性记录数（借鉴 magpie sticksKept = 512）。
MAX_RECORDS = 512


@dataclass
class AffinityRecord:
    """单条亲和性记录。"""

    alias: str
    """当前绑定的别名。"""
    session_id: str
    """会话标识。"""
    last_used: float = 0.0
    """最后使用时间（epoch seconds）。"""
    switch_count: int = 0
    """因不可用而切换的次数（用于诊断）。"""


class AffinityTracker:
    """会话亲和性追踪器。

    维护 ``session_id → AffinityRecord`` 映射，持久化到 JSON 文件。
    线程不安全，但在 asyncio 单线程模型下无需加锁。
    """

    def __init__(
        self,
        mode: str = AFFINITY_OFF,
        state_path: Path | None = None,
        *,
        ttl_seconds: int = 86400 * 7,  # 7 天
    ) -> None:
        if mode not in VALID_MODES:
            raise ValueError(
                f"未知亲和性模式 {mode!r}，有效值: {VALID_MODES}")
        self._mode = mode
        self._state_path = Path(state_path) if state_path else None
        self._ttl = ttl_seconds
        # session_id → AffinityRecord
        self._records: dict[str, AffinityRecord] = {}
        self._loaded = False

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def enabled(self) -> bool:
        return self._mode != AFFINITY_OFF

    def _load(self) -> None:
        """从磁盘加载亲和性映射。off 模式下不加载。"""
        if not self.enabled or self._loaded or self._state_path is None:
            self._loaded = True
            return
        if not self._state_path.exists():
            self._loaded = True
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            now = time.time()
            for sid, rec in data.get("records", {}).items():
                last_used = rec.get("last_used", 0.0)
                # 过期记录不加载
                if now - last_used > self._ttl:
                    continue
                self._records[sid] = AffinityRecord(
                    alias=rec["alias"],
                    session_id=sid,
                    last_used=last_used,
                    switch_count=rec.get("switch_count", 0),
                )
        except (json.JSONDecodeError, OSError, KeyError):
            pass
        self._loaded = True

    def _save(self) -> None:
        """持久化亲和性映射到磁盘。"""
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data: dict[str, Any] = {
                "records": {
                    sid: {
                        "alias": rec.alias,
                        "last_used": rec.last_used,
                        "switch_count": rec.switch_count,
                    }
                    for sid, rec in self._records.items()
                },
                "saved_at": time.time(),
                "mode": self._mode,
            }
            self._state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def resolve(
        self,
        primary: str,
        session_id: str | None,
        candidates: list[str] | None = None,
        *,
        is_available: Callable[[str], bool] | None = None,
    ) -> str:
        """解析本次调用应使用的别名。

        Args:
            primary: 主别名（无亲和性记录或回退时使用）。
            session_id: 会话标识。``None`` 时直接返回 primary。
            candidates: 候选别名列表（failover 池）。用于验证亲和性别名是否在池中。
            is_available: 可选的可用性检查函数，返回 False 时亲和性别名被视为不可用。

        Returns:
            应使用的别名。
        """
        if not self.enabled or session_id is None:
            return primary

        self._load()
        record = self._records.get(session_id)

        if record is None:
            return primary

        # turn 模式：每轮都允许重新选择，不强制亲和
        if self._mode == AFFINITY_TURN:
            return primary

        # session 模式：坚持使用已绑定的别名
        bound_alias = record.alias

        # V7 质量修复：cacheCold —— 超过 5 分钟未使用的亲和性视为缓存已冷，
        # 不再强制保持（借鉴 magpie cacheCold = 5 * time.Minute）
        if time.time() - record.last_used > CACHE_COLD_SECONDS:
            return primary

        # 检查亲和性别名是否在候选池中（如果提供了 candidates）
        if candidates is not None and bound_alias not in candidates:
            return primary

        # 检查可用性
        if is_available is not None and not is_available(bound_alias):
            return primary  # 不可用，回退到正常路由

        return bound_alias

    def remember(
        self,
        session_id: str | None,
        alias: str,
        *,
        force: bool = False,
    ) -> None:
        """记录本次调用使用的别名，建立/更新亲和性。

        Args:
            session_id: 会话标识。``None`` 时不记录。
            alias: 实际使用的别名。
            force: 即使已有记录也强制更新（用于别名切换后）。
        """
        if not self.enabled or session_id is None:
            return

        self._load()
        now = time.time()
        existing = self._records.get(session_id)

        if existing is not None and existing.alias == alias and not force:
            existing.last_used = now
        else:
            switch_count = existing.switch_count + 1 if existing else 0
            self._records[session_id] = AffinityRecord(
                alias=alias,
                session_id=session_id,
                last_used=now,
                switch_count=switch_count,
            )
        # V7 质量修复：清理过期记录并限制最大记录数（借鉴 magpie sticksKept=512）
        expired = [
            sid for sid, rec in self._records.items()
            if now - rec.last_used > self._ttl
        ]
        for sid in expired:
            del self._records[sid]
        if len(self._records) > MAX_RECORDS:
            sorted_records = sorted(
                self._records.items(), key=lambda x: x[1].last_used
            )
            for sid, _ in sorted_records[:len(self._records) - MAX_RECORDS]:
                del self._records[sid]
        self._save()

    def forget(self, session_id: str) -> None:
        """清除指定会话的亲和性记录。"""
        if session_id in self._records:
            del self._records[session_id]
            self._save()

    def get(self, session_id: str) -> AffinityRecord | None:
        """查询会话的亲和性记录。off 模式下始终返回 None。"""
        if not self.enabled:
            return None
        self._load()
        return self._records.get(session_id)

    def reset(self) -> None:
        """清除所有亲和性记录。"""
        self._records.clear()
        self._save()

    def cleanup_expired(self) -> int:
        """清理过期记录，返回清理数量。"""
        self._load()
        now = time.time()
        expired = [
            sid for sid, rec in self._records.items()
            if now - rec.last_used > self._ttl
        ]
        for sid in expired:
            del self._records[sid]
        if expired:
            self._save()
        return len(expired)
