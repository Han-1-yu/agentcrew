"""结构化输出降级链的行为测试（spec §12.3，Task 20）。

brief 用例原样保留；在其之上补控制器分辨率与自审清单要求的边界：

- ``extract_json``：转义引号、嵌套数组、单引号字符串字面量、前置 prose 里的
  未闭合花括号、围栏优先级、空围栏回落到扫描；
- 降级链：级别 1/2 因 provider 异常逐级下降（钉死 on_degrade 的
  provider/from_level/to_level/reason），级别 2 的校验失败同样降级
  （分辨率 2 的「failure」不限于 provider 异常）；
- 修复循环：修复提示逐字包含校验错误、原对话保留且不被就地修改、
  ``repair_attempts=0`` 只调用一次、默认 2 时最多 3 次调用、耗尽时 diagnostics 完整；
- ``on_degrade`` 只在真实降级时触发，修复重试绝不触发；
- 第 3 级的 provider 异常不进修复循环，原样上抛（分辨率 9 的选择）。
"""

import json

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agentcrew.models.config import parse_alias
from agentcrew.models.providers.base import CompletionResult
from agentcrew.models.structured import (
    StructuredOutputError,
    build_response_format,
    complete_structured,
    extract_json,
)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str
    score: float


class Commented(BaseModel):
    """带非 ASCII 描述，用于钉死提示词内嵌时 ensure_ascii=False。"""

    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(description="评审结论：approve 或 reject")


class ScriptedProvider:
    """按脚本依次返回预设文本；记录收到的 response_format。"""

    def __init__(self, binding, texts):
        self.binding = binding
        self.capabilities = binding.capabilities
        self._texts = list(texts)
        self.seen_formats = []

    async def complete(self, messages, *, temperature, max_tokens, response_format=None):
        self.seen_formats.append(response_format)
        text = self._texts.pop(0) if self._texts else "{}"
        return CompletionResult(text=text, provider=self.binding.provider,
                                model=self.binding.model,
                                prompt_tokens=1, completion_tokens=1, latency_ms=0)


VALID = json.dumps({"verdict": "approve", "score": 0.9})


# ── extract_json ──

def test_extract_plain_json():
    assert json.loads(extract_json(VALID))["verdict"] == "approve"


def test_extract_from_markdown_fence():
    assert json.loads(extract_json(f"```json\n{VALID}\n```"))["verdict"] == "approve"


def test_extract_from_surrounded_prose():
    txt = f"Sure! Here is the result:\n{VALID}\nHope that helps."
    assert json.loads(extract_json(txt))["score"] == 0.9


def test_extract_handles_nested_braces():
    txt = 'prefix {"a": {"b": {"c": 1}}, "d": [1,2]} suffix'
    assert json.loads(extract_json(txt))["a"]["b"]["c"] == 1


def test_extract_handles_braces_inside_strings():
    txt = '{"msg": "a } brace", "n": 1}'
    assert json.loads(extract_json(txt))["n"] == 1


def test_extract_raises_when_no_json():
    with pytest.raises(ValueError):
        extract_json("no json at all")


# ── extract_json 边界（控制器自审清单）──

def test_extract_handles_escaped_quote_inside_string():
    txt = r'{"msg": "say \"hi\" }", "n": 2}'
    assert json.loads(extract_json(txt))["n"] == 2


def test_extract_handles_nested_arrays():
    txt = 'lead [{"a": [1, [2, 3]]}, {"b": 2}] tail'
    assert json.loads(extract_json(txt))[1]["b"] == 2


def test_extract_handles_single_quoted_string_with_brackets():
    txt = "{'msg': 'a ] } [', 'n': 1}"
    assert extract_json(txt) == txt


def test_extract_skips_unclosed_stray_brace_before_json():
    txt = '说明 { 未闭合，结果：{"a": 1}'
    assert json.loads(extract_json(txt))["a"] == 1


def test_extract_prefers_fenced_block_over_earlier_braces():
    txt = '说明 {占位}\n```json\n{"a": 1}\n```\n结束'
    assert json.loads(extract_json(txt))["a"] == 1


def test_extract_empty_fence_falls_back_to_scanning():
    txt = '```json\n```\n结果是 {"a": 1}'
    assert json.loads(extract_json(txt))["a"] == 1


# ── build_response_format ──

def test_level1_builds_json_schema():
    rf = build_response_format(Answer, 1)
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True


def test_level2_builds_json_object():
    assert build_response_format(Answer, 2) == {"type": "json_object"}


def test_level3_returns_none():
    assert build_response_format(Answer, 3) is None


