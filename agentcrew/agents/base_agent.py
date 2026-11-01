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

import json
import re
from abc import ABC
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, NoReturn

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
    ArtifactStore,
    ArtifactTooLargeError,
    UndeclaredArtifactError,
)
from agentcrew.artifacts.summarizer import summarize_records
from agentcrew.context.context_bus import ContextPack
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import CompletionResult, Message
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskResult
from agentcrew.utils.files import UnsafePathError
from agentcrew.tools import Tool, ToolRegistry

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


class ParsedToolCall:
    """从模型回复里解析出的一次工具调用（轻量值对象，不进 pydantic 体系）。"""

    __slots__ = ("name", "args")

    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.name = name
        self.args = args


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
        except json.JSONDecodeError:
            calls.append(ParsedToolCall("<malformed>", {"_raw": body[:200]}))
            continue
        if not isinstance(data, dict):
            calls.append(ParsedToolCall("<malformed>", {"_raw": body[:200]}))
            continue
        name = str(data.get("name", "")).strip()
        args = data.get("args")
        if not isinstance(args, dict):
            args = {}
        calls.append(ParsedToolCall(name, args))
    return calls


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
        subtask_creator: Callable[[dict], str] | None = None,
        max_self_corrections: int = 2,
        stream: bool = False,
        message_router: Callable[[str, str], str] | None = None,
        max_message_rounds: int = 5,
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
        self._max_self_corrections = max_self_corrections
        self._stream = stream
        self._message_router = message_router
        self._max_message_rounds = max_message_rounds

    def set_subtask_creator(
        self, creator: Callable[[dict], str] | None
    ) -> None:
        """运行时更换子任务回调（AgentFactory 在调度器换板后刷新缓存实例用）。"""
        self._subtask_creator = creator

    # ── 模板方法（子类不重写）────────────────────────────────────────────

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
        """
        started_at = datetime.now()
        self._ledger.emit(
            "task_started",
            task_id=task.id,
            role=self.spec.key,
            attempt=task.attempts,
        )

        messages = self.build_messages(task, pack, fix_hints)
        alias = self._resolve_alias(task)
        final_text = await self._obtain_final_text(task, messages, alias)

        # P2-1：检测 <subtask> 围栏并创建动态子任务（仅当注入了 creator）。
        # 在 parse 之前做：子任务请求不影响本任务自己的产物解析，且创建后立即
        # 记 ledger，子任务在下一轮调度即可被派发。
        await self._maybe_create_subtasks(task, final_text)

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
    ) -> list[Message]:
        """默认提示词：system 走角色规格，user 走渲染后的上下文包。"""
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
        tools_section = self._tools_section()
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

    def _tools_section(self) -> str:
        """渲染进系统提示词的工具说明段；无可用工具时返回空串。"""
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
                f"{name}{"（必填）" if name in required else ""}" for name in props
            ) or "无参数"
            lines.append(f"- `{tool.name}`：{tool.description} 入参：{args_desc}")
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
        self, task: Task, messages: list[Message], alias: str
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
            )
        parts: list[str] = []
        finish_reason: str | None = None
        async for chunk in self._models.stream_call(
            alias,
            messages,
            temperature=self.spec.temperature,
            max_tokens=self.spec.max_output_tokens,
            purpose="execute",
            role=self.spec.key,
        ):
            parts.append(chunk.delta)
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
        )

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
            for msg in outgoing:
                reply = self._message_router(msg.to, msg.content)
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

        最多 ``self._max_tool_calls`` 轮。模型某次回复不再含 ``<tool_call>`` 即
        为最终产物。触顶仍在发 tool_call 时**降级**：记一条 ``tool_loop_exhausted``
        警告，把最后一次回复当最终产物（不判失败——重试同上限只会再循环一次）。
        """
        completion = None
        exhausted = False
        for round_index in range(self._max_tool_calls):
            self._configure_replay(alias, task)
            completion = await self._invoke_model(task, messages, alias)
            self._guard_truncation(completion, task)
            calls = _extract_tool_calls(completion.text)
            if not calls:
                break
            if round_index >= self._max_tool_calls - 1:
                exhausted = True
                break
            results_block = await self._execute_tool_calls(calls, task)
            messages.append({"role": "assistant", "content": completion.text})
            messages.append({"role": "user", "content": results_block})
        if exhausted:
            self._ledger.emit(
                "tool_loop_exhausted",
                task_id=task.id,
                role=self.spec.key,
                max_tool_calls=self._max_tool_calls,
            )
        assert completion is not None  # 循环至少跑一轮
        return completion.text

    async def _execute_tool_calls(
        self, calls: list[ParsedToolCall], task: Task
    ) -> str:
        """执行一批 tool_call，把每条结果包成 ``<tool_result>`` 块拼接返回。

        工具不存在 / 未授权 / 执行抛异常，都收敛成错误文本回灌（不中断循环），
        每条都记 ``tool_call`` ledger 事件（含入参摘要、success、耗时）——工具
        调用必须诚实留痕。
        """
        available = {tool.name: tool for tool in self._available_tools()}
        blocks: list[str] = []
        for call in calls:
            started = datetime.now()
            tool = available.get(call.name)
            if tool is None:
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
            else:
                try:
                    result_text = await tool.run(**call.args)
                    success = True
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
            )
            blocks.append(
                f'<tool_result name="{call.name}">\n{result_text}\n</tool_result>'
            )
        return "\n\n".join(blocks)

    async def _maybe_create_subtasks(self, task: Task, raw: str) -> None:
        """P2-1：检测 ``<subtask>`` 围栏，经回调创建动态子任务并记 ledger。

        未注入 ``subtask_creator`` 时是零成本 no-op（向后兼容：原行为完全不变）。
        每个合法 spec 都把本任务 id 作 ``parent_id`` 注入后交给回调；回调返回新
        任务 id，随后记一条 ``subtask_created``（含 parent_id/child_id/title）。
        坏 JSON 块记一条 ``subtask_malformed`` 留痕后跳过——不让一个写错的
        子任务请求拖垮本任务。
        """
        if self._subtask_creator is None:
            return
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
            self._ledger.emit(
                "subtask_created",
                parent_id=task.id,
                child_id=child_id,
                title=str(spec.get("title", "")),
                role=str(spec.get("role", "")),
            )

    def _guard_truncation(self, completion: object, task: Task) -> None:
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
                    self._store.commit(spec.path, content, spec=spec, task=task)
                )
        except (
            ArtifactConflictError,
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
