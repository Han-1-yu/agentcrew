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

import re
from abc import ABC
from collections.abc import Sequence
from datetime import datetime
from typing import NoReturn

from agentcrew.artifacts.parser import (
    ParsedOutput,
    normalize_lang,
    parse_fenced_blocks,
)
from agentcrew.artifacts.spec import ArtifactRecord
from agentcrew.artifacts.store import (
    ArtifactConflictError,
    ArtifactStore,
    ArtifactTooLargeError,
    UndeclaredArtifactError,
)
from agentcrew.context.context_bus import (
    TRUNCATION_NOTICE,
    ContextPack,
    DecisionRecord,
    FileExcerpt,
    UpstreamDigest,
)
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import Message
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskResult
from agentcrew.utils.files import UnsafePathError, safe_relpath

#: 默认摘要的长度上限（spec §8.1「取 notes 前 400 字符」）。
_SUMMARY_LIMIT = 400

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
    ) -> None:
        """``catalog`` 是本 run 的角色目录（含自定义角色），可为 ``None``。

        只有需要「看见整个团队」的 Agent 用它（PM 的拆解提示词要从目录渲染
        可用角色清单，否则自定义角色永远不会出现在计划里）；其余 Agent 不读。
        ``None`` 是合法的降级态：直接构造 Agent 的单测没有目录可给。
        """
        self.spec = spec
        self._models = models
        self._store = store
        self._ledger = ledger
        self._project_goal = project_goal
        self._catalog = catalog

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
        alias = self._resolve_alias()
        self._configure_replay(alias, task)
        completion = await self._models.call(
            alias,
            messages,
            temperature=self.spec.temperature,
            max_tokens=self.spec.max_output_tokens,
            purpose="execute",
            role=self.spec.key,
        )

        if _is_truncated(completion):
            # 回复是**被输出上限截断**的，不是模型写完了。此时围栏必然未闭合，
            # 而按「格式错误」重试毫无意义 —— 同一个上限、同一段内容，只会再
            # 截断一次。实测真实运行里架构师连续 3 次都停在 8192，每次都被判成
            # 「解析失败」，重试三次全废。这里必须把病因说清楚，让重试有得改。
            self._fail(
                task,
                f"模型回复被输出上限截断（finish_reason={completion.finish_reason}，"
                f"已输出 {completion.completion_tokens} token）：请精简内容，"
                f"或把交付物拆成更小的几次输出后重试。",
            )

        parsed = self._parse(completion.text, task)
        notes = _join_notes(parsed.notes, *_language_notes(completion.text, task))
        self._emit_notes(task, notes)
        self._validate(parsed, task)
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
        system = "\n\n".join(
            (
                self.spec.system_prompt,
                f"## 输出规范\n{self.spec.output_contract}",
                _ARTIFACT_FORMAT,
                # 明说 token 预算：撞上上限时回复会被**截断**，围栏随之未闭合，
                # 整次输出全废。实测架构师连写三版 8000 token 的文档全部撞线。
                f"## 输出长度\n本次回复的全部内容（含围栏与说明文字）上限约 "
                f"{self.spec.max_output_tokens} token。宁可写紧凑、写到要点为止，"
                f"也不要写满被截断——一旦截断，本次输出整体作废。",
            )
        )
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
        """默认摘要：``notes`` 前 400 字符；没有说明文字时回落到产物清单。

        「没有说明文字」是常态而非异常：只输出围栏块的规范回复里一个字的说明
        都没有，而 ``summary`` 是下游上下文包的信息来源，空串会让下游只看到
        一个文件名列表。
        """
        notes = result.notes.strip()
        if notes:
            return notes[:_SUMMARY_LIMIT]
        if not result.artifacts:
            return "执行完成，未产出文件"
        produced = "、".join(record.path for record in result.artifacts)
        return f"产出 {len(result.artifacts)} 个产物：{produced}"

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _resolve_alias(self) -> str:
        """spec §12.2 的三级优先级（高者胜）。

        角色级 ``RoleSpec.model`` → ``settings.role_bindings[role]`` → 默认模型；
        字面量 ``"default"`` 是「没指定」的哨兵，不是模型名。
        """
        if self.spec.model != _DEFAULT_ALIAS:
            return self.spec.model
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


# ── 上下文包渲染（模块级纯函数，便于单测）────────────────────────────────


