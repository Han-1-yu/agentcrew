"""G2 LLM 评审：格式错误重试、多评审人聚合与受治理调用（spec §10.2/§7.3/§13.2）。

brief 用例原样保留；在其之上补五件事：

- **重试次数可数**：``format_retry_attempts=1`` 表示「首次 + 1 次重试 = 2 次
  结构化调用」，默认 2 表示 3 次 —— ledger 的 ``llm_call`` 条数把它钉死；
- **聚合语义**：任一 ``reject`` 即整体 ``reject``，``issues``/``fix_hints``
  取并集（去重保序）、``score`` 取最小值 —— mock 的 review 夹具按 task_id
  查找、区分不了评审人，因此这两条用按 role 返回裁决的工厂替身；
- **受治理入口**：``purpose="review"`` 与评审角色 key 必须随 ``llm_call``
  落到 ledger（spec §7.3 的限额与 §14.3 的报告都靠它），且工厂计数递增；
- **无评审人**：零 token 放行，且 ``gate_result`` 同样留痕；
- **评审材料**：任务描述、验收标准、产物内容（经 ``store.read``、按
  ``excerpt_budget_tokens`` 截断）与返工历史都进提示词。
"""

import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.gate.reviewer import Reviewer, ReviewVerdict
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import ReviewRecord, Task
from agentcrew.utils.tokens import estimate_tokens

