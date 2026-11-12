"""R45：配置矩阵（``eval --variant``）的结构层测试。

只测"变体怎么读、怎么校验、怎么算差值"——真正跑 crew 的那部分由
``tests/test_evals.py`` 覆盖（这里不重复烧时间，也不引入夹具依赖）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.evals.case import CaseResult
from agentcrew.evals.runner import (
    EvalReport,
    MatrixReport,
    VariantResult,
    apply_overrides,
    load_variant_file,
    run_matrix,
)


def _result(case_id: str, *, ok: bool = True, calls: int = 2,
            cost: float = 0.01, revise: int = 0) -> CaseResult:
    return CaseResult(
        case_id=case_id, ok=ok, status="succeeded",
        llm_calls=calls, cost_usd=cost, revise_rounds=revise)


def _report(*results: CaseResult, repeat: int = 1) -> EvalReport:
    return EvalReport(results=list(results), repeat=repeat)


# ── 变体文件 ──────────────────────────────────────────────────────────────


def test_variant_uses_same_shape_as_main_config(tmp_path: Path):
    """变体文件与 agentcrew.yaml 同形：agents.profiles / roles 直接可用。"""
    path = tmp_path / "cheap.yaml"
    path.write_text(
        """
agents:
  profiles:
    be_fast:
      title: 便宜档
roles:
  backend_dev:
    agents:
      - ref: be_fast
        duty: primary
    aggregation:
      mode: primary_with_review
      review_rounds: 1
""",
        encoding="utf-8",
    )
    data = load_variant_file(path)
    assert set(data) == {"agent_profiles", "role_agents"}
    assert data["agent_profiles"]["be_fast"]["key"] == "be_fast", "profile key 自动回填"
    assert data["role_agents"]["backend_dev"]["aggregation"]["review_rounds"] == 1


def test_variant_file_missing_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="不存在"):
        load_variant_file(tmp_path / "nope.yaml")


# ── 覆盖与校验 ────────────────────────────────────────────────────────────


def test_apply_overrides_rejects_unknown_field():
    """未知字段必须当场拒绝：model_copy 会静默加属性，跑完才发现没生效。"""
    with pytest.raises(ValueError, match="未知配置项"):
        apply_overrides(Settings(), {"review_rounds": 1})


def test_apply_overrides_validates_nested_models():
    """嵌套校验真的执行：review_rounds 上界 3 由 pydantic 拦住。"""
    settings = Settings()
    with pytest.raises(Exception):  # noqa: B017 —— pydantic ValidationError
        apply_overrides(settings, {"role_agents": {"backend_dev": {
            "agents": [{"ref": "a", "duty": "primary"},
                       {"ref": "b", "duty": "review"}],
            "aggregation": {"mode": "primary_with_review", "review_rounds": 9}}}})


def test_apply_overrides_keeps_other_fields():
    settings = Settings()
    merged = apply_overrides(settings, {"cost_aware_routing": True})
    assert merged.cost_aware_routing is True
    assert merged.default_model == settings.default_model, "未点名的字段沿用基线"


# ── 矩阵结果 ──────────────────────────────────────────────────────────────


def test_matrix_delta_is_relative_to_first_variant():
    base = VariantResult("base", {}, _report(_result("a", calls=10, cost=0.10)))
    cheap = VariantResult(
        "cheap", {"x": 1},
        _report(_result("a", ok=False, calls=6, cost=0.04, revise=1)))
    data = MatrixReport([base, cheap]).to_dict()

    assert data["baseline"] == "base"
    assert data["variants"][0]["delta_vs_baseline"] == {}, "基线自己不算差值"
    delta = data["variants"][1]["delta_vs_baseline"]
    assert delta["llm_calls"] == -4
    assert round(delta["cost_usd"], 6) == -0.06
    assert delta["revise_rounds"] == 1
    assert delta["pass_pow_k"] == -1.0
    assert data["variants"][1]["metrics"]["passed"] == 0


def test_matrix_metrics_sum_across_repeats():
    report = _report(_result("a"), _result("a"), _result("b", ok=False),
                     repeat=2)
    metrics = VariantResult("v", {}, report).metrics
    assert metrics["cases"] == 2
    assert metrics["runs"] == 3
    assert metrics["passed"] == 2
    assert metrics["llm_calls"] == 6


# ── 入参防御 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_matrix_rejects_empty_and_duplicate_variants():
    from agentcrew.evals.case import EvalCase

    cases = [EvalCase(id="c", sample="linear")]
    with pytest.raises(ValueError, match="至少需要一个变体"):
        await run_matrix([], cases)
    with pytest.raises(ValueError, match="重复"):
        await run_matrix([("base", {}), ("base", {})], cases)


def test_variant_with_flat_field_names_is_rejected(tmp_path: Path):
    """写扁平字段名（``agent_profiles`` / ``role_agents``）必须报错。

    这类文件会被归一化**静默丢弃**，变体等于没生效，跑出来的 A/B 结论是假的。
    变体必须与 ``agentcrew.yaml`` 同形；宽松只留给主配置。
    """
    path = tmp_path / "flat.yaml"
    path.write_text(
        "agent_profiles:/n  a:/n    title: x\nrole_agents:/n  backend_dev: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="无法识别的顶层段"):
        load_variant_file(path)


def test_variant_with_typo_section_is_rejected(tmp_path: Path):
    path = tmp_path / "typo.yaml"
    path.write_text("agentss:/n  profiles: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="agentss"):
        load_variant_file(path)
