"""G1 静态校验门禁（spec §10.1）：确定性、零 token 的契约检查。

``run_static`` 是三个门禁里唯一不烧 token 的一个 —— 因此它必须在任何模型评审
被付费之前拦下一切机器可判的问题，且**一次回齐全部问题**：六类检查各自独立
收集、绝不早退。早退会让模型每轮只需修一个错，把一次返工拖成 N 次；而 issue /
fix_hint 的合并列表正是返工提示的来源（Task 24 渲染进上下文包）。

issue 说「哪里不对」，配对的 ``fix_hint`` 说「去做什么」。检查顺序即 issue
输出顺序：

1. 缺必需产物（``required=True`` 而未落盘）→ ``缺少必需产物：{path}``
2. 契约外产物 → ``产出未在契约中声明的文件：{path}``
3. 尺寸下界（``size < min_bytes``）→ ``{path} 为空文件``
4. 按 ``kind`` 的结构解析 → ``{path} {parse_check 返回的消息}``
5. 占位内容 → ``{path} 仅含占位内容``
6. 必需符号缺失 → ``{path} 缺少必需符号：{needle}``

两处刻意的边界：

- **只查尺寸下界**。``max_bytes`` 是 ``ArtifactStore.commit`` 的写入期硬上限
  （超限抛 ``ArtifactTooLargeError``，由 ``tests/test_artifact_store.py`` 钉死），
  超限字节根本落不了盘，G1 永远见不到过大产物 —— 再查一遍只会是死代码。
- **未落盘的路径一律不读**。文件不存在，``store.read`` 会抛
  ``FileNotFoundError``：必需缺失已由检查 1 报出并跳过检查 3–6，可选缺失本就
  合法。已落盘的产物（含 ``required=False`` 的）则一律照跑内容检查。

第 4 条把解析器消息前缀上产物路径（``{path} {msg}``，消息原话一字不改）：
``issues`` 会被 Task 30 的终端报告与 Task 27 的跨层聚合**单独**读取，而一个
任务常声明半打产物 —— 消息不带路径就说不出是哪个文件；spec §10.1 的示例
消息也正是路径在前的形式。
"""

import ast
from pathlib import Path

from agentcrew.artifacts.contract import has_placeholder, parse_check
from agentcrew.artifacts.spec import ArtifactRecord, ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.task.task import ReviewRecord, Task
from agentcrew.utils.clock import utc_now

#: G1 的 ``ReviewRecord.reviewer`` 标识（spec §5.3）。
STATIC_REVIEWER = "static:contract"

#: 一条 issue 与其配对 ``fix_hint``；1:1 配对由构造保证。
_Finding = tuple[str, str]


def run_static(task: Task, store: ArtifactStore, *, round_no: int) -> ReviewRecord:
    """对一个任务的产出做 G1 静态校验，返回本轮 ``ReviewRecord``。

    ``score`` 恒为 ``None``（静态层不打分）；``verdict="approve"`` 当且仅当
    ``issues`` 为空。
    """
    records = store.by_task(task.id)
    produced = {record.path for record in records}
    declared = {spec.path for spec in task.deliverables}

    findings: list[_Finding] = []
    findings += _missing_findings(task.deliverables, produced)
    findings += _undeclared_findings(records, declared)
    for spec in task.deliverables:
        # 未产出的路径不得读取：缺失已由检查 1 报出（必需）或本就合法（可选）。
        if spec.path in produced:
            findings += _content_findings(spec, store.read(spec.path))

    issues = [issue for issue, _ in findings]
    return ReviewRecord(
        task_id=task.id,
        round=round_no,
        layer="static",
        verdict="approve" if not issues else "reject",
        score=None,
        issues=issues,
        fix_hints=[hint for _, hint in findings],
        reviewer=STATIC_REVIEWER,
        created_at=utc_now(),
    )


def _missing_findings(specs: list[ArtifactSpec], produced: set[str]) -> list[_Finding]:
    """检查 1：``required=True`` 的产物必须有落盘记录。"""
    return [
        (f"缺少必需产物：{spec.path}", f"补齐产物 {spec.path}")
        for spec in specs
        if spec.required and spec.path not in produced
    ]


def _undeclared_findings(
    records: list[ArtifactRecord], declared: set[str]
) -> list[_Finding]:
    """检查 2：该任务写出了契约外的路径（Review Focus #1 的模型幻觉）。"""
    return [
        (
            f"产出未在契约中声明的文件：{record.path}",
            f"删除契约外产物 {record.path}",
        )
        for record in records
        if record.path not in declared
    ]


