"""G1 静态校验门禁（Task 13，spec §10.1）。

在 brief 用例之外钉死三件事：

- **一次回齐全部问题**：一个任务同时违反多条时，所有 issue 与配对的
  ``fix_hint`` 必须在**同一个** ``ReviewRecord`` 里 —— 这就是模型返工收到的
  完整清单，早退会把一次返工拖成 N 次。
- **空任务**：无交付物且无产出的任务必须 approve（不能凭空报错）。
- **可选产物只在缺失时豁免**：可选产物一旦落盘，照样过内容检查。
- **解析错误 issue 以路径开头**：``issues`` 会被 G3 报告与跨层聚合单独读取，必须自包含。

``test_oversize_file_rejected`` 按控制器裁定改为
``test_file_within_max_bytes_is_not_size_flagged``：``max_bytes`` 是
``ArtifactStore.commit`` 的写入期硬上限（超限抛 ``ArtifactTooLargeError``，
由 ``tests/test_artifact_store.py`` 钉死），超限字节根本落不了盘，G1 永远
见不到过大产物 —— 原用例的夹具无法构造。
"""

import pytest

from agentcrew.artifacts.contract import parse_check
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.gate.static_checks import STATIC_REVIEWER, run_static
from agentcrew.task.task import Task


def task_with(*specs, tid="T-1"):
    return Task(id=tid, title="t", description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=list(specs))


def py(path, **kw):
    return ArtifactSpec(path=path, kind="python", description="d", **kw)


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts", run_id="R-1")


def commit(store, task, spec, content):
    return store.commit(spec.path, content, spec=spec, task=task)


