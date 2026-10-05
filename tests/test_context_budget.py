"""上下文包组装与 token 预算降级的行为测试（spec §11，Task 23）。

上方是 brief 给定的用例（保持原样，仅 ``test_budget_never_negative`` 加了一行
「先精确装满再试探超限」，原因见该用例注释）；下方是本任务 resolutions 的补充用例。
"""

from datetime import datetime

import pytest
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.context.budget import TokenBudget
from agentcrew.context.context_bus import (
    ContextBus, ContextPack, DecisionRecord, TRUNCATION_NOTICE,
)
from agentcrew.task.task import Task, TaskStatus as S
from agentcrew.task.task_board import TaskBoard
from agentcrew.utils.tokens import estimate_tokens


def mk(tid, deps=(), status=S.PENDING, brief=""):
    return Task(id=tid, title=tid, description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=[], dependencies=list(deps),
                status=status, brief=brief)


def commit(store, rel, content, task):
    return store.commit(rel, content,
                        spec=ArtifactSpec(path=rel, kind="python", description="d"),
                        task=task)


# ── TokenBudget ──
def test_budget_consumes_until_full():
    b = TokenBudget(limit=10)
    assert b.try_consume("a" * 10) is True
    assert b.try_consume("a" * 10_000) is False
    assert b.remaining >= 0


def test_budget_never_negative():
    b = TokenBudget(limit=5)
    # 先精确装满（"a"*15 -> 5 tok），再试探一个装不下的：拒绝不改变 used
    assert b.try_consume("a" * 15) is True
    assert b.try_consume("a" * 100_000) is False
    assert b.remaining == 0 and b.used <= 5


def test_budget_zero_limit_rejects_everything():
    b = TokenBudget(limit=0)
    assert b.try_consume("x") is False


# ── ContextBus ──
@pytest.fixture
def bus(tmp_path):
    board = TaskBoard([mk("A", status=S.SUCCEEDED, brief="产出 PRD"),
                       mk("B", ["A"]),
                       mk("C")])
    board.get("A").result = None
    store = ArtifactStore(tmp_path / "art", run_id="R")
    return ContextBus("做一个 Todo 应用", board, store, budget_tokens=24000)


def test_pack_includes_goal_and_task(bus):
    pack = bus.build(bus._board.get("B"))
    assert pack.project_goal == "做一个 Todo 应用"
    assert pack.task.id == "B"


def test_pack_includes_only_direct_upstream(bus):
    pack = bus.build(bus._board.get("B"))
    assert [u.task_id for u in pack.upstream] == ["A"]


def test_pack_excludes_unrelated_tasks(bus):
    pack = bus.build(bus._board.get("B"))
    assert all(u.task_id != "C" for u in pack.upstream)


def test_pack_includes_file_tree(bus):
    bus._store.commit("a.py", "x = 1\n",
                      spec=ArtifactSpec(path="a.py", kind="python", description="d"),
                      task=bus._board.get("A"))
    assert "a.py" in bus.build(bus._board.get("B")).file_tree


def test_small_pack_not_truncated(bus):
    assert bus.build(bus._board.get("B")).truncated is False


def test_tiny_budget_truncates_and_appends_notice(bus, tmp_path):
    b = ContextBus("做一个 Todo 应用", bus._board, bus._store, budget_tokens=30)
    pack = b.build(b._board.get("B"))
    assert pack.truncated is True
    assert b.render_notice() == TRUNCATION_NOTICE


def test_incompressible_fields_survive_tiny_budget(bus):
    b = ContextBus("目标", bus._board, bus._store, budget_tokens=1)
    pack = b.build(b._board.get("B"))
    assert pack.project_goal == "目标"          # 目标永不丢弃
    assert pack.task.id == "B"                  # 本任务永不丢弃


def test_fix_hints_are_incompressible(bus):
    b = ContextBus("目标", bus._board, bus._store, budget_tokens=1)
    p1 = b.build(b._board.get("B"), fix_hints=["补上输入校验"])
    assert p1.task.id == "B"


