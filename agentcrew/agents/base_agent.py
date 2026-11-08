"""``BaseAgent``：把「任务 + 上下文包」变成「模型调用 + 落盘产物」（spec §8.1）。

八个角色（§8.2）的差异几乎全在提示词与输出契约上，因此 :meth:`BaseAgent.execute`
是**模板方法**：调用顺序、错误分类、记账与落盘全在基类，子类只覆写四个钩子——
``build_messages`` / ``parse_output`` / ``validate_output`` / ``summarize``。
「新角色是数据，不是代码」（§5.4）由 :class:`~agentcrew.agents.generic.GenericAgent`
兜底：它不覆写任何钩子，行为完全由 ``RoleSpec`` 驱动。

``execute`` 的固定顺序（子类不得重写）：

1. 记 ``task_started``（spec §14.1 的 ``attempt`` 字段取 ``task.attempts``）；
2. ``build_messages`` 组装两条消息：system = 角色提示词 + 输出契约 + 产物格式说明，
   user = 渲染后的上下文包；
3. 按 spec §12.2 的三级优先级解析模型别名，经 ``models.call(purpose="execute")``
   调用（限流与记账归工厂）；
4. ``parse_output``（默认围栏解析）；解析失败 → :class:`AgentExecutionError`；
5. 说明文字（``notes``）与语言标注交叉检查的提示**立刻**落 ledger（见下）；
6. ``validate_output``（默认双向契约检查：必需路径齐全、无未声明路径）；
7. 按契约声明顺序逐文件 ``store.commit``（``ArtifactSpec`` 是 kind 的权威来源）；
8. 组装 ``TaskResult``、记 ``task_finished``。

错误分类（Task 28 的调度器只接 :class:`AgentExecutionError`）：

- 解析 / 契约校验 / 落盘失败 → :class:`AgentExecutionError`，由调度器按
  ``attempts < max_attempts`` 决定重试还是 FAILED；
- **Provider 异常原样上抛**（网络、鉴权、限流耗尽）：重试语义归 provider 层与
  调度器，包一层只会掩盖真实原因。

两个刻意的不对称：

- ``validate_output`` 必须**先于**任何 ``store.commit``：模型越权产出未声明的
  文件时一个字节都不该落盘（``ArtifactStore`` 也会拒收，但那时抛的是裸
  ``ValueError``，调度器接不住，一个多余路径就能把整次 run 打崩）。
- ``notes`` 走独立事件 ``task_notes``，且在校验之前发。塞进 ``task_finished``
  更省事，但校验失败的任务根本不会走到那一步 —— 而解析器追加的「可能被提前
  截断」提示恰恰在那时最有解释力（Task 10 的裁定）。
"""

import inspect
import json
import re
from abc import ABC
from collections.abc import Callable, Sequence
from datetime import datetime
from functools import wraps
from typing import Any, NoReturn

from agentcrew.agents.peer import CONSULT_CONTEXT
from agentcrew.agents.prompting import (
    START_FENCE_RE,
    TRUNCATED_FINISH_REASONS,
    canonical_path,
    fence_languages,
    is_truncated,
    join_notes,
    language_notes,
    render_decisions,
    render_deliverables,
    render_excerpts,
    render_pack,
    render_task,
    render_upstream,
    section,
    truncate_summary,
)
from agentcrew.artifacts.parser import ParsedOutput, parse_fenced_blocks
from agentcrew.artifacts.spec import ArtifactRecord
from agentcrew.artifacts.store import (
    ArtifactConflictError,
    ArtifactScopeError,
    ArtifactStore,
    ArtifactTooLargeError,
    UndeclaredArtifactError,
)
from agentcrew.artifacts.summarizer import summarize_records
from agentcrew.context.context_bus import ContextPack
from agentcrew.models.agent_budget import (
    bind_budget,
    bind_failover,
    release_budget,
    release_failover,
)
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import (
    CompletionResult,
    Message,
    assemble_tool_calls,
)
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.conditions import parse_outcome
from agentcrew.task.task import Task, TaskResult
from agentcrew.tools import Tool, ToolRegistry
from agentcrew.tools.schema import function_spec, schema_hint, validate_tool_args
from agentcrew.utils.files import UnsafePathError
from agentcrew.utils.trace import TraceContext, reset_trace, set_trace

#: 默认摘要的长度上限（spec §8.1「取 notes 前 400 字符」）。
#: P3-6 起：有产物时产物正文智能摘要优先于此额度，notes 作为补充；无产物时
#: notes 仍按此截断。
_SUMMARY_LIMIT = 400

#: P3-6：有产物时，批量产物摘要（``summarize_records``）并入 notes 后的总
#: 长度上限。产物正文摘要比 notes 截断更有价值，因此额度放大到 1500；notes
#: 排在产物摘要之后，超出部分截断。
_ARTIFACT_SUMMARY_TOTAL = 1500

#: ``RoleSpec.model`` 的哨兵值：字面量 ``"default"`` 表示「按三级优先级解析」。
_DEFAULT_ALIAS = "default"

#: 起始围栏行：与 ``artifacts/parser.py`` 钉死的语法一致（顶格、≥3 个反引号、
#: 可选语言名、可选 ``file=`` 到行尾）。只用于下面的一致性提示，不裁决任何事。
_START_FENCE_RE = re.compile(
    r"^(?P<fence>`{3,})(?P<lang>[A-Za-z0-9_+.\-]*)(?:\s+file=(?P<path>.*?))?\s*$"
)

#: 产物输出协议：告诉模型怎么把文件交出来。第 3 条与第 5 条是硬要求 ——
#: 未声明的路径会被拒收（校验先于落盘），而含代码示例的 markdown 若不用 4 个
#: 反引号作外层围栏，会在第一个内层围栏处**静默**截断（剩余部分变成 notes，
#: 不报错，比抛错更难发现；spec §16.2 的 README 正是这种产物）。
_ARTIFACT_FORMAT = """\
## 产物输出协议（必须遵守）

每个交付物用一个围栏代码块输出。**照下面这个形状写**——它是完整的、正确的样子：

````markdown file=docs/ARCHITECTURE.md
# 架构设计文档
（这里开始是文件的真实内容，直接落盘）
````

- **路径必须写在信封行上**（上例的第一行），与语言名同行、用空格隔开；
  不要把 `file=` 另起一行写在正文里——那样整篇内容会被当作说明文字，一个文件都不落盘。
- 信封行三样东西，顺序固定：三个或更多反引号 → 可选语言名 → `file=<契约路径>`。
  信封行必须顶格（行首不得有空格或缩进）。`file=` 之后到行尾都是路径，允许空格与中文。
- **只有带 `file=` 的围栏块会被落盘**；块外的说明文字与不带 `file=` 的代码块
  只会被记录为说明，不会写入任何文件。
- 只输出本任务交付物契约中**声明过**的路径；任何未声明的路径都会被拒绝，
  本次执行直接判为失败。
- 围栏必须成对闭合；未闭合的代码块会导致本次执行失败。
- 若产物内容本身需要**包含**围栏代码示例（例如 README 里写运行命令），外层围栏
  必须用 **4 个反引号**（````）开启与闭合——与上面例子一样。否则内层的 3 个反引号
  会提前结束你的产物，后半段内容会被当作说明丢弃。
"""

#: ReAct 工具调用（P1-1）：模型在回复里用一对 ``<tool_call>`` 围栏发出一次调用，
#: 内含 ``{"name": "...", "args": {...}}`` JSON。与产物围栏刻意区分（标签而非
#: 反引号），免得和 ``file=`` 产物围栏相互干扰。
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)

#: ReAct 循环的默认最大轮数（每轮 = 一次 LLM 调用 + 一批工具执行）。
_DEFAULT_MAX_TOOL_CALLS = 10

#: P2-1 动态子任务：模型在回复里用一对 ``<subtask>`` 围栏请求创建子任务，
#: 内含一个 JSON 对象（title/description/role/deliverables）。与 ``<tool_call>``
#: 同族（标签而非反引号），不与 ``file=`` 产物围栏相互干扰。
_SUBTASK_RE = re.compile(r"<subtask>\s*(.*?)\s*</subtask>", re.DOTALL)

#: P2-1：仅当本 Agent 注入了 ``subtask_creator`` 时才在系统提示词里告知模型
#: 可以请求动态子任务——未注入时模型看不到这段，行为与原来完全一致。
_SUBTASK_SECTION = """\
## 动态子任务（可选）
如果本任务需要拆出独立的子任务来完成，可以在回复中用一对 `<subtask>` 围栏请求创建一个子任务。围栏内是一个 JSON 对象：

<subtask>
{"title": "子任务标题", "description": "子任务要做什么", "role": "backend_dev", "deliverables": ["path/to/output.py"]}
</subtask>

- `deliverables` 是子任务要产出的文件路径列表（字符串数组）。
- 子任务创建后由调度器独立派发；你创建完子任务后**继续完成本任务**，本任务自己的交付物仍须按上面的产物协议输出。
- 不要在最终产物回复里遗漏本任务自己的 `file=` 围栏——`<subtask>` 只是请求拆任务，不替代交付物。
"""

