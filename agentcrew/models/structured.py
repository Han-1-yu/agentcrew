"""结构化输出降级链：把「模型可能完全不支持 JSON 模式」收敛成一次可靠调用（spec §12.3）。

链条共三级 + 第 3 级的修复循环：

1. ``supports_json_schema`` → ``response_format={"type": "json_schema", ...}``
   （``strict=True``，由服务端强制 schema）；
2. 仅 ``supports_json_object`` → ``response_format={"type": "json_object"}``，
   并把 ``json.dumps(schema.model_json_schema(), ensure_ascii=False)`` 内嵌进
   提示词 —— ``json_object`` 模式不携带 schema，不内嵌模型就不知道要吐什么形状；
3. 两者都不支持 → 不传 ``response_format``，**同样把 schema 内嵌进提示词**
   （纯文本模式连 ``json_object`` 都没有，提示词是模型知道形状的唯一来源），
   靠提示词约束 + :func:`extract_json` 提取（真实模型高频把 JSON 包在围栏里或
   前后加解释文字，Review Focus #2）；解析/校验失败进入**修复循环**：把校验错误
   作为新的 ``user`` 轮追加（保留原对话），最多 ``repair_attempts`` 次
   （默认 2，即最多 3 次 LLM 调用）。

设计决定（控制器分辨率）：

- **起始级别由能力位决定**，不先试探；能力驱动的下降本身也经 ``on_degrade``
  上报一次（``from_level=1`` → 实际起始级别），调用方在 ledger 里能看到「这家
  provider 只能到第几级」；
- **级别 1/2 的失败（provider 异常或解析/校验失败）逐级下降一次并重试**，
  ``on_degrade`` 的 ``reason`` 取自异常；第 3 级是链条底部，失败进修复循环；
- **第 3 级的 provider 异常原样上抛，不进修复循环**：修复提示的语义是「你上次的
  输出不合格」，而 provider 异常根本没有输出可修；重试语义已由 provider 层
  （``retry_async``）与调用方负责，把网络故障包装成 :class:`StructuredOutputError`
  会误导调用方与 ledger；
- ``on_degrade`` 只在真实级别变化时触发，修复重试绝不触发；
- 不缓存、不放松 schema —— YAGNI。
"""

import json
import re
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from agentcrew.models.config import ProviderCapabilities
from agentcrew.models.providers.base import BaseProvider, Message, ResponseFormat

#: 第 1 级：服务端按 JSON Schema 严格约束输出。
_LEVEL_JSON_SCHEMA = 1
#: 第 2 级：只要求输出 JSON 对象，schema 由提示词内嵌补足。
_LEVEL_JSON_OBJECT = 2
#: 第 3 级：无 ``response_format``，解析文本 + 修复循环兜底。
_LEVEL_PROMPT_ONLY = 3
#: 链条底部：第 3 级失败只能进修复循环，没有更低级别可退。
_MAX_LEVEL = _LEVEL_PROMPT_ONLY

#: 能力驱动的起始降级原因（用户从未要求这一级，是 provider 逼的）。
_CAPABILITY_REASONS: dict[int, str] = {
    _LEVEL_JSON_OBJECT: "provider 不支持 json_schema，起始级别降为 2（json_object）",
    _LEVEL_PROMPT_ONLY: (
        "provider 不支持 json_schema/json_object，起始级别降为 3（提示词约束 + 修复循环）"),
}

#: 首个 ```json 围栏块的内容（非贪婪：到最近的闭合围栏为止）。
_FENCE_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)

#: JSON 容器起始字符。
_OPENERS = "{["

#: 闭合字符 → 配对的起始字符（用栈做类型匹配，``{]`` 这类错配不会被当成平衡）。
_CLOSERS = {"}": "{", "]": "["}


class StructuredOutputError(RuntimeError):
    """修复次数耗尽仍拿不到能通过校验的对象（第 3 级）。

    ``attempts`` 是本次 :func:`complete_structured` 调用实际发出的
    ``provider.complete()`` 次数（含降级与修复的每一次），``last_text`` /
    ``last_error`` 是最后一次失败的原样材料，便于调用方与 ledger 诊断。
    """

    def __init__(
        self,
        *,
        schema_name: str,
        attempts: int,
        last_text: str,
        last_error: Exception,
    ) -> None:
        super().__init__(
            f"结构化输出失败：schema={schema_name}，共 {attempts} 次调用，"
            f"最后一次输出：{last_text[:200]!r}，错误：{last_error}")
        self.schema_name = schema_name
        self.attempts = attempts
        self.last_text = last_text
        self.last_error = last_error


