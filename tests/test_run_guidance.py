"""C10 切片一 + 切片二：run 中途指引（存储 / 上下文 / 提示词 / 装配 / HTTP）。

切片一对应 ``docs/RUN-GUIDANCE-SPEC-2026-11-14.md`` 的验收标准第 1、2、4 条；
切片二覆盖定向 / 过期 / 撤回 / 历史压缩的落盘语义与控制面错误码。
刻意不在这里跑完整 run：一条指引只该被一个任务吃掉这件事，用
``Crew._guidance_provider`` 的闭包直接钉——闭包就是引擎侧唯一的接线点，
比等三任务调度跑到「恰好 T-002 派发」更确定。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agentcrew.agents.prompting import render_pack, section
from agentcrew.api.server import ApiServer
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.crew.crew import Crew
from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.runtime.guidance import EMPTY_TEXT_ERROR, GuidanceStore
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskStatus
from agentcrew.task.task_board import TaskBoard
from agentcrew.utils.clock import utc_now

GUIDANCE_HEADER = "用户中途指引（优先于计划，与交付物契约冲突时先问清楚再改）"


# ── 夹具 ──────────────────────────────────────────────────────────────────


def mk_task(tid: str = "T-001", deps: tuple[str, ...] = ()) -> Task:
    return Task(
        id=tid, title=tid, description="d", role="backend_dev",
        acceptance_criteria=["a"], deliverables=[], dependencies=list(deps),
        status=TaskStatus.READY,
    )


def mk_bus(tmp_path, tasks: list[Task], **kwargs) -> tuple[ContextBus, Ledger]:
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="R-guidance")
    store = ArtifactStore(tmp_path / "art", run_id="R-guidance", ledger=ledger)
    return ContextBus("目标", TaskBoard(tasks), store, **kwargs), ledger


# ── 存储层 ────────────────────────────────────────────────────────────────


def test_append_and_pending_keep_disk_order(tmp_path):
    store = GuidanceStore(tmp_path)
    first = store.append("先做登录页")
    second = store.append("再补错误提示", author="张三")
    pending = store.pending(limit=5, max_chars=400)
    assert [item.id for item in pending] == [first.id, second.id]
    assert pending[1].author == "张三"


def test_consume_is_idempotent_and_at_most_once(tmp_path):
    store = GuidanceStore(tmp_path)
    item = store.append("把登录页改成绿色")
    assert store.consume([item.id]) == 1
    assert store.consume([item.id]) == 0          # 再调不再消费
    assert store.pending(limit=5, max_chars=400) == []
    merged = store.items()[0]
    assert merged.consumed_at is not None
    assert store.pending_count() == 0


def test_consume_ignores_unknown_ids(tmp_path):
    store = GuidanceStore(tmp_path)
    store.append("a")
    assert store.consume(["GD-does-not-exist"]) == 0


def test_empty_text_is_rejected(tmp_path):
    store = GuidanceStore(tmp_path)
    with pytest.raises(ValueError, match="不能为空"):
        store.append("   \n  ")
    assert EMPTY_TEXT_ERROR == "指引文本不能为空"


def test_secrets_are_redacted_before_disk(tmp_path):
    """指引会落进 run 目录，密钥必须在写盘前就被抹掉。"""
    store = GuidanceStore(tmp_path, secrets=["sk-live-super-secret"])
    item = store.append("把 sk-live-super-secret 换掉")
    assert "sk-live-super-secret" not in item.text
    assert "sk-live-super-secret" not in store.path.read_text(encoding="utf-8")
    assert "***" in item.text


def test_bad_lines_are_skipped_not_raised(tmp_path):
    path = tmp_path / "guidance.jsonl"
    path.write_text(
        "{not json\n"
        + json.dumps({"op": "add", "id": "GD-1", "ts": "t", "author": "",
                      "text": "可用的一条"}, ensure_ascii=False) + "\n"
        + json.dumps({"op": "add", "id": 42, "text": "坏 id"}) + "\n",
        encoding="utf-8",
    )
    items = GuidanceStore(tmp_path).items()
    assert [item.text for item in items] == ["可用的一条"]


def test_pending_truncates_and_flags(tmp_path):
    store = GuidanceStore(tmp_path)
    store.append("长" * 600)
    pending = store.pending(limit=5, max_chars=100)
    assert len(pending) == 1
    assert pending[0].truncated is True
    assert len(pending[0].text) <= 100
    assert pending[0].text.endswith("…")


def test_pending_respects_limit(tmp_path):
    store = GuidanceStore(tmp_path)
    for index in range(4):
        store.append(f"第 {index} 条")
    assert len(store.pending(limit=2, max_chars=400)) == 2


# ── 上下文包与提示词 ──────────────────────────────────────────────────────


def test_pack_guidance_is_empty_by_default(tmp_path):
    """关闭态：provider 为 None → 不建字段以外的任何差别。"""
    bus, _ = mk_bus(tmp_path, [mk_task()])
    pack = bus.build(mk_task())
    assert pack.guidance == []
    assert "guidance" in pack.model_dump()


def test_build_absorbs_guidance_from_provider(tmp_path):
    bus, _ = mk_bus(tmp_path, [mk_task()],
                    guidance_provider=lambda _task_id: ["改用绿色主题"])
    assert bus.build(mk_task()).guidance == ["改用绿色主题"]


def test_provider_exception_degrades_to_empty(tmp_path):
    def boom(_task_id: str) -> list[str]:
        raise RuntimeError("磁盘炸了")

    bus, _ = mk_bus(tmp_path, [mk_task()], guidance_provider=boom)
    pack = bus.build(mk_task())          # 不抛
    assert pack.guidance == []


def test_render_pack_closed_state_has_no_guidance_section(tmp_path):
    bus, _ = mk_bus(tmp_path, [mk_task()])
    text = render_pack(bus.build(mk_task()), ())
    assert GUIDANCE_HEADER not in text
    headers = [line for line in text.splitlines() if line.startswith("## ")]
    assert headers == [
        "## 项目目标", "## 本任务", "## 交付物契约（文件清单以此为准）"]


def test_render_pack_inserts_guidance_and_keeps_rest_byte_identical(tmp_path):
    """打开态只**多一段**：除插入点外逐字节不变，契约段仍在最后。"""
    task = mk_task()
    closed_bus, _ = mk_bus(tmp_path, [task])
    open_bus, _ = mk_bus(
        tmp_path, [task],
        guidance_provider=lambda _task_id: ["改用绿色主题"])
    closed = render_pack(closed_bus.build(task), ())
    opened = render_pack(open_bus.build(task), ())
    block = section(GUIDANCE_HEADER, "- 改用绿色主题")
    marker = "## 交付物契约"
    index = closed.index(marker)
    assert opened == closed[:index] + block + "\n\n" + closed[index:]
    assert opened.rstrip().endswith("（本任务无文件交付物）")


# ── 引擎侧接线：Crew._guidance_provider ───────────────────────────────────


def mk_fake_run(tmp_path, ledger):
    return SimpleNamespace(
        checkpoint=SimpleNamespace(run_dir=tmp_path), ledger=ledger)


def test_crew_provider_is_none_when_disabled(tmp_path):
    crew = Crew(Settings())
    assert crew._guidance_provider(mk_fake_run(tmp_path, Ledger(
        tmp_path / "l.jsonl", run_id="R"))) is None


def test_crew_provider_consumes_once_and_emits_events(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="R-guidance")
    settings = Settings(guidance_enabled=True,
                        max_guidance_items=2, max_guidance_chars=400)
    store = GuidanceStore(tmp_path, secrets=tuple(settings.secret_values()))
    store.append("先做登录页")
    store.append("再补错误提示")
    crew = Crew(settings)
    take = crew._guidance_provider(mk_fake_run(tmp_path, ledger))
    assert take is not None
    assert take("T-001") == ["先做登录页", "再补错误提示"]
    assert take("T-001") == []              # at-most-once：第二次没了

    injected = list(ledger.events("guidance_injected"))
    assert len(injected) == 1
    assert injected[0].data["count"] == 2
    assert len(injected[0].data["ids"]) == 2
    assert injected[0].data["chars"] == len("先做登录页") + len("再补错误提示")
    assert not list(ledger.events("guidance_truncated"))   # 没超长就不发


def test_crew_provider_emits_truncated_event(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="R-guidance")
    settings = Settings(guidance_enabled=True, max_guidance_chars=40)
    GuidanceStore(tmp_path).append("长" * 200)
    take = Crew(settings)._guidance_provider(mk_fake_run(tmp_path, ledger))
    assert take is not None
    texts = take("T-001")
    assert len(texts[0]) <= 40
    truncated = list(ledger.events("guidance_truncated"))
    assert len(truncated) == 1 and truncated[0].data["max_chars"] == 40


# ── HTTP 控制面 ───────────────────────────────────────────────────────────


async def _http(port: int, method: str, path: str, body: bytes | None = None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = body or b""
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
        + payload)
    await writer.drain()
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    status = int(raw.split(b"\r\n", 1)[0].decode().split()[1])
    text = await reader.read(65536)
    writer.close()
    with contextlib.suppress(ConnectionError, asyncio.CancelledError):
        await writer.wait_closed()
    return status, text.decode("utf-8", errors="replace")


@pytest.fixture
async def guidance_server(tmp_path):
    output = tmp_path / "output"
    CheckpointStore.create(output, project_goal="g",
                           config_snapshot={}, run_id="R-c10")
    settings = Settings(guidance_enabled=True, max_guidance_items=1)
    srv = ApiServer(port=0, output_dir=output, settings=settings)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        yield output, port
    finally:
        await srv.stop()


async def test_post_and_get_guidance_roundtrip(guidance_server):
    output, port = guidance_server
    status, text = await _http(
        port, "POST", "/runs/R-c10/guidance",
        json.dumps({"text": "把首页改成深色", "author": "ops"}).encode())
    assert status == 202, text
    payload = json.loads(text)
    assert payload["id"].startswith("GD-")
    assert payload["count"] == 1

    status, text = await _http(port, "GET", "/runs/R-c10/guidance")
    assert status == 200, text
    listing = json.loads(text)
    assert listing["pending"] == 1
    assert listing["items"][0]["text"] == "把首页改成深色"
    assert listing["items"][0]["consumed_at"] is None


async def test_post_rejects_empty_text(guidance_server):
    _, port = guidance_server
    status, text = await _http(
        port, "POST", "/runs/R-c10/guidance", b'{"text": "   "}')
    assert status == 400
    assert "不能为空" in text


async def test_post_unknown_run_is_404(guidance_server):
    _, port = guidance_server
    status, _ = await _http(
        port, "POST", "/runs/R-absent/guidance", b'{"text": "x"}')
    assert status == 404


async def test_post_terminal_run_is_409(guidance_server):
    output, port = guidance_server
    store = CheckpointStore(output / "R-c10")
    record = store.load_record().model_copy(update={"status": RunStatus.COMPLETED,
                                                    "updated_at": datetime.now()})
    store.save_record(record)
    status, text = await _http(
        port, "POST", "/runs/R-c10/guidance", b'{"text": "x"}')
    assert status == 409, text
    assert "completed" in text


async def test_post_429_when_pending_is_full(guidance_server):
    _, port = guidance_server
    body = json.dumps({"text": "第一条"}).encode()
    assert (await _http(port, "POST", "/runs/R-c10/guidance", body))[0] == 202
    status, text = await _http(
        port, "POST", "/runs/R-c10/guidance",
        json.dumps({"text": "第二条"}).encode())
    assert status == 429, text


# ── C10 切片二：定向 / 过期 / 撤回 / 压缩（存储层） ─────────────────────────


def test_targeted_item_only_matches_its_task(tmp_path):
    store = GuidanceStore(tmp_path)
    global_item = store.append("全局约束")
    targeted = store.append("只给 T-003", target="T-003")
    first = store.pending(limit=5, max_chars=400, task_id="T-001")
    assert [item.id for item in first] == [global_item.id]
    third = store.pending(limit=5, max_chars=400, task_id="T-003")
    assert [item.id for item in third] == [global_item.id, targeted.id]
    # 不过滤时看到全部（切片一的调用口径保持兼容）。
    assert len(store.pending(limit=5, max_chars=400)) == 2
    assert store.pending_count() == 2


def test_append_rejects_bad_or_past_deadline(tmp_path):
    store = GuidanceStore(tmp_path)
    with pytest.raises(ValueError, match="ISO"):
        store.append("x", expires_at="not-a-date")
    with pytest.raises(ValueError, match="晚于"):
        store.append(
            "x", expires_at=(utc_now() - timedelta(seconds=1)).isoformat())


def test_expiry_is_evaluated_at_read_time(tmp_path):
    """过期是读时判定：不重写盘、不占配额，但仍留在 items() 里可审计。"""
    store = GuidanceStore(tmp_path)
    deadline = (utc_now() + timedelta(minutes=5)).isoformat()
    item = store.append("五分钟后作废", expires_at=deadline)
    future = datetime.fromisoformat(deadline) + timedelta(seconds=1)
    assert store.pending(limit=5, max_chars=400, now=future) == []
    assert [hit.id for hit in store.expired(now=future)] == [item.id]
    assert item.status(future) == "expired"
    assert store.items()[0].expires_at == deadline
    # 当前时刻还没过期，仍能被正常消费。
    assert store.consume([item.id]) == 1


def test_withdraw_releases_quota_and_stays_auditable(tmp_path):
    store = GuidanceStore(tmp_path)
    item = store.append("撤回我")
    assert store.withdraw(item.id) is True
    assert store.withdraw(item.id) is False          # 幂等
    assert store.pending_count() == 0
    assert store.items()[0].status() == "withdrawn"
    assert store.items()[0].withdrawn_at is not None
    assert store.consume([item.id]) == 0


def test_withdraw_cannot_undo_consumed_or_expired(tmp_path):
    store = GuidanceStore(tmp_path)
    consumed = store.append("已经吃掉了")
    assert store.consume([consumed.id]) == 1
    assert store.withdraw(consumed.id) is False
    deadline = (utc_now() + timedelta(seconds=30)).isoformat()
    soon = store.append("马上过期", expires_at=deadline)
    assert store.withdraw(soon.id) is True          # 当前时刻仍可撤
    assert store.items()[1].status() == "withdrawn"


def test_compact_keeps_active_items_and_recent_history(tmp_path):
    store = GuidanceStore(tmp_path)
    items = [store.append(f"第 {index} 条") for index in range(30)]
    for item in items[:28]:
        store.consume([item.id])
    result = store.compact(keep_recent=3)
    assert result["before_lines"] > result["after_lines"] == 1
    assert result["kept"] == 5                    # 2 条活跃 + 3 条最近已消费
    assert result["removed"] == 25
    assert [item.text for item in store.pending(limit=10, max_chars=400)] == [
        "第 28 条", "第 29 条"]
    fresh = store.append("压缩后再写")
    assert store.consume([fresh.id]) == 1         # 快照之后仍可继续追加


def test_snapshot_preserves_slice_two_fields(tmp_path):
    store = GuidanceStore(tmp_path)
    deadline = (utc_now() + timedelta(hours=1)).isoformat()
    targeted = store.append("定向", target="T-9", expires_at=deadline)
    withdrawn = store.append("撤回")
    store.withdraw(withdrawn.id)
    store.compact()
    got = {item.id: item for item in store.items()}
    assert got[targeted.id].target == "T-9"
    assert got[targeted.id].expires_at == deadline
    assert got[withdrawn.id].status() == "withdrawn"


def test_compact_ignores_bad_snapshot_lines(tmp_path):
    path = tmp_path / "guidance.jsonl"
    path.write_text(
        json.dumps({"op": "snapshot", "items": "不是数组"}) + "\n"
        + json.dumps({"op": "add", "id": "GD-ok", "ts": "t",
                      "author": "", "text": "可用"}) + "\n",
        encoding="utf-8")
    assert [item.text for item in GuidanceStore(tmp_path).items()] == ["可用"]


# ── C10 切片二：引擎接线 ───────────────────────────────────────────────────


def test_crew_provider_respects_target_and_emits_expired(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="R-guidance-2")
    settings = Settings(guidance_enabled=True,
                        max_guidance_items=5, max_guidance_chars=400)
    store = GuidanceStore(tmp_path, secrets=tuple(settings.secret_values()))
    store.append("全局约束")
    store.append("只给 T-003", target="T-003")
    # append 拒绝「已经过期」的写入；真机上过期是时间流逝造成的，这里手写一个
    # 过去时刻的 add 行来复现那个状态（读时判定，不改盘）。
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "op": "add", "id": "GD-expired", "ts": "t", "author": "",
            "text": "过期", "expires_at": (
                utc_now() - timedelta(seconds=1)).isoformat(),
        }, ensure_ascii=False) + "\n")

    take = Crew(settings)._guidance_provider(mk_fake_run(tmp_path, ledger))
    assert take is not None
    assert take("T-001") == ["全局约束"]
    assert take("T-002") == []
    assert take("T-003") == ["只给 T-003"]

    expired = list(ledger.events("guidance_expired"))
    assert len(expired) == 1                    # 同一实例不重复补发
    assert expired[0].data["ids"] == ["GD-expired"]
    injected = list(ledger.events("guidance_injected"))
    assert injected[0].data["task_id"] == "T-001"
    assert injected[-1].data["targets"] == ["T-003"]


# ── C10 切片二：HTTP 控制面 ───────────────────────────────────────────────


@pytest.fixture
async def guidance_roomy_server(tmp_path):
    output = tmp_path / "output2"
    CheckpointStore.create(output, project_goal="g",
                           config_snapshot={}, run_id="R-c10b")
    settings = Settings(guidance_enabled=True, max_guidance_items=5)
    srv = ApiServer(port=0, output_dir=output, settings=settings)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        yield output, port
    finally:
        await srv.stop()


async def test_post_target_and_ttl_roundtrip(guidance_roomy_server):
    _, port = guidance_roomy_server
    status, text = await _http(
        port, "POST", "/runs/R-c10b/guidance",
        json.dumps({"text": "定向", "target": "T-003",
                    "expires_in_seconds": 600}).encode())
    assert status == 202, text
    payload = json.loads(text)
    assert payload["target"] == "T-003"
    assert payload["expires_at"] is not None

    status, text = await _http(port, "GET", "/runs/R-c10b/guidance")
    listing = json.loads(text)
    assert listing["pending"] == 1
    assert listing["items"][0]["status"] == "pending"
    assert listing["items"][0]["target"] == "T-003"
    assert listing["items"][0]["withdrawn_at"] is None


async def test_post_rejects_bad_expiry_fields(guidance_roomy_server):
    _, port = guidance_roomy_server
    for body in ({"text": "x", "expires_in_seconds": 0},
                 {"text": "x", "expires_at": "昨天"},
                 {"text": "x", "target": 42}):
        status, _ = await _http(
            port, "POST", "/runs/R-c10b/guidance",
            json.dumps(body).encode())
        assert status == 400


async def test_delete_withdraws_pending_guidance(guidance_roomy_server):
    _, port = guidance_roomy_server
    status, text = await _http(
        port, "POST", "/runs/R-c10b/guidance",
        json.dumps({"text": "撤回我"}).encode())
    assert status == 202, text
    item_id = json.loads(text)["id"]
    status, text = await _http(
        port, "DELETE", f"/runs/R-c10b/guidance/{item_id}")
    assert status == 200, text
    assert json.loads(text)["pending"] == 0
    status, text = await _http(port, "GET", "/runs/R-c10b/guidance")
    listing = json.loads(text)
    assert listing["pending"] == 0
    assert listing["items"][0]["status"] == "withdrawn"


async def test_delete_unknown_guidance_is_404(guidance_roomy_server):
    _, port = guidance_roomy_server
    status, text = await _http(
        port, "DELETE", "/runs/R-c10b/guidance/GD-nope")
    assert status == 404
    assert "Not Found" not in text


async def test_compact_requires_terminal_run(guidance_roomy_server):
    output, port = guidance_roomy_server
    status, _ = await _http(
        port, "POST", "/runs/R-c10b/guidance/compact")
    assert status == 409                        # running：拒绝物理压缩
    store = CheckpointStore(output / "R-c10b")
    record = store.load_record().model_copy(update={
        "status": RunStatus.COMPLETED, "updated_at": datetime.now()})
    store.save_record(record)
    status, text = await _http(
        port, "POST", "/runs/R-c10b/guidance/compact")
    assert status == 200, text
    result = json.loads(text)
    assert result["run_id"] == "R-c10b"
    assert result["after_lines"] <= 1
