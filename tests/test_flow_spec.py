"""R26：可视化编排——流程 YAML 双向、图校验与流程库落盘。

四层：YAML 往返（含中文与契约字段）· 图校验（重复 id / 悬空依赖 / 自依赖 / 环 /
无入口）· 落盘（坏流程不写、名字越界拒绝）· HTTP 端点（校验不落盘 / 保存 / 读取 /
删除）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentcrew.api.server import ApiServer
from agentcrew.flow.spec import (
    FlowError,
    FlowSpec,
    delete_flow,
    dump_flow_yaml,
    list_flows,
    load_flow,
    parse_flow_yaml,
    sample_flow,
    save_flow,
    topological_order,
    validate_flow,
)
from agentcrew.task.planner import TaskDraft


def _draft(task_id: str, *, deps: list[str] | None = None,
           priority: int = 5) -> TaskDraft:
    return TaskDraft(
        id=task_id, title=task_id, description="d", role="backend_dev",
        acceptance_criteria=["ok"],
        deliverables=[{"path": f"docs/{task_id}.md", "kind": "markdown",
                       "description": "doc"}],
        dependencies=deps or [], priority=priority)


def test_sample_flow_is_valid_and_roundtrips():
    spec = sample_flow()
    assert validate_flow(spec) == []
    text = dump_flow_yaml(spec)
    assert "T-001" in text and "示例" in text     # 中文不转义
    back = parse_flow_yaml(text)
    assert [task.id for task in back.tasks] == [task.id for task in spec.tasks]
    assert back.tasks[0].deliverables[0].path == "docs/PRD.md"


def test_topological_order_respects_priority_and_deps():
    spec = FlowSpec(name="t", tasks=[
        _draft("C", deps=["A"], priority=1),
        _draft("A", priority=9),
        _draft("B", priority=5),
    ])
    order = topological_order(spec)
    assert order.index("A") < order.index("C")
    assert order[0] == "B"          # 无依赖里 priority 小的先（1..9 数字小=高优先）
    assert order == ["B", "A", "C"]


@pytest.mark.parametrize("spec,needle", [
    (FlowSpec(name="x", tasks=[]), "至少要有一个任务"),
    (FlowSpec(name="x", tasks=[_draft("A"), _draft("A")]), "id 重复"),
    (FlowSpec(name="x", tasks=[_draft("A", deps=["Z"])]), "不存在"),
    (FlowSpec(name="x", tasks=[_draft("A", deps=["A"])]), "依赖了自己"),
])
def test_graph_validation_reports_readable_errors(spec, needle):
    errors = validate_flow(spec)
    assert any(needle in error for error in errors)


def test_cycle_detection_mentions_path():
    spec = FlowSpec(name="x", tasks=[
        _draft("A", deps=["C"]), _draft("B", deps=["A"]), _draft("C", deps=["B"])])
    errors = validate_flow(spec)
    assert any("成环" in error for error in errors)
    assert "A" in next(error for error in errors if "成环" in error)


def test_flow_without_entry_is_rejected():
    spec = FlowSpec(name="x", tasks=[
        _draft("A", deps=["B"]), _draft("B", deps=["A"])])
    errors = validate_flow(spec)
    assert errors        # 环 + 无入口至少命中一条


def test_parse_rejects_bad_version_and_shape():
    with pytest.raises(FlowError, match="版本"):
        parse_flow_yaml("version: 99\nname: x\ntasks: []\n")
    with pytest.raises(FlowError, match="结构"):
        parse_flow_yaml("name: x\ntasks:\n- id: A\n")
    with pytest.raises(FlowError, match="空"):
        parse_flow_yaml("")


def test_save_load_list_delete(tmp_path):
    spec = sample_flow()
    path = save_flow(tmp_path, spec)
    assert path.name == "sample.yaml"
    items = list_flows(tmp_path)
    assert items[0]["name"] == "sample" and items[0]["tasks"] == 3
    assert load_flow(tmp_path, "sample").name == "sample"
    delete_flow(tmp_path, "sample")
    assert list_flows(tmp_path) == []
    with pytest.raises(FlowError, match="不存在"):
        load_flow(tmp_path, "sample")


def test_invalid_flow_is_not_saved(tmp_path):
    spec = FlowSpec(name="broken", tasks=[_draft("A", deps=["Z"])])
    with pytest.raises(FlowError, match="不合法"):
        save_flow(tmp_path, spec)
    assert list_flows(tmp_path) == []


@pytest.mark.parametrize("name", ["../escape", "带中文", "a" * 80, "", ".hidden"])
def test_flow_name_is_validated(tmp_path, name):
    spec = FlowSpec(name=name, tasks=[_draft("A")])
    with pytest.raises(FlowError, match="流程名"):
        save_flow(tmp_path, spec)


# ── HTTP ───────────────────────────────────────────────────────────────


async def _request(port: int, method: str, path: str,
                   body: dict | None = None) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = json.dumps(body).encode() if body is not None else b""
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
        + payload)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, asyncio.CancelledError):
        pass
    head, _, body_text = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    try:
        return status, json.loads(body_text.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return status, {}


@pytest.fixture
async def flow_server(tmp_path):
    srv = ApiServer(port=0, output_dir=tmp_path)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        yield port, tmp_path
    finally:
        await srv.stop()


async def test_api_validate_returns_view(flow_server):
    port, _ = flow_server
    yaml_text = dump_flow_yaml(sample_flow())
    status, data = await _request(port, "POST", "/flows/validate",
                                  {"yaml": yaml_text})
    assert status == 200 and data["ok"] is True
    view = data["view"]
    assert [node["id"] for node in view["nodes"]] == ["T-001", "T-002", "T-003"]
    assert {edge["to"] for edge in view["edges"]} == {"T-002", "T-003"}


async def test_api_validate_reports_errors_without_saving(flow_server):
    port, tmp = flow_server
    bad = "version: 1\nname: bad\ntasks:\n- id: A\n  title: a\n  description: d\n  role: backend_dev\n  acceptance_criteria: [x]\n  deliverables: []\n  dependencies: [Z]\n"
    status, data = await _request(port, "POST", "/flows/validate", {"yaml": bad})
    assert status == 200 and data["ok"] is False
    assert any("不存在" in error for error in data["errors"])
    assert list_flows(tmp) == []


async def test_api_save_get_delete_roundtrip(flow_server):
    port, tmp = flow_server
    yaml_text = dump_flow_yaml(sample_flow())
    status, data = await _request(port, "POST", "/flows", {"yaml": yaml_text})
    assert status == 201 and data["saved"] == "sample.yaml"
    assert (tmp / "_flows" / "sample.yaml").is_file()

    status, data = await _request(port, "GET", "/flows")
    assert status == 200 and data["flows"][0]["name"] == "sample"

    status, data = await _request(port, "GET", "/flows/sample")
    assert status == 200 and "T-001" in data["yaml"]
    assert len(data["view"]["nodes"]) == 3

    status, data = await _request(port, "DELETE", "/flows/sample")
    assert status == 200 and data["deleted"] == "sample"
    status, _ = await _request(port, "GET", "/flows/sample")
    assert status == 404


async def test_api_save_rejects_invalid_and_bad_body(flow_server):
    port, _ = flow_server
    status, data = await _request(port, "POST", "/flows", {"nope": 1})
    assert status == 400 and "yaml" in data["error"]
    status, data = await _request(
        port, "POST", "/flows",
        {"flow": {"name": "x", "tasks": [{"id": "A"}]}})
    assert status == 400
