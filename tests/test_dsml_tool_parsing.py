"""DeepSeek 私有工具标记的容错解析（2026-11-13 真机缺陷回归）。

背景：``deepseek-chat`` 在「提示词告知可用工具、请求未开原生 tools」时，会时而写
``<tool_call>``、时而漏出它自己的 ``<｜｜DSML｜｜ calls>`` 标记。后者不被解析时，
ReAct 循环会把这一轮判成「没有工具调用」直接退出，任务最终只报「缺少必需产物」，
从报告看不出病因。这组用例只钉「读得懂」，不改对模型宣告的协议。
"""

from __future__ import annotations

from agentcrew.agents.base_agent import _extract_tool_calls

_FLAT = """
<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="generate_image">
<｜｜DSML｜｜ parameter name="path" string="true">assets/cover.png</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="prompt" string="true">一只橘猫在敲代码</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="size" string="true">1024x1024</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>
"""

_JSON_ARGS = """
<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="generate_image">
<｜｜DSML｜｜ parameter name="args" string="false">{"path": "a/b.png", "prompt": "猫"}</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>
"""


def test_dsml_flat_parameters_are_parsed():
    calls = _extract_tool_calls(_FLAT)
    assert len(calls) == 1
    assert calls[0].name == "generate_image"
    assert calls[0].args == {
        "path": "assets/cover.png",
        "prompt": "一只橘猫在敲代码",
        "size": "1024x1024",
    }


def test_dsml_json_args_blob_is_unpacked():
    calls = _extract_tool_calls(_JSON_ARGS)
    assert len(calls) == 1
    assert calls[0].args == {"path": "a/b.png", "prompt": "猫"}


def test_dsml_and_fence_calls_coexist():
    raw = (
        '<tool_call>\n{"name": "list_files", "args": {"path": "."}}\n</tool_call>\n'
        + _FLAT
    )
    calls = _extract_tool_calls(raw)
    assert [c.name for c in calls] == ["list_files", "generate_image"]


def test_no_tool_markup_yields_no_calls():
    assert _extract_tool_calls("普通文字，没有任何工具调用") == []


def test_dsml_without_parameters_still_yields_call():
    raw = (
        "<｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="list_files">\n'
        "</｜｜DSML｜｜ invoke>\n"
        "</｜｜DSML｜｜ calls>\n"
    )
    calls = _extract_tool_calls(raw)
    assert len(calls) == 1 and calls[0].name == "list_files"
    assert calls[0].args == {}


def test_dsml_non_string_parameter_is_json_decoded():
    raw = (
        "<｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="generate_image">\n'
        '<｜｜DSML｜｜ parameter name="args" string="true">'
        + '{"path": "x.png"}'
        + "</｜｜DSML｜｜ parameter>\n"
        "</｜｜DSML｜｜ invoke>\n"
        "</｜｜DSML｜｜ calls>\n"
    )
    # string="true" 时按原样字符串：不进 args 对象，避免把「看起来像 JSON 的字符串」
    # 误当参数结构。
    calls = _extract_tool_calls(raw)
    assert calls[0].args == {"args": '{"path": "x.png"}'}
