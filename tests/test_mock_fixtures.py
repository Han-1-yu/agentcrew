"""fixture 自洽性：防止 fixtures 与 plan 契约漂移（spec §18）。

与 brief 的两处必要校正（其余断言逐字保留）：

1. ``reviews.json`` 的兜底键是 ``"_default"`` —— ``MockProvider._review_text``
   按 ``data[task_id]`` → ``data["_default"]`` 查找；写成 ``"*"`` 会查不到、落到
   占位回复，G2 静默走「评审器格式错误，已放行」通道。
2. ``parse_blocks`` 采用与 ``agentcrew.artifacts.parser`` 相同的 **CommonMark
   围栏语义**（闭栏不短于开栏）。README.md / docs/usage.md 这类内嵌代码示例的
   产物必须以 4 个反引号开栏，否则内层的 3 反引号会在运行时提前截断产物 ——
   只认 3 反引号的简易正则既看不见这些块，也就漏掉了对它们的全部校验。
"""

import ast
import json
import re
from pathlib import Path

import pytest
import yaml

FIX = Path(__file__).resolve().parents[1] / "examples" / "fixtures"
PLAN = json.loads((FIX / "plan_todo.json").read_text(encoding="utf-8"))

#: 与 ``artifacts/parser.py`` 的 ``_START_FENCE_RE`` 一致：顶格、≥3 个反引号、
#: 可选语言名、可选 ``file=``（到行尾）。
_START_FENCE_RE = re.compile(
    r"^(?P<fence>`{3,})(?P<lang>[A-Za-z0-9_+.\-]*)(?:\s+file=(?P<path>.*?))?\s*$"
)


def parse_blocks(text: str) -> dict[str, str]:
    """提取 ``file=`` 围栏块（闭栏必须只有反引号且不短于开栏）。"""
    out: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    fence_len = 0
    for line in text.splitlines():
        match = _START_FENCE_RE.match(line)
        path = (match.group("path") or "").strip().replace("\\", "/") if match else ""
        if match is not None and path and cur is None:
            cur, buf, fence_len = path, [], len(match.group("fence"))
            continue
        stripped = line.strip()
        if cur is not None and stripped and stripped.strip("`") == "" and len(stripped) >= fence_len:
            out[cur] = "".join(part + "\n" for part in buf)
            cur, buf, fence_len = None, [], 0
            continue
        if cur is not None:
            buf.append(line)
    assert cur is None, "存在未闭合的围栏块"
    return out


def all_planned_artifacts():
    for t in PLAN["tasks"]:
        for d in t["deliverables"]:
            yield t["id"], d


def attempts_for(task_id: str) -> list[Path]:
    d = FIX / "responses" / task_id
    return sorted(d.glob("attempt-*.md")) if d.is_dir() else []


def last_attempt_blocks(task_id: str) -> dict[str, str]:
    files = attempts_for(task_id)
    assert files, f"{task_id} 没有任何 attempt fixture"
    return parse_blocks(files[-1].read_text(encoding="utf-8"))


def test_plan_file_parses_against_schema():
    from agentcrew.task.planner import PlanOutput
    PlanOutput.model_validate(PLAN)


def test_every_task_has_responses():
    for t in PLAN["tasks"]:
        assert attempts_for(t["id"]), f"{t['id']} 缺少 responses fixture"


def test_every_required_artifact_is_producible():
    for tid, d in all_planned_artifacts():
        if d.get("required", True):
            assert d["path"] in last_attempt_blocks(tid), f"{tid} 未产出 {d['path']}"


def test_no_extra_artifacts_beyond_contract():
    for t in PLAN["tasks"]:
        declared = {d["path"] for d in t["deliverables"]}
        produced = set(last_attempt_blocks(t["id"]))
        assert produced <= declared, f"{t['id']} 产出了契约外文件：{produced - declared}"


def test_must_contain_symbols_are_present():
    for tid, d in all_planned_artifacts():
        content = last_attempt_blocks(tid).get(d["path"], "")
        for needle in d.get("must_contain", []):
            assert needle in content, f"{tid}/{d['path']} 缺少 {needle!r}"


def test_python_artifacts_parse():
    for tid, d in all_planned_artifacts():
        if d["kind"] == "python":
            ast.parse(last_attempt_blocks(tid)[d["path"]])


def test_json_artifacts_parse():
    for tid, d in all_planned_artifacts():
        if d["kind"] == "json":
            json.loads(last_attempt_blocks(tid)[d["path"]])


