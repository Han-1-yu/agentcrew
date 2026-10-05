"""角色规格：角色是数据，不是代码（spec §5.4）。

``RoleSpec`` 是一份纯数据声明——新增角色只需提供一个 ``RoleSpec`` 值，不需要
新增任何 Python 子类；角色之间的行为差异由 ``system_prompt`` 与各字段承载。
"""

from pydantic import BaseModel, ConfigDict, Field


class RoleSpec(BaseModel):
    """一个角色的完整声明（spec §5.4）。

    ``reviewer_for`` 声明评审关系：列出的 key 是**默认评审本角色**的角色
    （如 ``backend_dev`` 声明 ``["architect"]``，表示后端任务默认由架构师评审）；
    特殊值 ``["*"]`` 表示声明者可评审任意角色（通用评审人）。
    ``RoleCatalog.reviewer_for`` 据此计算某角色的全部评审人，并负责过滤自评审
    与未注册 key。
    """

    model_config = ConfigDict(extra="forbid")

    key: str  # "backend_dev"
    title: str  # "后端开发工程师"
    goal: str  # 核心职责（对应 CrewAI 的 goal）
    backstory: str  # 背景设定（对应 backstory）
    responsibilities: list[str]  # 职责清单
    boundaries: list[str]  # 能力边界：明确不做什么
    output_contract: str  # 输出规范的自然语言描述
    system_prompt: str  # 系统提示词
    temperature: float = 0.3
    max_output_tokens: int = 8192
    model: str = "default"  # 模型别名，见 spec §12.2
    reviewer_for: list[str] = Field(default_factory=list)  # 默认评审本角色的 key
