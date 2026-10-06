"""工具基类：Agent 在 ReAct 循环中可调用的能力单元（P1-1）。

一个工具是「名称 + 描述 + JSON Schema 入参 + 异步执行」的四元组。模型通过
``<tool_call>`` 围栏发起调用，:meth:`Tool.run` 返回文本结果，由 Agent 以
``<tool_result>`` 围栏回灌给模型。内置沙箱工具（读文件 / 列目录 / grep / 跑
Python）在 :mod:`agentcrew.tools.builtins`，外部 MCP 工具经
:class:`~agentcrew.tools.mcp_adapter.MCPToolAdapter` 适配到同一接口。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Tool(BaseModel):
    """一个可被 Agent 调用的工具。

    子类只须覆写 :meth:`run`；``name`` / ``description`` / ``input_schema``
    会被 :meth:`BaseAgent.build_messages` 渲染进系统提示词，作为模型选择工具的
    唯一依据。构造期依赖（如沙箱根目录、MCP 调用回调）声明为额外字段，不进
    ``input_schema``，因而对模型不可见。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """工具名：模型在 ``<tool_call>`` 的 ``name`` 字段里引用它。"""

    description: str
    """一句话说明：什么时候该用这个工具、它返回什么。"""

    input_schema: dict[str, Any] = Field(default_factory=dict)
    """入参的 JSON Schema（``type=object`` + ``properties`` + ``required``）。"""

    async def run(self, **kwargs: Any) -> str:
        """执行工具并返回文本结果。子类必须覆写。

        实现方应自行捕获内部异常并转成错误文本返回，还是抛出让 Agent 兜底？
        约定：**抛异常由 Agent 统一捕获**（转成 ``错误：…`` 文本回灌），工具内部
        只处理「正常路径上的业务失败」（如文件不存在）并以文本返回。
        """
        raise NotImplementedError
