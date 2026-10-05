"""确定性离线 Provider：按 purpose 回放 fixtures（spec §16.1）。

回放规则：

- ``plan`` → ``fixtures_dir/plan_todo.json`` 的**原始文本**
- ``review`` → ``fixtures_dir/reviews.json`` 的 ``data[task_id]``；无该键时用
  ``data["_default"]``
- ``execute``（默认）→ ``fixtures_dir/responses/<task_id>/attempt-<attempt>.md``；
  缺失时回退到 ``attempt-1.md``（**并上报**，这是真实替换）；若连 ``attempt-1.md``
  都没有、但存在其他 ``attempt-*.md``，取编号最小者（spec §16.1 的「任何尝试都
  返回同一内容」语义，不属回退、不上报）；再没有则返回占位回复

fixture **文件**缺失一律不抛异常：走回退/占位路径并调用 ``_on_fallback``
上报（绝不静默）。``MockFixtureMissing`` 仅在 ``fixtures_dir is None`` 且
purpose 为 plan/review（二者无法伪造回复）时抛出。
"""

import asyncio
import json
import re
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

#: 形如 ``attempt-7.md`` 的夹具文件名。
_ATTEMPT_RE = re.compile(r"attempt-(\d+)\.md\Z")


class MockFixtureMissing(RuntimeError):
    """plan/review 回放需要 ``fixtures_dir``，但构造时未提供。"""


class MockProvider(BaseProvider):
    """离线确定性回放 Provider（无网络、无 SDK）。

    execute 回放的四级顺序：精确 ``attempt-<n>.md`` → ``attempt-1.md``（替换，
    上报）→ 编号最小的其他 ``attempt-*.md``（「任何尝试同一内容」，不上报）→
    占位回复（上报）。详见模块 docstring。
    """

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
        exact = base / f"attempt-{self._attempt}.md"
        if exact.is_file():
            return exact.read_text(encoding="utf-8")
        if self._attempt != 1:
            first = base / "attempt-1.md"
            if first.is_file():
                self._report(
                    f"mock fixture 替换：{exact.name} 不存在，改用 attempt-1.md（{base}）")
                return first.read_text(encoding="utf-8")
        shared = self._lowest_attempt(base)
        if shared is not None:
            # spec §16.1：只有非 1 号 attempt 文件时视为「任何尝试都返回同一内容」，
            # 这是正常回放而非回退，因此**不上报**（上报会淹没真实替换）。
            return shared.read_text(encoding="utf-8")
        return self._fallback(f"execute fixture 不存在：{base} 下没有可用的 attempt-*.md")

    # ── 辅助 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _lowest_attempt(base: Path) -> Path | None:
        """``base`` 下编号最小的 ``attempt-<n>.md``（无则 ``None``，确定性排序）。"""
        numbered = sorted(
            (int(m.group(1)), candidate)
            for candidate in base.glob("attempt-*.md")
            if candidate.is_file() and (m := _ATTEMPT_RE.match(candidate.name))
        )
        return numbered[0][1] if numbered else None

    def _require_fixtures_dir(self, purpose: str) -> Path:
        if self._fixtures_dir is None:
            raise MockFixtureMissing(f"purpose={purpose!r} 需要 fixtures_dir，但未配置")
        return self._fixtures_dir

    def _report(self, message: str) -> None:
        """把一条上报消息交给 ``_on_fallback``（未接线回调时丢弃）。"""
        if self._on_fallback is not None:
            self._on_fallback(
                f"{message}（purpose={self._purpose}, task={self._task_id}, "
                f"attempt={self._attempt}）")

    def _fallback(self, reason: str) -> str:
        """上报缺失并返回占位回复（绝不静默返回空串）。"""
        self._report(f"mock fixture 缺失：{reason}")
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
