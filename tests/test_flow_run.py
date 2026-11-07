"""R28：从画布起跑——流程直通规划（跳过 PM），仍走同一套校验/评分/留痕。

为什么值得单独钉：``preset_tasks`` 一旦被误当成「额外提示」交给 PM，用户画好的
流程就会被模型重拆一遍——画布的意义就没了。这里用「PM 的 decompose 一被调用就
炸」的桩把这条边界钉死；另一个方向是坏流程必须**开跑前**失败，不能带着坏图跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew, PlanFailedError
from agentcrew.flow.spec import FlowSpec, dump_flow_yaml, sample_flow, save_flow
from agentcrew.runtime.checkpoint import RunStatus
from agentcrew.task.planner import TaskDraft

FIXTURES = Path(__file__).resolve().parents[1] / "examples" / "fixtures"


def _response(lang: str, path: str, body: str) -> str:
    return f"```{lang} file={path}\n{body}```\n"


def _review(count: int) -> dict:
    return {
        "judgements": [
            {"criterion_index": i, "satisfied": True, "evidence": "ok"}
            for i in range(count)
        ],
        "suggestions": [],
        "score": 0.9,
    }


PRD_BODY = """# PRD

## 范围
只做 Todo 的增删改查。

## 验收
- 可以新增待办
- 可以删除待办
"""
MAIN_BODY = "def main():\n    print(\"hello\")\n"
DELIVERY_BODY = (
    "# 交付总结\n\n## 完成\n"
    "- T-001 产品需求文档已交付并通过两层门禁验收\n"
    "- T-002 后端入口已交付并通过两层门禁验收\n"
    "- 全部 3 个任务均无遗留阻塞\n\n"
    "## 运行\n"
    "python main.py\n"
)


def _drafts() -> list[TaskDraft]:
    return [
        TaskDraft(
            id="T-001", title="需求文档", role="project_manager",
            description="输出 PRD",
            acceptance_criteria=["含范围", "含验收"],
            deliverables=[{"path": "docs/PRD.md", "kind": "markdown",
                           "description": "PRD", "must_contain": ["验收"]}],
            priority=9,
        ),
        TaskDraft(
            id="T-002", title="后端实现", role="backend_dev",
            description="按 PRD 实现入口",
            acceptance_criteria=["可调用"],
            deliverables=[{"path": "backend/app/main.py", "kind": "python",
                           "description": "入口", "must_contain": ["def main"]}],
            dependencies=["T-001"], priority=6,
        ),
    ]


@pytest.fixture
def fx(tmp_path, monkeypatch):
    """两任务 + T-FINAL 的离线夹具（自建，与流程草案的契约逐字对齐）。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(tmp_path / "absent.yaml"))
    directory = tmp_path / "fx"
    directory.mkdir()
    (directory / "plan_todo.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8")   # preset 模式下不会被读
    (directory / "reviews.json").write_text(json.dumps({
        "T-001": _review(2), "T-002": _review(1), "T-FINAL": _review(1),
    }), encoding="utf-8")
    for task_id, path, body in [
        ("T-001", "docs/PRD.md", PRD_BODY),
        ("T-002", "backend/app/main.py", MAIN_BODY),
        ("T-FINAL", "DELIVERY.md", DELIVERY_BODY),
    ]:
        target = directory / "responses" / task_id
        target.mkdir(parents=True)
        suffix = path.rsplit(".", 1)[-1]
        (target / "attempt-1.md").write_text(
            _response("markdown" if suffix == "md" else suffix, path, body),
            encoding="utf-8")
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", directory)
    return {"tmp": tmp_path, "drafts": _drafts()}


def _ledger_events(output_dir: Path, run_id: str) -> list[dict]:
    path = output_dir / run_id / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines() if line.strip()]


async def test_preset_flow_skips_pm_and_completes(fx, monkeypatch):
    """流程直通：PM 的 decompose 一次都不许调；run 照常完成。"""
    from agentcrew.agents.project_manager import ProjectManagerAgent

    async def _boom(self, *args, **kwargs):
        raise AssertionError("preset 模式下不应调用 PM 规划")

    monkeypatch.setattr(ProjectManagerAgent, "decompose", _boom)

    settings = Settings.load()
    crew = Crew(settings, preset_tasks=fx["drafts"])
    record = await crew.run("做一个 Todo 应用")

    assert record.status is RunStatus.COMPLETED
    events = _ledger_events(settings.output_dir, record.run_id)
    names = [event.get("event") for event in events]
    assert "flow_plan_loaded" in names
    created = next(event for event in events if event.get("event") == "plan_created")
    assert created["data"]["source"] == "flow"
    assert created["data"]["planner_attempts"] == 0
    assert created["data"]["task_count"] == 3   # 2 个流程任务 + T-FINAL
    assert "plan_scored" in names