#: P3-4 多轮对话：模型在回复里用一对 ``<message to="角色名">`` 围栏向其他角色
#:（或 "human"）发问。``to`` 是双引号包裹的目标角色 key，标签内是消息正文。
#: 与 ``<tool_call>`` / ``<subtask>`` 同族（标签而非反引号），不与产物围栏干扰。
_MESSAGE_RE = re.compile(
    r'<message\s+to="(?P<to>[^"]+)"\s*>\s*(?P<content>.*?)\s*</message>',
    re.DOTALL,
)

#: P3-4：仅当本 Agent 注入了 ``message_router`` 时才在系统提示词里告知模型
#: 可以跨角色对话——未注入时模型看不到这段，行为与原来完全一致。
_MESSAGE_SECTION = """\
## 向其他角色提问（可选）
执行过程中如果缺少关键信息（需要向上游确认契约、向其他角色求证细节），可以在回复中用一对 `<message>` 围栏发问。围栏的 `to` 属性写目标角色名，标签内写你的问题：

<message to="architect">
后端的数据模型字段应该叫 `Todo` 还是 `TodoItem`？
</message>

- 目标角色的回复会以 `<message_reply from="角色名">…</message_reply>` 的形式返回给你；拿到回复后再继续完成本任务。
- 可以一次发多条 `<message>`；拿到回复后**不要重复问同一个问题**。
- 对话轮次有限制，问完即继续产出本任务自己的 `file=` 围栏交付物——`<message>` 只是补充信息，不替代交付物。
"""

#: R5：被咨询时的回复上限。要的是「一句能立刻用上的答复」，不是长文——
#: 队友拿到长篇大论反而会把它当交付物模板抄。
_CONSULT_MAX_TOKENS = 1024


def _consult_context_hint(task: Task) -> str:
    """R6：给被咨询方的「提问方任务背景」——短到不占预算，又够对方判断。

    只带三样东西：任务标题、目标（截断）、要交付哪些文件。不带完整上下文包：
    咨询是补充信息，把上游所有产物摘要塞过去等于把一次咨询变成一次全量执行。
    """
    parts = [f"任务：{task.title}"]
    description = (task.description or "").strip()
    if description:
        parts.append(f"目标：{description[:300]}")
    if task.deliverables:
        paths = "、".join(spec.path for spec in task.deliverables[:8])
        parts.append(f"需交付：{paths}")
    return "\n".join(parts)


def _consult_system_prompt(spec: RoleSpec) -> str:
    """咨询模式的系统提示词：保留角色身份，但明确「只回答、不产出产物」。

    提示词与 :meth:`BaseAgent.build_messages` 刻意分开：那边带全套产物协议
    （``file=`` 围栏、长度预算），被咨询时带上只会让模型输出一堆围栏代码块，
    而咨询方并不需要文件。
    """
    return "\n\n".join([
        spec.system_prompt,
        "## 咨询模式\n"
        "队友在协作过程中向你提问。你只需要用简洁、确定的中文回答这个问题："
        "先给结论，再给必要依据（默认值、约束、命名约定之类）。"
        "不要输出代码围栏文件、不要声明交付物、不要反过来要求对方提交产物。"
        "若确实无法判断，直说不确定，并给出你认为最合理的做法。",
    ])


class ParsedToolCall:
    """从模型回复里解析出的一次工具调用（轻量值对象，不进 pydantic 体系）。

    P2-1 起多两个字段，让「原生调用」与「围栏解析」共用同一条执行路径：

    - ``id``：Provider 给的调用 id（原生路径用它回传 ``role="tool"`` 消息）；
      围栏路径为空串——那时没有 id，也不需要。
    - ``error``：这次解析本身失败的原因（原生 arguments 坏 JSON / 围栏块坏
      JSON）。非空时执行层**不调用工具**，直接把原因回灌，让模型自己改。
    """

    __slots__ = ("args", "error", "id", "name")

    def __init__(
        self, name: str, args: dict[str, Any], *,
        call_id: str = "", error: str | None = None,
    ) -> None:
        self.name = name
        self.args = args
        self.id = call_id
        self.error = error


def _native_tool_calls(completion: CompletionResult) -> list[ParsedToolCall]:
    """把 Provider 解析好的原生 ``tool_calls`` 转成执行层的统一形状（P2-1）。"""
    calls: list[ParsedToolCall] = []
    for item in completion.tool_calls:
        calls.append(ParsedToolCall(
            item.name, dict(item.arguments),
            call_id=item.id,
            error=(f"工具 {item.name!r} 的 arguments 不可用：{item.arguments_error}"
                   if item.arguments_error else None),
        ))
    return calls


def _extract_tool_calls(raw: str) -> list[ParsedToolCall]:
    """从模型回复里提取全部 ``<tool_call>`` 块并解析其 JSON。

    JSON 非法或不是对象的块以 ``name="<malformed>"`` 占位——执行层会把它转成
    一条错误文本回灌给模型，而不是悄悄丢掉（丢了模型就不知道自己写错了）。
    """
    calls: list[ParsedToolCall] = []
    for match in _TOOL_CALL_RE.finditer(raw):
        body = match.group(1).strip()
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            calls.append(ParsedToolCall(
                "<malformed>", {"_raw": body[:200]},
                error=f"<tool_call> 块内不是合法 JSON：{exc}"))
            continue
        if not isinstance(data, dict):
            calls.append(ParsedToolCall(
                "<malformed>", {"_raw": body[:200]},
                error="<tool_call> 块内必须是 JSON 对象（形如 {\"name\": ..., \"args\": {...}}）"))
            continue
        name = str(data.get("name", "")).strip()
        args = data.get("args")
        if not isinstance(args, dict):
            args = {}
        calls.append(ParsedToolCall(name, args))
    return calls


def _native_call_id(call: ParsedToolCall, index: int) -> str:
    """原生续传用的调用 id：Provider 给了就用它的，没给就造一个稳定的。"""
    return call.id or f"call_{index}"


def _native_tool_call_payload(calls: list[ParsedToolCall]) -> list[dict[str, Any]]:
    """把执行过的调用还原成 assistant 消息里的 ``tool_calls``（OpenAI 形状）。

    必须原样回传给端点：``role="tool"`` 的结果靠 ``tool_call_id`` 与这里的
    ``id`` 对齐，少一个字段请求就会被拒。``arguments`` 用 JSON 字符串
    （OpenAI 协议如此），坏 JSON 的情况回传 ``{}``——那时结果里已经写明原因，
    续传本身不该再失败一次。
    """
    payload: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        payload.append({
            "id": _native_call_id(call, index),
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": json.dumps(call.args, ensure_ascii=False),
            },
        })
    return payload


def _native_tool_messages(
    calls: list[ParsedToolCall], results: list[str],
) -> list[Message]:
    """原生路径的逐条工具结果消息（``role="tool"`` + ``tool_call_id``）。"""
    messages: list[Message] = []
    for index, (call, text) in enumerate(zip(calls, results, strict=False)):
        messages.append({
            "role": "tool",
            "content": text,
            "tool_call_id": _native_call_id(call, index),
        })
    return messages


def _fence_tool_results(
    calls: list[ParsedToolCall], results: list[str],
) -> str:
    """围栏路径的合并结果块（与改造前逐字节一致：``<tool_result name=…>``）。"""
    blocks = [
        f'<tool_result name="{call.name}">\n{text}\n</tool_result>'
        for call, text in zip(calls, results, strict=False)
    ]
    return "\n\n".join(blocks)


def _args_summary(args: dict[str, Any]) -> dict[str, str]:
    """ledger 用的入参摘要：超长值截断，避免大段代码把审计行撑爆。"""
    summary: dict[str, str] = {}
    for key, value in args.items():
        text = repr(value)
        summary[key] = text if len(text) <= 200 else text[:200] + "…"
    return summary


def _extract_subtask_specs(raw: str) -> list[dict[str, Any]]:
    """P2-1：从模型回复里提取全部 ``<subtask>`` 块并解析其 JSON。

    JSON 非法 / 不是对象的块**不抛错**：模型写砸一个子任务请求不该让本任务
    （可能已正确产出交付物）跟着失败。坏块以 ``{"_malformed": <原文>}`` 占位
    返回，由调用方记一条 ``subtask_malformed`` 留痕后跳过——与 tool_call 的
    ``<malformed>`` 同族（让模型知道自己写错了，而不是悄悄丢掉）。
    """
    specs: list[dict[str, Any]] = []
    for match in _SUBTASK_RE.finditer(raw):
        body = match.group(1).strip()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            specs.append({"_malformed": body[:200]})
            continue
        if not isinstance(data, dict):
            specs.append({"_malformed": body[:200]})
            continue
        specs.append(data)
    return specs


