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

**回放上下文按调用隔离**（C1）：purpose/role/task_id/attempt 存在
``contextvars.ContextVar`` 里，而不是实例字段上。``ModelFactory`` 按别名缓存
Provider（同一个实例被所有并发任务共用），而 ``complete()`` 内部有 ``await``
（``AGENTCREW_MOCK_LATENCY_MS`` 的延迟、工厂的调用级信号量）—— 实例字段会在
休眠期间被并发任务互相覆盖，任务读到别人的夹具、按错误的产物契约落盘。
contextvar 在每个 asyncio 任务里各有一份，写入只影响当前任务，读写之间没有
被他人插队的窗口。
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentcrew.models.config import ModelBinding
from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
    StreamChunk,
)
from agentcrew.utils.tokens import estimate_tokens

_PLAN = "plan"
_REVIEW = "review"
_EXECUTE = "execute"

#: 模拟流式时每块的字符数（P3-3）：按固定长度切片，保证即使很短的夹具也能
#: 切成多块，便于测试「逐块 yield」。
_STREAM_CHUNK_SIZE = 16

#: 形如 ``attempt-7.md`` 的夹具文件名。
_ATTEMPT_RE = re.compile(r"attempt-(\d+)\.md\Z")


@dataclass(frozen=True)
class _Replay:
    """一次回放的查找键：purpose / role / task_id / attempt。

    不可变值对象：上下文切换时整体替换，读到的四元组永远自洽（不会出现
    「新 task_id + 旧 attempt」这种半步状态）。
    """

    purpose: str
    role: str | None
    task_id: str | None
    attempt: int


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
        #: 构造期给定的回放上下文，只作为「本任务没人 set_purpose」时的兜底。
        self._default_replay = _Replay(
            purpose=purpose, role=role, task_id=task_id, attempt=attempt)
        #: **每个 Provider 实例一个 contextvar**：同一个上下文里可能有多个 mock
        #: 实例（不同别名），实例级别的变量保证 set_purpose 只影响它自己的回放；
        #: asyncio 的每个任务持有一份独立副本，并发调用互不可见（C1）。
        self._replay_var: ContextVar[_Replay | None] = ContextVar(
            "agentcrew_mock_replay", default=None)
        self._latency_ms = latency_ms
        self._on_fallback = on_fallback

    def set_replay(self, replay: _Replay) -> None:
        """设置**本次调用**的回放上下文（只影响当前 asyncio 任务）。"""
        self._replay_var.set(replay)

    def current_replay(self) -> _Replay:
        """当前上下文里的回放键；没人设置过时用构造期的那一份。"""
        return self._replay_var.get() or self._default_replay

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> CompletionResult:
        """按当前 purpose/task_id/attempt 回放 fixture。

        ``temperature`` / ``max_tokens`` / ``response_format`` 对 mock 无意义，
        接受但忽略。

        **先取上下文、再 await**：``complete`` 与 ``set_purpose`` 之间若隔着
        休眠，上下文就可能在休眠期间被别人改写 —— 这正是 C1 的竞态。
        """
        replay = self.current_replay()
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000)
        text = self._resolve_text(replay)
        prompt_tokens = sum(estimate_tokens(m["content"]) for m in messages)
        return CompletionResult(
            text=text,
            provider=self.binding.provider,
            model=self.binding.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=estimate_tokens(text),
            latency_ms=self._latency_ms,
        )

    async def stream_complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """模拟流式：与 :meth:`complete` 解析同一段夹具文本，按固定长度分块 yield。

        拼接所有 ``delta`` 必然逐字节等于 :meth:`complete` 的 ``text`` —— 流式
        与非流式在产物上必须不可区分（测试据此断言一致性）。同样**先取上下文、
        再 await**（C1），回放延迟只付一次。``temperature`` / ``max_tokens`` /
        ``response_format`` 对 mock 无意义，接受但忽略。
        """
        replay = self.current_replay()
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000)
        text = self._resolve_text(replay)
        for index in range(0, len(text), _STREAM_CHUNK_SIZE):
            piece = text[index:index + _STREAM_CHUNK_SIZE]
            is_last = index + _STREAM_CHUNK_SIZE >= len(text)
            yield StreamChunk(
                delta=piece,
                finish_reason="stop" if is_last else None,
            )

    # ── 查找顺序 ──────────────────────────────────────────────────────────

    def _resolve_text(self, replay: _Replay) -> str:
        if replay.purpose == _PLAN:
            return self._plan_text(replay)
        if replay.purpose == _REVIEW:
            return self._review_text(replay)
        if replay.purpose == _EXECUTE:
            return self._execute_text(replay)
        raise ValueError(f"未知 purpose：{replay.purpose!r}")

    def _plan_text(self, replay: _Replay) -> str:
        path = self._require_fixtures_dir(_PLAN) / "plan_todo.json"
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return self._fallback(f"plan fixture 不存在：{path}", replay)

    def _review_text(self, replay: _Replay) -> str:
        path = self._require_fixtures_dir(_REVIEW) / "reviews.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = data.get(replay.task_id) if replay.task_id is not None else None
            if entry is None:
                entry = data.get("_default")
            if entry is not None:
                return json.dumps(entry, ensure_ascii=False, indent=2)
            return self._fallback(
                f"reviews.json 无 task_id={replay.task_id!r} 且无 _default：{path}",
                replay)
        return self._fallback(f"review fixture 不存在：{path}", replay)

    def _execute_text(self, replay: _Replay) -> str:
        if self._fixtures_dir is None:
            return self._fallback("execute fixture 不可用：fixtures_dir 未配置", replay)
        base = self._fixtures_dir / "responses" / str(replay.task_id)
        exact = base / f"attempt-{replay.attempt}.md"
        if exact.is_file():
            return exact.read_text(encoding="utf-8")
        if replay.attempt != 1:
            first = base / "attempt-1.md"
            if first.is_file():
                self._report(
                    f"mock fixture 替换：{exact.name} 不存在，改用 attempt-1.md（{base}）",
                    replay)
                return first.read_text(encoding="utf-8")
        shared = self._lowest_attempt(base)
        if shared is not None:
            # spec §16.1：只有非 1 号 attempt 文件时视为「任何尝试都返回同一内容」，
            # 这是正常回放而非回退，因此**不上报**（上报会淹没真实替换）。
            return shared.read_text(encoding="utf-8")
        return self._fallback(
            f"execute fixture 不存在：{base} 下没有可用的 attempt-*.md", replay)

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

    def _report(self, message: str, replay: _Replay) -> None:
        """把一条上报消息交给 ``_on_fallback``（未接线回调时丢弃）。"""
        if self._on_fallback is not None:
            self._on_fallback(
                f"{message}（purpose={replay.purpose}, task={replay.task_id}, "
                f"attempt={replay.attempt}）")

    def _fallback(self, reason: str, replay: _Replay) -> str:
        """上报缺失并返回占位回复（绝不静默返回空串）。"""
        self._report(f"mock fixture 缺失：{reason}", replay)
        return f"# 占位回复（fixture 缺失）\ntask={replay.task_id} attempt={replay.attempt}\n"


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

    写入的是 **contextvar**（C1）：同一个 Provider 实例被并发任务共用，写入
    实例字段会被别人的调用覆盖，因此上下文只作用于**当前 asyncio 任务**，
    ``complete()`` 在同一个任务里读到的必然是这次调用自己设置的那一份。
    """
    provider.set_replay(_Replay(
        purpose=purpose, role=role, task_id=task_id, attempt=attempt))