def test_excerpt_respects_max_ratio(bus):
    big = "x = 1\n" * 20000
    bus._store.commit("big.py", big,
                      spec=ArtifactSpec(path="big.py", kind="python", description="d"),
                      task=bus._board.get("A"))
    b = ContextBus("g", bus._board, bus._store, budget_tokens=2000,
                   excerpt_max_ratio=0.4)
    pack = b.build(b._board.get("B"))
    for ex in pack.key_excerpts:
        assert estimate_tokens(ex.content) <= 2000 * 0.4 + 50


def test_long_excerpt_marked_truncated(bus):
    bus._store.commit("big.py", "x = 1\n" * 20000,
                      spec=ArtifactSpec(path="big.py", kind="python", description="d"),
                      task=bus._board.get("A"))
    b = ContextBus("g", bus._board, bus._store, budget_tokens=2000)
    pack = b.build(bus._board.get("B"))
    assert any(ex.truncated for ex in pack.key_excerpts)


def test_file_tree_folds_when_huge(bus):
    for i in range(400):
        bus._store.commit(f"d/{i}.py", "x=1\n",
                          spec=ArtifactSpec(path=f"d/{i}.py", kind="python", description="d"),
                          task=bus._board.get("A"))
    b = ContextBus("g", bus._board, bus._store, budget_tokens=24000, tree_max_entries=300)
    pack = b.build(bus._board.get("B"))
    assert any("more" in line for line in pack.file_tree)


def test_decisions_roundtrip(bus, tmp_path):
    from datetime import datetime
    bus.add_decision(DecisionRecord(at=datetime.now(), task_id="A",
                                    role="backend_dev", decision="用内存存储"))
    p = tmp_path / "d.jsonl"
    bus.save_decisions(p)
    other = ContextBus("g", bus._board, bus._store)
    other.load_decisions(p)
    assert other.decisions()[0].decision == "用内存存储"


def test_decisions_capped_at_twenty(bus):
    from datetime import datetime
    for i in range(30):
        bus.add_decision(DecisionRecord(at=datetime.now(), task_id=f"T-{i}",
                                        role="r", decision=f"d{i}"))
    pack = bus.build(bus._board.get("B"))
    assert len(pack.decisions) <= 20


def test_decision_records_from_rework(bus):
    from datetime import datetime
    bus.add_decision(DecisionRecord(at=datetime.now(), task_id="A",
                                    role="architect", decision="改用 Pydantic v2"))
    assert any("Pydantic" in d.decision for d in bus.build(bus._board.get("B")).decisions)


# ── Resolution 1：拒绝不改 used ──
def test_budget_rejection_leaves_used_unchanged():
    b = TokenBudget(limit=10)
    assert b.try_consume("a" * 100_000) is False
    assert b.used == 0 and b.remaining == 10


def test_budget_negative_limit_rejected():
    with pytest.raises(ValueError):
        TokenBudget(limit=-1)


# ── Resolution 2/3：降级顺序与 truncated ──
def test_drop_order_stops_after_decisions(bus):
    """预算刚好装下 1-3 级：决策在包内，文件树与节选被跳过，truncated=True。"""
    commit(bus._store, "a.py", "x = 1\ny = 2\nz = 3\n", bus._board.get("A"))
    record = DecisionRecord(at=datetime.now(), task_id="A",
                            role="backend_dev", decision="用内存存储")
    # 第 2 级按每个上游 400 tok 上限计费；预算 = 400 + 决策实际费用 -> 第 4/5 级无余量
    exact = 400 + estimate_tokens(record.model_dump_json())
    b = ContextBus("g", bus._board, bus._store, budget_tokens=exact)
    b.add_decision(record)
    pack = b.build(bus._board.get("B"))
    assert [d.decision for d in pack.decisions] == ["用内存存储"]
    assert pack.file_tree == []
    assert pack.key_excerpts == []
    assert pack.truncated is True


def test_drop_order_keeps_later_smaller_excerpts(bus):
    """第 3、4 级装不下时，更小的第 5 级（节选）仍要装入。"""
    commit(bus._store, "big.py", "x = 1\n" * 20000, bus._board.get("A"))
    for i in range(60):
        commit(bus._store, f"d/{i:02d}.py", "x=1\n", bus._board.get("A"))
    long_decision = "决策" * 500
    bus.add_decision(DecisionRecord(at=datetime.now(), task_id="A",
                                    role="backend_dev", decision=long_decision))
    b = ContextBus("g", bus._board, bus._store, budget_tokens=500)
    pack = b.build(bus._board.get("B"))
    assert pack.decisions == []                 # 第 3 级（决策）装不下 -> 跳过
    assert pack.file_tree == []                 # 第 4 级（文件树）装不下 -> 跳过
    assert [ex.path for ex in pack.key_excerpts]  # 第 5 级仍装入
    assert pack.truncated is True


