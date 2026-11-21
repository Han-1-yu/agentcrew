"""角色 → 执行体的解析结果（RunPlan）。

配置是"声明"，``RunPlan`` 是"本次 run 落地后的样子"：别名已定、工具已收窄、
路径域已确定。它写进 ``run.json`` 的 ``config_snapshot``，也是 UI 预览与
"这次为什么用了这个模型"的唯一依据。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.config import Settings
from agentcrew.roles.binding import (
    AgentProfile,
    BindingError,
    Duty,
    RoleAgents,
    resolve_binding,
    validate_bindings,
)
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec


class ResolvedAgent(BaseModel):
    """一个已经解析完毕的执行体（可以直接拿去构造 Agent）。"""

    model_config = ConfigDict(extra="forbid")

    role: str
    profile: str
    alias: str
    duty: Duty
    tools: list[str] = Field(default_factory=list)
    path_scope: list[str] = Field(default_factory=list)
    weight: float = 1.0


class RunPlan(BaseModel):
    """一次 run 的完整编排计划。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    agents: list[ResolvedAgent] = Field(default_factory=list)

    def for_role(self, role: str) -> list[ResolvedAgent]:
        return [a for a in self.agents if a.role == role]

    def primaries(self, role: str) -> list[ResolvedAgent]:
        return [a for a in self.for_role(role)
                if a.duty in (Duty.PRIMARY, Duty.SECTION)]

    def reviewers(self, role: str) -> list[ResolvedAgent]:
        return [a for a in self.for_role(role) if a.duty is Duty.REVIEW]

    def judge(self, role: str) -> ResolvedAgent | None:
        for agent in self.for_role(role):
            if agent.duty is Duty.JUDGE:
                return agent
        return None

    def to_snapshot(self) -> list[dict[str, object]]:
        return [a.model_dump(mode="json") for a in self.agents]


def binding_for(spec: RoleSpec, settings: Settings) -> RoleAgents:
    """取一个角色的绑定：``agentcrew.yaml`` 覆盖 ``roles.d`` 里的内联声明。

    实际规则在 :func:`agentcrew.roles.binding.resolve_binding`，这里保留旧签名
    供 crew / cli 使用；roles 层是纯数据层，不反向依赖 agents。
    """
    return resolve_binding(spec, settings.role_agents)


def profile_for(settings: Settings, ref: str) -> AgentProfile:
    profile = settings.agent_profiles.get(ref)
    if profile is None:
        raise BindingError(f"未定义的 agent profile：{ref}")
    if profile.key != ref:
        profile = profile.model_copy(update={"key": ref})
    return profile


def narrow_tools(role_tools: list[str], profile: AgentProfile | None) -> list[str]:
    """角色工具集 ∩ agent 工具集。``*`` 表示全集，交集时退化为对方的显式列表。"""
    if profile is None or profile.tools is None:
        return list(role_tools)
    if "*" in role_tools:
        return list(profile.tools)
    return [t for t in profile.tools if t in role_tools]


def alias_for(spec: RoleSpec, profile: AgentProfile | None, settings: Settings) -> str:
    """按既有优先级解析别名：profile > RoleSpec.model > role_bindings > 默认。"""
    if profile is not None and profile.model:
        return profile.model
    if spec.model and spec.model != "default":
        return spec.model
    return settings.role_bindings.get(spec.key) or settings.default_model


def resolve_plan(
    settings: Settings, catalog: RoleCatalog, run_id: str,
    *, overrides: dict[str, RoleAgents] | None = None,
) -> RunPlan:
    """把配置解析成 RunPlan；配置非法时抛 ``BindingError``（启动期 fail fast）。

    ``overrides`` 是运行中切换留下的覆盖（role → 绑定），优先级高于配置文件：
    resume 时必须按"切换后的样子"重建计划，否则切了等于没切。
    """
    plan = RunPlan(run_id=run_id)
    # 档案文件损坏时给出「哪份文件坏了」，而不是只在报错里说「未定义」——
    # 后者会让用户去翻配置找拼写错误，实际问题是文件没加载成功。
    broken = {
        str(failure.get("key")): failure
        for failure in settings.profile_errors
        if failure.get("key")
    }
    for spec in catalog.all():
        binding = (overrides or {}).get(spec.key) or binding_for(spec, settings)
        for bound in binding.agents:
            failure = broken.get(bound.ref)
            if failure is not None and bound.ref not in settings.agent_profiles:
                raise BindingError(
                    f"角色 {spec.key} 绑定的执行体 {bound.ref} 加载失败："
                    f"{failure.get('file')}：{failure.get('message')}")
        validate_bindings(spec.key, binding, settings.agent_profiles, spec.allowed_tools)
        if not binding.agents:
            # 未绑定：维持原行为——角色直接用一个默认执行体
            plan.agents.append(ResolvedAgent(
                role=spec.key, profile="default", alias=alias_for(spec, None, settings),
                duty=Duty.PRIMARY, tools=list(spec.allowed_tools), path_scope=[]))
            continue
        for bound in binding.agents:
            profile = profile_for(settings, bound.ref)
            plan.agents.append(ResolvedAgent(
                role=spec.key, profile=bound.ref, alias=alias_for(spec, profile, settings),
                duty=bound.duty, tools=narrow_tools(spec.allowed_tools, profile),
                path_scope=list(profile.path_scope), weight=bound.weight))
    return plan


def plan_spec(spec: RoleSpec, agent: ResolvedAgent) -> RoleSpec:
    """把一个执行体叠加到角色规格上，得到该执行体专属的 ``RoleSpec``。

    走 ``model_copy`` 而不是新建：角色其余字段（职责、边界、提示词）保持不变，
    差异只落在模型、工具与采样参数上——这正是"角色是数据、执行体是配置"的落点。
    """
    profile_fields: dict[str, object] = {
        "model": agent.alias,
        "allowed_tools": list(agent.tools) or ["*"],
    }
    return spec.model_copy(update=profile_fields)


@dataclass(frozen=True)
class _Pick:
    agent: ResolvedAgent
    reason: str


def pick_primary(
    agents: list[ResolvedAgent], *, run_id: str, task_id: str, attempt: int, role: str
) -> ResolvedAgent:
    """在 primary/section 里按权重选一个；同样的输入永远选同一个。

    种子含 run/task/attempt/role：resume 重派、mock 回放都不会"换一个人"，
    否则产物与测试结果无法比对。
    """
    if not agents:
        raise BindingError(f"角色 {role} 没有可用的 primary agent")
    if len(agents) == 1:
        return agents[0]
    seed = hashlib.sha1(
        f"{run_id}|{task_id}|{attempt}|{role}".encode()).hexdigest()[:8]
    rng = random.Random(int(seed, 16))
    total = sum(a.weight for a in agents)
    roll = rng.uniform(0, total)
    upto = 0.0
    for agent in agents:
        upto += agent.weight
        if roll <= upto:
            return agent
    return agents[-1]