def _render_pack(pack: ContextPack, fix_hints: Sequence[str]) -> str:
    """把上下文包渲染成 user 消息（spec §11.2 六级内容的可读形态）。

    末两段是**通道**而非信息：``fix_hints`` 是返工的唯一解释（模型必须看到），
    ``pack.truncated`` 时逐字追加 :data:`TRUNCATION_NOTICE`，免得模型把「没给
    全文」误当成「上游不存在」。
    """
    sections = [
        _section("项目目标", pack.project_goal),
        _section("本任务", _render_task(pack.task)),
    ]
    if pack.upstream:
        sections.append(_section("上游任务摘要", _render_upstream(pack.upstream)))
    if pack.decisions:
        sections.append(_section("全局决策", _render_decisions(pack.decisions)))
    if pack.file_tree:
        sections.append(
            _section("产物文件树", "\n".join(f"- {path}" for path in pack.file_tree))
        )
    if pack.key_excerpts:
        sections.append(_section("关键文件节选", _render_excerpts(pack.key_excerpts)))
    # 交付物契约放到**最后**（仅早于返工意见）。
    # 它是文件清单的唯一权威；而上面那些节选可能来自架构文档、带着另一套文件
    # 名与目录结构 —— 两者平级呈现时，实测真实模型会跟设计文档走：PM 契约写
    # `backend/src/app.js`，模型却产 `backend/src/server.js`，连续 3 次尝试全废、
    # 下游 5 个任务阻塞。放在末尾既给了优先级，也吃到近因效应。
    sections.append(
        _section("交付物契约（文件清单以此为准）", _render_deliverables(pack.task))
    )
    hints = [hint.strip() for hint in fix_hints if hint.strip()]
    if hints:
        sections.append(
            _section("上次评审意见，请修正", "\n".join(f"- {h}" for h in hints))
        )
    text = "\n\n".join(sections)
    if pack.truncated:
        text = f"{text}\n\n{TRUNCATION_NOTICE}"
    return text


def _section(title: str, body: str) -> str:
    """一个 markdown 二级标题段。"""
    return f"## {title}\n{body}"


def _render_task(task: Task) -> str:
    """本任务的元信息与验收标准（描述与验收标准不可压缩，spec §11.2 级 1）。"""
    lines = [
        f"- id: {task.id}",
        f"- 角色: {task.role}",
        f"- 标题: {task.title}",
        f"- 描述: {task.description}",
    ]
    if task.brief:
        lines.append(f"- 摘要: {task.brief}")
    criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria)
    return "\n".join(lines) + f"\n\n验收标准：\n{criteria or '- （无）'}"


def _render_deliverables(task: Task) -> str:
    """交付物契约：路径是模型唯一被允许写出的东西，必须逐条列清。"""
    if not task.deliverables:
        return "- （本任务无文件交付物）"
    lines: list[str] = [
        "**本清单是本次输出文件路径的唯一权威。** 上游设计文档若给出不同的文件名"
        "或目录，**以本清单为准**：先按下面的路径落盘，设计文档里的结构体现在文件"
        "内容里。输出任何未列出的路径都会让本次执行直接失败。"
    ]
    for spec in task.deliverables:
        flags = "必需" if spec.required else "可选"
        line = f"- `{spec.path}`（{spec.kind}，{flags}）：{spec.description}"
        if spec.must_contain:
            line += f"｜必须包含：{'、'.join(spec.must_contain)}"
        lines.append(line)
    return "\n".join(lines)


#: provider 表示「回复撞上输出上限」的停止原因：OpenAI 兼容端点是 ``length``，
#: Anthropic 是 ``max_tokens``。两者含义相同，都必须与「模型写完了」区分开。
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})


def _is_truncated(completion: object) -> bool:
    """这次回复是不是被输出上限截断的？

    用 ``getattr`` 而不是类型标注，是为了让测试替身（只有 ``text``/``usage`` 的
    假 provider）也能走同一条路径。
    """
    reason = getattr(completion, "finish_reason", None) or ""
    return str(reason).lower() in _TRUNCATED_FINISH_REASONS


