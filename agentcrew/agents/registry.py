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
from agentcrew.agents.plan import (
    ResolvedAgent,
    RunPlan,
    alias_for,
    binding_for,
    narrow_tools,
    profile_for,
    resolve_plan,
)
from agentcrew.agents.product_manager import ProductManagerAgent
from agentcrew.agents.project_manager import ProjectManagerAgent
from agentcrew.agents.qa_engineer import QaEngineerAgent
from agentcrew.agents.team import TeamAgent
from agentcrew.agents.technical_writer import TechnicalWriterAgent
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.binding import Aggregation, BoundAgent, Duty, RoleAgents
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
        subtask_creator: Callable[[dict], str | None] | None = None,
        settings: Settings | None = None,
        run_id: str = "",
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
        self._subtask_creator: Callable[[dict], str | None] | None = subtask_creator
        #: 多 agent：配置来自 Settings；未注入时等价于"每个角色一个默认执行体"。
        self._settings = settings
        self._run_id = run_id
        self._plan: RunPlan | None = None
        self._teams: dict[str, TeamAgent] = {}
        #: 运行中切换：role → 覆盖后的绑定（进程内生效，可整体导出持久化）。
        self._binding_overrides: dict[str, RoleAgents] = {}

    @property
    def subtask_creator(self) -> Callable[[dict], str | None] | None:
        """当前子任务回调（``None`` = 不启用动态子任务）。"""
        return self._subtask_creator

    @subtask_creator.setter
    def subtask_creator(self, creator: Callable[[dict], str | None] | None) -> None:
        """更换子任务回调，并把它推给所有已缓存的 Agent 实例。

        Agent 按 role key 缓存、跨调度器循环复用；调度器换板（交付打回重建
        面板）后，旧闭包会把新任务加到已废弃的面板上——必须连缓存实例一起刷新。
        """
        self._subtask_creator = creator
        for agent in self._agents.values():
            agent.set_subtask_creator(creator)

    def switch_agent(
        self, role_key: str, profile: str, *, duty: Duty = Duty.PRIMARY,
        reason: str = "",
    ) -> list[ResolvedAgent]:
        """把某个角色的 primary 换成另一个已解析的执行体（下一次派发生效）。

        只接受 **RunPlan 里已存在** 的 profile：随便写一个没解析过的名字会让
        模型/工具/路径域全部落空，属于配置错误，直接拒绝而不是运行期兜底。
        在途任务不受影响——中断它等于把已经花掉的 token 扔掉。
        """
        plan = self.plan()
        if plan is None:
            raise ValueError("当前 run 没有解析出编排计划，无法切换 agent")
        target = next((a for a in plan.for_role(role_key) if a.profile == profile), None)
        if target is None and self._settings is not None:
            # 允许切到"已定义但当前没绑"的执行体：否则想临时换人必须改配置文件重启
            prof = self._settings.agent_profiles.get(profile)
            if prof is not None:
                spec = (self._catalog.get(role_key)
                        if self._catalog.has(role_key) else _generic_spec(role_key))
                target = ResolvedAgent(
                    role=role_key, profile=profile,
                    alias=alias_for(spec, prof, self._settings), duty=duty,
                    tools=narrow_tools(spec.allowed_tools, prof),
                    path_scope=list(prof.path_scope))
        if target is None:
            available = [a.profile for a in plan.for_role(role_key)]
            raise ValueError(
                f"角色 {role_key} 没有已解析的 agent：{profile}"
                f"（可选：{', '.join(available) or '无'}）")

        previous = next(
            (a.profile for a in plan.for_role(role_key) if a.duty is duty), "")
        kept = [a for a in plan.for_role(role_key)
                if a.duty is not duty and a.profile != profile]
        switched = target.model_copy(update={"duty": duty})
        plan.agents = [a for a in plan.agents if a.role != role_key] + kept + [switched]
        self._binding_overrides[role_key] = RoleAgents(
            agents=[BoundAgent(ref=a.profile, duty=a.duty, weight=a.weight) for a in kept]
                   + [BoundAgent(ref=profile, duty=duty)],
            aggregation=(Aggregation(mode="primary_with_review")
                         if any(a.duty is Duty.REVIEW for a in kept) else None))
        self._teams.pop(role_key, None)
        for key in [k for k in self._agents if k.split("\u0000", 1)[0] == role_key]:
            self._agents.pop(key, None)
        self._ledger.emit(
            "agent_switched", role=role_key, from_profile=previous,
            to=profile, duty=duty.value, reason=reason)
        return list(kept) + [switched]

    def reset_agent(self, role_key: str) -> tuple[list[ResolvedAgent], bool]:
        """撤掉某角色的运行中覆盖，回到配置文件里的绑定。

        幂等：本来就没覆盖时返回 ``changed=False`` 且不改任何状态——调用方
        （Studio 的「恢复默认」）不需要先查"到底切过没切过"。
        """
        previous = ""
        if role_key in self._binding_overrides:
            current = (self.plan() or RunPlan(run_id=self._run_id)).for_role(role_key)
            previous = next((a.profile for a in current if a.duty is Duty.PRIMARY), "")
        changed = self._binding_overrides.pop(role_key, None) is not None
        self._teams.pop(role_key, None)
        for key in [k for k in self._agents if k.split("\u0000", 1)[0] == role_key]:
            self._agents.pop(key, None)
        if changed:
            self._plan = None          # 重新按配置解析
            self._ledger.emit(
                "agent_binding_reset", role=role_key, from_profile=previous)
        plan = self.plan() or RunPlan(run_id=self._run_id)
        return list(plan.for_role(role_key)), changed

    def export_bindings(self) -> dict[str, dict]:
        """导出运行中切换的结果，供落盘后 resume 复用。"""
        return {
            role: {"agents": [b.model_dump(mode="json") for b in binding.agents],
                   "aggregation": (binding.aggregation.model_dump(mode="json")
                                   if binding.aggregation else None)}
            for role, binding in self._binding_overrides.items()
        }

    def import_bindings(self, payload: dict[str, dict]) -> int:
        """从落盘的覆盖恢复；格式不合法就跳过并继续（不让坏文件废掉一次 resume）。"""
        applied = 0
        for role, raw in (payload or {}).items():
            try:
                self._binding_overrides[role] = RoleAgents.model_validate(raw)
                self._teams.pop(role, None)
                applied += 1
            except Exception:  # noqa: BLE001 —— 单个条目坏掉不该拖垮整个 resume
                continue
        if applied:
            self._plan = None   # 重新解析，让覆盖进入 RunPlan
        return applied

    def register(self, role_key: str, cls: type[BaseAgent]) -> None:
        """为 ``role_key`` 指定专用 Agent 类；覆盖旧注册并丢弃已缓存实例。

        **不改**模块级 :data:`AGENT_CLASSES`：注册是 run 级行为（Task 29 的
        ``Crew.register_role``），写进全局字典会让同进程里的其他 run 受影响。
        """
        self._classes[role_key] = cls
        for key in [k for k in self._agents if k.split("\u0000", 1)[0] == role_key]:
            self._agents.pop(key, None)
        self._teams.pop(role_key, None)

    def plan(self) -> RunPlan | None:
        """解析一次本次 run 的编排计划（无 Settings 时为 None）。"""
        if self._plan is None and self._settings is not None:
            self._plan = resolve_plan(
                self._settings, self._catalog, self._run_id,
                overrides=self._binding_overrides)
        return self._plan

    def get(self, role_key: str, profile: str | None = None) -> BaseAgent | TeamAgent:
        """返回该角色的 Agent（缓存命中即复用）。

        传 ``profile`` 时返回该执行体专属的实例（多 agent 路径）；
        不传且角色绑定了多个执行体时，返回一个 ``TeamAgent`` 负责分流与汇总。

        类按「本工厂注册 → 模块级 ``AGENT_CLASSES`` → ``GenericAgent``」解析，
        规格按「目录里有 → 目录里的 ``RoleSpec``；没有 → 兜底规格」解析。
        每次都从 ``AGENT_CLASSES`` 现读：Task 25/29 可能在工厂构造之后才注册。
        """
        cache_key = f"{role_key}\u0000{profile or ''}"
        cached = self._agents.get(cache_key)
        if cached is not None:
            return cached

        if profile is None and self._settings is not None:
            spec_obj = (self._catalog.get(role_key)
                        if self._catalog.has(role_key) else _generic_spec(role_key))
            binding = (self._binding_overrides.get(role_key)
                       or binding_for(spec_obj, self._settings))
            if binding.is_multi:
                team = self._teams.get(role_key)
                if team is None:
                    members = (self.plan() or RunPlan(run_id=self._run_id)).for_role(role_key)
                    team = TeamAgent(
                        spec_obj, binding, members,
                        get_agent=lambda p: self.get(role_key, p),
                        models=self._models, store=self._store, ledger=self._ledger,
                        settings=self._settings, run_id=self._run_id)
                    self._teams[role_key] = team
                return team
        cls = self._classes.get(role_key) or AGENT_CLASSES.get(role_key) or GenericAgent
        spec = (
            self._catalog.get(role_key)
            if self._catalog.has(role_key)
            else _generic_spec(role_key)
        )
        resolved = None
        if profile and self._settings is not None:
            resolved = next(
                (a for a in (self.plan() or RunPlan(run_id=self._run_id)).for_role(role_key)
                 if a.profile == profile), None)
            if resolved is None:
                raise ValueError(f"角色 {role_key} 没有已解析的 agent：{profile}")
            prof = profile_for(self._settings, profile)
            spec = spec.model_copy(update={
                "model": resolved.alias,
                "allowed_tools": list(resolved.tools) or ["*"],
                **({"temperature": prof.temperature} if prof.temperature is not None else {}),
                **({"max_output_tokens": prof.max_output_tokens}
                   if prof.max_output_tokens is not None else {}),
            })
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
        if resolved is not None:
            agent.path_scope = list(resolved.path_scope)
            agent.agent_key = resolved.profile
            agent.cost_ceiling_usd = prof.cost_ceiling_usd
            agent.max_calls_per_task = prof.max_calls_per_task
            agent.fallback_models = list(prof.fallback)
            agent.max_concurrency = prof.max_concurrency
            agent.max_rpm = prof.max_rpm
        self._agents[cache_key] = agent
        return agent

    def reviewer_agent_for(self, role_key: str) -> BaseAgent | None:
        """返回 ``role_key`` 的**首个**评审人 Agent；没有评审人时返回 ``None``。

        没有评审人（含未注册的角色）是合法状态：调用方据此跳过 G2，由 G1 定夺。
        多评审人（``project_manager`` 的 architect + product_manager 双评审）的
        遍历不在这里展开——那是 Task 26 ``Reviewer`` 的职责。
        """
        reviewers = self._catalog.reviewer_for(role_key)
        return self.get(reviewers[0]) if reviewers else None
