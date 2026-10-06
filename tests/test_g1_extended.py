"""P2-4：G1 扩展代码可执行性校验。

钉三件事：

- **error 阻断**：Python 语法错误、空文件 → 扩展检查报 error，使 G1 不通过；
- **warning 不阻断**：危险模块导入（os / subprocess …）只记 ledger，挡不住放行；
- **按 kind 分流**：只对 ``kind="python"`` 做语法 / 危险导入检查，其余 kind 只做
  非空检查；全过程记 ``g1_extended_check`` 事件（errors / warnings）。
"""

import json

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.gate.gatekeeper import Gatekeeper
from agentcrew.gate.reviewer import Reviewer
from agentcrew.gate.static_checks import (
    check_file_nonempty,
    check_imports,
    check_python_syntax,
    run_extended_checks,
)
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard
from agentcrew.task.task import TaskStatus as S


# ── 纯函数 ─────────────────────────────────────────────────────────────────


def test_valid_python_passes_syntax_check():
    assert check_python_syntax("a.py", "def f():\n    return 1\n") == []


def test_python_syntax_error_detected():
    errors = check_python_syntax("a.py", "def f(\n  pass\n")
    assert len(errors) == 1 and "a.py" in errors[0] and "语法" in errors[0]


def test_dangerous_import_is_warning():
    warns = check_imports("import os\nos.getcwd()\n")
    assert len(warns) == 1 and "os" in warns[0]


def test_safe_import_is_not_warning():
    assert check_imports("import json\nimport math\n") == []


def test_empty_file_is_error():
    assert check_file_nonempty("a.py", "") == ["a.py 为空文件"]
    assert check_file_nonempty("a.py", "   \n  \t ") == ["a.py 为空文件"]


def test_nonempty_file_passes_nonempty_check():
    assert check_file_nonempty("a.py", "x = 1\n") == []


def test_run_extended_python_kind_runs_syntax_and_imports():
    result = run_extended_checks(
        [("a.py", "import os\ndef f():\n    return 1\n")], kind="python")
    assert result.errors == []                              # 合法代码无 error
    assert any("os" in w for w in result.warnings)          # 危险导入 = warning


def test_run_extended_syntax_error_in_python_is_error():
    result = run_extended_checks([("a.py", "def f(\n")], kind="python")
    assert any("语法" in e for e in result.errors)


def test_run_extended_non_python_kind_only_nonempty():
    """非 python kind：语法/导入检查不跑，只做非空。"""
    result = run_extended_checks([("note.txt", "def f(\n")], kind="text")
    assert result.errors == [] and result.warnings == []


def test_run_extended_non_python_empty_file_is_error():
    result = run_extended_checks([("note.txt", "")], kind="text")
    assert result.errors == ["note.txt 为空文件"]


# ── Gatekeeper 集成 ────────────────────────────────────────────────────────


GOOD = {"judgements": [{"criterion_index": 0, "satisfied": True,
                        "evidence": "ok"}],
        "suggestions": [], "score": 0.9}


def _board(task):
    board = TaskBoard([task])
    board.transition(task.id, S.READY)
    board.transition(task.id, S.RUNNING)
    board.transition(task.id, S.REVIEW)
    return board


def _make(task_id, path, kind, content, tmp_path, monkeypatch, *, must_contain=None):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=lg)
    task = Task(id=task_id, title="t", description="d", role="backend_dev",
                acceptance_criteria=["a"],
                deliverables=[ArtifactSpec(path=path, kind=kind, description="d",
                                           must_contain=must_contain or [])])
    store.commit(path, content, spec=task.deliverables[0], task=task)
    board = _board(task)
    fx = tmp_path / "fx"
    fx.mkdir(exist_ok=True)
    (fx / "reviews.json").write_text(json.dumps({task_id: GOOD}), encoding="utf-8")
    models = ModelFactory(s, ledger=lg, fixtures_dir=fx)
    rev = Reviewer(models, RoleCatalog(BUILTIN_ROLES), store, lg)
    gk = Gatekeeper(rev, store, board, gate="none", ledger=lg, prompter=None)
    return dict(gk=gk, lg=lg, board=board, task=task)


async def test_dangerous_import_warns_but_does_not_block(tmp_path, monkeypatch):
    """危险导入 → warning 不阻断，G1/G2 照常放行，ledger 记 g1_extended_check。"""
    ctx = _make("T-1", "a.py", "python", "def f():\n    import os\n    return 1\n",
                tmp_path, monkeypatch)
    d = await ctx["gk"].evaluate(ctx["task"], round_no=1)
    assert d.verdict == "approve" and d.next_status is S.SUCCEEDED
    events = list(ctx["lg"].events("g1_extended_check"))
    assert len(events) == 1
    assert events[0].data["errors"] == []
    assert any("os" in w for w in events[0].data["warnings"])


async def test_extended_syntax_error_blocks_gate(tmp_path, monkeypatch):
    """扩展语法错误（此处 run_static 也已报）→ G1 reject。"""
    ctx = _make("T-2", "a.py", "python", "def f(\n", tmp_path, monkeypatch)
    d = await ctx["gk"].evaluate(ctx["task"], round_no=1)
    assert d.verdict == "reject"
    assert d.record.layer == "static"
    events = list(ctx["lg"].events("g1_extended_check"))
    assert events and events[0].data["errors"]


async def test_extended_empty_python_file_is_error(tmp_path, monkeypatch):
    ctx = _make("T-3", "a.py", "python", "", tmp_path, monkeypatch)
    d = await ctx["gk"].evaluate(ctx["task"], round_no=1)
    assert d.verdict == "reject"
    assert any("空文件" in i for i in d.record.issues)


async def test_g1_extended_check_event_recorded(tmp_path, monkeypatch):
    ctx = _make("T-4", "a.py", "python", "def f():\n    return 1\n",
                tmp_path, monkeypatch)
    await ctx["gk"].evaluate(ctx["task"], round_no=1)
    events = list(ctx["lg"].events("g1_extended_check"))
    assert len(events) == 1
    assert events[0].data["task_id"] == "T-4"
    assert events[0].data["round"] == 1
    assert events[0].data["errors"] == []
    assert events[0].data["warnings"] == []