APPROVE = {"verdict": "approve", "score": 0.9, "issues": [], "fix_hints": []}
REJECT = {"verdict": "reject", "score": 0.3,
          "issues": ["缺少输入校验"], "fix_hints": ["给 title 加非空校验"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=lg)
    task = Task(id="T-2", title="后端 API", description="实现 CRUD", role="backend_dev",
                acceptance_criteria=["支持增删改查", "输入有校验"],
                deliverables=[ArtifactSpec(path="a.py", kind="python", description="d")])
    store.commit("a.py", "def f():\n    return 1\n", spec=task.deliverables[0], task=task)
    return dict(settings=s, ledger=lg, store=store, task=task, tmp_path=tmp_path)


def reviewer(env, reviews: dict, tmp_path):
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    (fx / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    return Reviewer(models, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"])


async def test_approve_produces_record(env, tmp_path):
    r = await reviewer(env, {"T-2": APPROVE}, tmp_path).review(env["task"], round_no=1)
    assert r.verdict == "approve" and r.layer == "llm"
    assert r.score == 0.9 and r.round == 1
    assert r.reviewer == "architect"           # backend_dev 的评审人


async def test_reject_carries_issues_and_hints(env, tmp_path):
    r = await reviewer(env, {"T-2": REJECT}, tmp_path).review(env["task"], round_no=1)
    assert r.verdict == "reject"
    assert r.issues == ["缺少输入校验"]
    assert r.fix_hints == ["给 title 加非空校验"]


async def test_reject_without_issues_is_treated_as_format_error(env, tmp_path):
    """spec §10.2：reject 必须带 issues，否则重试评审。"""
    bad = {"verdict": "reject", "score": 0.1, "issues": [], "fix_hints": []}
    rev = reviewer(env, {"T-2": bad}, tmp_path)
    r = await rev.review(env["task"], round_no=1)
    assert r.verdict == "approve"              # 格式错误重试耗尽后放行
    assert any("格式" in i or "warning" in i.lower() for i in r.issues) or r.score is not None


async def test_format_error_does_not_consume_rework_quota(env, tmp_path):
    bad = {"verdict": "reject", "score": 0.1, "issues": [], "fix_hints": []}
    rev = reviewer(env, {"T-2": bad}, tmp_path)
    before = env["task"].rework_count
    await rev.review(env["task"], round_no=1)
    assert env["task"].rework_count == before


async def test_reviewer_role_recorded(env, tmp_path):
    r = await reviewer(env, {"T-2": APPROVE}, tmp_path).review(env["task"], round_no=1)
    assert r.reviewer in {x.key for x in BUILTIN_ROLES}


async def test_pm_task_reviewed_by_two_roles(env, tmp_path):
    env["task"].role = "project_manager"
    r = await reviewer(env, {"T-2": APPROVE}, tmp_path).review(env["task"], round_no=1)
    assert "+" in r.reviewer or r.reviewer in ("architect", "product_manager")


async def test_review_emits_ledger_event(env, tmp_path):
    await reviewer(env, {"T-2": APPROVE}, tmp_path).review(env["task"], round_no=1)
    assert env["ledger"].count("gate_result") >= 1


async def test_missing_fixture_falls_back_not_crash(env, tmp_path):
    r = await reviewer(env, {}, tmp_path).review(env["task"], round_no=1)
    assert r.verdict in ("approve", "reject")


def test_review_verdict_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ReviewVerdict(verdict="approve", score=1.0, issues=[], fix_hints=[], bonus=1)


# ── 以下是 brief 之外的补充用例 ──────────────────────────────────────────

class _FakeModels:
    """``ModelFactory`` 替身：按 role 返回预设裁决并记录调用参数。

    mock 的 review 夹具只按 task_id 查找，两个评审人拿到同一份裁决 —— 要钉死
    聚合语义只能替换工厂。``get()`` 返回非 ``MockProvider``，Reviewer 因此不会
    去 ``set_purpose``。
    """

    def __init__(self, by_role: dict[str, dict], settings: Settings) -> None:
        self._by_role = by_role
        self.settings = settings
        self.calls: list[dict] = []

    def get(self, alias: str) -> object:
        return object()

    async def complete_structured(self, alias, messages, schema, *,
                                  temperature, max_tokens, purpose, role):
        self.calls.append(dict(
            alias=alias, messages=list(messages), schema=schema,
            purpose=purpose, role=role,
            temperature=temperature, max_tokens=max_tokens))
        return ReviewVerdict(**self._by_role[role])


def _reviewer(env, reviews: dict, tmp_path, **kwargs) -> Reviewer:
    """与 brief 的 ``reviewer`` 同形，但可传构造参数、fixtures 换一个目录。"""
    fx = tmp_path / "fx2"
    fx.mkdir(exist_ok=True)
    (fx / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    return Reviewer(models, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"],
                    **kwargs)


async def test_malformed_reject_appends_verbatim_issue(env, tmp_path):
    bad = {"verdict": "reject", "score": 0.1, "issues": [], "fix_hints": []}
    r = await _reviewer(env, {"T-2": bad}, tmp_path).review(env["task"], round_no=1)
    assert r.verdict == "approve"
    assert r.issues == ["评审器返回格式错误，已放行"]
    assert r.fix_hints == []
    assert r.score is None                     # 评审器失败，没有可信的质量分


async def test_malformed_reject_retries_configured_times(env, tmp_path):
    bad = {"verdict": "reject", "score": 0.1, "issues": [], "fix_hints": []}
    rev = _reviewer(env, {"T-2": bad}, tmp_path, format_retry_attempts=1)
    await rev.review(env["task"], round_no=1)
    # 首次 + 1 次重试 = 2 次结构化调用，每次一条 llm_call（mock 回放）
    assert env["ledger"].count("llm_call") == 2


async def test_malformed_reject_default_retries(env, tmp_path):
    bad = {"verdict": "reject", "score": 0.1, "issues": [], "fix_hints": []}
    await _reviewer(env, {"T-2": bad}, tmp_path).review(env["task"], round_no=1)
    # 默认 format_retry_attempts=2：首次 + 2 次重试 = 3 次结构化调用
    assert env["ledger"].count("llm_call") == 3


async def test_missing_fixture_releases_with_format_issue(env, tmp_path):
    """夹具缺失 = 结构化输出彻底失败：按格式错误放行，理由进 issues。"""
    r = await reviewer(env, {}, tmp_path).review(env["task"], round_no=1)
    assert r.verdict == "approve"
    assert r.issues == ["评审器返回格式错误，已放行"]


async def test_review_never_bumps_rework_count(env, tmp_path):
    """返工计数归 Task 27 的 Gatekeeper；评审层碰都不碰。"""
    before = env["task"].rework_count
    r = await reviewer(env, {"T-2": REJECT}, tmp_path).review(env["task"], round_no=1)
    assert r.verdict == "reject"
    assert env["task"].rework_count == before == 0


async def test_no_reviewer_approves_without_llm_call(env, tmp_path):
    """没有任何评审人（含回落 project_manager 也不存在）：零 token 放行。"""
    task = env["task"].model_copy(update={"role": "lone_wolf"})
    catalog = RoleCatalog([RoleSpec(
        key="lone_wolf", title="独行侠", goal="g", backstory="b",
        responsibilities=["r"], boundaries=["b"], output_contract="o",
        system_prompt="s")])
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    r = await Reviewer(models, catalog, env["store"], env["ledger"]).review(
        task, round_no=1)
    assert r.verdict == "approve" and r.reviewer == "none"
    assert r.issues == ["无可用评审人，跳过 LLM 评审"]
    assert models.llm_calls == 0
    events = list(env["ledger"].events("gate_result"))
    assert len(events) == 1 and events[0].data["verdict"] == "approve"


async def test_dual_reviewer_reject_unions_issues_and_takes_min_score(env, tmp_path):
    env["task"].role = "project_manager"
    fake = _FakeModels({
        "architect": {"verdict": "reject", "score": 0.6,
                      "issues": ["接口缺少错误语义", "命名不一致"],
                      "fix_hints": ["补充错误码表"]},
        "product_manager": {"verdict": "reject", "score": 0.2,
                            "issues": ["命名不一致", "验收标准覆盖不足"],
                            "fix_hints": ["统一命名规范", "补充错误码表"]},
    }, env["settings"])
    r = await Reviewer(fake, RoleCatalog(BUILTIN_ROLES), env["store"],
                       env["ledger"]).review(env["task"], round_no=1)
    assert r.verdict == "reject"
    assert r.score == 0.2                                    # 最小值
    assert r.issues == ["接口缺少错误语义", "命名不一致", "验收标准覆盖不足"]   # 并集去重保序
    assert r.fix_hints == ["补充错误码表", "统一命名规范"]
    assert r.reviewer == "architect+product_manager"
    assert [c["role"] for c in fake.calls] == ["architect", "product_manager"]


async def test_one_reject_among_two_approvals_rejects(env, tmp_path):
    env["task"].role = "project_manager"
    fake = _FakeModels({
        "architect": {"verdict": "approve", "score": 0.9, "issues": [], "fix_hints": []},
        "product_manager": {"verdict": "reject", "score": 0.3,
                            "issues": ["缺少输入校验"], "fix_hints": ["给 title 加非空校验"]},
    }, env["settings"])
    r = await Reviewer(fake, RoleCatalog(BUILTIN_ROLES), env["store"],
                       env["ledger"]).review(env["task"], round_no=1)
    assert r.verdict == "reject"                 # 一票否决
    assert r.score == 0.3
    assert r.issues == ["缺少输入校验"]


async def test_all_approve_aggregates_min_score(env, tmp_path):
    env["task"].role = "project_manager"
    fake = _FakeModels({
        "architect": {"verdict": "approve", "score": 0.9, "issues": [],
                      "fix_hints": []},
        "product_manager": {"verdict": "approve", "score": 0.7, "issues": [],
                            "fix_hints": []},
    }, env["settings"])
    r = await Reviewer(fake, RoleCatalog(BUILTIN_ROLES), env["store"],
                       env["ledger"]).review(env["task"], round_no=1)
    assert r.verdict == "approve"
    assert r.score == 0.7
    assert r.issues == [] and r.fix_hints == []


async def test_llm_calls_are_governed_and_attributed(env, tmp_path):
    """spec §7.3/§14.3：评审调用走工厂受治理入口，purpose/role 落 ledger。"""
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    (fx / "reviews.json").write_text(json.dumps({"T-2": APPROVE}), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    rev = Reviewer(models, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"])
    await rev.review(env["task"], round_no=1)
    assert models.llm_calls == 1                 # 工厂记账（未绕过限流/审计）
    assert models.total_prompt_tokens > 0
    calls = list(env["ledger"].events("llm_call"))
    assert len(calls) == 1
    assert calls[0].data["purpose"] == "review"
    assert calls[0].data["role"] == "architect"


async def test_gate_result_carries_llm_verdict(env, tmp_path):
    r = await reviewer(env, {"T-2": REJECT}, tmp_path).review(env["task"], round_no=3)
    assert r.task_id == "T-2" and r.round == 3
    events = list(env["ledger"].events("gate_result"))
    assert len(events) == 1
    data = events[0].data
    assert data["task_id"] == "T-2" and data["round"] == 3
    assert data["layer"] == "llm"
    assert data["verdict"] == "reject"
    assert data["score"] == 0.3
    assert data["issues"] == ["缺少输入校验"]
    assert data["reviewer"] == "architect"


async def test_prompt_carries_criteria_and_artifact_content(env, tmp_path):
    fake = _FakeModels({"architect": APPROVE}, env["settings"])
    await Reviewer(fake, RoleCatalog(BUILTIN_ROLES), env["store"],
                   env["ledger"]).review(env["task"], round_no=1)
    call = fake.calls[0]
    system, user = call["messages"][0], call["messages"][1]
    assert system["role"] == "system" and user["role"] == "user"
    assert "只输出 JSON" in system["content"]
    assert "验收标准" in system["content"]        # 逐条核对验收标准
    assert call["schema"] is ReviewVerdict
    assert call["purpose"] == "review"
    for text in ("实现 CRUD", "支持增删改查", "输入有校验", "return 1"):
        assert text in user["content"]


async def test_prompt_carries_rework_history(env, tmp_path):
    env["task"].rework_count = 1
    env["task"].reviews.append(ReviewRecord(
        task_id="T-2", round=1, layer="static", verdict="reject",
        issues=["缺少必需产物：a.py"], fix_hints=["补齐产物 a.py"],
        reviewer="static:contract", created_at=datetime.now()))
    fake = _FakeModels({"architect": APPROVE}, env["settings"])
    await Reviewer(fake, RoleCatalog(BUILTIN_ROLES), env["store"],
                   env["ledger"]).review(env["task"], round_no=2)
    user = fake.calls[0]["messages"][1]["content"]
    assert "已返工次数：1" in user
    assert "缺少必需产物：a.py" in user


async def test_artifact_excerpt_respects_token_budget(env, tmp_path):
    long = "".join(f"第{i}行内容\n" for i in range(1, 60))
    env["store"].commit("a.py", long, spec=env["task"].deliverables[0], task=env["task"])
    fake = _FakeModels({"architect": APPROVE}, env["settings"])
    budget = estimate_tokens("第1行内容\n")
    await Reviewer(fake, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"],
                   excerpt_budget_tokens=budget).review(env["task"], round_no=1)
    user = fake.calls[0]["messages"][1]["content"]
    assert "第1行内容" in user
    assert "第59行内容" not in user
    assert "截断" in user


async def test_artifacts_are_read_through_store(env, tmp_path):
    reads: list[str] = []
    original = env["store"].read

    def spy(rel, **kwargs):
        reads.append(rel)
        return original(rel, **kwargs)

    env["store"].read = spy
    fake = _FakeModels({"architect": APPROVE}, env["settings"])
    await Reviewer(fake, RoleCatalog(BUILTIN_ROLES), env["store"],
                   env["ledger"]).review(env["task"], round_no=1)
    assert reads == ["a.py"]
