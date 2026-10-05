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

from datetime import datetime

from agentcrew.artifacts.contract import has_placeholder, parse_check
from agentcrew.artifacts.spec import ArtifactRecord, ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.task.task import ReviewRecord, Task

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
        created_at=datetime.now(),
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
    if len(content.encode("utf-8")) < spec.min_bytes:
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
    for needle in spec.must_contain:
        if needle not in content:
            findings.append((
                f"{spec.path} 缺少必需符号：{needle}",
                f"在 {spec.path} 中定义 {needle}",
            ))
    return findings
