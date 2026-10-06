"""项目经理：``RoleSpec`` + 规划阶段的一次模型调用 ``decompose``（spec §7.0/§8.2）。

拆解提示词里的「可用角色」清单从**本次 run 的 ``RoleCatalog``** 渲染（含
``roles.d`` / ``register_role`` 注册的自定义角色）：PM 因此在提示词层面就可能
建议一个新角色 —— 这正是 §8.3「新增角色是数据，不是代码」的前提，旧版只列
``BUILTIN_ROLES`` 会让自定义角色在菜单里根本不可见。目录经 :class:`BaseAgent`
的 ``catalog`` 参数下发（``AgentFactory`` 负责接线）；没有目录时（直接构造
Agent 的单测）回落到八个内置角色。

``roles/builtin.py`` 的导入仍保持函数级（``_builtin_roles``）：若本模块在顶层
反向导入 ``roles.builtin``，builtin 展开到 ``agents.project_manager`` 时会拿到
尚未定义 ``ROLE`` 的半初始化模块并直接 ImportError。``roles.catalog`` 不依赖
agents 层，顶层导入安全。

``decompose`` 只做「一次受治理的结构化调用」：``PlanOutput`` 经
``ModelFactory.complete_structured``（全局并发信号量 + token/成本记账，
``purpose="plan"``）。``to_tasks`` / ``validate_plan`` / ``with_final_task``
由 Task 29 的 Crew 负责，本方法不做任何校验 —— 校验失败的具体错误经
``feedback`` 回灌到下一次调用。
"""

from collections.abc import Sequence
from typing import cast

from agentcrew.agents.base_agent import BaseAgent
from agentcrew.models.providers.base import Message
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.roles.spec import RoleSpec
from agentcrew.task.planner import PlanOutput

ROLE = RoleSpec(
    key="project_manager",
    title="项目经理",
    goal="把项目目标拆解为可执行、可验收的任务 DAG，并为整个交付过程把关。",
    backstory=(
        "你有多年软件项目交付经验，擅长在信息不完整时做出可执行的计划，"
        "并把风险控制在早期。你相信清晰的任务边界与依赖关系是并行推进的前提。"
    ),
    responsibilities=[
        "把项目目标拆解为粒度合适、依赖明确的任务 DAG",
        "为每个任务选择执行角色，并写清验收标准与交付物",
        "汇总各角色的评审意见，行使最终验收权（G3），决定返工或收尾",
    ],
    boundaries=[
        "不编写任何业务代码，也不直接产出 PRD、架构、测试等交付文件",
        "不替其他角色做专业判断，只负责拆解、协调与终验",
    ],
    output_contract=(
        "输出结构化计划（任务列表 JSON：id / title / description / role / "
        "依赖 / 验收标准 / 交付物 / 优先级 / 一句话摘要），不落盘为普通文件；"
        "终验时输出裁决与理由。"
    ),
    system_prompt=(
        "你是一名项目经理，负责把项目目标拆解成可执行的任务 DAG，并守住最终验收权。"
        "你的输出必须是结构化的任务计划：每个任务有明确的 id、职责、依赖、验收标准与"
        "交付物，粒度适合单次执行；终验时逐条对照验收标准，给出通过或返工的裁决与理由。"
        "你不编写业务代码，不直接撰写 PRD、架构设计、测试用例等任何交付文件，"
        "也不代替其他角色做专业判断；你只负责拆解、协调与终验。计划之外的内容一律不要输出。"
    ),
    temperature=0.2,
    reviewer_for=["architect", "product_manager"],
)

#: 规划调用的 purpose：``MockProvider`` 据此回放 ``plan_todo.json``，ledger 的
#: ``llm_call`` 也靠它把规划开销与执行/评审区分开。
_PLAN_PURPOSE = "plan"

