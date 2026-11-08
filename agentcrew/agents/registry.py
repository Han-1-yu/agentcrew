"""``AgentFactory``：role key → Agent 实例（spec §8.1/§8.3）。

``AGENT_CLASSES`` 是模块级的「role key → 专用 Agent 类」注册表：8 个内置角色的
类在此登记（Task 25），Task 29 的 ``Crew.register_role`` 为自定义角色追加。**未注册的 key 一律
回落到** :class:`~agentcrew.agents.generic.GenericAgent` —— 这正是 §8.3 的实现：
角色是数据（``RoleSpec``），只有需要特殊输出解析的角色才需要一个类。

实例按 role key 缓存：同一个 key 反复 ``get`` 返回同一实例（Agent 只持有只读依赖，
复用安全），调度器每个任务都要取一次 Agent，缓存省掉重复构造。
"""

from collections.abc import Callable
from typing import Any

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
from agentcrew.tools.registry import ToolRegistry

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
        tool_registry: ToolRegistry | None = None,
        tool_guard: Any | None = None,
        stream: bool = False,
        stream_sink: Callable[[str, str, str], None] | None = None,
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
        #: R3：run 级工具注册表。``None``（默认）时所有 Agent 走「单次调用」
        #: 的原有路径；Crew 在 ``tools_enabled`` 开启时注入一份。
        self._tool_registry = tool_registry
        #: P2-4：工具级 guardrails。``None``（默认 / 未配置规则）时 Agent 侧
        #: 每次工具调用只多一次 ``None`` 判断。
        self._tool_guard = tool_guard
        #: P3-3：流式开关。``False``（默认）时每个 Agent 走 ``models.call``；
        #: ``True`` 时走 ``stream_call`` 并记 ``agent_stream_chunk``。
        self._stream = stream
        #: P3-3：流式增量正文出口，透传给每个 Agent（``None`` = 不推正文）。
        self._stream_sink = stream_sink
        #: R5：跨角色咨询路由。``None``（默认）时不检测 ``<message>`` 围栏。
        #: 实际类型要么是二参回调，要么是提供 ``bind()`` 的 PeerRouter（本身
        #: 不可调用），所以按 duck typing 存 ``Any``，由 _bind_router 统一解析。
        self._message_router: Any | None = None
        #: 运行中切换：role → 覆盖后的绑定（进程内生效，可整体导出持久化）。
        self._binding_overrides: dict[str, RoleAgents] = {}
        #: 任务级覆盖：task_id → {"role", "profile"}。只影响这一次任务的派发，
        #: 不写进 Plan（否则 resume 后分不清"配置"与"临时覆盖"）。
        self._task_overrides: dict[str, dict[str, str]] = {}

    @property
    def subtask_creator(self) -> Callable[[dict], str | None] | None:
        """当前子任务回调（``None`` = 不启用动态子任务）。"""
        return self._subtask_creator

    @subtask_creator.setter
    def subtask_creator(self, creator: Callable[[dict], str | None] | None) -> None:
        """更换子任务回调，并把它推给所有已缓存的 Agent 实例。

        Agent 按 role key 缓存、跨调度器循环复用；调度器换板（交付打回重建
        面板）后，旧闭包会把新任务加到已废弃的面板上——必须连缓存实例一起刷新。
        setter 与 getter 相邻定义，mypy 才能把它识别成可写 property。
        """
        self._subtask_creator = creator
        for agent in self._agents.values():
            agent.set_subtask_creator(creator)

    @property
    def message_router(self) -> Any | None:
        """当前跨角色咨询路由（``None`` = 不启用 ``<message>`` 对话）。"""
        return self._message_router

    @message_router.setter
    def message_router(self, router: Any | None) -> None:
        """更换路由，并推给所有已缓存的 Agent 实例（R5）。

        与 :attr:`subtask_creator` 同理：Agent 按 role key 缓存、跨调度器循环
        复用，只改工厂字段而不刷新缓存，已经在内存里的实例会一直拿着旧的
        ``None``，对话能力等于没开。

        路由若提供 ``bind``（见 :class:`~agentcrew.agents.peer.PeerRouter`），
        按实例所属角色重新绑定 ``from_role``——共享一个路由对象时，发问方身份
        只能在这里补。
        """
        self._message_router = router
        for cache_key, agent in self._agents.items():
            setter = getattr(agent, "set_message_router", None)
            if setter is not None:
                setter(self._bind_router(cache_key.split("\u0000", 1)[0]))

    def _bind_router(self, role_key: str) -> Any:
        """把共享路由按角色绑定成二参回调；没有路由时返回 ``None``。"""
        router = self._message_router
        if router is None:
            return None
        binder = getattr(router, "bind", None)
        return binder(role_key) if binder is not None else router

    def has_role(self, role_key: str) -> bool:
        """目录里是否有这个角色（拼错的角色名不该白花一次模型调用）。"""
        return self._catalog.has(role_key)

    async def consult(
        self, role_key: str, question: str, *, from_role: str = "",
        context: str = "",
    ) -> str:
        """让 ``role_key`` 对 ``question`` 作一次轻量作答（R5）。

        ``context``（R6）是提问方的任务背景，原样透传给被问角色的
        :meth:`~agentcrew.agents.base_agent.BaseAgent.consult`。

        多执行体角色（``TeamAgent``）没有自己的提示词——取它的 primary 成员来
        回答，而不是让整支队伍做一次聚合执行（那是任务级行为，成本高一个量级）。
        """
        agent = self.get(role_key)
        if isinstance(agent, TeamAgent):
            primary = next(
                (m for m in agent.members
                 if m.duty in (Duty.PRIMARY, Duty.SECTION)), None)
            if primary is None:
                primary = agent.members[0] if agent.members else None
            if primary is not None:
                agent = self.get(role_key, primary.profile)
        consult_fn = getattr(agent, "consult", None)
        if consult_fn is None:  # 理论上不会发生；不静默炸掉提问方
            return "（该角色暂不支持咨询：请按最合理的默认值继续。）"
        return await consult_fn(question, from_role=from_role, context=context)

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
        if target is None:
            # 允许切到"已定义但当前没绑"的执行体：否则想临时换人必须改配置文件重启
            target = self._member_from_settings(role_key, profile, duty)
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

    def _member_from_settings(
        self, role_key: str, profile: str, duty: Duty = Duty.PRIMARY
    ) -> ResolvedAgent | None:
        """把"已定义但当前没绑定"的执行体解析成成员；未定义返回 ``None``。

        ``switch_agent`` 与任务级覆盖都用它，保证两条路对"能不能切到某人"
        给出同一个答案。
        """
        if self._settings is None:
            return None
        prof = self._settings.agent_profiles.get(profile)
        if prof is None:
            return None
        spec = (self._catalog.get(role_key)
                if self._catalog.has(role_key) else _generic_spec(role_key))
        return ResolvedAgent(
            role=role_key, profile=profile,
            alias=alias_for(spec, prof, self._settings), duty=duty,
            tools=narrow_tools(spec.allowed_tools, prof),
            path_scope=list(prof.path_scope))

    def agent_for_task(self, role_key: str, task_id: str) -> BaseAgent | TeamAgent:
        """派发入口：**任务级覆盖 > 角色绑定**。

        覆盖只作用于这一个任务（含它的每次 attempt 与返工），别的任务照旧。
        目标在执行前失效（配置被删/改名）时不静默失败也不炸掉 run：丢掉这条
        覆盖、记 ``task_agent_override_dropped``，按角色默认继续。
        """
        override = self._task_overrides.get(task_id) or {}
        profile = override.get("profile") or ""
        if not profile:
            return self.get(role_key)
        try:
            return self.get(role_key, profile)
        except ValueError:
            self._task_overrides.pop(task_id, None)
            self._ledger.emit(
                "task_agent_override_dropped", task_id=task_id,
                role=role_key, profile=profile)
            return self.get(role_key)

    def set_task_override(
        self, role_key: str, task_id: str, profile: str, *, reason: str = ""
    ) -> ResolvedAgent:
        """给单个任务指定执行体；非法 profile 当场拒绝（不在派发时才炸）。"""
        plan = self.plan()
        target = (
            next((a for a in plan.for_role(role_key) if a.profile == profile), None)
            if plan is not None else None)
        if target is None:
            target = self._member_from_settings(role_key, profile)
        if target is None:
            available = ([a.profile for a in plan.for_role(role_key)]
                         if plan is not None else [])
            raise ValueError(
                f"角色 {role_key} 没有可用的 agent：{profile}"
                f"（可选：{', '.join(available) or '无'}）")
        previous = (self._task_overrides.get(task_id) or {}).get("profile", "")
        self._task_overrides[task_id] = {"role": role_key, "profile": profile}
        self._ledger.emit(
            "task_agent_switched", task_id=task_id, role=role_key,
            from_profile=previous, to=profile, reason=reason)
        return target

    def reset_task_override(self, task_id: str) -> tuple[str, bool]:
        """撤销任务级覆盖（幂等）。返回被撤销的 profile 与是否真的改过。"""
        previous = self._task_overrides.pop(task_id, None)
        if previous is None:
            return "", False
        self._ledger.emit(
            "task_agent_reset", task_id=task_id,
            role=previous.get("role", ""), from_profile=previous.get("profile", ""))
        return previous.get("profile", ""), True

    def export_task_overrides(self) -> dict[str, dict[str, str]]:
        return {k: dict(v) for k, v in self._task_overrides.items()}

    def import_task_overrides(self, payload: dict[str, dict]) -> int:
        """从落盘结果恢复；单条格式坏了就跳过（不让坏文件废掉一次 resume）。"""
        applied = 0
        for task_id, raw in (payload or {}).items():
            if not isinstance(raw, dict):
                continue
            profile = str(raw.get("profile") or "")
            if not task_id or not profile:
                continue
            self._task_overrides[str(task_id)] = {
                "role": str(raw.get("role") or ""), "profile": profile}
            applied += 1
        return applied

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
                # 任务级覆盖可能指向"已定义但当前没绑"的执行体，与 switch 一致
                resolved = self._member_from_settings(role_key, profile)
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
            # R8：子任务被拒时把原因回灌给模型（0 = 关闭，等价 R8 之前）。
            max_subtask_corrections=(
                self._settings.max_subtask_corrections
                if self._settings is not None else 1),
            # R3：工具链只在显式开启时下发；None 与改造前逐字一致。
            tool_registry=self._tool_registry,
            # P2-4：guardrails 与工具链同生共死——没有工具就谈不上拦工具。
            tool_guard=self._tool_guard,
            # R5：对话路由同理——None 时系统提示词里连这段能力都不会出现。
            message_router=self._bind_router(role_key),
            # P3-3：流式开关。此前 BaseAgent(stream=...) 只有测试在传，生产路径
            # 永远 False——现在由 Settings.stream 一路下发，成为真能力。
            stream=self._stream,
            # P3-3 → UI：流式增量正文出口（None = 不推正文，仅 ledger 摘要）。
            stream_sink=self._stream_sink,
        )
        if resolved is not None:
            agent.path_scope = list(resolved.path_scope)
            agent.agent_key = resolved.profile
            agent.cost_ceiling_usd = prof.cost_ceiling_usd
            agent.max_calls_per_task = prof.max_calls_per_task
            agent.fallback_models = list(prof.fallback)
            agent.max_concurrency = prof.max_concurrency
            agent.max_rpm = prof.max_rpm
            agent.run_cost_ceiling_usd = prof.run_cost_ceiling_usd
        self._agents[cache_key] = agent
        return agent

    def reviewer_agent_for(self, role_key: str) -> BaseAgent | TeamAgent | None:
        """返回 ``role_key`` 的**首个**评审人 Agent；没有评审人时返回 ``None``。

        没有评审人（含未注册的角色）是合法状态：调用方据此跳过 G2，由 G1 定夺。
        多评审人（``project_manager`` 的 architect + product_manager 双评审）的
        遍历不在这里展开——那是 Task 26 ``Reviewer`` 的职责。
        """
        reviewers = self._catalog.reviewer_for(role_key)
        return self.get(reviewers[0]) if reviewers else None