def test_all_good_passes(store):
    t = task_with(py("app/main.py", must_contain=["def create_app"]))
    commit(store, t, t.deliverables[0], "def create_app():\n    return None\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "approve" and r.layer == "static"
    assert r.reviewer == STATIC_REVIEWER and r.round == 1


def test_round_no_is_recorded(store):
    t = task_with(py("a.py"))
    commit(store, t, t.deliverables[0], "x = 1\n")
    assert run_static(t, store, round_no=3).round == 3


def test_missing_required_artifact_rejected(store):
    t = task_with(py("app/main.py"))
    # app/main.py 从未落盘：G1 必须先报缺失、且**不读取**该路径 —— 读它必抛
    # FileNotFoundError，用例本身就是那条「缺失不得读」的回归。
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and any("app/main.py" in i for i in r.issues)
    assert "补齐产物 app/main.py" in r.fix_hints


def test_optional_artifact_may_be_missing(store):
    t = task_with(py("app/main.py"),
                  py("app/extra.py", required=False))
    commit(store, t, t.deliverables[0], "x = 1\n")
    assert run_static(t, store, round_no=1).verdict == "approve"


def test_present_optional_artifact_is_still_checked(store):
    """可选只豁免「缺失」：一旦落盘，内容检查一律照跑。"""
    t = task_with(py("main.py"),
                  py("extra.py", required=False, must_contain=["def helper"]))
    commit(store, t, t.deliverables[0], "x = 1\n")
    commit(store, t, t.deliverables[1], "def other():\n    return 1\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject"
    assert "extra.py 缺少必需符号：def helper" in r.issues


def test_task_without_deliverables_approves(store):
    r = run_static(task_with(), store, round_no=1)
    assert r.verdict == "approve" and r.issues == [] and r.fix_hints == []


def test_empty_file_rejected(store):
    t = task_with(py("a.py", min_bytes=1))
    commit(store, t, t.deliverables[0], "")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and any("空" in i for i in r.issues)


def test_file_within_max_bytes_is_not_size_flagged(store):
    """G1 只查尺寸下界；``max_bytes`` 由 ``commit`` 在写入期把关（Task 11 裁定）。"""
    s = py("a.py", max_bytes=20)
    t = task_with(s)
    commit(store, t, s, "x = 1\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "approve" and r.issues == []


def test_syntax_error_rejected(store):
    t = task_with(py("a.py"))
    commit(store, t, t.deliverables[0], "def f(\n  pass\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and any("语法错误" in i for i in r.issues)


def test_parse_error_issue_leads_with_path(store):
    """解析器消息前必须带产物路径：issues 会被 G3 终端报告与跨层聚合**单独**读取，
    一个任务常声明半打产物，``语法错误：line 5`` 必须说得出是哪个文件。"""
    t = task_with(py("app/main.py"))
    commit(store, t, t.deliverables[0], "def f(\n")
    r = run_static(t, store, round_no=1)
    assert len(r.issues) == 1
    assert r.issues[0].startswith("app/main.py 语法错误：")
    # 注意：调用必须提到 f-string 之外 —— 替换字段里的反斜杠是 PEP 701 特性，
    # 3.12 才允许，写进去会让整个测试文件在 3.10/3.11 上无法解析
    detail = parse_check("python", "def f(\n")
    assert r.issues[0] == f"app/main.py {detail}"


def test_json_parse_error_issue_leads_with_path(store):
    """非 python 的解析消息同样以路径开头（其余 kind 的消息不带任何位置信息）。"""
    t = task_with(ArtifactSpec(path="docs/api.json", kind="json", description="d"))
    commit(store, t, t.deliverables[0], "{")
    r = run_static(t, store, round_no=1)
    assert len(r.issues) == 1
    assert r.issues[0].startswith("docs/api.json JSON 解析失败：")


def test_missing_must_contain_symbol_rejected(store):
    t = task_with(py("a.py", must_contain=["def create_app", "FastAPI"]))
    commit(store, t, t.deliverables[0], "def create_app():\n    return 1\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject"
    assert any("FastAPI" in i for i in r.issues)
    assert not any(i for i in r.issues if "create_app" in i and "缺少" in i)


def test_placeholder_only_file_rejected(store):
    t = task_with(py("a.py"))
    commit(store, t, t.deliverables[0], "TODO\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and any("占位" in i for i in r.issues)


# ── Review Focus #1：模型幻觉出契约外文件 ──
def test_undeclared_extra_artifact_rejected(store):
    t = task_with(py("app/main.py"))
    commit(store, t, t.deliverables[0], "x = 1\n")
    # 该任务某次执行写了契约外的文件
    store.commit("app/hack.py", "y = 1\n",
                 spec=ArtifactSpec(path="app/hack.py", kind="python", description="d"), task=t)
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject"
    assert any("app/hack.py" in i and ("未声明" in i or "契约" in i) for i in r.issues)


def test_declared_but_unproduced_required_artifact_reported(store):
    t = task_with(py("app/main.py"), py("app/models.py"))
    commit(store, t, t.deliverables[0], "x = 1\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject"
    assert any("app/models.py" in i for i in r.issues)
    assert not any("app/main.py" in i for i in r.issues)


def test_multiple_problems_all_reported(store):
    t = task_with(py("a.py", must_contain=["def f"]), py("b.py"))
    commit(store, t, t.deliverables[0], "def f(\n")
    r = run_static(t, store, round_no=1)
    assert len(r.issues) >= 2


def test_all_violations_reported_in_one_record(store):
    """同时违反 1/2/5/6 四条检查：四条 issue 必须在同一次裁决里回齐（不早退），
    且顺序即检查顺序（缺产物 → 契约外 → 占位 → 必需符号）。"""
    t = task_with(py("a.py", must_contain=["def create_app"]), py("b.py"))
    commit(store, t, t.deliverables[0], "TODO\n")
    store.commit("hack.py", "y = 1\n",
                 spec=ArtifactSpec(path="hack.py", kind="python", description="d"), task=t)
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject"
    assert r.issues == [
        "缺少必需产物：b.py",
        "产出未在契约中声明的文件：hack.py",
        "a.py 仅含占位内容",
        "a.py 缺少必需符号：def create_app",
    ]


def test_rejected_record_has_no_score(store):
    t = task_with(py("a.py"))
    assert run_static(t, store, round_no=1).score is None


def test_fix_hints_are_populated_on_reject(store):
    t = task_with(py("a.py"))
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and r.fix_hints


def test_fix_hints_pair_with_issues(store):
    """一条 issue 恰好配一条 fix_hint，且 hint 说的是「去做什么」而非复述问题。"""
    t = task_with(py("a.py", must_contain=["def f"]))
    commit(store, t, t.deliverables[0], "TODO\n")
    r = run_static(t, store, round_no=1)
    assert len(r.fix_hints) == len(r.issues) == 2
    assert all(hint != issue for issue, hint in zip(r.issues, r.fix_hints, strict=True))


def test_must_contain_matching_is_case_insensitive(store):
    """真实模型写 `Flask==3.0.3`，而 PM 定的 must_contain 是 `flask`。

    大小写敏感时这个**完全正确**的产物会连续失败多轮，且失败信息只说
    「缺少必需符号：flask」—— 文件里明明写着 Flask，从症状推不出病因。
    """
    t = task_with(ArtifactSpec(path="backend/requirements.txt", kind="text",
                               description="d", must_contain=["flask"]))
    commit(store, t, t.deliverables[0], "Flask==3.0.3\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "approve", r.issues


def test_must_contain_case_insensitive_also_matches_upper(store):
    t = task_with(ArtifactSpec(path="a.py", kind="python", description="d",
                               must_contain=["def F"]))
    commit(store, t, t.deliverables[0], "def f():\n    return 1\n")
    assert run_static(t, store, round_no=1).verdict == "approve"


def test_must_contain_still_reports_a_genuinely_absent_symbol(store):
    """宽容的是大小写，不是缺失 —— 真没有就还得报。"""
    t = task_with(ArtifactSpec(path="a.py", kind="python", description="d",
                               must_contain=["django"]))
    commit(store, t, t.deliverables[0], "def f():\n    return 1\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject"
    assert any("django" in i for i in r.issues)