def test_yaml_artifacts_parse():
    for tid, d in all_planned_artifacts():
        if d["kind"] == "yaml":
            yaml.safe_load(last_attempt_blocks(tid)[d["path"]])


def test_no_placeholder_markers_anywhere():
    from agentcrew.artifacts.contract import has_placeholder
    for tid, d in all_planned_artifacts():
        content = last_attempt_blocks(tid)[d["path"]]
        assert not has_placeholder(content), f"{tid}/{d['path']} 含占位内容"


def test_null_byte_and_encoding_clean():
    for tid, d in all_planned_artifacts():
        content = last_attempt_blocks(tid)[d["path"]]
        assert "\x00" not in content and "�" not in content


# ── 演示 G1 驳回 → 返工 → 通过的闭环 ──
def test_at_least_one_task_has_rework_loop():
    loops = [t["id"] for t in PLAN["tasks"] if len(attempts_for(t["id"])) >= 2]
    assert loops, "至少需要一个任务演示返工闭环"


def test_rework_loop_first_attempt_fails_second_passes():
    from agentcrew.artifacts.contract import has_placeholder
    for t in PLAN["tasks"]:
        files = attempts_for(t["id"])
        if len(files) < 2:
            continue
        first = parse_blocks(files[0].read_text(encoding="utf-8"))
        last = parse_blocks(files[-1].read_text(encoding="utf-8"))
        broken = any(has_placeholder(c) for c in first.values()) or (
            set(first) != set(last))
        assert broken, f"{t['id']} 的 attempt-1 应当不合格"
        assert not any(has_placeholder(c) for c in last.values())
        return
    pytest.fail("没有找到返工样例")


def test_reviews_fixture_covers_all_tasks_and_final():
    reviews = json.loads((FIX / "reviews.json").read_text(encoding="utf-8"))
    for t in PLAN["tasks"]:
        assert t["id"] in reviews or "_default" in reviews


def test_review_entries_are_schema_valid():
    """``reviews.json`` 的**每一条**（含兜底键 ``_default``）都必须满足 ``ReviewVerdict``。

    过去这条守卫跳过 ``_`` 前缀的键，而演示夹具里 ``reviews.json`` **只有**
    ``_default`` —— 于是唯一的形状校验在空转。夹具漂移出 schema 的后果不是报错
    而是**静默放行**：回放内容进不了 ``ReviewVerdict``，``structured`` 阶梯判它
    格式错误，``Reviewer._opinion`` 走「评审器返回格式错误，已放行」通道，
    每个任务照样通过、整套测试照样绿。所以这里既验形状，也验「兜底裁决真的是
    一份可用的裁决」（格式错误的 reject 与 JSON 解析失败走的是同一条放行通道）。
    """
    from agentcrew.gate.reviewer import ReviewVerdict, _is_malformed
    reviews = json.loads((FIX / "reviews.json").read_text(encoding="utf-8"))
    assert reviews, "reviews.json 不能为空"
    default = reviews.get("_default")
    assert default is not None, "reviews.json 必须提供 _default 兜底裁决"
    for key, verdict in reviews.items():
        ReviewVerdict.model_validate(verdict)     # 含 _default：漂移即失败
    assert not _is_malformed(ReviewVerdict.model_validate(default)), (
        "兜底裁决是「reject 却不带 issues」——评审器会把它当格式错误而放行")
    assert default["verdict"] == "approve", (
        "演示夹具的兜底裁决必须是 approve：一次干净的 run 不该有 G2 驳回")


def test_plan_has_no_duplicate_artifact_paths():
    seen = {}
    for tid, d in all_planned_artifacts():
        assert d["path"] not in seen, f"{d['path']} 被 {seen.get(d['path'])} 与 {tid} 重复声明"
        seen[d["path"]] = tid


def test_plan_is_acyclic_and_roles_known():
    from agentcrew.task.planner import PlanOutput, to_tasks, validate_plan
    from agentcrew.roles.catalog import RoleCatalog
    from agentcrew.roles.builtin import BUILTIN_ROLES
    validate_plan(to_tasks(PlanOutput.model_validate(PLAN)), RoleCatalog(BUILTIN_ROLES))


def test_final_task_fixture_exists():
    assert attempts_for("T-FINAL"), "缺少 T-FINAL 的 DELIVERY.md fixture"
