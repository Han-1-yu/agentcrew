"""``GenericAgent``：不覆写任何钩子的通用角色（spec §8.3）。

自定义角色的 Agent 行为默认由它承载——提示词、输出契约、温度、模型绑定全部来自
``RoleSpec``，所以「新增角色是数据，不是代码」才成立。只有需要特殊解析逻辑的角色
才值得写子类；Task 25 的 8 个内置角色也大多只是声明 ``spec`` 而已。
"""

from agentcrew.agents.base_agent import BaseAgent


class GenericAgent(BaseAgent):
    """``BaseAgent`` 的最小具体实现：模板方法 + 全部默认钩子。"""
