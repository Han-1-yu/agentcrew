"""预设 Crew 样例登记表（spec §16.2 的三种编排形状）。

三个样例覆盖三种协作形状，都可以在**完全离线**（无任何 API Key）下跑通：

============  ==============================================  ==========
样例          形状                                            任务数
============  ==============================================  ==========
``todo``      混合并发：多条支线并行，最后汇入收尾任务          8 + 1
``linear``    简单串行：严格一条链，每一步等上一步              4 + 1
``hierarchy`` 层级管理：分出两条实现路径，汇合后进用例与文档     6 + 1
============  ==============================================  ==========

``todo`` 是仓库原有的演示目标（``examples/todo_web_project.py``），
夹具在 ``examples/fixtures``；后两个是本模块新增的独立夹具目录。
每个样例自带 ``plan_todo.json`` / ``reviews.json`` / ``responses/<task>/``，
因此互不干扰 —— 想加新样例只需在 :data:`SAMPLES` 里登记一行。

用法：

- CLI：``python main.py run --sample linear --mock --no-live``
- 测试：``examples.samples.activate("hierarchy")`` 后再跑 ``Crew``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["SAMPLES", "SampleSpec", "activate", "get", "names"]

#: 本模块所在目录（``examples/samples``）。
_SAMPLES_DIR: Path = Path(__file__).resolve().parent

#: 仓库根目录（``examples/samples`` 上两级）。
_REPO_ROOT: Path = _SAMPLES_DIR.parents[1]


@dataclass(frozen=True)
class SampleSpec:
    """一个预设样例的完整定义。

    ``fixtures`` 是 mock provider 的回放根目录：里面有 ``plan_todo.json``、
    ``reviews.json`` 与 ``responses/<task_id>/attempt-1.md``。
    ``expected_task_ids`` 只列**计划阶段**的任务；收尾任务 ``T-FINAL`` 由平台
    追加，不在其中 —— 断言时用 ``len(expected) + 1`` 拿真实任务数。
    """

    name: str
    title: str
    shape: str
    goal: str
    fixtures: Path
    expected_task_ids: tuple[str, ...]

    @property
    def expected_total_tasks(self) -> int:
        """真实任务数：计划任务 + 平台追加的 ``T-FINAL``。"""
        return len(self.expected_task_ids) + 1

    def dependency_map(self) -> dict[str, tuple[str, ...]]:
        """从 ``plan_todo.json`` 读依赖关系，供形状断言使用。

        直接读夹具而不是读运行结果：样例的**形状**是设计意图，
        应该在跑之前就能验，跑挂了也能先说清「本来该是什么形状」。
        """
        import json

        data = json.loads((self.fixtures / "plan_todo.json").read_text(encoding="utf-8"))
        return {
            task["id"]: tuple(task.get("dependencies") or ())
            for task in data["tasks"]
        }


def _spec(name: str, title: str, shape: str, goal: str, task_ids: tuple[str, ...]) -> SampleSpec:
    return SampleSpec(
        name=name,
        title=title,
        shape=shape,
        goal=goal,
        fixtures=_SAMPLES_DIR / name,
        expected_task_ids=task_ids,
    )


#: 已登记的样例；键即 ``--sample`` 的取值。
SAMPLES: dict[str, SampleSpec] = {
    "todo": SampleSpec(
        name="todo",
        title="Todo List Web 应用（混合并发）",
        shape="mixed",
        # 与 ``examples/todo_web_project.py`` 的 PROJECT_GOAL 同一句话：
        # 两处必须一致，否则 --demo 与 --sample todo 会跑到不同的目标上。
        goal=(
            "开发一个 Todo List Web 应用：用户可在浏览器中新增、查看、编辑、删除待办事项，"
            "数据通过 REST API 持久化在服务端，前端为单页应用，附完整文档、测试与 Docker 部署配置。"
        ),
        fixtures=_REPO_ROOT / "examples" / "fixtures",
        expected_task_ids=(
            "T-001", "T-002", "T-003", "T-004",
            "T-005", "T-006", "T-007", "T-008",
        ),
    ),
    "linear": _spec(
        "linear",
        "logstat 日志统计工具（简单串行）",
        "serial",
        "开发一个命令行日志统计工具 logstat：读取访问日志，输出状态码分布与耗时分位，"
        "附设计说明与使用文档。",
        ("T-001", "T-002", "T-003", "T-004"),
    ),
    "hierarchy": _spec(
        "hierarchy",
        "内部知识库检索服务（层级管理）",
        "hierarchical",
        "实现一个内部知识库检索服务：倒排索引 + 检索接口 + 用例与接入文档，"
        "索引层与服务层并行实现后在用例层汇合。",
        ("T-001", "T-002", "T-003", "T-004", "T-005", "T-006"),
    ),
}


def names() -> tuple[str, ...]:
    """全部样例名，按登记顺序。"""
    return tuple(SAMPLES)


def get(name: str) -> SampleSpec:
    """按名取样例；未登记时抛 ``KeyError`` 并列出可选值。"""
    try:
        return SAMPLES[name]
    except KeyError:
        raise KeyError(
            f"未登记的样例 {name!r}；可选：{', '.join(names())}"
        ) from None


def activate(name: str) -> SampleSpec:
    """把样例夹具设为 mock provider 的默认回放目录，并返回样例定义。

    改的是 ``agentcrew.models.factory.DEFAULT_FIXTURES_DIR`` —— 这是
    :mod:`agentcrew.crew.crew` 在 ``run()`` 时**现读**的注入点（见其
    ``_fixtures_dir()`` 的注释），测试与 CLI 都靠它切换夹具。
    只在当前进程内生效，不影响其它进程或已存在的 run。
    """
    spec = get(name)
    if not (spec.fixtures / "plan_todo.json").is_file():
        raise FileNotFoundError(f"样例 {name!r} 缺少夹具：{spec.fixtures / 'plan_todo.json'}")

    from agentcrew.models import factory as model_factory

    model_factory.DEFAULT_FIXTURES_DIR = spec.fixtures
    return spec
