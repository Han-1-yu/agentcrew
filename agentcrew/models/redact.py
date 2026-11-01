"""数据脱敏模块（V7-P0，借鉴 magpie internal/redact）。

在 LLM 请求发送给厂商之前，自动识别并替换敏感信息（API Key、密码、token、
手机号、邮箱等），响应返回后再还原。核心设计：

- **确定性占位符**：用 ``<REDACT:sha1前8位>`` 形式替换，同一原文始终映射到同一
  占位符，因此模型在回复中引用占位符时可以精确还原。
- **双向映射**：``Redactor`` 实例维护 ``placeholder → original`` 映射，``mask()``
  脱敏后调用 ``unmask()`` 还原。一次请求-响应周期使用同一个 Redactor 实例。
- **JSON 安全**：对 JSON 字符串值脱敏时保留 JSON 结构，只替换字符串内容；
  对纯文本直接正则替换。
- **可配置**：内置常见敏感模式，支持自定义正则、自定义词表、开关控制。

使用方式（工厂内部）::

    redactor = Redactor(options)
    masked_messages = redactor.mask_messages(messages)
    result = await provider.complete(masked_messages, ...)
    result.text = redactor.unmask(result.text)

默认关闭（``enabled=False``），开启后零侵入。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# ── 内置敏感模式 ────────────────────────────────────────────────────────────

#: API Key / Secret 类：sk-..., Bearer ..., 各种 key= 赋值
_API_KEY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("generic_key", re.compile(r"\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}['\"]?", re.IGNORECASE)),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{16,}", re.IGNORECASE)),
    ("jwt_token", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
]

#: 个人身份信息
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("phone_cn", re.compile(r"\b1[3-9]\d{9}\b")),
    ("phone_us", re.compile(r"\b\+?1[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
    ("id_card_cn", re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")),
    ("bank_card", re.compile(r"\b\d{16,19}\b")),
]

#: 密码 / 凭据类
_CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("password_field", re.compile(r"\b(?:password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\"]{6,}['\"]?", re.IGNORECASE)),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
]


@dataclass
class RedactOptions:
    """脱敏配置。

    Attributes:
        enabled: 总开关。False 时 mask/unmask 均为恒等操作。
        mask_api_keys: 脱敏 API Key / Secret / Token。
        mask_pii: 脱敏个人身份信息（邮箱、手机号、身份证、银行卡）。
        mask_credentials: 脱敏密码和私钥。
        custom_patterns: 自定义正则列表，每项为 (label, pattern)。
        custom_words: 自定义敏感词列表（精确匹配，大小写不敏感）。
        placeholder_prefix: 占位符前缀，默认 ``REDACT``。
    """

    enabled: bool = False
    mask_api_keys: bool = True
    mask_pii: bool = True
    mask_credentials: bool = True
    custom_patterns: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    custom_words: list[str] = field(default_factory=list)
    placeholder_prefix: str = "REDACT"

    def active_patterns(self) -> list[tuple[str, re.Pattern[str]]]:
        """返回当前启用的所有模式。"""
        patterns: list[tuple[str, re.Pattern[str]]] = []
        if self.mask_api_keys:
            patterns.extend(_API_KEY_PATTERNS)
        if self.mask_pii:
            patterns.extend(_PII_PATTERNS)
        if self.mask_credentials:
            patterns.extend(_CREDENTIAL_PATTERNS)
        patterns.extend(self.custom_patterns)
        return patterns


class Redactor:
    """单次请求-响应周期的脱敏器。

    每个请求创建一个新实例，维护该周期内的占位符映射。线程不安全，
    但在 asyncio 单线程模型下无需加锁。
    """

    def __init__(self, options: RedactOptions | None = None) -> None:
        self.options = options or RedactOptions()
        # placeholder → original text
        self._mapping: dict[str, str] = {}
        # original → placeholder（避免重复计算）
        self._reverse: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self.options.enabled

    def mask_count(self) -> int:
        """本次脱敏替换的唯一敏感片段数量。"""
        return len(self._mapping)

    def _placeholder_for(self, original: str) -> str:
        """为原文生成确定性占位符。"""
        if original in self._reverse:
            return self._reverse[original]
        digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:8]
        placeholder = f"<{self.options.placeholder_prefix}:{digest}>"
        self._mapping[placeholder] = original
        self._reverse[original] = placeholder
        return placeholder

    def mask_text(self, text: str) -> str:
        """对纯文本脱敏。未启用时原样返回。"""
        if not self.options.enabled or not text:
            return text
        result = text
        for _label, pattern in self.options.active_patterns():
            result = pattern.sub(
                lambda m: self._placeholder_for(m.group(0)), result
            )
        for word in self.options.custom_words:
            if not word:
                continue
            result = re.sub(
                re.escape(word),
                lambda m: self._placeholder_for(m.group(0)),
                result,
                flags=re.IGNORECASE,
            )
        return result

    def unmask_text(self, text: str) -> str:
        """还原被脱敏的文本。未启用或无映射时原样返回。"""
        if not self.options.enabled or not self._mapping or not text:
            return text
        result = text
        for placeholder, original in self._mapping.items():
            result = result.replace(placeholder, original)
        return result

    def mask_messages(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """对消息列表脱敏。

        遍历每条消息的 ``content`` 字段（支持字符串和内容块列表），
        对其中的文本部分脱敏。其他字段（role、tool_call_id 等）保持不变。
        """
        if not self.options.enabled:
            return [dict(m) for m in messages]
        masked: list[dict[str, Any]] = []
        for msg in messages:
            new_msg = dict(msg)
            content = msg.get("content")
            if isinstance(content, str):
                new_msg["content"] = self.mask_text(content)
            elif isinstance(content, list):
                new_msg["content"] = [
                    self._mask_content_block(block) for block in content
                ]
            masked.append(new_msg)
        return masked

    def _mask_content_block(self, block: Any) -> Any:
        """脱敏单个内容块（OpenAI 格式的 content 列表项）。"""
        if not isinstance(block, Mapping):
            return block
        new_block = dict(block)
        block_type = block.get("type")
        if block_type in ("text", "input_text") and "text" in block:
            new_block["text"] = self.mask_text(str(block["text"]))
        elif block_type == "tool_result" and isinstance(block.get("content"), str):
            new_block["content"] = self.mask_text(str(block["content"]))
        return new_block

    def mask_json_body(self, body: str) -> str:
        """对 JSON 请求体脱敏。

        尝试解析为 JSON，对所有字符串值递归脱敏；解析失败时回退到纯文本脱敏。
        """
        if not self.options.enabled or not body:
            return body
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return self.mask_text(body)
        masked = self._mask_json_value(data)
        return json.dumps(masked, ensure_ascii=False)

    def _mask_json_value(self, value: Any) -> Any:
        """递归脱敏 JSON 值中的字符串。"""
        if isinstance(value, str):
            return self.mask_text(value)
        if isinstance(value, list):
            return [self._mask_json_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._mask_json_value(v) for k, v in value.items()}
        return value

    def unmask_json_body(self, body: str) -> str:
        """还原 JSON 响应体中的占位符。"""
        if not self.options.enabled or not self._mapping or not body:
            return body
        # 直接字符串替换即可，占位符格式不会与 JSON 语法冲突
        return self.unmask_text(body)

    # ── 流式还原（V7 质量修复：处理占位符被拆分成多个 chunk）──────────────

    def unmask_stream_chunk(self, chunk: str) -> str:
        """流式逐块还原，处理占位符被拆分成两个 chunk 的边界情况。

        占位符格式为 ``<REDACT:8hex>``，长度固定 17 字符（含尖括号）。
        当 chunk 末尾出现不完整的占位符前缀时，缓冲到下一块再还原。

        用法（在 stream_call 中）::

            redactor = self._make_redactor()
            async for chunk in provider.stream_complete(...):
                delta = redactor.unmask_stream_chunk(chunk.delta)
                yield chunk.model_copy(update={"delta": delta})
            # 流结束后冲刷缓冲区
            final = redactor.unmask_stream_flush()
            if final:
                yield StreamChunk(delta=final, ...)
        """
        if not self.options.enabled or not self._mapping or not chunk:
            return chunk
        # 拼接上一块残留的不完整占位符
        buffered = getattr(self, "_stream_buffer", "")
        text = buffered + chunk
        # 查找最后一个 '<' 的位置，检查其后是否是不完整的占位符
        last_lt = text.rfind("<")
        if last_lt >= 0:
            tail = text[last_lt:]
            # 占位符前缀 <REDACT: 长度 8，完整占位符 <REDACT:xxxxxxxx> 长度 17
            if tail.startswith("<REDACT:") and ">" not in tail:
                # 不完整的占位符，缓冲到下一块
                self._stream_buffer = tail
                text = text[:last_lt]
            else:
                self._stream_buffer = ""
        else:
            self._stream_buffer = ""
        return self.unmask_text(text)

    def unmask_stream_flush(self) -> str:
        """流结束时冲刷缓冲区，还原最后残留的不完整占位符。"""
        buffered = getattr(self, "_stream_buffer", "")
        if not buffered:
            return ""
        self._stream_buffer = ""
        return self.unmask_text(buffered)


def mask_messages(
    messages: Sequence[Mapping[str, Any]],
    options: RedactOptions | None = None,
) -> tuple[list[dict[str, Any]], Redactor]:
    """便捷函数：创建 Redactor 并脱敏消息列表。

    Returns:
        (masked_messages, redactor) — 用同一个 redactor 调用 unmask_text 还原响应。
    """
    redactor = Redactor(options)
    return redactor.mask_messages(messages), redactor