class ParsedAgentMessage:
    """从模型回复里解析出的一条跨角色消息（P3-4，轻量值对象）。"""

    __slots__ = ("to", "content")

    def __init__(self, to: str, content: str) -> None:
        self.to = to
        self.content = content


def _extract_agent_messages(raw: str) -> list[ParsedAgentMessage]:
    """从模型回复里提取全部 ``<message to="…">…</message>`` 围栏（P3-4）。

    ``to`` 为空串的块仍保留（路由层会拒收并回灌错误文本，而不是悄悄丢掉——
    与 tool_call 的 ``<malformed>`` 同族）。
    """
    messages: list[ParsedAgentMessage] = []
    for match in _MESSAGE_RE.finditer(raw):
        messages.append(
            ParsedAgentMessage(to=match.group("to").strip(), content=match.group("content").strip())
        )
    return messages


class AgentExecutionError(RuntimeError):
    """一次 Agent 执行失败：解析、契约校验或落盘不通过。

    携带 ``task_id`` 与 ``role``：调度器据此把任务送回 ``READY`` 重试或判
    ``FAILED``，运行报告也用它点名是哪个角色的哪个任务（spec §7.3/§14.3）。
    """

    def __init__(self, message: str, *, task_id: str, role: str) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.role = role


def _budgeted(fn):
    """给 ``execute`` 绑一份执行体预算上下文；没有上限时零开销。

    用装饰器而不是改方法体：``execute`` 很长且多处 return，手工包 try/finally
    容易漏路径。上下文随协程走，split 模式下多个 agent 并发互不干扰。
    """
    @wraps(fn)
    async def wrapper(self, *args, **kwargs):
        token = bind_budget(
            self.agent_key, self.cost_ceiling_usd, self.max_calls_per_task,
            self.max_concurrency, self.max_rpm, self.run_cost_ceiling_usd)
        fo_token = bind_failover(self.fallback_models)
        try:
            return await fn(self, *args, **kwargs)
        finally:
            release_failover(fo_token)
            release_budget(token)
    return wrapper


