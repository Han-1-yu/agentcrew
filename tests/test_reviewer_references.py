"""G2 评审材料：验收标准引用的**其他任务产物**必须补进「参考材料」。

背景（realrun9 实测，真实 DeepSeek）：PM 写出「README 的参数与 ``logstat/cli.py``
实现一致」这类跨任务标准，而 G2 过去只渲染本任务的交付物。评审员看不到 cli.py，
只能连续驳回，三轮后返工额度烧光 → README 任务 FAILED → T-FINAL BLOCKED。
mock 夹具不会真的去核对文件，所以这个缺陷在离线演示里结构上不可能暴露。

本文件钉死修复后的契约：

- 标准里点到的路径若**确实已落盘**，就作为参考材料附进提示词；
- 参考材料**明确标注不是本任务交付物**，避免评审员反过来判「没交付该文件」；
- 本任务自己的交付物不进参考材料（不重复占预算）；
- 不存在的路径、越界路径静默跳过 —— 评审材料渲染绝不因标准文本而报错；
- 参考文件数量与 token 预算都有上界，装不下就明说，不静默丢内容。
"""

from __future__ import annotations

import json

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.gate.reviewer import _MAX_REFERENCE_FILES, Reviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.utils.tokens import estimate_tokens

#: 文档任务：标准跨到上游的源码文件上（就是 realrun9 里 T6 的形状）。
WRITER_CRITERIA = [
    "README 包含安装步骤与用法",
    "README 中的参数与 cli.py 实现一致",
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=ledger)

    coder = Task(id="T-3", title="CLI 实现", description="实现 argparse",
                 role="backend_dev", acceptance_criteria=[],
                 deliverables=[ArtifactSpec(path="cli.py", kind="python", description="d")])
    store.commit("cli.py", "def build_parser():\n    return None\n",
                 spec=coder.deliverables[0], task=coder)

    writer = Task(id="T-6", title="写 README", description="写使用文档",
                  role="technical_writer", acceptance_criteria=list(WRITER_CRITERIA),
                  deliverables=[ArtifactSpec(path="README.md", kind="markdown",
                                             description="d")])
    store.commit("README.md", "# logstat\n\n用法：logstat --top 10\n",
                 spec=writer.deliverables[0], task=writer)
    return dict(settings=settings, ledger=ledger, store=store, writer=writer, tmp_path=tmp_path)


def _reviewer(env, **kwargs) -> Reviewer:
    fx = env["tmp_path"] / "fx"
    fx.mkdir(exist_ok=True)
    (fx / "reviews.json").write_text(json.dumps({}), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    return Reviewer(models, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"],
                    **kwargs)


def test_referenced_path_is_appended_as_reference_material(env):
    """标准点到 cli.py → 材料里出现「参考材料」且带着 cli.py 正文。"""
    material = _reviewer(env)._render_material(env["writer"], round_no=1)
    assert "## 参考材料" in material
    assert "### cli.py" in material
    assert "def build_parser()" in material          # 正文真的进来了，不是只有标题
    assert "不是本任务的交付物" in material            # 明确免责，避免反向驳回


def test_own_deliverables_are_not_repeated_as_references(env):
    """标准里提到自己的交付物（README.md）时不该重复占预算。"""
    task = env["writer"]
    task.acceptance_criteria = ["README.md 必须包含安装步骤"]
    material = _reviewer(env)._render_material(task, round_no=1)
    assert "## 参考材料" not in material
    assert material.count("### README.md") == 1


def test_missing_or_unsafe_paths_are_skipped_silently(env):
    """不存在的路径、越界路径都不能让渲染抛错，也不该留空标题。"""
    task = env["writer"]
    task.acceptance_criteria = ["与 not/produced.py 保持一致", "与 ../escape.py 保持一致"]
    material = _reviewer(env)._render_material(task, round_no=1)
    assert "## 参考材料" not in material
    # 标准原文里当然还会出现这两个路径；要断言的是它们没被当成产物节选渲染进来
    assert "### not/produced.py" not in material
    assert "### ../escape.py" not in material


def test_reference_count_is_capped(env):
    """一屏标准塞满路径时最多补 _MAX_REFERENCE_FILES 个，避免吃光预算。"""
    coder = Task(id="T-9", title="多产物", description="", role="backend_dev",
                 acceptance_criteria=[], deliverables=[ArtifactSpec(path=f"m{i}.py", kind="python", description="d")
                               for i in range(_MAX_REFERENCE_FILES + 2)])
    for spec in coder.deliverables:
        env["store"].commit(spec.path, f"# {spec.path}\n", spec=spec, task=coder)

    task = env["writer"]
    task.acceptance_criteria = [
        f"与 {spec.path} 保持一致" for spec in coder.deliverables]
    material = _reviewer(env)._render_material(task, round_no=1)
    assert material.count("### m") == _MAX_REFERENCE_FILES


def test_reference_material_shares_budget_and_says_so(env):
    """预算被本任务产物吃满时，明说「未能附上」，而不是静默省略。"""
    material = _reviewer(env, excerpt_budget_tokens=1)._render_material(
        env["writer"], round_no=1)
    assert "## 参考材料" in material
    assert "未能附上参考文件" in material
    assert "def build_parser()" not in material


def test_reference_material_stays_within_total_budget(env):
    """本任务产物 + 参考材料的合计不超过预算（参考材料是加量，不是另开额度）。"""
    budget = 60
    reviewer = _reviewer(env, excerpt_budget_tokens=budget)
    material = reviewer._render_material(env["writer"], round_no=1)
    body = material.split("## 产物内容", 1)[1]
    assert estimate_tokens(body) <= budget + 200   # 标题与说明行不计入节选预算


def test_system_prompt_tells_reviewer_to_open_reference_material():
    """提示词必须写明：标准要求比对参考材料时不得以「未提供」为由判不满足。

    这是 realrun9 驳回原话的直接反面 —— 评审员当时正是以「本次材料未提供
    logstat/cli.py」为由连续三轮判不满足。
    """
    from agentcrew.gate.reviewer import _system_prompt

    prompt = _system_prompt(next(r for r in BUILTIN_ROLES if r.key == "architect"))
    assert "参考材料" in prompt
    assert "未提供" in prompt
