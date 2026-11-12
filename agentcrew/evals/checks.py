"""断言的具体实现（P2-5）。

只提供「一眼能看出对错」的几种：产物存在 / 含某段文本 / 不含 / 匹配正则 /
行数区间 / 数量 / 任务终态 / 事件次数。**不提供 LLM 评审**——判不动的就写成人看，
不要伪装成自动化的确定性。

每种 check 的写法（JSONL 里一条）：

```json
{"kind": "artifact_contains", "path": "README.md", "text": "logstat"}
{"kind": "artifact_matches", "path": "src/logstat.py", "pattern": "def\\s+main"}
{"kind": "artifact_min_lines", "path": "README.md", "lines": 10}
{"kind": "artifact_count", "count": 4}
{"kind": "task_status", "task": "T-001", "status": "succeeded"}
{"kind": "event_count", "event": "gate_result", "min": 4}
{"kind": "artifact_any_matches", "pattern": "def\\s+\\w+", "path_pattern": "\\.py$"}
{"kind": "event_field", "event": "tool_registry_ready", "field": "tools",
 "contains": "run_tests"}
{"kind": "event_field", "event": "g1_exec_check", "field": "status",
 "equals": "passed"}
{"kind": "run_status", "status": "completed"}
{"kind": "no_failed_tasks"}
```

未知 ``kind`` 直接算失败并把可用列表写进 detail：数据集写错时必须**立刻可见**，
不能静默跳过（否则「全绿」是假的）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from agentcrew.evals.case import CaseOutput, CheckResult

__all__ = ["CHECK_KINDS", "evaluate_checks"]

#: 全部支持的断言类型（CLI 报错与文档都引用它）。
CHECK_KINDS: tuple[str, ...] = (
    "artifact_exists",
    "artifact_contains",
    "artifact_not_contains",
    "artifact_matches",
    "artifact_min_lines",
    "artifact_max_lines",
    "artifact_count",
    "artifact_any_contains",
    "artifact_any_matches",
    "task_status",
    "event_count",
    "event_field",
    "run_status",
    "no_failed_tasks",
)


def evaluate_checks(
    checks: Sequence[Mapping[str, Any]], output: CaseOutput,
) -> list[CheckResult]:
    """逐条求值；**不短路**——一次跑完就能看到全部问题。"""
    return [_evaluate(check, output) for check in checks]


def _evaluate(check: Mapping[str, Any], output: CaseOutput) -> CheckResult:
    kind = str(check.get("kind") or "").strip()
    if output.error:
        # 运行本身失败时，断言一律失败：把运行错误带进 detail，避免
        # 「产物为空 + 断言 artifact_not_contains 恰好通过」这种假绿。
        return CheckResult(
            kind=kind, ok=False, detail=f"运行失败：{output.error[:300]}")
    handler = _HANDLERS.get(kind)
    if handler is None:
        return CheckResult(
            kind=kind, ok=False,
            detail=f"未知 check kind；可用：{', '.join(CHECK_KINDS)}")
    try:
        return handler(check, output)
    except (KeyError, TypeError, ValueError) as exc:
        return CheckResult(kind=kind, ok=False, detail=f"check 参数不合法：{exc}")


def _artifact_any(check: Mapping[str, Any], output: CaseOutput) -> tuple[bool, str]:
    """在**任意**产物里找文本/正则（真实模型 case 用：文件路径由模型自己定）。

    ``path_pattern`` 可先把范围收窄到匹配的产物（如只看 ``.py`` 后缀）。返回
    ``(是否命中, 说明)``；没有产物时直接判失败，不做「空集恰好通过」。
    """
    if not output.artifacts:
        return False, "本次 run 没有任何文本产物"
    path_re = check.get("path_pattern")
    compiled_path = re.compile(str(path_re)) if path_re else None
    candidates = [
        (path, text) for path, text in output.artifacts.items()
        if compiled_path is None or compiled_path.search(path)
    ]
    if not candidates:
        return False, f"没有路径匹配 /{path_re}/ 的产物（实际：{', '.join(sorted(output.artifacts))}）"
    if "pattern" in check:
        regex = re.compile(str(check["pattern"]))
        for _path, text in candidates:
            if regex.search(text):
                return True, ""
        return False, f"没有任何产物匹配 /{check['pattern']}/"
    needle = str(check.get("text") or "")
    for _path, text in candidates:
        if needle in text:
            return True, ""
    return False, f"没有任何产物包含 {needle!r}"


def _artifact_any_contains(
    check: Mapping[str, Any], output: CaseOutput
) -> CheckResult:
    ok, detail = _artifact_any(check, output)
    return CheckResult(kind="artifact_any_contains", ok=ok, detail=detail)


def _artifact_any_matches(
    check: Mapping[str, Any], output: CaseOutput
) -> CheckResult:
    ok, detail = _artifact_any(check, output)
    return CheckResult(kind="artifact_any_matches", ok=ok, detail=detail)


def _event_field(check: Mapping[str, Any], output: CaseOutput) -> CheckResult:
    """断言某个事件的某个载荷字段（C11）。

    ``contains``：字段是列表时判成员、是字符串时判子串；``equals``：严格相等。
    两者都不给时只判字段存在且为真值——那种写法容易写出「假绿」，文档里不推荐。
    同名事件有多条时取**最后一条**（run 收尾时的结论通常比中途的更有意义）。
    """
    name = str(check["event"])
    field = str(check["field"])
    matched = [item for item in output.events if item.get("event") == name]
    if not matched:
        return CheckResult(
            kind="event_field", ok=False,
            detail=f"没有事件 {name!r}（出现过的："
                   f"{', '.join(sorted({str(e.get('event')) for e in output.events})) or '无'}）")
    data = matched[-1].get("data") or {}
    if field not in data:
        return CheckResult(
            kind="event_field", ok=False,
            detail=f"事件 {name!r} 没有字段 {field!r}（实际字段："
                   f"{', '.join(sorted(map(str, data))) or '无'}）")
    value = data[field]
    if "equals" in check:
        ok = value == check["equals"]
        return CheckResult(
            kind="event_field", ok=ok,
            detail="" if ok else f"{name}.{field} = {value!r}，期望 {check['equals']!r}")
    needle = check.get("contains")
    if needle is None:
        ok = bool(value)
        return CheckResult(
            kind="event_field", ok=ok,
            detail="" if ok else f"{name}.{field} 为空")
    if isinstance(value, list):
        ok = needle in value
    else:
        ok = str(needle) in str(value)
    return CheckResult(
        kind="event_field", ok=ok,
        detail="" if ok else f"{name}.{field} = {value!r} 不含 {needle!r}")


# ── 各断言的实现（都很短，刻意保持「一眼看完」）──────────────────────────


def _artifact_text(check: Mapping[str, Any], output: CaseOutput) -> str:
    path = str(check["path"])
    if path not in output.artifacts:
        raise KeyError(f"没有产物 {path!r}（实际产物：{', '.join(sorted(output.artifacts)) or '无'}）")
    return output.artifacts[path]


def _artifact_exists(check: Mapping[str, Any], output: CaseOutput) -> CheckResult:
    path = str(check["path"])
    ok = path in output.artifacts
    return CheckResult(
        kind="artifact_exists", ok=ok,
        detail="" if ok else f"缺少产物 {path!r}（实际：{', '.join(sorted(output.artifacts)) or '无'}）")


def _artifact_contains(check: Mapping[str, Any], output: CaseOutput) -> CheckResult:
    text = str(check["text"])
    body = _artifact_text(check, output)
    ok = text in body
    return CheckResult(
        kind="artifact_contains", ok=ok,
        detail="" if ok else f"{check['path']} 里没有 {text!r}")


def _artifact_not_contains(
    check: Mapping[str, Any], output: CaseOutput
) -> CheckResult:
    text = str(check["text"])
    body = _artifact_text(check, output)
    ok = text not in body
    return CheckResult(
        kind="artifact_not_contains", ok=ok,
        detail="" if ok else f"{check['path']} 里不该出现 {text!r}")


def _artifact_matches(check: Mapping[str, Any], output: CaseOutput) -> CheckResult:
    pattern = str(check["pattern"])
    body = _artifact_text(check, output)
    ok = re.search(pattern, body) is not None
    return CheckResult(
        kind="artifact_matches", ok=ok,
        detail="" if ok else f"{check['path']} 不匹配 /{pattern}/")


def _line_count(body: str) -> int:
    return len(body.splitlines())


def _artifact_min_lines(
    check: Mapping[str, Any], output: CaseOutput
) -> CheckResult:
    lines = int(check["lines"])
    body = _artifact_text(check, output)
    count = _line_count(body)
    return CheckResult(
        kind="artifact_min_lines", ok=count >= lines,
        detail="" if count >= lines
        else f"{check['path']} 只有 {count} 行（要求 >= {lines}）")


def _artifact_max_lines(
    check: Mapping[str, Any], output: CaseOutput
) -> CheckResult:
    lines = int(check["lines"])
    body = _artifact_text(check, output)
    count = _line_count(body)
    return CheckResult(
        kind="artifact_max_lines", ok=count <= lines,
        detail="" if count <= lines
        else f"{check['path']} 有 {count} 行（要求 <= {lines}）")


def _artifact_count(check: Mapping[str, Any], output: CaseOutput) -> CheckResult:
    expected = int(check["count"])
    actual = len(output.artifacts)
    ok = actual == expected
    return CheckResult(
        kind="artifact_count", ok=ok,
        detail="" if ok else f"产物 {actual} 个（期望 {expected}）：{', '.join(sorted(output.artifacts))}")


def _task_status(check: Mapping[str, Any], output: CaseOutput) -> CheckResult:
    task = str(check["task"])
    want = str(check["status"])
    actual = output.task_status.get(task, "")
    ok = actual == want
    return CheckResult(
        kind="task_status", ok=ok,
        detail="" if ok else f"{task} 终态是 {actual or '（缺失）'}（期望 {want}）")


def _event_count(check: Mapping[str, Any], output: CaseOutput) -> CheckResult:
    event = str(check["event"])
    actual = output.event_counts.get(event, 0)
    low = check.get("min")
    high = check.get("max")
    ok = True
    problems: list[str] = []
    if low is not None and actual < int(low):
        ok = False
        problems.append(f"< {int(low)}")
    if high is not None and actual > int(high):
        ok = False
        problems.append(f"> {int(high)}")
    if low is None and high is None:
        ok = actual > 0
        problems.append("要求 > 0")
    return CheckResult(
        kind="event_count", ok=ok,
        detail="" if ok
        else f"事件 {event} 出现 {actual} 次（{'/'.join(problems)}）")


def _run_status(check: Mapping[str, Any], output: CaseOutput) -> CheckResult:
    want = str(check["status"])
    ok = output.status == want
    return CheckResult(
        kind="run_status", ok=ok,
        detail="" if ok else f"run 终态是 {output.status or '（缺失）'}（期望 {want}）")


def _no_failed_tasks(check: Mapping[str, Any], output: CaseOutput) -> CheckResult:
    failed = sorted(
        task for task, status in output.task_status.items()
        if status in ("failed", "blocked", "cancelled")
    )
    return CheckResult(
        kind="no_failed_tasks", ok=not failed,
        detail="" if not failed else f"这些任务没成功：{', '.join(failed)}")


_HANDLERS = {
    "artifact_exists": _artifact_exists,
    "artifact_contains": _artifact_contains,
    "artifact_not_contains": _artifact_not_contains,
    "artifact_matches": _artifact_matches,
    "artifact_min_lines": _artifact_min_lines,
    "artifact_max_lines": _artifact_max_lines,
    "artifact_count": _artifact_count,
    "artifact_any_contains": _artifact_any_contains,
    "artifact_any_matches": _artifact_any_matches,
    "task_status": _task_status,
    "event_count": _event_count,
    "event_field": _event_field,
    "run_status": _run_status,
    "no_failed_tasks": _no_failed_tasks,
}