class BaseAgent(ABC):
    """所有角色的基类：模板方法（``execute``）+ 四个可覆写钩子（spec §8.1）。

    四个钩子都有默认实现，因此没有 ``abstractmethod``；写 ``ABC`` 是刻意声明
    「这是被继承的边界」，不是「不可实例化」。``spec`` 是构造时传入的那一份
    （Task 25 的角色子类把它写成类属性 ``spec = ROLE``，两条路径指向同一个
    ``RoleSpec`` 对象）。
    """

    spec: RoleSpec

    def __init__(
        self,
        spec: RoleSpec,
        *,
        models: ModelFactory,
        store: ArtifactStore,
        ledger: Ledger,
        project_goal: str,
        catalog: RoleCatalog | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_calls: int = _DEFAULT_MAX_TOOL_CALLS,
        subtask_creator: Callable[[dict], str | None] | None = None,
        max_self_corrections: int = 2,
        stream: bool = False,
        message_router: Callable[[str, str], str] | None = None,
        max_message_rounds: int = 5,
        max_subtask_corrections: int = 1,
        tool_guard: Any | None = None,
    ) -> None:
        """``catalog`` 是本 run 的角色目录（含自定义角色），可为 ``None``。

        只有需要「看见整个团队」的 Agent 用它（PM 的拆解提示词要从目录渲染
        可用角色清单，否则自定义角色永远不会出现在计划里）；其余 Agent 不读。
        ``None`` 是合法的降级态：直接构造 Agent 的单测没有目录可给。

        ``tool_registry`` 非空时 :meth:`execute` 走 ReAct 循环（P1-1）：模型可在
        产出最终产物前调用工具获取信息。``None``（默认）保持原有单次调用行为，
        向后兼容。``max_tool_calls`` 是 ReAct 最大轮数，触顶仍想调工具时降级为
        把最后一次回复当最终产物，并记一条 ``tool_loop_exhausted`` 警告。

        ``subtask_creator``（P2-1）非空时 :meth:`execute` 会检测回复里的
        ``<subtask>`` 围栏并通过它创建动态子任务；``None``（默认）不检测、
        系统提示词也不告知子任务能力，行为与原来完全一致。它接收子任务 spec
        字典（含 Agent 注入的 ``parent_id``），返回新任务 id。

        ``max_self_corrections``（P2-8）：parse/validate 失败时，在**同一次
        execute() 内**把错误回灌给模型重调 LLM 的最大次数（默认 2）。仍失败才抛
        :class:`AgentExecutionError` 让调度器重试。``0`` = 首次失败即抛错（与
        原行为完全一致）。Provider 异常（网络/鉴权）不在自我修正范围内，原样上抛。

        ``stream``（P3-3）：``True`` 时 LLM 调用走 ``models.stream_call`` 流式
        路径，逐块收集并对每个增量块记一条 ``agent_stream_chunk`` ledger 事件；
        收集齐全文后仍走原有的 parse/validate/commit 流程。``False``（默认）
        完全走 ``models.call``，行为与改造前逐字节一致（向后兼容）。

        ``message_router``（P3-4）非空时 :meth:`execute` 会检测回复里的
        ``<message to="角色名">…</message>`` 围栏：经回调向指定角色（或用户）发问，
        把回复以 ``<message_reply from="…">…</message_reply>`` 回灌后继续 LLM 调用，
        直到模型不再发消息围栏。``None``（默认）不检测、系统提示词也不告知对话
        能力，行为与原来完全一致。回调签名 ``(目标角色, 消息内容) -> 回复文本``，
        是同步函数（路由本身不发 LLM）。``max_message_rounds`` 是一次 execute 内
        的最大对话轮数（默认 5），触顶降级为把最后一次回复当最终产物。
        """
        self.spec = spec
        self._models = models
        self._store = store
        self._ledger = ledger
        self._project_goal = project_goal
        self._catalog = catalog
        self._tool_registry = tool_registry
        self._max_tool_calls = max_tool_calls
        self._subtask_creator = subtask_creator
        #: 多 agent 分工时的写保护：由 AgentFactory 按 profile 注入。
        #: 空列表 = 不限（单 agent 路径完全不受影响）。
        self.path_scope: list[str] = []
        self.agent_key: str = ""
        #: 执行体级预算（来自 AgentProfile）；None = 不限制。
        self.cost_ceiling_usd: float | None = None
        self.max_calls_per_task: int | None = None
        #: 执行体级备用别名（AgentProfile.fallback），优先级高于全局 failover。
        self.fallback_models: list[str] = []
        #: 执行体级并发上限（AgentProfile.max_concurrency）；None = 不额外限制。
        self.max_concurrency: int | None = None
        #: 执行体级每分钟调用上限（AgentProfile.max_rpm）；None = 只受 provider 约束。
        self.max_rpm: int | None = None
        #: 整次 run 的累计成本上限（AgentProfile.run_cost_ceiling_usd）。
        self.run_cost_ceiling_usd: float | None = None
        self._max_self_corrections = max_self_corrections
        self._stream = stream
        self._message_router = message_router
        self._max_message_rounds = max_message_rounds
        #: R8：子任务被拒时，把原因回灌并重问 LLM 的次数上限（``0`` = 不回灌，
        #: 行为与 R8 之前逐字一致）。只有回调提供了 ``last_rejection`` 才可能触发。
        self._max_subtask_corrections = max(0, int(max_subtask_corrections))
        #: P2-4：工具级 guardrails（``None`` = 没配规则，整段逻辑是一次判断）。
        self._tool_guard = tool_guard

    def set_tool_guard(self, guard: Any | None) -> None:
        """运行时更换 guard（AgentFactory 在配置变更后刷新缓存实例用，P2-4）。"""
        self._tool_guard = guard

    def set_subtask_creator(
        self, creator: Callable[[dict], str | None] | None
    ) -> None:
        """运行时更换子任务回调（AgentFactory 在调度器换板后刷新缓存实例用）。"""
        self._subtask_creator = creator

    def set_message_router(
        self, router: Callable[[str, str], Any] | None
    ) -> None:
        """运行时更换跨角色路由（AgentFactory 刷新已缓存实例用，R5）。

        ``router`` 可以是同步回调（旧行为）或返回 awaitable 的协程函数——后者
        才能真的去问目标角色。``None`` 退回「不检测消息围栏」的原始行为。
        """
        self._message_router = router

    # ── 模板方法（子类不重写）────────────────────────────────────────────

    @_budgeted
    async def execute(
        self,
        task: Task,
        pack: ContextPack,
        *,
        fix_hints: Sequence[str] = (),
    ) -> TaskResult:
        """执行一个任务：组装提示词 → 调用 → 解析 → 校验 → 落盘 → 返回结果。

        异常分类见模块 docstring；成功路径一定会留下 ``task_started`` 与
        ``task_finished`` 两条事件（``artifact_written`` 由 ``ArtifactStore`` 发）。

        2026-11 可观测性改造：进入即设置 :class:`TraceContext`（contextvar，
        按协程隔离），使执行期所有 ``llm_call`` 记账能带上 ``task_id`` /
        ``attempt``——同一角色并发多任务时 token / 成本 / 延迟可精确归因。
        ``finally`` 恢复上下文，业务逻辑原样移入 :meth:`_execute_traced`。
        """
        token = set_trace(TraceContext(
            task_id=task.id, role=self.spec.key, attempt=task.attempts))
        try:
            return await self._execute_traced(task, pack, fix_hints=fix_hints)
        finally:
            reset_trace(token)

    async def _execute_traced(
        self,
        task: Task,
        pack: ContextPack,
        *,
        fix_hints: Sequence[str] = (),
    ) -> TaskResult:
        """:meth:`execute` 的原始执行体（追踪上下文已就位）。"""
        started_at = datetime.now()
        self._ledger.emit(
            "task_started",
            task_id=task.id,
            role=self.spec.key,
            attempt=task.attempts,
        )

        # P2-1：先定别名再构提示词——工具段要按「这个别名能不能原生调函数」
        # 选一套指令，两套同时给会让模型在协议之间摇摆。
        alias = self._resolve_alias(task)
        messages = self.build_messages(
            task, pack, fix_hints,
            native_tools=self._native_tools_enabled(alias))
        final_text = await self._obtain_final_text(task, messages, alias)

        # P2-1：检测 <subtask> 围栏并创建动态子任务（仅当注入了 creator）。
        # 在 parse 之前做：子任务请求不影响本任务自己的产物解析，且创建后立即
        # 记 ledger，子任务在下一轮调度即可被派发。
        # R8：被拒且**知道原因**时，把原因回灌给模型重新产出一次——否则它会
        # 换个同样错的名字继续试，或者干脆把这件活丢掉。
        rejections = await self._maybe_create_subtasks(task, final_text)
        if rejections and self._max_subtask_corrections > 0:
            final_text = await self._retry_after_subtask_rejection(
                task, messages, alias, final_text, rejections)

        # P2-8：parse/validate 失败时在同一次 execute() 内自我重调 LLM 修正。
        parsed, notes = await self._produce_with_corrections(
            task, messages, alias, final_text
        )
        records = self._commit(parsed, task)

        finished_at = datetime.now()
        provisional = TaskResult(
            task_id=task.id,
            role=self.spec.key,
            summary="",  # 下面用 summarize 的结果替换（钩子要拿到完整的 TaskResult）
            artifacts=records,
            notes=notes,
            # R4：结果标签来自 Agent 回复里的 <outcome> 围栏，供下游条件边判定。
            # 没有围栏（既有 run / mock 夹具）时为 None，行为逐字不变。
            outcome=parse_outcome(final_text),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((finished_at - started_at).total_seconds() * 1000),
        )
        result = provisional.model_copy(
            update={"summary": self.summarize(provisional)}
        )
        self._ledger.emit(
            "task_finished",
            task_id=task.id,
            role=self.spec.key,
            attempt=task.attempts,
            duration_ms=result.duration_ms,
        )
        return result

    # ── 钩子（均提供默认实现）────────────────────────────────────────────

    def build_messages(
        self,
        task: Task,
        pack: ContextPack,
        fix_hints: Sequence[str],
        *,
        native_tools: bool = False,
    ) -> list[Message]:
        """默认提示词：system 走角色规格，user 走渲染后的上下文包。

        ``native_tools``（P2-1）：本次调用是否走原生 function calling。是则工具段
        改成「直接调函数」，不再教模型写 ``<tool_call>`` 文本——两套指令同时给会
        让模型在两套协议之间摇摆。默认 ``False``，提示词与改造前逐字一致。
        """
        system_parts: list[str] = [
            self.spec.system_prompt,
            f"## 输出规范\n{self.spec.output_contract}",
            _ARTIFACT_FORMAT,
            # 明说 token 预算：撞上上限时回复会被**截断**，围栏随之未闭合，
            # 整次输出全废。实测架构师连写三版 8000 token 的文档全部撞线。
            f"## 输出长度\n本次回复的全部内容（含围栏与说明文字）上限约 "
            f"{self.spec.max_output_tokens} token。宁可写紧凑、写到要点为止，"
            f"也不要写满被截断——一旦截断，本次输出整体作废。",
        ]
        tools_section = self._tools_section(native=native_tools)
        if tools_section:
            system_parts.append(tools_section)
        # P2-1：仅在注入了子任务回调时告知模型可以请求动态子任务。
        if self._subtask_creator is not None:
            system_parts.append(_SUBTASK_SECTION)
        # P3-4：仅在注入了消息路由时告知模型可以跨角色发问。
        if self._message_router is not None:
            system_parts.append(_MESSAGE_SECTION)
        system = "\n\n".join(system_parts)
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": _render_pack(pack, fix_hints)},
        ]

    def parse_output(self, raw: str, task: Task) -> ParsedOutput:
        """默认解析围栏块（spec §9.1）；只有带 ``file=`` 的块会成为产物。"""
        return parse_fenced_blocks(raw)

    def validate_output(self, parsed: ParsedOutput, task: Task) -> None:
        """默认契约校验：必需路径齐全 + 无未声明路径（两个方向都判失败）。

        两侧比较走**同一条规范化**（``safe_relpath``，与 ``ArtifactSpec.path``
        相同），所以 ``file=./a.py`` 命中契约里的 ``a.py``；无法规范化的不安全
        路径原样当作「未声明」报出，绝不让 ``UnsafePathError`` 逃逸。
        """
        declared = {spec.path for spec in task.deliverables}
        produced = {_canonical_path(path) for path in parsed.files}
        missing = sorted(
            {spec.path for spec in task.deliverables if spec.required} - produced
        )
        undeclared = sorted(produced - declared)
        problems: list[str] = []
        if missing:
            problems.append(f"缺少必需产物：{'、'.join(missing)}")
        if undeclared:
            # 必须**同时**说出允许写哪些：实测真实模型会既按契约产出 app.js、
            # 又按上游设计文档多加一个 server.js，而错误只说「多出 server.js」
            # 时它无从判断是「名字错了」还是「不该多写」——三次尝试原地打转。
            allowed = "、".join(sorted(declared)) or "（本任务无文件交付物）"
            problems.append(
                f"产出未在契约中声明的文件：{'、'.join(undeclared)}"
                f"；本任务只允许写：{allowed}"
            )
        if problems:
            self._fail(task, "；".join(problems))

    def summarize(self, result: TaskResult) -> str:
        """默认摘要：有产物时优先产物正文智能摘要（P3-6），否则回落到 notes。

        「没有说明文字」是常态而非异常：只输出围栏块的规范回复里一个字的说明
        都没有，而 ``summary`` 是下游上下文包的信息来源，空串会让下游只看到
        一个文件名列表。

        P3-6：当本次执行产出了文件时，用 :func:`summarize_records` 对**正文**做
        结构性摘要（标题 / 接口 / schema），比原来只列文件名有价值得多。产物
        摘要在前、notes 在后（若有），整体截断到 :data:`_ARTIFACT_SUMMARY_TOTAL`。
        无产物时**完全**回落到旧的 notes 截断 / 「执行完成」逻辑，向后兼容。
        """
        notes = result.notes.strip()
        if not result.artifacts:
            # 向后兼容：无产物时沿用原 notes 截断 / 空回落逻辑。
            if notes:
                return notes[:_SUMMARY_LIMIT]
            return "执行完成，未产出文件"
        artifact_summary = summarize_records(
            result.artifacts, self._store, max_total=_ARTIFACT_SUMMARY_TOTAL
        )
        if notes:
            combined = f"{artifact_summary}\n说明：{notes}"
            return _truncate_summary(combined, _ARTIFACT_SUMMARY_TOTAL)
        return artifact_summary

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _resolve_alias(self, task: Task | None = None) -> str:
        """spec §12.2 的三级优先级 + V2 复杂度路由（高者胜）。

        1. 角色级 ``RoleSpec.model`` →
        2. ``settings.role_bindings[role]`` →
        3. ``settings.model_tiers[<任务复杂度>]``（V2，未配置则跳过）→
        4. 默认模型。

        ``task`` 为 None 时（如结构化输出调用不感知任务），退化为前两级 + 默认模型。
        字面量 ``"default"`` 是「没指定」的哨兵，不是模型名。
        """
        if self.spec.model != _DEFAULT_ALIAS:
            return self.spec.model
        if task is not None:
            return self._models.alias_for_task(task)
        settings = self._models.settings
        return settings.role_bindings.get(self.spec.key) or settings.default_model

    def _configure_replay(self, alias: str, task: Task) -> None:
        """给 mock provider 设置回放上下文（spec §16.1：purpose/role/task_id/attempt）。

        purpose 上下文归**调用方**（Task 21 的裁定）：工厂的 ``call()`` 不管这些
        字段，硬在工厂里改会覆盖调用方状态、弄坏夹具查找。真实 provider 不认
        ``set_purpose``，因此只在 mock 上设置。

        夹具编号 = ``attempts + rework_count``：``attempts`` 在派发时自增
        （spec §6.2），而 ``REWORK → RUNNING`` 不扣 ``attempts``、轮次记在
        ``rework_count`` 上，所以「第几次执行」是两者之和 —— §16.1 的演示
        「attempt-1 不合格 → 返工 → attempt-2 通过」正依赖这一点。直接调用
        （未经调度器派发，``attempts == 0``）时按第 1 次算，否则 mock 会把
        「attempt-0 不存在」误报成一次夹具替换。
        """
        provider = self._models.get(alias)
        if isinstance(provider, MockProvider):
            set_purpose(
                provider,
                "execute",
                role=self.spec.key,
                task_id=task.id,
                attempt=max(1, task.attempts + task.rework_count),
            )

    # ── ReAct 工具循环（P1-1）────────────────────────────────────────────

    def _available_tools(self) -> list[Tool]:
        """本角色在本次执行中可用的工具（注册表 × ``spec.allowed_tools``）。

        注册表为 ``None``（默认，向后兼容）或角色白名单过滤后为空 → 返回空列表，
        :meth:`execute` 退回单次调用，系统提示词也不带工具说明。
        """
        if self._tool_registry is None:
            return []
        return self._tool_registry.filter_by_allowed(self.spec.allowed_tools)

    def _native_tools_enabled(self, alias: str) -> bool:
        """本次执行是否走原生 function calling（P2-1 / P2-5）。

        两条同时满足：有可用工具、该别名能力位支持。**流式不再降级**——P2-5 起
        ``stream_call`` 会解析 ``delta.tool_calls`` 并在 :meth:`_invoke_model` 里
        用 ``assemble_tool_calls`` 拼装，与非流式共用同一条消费路径。
        """
        return (
            bool(self._available_tools())
            and self._models.supports_native_tools(alias)
        )

    def _tools_section(self, *, native: bool = False) -> str:
        """渲染进系统提示词的工具说明段；无可用工具时返回空串。

        ``native=True``（P2-1）时给的是「直接调用请求里的函数」这一套指令；否则
        是既有的 ``<tool_call>`` 围栏写法（默认，逐字不变）。
        """
        tools = self._available_tools()
        if not tools:
            return ""
        lines = [
            "## 可用工具（ReAct）",
            "你可以在产出最终产物之前调用工具来获取信息或执行代码。工具清单：",
        ]
        for tool in tools:
            schema = tool.input_schema or {}
            props = schema.get("properties", {}) if isinstance(schema, dict) else {}
            required = schema.get("required", []) if isinstance(schema, dict) else []
            args_desc = "、".join(
                f"{name}{'（必填）' if name in required else ''}" for name in props
            ) or "无参数"
            lines.append(f"- `{tool.name}`：{tool.description} 入参：{args_desc}")
        if native:
            lines += [
                "调用方式：本次请求已经通过 **函数调用（tools）** 提供上面这些函数，"
                "直接发起函数调用即可（可并行调用多个）。",
                "函数结果会作为工具消息返回给你。收集到足够信息后，"
                "再产出带 `file=` 的最终产物围栏；**最终产物的那条回复里不要再发起函数调用**。",
                "如果函数调用不可用，可以退回到文本围栏写法：在回复里插入一个 "
                "`<tool_call>` 块，内含 `{\"name\": \"<工具名>\", \"args\": {…}}`。",
            ]
            return "\n".join(lines)
        lines += [
            "调用方式：在回复中插入一个 `<tool_call>` 块，内含一个 JSON 对象 "
            "`{\"name\": \"<工具名>\", \"args\": {…}}`，例如：",
            "",
            '<tool_call>\n{"name": "read_file", "args": {"path": "src/app.py"}}\n</tool_call>',
            "",
            "工具结果会以 `<tool_result name=\"工具名\">…</tool_result>` 块返回给你。"
            "可以在一条回复里放多个 `<tool_call>` 连续调用。收集到足够信息后，"
            "再产出带 `file=` 的最终产物围栏；**最终产物的那条回复里不要再放 `<tool_call>`**。",
        ]
        return "\n".join(lines)

    async def _invoke_model(
        self, task: Task, messages: list[Message], alias: str,
        *, tools: list[dict[str, Any]] | None = None,
    ) -> CompletionResult:
        """调一次 LLM：非流式走 ``models.call``，流式走 ``models.stream_call``（P3-3）。

        ``stream=False``（默认）时就是 ``models.call`` 的薄封装，行为与改造前逐
        字节一致。``stream=True`` 时逐块消费 ``stream_call``：每拼一个增量块就记
        一条 ``agent_stream_chunk`` ledger 事件（含 ``task_id`` 与 ``delta_length``），
        全部收齐后拼成完整文本、重建一个 :class:`CompletionResult`（只带
        ``text``/``finish_reason``，token 记账已在工厂 ``stream_call`` 内完成），
        后续截断守卫/解析/校验/落盘与非流式共用同一条路径。
        """
        if not self._stream:
            return await self._models.call(
                alias,
                messages,
                temperature=self.spec.temperature,
                max_tokens=self.spec.max_output_tokens,
                purpose="execute",
                role=self.spec.key,
                # P2-1：只有能力位支持时才带 tools；不支持时传 None，Provider
                # 侧与改造前逐字节一致（不会凭空多出 tools 字段）。
                tools=tools,
                tool_choice="auto" if tools else None,
            )
        parts: list[str] = []
        finish_reason: str | None = None
        # P2-5：流式下的原生工具调用——把分片增量攒起来，流结束后一次性拼装。
        # 每片都解析是错的：一个 JSON 字符串字面量可能被切成两片。
        tool_deltas: list[Any] = []
        async for chunk in self._models.stream_call(
            alias,
            messages,
            temperature=self.spec.temperature,
            max_tokens=self.spec.max_output_tokens,
            purpose="execute",
            role=self.spec.key,
            tools=tools,
            tool_choice="auto" if tools else None,
        ):
            parts.append(chunk.delta)
            if chunk.tool_call_deltas:
                tool_deltas.extend(chunk.tool_call_deltas)
            if chunk.finish_reason is not None:
                finish_reason = chunk.finish_reason
            self._ledger.emit(
                "agent_stream_chunk",
                task_id=task.id,
                role=self.spec.key,
                delta_length=len(chunk.delta),
            )
        provider = self._models.get(alias)
        return CompletionResult(
            text="".join(parts),
            provider=provider.binding.provider,
            model=provider.binding.model,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
            finish_reason=finish_reason,
            tool_calls=assemble_tool_calls(tool_deltas),
        )

    async def consult(
        self, question: str, *, from_role: str = "", context: str = ""
    ) -> str:
        """被其他角色点名咨询时的一次轻量作答（R5 真协作通道）。

        与 :meth:`execute` 的区别是刻意的：**不**解析产物围栏、**不**落盘、
        **不**改任务状态、**不**进看板。咨询只是「问一句话拿一句话」，不是一次
        任务执行——所以它复用同一个 ``ModelFactory``（记账、预算、限流照旧），
        但只发一次非流式调用，且不带产物协议，避免把队友的回复误当成交付物。

        ``context``（R6）是**提问方当前任务的背景**（标题 / 目标 / 交付契约）。
        没有它时队友只能看见一句孤立的问题，只能反问基础信息；带上之后第一次
        就能问到点上。由 :class:`~agentcrew.agents.peer.PeerRouter` 从
        contextvar 读出并透传，调用方不用手动拼。

        离线 mock 通道没有 consult 夹具（``MockProvider`` 只认
        plan/review/execute），此时返回一句占位说明而不是抛错：演示 run 不该
        因为「模型答不出咨询」而失败。Provider 异常同样不抛给提问方——路由
        失败不该打断在跑的任务，记 ``agent_consult_failed`` 后返回可继续的文案。
        """
        alias = self._resolve_alias(None)
        provider = self._models.get(alias)
        if isinstance(provider, MockProvider):
            return (
                f"（离线演示：{self.spec.title}未接真实模型，"
                f"请按通用做法继续。）"
            )
        question_text = question.strip() or "（对方没有写明具体问题）"
        asker = from_role or "其他角色"
        if context.strip():
            user_content = (
                f"来自 {asker} 的提问。对方的任务背景如下（仅供判断，不必复述）：\n"
                f"{context.strip()}\n\n问题：\n{question_text}"
            )
        else:
            user_content = f"来自 {asker} 的提问：\n{question_text}"
        messages: list[Message] = [
            {"role": "system", "content": _consult_system_prompt(self.spec)},
            {"role": "user", "content": user_content},
        ]
        try:
            completion = await self._models.call(
                alias,
                messages,
                temperature=self.spec.temperature,
                # 咨询要的是能立刻用上的一句话，不是长文；压到 1k 以内。
                max_tokens=min(_CONSULT_MAX_TOKENS, self.spec.max_output_tokens),
                purpose="consult",
                role=self.spec.key,
            )
        except Exception as exc:  # noqa: BLE001 —— 路由失败不打断提问方任务
            self._ledger.emit(
                "agent_consult_failed",
                role=self.spec.key,
                from_role=from_role,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            return "（对方未能作答：请按最合理的默认值自行决定并继续。）"
        reply = completion.text.strip() or "（对方未给出有效答复）"
        self._ledger.emit(
            "agent_consult",
            role=self.spec.key,
            from_role=from_role,
            question=question_text[:300],
            reply_length=len(reply),
        )
        return reply

    async def _obtain_final_text(
        self, task: Task, messages: list[Message], alias: str
    ) -> str:
        """取得最终产物回复。外层是跨角色对话循环（P3-4），内层是单次/ReAct 取答。

        ``message_router is None``（默认）时直接返回一次 :meth:`_acquire_reply` 的
        结果，与改造前逐字节一致。注入了路由时：拿到一次回复后检测 ``<message
        to="…">`` 围栏——有就逐条经路由取回回复、以 ``<message_reply from="…">``
        回灌，再问一次 LLM；直到模型不再发消息围栏，或触顶 ``max_message_rounds``
        （触顶降级为把最后一次回复当最终产物，记 ``message_loop_exhausted``）。
        """
        text = await self._acquire_reply(task, messages, alias)
        if self._message_router is None:
            return text
        for round_index in range(self._max_message_rounds):
            outgoing = _extract_agent_messages(text)
            if not outgoing:
                return text
            if round_index >= self._max_message_rounds - 1:
                self._ledger.emit(
                    "message_loop_exhausted",
                    task_id=task.id,
                    role=self.spec.key,
                    max_message_rounds=self._max_message_rounds,
                )
                break
            # 把这轮回复（含消息围栏）与每条路由回复回灌给模型，再问一次。
            messages.append({"role": "assistant", "content": text})
            # R6：把「提问方在做什么」放进 contextvar，路由转发给被问角色。
            # 用 contextvar 而不是改回调签名——同步夹具（旧测试）一行不用动。
            context_token = CONSULT_CONTEXT.set(_consult_context_hint(task))
            try:
                for msg in outgoing:
                    # R5：路由可以是同步回调（旧行为）或协程（真去问目标角色）。
                    # 两者都支持，既有同步夹具一行不用改。
                    reply = self._message_router(msg.to, msg.content)
                    if inspect.isawaitable(reply):
                        reply = await reply
                    self._ledger.emit(
                        "agent_message",
                        task_id=task.id,
                        role=self.spec.key,
                        from_role=self.spec.key,
                        to_role=msg.to,
                        content=msg.content[:300],
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": f'<message_reply from="{msg.to}">\n{reply}\n</message_reply>',
                        }
                    )
            finally:
                CONSULT_CONTEXT.reset(context_token)
            text = await self._acquire_reply(task, messages, alias)
        return text

    async def _acquire_reply(
        self, task: Task, messages: list[Message], alias: str
    ) -> str:
        """单次/ReAct 取答：无工具走单次调用，有工具走 ReAct 循环。

        两次路径在截断检查、mock 回放上下文、temperature/max_tokens 上完全一致；
        唯一区别是有工具时会循环把工具结果回灌、直到模型不再发 ``<tool_call>``。
        """
        if not self._available_tools():
            self._configure_replay(alias, task)
            completion = await self._invoke_model(task, messages, alias)
            self._guard_truncation(completion, task)
            return completion.text
        return await self._run_agent_loop(task, messages, alias)

    async def _run_agent_loop(
        self, task: Task, messages: list[Message], alias: str
    ) -> str:
        """ReAct 循环：LLM → 解析 tool_call → 执行工具 → 回灌 → 再问 LLM。

        最多 ``self._max_tool_calls`` 轮。模型某次回复不再发工具调用即为最终产物。
        触顶仍在发 tool_call 时**降级**：记一条 ``tool_loop_exhausted`` 警告，把
        最后一次回复当最终产物（不判失败——重试同上限只会再循环一次）。

        **两条来源、同一个消费端**（P2-1）：能力位支持原生 function calling 时
        优先用 ``completion.tool_calls``；为空再回退到 ``<tool_call>`` 围栏。
        回灌协议也随之切换（原生：assistant(tool_calls) + 逐条 role="tool"；
        围栏：assistant 文本 + user(tool_result) 文本块），但**解析之后的执行、
        校验、留痕、错误回灌完全同一条路径**——这正是「原生与回退同任务同结果」
        的实现方式。ledger 的 ``tool_call`` 事件带 ``mode`` 字段，运维能看出这次
        实际走的是哪条。
        """
        native = self._native_tools_enabled(alias)
        specs = (
            [function_spec(tool) for tool in self._available_tools()]
            if native else None)
        completion = None
        exhausted = False
        for round_index in range(self._max_tool_calls):
            self._configure_replay(alias, task)
            completion = await self._invoke_model(
                task, messages, alias, tools=specs)
            self._guard_truncation(completion, task)
            native_calls = _native_tool_calls(completion)
            mode = "native" if native_calls else "fence"
            calls = native_calls or _extract_tool_calls(completion.text)
            if not calls:
                break
            if round_index >= self._max_tool_calls - 1:
                exhausted = True
                break
            results = await self._execute_tool_calls(calls, task, mode=mode)
            if mode == "native":
                messages.append({
                    "role": "assistant",
                    "content": completion.text,
                    "tool_calls": _native_tool_call_payload(calls),
                })
                messages.extend(
                    _native_tool_messages(calls, results))
            else:
                messages.append({"role": "assistant", "content": completion.text})
                messages.append({
                    "role": "user",
                    "content": _fence_tool_results(calls, results),
                })
        if exhausted:
            self._ledger.emit(
                "tool_loop_exhausted",
                task_id=task.id,
                role=self.spec.key,
                max_tool_calls=self._max_tool_calls,
            )
        assert completion is not None  # 循环至少跑一轮
        return completion.text

    def _guard_input(
        self, task: Task, call: ParsedToolCall,
    ) -> tuple[bool, str]:
        """P2-4：执行前的入参检查，返回 ``(是否拦下, 回灌文本)``。

        未配置规则时是一次 ``None`` 判断（零开销）。命中 ``warn`` 只留痕、照常
        执行；命中 ``block`` 则**不执行**，把原因与规则名一起回灌给模型——它下一轮
        能自己换写法，而不是让整个任务失败。
        """
        guard = self._tool_guard
        if guard is None:
            return False, ""
        try:
            hits = guard.check_input(call.name, call.args)
        except Exception as exc:  # noqa: BLE001 —— 规则执行失败按「放行」处理
            self._ledger.emit(
                "tool_guard_error", task_id=task.id, role=self.spec.key,
                tool_name=call.name, error=str(exc)[:200])
            return False, ""
        if not hits:
            return False, ""
        self._emit_guard_hits(task, call, hits)
        blocked = [hit for hit in hits if hit.action == "block"]
        if not blocked:
            return False, ""
        reasons = "；".join(
            f"{hit.rule}：{hit.reason}" for hit in blocked)
        return True, (
            f"错误：工具 {call.name!r} 的调用被安全规则拦截（{reasons}）。"
            "请换一种不触发该规则的写法后重新发起调用。")

    def _guard_output(
        self, task: Task, call: ParsedToolCall, text: str,
    ) -> str | None:
        """P2-4：回灌前的输出检查；被拦时返回替换文本（``None`` = 放行原样）。

        输出相位拦的是**泄漏**（把密钥/凭据读进上下文再发给模型）。拦截后原文
        一律不回灌——留一段说明，让模型知道结果是「被遮掉」而不是「空」。
        """
        guard = self._tool_guard
        if guard is None:
            return None
        try:
            hits = guard.check_output(call.name, text)
        except Exception as exc:  # noqa: BLE001
            self._ledger.emit(
                "tool_guard_error", task_id=task.id, role=self.spec.key,
                tool_name=call.name, error=str(exc)[:200])
            return None
        if not hits:
            return None
        self._emit_guard_hits(task, call, hits)
        blocked = [hit for hit in hits if hit.action == "block"]
        if not blocked:
            return None
        reasons = "；".join(f"{hit.rule}：{hit.reason}" for hit in blocked)
        return f"（工具 {call.name!r} 的输出被安全规则拦截：{reasons}；内容未回灌）"

    def _emit_guard_hits(
        self, task: Task, call: ParsedToolCall, hits: list[Any],
    ) -> None:
        """每条命中都留一条 ``tool_guard_triggered``（含相位与动作）。"""
        for hit in hits:
            self._ledger.emit(
                "tool_guard_triggered",
                task_id=task.id,
                role=self.spec.key,
                tool_name=call.name,
                rule=hit.rule,
                phase=hit.phase,
                action=hit.action,
                field=hit.field,
                reason=hit.reason,
            )

    def _record_tool_span(
        self, *, task: Task, call: ParsedToolCall, success: bool,
        duration_ms: int, error: str | None,
    ) -> None:
        """P2-3：把一次工具调用记成 ``gen_ai.tool.*`` span（未启用时零开销）。

        tracer 由工厂持有（``models.tracer``），本方法只做一次 ``getattr``：
        未启用 OTel 时这里是纯判断，不影响工具执行路径。
        """
        tracer = getattr(self._models, "tracer", None)
        if tracer is None:
            return
        try:
            tracer.record_tool(
                tool_name=call.name,
                task_id=task.id,
                role=self.spec.key,
                success=success,
                duration_ms=duration_ms,
                call_id=call.id,
                error=error,
            )
        except Exception:  # noqa: BLE001 —— 可观测性不许打断工具执行
            pass

    async def _execute_tool_calls(
        self, calls: list[ParsedToolCall], task: Task, *, mode: str = "fence",
    ) -> list[str]:
        """执行一批 tool_call，返回**逐条**结果文本（顺序与 ``calls`` 一致）。

        工具不存在 / 未授权 / 入参不合法 / 执行抛异常，都收敛成错误文本回灌
        （不中断循环），每条都记 ``tool_call`` ledger 事件（含入参摘要、success、
        耗时、``mode``）——工具调用必须诚实留痕。

        返回列表而不是拼好的字符串：原生路径要按 ``tool_call_id`` 逐条回传
        ``role="tool"`` 消息，围栏路径要拼成 ``<tool_result>`` 文本块。两条路径
        在**执行**这一层完全共用（P2-1 的「同任务同结果」就靠这一点）。
        """
        available = {tool.name: tool for tool in self._available_tools()}
        blocks: list[str] = []
        for fence_index, call in enumerate(calls):
            started = datetime.now()
            # P2-2：执行**前**先记 started。崩溃发生在工具执行中途时，ledger 里
            # 就会留下「有 started、没有配对的 tool_call」，resume 据此把这次
            # 调用标成「被中断」——否则重跑一次 write_file / run_python 时没人
            # 知道上一次已经动过手。
            self._ledger.emit(
                "tool_call_started",
                task_id=task.id,
                role=self.spec.key,
                tool_name=call.name,
                call_id=call.id,
                fence_index=fence_index,
                mode=mode,
            )
            tool = available.get(call.name)
            if call.error is not None:
                # 解析阶段就失败（原生 arguments 坏 JSON / 围栏块坏 JSON）：
                # 不执行任何工具，把原因原样回灌，模型下一轮自己改。
                result_text = f"错误：{call.error}。请修正后重新发起调用。"
                success = False
            elif tool is None:
                result_text = (
                    f"错误：工具 {call.name!r} 不存在或未授权。"
                    f"本角色可用工具：{'、'.join(sorted(available)) or '（无）'}"
                )
                success = False
            elif self._tool_registry is not None and not self._tool_registry.enforce(
                self.spec.key, call.name
            ):
                # P3-9：角色级权限闸门——拒绝的工具绝不执行（enforce 已记
                # tool_permission_denied 事件）。
                result_text = (
                    f"错误：工具 {call.name!r} 未授权给角色 "
                    f"{self.spec.key!r}，已拒绝执行。"
                )
                success = False
            elif schema_errors := validate_tool_args(tool.input_schema, call.args):
                # R3：执行前的 JSON Schema 强校验（对标 CrewAI _validate_kwargs）。
                # 不抛异常、不改 <tool_result> 形状：把「错在哪 + 正确形状」一起
                # 回灌，让模型在下一轮 ReAct 里自己写对。空 schema / 自由形状
                # schema 一律放行（自定义工具与外部 MCP 向后兼容）。
                hint = schema_hint(tool.input_schema)
                result_text = (
                    f"错误：工具 {call.name!r} 的入参不符合声明的 JSON Schema："
                    + "；".join(schema_errors)
                    + (f"。正确形状：{hint}" if hint else "")
                    + "。请按正确形状重新发起 <tool_call>。"
                )
                success = False
                self._ledger.emit(
                    "tool_schema_invalid",
                    task_id=task.id,
                    role=self.spec.key,
                    tool_name=call.name,
                    errors=schema_errors,
                )
            else:
                blocked, reason = self._guard_input(task, call)
                if blocked:
                    # P2-4：**副作用发生前**拦下。这是 guardrails 唯一真正有意义的
                    # 时点——write_file / run_python 跑完之后再拦只是事后报警。
                    result_text = reason
                    success = False
                else:
                    try:
                        result_text = await tool.run(**call.args)
                        success = True
                        # P2-4：输出相位（回灌前）——拦泄漏，不改执行结果本身。
                        leaked = self._guard_output(task, call, result_text)
                        if leaked is not None:
                            result_text = leaked
                            success = False
                    except Exception as exc:  # 工具内部异常统一兜底回灌
                        result_text = f"错误：工具 {call.name!r} 执行失败：{exc}"
                        success = False
            duration_ms = int((datetime.now() - started).total_seconds() * 1000)
            self._ledger.emit(
                "tool_call",
                task_id=task.id,
                role=self.spec.key,
                tool_name=call.name,
                args=_args_summary(call.args),
                success=success,
                duration_ms=duration_ms,
                mode=mode,
                # 与上面的 tool_call_started 配对（P2-2 的 pending 推导靠这三个键）
                call_id=call.id,
                fence_index=fence_index,
            )
            self._record_tool_span(
                task=task, call=call, success=success,
                duration_ms=duration_ms,
                error=None if success else result_text)
            blocks.append(result_text)
        return blocks

    async def _maybe_create_subtasks(self, task: Task, raw: str) -> list[str]:
        """P2-1：检测 ``<subtask>`` 围栏，经回调创建动态子任务并记 ledger。

        未注入 ``subtask_creator`` 时是零成本 no-op（向后兼容：原行为完全不变）。
        每个合法 spec 都把本任务 id 作 ``parent_id`` 注入后交给回调；回调返回新
        任务 id，随后记一条 ``subtask_created``（含 parent_id/child_id/title）。
        坏 JSON 块记一条 ``subtask_malformed`` 留痕后跳过——不让一个写错的
        子任务请求拖垮本任务。

        R8：返回**被拒原因**列表（没有就是空列表）。原因来自回调对象上的
        ``last_rejection`` —— 裸 ``lambda`` 没有这个属性，此处的 ``getattr``
        会拿到 ``None``，于是既不回灌也不改变任何既有行为。
        """
        if self._subtask_creator is None:
            return []
        rejected: list[str] = []
        for spec in _extract_subtask_specs(raw):
            if "_malformed" in spec:
                self._ledger.emit(
                    "subtask_malformed",
                    task_id=task.id,
                    role=self.spec.key,
                    raw=spec["_malformed"],
                )
                continue
            enriched = {**spec, "parent_id": task.id}
            child_id = self._subtask_creator(enriched)
            if child_id is None:
                # 回调拒绝建。不能静默丢弃——留痕让报告能解释「这个子任务为什么
                # 没出现」，有原因时还要回灌给模型让它改（见 _execute_traced）。
                reason = getattr(
                    self._subtask_creator, "last_rejection", None)
                self._ledger.emit(
                    "subtask_rejected",
                    task_id=task.id,
                    role=self.spec.key,
                    title=str(spec.get("title", "")),
                    reason=reason or "",
                )
                if reason:
                    rejected.append(str(reason))
                continue
            self._ledger.emit(
                "subtask_created",
                parent_id=task.id,
                child_id=child_id,
                title=str(spec.get("title", "")),
                role=str(spec.get("role", "")),
            )
        return rejected

    async def _retry_after_subtask_rejection(
        self,
        task: Task,
        messages: list[Message],
        alias: str,
        raw: str,
        reasons: Sequence[str],
    ) -> str:
        """R8：把子任务被拒的原因回灌给模型，要一次调整后的回复。

        只做一轮。给它一次改的机会，改不好就按原样继续——**本任务自己的交付物
        才是主体**，子任务拆不成不该把任务本身拖垮。重试后再次请求的子任务照建，
        但再被拒也不追问（否则就成了无限套娃）。
        """
        joined = "\n".join(f"- {reason}" for reason in reasons)
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": (
            "你请求的子任务没有被接受，原因如下：\n"
            f"{joined}\n\n"
            "请据此调整后重新输出本任务的**完整最终回复**："
            "换一个真实存在的角色、改为自己完成，或减少拆分。"
            "仍然必须包含本任务自己的 `file=` 交付物围栏。"
        )})
        self._ledger.emit(
            "subtask_rejection_fed_back",
            task_id=task.id,
            role=self.spec.key,
            reasons=list(reasons),
        )
        text = await self._acquire_reply(task, messages, alias)
        await self._maybe_create_subtasks(task, text)
        return text

    def _guard_truncation(self, completion: CompletionResult, task: Task) -> None:
        """回复撞上输出上限时直接失败（与单次调用路径同一条病因说明）。"""
        if _is_truncated(completion):
            # 回复是**被输出上限截断**的，不是模型写完了。此时围栏必然未闭合，
            # 而按「格式错误」重试毫无意义 —— 同一个上限、同一段内容，只会再
            # 截断一次。这里必须把病因说清楚，让重试有得改。
            self._fail(
                task,
                f"模型回复被输出上限截断（finish_reason={completion.finish_reason}，"
                f"已输出 {completion.completion_tokens} token）：请精简内容，"
                f"或把交付物拆成更小的几次输出后重试。",
            )

    async def _produce_with_corrections(
        self,
        task: Task,
        messages: list[Message],
        alias: str,
        final_text: str,
    ) -> tuple[ParsedOutput, str]:
        """P2-8：parse + validate，失败时把错误回灌给模型重调 LLM 自我修正。

        最多 ``max_self_corrections`` 次重调：每次失败都把错误信息作为新的 user
        消息（连同上一条 assistant 回复）追加进 messages，再 ``models.call`` 一次。
        每次修正记 ``agent_self_correction``（含 task_id/role/attempt/error）；
        重调同样走 ``models.call``，token/成本照记。达到上限仍失败 → 抛
        :class:`AgentExecutionError`（与原行为一致，交给调度器重试）。

        ``max_self_corrections=0`` 时循环只跑一轮，首次失败即抛错——与改造前完全
        一致。Provider 异常（网络/鉴权）不在这里 catch，原样上抛。
        """
        parsed: ParsedOutput | None = None
        notes = ""
        for attempt in range(self._max_self_corrections + 1):
            try:
                parsed = self._parse(final_text, task)
                notes = _join_notes(
                    parsed.notes, *_language_notes(final_text, task)
                )
                self._emit_notes(task, notes)
                self._validate(parsed, task)
                return parsed, notes
            except AgentExecutionError as exc:
                if attempt >= self._max_self_corrections:
                    raise
                # 自我修正：留痕 → 把坏回复与错误回灌 → 重调 LLM。
                self._ledger.emit(
                    "agent_self_correction",
                    task_id=task.id,
                    role=self.spec.key,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                messages.append({"role": "assistant", "content": final_text})
                messages.append(
                    {"role": "user", "content": self._correction_hint(str(exc))}
                )
                completion = await self._invoke_model(task, messages, alias)
                self._guard_truncation(completion, task)
                final_text = completion.text
                # 自我修正轮里再出现的子任务请求照建，但不再为「被拒」额外追问：
                # 纠正循环本身已经有轮次上限，再套一层只会成倍放大成本。
                await self._maybe_create_subtasks(task, final_text)
        raise AssertionError("unreachable")

    def _correction_hint(self, error: str) -> str:
        """把一次 parse/validate 失败变成给模型的修正提示。"""
        return (
            "你上一次输出没有通过校验，问题如下：\n"
            f"{error}\n\n"
            "请严格按系统提示里的「产物输出协议」重新输出**完整的交付物**"
            "（每个交付物一个带正确 `file=` 路径的围栏块），不要只写解释文字。"
        )

    def _parse(self, raw: str, task: Task) -> ParsedOutput:
        """调 ``parse_output``，把任何解析失败收敛成 ``AgentExecutionError``。"""
        try:
            return self.parse_output(raw, task)
        except AgentExecutionError:
            raise
        except Exception as exc:
            self._fail(task, f"解析模型回复失败：{exc}", cause=exc)

    def _validate(self, parsed: ParsedOutput, task: Task) -> None:
        """调 ``validate_output``，把任何校验失败收敛成 ``AgentExecutionError``。"""
        try:
            self.validate_output(parsed, task)
        except AgentExecutionError:
            raise
        except Exception as exc:
            self._fail(task, f"产物契约校验失败：{exc}", cause=exc)

    def _commit(self, parsed: ParsedOutput, task: Task) -> list[ArtifactRecord]:
        """按契约声明顺序逐文件落盘（顺序确定、可复现）。

        只落**契约里声明过**的产物（``validate_output`` 已保证没有别的），并且
        一律以 ``spec.path`` 提交：交付物的 kind 权威在契约上，模型在围栏上标的
        语言名只作提示。落盘异常按 ``ArtifactStore`` 自己声明的错误类型收敛，
        不让裸 ``ValueError`` 逃到调度器。
        """
        produced = {
            _canonical_path(path): content for path, content in parsed.files.items()
        }
        records: list[ArtifactRecord] = []
        try:
            for spec in task.deliverables:
                content = produced.get(spec.path)
                if content is None:
                    continue  # 非必需且未产出：validate_output 已放行
                records.append(
                    self._store.commit(
                        spec.path, content, spec=spec, task=task,
                        path_scope=self.path_scope or None, agent=self.agent_key)
                )
        except (
            ArtifactConflictError,
            ArtifactScopeError,
            ArtifactTooLargeError,
            UndeclaredArtifactError,
            UnsafePathError,
        ) as exc:
            self._fail(task, f"产物流盘失败：{exc}", cause=exc)
        return records

    def _emit_notes(self, task: Task, notes: str) -> None:
        """说明文字落 ledger（**先于**校验与落盘，空/纯空白 notes 不发）。

        事件名 ``task_notes`` 不在 spec §14.1 的事件表里，属纯增量（同 Task 21 的
        ``llm_retry``）：看板/报告按事件名过滤，忽略未知事件。这条事件的读者是
        人 —— 模型在产物之外说了什么（含解析器的重复路径告警与截断提示）不该只
        存在于内存里的 ``TaskResult``。
        """
        if notes.strip():
            self._ledger.emit(
                "task_notes",
                task_id=task.id,
                role=self.spec.key,
                attempt=task.attempts,
                notes=notes,
            )

    def _fail(
        self, task: Task, message: str, *, cause: Exception | None = None
    ) -> NoReturn:
        """抛带 ``task_id``/``role`` 的执行错误（``cause`` 保留原始异常链）。"""
        error = AgentExecutionError(message, task_id=task.id, role=self.spec.key)
        if cause is None:
            raise error
        raise error from cause


# ── 渲染工具（实现已搬到 prompting.py，此处保留旧名字）────────────────────
#
# 这些带下划线的别名是**兼容层**：渲染逻辑现在住在 ``agents/prompting.py``
# （纯函数、可独立断言），本模块只做再导出，既有的调用点与测试不受影响。
# 模型输出解析（``<tool_call>`` / ``<subtask>`` / ``<message>``）留在本模块：
# 它与 ReAct 循环、纠正循环紧耦合，且需要和上面的协议模板放在一起读。

_render_pack = render_pack
_section = section
_render_task = render_task
_render_deliverables = render_deliverables
_is_truncated = is_truncated
_render_upstream = render_upstream
_render_decisions = render_decisions
_render_excerpts = render_excerpts
_join_notes = join_notes
_truncate_summary = truncate_summary
_canonical_path = canonical_path
_language_notes = language_notes
_fence_languages = fence_languages
_START_FENCE_RE = START_FENCE_RE
_TRUNCATED_FINISH_REASONS = TRUNCATED_FINISH_REASONS