def _content_findings(spec: ArtifactSpec, content: str) -> list[_Finding]:
    """检查 3–6：已落盘产物的内容检查；顺序即 issue 输出顺序。"""
    findings: list[_Finding] = []
    # A-5：孤立 UTF-16 代理码点（截断的多字节输出里常见）会让 ``encode`` 抛
    # ``UnicodeEncodeError``，穿透 Gatekeeper 后在 REVIEW 阶段被直接判 FAILED、
    # 不给返工机会——一次格式异常烧掉整个任务。用 ``errors="replace"`` 按
    # 欠尺寸（0 字节）产出 issue，让模型走正常返工通道。
    if len(content.encode("utf-8", errors="replace")) < spec.min_bytes:
        findings.append((
            f"{spec.path} 为空文件",
            f"为 {spec.path} 补充实质内容（至少 {spec.min_bytes} 字节）",
        ))
    parse_error = parse_check(spec.kind, content)
    if parse_error is not None:
        findings.append((
            f"{spec.path} {parse_error}",
            f"修正 {spec.path} 的语法错误后重新产出",
        ))
    if has_placeholder(content):
        findings.append((
            f"{spec.path} 仅含占位内容",
            f"用真实实现替换 {spec.path} 中的占位内容",
        ))
    lowered = content.lower()
    for needle in spec.must_contain:
        # 大小写**不敏感**地匹配。实测真实模型产出 `Flask==3.0.3`，而 PM 定的
        # must_contain 是 `flask`：大小写敏感的 `in` 会让一个完全正确的产物连续
        # 失败多轮，而失败信息只说「缺少必需符号：flask」—— 文件里明明写着 Flask，
        # 从症状根本推不出病因。假阳性（把 `DEF F()` 认成 `def f()`）的风险远小于
        # 假阴性，而符号语义真错时 G2 的 LLM 评审仍会把关。
        if needle.lower() not in lowered:
            findings.append((
                f"{spec.path} 缺少必需符号：{needle}",
                f"在 {spec.path} 中定义 {needle}",
            ))
    return findings


# ── P2-4：扩展代码可执行性校验 ──────────────────────────────────────────────
#
# 与 ``run_static`` 的契约检查互补：本层在 G1 既有检查之后跑，对 ``kind=python``
# 的产物额外做语法 / 危险导入 / 非空校验。结果分两级——
#
# - **error（阻断）**：语法错误、空文件 → 使 G1 不通过；
# - **warning（不阻断）**：危险模块导入 → 只记 ``g1_extended_check`` ledger 事件，
#   不挡放行（生成代码引用 ``os`` 不必然是错，交 G2 与人工把关）。
#
# 与 :func:`agentcrew.artifacts.contract.parse_check` 一样，这些检查**绝不抛异常**：
# 畸形产物是数据，错误以中文消息回灌给返工方。

#: 视为「危险」的标准库模块：生成代码引入它们往往意味着越过沙箱（os.system /
# subprocess / 网络 / 动态加载）。只报 warning——G1 不在宿主上跑代码，无法判定
# 用得对不对，只把它亮出来。
_DANGEROUS_IMPORTS: frozenset[str] = frozenset({
    "os", "sys", "subprocess", "shutil", "socket", "ctypes",
    "multiprocessing", "pickle", "shelve",
})


class ExtendedCheckResult:
    """``run_extended_checks`` 的结果：阻断项与仅留痕项分开。"""

    __slots__ = ("errors", "warnings")

    def __init__(self, errors: list[str] | None = None,
                 warnings: list[str] | None = None) -> None:
        self.errors: list[str] = list(errors or [])
        self.warnings: list[str] = list(warnings or [])


def check_python_syntax(path: Path | str, content: str) -> list[str]:
    """用 ``ast.parse`` 检查 Python 语法；返回错误消息列表（空 = 通过）。"""
    try:
        ast.parse(content)
    except SyntaxError as exc:
        lineno = getattr(exc, "lineno", None)
        where = f"line {lineno}" if lineno is not None else "语法错误"
        return [f"{path} Python 语法错误：{where}: {exc.msg}"]
    except (ValueError, RecursionError):
        return [f"{path} Python 语法无法解析"]
    return []


def check_imports(content: str) -> list[str]:
    """扫描可疑导入（危险模块）；返回 warning 列表（空 = 通过）。

    用 ``ast`` 遍历 ``Import`` / ``ImportFrom`` 节点，比正则更准（不误报注释里
    的 ``import os`` 字样）。命中危险模块只记 warning。
    """
    warnings: list[str] = []
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return warnings  # 语法错误归 check_python_syntax，这里不重复报
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _DANGEROUS_IMPORTS:
                    warnings.append(f"导入了潜在危险模块：{alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in _DANGEROUS_IMPORTS:
                warnings.append(f"从 {node.module} 导入（潜在危险模块）")
    return warnings


def check_file_nonempty(path: Path | str, content: str) -> list[str]:
    """检查文件非空；空文件返回一条阻断错误。"""
    if not content.strip():
        return [f"{path} 为空文件"]
    return []


def run_extended_checks(
    artifacts: list[tuple[Path | str, str]], kind: str
) -> ExtendedCheckResult:
    """按产物 ``kind`` 跑扩展检查：python 做语法 + 危险导入，其余只做非空检查。

    ``artifacts`` 是 ``[(路径, 内容)]``；同一 kind 的产物批量传入。语法错误与空
    文件进 ``errors``（阻断），危险导入进 ``warnings``（仅留痕）。
    """
    result = ExtendedCheckResult()
    for path, content in artifacts:
        result.errors.extend(check_file_nonempty(path, content))
        if kind == "python":
            result.errors.extend(check_python_syntax(path, content))
            result.warnings.extend(check_imports(content))
    return result
