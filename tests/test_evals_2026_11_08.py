"""P2-5：evals —— 数据集、断言、``pass^k`` 与 CLI。

评测工具本身最怕「永远绿」：断言写错、数据集漏读、指标算错，都会让整套东西变成
装饰。所以这里既测正例，也**故意用错断言**证明它会红（``test_wrong_expectation_fails``、
``test_pass_pow_k_penalises_flaky_case``）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcrew.evals import (
    CaseOutput,
    CaseResult,
    DatasetError,
    EvalCase,
    compute_pass_pow_k,
    evaluate_checks,
    load_dataset,
    run_case,
    run_dataset,
)
from agentcrew.evals.checks import CHECK_KINDS

DATASET = Path(__file__).resolve().parents[1] / "examples" / "evals" / "smoke.jsonl"


# ── 1. 数据集加载 ──────────────────────────────────────────────────────


def test_loads_bundled_smoke_dataset():
    cases = load_dataset(DATASET)
    assert {case.id for case in cases} >= {
        "linear-completes", "hierarchy-parallel-join", "todo-mixed-shape"}
    # 注释行 / 空行被跳过，且每行必须是单行 JSON
    assert all(case.sample for case in cases)


def test_dataset_requires_sample_or_goal(tmp_path: Path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "x", "checks": []}\n', encoding="utf-8")
    with pytest.raises(DatasetError) as exc:
        load_dataset(path)
    assert "sample" in str(exc.value)


def test_dataset_rejects_duplicate_ids(tmp_path: Path):
    path = tmp_path / "dup.jsonl"
    path.write_text(
        '{"id": "a", "sample": "linear"}\n{"id": "a", "sample": "linear"}\n',
        encoding="utf-8")
    with pytest.raises(DatasetError) as exc:
        load_dataset(path)
    assert "重复" in str(exc.value)


def test_dataset_reports_line_number_for_broken_json(tmp_path: Path):
    path = tmp_path / "broken.jsonl"
    # 故意写成多行（JSONL 必须是单行）——这是最常见的手写错误
    path.write_text(
        '{"id": "a", "sample": "linear",\n "checks": []}\n', encoding="utf-8")
    with pytest.raises(DatasetError) as exc:
        load_dataset(path)
    assert ":1" in str(exc.value) and "JSON" in str(exc.value)


def test_dataset_rejects_empty(tmp_path: Path):
    path = tmp_path / "empty.jsonl"
    path.write_text("# 只有注释\n\n", encoding="utf-8")
    with pytest.raises(DatasetError):
        load_dataset(path)


def test_case_id_cannot_contain_path_separators():
    with pytest.raises(ValueError):
        EvalCase(id="../escape", sample="linear")


# ── 2. 断言引擎（纯单元，不跑 run）────────────────────────────────────


def _output(**kwargs) -> CaseOutput:
    base = CaseOutput(
        status="completed",
        artifacts={"README.md": "# 标题\n第一行\n第二行\n", "a.py": "def f():\n    pass\n"},
        task_status={"T-1": "succeeded", "T-2": "succeeded"},
        event_counts={"task_finished": 2, "gate_result": 4},
    )
    return base.model_copy(update=kwargs)


@pytest.mark.parametrize("check,expected", [
    ({"kind": "artifact_exists", "path": "README.md"}, True),
    ({"kind": "artifact_exists", "path": "nope.md"}, False),
    ({"kind": "artifact_contains", "path": "README.md", "text": "标题"}, True),
    ({"kind": "artifact_contains", "path": "README.md", "text": "缺失"}, False),
    ({"kind": "artifact_not_contains", "path": "README.md", "text": "缺失"}, True),
    ({"kind": "artifact_not_contains", "path": "README.md", "text": "标题"}, False),
    ({"kind": "artifact_matches", "path": "a.py", "pattern": r"def\s+f"}, True),
    ({"kind": "artifact_matches", "path": "a.py", "pattern": r"def\s+g"}, False),
    ({"kind": "artifact_min_lines", "path": "README.md", "lines": 3}, True),
    ({"kind": "artifact_min_lines", "path": "README.md", "lines": 99}, False),
    ({"kind": "artifact_max_lines", "path": "a.py", "lines": 2}, True),
    ({"kind": "artifact_max_lines", "path": "README.md", "lines": 1}, False),
    ({"kind": "artifact_count", "count": 2}, True),
    ({"kind": "artifact_count", "count": 3}, False),
    ({"kind": "task_status", "task": "T-1", "status": "succeeded"}, True),
    ({"kind": "task_status", "task": "T-1", "status": "failed"}, False),
    ({"kind": "event_count", "event": "gate_result", "min": 4}, True),
    ({"kind": "event_count", "event": "gate_result", "max": 3}, False),
    ({"kind": "event_count", "event": "从未有过"}, False),
    ({"kind": "run_status", "status": "completed"}, True),
    ({"kind": "run_status", "status": "failed"}, False),
    ({"kind": "no_failed_tasks"}, True),
])
def test_check_matrix(check: dict, expected: bool):
    result = evaluate_checks([check], _output())[0]
    assert result.ok is expected, result.detail


def test_no_failed_tasks_detects_failure():
    output = _output(task_status={"T-1": "succeeded", "T-2": "failed"})
    result = evaluate_checks([{"kind": "no_failed_tasks"}], output)[0]
    assert result.ok is False and "T-2" in result.detail


def test_unknown_kind_fails_loudly():
    result = evaluate_checks([{"kind": "vibes_check"}], _output())[0]
    assert result.ok is False
    assert "未知 check kind" in result.detail
    assert "artifact_exists" in result.detail, "错误里要列出可用类型"


def test_run_error_fails_every_check():
    """运行本身失败时不能出现「artifact_not_contains 恰好通过」的假绿。"""
    output = CaseOutput(error="Crew 崩了")
    results = evaluate_checks(
        [{"kind": "artifact_not_contains", "path": "x", "text": "y"}], output)
    assert results[0].ok is False
    assert "运行失败" in results[0].detail


def test_missing_artifact_parameter_is_reported():
    result = evaluate_checks([{"kind": "artifact_exists"}], _output())[0]
    assert result.ok is False and "参数不合法" in result.detail


def test_all_declared_kinds_have_handlers():
    """``CHECK_KINDS`` 与实现不能漂移：文档里列的类型必须真的能跑。"""
    for kind in CHECK_KINDS:
        result = evaluate_checks([{"kind": kind}], _output())[0]
        assert "未知 check kind" not in result.detail, kind


# ── 3. pass^k ──────────────────────────────────────────────────────────


def _result(case_id: str, ok: bool) -> CaseResult:
    return CaseResult(case_id=case_id, ok=ok)


def test_pass_pow_k_penalises_flaky_case():
    """k 次里只要有一次没过，这条 case 就不算 pass^k——这正是它存在的理由。"""
    results = [
        _result("a", True), _result("a", True),      # 全过
        _result("b", True), _result("b", False),     # 波动
    ]
    assert compute_pass_pow_k(results, 2) == 0.5
    # pass@1 看起来是 75%，真实稳定性只有 50%
    assert sum(1 for item in results if item.ok) / len(results) == 0.75


def test_pass_pow_k_with_single_repeat_equals_pass_at_1():
    results = [_result("a", True), _result("b", False)]
    assert compute_pass_pow_k(results, 1) == 0.5


def test_pass_pow_k_is_zero_without_results():
    assert compute_pass_pow_k([], 1) == 0.0


# ── 4. 端到端：跑真 run（mock 夹具，秒级）──────────────────────────────


async def test_runs_a_real_case_and_passes():
    case = EvalCase(id="e2e-linear", sample="linear", checks=[
        {"kind": "run_status", "status": "completed"},
        {"kind": "no_failed_tasks"},
        {"kind": "artifact_exists", "path": "README.md"},
        {"kind": "event_count", "event": "task_finished", "min": 4},
    ])
    output = await run_case(case)
    assert output.status == "completed"
    assert "README.md" in output.artifacts
    assert all(item.ok for item in evaluate_checks(case.checks, output))


async def test_wrong_expectation_fails():
    """负例：断言写错必须红。写评测最怕「怎么跑都是绿的」。"""
    case = EvalCase(id="e2e-wrong", sample="linear", checks=[
        {"kind": "artifact_exists", "path": "根本不存在的文件.md"},
    ])
    output = await run_case(case)
    ok = [item.ok for item in evaluate_checks(case.checks, output)]
    assert ok == [False]


async def test_global_fixtures_are_restored_after_each_case():
    """跑 case 会切全局夹具注入点；跑完必须还原，否则同进程后续 run 会用错夹具。"""
    from agentcrew.models import factory

    before = factory.DEFAULT_FIXTURES_DIR
    await run_case(EvalCase(id="restore", sample="todo", checks=[]))
    assert factory.DEFAULT_FIXTURES_DIR == before


async def test_bad_sample_is_recorded_not_raised():
    """样例名写错：整批不能崩，要如实记成「这条 case 运行失败」。"""
    case = EvalCase(id="e2e-bad-sample", sample="不存在的样例", checks=[
        {"kind": "run_status", "status": "completed"}])
    output = await run_case(case)
    assert output.error
    assert evaluate_checks(case.checks, output)[0].ok is False


async def test_run_dataset_aggregates(tmp_path: Path):
    cases = [
        EvalCase(id="ok", sample="linear", checks=[
            {"kind": "run_status", "status": "completed"}]),
        EvalCase(id="bad", sample="linear", checks=[
            {"kind": "artifact_exists", "path": "nope.md"}]),
    ]
    report = await run_dataset(cases, repeat=2, output_dir=tmp_path / "out")
    assert report.repeat == 2
    assert len(report.results) == 4
    assert report.pass_pow_k == 0.5        # 只有 ok 这条 k 次全过
    assert report.strict_cases == ["ok"]
    # --output-dir 给了就留账：每个 case 每次一个目录
    assert (tmp_path / "out" / "ok-1").is_dir()
    payload = report.to_dict()
    assert payload["pass_pow_k"] == 0.5
    assert json.dumps(payload)            # 可 JSON 序列化（--json 的前提）
