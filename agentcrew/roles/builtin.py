"""8 个内置角色（spec §8.2）。

角色定义居各自的 ``agentcrew/agents/<key>.py`` 模块（每个模块同时导出该角色的
``ROLE`` 与 ``<Name>Agent``），本模块只按目录顺序汇总成 ``BUILTIN_ROLES``。
导入链保持单向无环：``roles/spec.py`` ← ``agents/*.py`` ← ``roles/builtin.py``；
``roles/spec.py`` 与 ``roles/catalog.py`` 不依赖 agents 层（「角色是数据」的边界）。

Task 25 之前这里内联着 8 份 ``RoleSpec``；迁移后不再有第二份副本，索引顺序
（注册顺序）与原 ``BUILTIN_ROLES`` 完全一致。
"""

from agentcrew.agents.architect import ROLE as ARCHITECT
from agentcrew.agents.backend_dev import ROLE as BACKEND_DEV
from agentcrew.agents.devops_engineer import ROLE as DEVOPS_ENGINEER
from agentcrew.agents.frontend_dev import ROLE as FRONTEND_DEV
from agentcrew.agents.product_manager import ROLE as PRODUCT_MANAGER
from agentcrew.agents.project_manager import ROLE as PROJECT_MANAGER
from agentcrew.agents.qa_engineer import ROLE as QA_ENGINEER
from agentcrew.agents.technical_writer import ROLE as TECHNICAL_WRITER
from agentcrew.roles.spec import RoleSpec

BUILTIN_ROLES: tuple[RoleSpec, ...] = (
    PROJECT_MANAGER,
    PRODUCT_MANAGER,
    ARCHITECT,
    BACKEND_DEV,
    FRONTEND_DEV,
    QA_ENGINEER,
    DEVOPS_ENGINEER,
    TECHNICAL_WRITER,
)
