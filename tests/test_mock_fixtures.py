"""fixture 自洽性：防止 fixtures 与 plan 契约漂移（spec §18）。

与 brief 的两处必要校正（其余断言逐字保留）：

1. ``reviews.json`` 按 task_id 提供逐条 judgement —— 新的 ``ReviewVerdict`` 要求
   判定 ``acceptance_criteria`` 的每一条（序号恰好 0..N-1），judgement 条数按
   任务而定，兜底键 ``_default`` 不再可用。条目缺失或条数对不上，G2 都会静默
   走「评审器格式错误，已放行」通道。
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
    """每个计划任务与引擎追加的 T-FINAL 都必须在 ``reviews.json`` 里有条目。"""
    reviews = json.loads((FIX / "reviews.json").read_text(encoding="utf-8"))
    for t in PLAN["tasks"]:
        assert t["id"] in reviews
    assert "T-FINAL" in reviews, "缺少 T-FINAL（引擎追加任务）的评审夹具"


def test_review_entries_are_schema_valid():
    """``reviews.json`` 的**每一条**都必须按对应任务的验收标准完整覆盖。

    漂移的后果不是报错而是**静默放行**：回放内容进不了 ``ReviewVerdict``（或
    judgement 条数与任务的标准条数对不上），``structured`` 阶梯判它格式错误，
    ``Reviewer._opinion`` 走「评审器格式错误，已放行」通道，每个任务照样通过、
    整套测试照样绿。所以这里既验 schema 形状，也验覆盖率与「不存在兜底键」
    （条数按任务而定，``_default`` 无法再兜住任何任务）。
    """
    from agentcrew.gate.reviewer import ReviewVerdict, _is_malformed
    from agentcrew.task.planner import PlanOutput, to_tasks, with_final_task

    reviews = json.loads((FIX / "reviews.json").read_text(encoding="utf-8"))
    assert reviews, "reviews.json 不能为空"
    tasks = with_final_task(to_tasks(PlanOutput.model_validate(PLAN)))
    assert set(reviews) == {task.id for task in tasks}, (
        "reviews.json 必须恰好覆盖全部任务（含 T-FINAL）："
        "条数按任务而定，既不能留兜底键，也不能有陈旧条目")
    for task in tasks:
        verdict = ReviewVerdict.model_validate(reviews[task.id])
        assert len(verdict.judgements) == len(task.acceptance_criteria), (
            f"{task.id} 的 judgement 条数必须等于其验收标准条数")
        assert not _is_malformed(verdict, task), (
            f"{task.id} 的评审条目会被判格式错误而静默放行")
        # 演示夹具是一次干净的 run：全部判定都必须满足（G2 不得驳回）
        assert all(j.satisfied for j in verdict.judgements), (
            f"{task.id} 的演示评审出现不满足判定：一次干净的 run 不该有 G2 驳回")


def test_plan_has_no_duplicate_artifact_paths():
    seen = {}
    for tid, d in all_planned_artifacts():
        assert d["path"] not in seen, f"{d['path']} 被 {seen.get(d['path'])} 与 {tid} 重复声明"
        seen[d["path"]] = tid


def test_plan_is_acyclic_and_roles_known():
    from agentcrew.roles.builtin import BUILTIN_ROLES
    from agentcrew.roles.catalog import RoleCatalog
    from agentcrew.task.planner import PlanOutput, to_tasks, validate_plan
    validate_plan(to_tasks(PlanOutput.model_validate(PLAN)), RoleCatalog(BUILTIN_ROLES))


def test_final_task_fixture_exists():
    assert attempts_for("T-FINAL"), "缺少 T-FINAL 的 DELIVERY.md fixture"
