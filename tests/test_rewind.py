"""R62：run 时间旅行（``rewind``）的回归用例。

覆盖四层：

1. **影响集**：依赖传递后代、``parent_id`` 动态子任务、未知 id；
2. **状态与盘面**：只重置目标子树、旧产物先快照后移除、``run.json`` 置回
   ``interrupted``、``run_rewound`` 审计事件；
3. **陷阱**：``tasks.delta.jsonl`` 未被清掉时，resume 会把回滚前的状态盖回来；
4. **端到端**：回滚后用既有的 ``resume`` 重跑，run 重新回到 ``completed``，
   产物、清单与任务契约都对得上。

全部离线（mock 夹具 + 无 API Key），单次 run 约 2 秒，按模块共享。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.crew.rewind import (
    REWIND_DIR,
    affected_task_ids,
    rewind_run,
)
from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.runtime.lifecycle import ResumeLock, ResumeLockError
from agentcrew.task.task import Task, TaskStatus
from agentcrew.task.task_board import TaskBoard

#: 与其它端到端套件同一份清单：断言「无密钥可跑」要逐个删干净。
API_KEY_ENVS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DASHSCOPE_API_KEY",
    "ARK_API_KEY",
    "QIANFAN_API_KEY",
    "DEEPSEEK_API_KEY",
)

#: ``linear`` 样例的任务：严格一条链，回滚 T-002 必然连带 T-003/T-004/T-FINAL。
LINEAR_GOAL = (
    "开发一个命令行日志统计工具 logstat：读取访问日志，输出状态码分布与耗时分位，"
    "附设计说明与使用文档。"
)


@dataclass(frozen=True)
class BaseRun:
    """模块级共享的基线 run（只读；每个用例各复制一份再回滚）。"""

    settings: Settings
    run_id: str
    run_dir: Path


@pytest.fixture(scope="module")
def base_run(tmp_path_factory) -> BaseRun:
    """跑一次离线 ``linear`` 样例，得到一份可复制的 completed run。"""
    from examples.samples import get

    spec = get("linear")
    tmp = tmp_path_factory.mktemp("rewind-base")
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
        assert record.status is RunStatus.COMPLETED
        yield BaseRun(
            settings=settings,
            run_id=record.run_id,
            run_dir=settings.output_dir / record.run_id,
        )
    finally:
        mp.undo()


@pytest.fixture()
def run_copy(base_run: BaseRun, tmp_path) -> tuple[Settings, Path]:
    """把基线 run 整目录复制到临时目录，返回 ``(settings, run_dir)``。"""
    dest_root = tmp_path / "out"
    dest = dest_root / base_run.run_id
    shutil.copytree(base_run.run_dir, dest)
    settings = base_run.settings.model_copy(update={"output_dir": dest_root})
    return settings, dest


def _tasks(run_dir: Path) -> dict[str, Task]:
    board = CheckpointStore(run_dir).load_board()
    return {task.id: task for task in board.all()}


def _manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "artifacts" / "MANIFEST.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── 1. 影响集 ─────────────────────────────────────────────────────────────


def test_affected_ids_covers_descendants_and_final() -> None:
    board = TaskBoard(
        [
            Task(
                id="A",
                title="a",
                description="a",
                role="coder",
                acceptance_criteria=["x"],
                deliverables=[],
            ),
            Task(
                id="B",
                title="b",
                description="b",
                role="coder",
                acceptance_criteria=["x"],
                deliverables=[],
                dependencies=["A"],
            ),
            Task(
                id="C",
                title="c",
                description="c",
                role="coder",
                acceptance_criteria=["x"],
                deliverables=[],
                dependencies=["B"],
            ),
        ]
    )
    assert affected_task_ids(board, "B") == ("B", "C")


def test_affected_ids_covers_dynamic_child_by_parent() -> None:
    """动态委派子任务只挂 ``parent_id``、不进依赖图，也必须一起重做。"""
    board = TaskBoard(
        [
            Task(
                id="P",
                title="p",
                description="p",
                role="project_manager",
                acceptance_criteria=["x"],
                deliverables=[],
            ),
            Task(
                id="P-CHILD",
                title="child",
                description="child",
                role="coder",
                acceptance_criteria=["x"],
                deliverables=[],
                parent_id="P",
            ),
            Task(
                id="P-GRAND",
                title="grand",
                description="grand",
                role="coder",
                acceptance_criteria=["x"],
                deliverables=[],
                parent_id="P-CHILD",
            ),
        ]
    )
    assert affected_task_ids(board, "P") == ("P", "P-CHILD", "P-GRAND")


def test_affected_ids_always_includes_final_task() -> None:
    """``T-FINAL`` 无条件纳入：即便它当前不是目标的依赖后代（防御性）。"""
    board = TaskBoard(
        [
            Task(
                id="A",
                title="a",
                description="a",
                role="coder",
                acceptance_criteria=["x"],
                deliverables=[],
            ),
            Task(
                id="T-FINAL",
                title="final",
                description="final",
                role="project_manager",
                acceptance_criteria=["x"],
                deliverables=[],
            ),
        ]
    )
    assert affected_task_ids(board, "A") == ("A", "T-FINAL")


def test_affected_ids_rejects_unknown_task() -> None:
    board = TaskBoard(
        [
            Task(
                id="A",
                title="a",
                description="a",
                role="coder",
                acceptance_criteria=["x"],
                deliverables=[],
            ),
        ]
    )
    with pytest.raises(ValueError, match="不在 run 的任务板上"):
        affected_task_ids(board, "NOPE")


# ── 2. 状态与盘面 ─────────────────────────────────────────────────────────


def test_rewind_resets_only_target_subtree(run_copy) -> None:
    settings, run_dir = run_copy
    result = rewind_run(run_dir, target="T-002", settings=settings)

    assert result.affected == ("T-002", "T-003", "T-004", "T-FINAL")
    tasks = _tasks(run_dir)
    assert tasks["T-001"].status is TaskStatus.SUCCEEDED, "上游已成功任务不该被重置"
    for task_id in result.affected:
        task = tasks[task_id]
        assert task.status is TaskStatus.PENDING, f"{task_id} 未重置：{task.status}"
        assert task.attempts == 0, f"{task_id} 的执行额度未清零"
        assert task.rework_count == 0
        assert task.result is None, f"{task_id} 的旧结果会让下游条件边误判"
        assert task.reviews == [], f"{task_id} 的旧评审不该跟到重做里"
        assert task.status_history[-1].reason == "rewind"


def test_rewind_snapshots_then_discards_artifacts(run_copy) -> None:
    settings, run_dir = run_copy
    before = _manifest(run_dir)
    result = rewind_run(run_dir, target="T-002", settings=settings)

    assert result.snapshot_dir is not None
    snapshot = Path(result.snapshot_dir)
    payload = json.loads((snapshot / "rewind.json").read_text(encoding="utf-8"))
    assert payload["target"] == "T-002"
    assert payload["affected"] == list(result.affected)
    assert payload["artifacts"], "回滚必须留下旧产物的快照条目"

    # 快照逐字节一致
    for entry in payload["artifacts"]:
        copy = snapshot / entry["snapshot"]
        assert copy.is_file(), f"快照缺文件：{entry['path']}"
        assert _sha256(copy) == entry["sha256"]

    # 旧产物已从 artifacts/ 与清单里移除，上游的仍在
    after = _manifest(run_dir)
    remaining = {entry["path"] for entry in after["entries"]}
    dropped = {entry["path"] for entry in before["entries"]} - remaining
    assert dropped, "受影响任务的产物没有从清单里移除"
    for path in dropped:
        assert not (run_dir / "artifacts" / path).exists(), f"{path} 仍留在 artifacts/"
    kept = remaining & {entry["path"] for entry in before["entries"]}
    assert kept, "上游产物不该被一并清掉"
    for path in kept:
        assert (run_dir / "artifacts" / path).is_file()


def test_rewind_marks_run_interrupted_and_audits(run_copy) -> None:
    settings, run_dir = run_copy
    result = rewind_run(run_dir, target="T-002", settings=settings)

    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == RunStatus.INTERRUPTED.value
    assert record["stats"]["succeeded"] == 1, "状态汇总应反映只剩上游成功"
    events = [
        json.loads(line)
        for line in (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rewound = [event for event in events if event["event"] == "run_rewound"]
    assert len(rewound) == 1
    data = rewound[0]["data"]
    assert data["target"] == "T-002"
    assert data["affected"] == list(result.affected)
    assert data["discarded_artifacts"] == result.discarded_artifacts
    assert data["snapshotted_artifacts"] == len(
        json.loads((Path(result.snapshot_dir) / "rewind.json").read_text(encoding="utf-8"))[
            "artifacts"
        ]
    )


def test_rewind_dry_run_writes_nothing(run_copy) -> None:
    settings, run_dir = run_copy

    def snapshot_tree(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    before = snapshot_tree(run_dir)
    result = rewind_run(run_dir, target="T-002", settings=settings, dry_run=True)

    assert result.dry_run
    assert result.snapshot_dir is None
    assert result.discarded_artifacts > 0, "dry-run 也要报出将被移除的产物数量"
    assert snapshot_tree(run_dir) == before, "dry-run 不得写盘（含快照目录）"
    assert not (run_dir / REWIND_DIR).exists()


# ── 3. 陷阱：增量 checkpoint ──────────────────────────────────────────────


def test_rewind_merges_stale_task_delta(run_copy) -> None:
    """``tasks.delta.jsonl`` 里的旧快照必须被合并清空，否则 resume 会盖回来。"""
    settings, run_dir = run_copy
    checkpoint = CheckpointStore(run_dir)
    board = checkpoint.load_board()
    stale = board.get("T-002").model_dump(mode="json")
    stale["status"] = TaskStatus.SUCCEEDED.value
    with open(run_dir / "tasks.delta.jsonl", "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"op": "update", "task_id": "T-002", "changes": stale},
                ensure_ascii=False,
            )
            + "\n"
        )

    rewind_run(run_dir, target="T-002", settings=settings)

    assert not (run_dir / "tasks.delta.jsonl").exists(), "增量文件应已被合并清空"
    assert _tasks(run_dir)["T-002"].status is TaskStatus.PENDING


# ── 4. 指令与端到端 ───────────────────────────────────────────────────────


def test_rewind_instruction_lands_in_task_contract(run_copy) -> None:
    settings, run_dir = run_copy
    instruction = "输出改为 Markdown 表格，并补一列 p95 耗时"
    rewind_run(run_dir, target="T-002", instruction=instruction, settings=settings)

    task = _tasks(run_dir)["T-002"]
    assert instruction in task.description
    assert "[回滚重做指令 " in task.description
    assert task.acceptance_criteria[-1] == f"补充要求：{instruction}"
    assert instruction not in _tasks(run_dir)["T-003"].description, "指令只落在目标任务上"


def test_rewind_then_resume_completes(run_copy) -> None:
    settings, run_dir = run_copy
    original_entry_count = len(_manifest(run_dir)["entries"])
    rewind_run(run_dir, target="T-002", settings=settings)

    crew = Crew.resume_from(settings, run_dir.name)
    record = asyncio.run(crew.resume())

    assert record.status is RunStatus.COMPLETED
    tasks = _tasks(run_dir)
    assert all(task.status is TaskStatus.SUCCEEDED for task in tasks.values())
    assert len(_manifest(run_dir)["entries"]) == original_entry_count, (
        "重跑后产物集合应与首次一致（被移除的产物都重新落盘）"
    )
    for entry in _manifest(run_dir)["entries"]:
        path = run_dir / "artifacts" / entry["path"]
        assert path.is_file(), f"清单声明了 {entry['path']} 但文件不存在"
        assert _sha256(path) == entry["sha256"]


# ── 5. 拒绝路径与 CLI 接线 ────────────────────────────────────────────────


def test_rewind_rejects_running_run(run_copy) -> None:
    _, run_dir = run_copy
    checkpoint = CheckpointStore(run_dir)
    record = checkpoint.load_record()
    record.status = RunStatus.RUNNING
    checkpoint.save_record(record)

    with pytest.raises(ValueError, match="正在运行"):
        rewind_run(run_dir, target="T-002")


def test_rewind_rejects_awaiting_approval(run_copy) -> None:
    _, run_dir = run_copy
    checkpoint = CheckpointStore(run_dir)
    record = checkpoint.load_record()
    record.status = RunStatus.AWAITING_APPROVAL
    checkpoint.save_record(record)

    with pytest.raises(ValueError, match="人工审批"):
        rewind_run(run_dir, target="T-002")


def test_rewind_refuses_when_another_process_holds_lock(run_copy) -> None:
    _, run_dir = run_copy
    lock = ResumeLock(run_dir)
    lock.acquire()
    try:
        with pytest.raises(ResumeLockError):
            rewind_run(run_dir, target="T-002")
    finally:
        lock.release()

    # 锁释放后可以正常回滚（顺带证明失败路径没有留下半截状态）
    settings, _ = run_copy
    result = rewind_run(run_dir, target="T-002", settings=settings)
    assert result.status_after == RunStatus.INTERRUPTED.value


def test_rewind_is_wired_into_the_cli() -> None:
    import main

    args = main.build_parser().parse_args(
        ["rewind", "r1", "--task", "T-002", "--dry-run", "--json"]
    )
    assert args.command == "rewind"
    assert args.task == "T-002"
    assert args.dry_run is True
    assert args.json is True
    assert "rewind" in main._HANDLERS


@pytest.mark.asyncio
async def test_api_rewind_endpoint(run_copy) -> None:
    """``POST /runs/{id}/rewind`` 与 CLI 走同一条实现，返回同一份形状。"""
    from agentcrew.api.server import ApiServer

    settings, run_dir = run_copy
    server = ApiServer(port=0, output_dir=settings.output_dir, settings=settings)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        path = f"/runs/{run_dir.name}/rewind"

        status, data = await _http_json(port, "POST", path, {"task": "T-002", "dry_run": True})
        assert status == 200
        assert data["dry_run"] is True
        assert data["affected"] == ["T-002", "T-003", "T-004", "T-FINAL"]
        assert not (run_dir / REWIND_DIR).exists(), "dry-run 不得落盘"

        status, data = await _http_json(
            port, "POST", path, {"task": "T-002", "instruction": "补一列 p95 耗时"}
        )
        assert status == 200
        assert data["status_after"] == RunStatus.INTERRUPTED.value
        assert data["snapshot_dir"]
        assert "补一列 p95 耗时" in _tasks(run_dir)["T-002"].description
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_api_rewind_rejects_missing_task(run_copy) -> None:
    from agentcrew.api.server import ApiServer

    settings, run_dir = run_copy
    server = ApiServer(port=0, output_dir=settings.output_dir, settings=settings)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _http_json(
            port, "POST", f"/runs/{run_dir.name}/rewind", {"instruction": "x"}
        )
        assert status == 400
        assert "task" in data["error"]
    finally:
        await server.stop()


async def _http_json(port: int, method: str, path: str, body: dict) -> tuple[int, dict]:
    """发一个裸 HTTP 请求并解析 JSON 响应（与 API 套件同一种最小实现）。"""
    payload = json.dumps(body).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = (
        f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(payload)}\r\n\r\n"
    )
    writer.write(request.encode() + payload)
    await writer.drain()
    raw = await reader.read(65536)
    writer.close()
    await writer.wait_closed()
    head, _, text = raw.decode("utf-8", errors="replace").partition("\r\n\r\n")
    status = int(head.split("\r\n")[0].split(" ")[1])
    return status, json.loads(text) if text else {}