class DegradeEvent(BaseModel):
    """一次降级（经 ``on_degrade`` 上报，供 ledger 审计）。"""

    model_config = ConfigDict(extra="forbid")

    provider: str
    from_level: int
    to_level: int
    reason: str


def _skip_string(text: str, start: int) -> int:
    """``text[start]`` 是引号；返回配对引号**之后**的下标；未闭合返回 -1。

    反斜杠转义整体跳过（``\\"``、``\\'``、``\\\\``），因此被转义的引号不会
    提前结束字符串。
    """
    quote = text[start]
    index = start + 1
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return -1


def _matching_close(text: str, start: int) -> int | None:
    """``text[start]`` 为 ``{``/``[``；返回配对闭合字符的下标，找不到返回 ``None``。

    用**类型匹配的栈**而非单一深度计数：``{]`` 这类错配会返回 ``None``，由
    调用方改试下一个起点。字符串字面量（单双引号，含反斜杠转义）整段跳过 ——
    里面的括号不参与配对（模型常常把 ``}`` 写进解释性字符串里）。
    """
    stack: list[str] = []
    index = start
    length = len(text)
    while index < length:
        char = text[index]
        if char in "\"'":
            index = _skip_string(text, index)
            if index < 0:
                return None
            continue
        if char in _OPENERS:
            stack.append(char)
        elif char in _CLOSERS:
            if not stack or stack[-1] != _CLOSERS[char]:
                return None
            stack.pop()
            if not stack:
                return index
        index += 1
    return None


def extract_json(text: str) -> str:
    """从任意文本中提取首个平衡的 JSON 对象/数组字符串。

    先取首个 ```json 围栏块的内容（围栏优先级最高）；否则从第一个 ``{``/``[``
    起用跳过字符串字面量的配对栈找闭合位置，某个起点无法配对时继续试下一个
    起点（前置 prose 里可能有未闭合的花括号）。围栏内容为空、以及全文本都找不到
    平衡候选时，回落到扫描/抛 :class:`ValueError`。

    只保证「平衡」，不保证 ``json.loads`` 一定成功：prose 里先出现自平衡的
    ``{占位}`` 时返回的就是它；把它变成合法 JSON 是上层修复循环的职责
    （见 :func:`complete_structured`）。
    """
    fence = _FENCE_RE.search(text)
    if fence is not None:
        content = fence.group(1).strip()
        if content:
            return content
    for index, char in enumerate(text):
        if char not in _OPENERS:
            continue
        close = _matching_close(text, index)
        if close is not None:
            return text[index:close + 1]
    raise ValueError(f"文本中找不到平衡的 JSON 对象或数组：{text[:120]!r}")


def build_response_format(schema: type[BaseModel], level: int) -> ResponseFormat | None:
    """按级别构造 ``response_format``；第 3 级返回 ``None``（不传该参数）。"""
    if level == _LEVEL_JSON_SCHEMA:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": schema.model_json_schema(),
                "strict": True,
            },
        }
    if level == _LEVEL_JSON_OBJECT:
        return {"type": "json_object"}
    return None


def _starting_level(capabilities: ProviderCapabilities) -> int:
    """能力位 → 起始级别：schema → 1；仅 object → 2；都不支持 → 3。"""
    if capabilities.supports_json_schema:
        return _LEVEL_JSON_SCHEMA
    if capabilities.supports_json_object:
        return _LEVEL_JSON_OBJECT
    return _LEVEL_PROMPT_ONLY


def _notify_degrade(
    on_degrade: Callable[[DegradeEvent], None] | None,
    provider: str,
    from_level: int,
    to_level: int,
    reason: str,
) -> None:
    """构造并投递一条降级事件（未接线回调时不产生任何开销）。"""
    if on_degrade is None:
        return
    on_degrade(DegradeEvent(
        provider=provider, from_level=from_level, to_level=to_level, reason=reason))