async def test_preset_flow_validation_error_fails_before_execution(fx):
    """坏流程（依赖不存在）在规划阶段就失败，且标明来源是 flow。"""
    draft = fx["drafts"][0].model_copy(update={"dependencies": ["T-999"]})
    settings = Settings.load()
    crew = Crew(settings, preset_tasks=[draft])
    with pytest.raises(PlanFailedError):
        await crew.run("坏流程")
    runs = [path for path in settings.output_dir.iterdir() if path.is_dir()]
    assert len(runs) == 1
    events = _ledger_events(settings.output_dir, runs[0].name)
    rejected = next(event for event in events
                    if event.get("event") == "plan_rejected")
    assert rejected["data"]["source"] == "flow"
    assert any("不在本次拆解中" in error for error in rejected["data"]["errors"])


def test_validate_run_options_flow_guards():
    """flow / flow_name 的类型与互斥在入口就挡住（不是等到建 Crew 才炸）。"""
    from agentcrew.api.server import _validate_run_options

    assert _validate_run_options({"flow": {"tasks": []}}) is None
    assert _validate_run_options({"flow_name": "demo"}) is None
    both = _validate_run_options({"flow": {}, "flow_name": "demo"})
    assert both is not None and "只能给一个" in both
    wrong_type = _validate_run_options({"flow": "文本"})
    assert wrong_type is not None and "必须是对象" in wrong_type
    wrong_name = _validate_run_options({"flow_name": 123})
    assert wrong_name is not None and "必须是字符串" in wrong_name


def test_load_flow_definition_accepts_file_and_name(tmp_path):
    """CLI 的 --flow 既认 .yaml 文件，也认流程库里的名字。"""
    import main as cli

    settings = Settings.load(output_dir=tmp_path)
    spec = sample_flow()
    save_flow(tmp_path, spec)
    by_name = cli._load_flow_definition(settings, "sample")
    assert by_name.name == "sample" and len(by_name.tasks) == 3

    path = tmp_path / "inline.yaml"
    path.write_text(dump_flow_yaml(spec), encoding="utf-8")
    by_file = cli._load_flow_definition(settings, str(path))
    assert [task.id for task in by_file.tasks] == [task.id for task in spec.tasks]

    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nname: bad\ntasks: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不合法"):
        cli._load_flow_definition(settings, str(bad))
    with pytest.raises(ValueError):
        cli._load_flow_definition(settings, "no-such-flow")


def test_flow_spec_roundtrip_through_options_payload():
    """画布提交的 flow 对象就是 FlowSpec 的 model_dump——两边不漂移。"""
    spec = sample_flow()
    payload = spec.model_dump(mode="json")
    assert FlowSpec(**payload).model_dump(mode="json") == payload


async def test_api_accepts_inline_flow_and_starts_run(fx):
    """画布「起跑」走的通道：POST /runs + options.flow（内联流程）。"""
    import asyncio

    from agentcrew.api.server import ApiServer

    settings = Settings.load()
    srv = ApiServer(port=0, output_dir=settings.output_dir, settings=settings)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        payload = json.dumps({
            "goal": "画布起跑",
            "options": {"flow": FlowSpec(
                name="canvas", tasks=_drafts()).model_dump(mode="json")},
        }).encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"POST /runs HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), timeout=15)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, asyncio.CancelledError):
            pass
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b"\r\n", 1)[0].split()[1])
        data = json.loads(body.decode("utf-8") or "{}")
        assert status == 202 and data.get("run_id")
    finally:
        await srv.stop()


async def test_api_rejects_invalid_flow_with_400(fx):
    """坏流程在入口 400：不建 run、不带病起跑。"""
    import asyncio

    from agentcrew.api.server import ApiServer

    settings = Settings.load()
    srv = ApiServer(port=0, output_dir=settings.output_dir, settings=settings)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        payload = json.dumps({
            "goal": "坏流程",
            "options": {"flow": {"version": 1, "name": "x",
                                 "tasks": [{"id": "A"}]}},
        }).encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"POST /runs HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), timeout=15)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, asyncio.CancelledError):
            pass
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b"\r\n", 1)[0].split()[1])
        data = json.loads(body.decode("utf-8") or "{}")
        assert status == 400 and "无法应用" in data.get("error", "")
    finally:
        await srv.stop()
