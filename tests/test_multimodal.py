"""C4：多模态输入——图片从入口到 provider 请求的完整链路。

四层验证：

1. **Provider 序列化**：``supports_vision`` 打开时 OpenAI 兼容端点产出
   ``image_url`` 内容块、Anthropic 产出 ``image`` source 块；关闭时带图消息
   **与纯文本路径逐字节一致**（图片被丢弃，不留半截结构）；
2. **入口校验**：``validate_images``（数量 / MIME / base64 合法性 / 拒 URL）；
3. **Agent 层**：有图 + 有能力 → 挂到 user 消息；有图 + 无能力 → 不挂且
   ``vision_unsupported`` 事件恰好一条（降级可见，不重复刷屏）；
4. **Crew 落盘**：图片写入 ``<run_dir>/context/images/`` 并记 ``images_attached``。
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from agentcrew.api.run_options import validate_images
from agentcrew.models.config import ModelBinding, ProviderCapabilities
from agentcrew.models.providers.anthropic import AnthropicProvider
from agentcrew.models.providers.base import Message
from agentcrew.models.providers.openai_compat import OpenAICompatProvider

_PNG_B64 = base64.b64encode(b"\x89PNG-fake").decode()


def _binding(vision: bool) -> ModelBinding:
    return ModelBinding(
        alias="test:model", provider="test", model="model-x",
        implementation="openai_compat", base_url="http://127.0.0.1:1",
        api_key_env="X", capabilities=ProviderCapabilities(
            supports_vision=vision,
        ),
    )


_IMG = {"mime": "image/png", "data": _PNG_B64}
_MSG: Message = {"role": "user", "content": "看这张图", "images": [_IMG]}
_TEXT_MSG: Message = {"role": "user", "content": "看这张图"}


# ── Provider 序列化 ────────────────────────────────────────────────────

def test_openai_vision_on_produces_image_url_parts():
    provider = OpenAICompatProvider(_binding(True), api_key="k", client=object())
    prepared = provider._prepare_messages([_MSG])
    content = prepared[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看这张图"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_vision_off_matches_text_only_path():
    """能力位关闭时，带图消息的 payload 必须与纯文本消息**完全一致**。"""
    no_image_provider = OpenAICompatProvider(
        _binding(False), api_key="k", client=object())
    with_image_provider = OpenAICompatProvider(
        _binding(False), api_key="k", client=object())
    assert no_image_provider._prepare_messages([_TEXT_MSG]) \
        == with_image_provider._prepare_messages([_MSG])


def test_anthropic_vision_on_produces_image_source_blocks():
    provider = AnthropicProvider(_binding(True), api_key="k", client=object())
    _, chat = provider._split_system([_MSG])
    content = chat[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看这张图"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/png"


def test_anthropic_vision_off_matches_text_only_path():
    no_image_provider = AnthropicProvider(
        _binding(False), api_key="k", client=object())
    with_image_provider = AnthropicProvider(
        _binding(False), api_key="k", client=object())
    assert no_image_provider._split_system([_TEXT_MSG]) \
        == with_image_provider._split_system([_MSG])


# ── 入口校验 ───────────────────────────────────────────────────────────

def test_validate_images_accepts_inline_base64():
    normalized = validate_images([{"data": _PNG_B64, "mime": "image/png"}])
    assert normalized == [{"mime": "image/png", "data": _PNG_B64}]


def test_validate_images_rejects_url_form():
    try:
        validate_images([{"url": "http://example.com/a.png"}])
    except ValueError as exc:
        assert "url" in str(exc)
    else:
        raise AssertionError("URL 形式必须被拒绝（SSRF 面）")


def test_validate_images_rejects_bad_mime_and_bad_base64():
    for bad in ({"data": _PNG_B64, "mime": "image/bmp"},
                {"data": "not-base64!!", "mime": "image/png"},
                {"mime": "image/png"}):
        try:
            validate_images([bad])
        except ValueError:
            continue
        raise AssertionError(f"应拒绝：{bad}")


def test_validate_images_caps_count():
    try:
        validate_images([{"data": _PNG_B64}] * 5)
    except ValueError as exc:
        assert "4" in str(exc)
    else:
        raise AssertionError("超过 4 张必须被拒绝")


# ── Agent 层：降级可见 ─────────────────────────────────────────────────

class _FakeModels:
    def __init__(self, vision: bool) -> None:
        self._vision = vision

    def supports_vision(self, alias: str) -> bool:
        return self._vision

    def alias_for_task(self, task) -> str:  # noqa: ANN001
        return "test:model"


def _make_agent(tmp_path, vision: bool):
    from agentcrew.agents.project_manager import ProjectManagerAgent
    from agentcrew.artifacts.store import ArtifactStore
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger)
    agent = ProjectManagerAgent(
        spec=ProjectManagerAgent.spec, models=_FakeModels(vision),
        store=store, ledger=ledger, project_goal="g",
    )
    agent._images = [_IMG]
    return agent, ledger, store


async def test_agent_attaches_images_when_vision_supported(tmp_path):
    agent, ledger, store = _make_agent(tmp_path, vision=True)
    task = _dummy_task()
    pack = _dummy_pack(task)
    messages = agent.build_messages(task, pack, [])
    assert "images" in messages[-1]
    assert messages[-1]["images"] == [_IMG]
    assert ledger.count("vision_unsupported") == 0


async def test_agent_drops_images_with_visible_event_when_no_vision(tmp_path):
    agent, ledger, store = _make_agent(tmp_path, vision=False)
    task = _dummy_task()
    pack = _dummy_pack(task)
    messages = agent.build_messages(task, pack, [])
    assert "images" not in messages[-1]
    # 与无图 Agent 的输出逐字节一致：图片被丢干净，不留半截结构
    plain, _, _ = _make_agent(tmp_path, vision=False)
    plain._images = []
    assert messages[-1]["content"] == plain.build_messages(task, pack, [])[-1]["content"]
    # 两次调用只记一条：降级提示不该刷屏
    agent.build_messages(task, pack, [])
    events = list(ledger.events("vision_unsupported"))
    assert len(events) == 1
    assert events[0].data["images"] == 1


def _dummy_task():
    from agentcrew.artifacts.spec import ArtifactSpec
    from agentcrew.task.task import Task

    return Task(
        id="T-1", title="t", description="d", role="project_manager",
        acceptance_criteria=["a"],
        deliverables=[ArtifactSpec(path="docs/PRD.md", kind="markdown",
                                   description="d")],
    )


def _dummy_pack(task):
    from agentcrew.context.context_bus import ContextPack

    return ContextPack(project_goal="g", task=task)


# ── Crew 落盘 ──────────────────────────────────────────────────────────

def test_persist_images_writes_files_and_ledger(tmp_path):
    from agentcrew.crew.crew import Crew
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    crew = Crew.__new__(Crew)  # 只测落盘逻辑，不装配整套引擎
    crew._images = [_IMG]
    crew._persist_images(tmp_path, ledger)
    saved = tmp_path / "context" / "images" / "00.png"
    assert saved.read_bytes() == b"\x89PNG-fake"
    event = list(ledger.events("images_attached"))[-1]
    assert event.data["count"] == 1
    assert event.data["files"] == ["00.png"]


def test_persist_images_noop_when_empty(tmp_path):
    from agentcrew.crew.crew import Crew
    from agentcrew.runtime.ledger import Ledger

    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    crew = Crew.__new__(Crew)
    crew._images = []
    crew._persist_images(tmp_path, ledger)
    assert ledger.count("images_attached") == 0
    assert not (tmp_path / "context").exists()


# ── API 入口：校验与透传 ────────────────────────────────────────────────

async def test_api_rejects_invalid_images(tmp_path):
    import asyncio

    from agentcrew.api.server import ApiServer

    srv = ApiServer(port=0, output_dir=tmp_path,
                    settings=SimpleNamespace(), crew_factory=lambda *a: _Crew())
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        body = json.dumps({"goal": "x",
                           "images": [{"data": "not-base64!!"}]}).encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"POST /runs HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body)
        await writer.drain()
        raw = await reader.read(65536)
        writer.close()
        status = int(raw.decode().split("\r\n")[0].split()[1])
        assert status == 400
        assert "images" in raw.decode()
    finally:
        await srv.stop()


async def test_api_passes_images_to_crew_factory(tmp_path):
    import asyncio

    from agentcrew.api.server import ApiServer

    captured: dict = {}

    class _Crew:
        run_id = "20261112-100000-ab12"
        cancelled = False
        _on_event = None
        goal = ""

        async def run(self, goal: str) -> None:
            self.goal = goal
            if self._on_event is not None:
                self._on_event("run_started",
                               {"run_id": self.run_id, "goal": goal})
            await asyncio.sleep(0)
            if self._on_event is not None:
                self._on_event("run_finished",
                               {"run_id": self.run_id, "status": "completed"})

    def factory(goal, options, images=None):
        captured["images"] = images
        return _Crew()

    srv = ApiServer(port=0, output_dir=tmp_path, crew_factory=factory)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        body = json.dumps({"goal": "x",
                           "images": [{"data": _PNG_B64,
                                       "mime": "image/png"}]}).encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"POST /runs HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body)
        await writer.drain()
        raw = await reader.read(65536)
        writer.close()
        assert raw.decode().split("\r\n")[0].split()[1] == "202"
        assert captured["images"] == [{"mime": "image/png", "data": _PNG_B64}]
    finally:
        await srv.stop()


class _Crew:
    """占位：仅供上面第一个用例的 factory 签名兼容。"""

    async def run(self, goal: str) -> None:  # pragma: no cover
        raise AssertionError("不应真正执行")
