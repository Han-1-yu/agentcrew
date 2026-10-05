"""确定性离线 Provider：按 purpose 回放 fixtures（spec §16.1）。

回放规则：

- ``plan`` → ``fixtures_dir/plan_todo.json`` 的**原始文本**
- ``review`` → ``fixtures_dir/reviews.json`` 的 ``data[task_id]``；无该键时用
  ``data["_default"]``
- ``execute``（默认）→ ``fixtures_dir/responses/<task_id>/attempt-<attempt>.md``；
  该文件不存在时回退到 ``attempt-1.md``；再不存在则返回占位回复

fixture **文件**缺失一律不抛异常：走回退/占位路径并调用 ``_on_fallback``
上报（绝不静默）。``MockFixtureMissing`` 仅在 ``fixtures_dir is None`` 且
purpose 为 plan/review（二者无法伪造回复）时抛出。
"""

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path

from agentcrew.models.config import ModelBinding
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.utils.tokens import estimate_tokens

_PLAN = "plan"
_REVIEW = "review"
_EXECUTE = "execute"


class MockFixtureMissing(RuntimeError):
    """plan/review 回放需要 ``fixtures_dir``，但构造时未提供。"""


class MockProvider(BaseProvider):
    """离线确定性回放 Provider（无网络、无 SDK）。"""

    def __init__(
        self,
        binding: ModelBinding,
        *,
        fixtures_dir: Path | None = None,
        purpose: str = _EXECUTE,
        role: str | None = None,
        task_id: str | None = None,
        attempt: int = 1,
        latency_ms: int = 0,
        on_fallback: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(binding)
        self._fixtures_dir = Path(fixtures_dir) if fixtures_dir is not None else None
        self._purpose = purpose
        self._role = role
        self._task_id = task_id
        self._attempt = attempt
        self._latency_ms = latency_ms
        self._on_fallback = on_fallback

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        """按当前 purpose/task_id/attempt 回放 fixture。

        ``temperature`` / ``max_tokens`` / ``response_format`` 对 mock 无意义，
        接受但忽略。
        """
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000)
        text = self._resolve_text()
        prompt_tokens = sum(estimate_tokens(m["content"]) for m in messages)
        return CompletionResult(
            text=text,
            provider=self.binding.provider,
            model=self.binding.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=estimate_tokens(text),
            latency_ms=self._latency_ms,
        )

    # ── 查找顺序 ──────────────────────────────────────────────────────────

    def _resolve_text(self) -> str:
        if self._purpose == _PLAN:
            return self._plan_text()
        if self._purpose == _REVIEW:
            return self._review_text()
        if self._purpose == _EXECUTE:
            return self._execute_text()
        raise ValueError(f"未知 purpose：{self._purpose!r}")

    def _plan_text(self) -> str:
        path = self._require_fixtures_dir(_PLAN) / "plan_todo.json"
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return self._fallback(f"plan fixture 不存在：{path}")

    def _review_text(self) -> str:
        path = self._require_fixtures_dir(_REVIEW) / "reviews.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = data.get(self._task_id) if self._task_id is not None else None
            if entry is None:
                entry = data.get("_default")
            if entry is not None:
                return json.dumps(entry, ensure_ascii=False, indent=2)
            return self._fallback(
                f"reviews.json 无 task_id={self._task_id!r} 且无 _default：{path}")
        return self._fallback(f"review fixture 不存在：{path}")

    def _execute_text(self) -> str:
        if self._fixtures_dir is None:
            return self._fallback("execute fixture 不可用：fixtures_dir 未配置")
        base = self._fixtures_dir / "responses" / str(self._task_id)
        candidates = [base / f"attempt-{self._attempt}.md"]
        if self._attempt != 1:
            candidates.append(base / "attempt-1.md")
        for candidate in candidates:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        return self._fallback(
            "execute fixture 不存在：" + "、".join(str(c) for c in candidates))

    # ── 辅助 ──────────────────────────────────────────────────────────────

    def _require_fixtures_dir(self, purpose: str) -> Path:
        if self._fixtures_dir is None:
            raise MockFixtureMissing(f"purpose={purpose!r} 需要 fixtures_dir，但未配置")
        return self._fixtures_dir

    def _fallback(self, reason: str) -> str:
        """上报缺失并返回占位回复（绝不静默返回空串）。"""
        message = (f"mock fixture 缺失：{reason}"
                   f"（purpose={self._purpose}, task={self._task_id}, "
                   f"attempt={self._attempt}）")
        if self._on_fallback is not None:
            self._on_fallback(message)
        return f"# 占位回复（fixture 缺失）\ntask={self._task_id} attempt={self._attempt}\n"


def set_purpose(
    provider: MockProvider,
    purpose: str,
    *,
    role: str | None = None,
    task_id: str | None = None,
    attempt: int = 1,
) -> None:
    """设置 mock 回放上下文（Agent/调度层在每次调用前调用）。

    未传的 ``role`` / ``task_id`` 会被重置为 ``None``，``attempt`` 默认 1。
    """
    provider._purpose = purpose
    provider._role = role
    provider._task_id = task_id
    provider._attempt = attempt