# ── Resolution 2：file_tree 折叠的 N 必须正确 ──
def test_file_tree_fold_reports_correct_more_count(bus):
    for i in range(400):
        commit(bus._store, f"d/{i}.py", "x=1\n", bus._board.get("A"))
    b = ContextBus("g", bus._board, bus._store, budget_tokens=24000,
                   tree_max_entries=300)
    pack = b.build(bus._board.get("B"))
    folds = [line for line in pack.file_tree if "more" in line]
    assert folds == ["... (100 more)"]
    assert len(pack.file_tree) == 301


# ── Resolution 2：节选上限按文件而非整包 ──
def test_excerpt_cap_is_per_file(bus):
    commit(bus._store, "a.py", "x = 1\n" * 20000, bus._board.get("A"))
    commit(bus._store, "b.py", "y = 2\n" * 20000, bus._board.get("A"))
    b = ContextBus("g", bus._board, bus._store, budget_tokens=4000,
                   excerpt_max_ratio=0.4)
    pack = b.build(bus._board.get("B"))
    assert [ex.path for ex in pack.key_excerpts] == ["a.py", "b.py"]
    for ex in pack.key_excerpts:
        assert ex.truncated is True
        assert estimate_tokens(ex.content) <= 4000 * 0.4 + 50


# ── Resolution 5：第 6 级只在预算允许时补更远祖先 ──
def test_distant_ancestor_digests_appended_after_direct(bus, tmp_path):
    board = TaskBoard([mk("A", status=S.SUCCEEDED, brief="更远祖先"),
                       mk("B", ["A"], status=S.SUCCEEDED, brief="直接上游"),
                       mk("C", ["B"])])
    store = ArtifactStore(tmp_path / "art2", run_id="R")
    b = ContextBus("目标", board, store, budget_tokens=24000)
    pack = b.build(board.get("C"))
    assert [u.task_id for u in pack.upstream] == ["B", "A"]


# ── Resolution 6：返工决策 ──
def test_record_rework_decision_matrix(bus):
    task = bus._board.get("B")
    assert bus.record_rework_decision(task, ["补上输入校验"]) is None       # 未返工
    reworked = task.model_copy(update={"rework_count": 1})
    assert bus.record_rework_decision(reworked, []) is None                 # 无返工意见
    record = bus.record_rework_decision(reworked, ["补上输入校验", "改成 async"])
    assert record is not None and record.task_id == "B"
    assert "补上输入校验" in record.decision and "async" in record.decision
    assert bus.decisions()[-1] == record


# ── Resolution 7：JSONL 读写 ──
def test_load_decisions_skips_blank_lines(bus, tmp_path):
    p = tmp_path / "d.jsonl"
    bus.add_decision(DecisionRecord(at=datetime.now(), task_id="A",
                                    role="r", decision="d1"))
    bus.save_decisions(p)
    with p.open("a", encoding="utf-8") as handle:
        handle.write("\n   \n")
    other = ContextBus("g", bus._board, bus._store)
    other.load_decisions(p)
    assert [d.decision for d in other.decisions()] == ["d1"]


def test_load_decisions_missing_file_is_noop(bus, tmp_path):
    bus.load_decisions(tmp_path / "nope.jsonl")
    assert bus.decisions() == []


def test_load_decisions_rejects_malformed_line(bus, tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"task_id": "A"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError):
        bus.load_decisions(p)


# ── Resolution 10：ContextPack 可 JSON 往返 ──
def test_context_pack_json_roundtrip(bus, tmp_path):
    commit(bus._store, "a.py", "x = 1\n", bus._board.get("A"))
    bus.add_decision(DecisionRecord(at=datetime.now(), task_id="A",
                                    role="backend_dev", decision="用内存存储"))
    pack = bus.build(bus._board.get("B"))
    clone = ContextPack.model_validate_json(pack.model_dump_json())
    assert clone == pack