def _render_upstream(digests: Sequence[UpstreamDigest]) -> str:
    """上游摘要：只给 ``brief``/``summary``/文件路径，不给全文（spec §11.2 级 2）。"""
    blocks: list[str] = []
    for digest in digests:
        parts = [
            f"### {digest.task_id} · {digest.role} · {digest.title}",
            f"brief: {digest.brief}" if digest.brief else "",
            f"summary: {digest.summary}" if digest.summary else "",
            f"文件: {'、'.join(digest.files)}" if digest.files else "",
        ]
        blocks.append("\n".join(part for part in parts if part))
    return "\n\n".join(blocks)


def _render_decisions(decisions: Sequence[DecisionRecord]) -> str:
    """全局决策记录（最新在前，由 ``ContextBus`` 决定顺序）。"""
    return "\n".join(
        f"- [{record.task_id} · {record.role}] {record.decision}"
        for record in decisions
    )


def _render_excerpts(excerpts: Sequence[FileExcerpt]) -> str:
    """关键文件节选；被预算截断的逐条标注，避免模型把节选当全文。"""
    blocks: list[str] = []
    for excerpt in excerpts:
        title = f"### {excerpt.path}"
        if excerpt.truncated:
            title += "（仅节选，未全文展示）"
        blocks.append(f"{title}\n{excerpt.content}")
    return "\n\n".join(blocks)


def _join_notes(*parts: str) -> str:
    """拼接说明段落，丢掉空段（``ParsedOutput.notes`` 原样保留）。"""
    return "\n".join(part for part in parts if part)


def _canonical_path(raw: str) -> str:
    """解析路径的契约口径：与 ``ArtifactSpec.path`` 同一条规范化。

    不安全路径（``../x``、绝对路径、Windows 保留名…）返回原样字符串：解析器
    刻意原样透传它们，好让诊断信息是模型真正写下的那个路径；这里它们永远不可能
    等于任何已声明路径，于是按「未声明」报出，而不是抛 ``UnsafePathError``。
    """
    try:
        return safe_relpath(raw).as_posix()
    except UnsafePathError:
        return raw


def _language_notes(raw: str, task: Task) -> list[str]:
    """围栏语言名与契约 ``kind`` 的一致性交叉检查（只记提示，不判失败）。

    这是 ``normalize_lang``（``artifacts/parser.py``）的生产消费者：模型把 python
    文件标成 ``json`` 值得让人看见，却不值得判失败 —— 真正的结构校验是 G1 的职责，
    且以契约 ``kind`` 为准（这里落盘用的也正是契约里的 ``ArtifactSpec``）。
    只对契约声明过的路径比较；未声明的路径由 ``validate_output`` 负责。
    """
    declared = {spec.path: spec.kind for spec in task.deliverables}
    notes: list[str] = []
    for path, lang in _fence_languages(raw).items():
        expected = declared.get(_canonical_path(path))
        actual = normalize_lang(lang) if lang else None
        if expected is None or actual is None or actual == expected:
            continue
        notes.append(
            f"注意：{_canonical_path(path)} 的围栏语言标注 {lang!r} 与契约声明的 "
            f"kind {expected!r} 不一致，已按契约 kind 处理"
        )
    return notes


def _fence_languages(raw: str) -> dict[str, str | None]:
    """``raw`` 中每个 ``file=`` 块的围栏语言名（按路径，后写覆盖前写）。

    只服务于上面的一致性提示。它复刻 ``parser.py`` 钉死的围栏规则（开栏顶格且
    ≥3 个反引号；闭栏是长度不短于开栏的纯反引号行），因此 4 反引号块**体内**的
    示例行不会被误读成真实块 —— 那正是 README 里写运行命令的合法形态（Task 10
    的裁定：N 开 M 闭当且仅当 M ≥ N）。扫描结果永不参与裁决，与解析器失配的
    最坏后果只是一条多出或缺失的提示。
    """
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    found: dict[str, str | None] = {}
    index = 0
    while index < len(lines):
        match = _START_FENCE_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        fence_length = len(match.group("fence"))
        close = len(lines)
        for candidate in range(index + 1, len(lines)):
            stripped = lines[candidate].strip()
            if (
                stripped
                and stripped.strip("`") == ""
                and len(stripped) >= fence_length
            ):
                close = candidate
                break
        path = (match.group("path") or "").strip().replace("\\", "/")
        if path:
            found[path] = match.group("lang").strip() or None
        index = close + 1
    return found