def test_level1_carries_schema_name_and_body():
    rf = build_response_format(Answer, 1)
    assert rf["json_schema"]["name"] == "Answer"
    assert rf["json_schema"]["schema"]["properties"]["score"]["type"] == "number"


# ── 降级链 ──

async def test_level1_used_when_capable():
    b = parse_alias("openai:gpt-4o-mini")
    p = ScriptedProvider(b, [VALID])
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert r.verdict == "approve"
    assert p.seen_formats[0]["type"] == "json_schema"


async def test_level2_used_when_only_object_supported():
    b = parse_alias("qwen:qwen-max")
    p = ScriptedProvider(b, [VALID])
    await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                              temperature=0, max_tokens=10)
    assert p.seen_formats[0] == {"type": "json_object"}


# ── Review Focus #2：模型把 JSON 包在围栏里/加解释文字 ──

async def test_level3_extracts_fenced_json_when_provider_incapable():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, [f"好的，结果如下：\n```json\n{VALID}\n```\n以上。"])
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert r.verdict == "approve" and p.seen_formats[0] is None


async def test_level3_extracts_prose_wrapped_json():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, [f"Here you go: {VALID} — done."])
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert r.score == 0.9


# ── 降级链：provider 异常逐级下降（分辨率 2）──

class DegradingProvider:
    """前 ``fail_calls`` 次调用抛异常，之后按脚本返回；记录每次的格式与消息。"""

    def __init__(self, binding, texts, *, fail_calls=0, error=None):
        self.binding = binding
        self.capabilities = binding.capabilities
        self._texts = list(texts)
        self._fail_calls = fail_calls
        self._error = error if error is not None else RuntimeError("不支持该 response_format")
        self.seen_formats = []
        self.messages_seen = []

    async def complete(self, messages, *, temperature, max_tokens, response_format=None):
        self.seen_formats.append(response_format)
        self.messages_seen.append(list(messages))
        if len(self.seen_formats) <= self._fail_calls:
            raise self._error
        text = self._texts.pop(0) if self._texts else "{}"
        return CompletionResult(text=text, provider=self.binding.provider,
                                model=self.binding.model,
                                prompt_tokens=1, completion_tokens=1, latency_ms=0)


async def test_level1_provider_error_degrades_to_level2_with_event():
    seen = []
    b = parse_alias("openai:gpt-4o-mini")
    p = DegradingProvider(b, [VALID], fail_calls=1)
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10, on_degrade=seen.append)
    assert r.score == 0.9
    assert p.seen_formats[0]["type"] == "json_schema"
    assert p.seen_formats[1] == {"type": "json_object"}
    assert len(seen) == 1
    assert (seen[0].provider, seen[0].from_level, seen[0].to_level) == ("openai", 1, 2)
    assert "不支持该 response_format" in seen[0].reason


async def test_level2_provider_error_degrades_to_level3_with_events():
    seen = []
    b = parse_alias("qwen:qwen-max")
    p = DegradingProvider(b, [VALID], fail_calls=1)
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10, on_degrade=seen.append)
    assert r.verdict == "approve"
    assert p.seen_formats == [{"type": "json_object"}, None]
    assert [(e.from_level, e.to_level) for e in seen] == [(1, 2), (2, 3)]


async def test_capability_driven_degrade_emits_single_event():
    seen = []
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, [VALID])
    await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                              temperature=0, max_tokens=10, on_degrade=seen.append)
    assert [(e.provider, e.from_level, e.to_level) for e in seen] == [("ernie", 1, 3)]
    assert seen[0].reason


# ── 降级链：校验失败同样降级（分辨率 2 的 failure）──

async def test_level2_validation_failure_degrades_to_level3():
    seen = []
    b = parse_alias("qwen:qwen-max")
    p = DegradingProvider(b, ['{"verdict": "approve"}', VALID])  # 首次缺 score
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10, on_degrade=seen.append)
    assert r.score == 0.9
    assert [(e.from_level, e.to_level) for e in seen] == [(1, 2), (2, 3)]
    assert p.seen_formats == [{"type": "json_object"}, None]
    assert "score" in seen[-1].reason


# ── 第 2 级：schema 内嵌提示词（分辨率 7）──

async def test_level2_embeds_schema_in_prompt_with_unicode():
    b = parse_alias("qwen:qwen-max")
    p = DegradingProvider(b, [json.dumps({"verdict": "approve"})])
    await complete_structured(p, [{"role": "user", "content": "x"}], Commented,
                              temperature=0, max_tokens=10)
    joined = " ".join(m["content"] for m in p.messages_seen[0])
    assert "评审结论：approve 或 reject" in joined  # ensure_ascii=False
    assert json.dumps(Commented.model_json_schema(), ensure_ascii=False) in joined


