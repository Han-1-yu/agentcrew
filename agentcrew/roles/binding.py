"""角色绑定多 Agent：绑定与执行体的数据模型（纯数据层，不依赖 agents）。

分工：
- ``AgentProfile`` 描述**执行体**（用哪个模型、能碰哪些工具与路径、花多少钱）；
- ``RoleAgents`` 描述**绑定**（角色挂哪些执行体、各自担什么 duty、怎么汇总）；
- 校验全部放在启动期（fail fast）：配置错误不应该等到跑了半小时才暴露。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Duty(str, Enum):
    """执行体在一个角色里承担的责任。

    只有 ``PRIMARY``/``SECTION`` 能写正式产物；``REVIEW`` 只出意见；
    ``JUDGE`` 只做择优，不参赛。
    """

    PRIMARY = "primary"
    SECTION = "section"
    REVIEW = "review"
    JUDGE = "judge"


class AgentProfile(BaseModel):
    """一个可被角色调用的执行体（纯配置，不含职责）。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    title: str = ""
    model: str = ""
    """模型别名；留空表示沿用角色的 ``model`` / ``role_bindings`` / 默认模型。"""

    fallback: list[str] = Field(default_factory=list)
    tools: list[str] | None = None
    """工具白名单；``None`` = 继承角色。列出时取与角色白名单的**交集**（只能收窄）。"""

    path_scope: list[str] = Field(default_factory=list)
    """允许写的前缀（目录以 ``/`` 结尾）。空表示不限，但仍受任务契约约束。"""

    cost_ceiling_usd: float | None = None
    max_calls_per_task: int | None = None
    max_rpm: int | None = Field(default=None, ge=1)
    """本执行体每分钟最多几次调用（60 秒滑动窗口）；``None`` = 只受 provider 约束。

    与 ``max_concurrency`` 互补：并发管"同时几个"，RPM 管"一分钟几个"。
    廉价档常被上游按 RPM 计费/限流，只用并发挡不住。
    """

    max_concurrency: int | None = Field(default=None, ge=1)
    """本执行体同时在飞的 LLM 调用上限；``None`` = 只受 provider/全局约束。

    场景：一个慢而贵的主笔不该把并发槽位占满，导致复核 agent 排队干等；
    或者反过来，便宜 agent 想跑得比 provider 默认更保守。
    """
    temperature: float | None = None
    max_output_tokens: int | None = None

    def allows(self, path: str) -> bool:
        """路径是否落在本执行体的写入范围内（空前缀 = 不限）。"""
        if not self.path_scope:
            return True
        normalized = path.replace("\\", "/").lstrip("./")
        return any(normalized.startswith(scope) for scope in self.path_scope)


class BoundAgent(BaseModel):
    """角色 → 执行体的一条绑定。"""

    model_config = ConfigDict(extra="forbid")

    ref: str
    duty: Duty = Duty.PRIMARY
    weight: float = 1.0
    on_failure: Literal["fail", "degrade"] = "fail"


