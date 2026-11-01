"""预设 Crew 样例：登记表自洽、编排形状、离线端到端。

三层断言各自独立，失败时能直接指向"哪一层坏了"：

1. **登记表与夹具**（纯文件检查）：三个样例的 plan / reviews / responses 是否齐备；
2. **编排形状**（读 ``plan_todo.json`` 的依赖图）：串行样例每层只有一个任务，
   层级样例必须有分叉与汇合 —— 形状是设计意图，跑挂了也能先说清"本来该是什么样"；
3. **端到端**（真的跑一次）：状态、任务数、依赖关系与产物是否与夹具一致。

第 3 层每个样例跑一次完整 mock run（约 0.2–1 秒），因此按模块共享。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.runtime.checkpoint import RunStatus
from examples.samples import SAMPLES, SampleSpec, get, names

#: 平台追加的收尾任务 id（不在 plan_todo.json 里，由 Crew 补上）。
FINAL_TASK_ID = "T-FINAL"

#: 与 conftest 同一份清单：断言"无密钥可跑"要逐个删干净。
API_KEY_ENVS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DASHSCOPE_API_KEY",
    "ARK_API_KEY",
    "QIANFAN_API_KEY",
    "DEEPSEEK_API_KEY",
)


# ── 形状计算 ──────────────────────────────────────────────────────────────


def layers_of(deps: dict[str, tuple[str, ...]]) -> list[list[str]]:
    """把依赖图按最长路径分层，返回每层的任务 id（层内按 id 排序）。

    用最长路径而不是最短路径：一个任务必须等**全部**依赖完成才可能就绪，
    所以它的层号是 ``max(依赖层号) + 1``；用最短路径会把汇合任务提前到
    它还有依赖没完成的层上，算出来的"并行度"是假的。
    """
    layer: dict[str, int] = {}

    def depth(task_id: str, seen: frozenset[str] = frozenset()) -> int:
        if task_id in layer:
            return layer[task_id]
        assert task_id not in seen, f"依赖图有环：{sorted(seen)} -> {task_id}"
        parents = deps.get(task_id, ())
        value = 0 if not parents else 1 + max(depth(p, seen | {task_id}) for p in parents)
        layer[task_id] = value
        return value

    for task_id in deps:
        depth(task_id)

    # 收尾任务不在 plan 里：它依赖所有没有后继的任务，层号落在最深的后继层
    width = max(layer.values()) + 1 if layer else 0
    buckets: list[list[str]] = [[] for _ in range(width)]
    for task_id, level in layer.items():
        buckets[level].append(task_id)
    return [sorted(bucket) for bucket in buckets]


def fan_out(deps: dict[str, tuple[str, ...]]) -> dict[str, int]:
    """每个任务被多少个后继依赖（分叉点 = 计数 ≥ 2）。"""
    counts = {task_id: 0 for task_id in deps}
    for parents in deps.values():
        for parent in parents:
            counts[parent] = counts.get(parent, 0) + 1
    return counts


# ── 1. 登记表与夹具（不跑 run）──────────────────────────────────────────────


@pytest.mark.parametrize("name", names())
def test_sample_registry_entry_is_complete(name: str) -> None:
    spec = get(name)
    assert spec.goal.strip(), f"{name} 的目标为空"
    assert spec.shape in {"serial", "hierarchical", "mixed"}, f"{name} 形状未归类：{spec.shape}"
    assert spec.expected_task_ids, f"{name} 没有声明任何计划任务"
    assert FINAL_TASK_ID not in spec.expected_task_ids, "收尾任务由平台追加，不该出现在登记表里"
    assert spec.fixtures.is_dir(), f"{name} 夹具目录不存在：{spec.fixtures}"


@pytest.mark.parametrize("name", names())
def test_sample_fixtures_cover_every_task(name: str) -> None:
    """每个任务都要有 attempt-1 回放，含平台追加的 T-FINAL。"""
    spec = get(name)
    plan = json.loads((spec.fixtures / "plan_todo.json").read_text(encoding="utf-8"))
    planned = [task["id"] for task in plan["tasks"]]
    assert tuple(planned) == spec.expected_task_ids, (
        f"{name} 登记表与 plan_todo.json 的任务 id 不一致：\n"
        f"  登记表 {spec.expected_task_ids}\n  夹具   {tuple(planned)}"
    )

    reviews = json.loads((spec.fixtures / "reviews.json").read_text(encoding="utf-8"))
    for task_id in [*planned, FINAL_TASK_ID]:
        response = spec.fixtures / "responses" / task_id / "attempt-1.md"
        assert response.is_file(), f"{name} 缺少回放夹具：{response}"
        assert response.stat().st_size > 0, f"{name} 的回放夹具为空：{response}"
        assert task_id in reviews, f"{name} 的 reviews.json 缺少 {task_id}"


# ── 2. 编排形状 ───────────────────────────────────────────────────────────


def test_linear_sample_is_a_strict_chain() -> None:
    """简单串行：每层恰好一个任务，每个非首任务恰好一个依赖。"""
    spec = get("linear")
    deps = spec.dependency_map()

    assert all(len(parents) <= 1 for parents in deps.values()), "串行样例里出现了多依赖任务"
    layers = layers_of(deps)
    assert [len(bucket) for bucket in layers] == [1] * len(layers), (
        f"串行样例应为每层 1 个任务，实际 {layers}"
    )
    assert len(layers) == len(deps), "串行链的层数应等于任务数（每个任务独占一层）"
    assert max(fan_out(deps).values()) == 1, "串行样例不应有分叉点"


def test_hierarchy_sample_has_fork_and_join() -> None:
    """层级管理：必须同时有分叉（一个任务被多条路径依赖）与汇合（一个任务依赖多条）。"""
    spec = get("hierarchy")
    deps = spec.dependency_map()

    forks = [task_id for task_id, count in fan_out(deps).items() if count >= 2]
    joins = [task_id for task_id, parents in deps.items() if len(parents) >= 2]
    assert forks, "层级样例缺少分叉点（两条并行实现路径的起点）"
    assert joins, "层级样例缺少汇合点（用例任务应同时等两侧实现）"

    layers = layers_of(deps)
    assert max(len(bucket) for bucket in layers) >= 2, f"层级样例应有并行层，实际 {layers}"


def test_mixed_sample_has_concurrency() -> None:
    """原有 Todo 样例是混合并发：也要有分叉与汇合，作为三种形状里的对照。"""
    deps = get("todo").dependency_map()
    assert any(count >= 2 for count in fan_out(deps).values()), "混合样例缺少分叉点"
    assert any(len(parents) >= 2 for parents in deps.values()), "混合样例缺少汇合点"


def test_concurrency_grows_from_serial_to_hierarchy_to_mixed() -> None:
    """三种形状的**最大并行宽度**必须递增：1 → ≥2 → 更宽。

    层深不是区分点（三者都是 4 层），宽度才是：串行每层一个任务，
    层级有两路并行实现，混合并发同时开四条支线。用递增关系而不是写死数字，
    这样给样例加任务时不会因为"数字变了"而误报 —— 只有并行度真的退化才会红。
    """
    widths = {
        name: max(len(bucket) for bucket in layers_of(get(name).dependency_map()))
        for name in ("linear", "hierarchy", "todo")
    }
    assert widths["linear"] == 1, f"串行样例出现了并行：{widths}"
    assert widths["hierarchy"] >= 2, f"层级样例应当有并行层：{widths}"
    assert widths["todo"] > widths["hierarchy"], (
        f"混合并发应当比层级更宽：{widths}")


# ── 3. 端到端（每个样例跑一次）─────────────────────────────────────────────


@dataclass(frozen=True)
class SampleRun:
    """一次已完成的样例 run，以及断言它所需的材料。"""

    spec: SampleSpec
    record: object
    run_dir: Path
    tasks: tuple[dict, ...]

    @property
    def artifacts(self) -> Path:
        return self.run_dir / "artifacts"


@pytest.fixture(scope="module", params=names())
def sample_run(request: pytest.FixtureRequest, tmp_path_factory) -> SampleRun:
    """在完全离线的环境下跑一次样例（无 API Key、无 dotenv、独立输出目录）。"""
    spec = get(request.param)
    tmp = tmp_path_factory.mktemp(f"sample-{spec.name}")
    mp = pytest.MonkeyPatch()
    mp.setenv("AGENTCREW_MOCK", "true")
    mp.setenv("AGENTCREW_GATE", "none")
    mp.setenv("AGENTCREW_OUTPUT_DIR", str(tmp / "out"))
    mp.setenv("AGENTCREW_CONFIG", str(tmp / "absent-agentcrew.yaml"))
    for env_name in API_KEY_ENVS:
        mp.delenv(env_name, raising=False)
    mp.setattr("agentcrew.config.load_dotenv", lambda *a, **kw: False)

    import agentcrew.models.factory as model_factory

    mp.setattr(model_factory, "DEFAULT_FIXTURES_DIR", spec.fixtures)
    try:
        settings = Settings.load()
        record = asyncio.run(Crew(settings).run(spec.goal))
        run_dir = settings.output_dir / record.run_id
        payload = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))
        tasks = tuple(payload["tasks"] if isinstance(payload, dict) else payload)
        yield SampleRun(spec=spec, record=record, run_dir=run_dir, tasks=tasks)
    finally:
        mp.undo()


def test_sample_run_completes(sample_run: SampleRun) -> None:
    rec = sample_run.record
    assert rec.status is RunStatus.COMPLETED, f"{sample_run.spec.name} 未完成：{rec.status}"
    assert rec.stats.failed == 0, f"{sample_run.spec.name} 有失败任务"
    assert rec.stats.blocked == 0, f"{sample_run.spec.name} 有阻塞任务"
    assert rec.stats.total_tasks == sample_run.spec.expected_total_tasks
    assert rec.stats.succeeded == sample_run.spec.expected_total_tasks


def test_sample_run_keeps_declared_dependencies(sample_run: SampleRun) -> None:
    """跑完之后面板上的依赖必须与 plan 里声明的一致。

    T-FINAL 例外：它由平台追加，依赖是"所有没有后继的任务"，plan 里没有这条。
    """
    declared = sample_run.spec.dependency_map()
    actual = {task["id"]: tuple(task.get("dependencies") or ()) for task in sample_run.tasks}

    for task_id, parents in declared.items():
        assert actual[task_id] == parents, (
            f"{sample_run.spec.name} 的 {task_id} 依赖被改动："
            f"计划 {parents} → 实际 {actual[task_id]}"
        )
        assert "FAILED" not in str(task_id)

    leaves = {task_id for task_id, parents in declared.items() if not parents}
    assert leaves <= set(actual), "首任务不应在运行后消失"
    assert FINAL_TASK_ID in actual, "平台必须追加收尾任务"
    assert all(p == "succeeded" for p in
               (task["status"] for task in sample_run.tasks)), "存在非 succeeded 的任务"


def test_sample_artifacts_are_real_files(sample_run: SampleRun) -> None:
    """清单里的每个产物都要真的在盘上，且 sha256 对得上。"""
    manifest = json.loads(
        (sample_run.artifacts / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["entries"], f"{sample_run.spec.name} 一个产物都没落盘"
    for entry in manifest["entries"]:
        path = sample_run.artifacts / entry["path"]
        assert path.is_file(), f"清单声明了 {entry['path']} 但文件不存在"
        data = path.read_bytes()
        assert len(data) == entry["size_bytes"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], (
            f"{entry['path']} 内容与清单 sha256 不符")


def test_sample_python_artifacts_parse(sample_run: SampleRun) -> None:
    """生成物里每个 .py 都要能解析 —— 夹具写坏了在这里就该现形。"""
    parsed = 0
    for path in sample_run.artifacts.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parsed += 1
    assert parsed > 0, f"{sample_run.spec.name} 没有 .py 产物，上面的循环是空转"


# ── 4. CLI 面 ─────────────────────────────────────────────────────────────


def test_apply_sample_enables_mock_and_sets_goal() -> None:
    """``--sample`` 必须顺手打开 mock：样例是回放，用真实 provider 跑就成了别的项目。"""
    import main

    args = type("Args", (), {"sample": "linear", "mock": False})()
    goal, error = main._apply_sample(args)

    assert error is None
    assert args.mock is True, "--sample 未自动开启 mock"
    assert goal == SAMPLES["linear"].goal


def test_apply_sample_reports_unknown_name() -> None:
    """未登记的样例返回错误文案而不是抛异常（CLI 要转成退出码 3）。"""
    import main

    args = type("Args", (), {"sample": "nope", "mock": False})()
    goal, error = main._apply_sample(args)

    assert goal is None
    assert error is not None and "nope" in error
    assert "linear" in error, "错误信息应列出可选样例"


def test_samples_command_lists_all(capsys: pytest.CaptureFixture[str]) -> None:
    import main

    args = type("Args", (), {"name": None, "json": True})()
    assert main._cmd_samples(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [item["name"] for item in payload] == list(names())
    for item in payload:
        assert item["tasks"] == len(item["expected_task_ids"]) + 1
        assert Path(item["fixtures"]).is_dir()
