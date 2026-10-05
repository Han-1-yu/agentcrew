"""DAG 图算法行为测试（spec §6.2）。"""

import pytest

from agentcrew.task.graph import CyclicGraphError, TaskGraph, blocked_ids, ready_ids
from agentcrew.task.task import Task
from agentcrew.task.task import TaskStatus as S


def mk(tid, deps=(), status=S.PENDING):
    return Task(id=tid, title=tid, description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=[], dependencies=list(deps),
                status=status)

def test_build_records_deps_and_dependents():
    g = TaskGraph.build([mk("A"), mk("B", ["A"]), mk("C", ["A"])])
    assert g.dependencies("B") == frozenset({"A"})
    assert g.dependents("A") == frozenset({"B", "C"})

def test_diamond_topology_orders_correctly():
    g = TaskGraph.build([mk("A"), mk("B", ["A"]), mk("C", ["A"]), mk("D", ["B", "C"])])
    order = g.topo_order()
    assert order.index("A") < order.index("B") < order.index("D")
    assert order.index("A") < order.index("C") < order.index("D")

def test_self_loop_detected():
    assert TaskGraph.build([mk("A", ["A"])]).cycles() == [["A"]]

def test_two_cycle_detected():
    cycles = TaskGraph.build([mk("A", ["B"]), mk("B", ["A"])]).cycles()
    assert len(cycles) == 1 and set(cycles[0]) == {"A", "B"}

def test_three_cycle_detected():
    cycles = TaskGraph.build([mk("A", ["C"]), mk("B", ["A"]), mk("C", ["B"])]).cycles()
    assert len(cycles) == 1 and set(cycles[0]) == {"A", "B", "C"}

def test_acyclic_graph_has_no_cycles():
    assert TaskGraph.build([mk("A"), mk("B", ["A"]), mk("C", ["B"])]).cycles() == []

def test_topo_order_raises_on_cycle():
    with pytest.raises(CyclicGraphError) as ei:
        TaskGraph.build([mk("A", ["B"]), mk("B", ["A"])]).topo_order()
    assert ei.value.cycles

def test_descendants_is_transitive():
    g = TaskGraph.build([mk("A"), mk("B", ["A"]), mk("C", ["B"]), mk("D")])
    assert g.descendants("A") == {"B", "C"}
    assert g.descendants("D") == set()

def test_ready_ids_requires_all_deps_succeeded():
    tasks = {"A": mk("A", status=S.SUCCEEDED), "B": mk("B", ["A"]),
             "C": mk("C", ["A", "B"])}
    assert set(ready_ids(tasks)) == {"B"}

def test_ready_ids_empty_when_root_running():
    tasks = {"A": mk("A", status=S.RUNNING), "B": mk("B", ["A"])}
    assert ready_ids(tasks) == []

def test_blocked_ids_when_dep_failed():
    tasks = {"A": mk("A", status=S.FAILED), "B": mk("B", ["A"])}
    assert blocked_ids(tasks) == ["B"]

def test_skipped_propagates():
    tasks = {"A": mk("A", status=S.SKIPPED), "B": mk("B", ["A"])}
    assert ready_ids(tasks) == [] and blocked_ids(tasks) == []


# ---- 环检测的补充形状（brief 之外，按控制器要求专项验证）----

def test_two_disjoint_cycles_are_both_reported():
    g = TaskGraph.build([mk("A", ["B"]), mk("B", ["A"]),
                         mk("C", ["D"]), mk("D", ["C"]), mk("E")])
    cycles = g.cycles()
    assert {frozenset(c) for c in cycles} == {
        frozenset({"A", "B"}), frozenset({"C", "D"}),
    }

def test_cycles_exclude_acyclic_nodes_hanging_off_a_cycle():
    g = TaskGraph.build([mk("A", ["B"]), mk("B", ["A"]), mk("C", ["B"]),
                         mk("Z"), mk("Y", ["Z"])])
    assert g.cycles() == [["A", "B"]]

def test_each_cycle_is_a_closed_path_in_dependents_direction():
    g = TaskGraph.build([mk("A", ["C"]), mk("B", ["A"]), mk("C", ["B"])])
    (cycle,) = g.cycles()
    assert len(cycle) == 3
    for src, dst in zip(cycle, cycle[1:] + cycle[:1], strict=True):
        assert dst in g.dependents(src)

