"""外部 CLI 引擎 Provider（V2 第 3 项）。

把任意命令行 LLM 引擎（ollama CLI、llama.cpp、本地脚本等）接入平台。
通过 ``provider_overrides.cli.command`` 指定命令模板，支持占位符：
``{model}`` ``{prompt}`` ``{max_tokens}`` ``{temperature}``。

设计要点：
- 消息列表格式化为单段 prompt（CLI 工具通常只接受 stdin 或参数文本）；
- 通过 ``asyncio.create_subprocess_shell`` 异步执行，不阻塞事件循环；
- stdout 作为补全文本，stderr 记入异常信息；
- token 估算用平台统一的启发式（CLI 工具不返回用量）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime

from agentcrew.models.providers.base import (
    BaseProvider,
    CompletionResult,
    Message,
    ResponseFormat,
)
from agentcrew.utils.tokens import estimate_tokens

#: CLI 执行超时（秒）。本地模型推理可能较慢，给足余量。
_CLI_TIMEOUT_SEC = 300


class CliProvider(BaseProvider):
    """通过外部命令行工具生成补全。"""

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: ResponseFormat | None = None,
    ) -> CompletionResult:
        if self.binding.command is None:
            raise ValueError(
                f"cli provider 缺少 command 模板（alias={self.binding.alias!r}）："
                f"请在 agentcrew.yaml 的 models.providers.cli.command 或 "
                f"provider_overrides 中指定，例如 'ollama run {self.binding.model}'"
            )
        prompt = self._format_prompt(messages)
        command = self.binding.command.format(
            model=self.binding.model,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        started = datetime.now()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=_CLI_TIMEOUT_SEC,
            )
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