#: 拆解硬约束：§7.2 的校验 1/2/3/4/5/7，以及 T-FINAL 的保留声明（Task 9 会在
#: 校验通过后追加收尾任务并独占 ``DELIVERY.md``；PM 若也生成，冲突只在追加后
#: 才暴露，等于白烧 ``plan_max_attempts`` 里的一轮）。
_PLAN_RULES = """\
## 硬性约束（违反任何一条都会被校验拒绝，整份拆解退回重做）
1. role 只能取「可用角色」中列出的 key，不得自造角色。
2. 每个任务的 id 在本次拆解中唯一且非空；**不要生成 id 为 T-FINAL 的任务** ——
   收尾任务由系统自动追加，你只需给出它的上游交付链。
3. dependencies 只能引用本次拆解中已定义的 id，不得引用自身；任务图必须无环。
4. 所有任务的产物路径（deliverables[].path）必须全局唯一，任何两个任务都不得
   声明同一路径；**不要声明 DELIVERY.md** —— 它属于系统追加的 T-FINAL。
5. 每个任务的 acceptance_criteria 至少一条，且必须逐条可判定，**并且可由该任务
   自己声明的 deliverables 证明**。特别是：若某条标准要求产出文档（「说明」「文档」
   「README」「手册」「指南」等），该任务的 deliverables 里就必须**有一份**
   .md/.txt/.html —— 否则 agent 写出文档会被 G1 判「产出未在契约中声明的文件」
   驳回、不写又被 G2 按这条标准驳回，**该任务永远不可能通过**，整条交付链会
   被它拖死。写标准时先看本任务的交付物能不能证明它，不能就补交付物或删标准。
6. 每个任务都要填写 brief：一句话摘要，供下游任务的上下文包使用。
7. 计划必须覆盖从需求、设计、实现到测试与文档的完整交付链；按需选用角色，
   不必每个角色都有任务。"""


class ProjectManagerAgent(BaseAgent):
    """项目经理 Agent：除 ``decompose`` 外不覆写任何钩子（spec §8.2）。"""

    spec = ROLE

    async def decompose(
        self,
        goal: str,
        *,
        feedback: Sequence[str] = (),
        context_hint: str = "",
    ) -> PlanOutput:
        """把项目目标拆解为 ``PlanOutput``（spec §7.0 的规划调用）。

        ``feedback`` 是上一轮 §7.2 校验的具体错误，非空时逐条追加到 user 消息
        （「上次拆解被拒绝，原因」）。``context_hint``（P3-7）是从全局经验库检索到
        的历史成功经验文本，非空时追加到 user 消息末尾——它是**参考材料**而非新
        的约束，PM 仍按原规则输出结构化计划。调用走 ``ModelFactory.complete_structured``
        的受治理入口：全局调用信号量与 token/成本记账一项不少 —— 规划是整次
        run 最贵的一次调用，绕过工厂的裸阶梯不可接受。
        """
        alias = self._resolve_alias()
        self._set_plan_replay(alias)
        result = await self._models.complete_structured(
            alias,
            _plan_messages(
                self.spec, goal, feedback, self._prompt_roles(), context_hint
            ),
            PlanOutput,
            temperature=self.spec.temperature,
            max_tokens=self.spec.max_output_tokens,
            purpose=_PLAN_PURPOSE,
            role=self.spec.key,
        )
        return cast(PlanOutput, result)

    def _prompt_roles(self) -> Sequence[RoleSpec]:
        """提示词的可用角色：优先本次 run 的目录（含自定义角色），否则内置八角色。

        ``catalog`` 为空是直接构造 Agent 的降级态（单测、库外的极简用法）；
        正常 run 由 ``AgentFactory`` 传入 Crew 的目录，顺序即注册顺序（确定）。
        """
        if self._catalog is not None:
            return self._catalog.all()
        return _builtin_roles()

    def _set_plan_replay(self, alias: str) -> None:
        """给 mock provider 设置 ``plan`` 回放上下文。

        purpose 上下文归**调用方**（同 ``BaseAgent._configure_replay`` 与
        ``Reviewer._request`` 的分工）：工厂的 ``complete_structured`` 不管
        夹具查找键，硬在工厂里设置会覆盖调用方状态。
        """
        provider = self._models.get(alias)
        if isinstance(provider, MockProvider):
            set_purpose(provider, _PLAN_PURPOSE, role=self.spec.key)