def test_figure_eight_reports_one_entry_per_simple_cycle():
    g = TaskGraph.build([mk("A", ["B", "C"]), mk("B", ["A"]), mk("C", ["A"])])
    cycles = g.cycles()
    assert {frozenset(c) for c in cycles} == {
        frozenset({"A", "B"}), frozenset({"A", "C"}),
    }

def test_self_loop_reported_but_build_does_not_raise():
    g = TaskGraph.build([mk("A", ["A"])])
    assert g.cycles() == [["A"]]
    with pytest.raises(CyclicGraphError) as ei:
        g.topo_order()
    assert ei.value.cycles == [["A"]]


# ---- 拓扑序与建图的补充行为 ----

def test_topo_order_is_stable_by_id_not_insertion_order():
    g = TaskGraph.build([mk("D"), mk("B"), mk("C"), mk("A")])
    assert g.topo_order() == ["A", "B", "C", "D"]

def test_diamond_topo_order_is_exact():
    g = TaskGraph.build([mk("D", ["B", "C"]), mk("C", ["A"]),
                         mk("B", ["A"]), mk("A")])
    assert g.topo_order() == ["A", "B", "C", "D"]

def test_duplicate_dependency_entries_do_not_break_topo_order():
    g = TaskGraph.build([mk("A"), mk("B", ["A", "A"])])
    assert g.dependencies("B") == frozenset({"A"})
    assert g.topo_order() == ["A", "B"]

def test_dangling_dependency_becomes_sourceless_node():
    g = TaskGraph.build([mk("B", ["GHOST"])])
    assert g.dependencies("GHOST") == frozenset()
    assert g.dependents("GHOST") == frozenset({"B"})
    assert g.topo_order() == ["GHOST", "B"]

def test_leaf_task_is_a_key_with_empty_dependents():
    g = TaskGraph.build([mk("A"), mk("B", ["A"])])
    assert g.dependents("B") == frozenset()
    # 映射在 dependents_map 上（dependents 是方法），且是普通 dict
    assert type(g.dependents_map) is dict
    assert g.dependents_map == {"A": frozenset({"B"}), "B": frozenset()}

def test_build_does_not_mutate_input_tasks():
    tasks = [mk("A"), mk("B", ["A"])]
    g = TaskGraph.build(tasks)
    assert [list(t.dependencies) for t in tasks] == [[], ["A"]]
    assert isinstance(g.dependencies("B"), frozenset)

def test_descendants_terminates_on_cyclic_graph():
    g = TaskGraph.build([mk("A", ["B"]), mk("B", ["A"]), mk("C", ["A"])])
    assert g.descendants("A") == {"B", "C"}

def test_empty_graph_has_no_nodes_and_no_cycles():
    g = TaskGraph.build([])
    assert g.topo_order() == [] and g.cycles() == []
    assert set(g.deps) == set(g.dependents_map) == set()


# ---- 就绪 / 阻塞集的补充行为 ----

def test_ready_ids_returns_ascending_order():
    tasks = {"C": mk("C"), "A": mk("A"), "B": mk("B")}
    assert ready_ids(tasks) == ["A", "B", "C"]

def test_blocked_ids_covers_cancelled_and_returns_ascending_order():
    tasks = {"D": mk("D", ["A"]), "A": mk("A", status=S.CANCELLED),
             "C": mk("C", ["A"]), "B": mk("B")}
    assert blocked_ids(tasks) == ["C", "D"]
    assert ready_ids(tasks) == ["B"]

def test_missing_dependency_is_neither_ready_nor_blocked():
    tasks = {"B": mk("B", ["GHOST"])}
    assert ready_ids(tasks) == []
    assert blocked_ids(tasks) == []

def test_non_pending_tasks_are_never_ready_or_blocked():
    tasks = {"A": mk("A", status=S.FAILED),
             "B": mk("B", ["A"], status=S.READY),
             "C": mk("C", ["A"], status=S.BLOCKED)}
    assert ready_ids(tasks) == []
    assert blocked_ids(tasks) == []