def _degrade(
    level: int,
    provider: str,
    reason: str,
    on_degrade: Callable[[DegradeEvent], None] | None,
) -> int:
    """降一级并上报（只由 1/2 级的失败路径调用；链条底部不降）。"""
    to_level = level + 1
    _notify_degrade(on_degrade, provider, level, to_level, reason)
    return to_level


def _messages_for_level(
    conversation: list[Message], schema: type[BaseModel], level: int
) -> list[Message]:
    """构造本次调用实际发送的消息（每次返回新列表，绝不就地修改对话）。

    第 2/3 级都要把 ``json.dumps(schema.model_json_schema(),
    ensure_ascii=False)`` 内嵌进提示词：第 2 级 ``json_object`` 不携带 schema；
    第 3 级连 ``json_object`` 都没有，**更是**只能从提示词得知形状（这是
    PM 拆解与 G2 评审在 anthropic/ernie 上的默认路径，不内嵌会把每次结构化
    调用都推成 2~3 次）。第 1 级由 API 的 ``json_schema`` 参数携带，不注入 ——
    重复一遍只浪费上下文。
    """
    if level == _LEVEL_JSON_SCHEMA:
        return list(conversation)
    instruction = (
        "你必须只返回一个 JSON 对象，不要任何解释文字。"
        "该 JSON 必须符合以下 JSON Schema：\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )
    return [{"role": "system", "content": instruction}, *conversation]


def _repair_prompt(error: Exception) -> str:
    """修复提示：把校验错误原样交给模型（brief 逐字钉死的三段式）。"""
    return f"你上次的输出校验失败：\n{error}\n请仅返回修正后的 JSON，不要任何解释文字。"


async def complete_structured(
    provider: BaseProvider,
    messages: Sequence[Message],
    schema: type[BaseModel],
    *,
    temperature: float,
    max_tokens: int,
    repair_attempts: int = 2,
    on_degrade: Callable[[DegradeEvent], None] | None = None,
) -> BaseModel:
    """按降级链取得一个通过 ``schema`` 校验的对象。

    每一步的调用次数都刻意可数：级别 1/2 每次失败只降一级（最多降到第 3 级）；
    第 3 级最多调用 ``1 + repair_attempts`` 次。校验失败与 provider 异常的区别
    见模块 docstring。全部修复失败抛 :class:`StructuredOutputError`；第 3 级的
    provider 异常原样上抛。
    """
    level = _starting_level(provider.capabilities)
    provider_name = provider.binding.provider
    if level > _LEVEL_JSON_SCHEMA:
        _notify_degrade(on_degrade, provider_name, _LEVEL_JSON_SCHEMA, level,
                        _CAPABILITY_REASONS[level])

    conversation: list[Message] = list(messages)
    calls = 0
    repairs = 0
    last_text = ""

    while True:
        calls += 1
        try:
            result = await provider.complete(
                _messages_for_level(conversation, schema, level),
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=build_response_format(schema, level),
            )
        except Exception as exc:
            # provider 层错误（网络/SDK/参数被拒）：第 3 级没有更低级别可退，
            # 也不是解析失败（分辨率 9）→ 原样上抛；1/2 级降一级重试一次。
            # 捕获 ``Exception`` 而非裸 except：CancelledError 等 BaseException
            # 照常穿透。
            if level == _MAX_LEVEL:
                raise
            level = _degrade(level, provider_name, f"{type(exc).__name__}: {exc}", on_degrade)
            continue

        last_text = result.text
        try:
            payload = json.loads(extract_json(result.text))
            return schema.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            # 解析失败：``json.JSONDecodeError`` 与 ``extract_json`` 的「没有
            # JSON」都是 ValueError；pydantic 校验失败是 ValidationError。
            # 级别 1/2 视为该级别失败 → 降一级重试；第 3 级进修复循环。
            if level < _MAX_LEVEL:
                level = _degrade(level, provider_name, f"{type(exc).__name__}: {exc}", on_degrade)
                continue
            if repairs >= repair_attempts:
                raise StructuredOutputError(
                    schema_name=schema.__name__,
                    attempts=calls,
                    last_text=last_text,
                    last_error=exc,
                ) from exc
            repairs += 1
            conversation.append({"role": "user", "content": _repair_prompt(exc)})
