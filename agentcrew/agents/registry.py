"""``AgentFactory``：role key → Agent 实例（spec §8.1/§8.3）。

``AGENT_CLASSES`` 是模块级的「role key → 专用 Agent 类」注册表：8 个内置角色的
类在此登记（Task 25），Task 29 的 ``Crew.register_role`` 为自定义角色追加。**未注册的 key 一律
回落到** :class:`~agentcrew.agents.generic.GenericAgent` —— 这正是 §8.3 的实现：
角色是数据（``RoleSpec``），只有需要特殊输出解析的角色才需要一个类。

实例按 role key 缓存：同一个 key 反复 ``get`` 返回同一实例（Agent 只持有只读依赖，
复用安全），调度器每个任务都要取一次 Agent，缓存省掉重复构造。
"""

from collections.abc import Callable

from agentcrew.agents.architect import ArchitectAgent
from agentcrew.agents.backend_dev import BackendDevAgent
from agentcrew.agents.base_agent import BaseAgent
from agentcrew.agents.devops_engineer import DevopsEngineerAgent
from agentcrew.agents.frontend_dev import FrontendDevAgent
from agentcrew.agents.generic import GenericAgent
from agentcrew.agents.product_manager import ProductManagerAgent
from agentcrew.agents.project_manager import ProjectManagerAgent
from agentcrew.agents.qa_engineer import QaEngineerAgent
from agentcrew.agents.technical_writer import TechnicalWriterAgent
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger

#: role key → 专用 Agent 类。8 个内置角色在此登记（键顺序与 ``BUILTIN_ROLES``
#: 一致）；未登记的 key 一律回落到 ``GenericAgent``。字典本身是模块级单例，
#: ``AgentFactory.register`` **不写**它（见该方法 docstring）。
AGENT_CLASSES: dict[str, type[BaseAgent]] = {
    "project_manager": ProjectManagerAgent,
    "product_manager": ProductManagerAgent,
    "architect": ArchitectAgent,
    "backend_dev": BackendDevAgent,
    "frontend_dev": FrontendDevAgent,
    "qa_engineer": QaEngineerAgent,
    "devops_engineer": DevopsEngineerAgent,
    "technical_writer": TechnicalWriterAgent,
}


def _generic_spec(role_key: str) -> RoleSpec:
    """未注册角色的兜底规格：行为完全交给任务描述与交付物契约。

    只出现在「任务引用了目录里没有的角色」时（例如自定义角色只出现在计划里、
    却没注册 ``RoleSpec``）：给一份诚实的通用提示词，让任务仍有机会跑完，
    而不是在取 Agent 时抛错把整次 run 打崩。
    """
    return RoleSpec(
        key=role_key,
        title=role_key,
        goal="按任务描述与交付物契约完成本次任务。",
        backstory="通用执行者：不预设专业背景，一切以任务契约为准。",
        responsibilities=["按任务描述产出契约声明的全部文件", "确保产物内容完整、可直接使用"],
        boundaries=["只产出任务交付物契约中声明的路径", "不做契约之外的假设与扩展"],
        output_contract="使用带 file= 路径的围栏代码块逐文件输出，路径必须与契约一致。",
        system_prompt=(
            "你是 AgentCrew 的通用执行者。严格按任务描述与交付物契约工作："
            "只产出契约中声明的文件，内容完整可直接使用；契约之外的内容一律不输出。"
        ),
    )


class AgentFactory:
    """构造并缓存每个角色的 Agent；未注册的角色用通用实现兜底。"""

    def __init__(
        self,
        models: ModelFactory,
        store: ArtifactStore,
        ledger: Ledger,
        project_goal: str,
        catalog: RoleCatalog,
        subtask_creator: Callable[[dict], str] | None = None,
    ) -> None:
        self._models = models
        self._store = store
        self._ledger = ledger
        self._project_goal = project_goal
        self._catalog = catalog
        #: 本工厂内的类覆盖（run 级）；``AGENT_CLASSES`` 是全局默认，二者在
        #: ``get()`` 里按「本地优先」合并。
        self._classes: dict[str, type[BaseAgent]] = {}
        self._agents: dict[str, BaseAgent] = {}
        #: P2-1：动态子任务回调；``setter`` 会同步刷新已缓存的 Agent 实例
        #: （调度器在交付打回后换板重入时，缓存实例仍指向旧面板的闭包）。
        self._subtask_creator: Callable[[dict], str] | None = subtask_creator

    @property
    def subtask_creator(self) -> Callable[[dict], str] | None:
        """当前子任务回调（``None`` = 不启用动态子任务）。"""
        return self._subtask_creator

    @subtask_creator.setter
    def subtask_creator(self, creator: Callable[[dict], str] | None) -> None:
        """更换子任务回调，并把它推给所有已缓存的 Agent 实例。

        Agent 按 role key 缓存、跨调度器循环复用；调度器换板（交付打回重建
        面板）后，旧闭包会把新任务加到已废弃的面板上——必须连缓存实例一起刷新。
        """
        self._subtask_creator = creator
        for agent in self._agents.values():
            agent.set_subtask_creator(creator)

    def register(self, role_key: str, cls: type[BaseAgent]) -> None:
        """为 ``role_key`` 指定专用 Agent 类；覆盖旧注册并丢弃已缓存实例。

        **不改**模块级 :data:`AGENT_CLASSES`：注册是 run 级行为（Task 29 的
        ``Crew.register_role``），写进全局字典会让同进程里的其他 run 受影响。
        """
        self._classes[role_key] = cls
        self._agents.pop(role_key, None)

    def get(self, role_key: str) -> BaseAgent:
        """返回该角色的 Agent（缓存命中即复用）。

        类按「本工厂注册 → 模块级 ``AGENT_CLASSES`` → ``GenericAgent``」解析，
        规格按「目录里有 → 目录里的 ``RoleSpec``；没有 → 兜底规格」解析。
        每次都从 ``AGENT_CLASSES`` 现读：Task 25/29 可能在工厂构造之后才注册。
        """
        cached = self._agents.get(role_key)
        if cached is not None:
            return cached
        cls = self._classes.get(role_key) or AGENT_CLASSES.get(role_key) or GenericAgent
        spec = (
            self._catalog.get(role_key)
            if self._catalog.has(role_key)
            else _generic_spec(role_key)
        )
        agent = cls(
            spec,
            models=self._models,
            store=self._store,
            ledger=self._ledger,
            project_goal=self._project_goal,
            # 目录随 Agent 下发：PM 的拆解提示词据此渲染**含自定义角色**的
            # 可用角色清单（§8.3 的「角色是数据」只有在模型看得见新角色时才成立）。
            catalog=self._catalog,
            subtask_creator=self._subtask_creator,
        )
        self._agents[role_key] = agent
        return agent

    def reviewer_agent_for(self, role_key: str) -> BaseAgent | None:
        """返回 ``role_key`` 的**首个**评审人 Agent；没有评审人时返回 ``None``。

        没有评审人（含未注册的角色）是合法状态：调用方据此跳过 G2，由 G1 定夺。
        多评审人（``project_manager`` 的 architect + product_manager 双评审）的
        遍历不在这里展开——那是 Task 26 ``Reviewer`` 的职责。
        """
        reviewers = self._catalog.reviewer_for(role_key)
        return self.get(reviewers[0]) if reviewers else None
