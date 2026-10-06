"""树状任务拆解的测试（V2 第 4 项）。"""

from __future__ import annotations

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.task.graph import TaskGraph
from agentcrew.task.task import Task


def _task(task_id: str, **kwargs) -> Task:
    defaults = dict(
        id=task_id, title=f"Task {task_id}", description="d",
        role="backend_dev", acceptance_criteria=["ok"],
        deliverables=[ArtifactSpec(path=f"{task_id}.py", kind="python", description="d")],
    )
    defaults.update(kwargs)
    return Task.model_validate(defaults)


class TestTaskTree:
    def test_parent_id_field_exists(self):
        task = _task("T-1", parent_id="T-0")
        assert task.parent_id == "T-0"

    def test_root_task_has_no_parent(self):
        task = _task("T-1")
        assert task.parent_id is None

    def test_build_records_parent_child(self):
        tasks = [
            _task("T-1"),
            _task("T-2", parent_id="T-1"),
            _task("T-3", parent_id="T-1"),
        ]
        graph = TaskGraph.build(tasks)
        assert graph.parent("T-2") == "T-1"
        assert graph.parent("T-3") == "T-1"
        assert graph.children("T-1") == frozenset({"T-2", "T-3"})

    def test_parent_auto_added_as_dependency(self):
        """子任务声明 parent_id 后，父任务自动进入 dependencies（DAG 边子集）。"""
        tasks = [
            _task("T-1"),
            _task("T-2", parent_id="T-1"),  # 没有显式 dependencies
        ]
        graph = TaskGraph.build(tasks)
        assert "T-1" in graph.dependencies("T-2")
        # 拓扑序保证 T-1 在 T-2 之前
        order = graph.topo_order()
        assert order.index("T-1") < order.index("T-2")

    def test_parent_already_in_dependencies_no_duplicate(self):
        tasks = [
            _task("T-1"),
            _task("T-2", parent_id="T-1", dependencies=["T-1"]),
        ]
        graph = TaskGraph.build(tasks)
        assert graph.dependencies("T-2") == frozenset({"T-1"})  # 不重复

    def test_ancestors_chain(self):
        tasks = [
            _task("T-1"),
            _task("T-2", parent_id="T-1"),
            _task("T-3", parent_id="T-2"),
            _task("T-4", parent_id="T-3"),
        ]
        graph = TaskGraph.build(tasks)
        assert graph.ancestors("T-4") == ["T-3", "T-2", "T-1"]
        assert graph.ancestors("T-1") == []

    def test_subtree(self):
        tasks = [
            _task("T-1"),
            _task("T-2", parent_id="T-1"),
            _task("T-3", parent_id="T-1"),
            _task("T-4", parent_id="T-2"),
            _task("T-5", parent_id="T-2"),
        ]
        graph = TaskGraph.build(tasks)
        assert graph.subtree("T-1") == {"T-2", "T-3", "T-4", "T-5"}
        assert graph.subtree("T-2") == {"T-4", "T-5"}
        assert graph.subtree("T-4") == set()

    def test_roots_and_leaves(self):
        tasks = [
            _task("T-1"),
            _task("T-2", parent_id="T-1"),
            _task("T-3", parent_id="T-1"),
            _task("T-4", parent_id="T-2"),
            _task("T-5"),  # 另一个根
        ]
        graph = TaskGraph.build(tasks)
        assert graph.roots() == ["T-1", "T-5"]
        assert graph.leaves() == ["T-3", "T-4", "T-5"]

    def test_depth(self):
        tasks = [
            _task("T-1"),
            _task("T-2", parent_id="T-1"),
            _task("T-3", parent_id="T-2"),
        ]
        graph = TaskGraph.build(tasks)
        assert graph.depth("T-1") == 0
        assert graph.depth("T-2") == 1
        assert graph.depth("T-3") == 2

    def test_mixed_tree_and_dag(self):
        """树状父子边与普通依赖边共存，不冲突。"""
        tasks = [
            _task("T-1"),
            _task("T-2", parent_id="T-1"),
            _task("T-3", dependencies=["T-2"]),  # 普通依赖，非父子
        ]
        graph = TaskGraph.build(tasks)
        assert graph.parent("T-2") == "T-1"
        assert graph.parent("T-3") is None  # T-3 不是 T-2 的子任务
        assert "T-2" in graph.dependencies("T-3")
        assert graph.children("T-1") == frozenset({"T-2"})

    def test_backward_compatible_no_parent(self):
        """没有 parent_id 的任务图行为与 V1 完全一致。"""
        tasks = [
            _task("T-1"),
            _task("T-2", dependencies=["T-1"]),
        ]
        graph = TaskGraph.build(tasks)
        assert graph.parent("T-1") is None
        assert graph.parent("T-2") is None
        assert graph.children("T-1") == frozenset()
        assert graph.roots() == ["T-1", "T-2"]  # 无父子关系时都是根
        assert graph.topo_order() == ["T-1", "T-2"]
