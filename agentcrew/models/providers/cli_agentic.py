"""外部 coding CLI 的**真 agentic 执行体** Provider（R60）。

与 :class:`~agentcrew.models.providers.cli.CliProvider`（一次性 stdin→stdout 桥）
不同，本 Provider 把 Claude Code / Codex 这类**自主多步**的 coding CLI 接成执行体：

- **cwd = run 工作区**：子进程在 ``<run_dir>/workspace`` 里自主读写文件（经
  :func:`agentcrew.utils.workspace.current_workspace` 读取，crew 在 run 入口写入）。
  未设置工作区时**明确报错**，绝不退回到服务进程 cwd 乱写；
- **结构化输出解析**：Claude Code ``--output-format json`` 的单对象结果、Codex
  ``exec --json`` 的 JSONL 事件流，分别抽出「最终文本 + token 用量」；解析不出
  用量时回退平台统一启发式（与 CliProvider 同口径），成本仍由工厂按绑定价格记账；
- **可配置超时**：agentic 多步执行比单次补全慢得多，默认放宽，且可按绑定覆盖；
- **安全默认**：权限/沙箱旗标写在命令模板里（claude ``--permission-mode
  acceptEdits``、codex ``--sandbox workspace-write``），argv 仍**先切分后替换**、
  不经 shell（继承 CliProvider 的加固）。

默认关闭：只有显式把某个别名解析到本 Provider（工厂按绑定的 agentic 标记选择）
才走这条路径；普通 cli / 其它 provider 行为逐字不变。离线可测：子进程可被
monkeypatch，解析函数是纯函数。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from agentcrew.models.config import CliOutputFormat
from agentcrew.models.providers.base import (
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.models.providers.cli import CliProvider
from agentcrew.utils.clock import seconds_between
from agentcrew.utils.tokens import estimate_tokens
from agentcrew.utils.workspace import current_workspace

#: agentic 多步执行的默认超时（秒）。比单次补全的 300s 放宽，仍可被绑定覆盖。
_DEFAULT_AGENTIC_TIMEOUT_SEC = 900


def _parse_claude_json(stdout: str) -> tuple[str, int | None, int | None]:
    """Claude Code ``--output-format json``：单对象 → (最终文本, in_tok, out_tok)。

    ``is_error`` 为真时抛 ``RuntimeError``（把 ``result``/``api_error`` 带出去），
    让上层走执行失败/返工通道，而不是把错误文本当产物。用量缺失返回 ``None``。
    """
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        raise ValueError("claude-json 输出不是 JSON 对象")
    if payload.get("is_error"):
        detail = payload.get("result") or payload.get("api_error") or "(无详情)"
        raise RuntimeError(f"Claude Code 返回错误：{detail}")
    text = str(payload.get("result") or "")
    usage = payload.get("usage") or {}
    return (
        text,
        _opt_int(usage.get("input_tokens")),
        _opt_int(usage.get("output_tokens")),
    )


def _parse_codex_jsonl(stdout: str) -> tuple[str, int | None, int | None]:
    """Codex ``exec --json``：JSONL 事件流 → (最终文本, in_tok, out_tok)。

    最终文本取**最后一条** ``item.completed`` 且 ``item.type == "agent_message"``
    的 ``item.text``；用量取 ``turn.completed`` 的 ``usage``。坏行跳过（事件流里
    混入非 JSON 行不该整次失败），一条都解析不出时回退原始 stdout 当文本。
    """
    text: str | None = None
    in_tok: int | None = None
    out_tok: int | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = str(item.get("text") or "")
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            in_tok = _opt_int(usage.get("input_tokens"))
            out_tok = _opt_int(usage.get("output_tokens"))
    if text is None:
        text = stdout
    return text, in_tok, out_tok


def _opt_int(value: Any) -> int | None:
    """把用量字段收敛成非负 int；缺失/非法返回 ``None``（回退启发式）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


class AgenticCliProvider(CliProvider):
    """在 run 工作区里自主执行的 coding CLI（cwd 隔离 + 结构化解析 + 用量）。"""

    def __init__(
        self,
        binding: Any,
        *,
        output_format: CliOutputFormat = "claude-json",
        timeout_seconds: int | None = None,
        workspace: Path | None = None,
    ) -> None:
        super().__init__(binding)
        self._output_format = output_format
        self._timeout = (
            timeout_seconds if timeout_seconds and timeout_seconds > 0
            else _DEFAULT_AGENTIC_TIMEOUT_SEC)
        #: 显式工作区（单测/特殊装配用）；缺省读 contextvar。
        self._workspace = workspace

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
        if self.binding.command is None:
            raise ValueError(
                f"agentic cli 缺少 command 模板（alias={self.binding.alias!r}）："
                f"请在 provider_overrides 或预设里指定，例如 "
                f"'claude -p --output-format json --permission-mode acceptEdits'"
            )
        cwd = self._workspace if self._workspace is not None else current_workspace()
        if cwd is None:
            raise ValueError(
                f"agentic cli 未设置 run 工作区（alias={self.binding.alias!r}）："
                f"拒绝在服务进程当前目录执行；应由 crew 在 run 入口注入工作区"
            )
        # 按需建工作区：非 agentic run 不会走到这里，目录布局保持不变。
        cwd.mkdir(parents=True, exist_ok=True)
        prompt = self._format_prompt(messages)
        argv = self._build_argv(
            model=self.binding.model, prompt=prompt,
            max_tokens=max_tokens, temperature=temperature)
        started = datetime.now()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise ValueError(
                f"coding CLI 可执行文件不存在（alias={self.binding.alias!r}）："
                f"{argv[0]!r}；检查 PATH 或 command 模板的第一段"
            ) from exc
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(
                f"coding CLI 超时（{self._timeout}s）：{self.binding.command!r}"
            ) from None
        latency_ms = int(seconds_between(started) * 1000)

        if proc.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"coding CLI 退出码 {proc.returncode}（alias={self.binding.alias!r}）："
                f"{stderr or '(无 stderr)'}"
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        text, in_tok, out_tok = self._parse(stdout)
        return CompletionResult(
            text=text,
            provider="cli",
            model=self.binding.model,
            prompt_tokens=in_tok if in_tok is not None else estimate_tokens(prompt),
            completion_tokens=(
                out_tok if out_tok is not None else estimate_tokens(text)),
            latency_ms=latency_ms,
            finish_reason="stop",
        )

    def _parse(self, stdout: str) -> tuple[str, int | None, int | None]:
        if self._output_format == "claude-json":
            return _parse_claude_json(stdout)
        if self._output_format == "codex-jsonl":
            return _parse_codex_jsonl(stdout)
        return stdout, None, None