class Aggregation(BaseModel):
    """多执行体的汇总方式。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["primary_with_review", "split_by_path", "best_of", "merge"] = (
        "primary_with_review")
    quorum: int = 1
    judge: str | None = None


class RoleAgents(BaseModel):
    """一个角色的全部绑定 + 汇总策略。"""

    model_config = ConfigDict(extra="forbid")

    agents: list[BoundAgent] = Field(default_factory=list)
    aggregation: Aggregation | None = None

    @property
    def primaries(self) -> list[BoundAgent]:
        return [a for a in self.agents if a.duty in (Duty.PRIMARY, Duty.SECTION)]

    @property
    def reviewers(self) -> list[BoundAgent]:
        return [a for a in self.agents if a.duty is Duty.REVIEW]

    @property
    def judge(self) -> BoundAgent | None:
        for agent in self.agents:
            if agent.duty is Duty.JUDGE:
                return agent
        return None

    @property
    def is_multi(self) -> bool:
        """是否需要走团队执行器（单 agent 走原路径，行为不变）。"""
        return len(self.agents) > 1


class BindingError(ValueError):
    """绑定配置非法（启动期抛出，对应退出码 3）。"""


def validate_bindings(
    role_key: str,
    binding: RoleAgents,
    profiles: dict[str, AgentProfile],
    role_tools: list[str],
) -> None:
    """校验一个角色的绑定；任何问题都直接抛 ``BindingError``。

    这里刻意不做"修正后继续"：配置写错就是写错，静默纠正会让用户以为生效了。
    """
    seen_refs: set[str] = set()
    for bound in binding.agents:
        if bound.ref not in profiles:
            raise BindingError(
                f"角色 {role_key} 绑定了未定义的 agent：{bound.ref}"
                f"（已定义：{', '.join(sorted(profiles)) or '无'}）")
        if bound.ref in seen_refs:
            raise BindingError(f"角色 {role_key} 重复绑定同一个 agent：{bound.ref}")
        seen_refs.add(bound.ref)
        if bound.weight <= 0:
            raise BindingError(f"角色 {role_key} 的 {bound.ref} 权重必须 > 0")
        profile = profiles[bound.ref]
        if profile.tools is not None and "*" not in role_tools:
            extra = [t for t in profile.tools if t not in role_tools]
            if extra:
                raise BindingError(
                    f"角色 {role_key} 的 agent {bound.ref} 申请了角色没有的工具："
                    f"{', '.join(extra)}（agent 只能收窄权限，不能扩权）")

    if len(binding.agents) <= 1:
        return

    aggregation = binding.aggregation
    if aggregation is None:
        raise BindingError(
            f"角色 {role_key} 绑定了 {len(binding.agents)} 个 agent，必须声明 aggregation")
    primaries = binding.primaries
    if not primaries:
        raise BindingError(f"角色 {role_key} 的多个 agent 里没有 primary/section")
    if aggregation.quorum < 1 or aggregation.quorum > len(primaries):
        raise BindingError(
            f"角色 {role_key} 的 quorum={aggregation.quorum} 超出 primary 数量 "
            f"{len(primaries)}")
    if aggregation.mode == "best_of":
        if not aggregation.judge:
            raise BindingError(f"角色 {role_key} 的 best_of 必须指定 judge")
        if aggregation.judge in {a.ref for a in primaries}:
            raise BindingError(f"角色 {role_key} 的 judge 不能同时参赛：{aggregation.judge}")
        if aggregation.judge not in profiles:
            raise BindingError(f"角色 {role_key} 的 judge 未定义：{aggregation.judge}")
    if aggregation.mode == "primary_with_review" and not binding.reviewers:
        raise BindingError(f"角色 {role_key} 的 primary_with_review 没有 review agent")
    if aggregation.mode == "split_by_path":
        if len(primaries) < 2:
            raise BindingError(f"角色 {role_key} 的 split_by_path 至少要两个 primary/section")
        _check_path_scopes(role_key, primaries, profiles)


def _check_path_scopes(
    role_key: str, primaries: list[BoundAgent], profiles: dict[str, AgentProfile]
) -> None:
    """split_by_path：每个 primary 必须有独占的 path_scope，否则必然互相覆盖。"""
    scopes: dict[str, str] = {}
    for bound in primaries:
        profile = profiles[bound.ref]
        if not profile.path_scope:
            raise BindingError(
                f"角色 {role_key} 的 split_by_path 要求每个 agent 声明 path_scope，"
                f"但 {bound.ref} 没有")
        for scope in profile.path_scope:
            owner = scopes.get(scope)
            if owner is not None:
                raise BindingError(
                    f"角色 {role_key} 的路径域重叠：{scope} 同时属于 {owner} 与 {bound.ref}")
            scopes[scope] = bound.ref