# ── 提示词组装（模块级纯函数，便于单测）──────────────────────────────────


def _builtin_roles() -> Sequence[RoleSpec]:
    """八个内置角色；函数级导入以断开 builtin ↔ agents 的模块级组装环。

    ``roles/builtin.py`` 从本模块导入 ``ROLE``；若本模块在顶层反向导入
    ``roles.builtin``，builtin 展开到 ``agents.project_manager`` 时会拿到尚未
    定义 ``ROLE`` 的半初始化模块并直接 ImportError。延迟到调用时导入：此刻
    builtin 已完整可用（最多被本函数当场导入一次，而本模块早已在
    ``sys.modules`` 中），两种导入顺序都安全。
    """
    from agentcrew.roles.builtin import BUILTIN_ROLES

    return BUILTIN_ROLES


def _plan_messages(
    spec: RoleSpec,
    goal: str,
    feedback: Sequence[str],
    roles: Sequence[RoleSpec],
    context_hint: str = "",
) -> list[Message]:
    """规划对话：system 钉死输出契约与硬约束，user 给目标与上轮错误。"""
    return [
        {"role": "system", "content": _plan_system_prompt(spec, roles)},
        {"role": "user", "content": _plan_user_message(goal, feedback, context_hint)},
    ]


def _plan_system_prompt(spec: RoleSpec, roles: Sequence[RoleSpec]) -> str:
    """PM 规划系统提示词：角色提示词 + 输出形状 + 可用角色清单 + 硬约束。

    角色清单从 ``RoleSpec`` 数据渲染（key 与全部职责），且由调用方传入**本次
    run 的完整目录**：PM 因此既不可能建议一个目录里不存在的角色，也不会漏掉
    自定义角色 —— §7.2 校验 2 的错误只该来自模型笔误，不该来自提示词缺料。
    """
    return "\n\n".join(
        (
            spec.system_prompt,
            "## 拆解输出\n"
            "输出一个 JSON 对象，顶层字段为 tasks（任务数组）；每个任务包含 "
            "id、title、description、role、acceptance_criteria、deliverables、"
            "dependencies、priority、brief，字段形状以随附的 JSON Schema 为准。",
            "## 可用角色（role 只能取下列 key 之一）\n" + _role_menu(roles),
            _PLAN_RULES,
        )
    )


def _role_menu(roles: Sequence[RoleSpec]) -> str:
    """角色清单：一行一个 ``key``，附岗位名与全部职责。"""
    return "\n".join(
        f"- `{role.key}`（{role.title}）：{'；'.join(role.responsibilities)}"
        for role in roles
    )


def _plan_user_message(
    goal: str, feedback: Sequence[str], context_hint: str = ""
) -> str:
    """项目目标；``feedback`` 非空时追加「上次拆解被拒绝，原因」。

    空白项丢弃：Crew 只会传 ``PlanValidationError.errors``，但空串会渲染出
    没有信息量的空 bullet，让模型误以为有一条「未知错误」。

    ``context_hint``（P3-7）非空时追加在末尾：它是历史成功 run 沉淀的经验参考，
    不是硬约束——PM 可借鉴其解法，但仍须遵守全部拆解规则与输出契约。
    """
    sections = [f"## 项目目标\n{goal}"]
    errors = [error.strip() for error in feedback if error.strip()]
    if errors:
        sections.append(
            "## 上次拆解被拒绝，原因\n"
            + "\n".join(f"- {error}" for error in errors)
            + "\n\n请逐条修正上述问题后，重新输出完整计划。"
        )
    if context_hint.strip():
        sections.append(context_hint)
    return "\n\n".join(sections)