async def test_level1_does_not_inject_schema_into_prompt():
    b = parse_alias("openai:gpt-4o-mini")
    p = DegradingProvider(b, [VALID])
    await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                              temperature=0, max_tokens=10)
    assert p.messages_seen[0] == [{"role": "user", "content": "x"}]


# ── 修复循环 ──

async def test_repair_loop_fixes_invalid_then_succeeds():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, ['{"verdict": "approve"}',        # 缺 score
                             VALID])
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert r.score == 0.9 and len(p.seen_formats) == 2


async def test_repair_prompt_contains_validation_error():
    captured = []

    class Capturing(ScriptedProvider):
        async def complete(self, messages, *, temperature, max_tokens, response_format=None):
            captured.append(messages)
            return await super().complete(messages, temperature=temperature,
                                          max_tokens=max_tokens,
                                          response_format=response_format)

    b = parse_alias("ernie:ernie-4")
    p = Capturing(b, ['{"verdict": "approve"}', VALID])
    await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                              temperature=0, max_tokens=10)
    repair_msg = " ".join(m["content"] for m in captured[1])
    assert "score" in repair_msg or "校验" in repair_msg


async def test_repair_turn_preserves_conversation_and_exact_prompt():
    captured = []

    class Capturing(ScriptedProvider):
        async def complete(self, messages, *, temperature, max_tokens, response_format=None):
            captured.append(list(messages))
            return await super().complete(messages, temperature=temperature,
                                          max_tokens=max_tokens,
                                          response_format=response_format)

    b = parse_alias("ernie:ernie-4")
    p = Capturing(b, ['{"verdict": "approve"}', VALID])
    original = [{"role": "system", "content": "S"}, {"role": "user", "content": "x"}]
    await complete_structured(p, original, Answer, temperature=0, max_tokens=10)
    assert captured[0] == original                     # 原对话原样保留
    assert [m["role"] for m in captured[1]] == ["system", "user", "user"]
    repair_content = captured[1][-1]["content"]
    assert repair_content.startswith("你上次的输出校验失败：\n")
    assert repair_content.endswith("\n请仅返回修正后的 JSON，不要任何解释文字。")


async def test_caller_messages_list_not_mutated():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, ['{"verdict": "approve"}', VALID])
    original = [{"role": "user", "content": "x"}]
    await complete_structured(p, original, Answer, temperature=0, max_tokens=10)
    assert original == [{"role": "user", "content": "x"}]


async def test_extra_field_rejected_then_repaired():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, [json.dumps({"verdict": "approve", "score": 1.0, "x": 1}),
                             VALID])
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert r.verdict == "approve"


async def test_repair_attempts_zero_single_call_then_error():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, ["not json", VALID])
    with pytest.raises(StructuredOutputError) as ei:
        await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10, repair_attempts=0)
    assert len(p.seen_formats) == 1
    assert ei.value.attempts == 1
    assert ei.value.schema_name == "Answer"
    assert ei.value.last_text == "not json"
    assert ei.value.last_error is not None


async def test_exhausting_repairs_raises_with_diagnostics():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, ["not json", "still not json", "nope", "nope"])
    with pytest.raises(StructuredOutputError) as ei:
        await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10, repair_attempts=2)
    assert ei.value.schema_name == "Answer" and ei.value.last_text


async def test_on_degrade_called_when_falling_back():
    seen = []
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, [VALID])
    await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                              temperature=0, max_tokens=10, on_degrade=seen.append)
    assert seen and seen[0].to_level == 3


async def test_default_repair_attempts_means_three_calls():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, ["no", "no", "no", "unused"])
    with pytest.raises(StructuredOutputError) as ei:
        await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert len(p.seen_formats) == 3
    assert ei.value.attempts == 3
    assert ei.value.last_text == "no"


async def test_on_degrade_not_called_for_repair_retry():
    seen = []
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, ['{"verdict": "approve"}', VALID])
    await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                              temperature=0, max_tokens=10, on_degrade=seen.append)
    assert [e.to_level for e in seen] == [3] and len(p.seen_formats) == 2


# ── 分辨率 9：第 3 级的 provider 异常不伪装成校验失败 ──

async def test_level3_provider_error_propagates_without_repair():
    b = parse_alias("ernie:ernie-4")
    p = DegradingProvider(b, [VALID], fail_calls=99)
    with pytest.raises(RuntimeError) as ei:
        await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert not isinstance(ei.value, StructuredOutputError)
    assert len(p.seen_formats) == 1  # 不降级、不修复
