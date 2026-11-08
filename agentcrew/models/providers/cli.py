"""外部 CLI 引擎 Provider（V2 第 3 项）。

把任意命令行 LLM 引擎（ollama CLI、llama.cpp、本地脚本等）接入平台。
通过 ``provider_overrides.cli.command`` 指定命令模板，支持占位符：
``{model}`` ``{prompt}`` ``{max_tokens}`` ``{temperature}``。

设计要点（2026-11 安全加固后）：

- 消息列表格式化为单段 prompt（CLI 工具通常只接受 stdin 或参数文本），
  **prompt 一律走 stdin**，不作为命令行参数传递；
- 用 ``asyncio.create_subprocess_exec`` + **argv 模板**执行，**不经 shell**：
  命令模板先按 shell 词法切成 argv，再对**每个 token** 做占位符替换。顺序很关键
  ——先替换再切分等于把模型/配置里的空格与引号当成新的参数边界；先切分再替换
  则替换结果永远只是一个 argv 元素，不会被重新解释；
- stdout 作为补全文本，stderr 记入异常信息；
- token 估算用平台统一的启发式（CLI 工具不返回用量）。

为什么不保留 ``create_subprocess_shell``：命令模板来自配置文件（不是模型输出），
所以「模型注入 shell 命令」不成立；但 shell 解释层会让模板里的 ``|`` ``&&``
``$(...)`` 全部生效，一个被改写的配置文件就等于本机任意命令执行。去掉 shell
之后，模板写错最多是「参数不对」，不会是「命令被拼出来」。
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.utils.tokens import estimate_tokens

#: CLI 执行超时（秒）。本地模型推理可能较慢，给足余量。
_CLI_TIMEOUT_SEC = 300

#: 占位符解析失败（模板里有非占位符的花括号）时的提示。
_FORMAT_HINT = (
    "command 模板里的占位符只有 {model} / {prompt} / {max_tokens} / "
    "{temperature}；需要字面花括号请写成 {{ 和 }}"
)


class CliProvider(BaseProvider):
    """通过外部命令行工具生成补全（argv 直接执行，不经 shell）。"""

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
                f"cli provider 缺少 command 模板（alias={self.binding.alias!r}）："
                f"请在 agentcrew.yaml 的 models.providers.cli.command 或 "
                f"provider_overrides 中指定，例如 'ollama run {self.binding.model}'"
            )
        prompt = self._format_prompt(messages)
        argv = self._build_argv(
            model=self.binding.model, prompt=prompt,
            max_tokens=max_tokens, temperature=temperature)
        started = datetime.now()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=_CLI_TIMEOUT_SEC,
            )
        except FileNotFoundError as exc:
            raise ValueError(
                f"CLI 引擎可执行文件不存在（alias={self.binding.alias!r}）："
                f"{argv[0]!r}；检查 PATH 或 command 模板的第一段"
            ) from exc
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(
                f"CLI 引擎超时（{_CLI_TIMEOUT_SEC}s）：{self.binding.command!r}"
            ) from None
        latency_ms = int((datetime.now() - started).total_seconds() * 1000)

        if proc.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"CLI 引擎退出码 {proc.returncode}（alias={self.binding.alias!r}）："
                f"{stderr or '(无 stderr)'}"
            )

        text = stdout_bytes.decode("utf-8", errors="replace")
        prompt_tokens = estimate_tokens(prompt)
        completion_tokens = estimate_tokens(text)
        return CompletionResult(
            text=text,
            provider="cli",
            model=self.binding.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            finish_reason="stop",
        )

    def _build_argv(
        self, *, model: str, prompt: str, max_tokens: int, temperature: float,
    ) -> list[str]:
        """命令模板 → argv 列表：**先切分、后替换**，替换结果永不参与词法分析。

        ``shlex.split`` 用 POSIX 词法（跨平台一致：Windows 上不必按 cmd.exe 的
        引号规则再猜一遍）。含空格的可执行文件路径要加双引号，例如
        ``"C:/Program Files/foo/bar.exe" --flag``。
        """
        template = self.binding.command or ""
        try:
            tokens = shlex.split(template, posix=True)
        except ValueError as exc:
            raise ValueError(
                f"cli provider 的 command 模板引号不配对（alias="
                f"{self.binding.alias!r}）：{exc}；模板写的是 {template!r}"
            ) from None
        if not tokens:
            raise ValueError(
                f"cli provider 的 command 模板为空（alias={self.binding.alias!r}）")
        argv: list[str] = []
        for token in tokens:
            try:
                argv.append(token.format(
                    model=model, prompt=prompt,
                    max_tokens=max_tokens, temperature=temperature,
                ))
            except (KeyError, IndexError, ValueError):
                raise ValueError(
                    f"cli provider 的 command 模板占位符非法（alias="
                    f"{self.binding.alias!r}）：{token!r}；{_FORMAT_HINT}"
                ) from None
        if not argv[0].strip():
            raise ValueError(
                f"cli provider 的 command 模板第一段不是可执行文件（alias="
                f"{self.binding.alias!r}）")
        return argv

    @staticmethod
    def _format_prompt(messages: Sequence[Message]) -> str:
        """把消息列表格式化为 CLI 工具可消费的单段文本。

        system 消息放在最前，user/assistant 交替，用角色标签分隔。
        这是大多数本地 CLI 工具的通用输入格式；特定工具如需不同格式，
        应在 command 模板里用 ``{prompt}`` 接收后自行处理。
        """
        parts: list[str] = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if role == "system":
                parts.append(f"[System]\n{content}")
            elif role == "user":
                parts.append(f"[User]\n{content}")
            else:
                parts.append(f"[Assistant]\n{content}")
        return "\n\n".join(parts)
