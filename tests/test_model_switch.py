"""P1-6：运行时切换大模型选项的测试。

覆盖三层：

- :class:`ModelFactory` 的 ``set_default_alias`` / ``set_role_binding`` /
  ``current_bindings``（含缓存清空、ledger 留痕、非法别名拒入）；
- :meth:`Crew.switch_model`（含 config_snapshot 回写、角色绑定分支）；
- HTTP ``GET/PUT /runs/{id}/model``（mock crew）与 CLI 菜单 ``[m]`` 选项。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agentcrew.api.server import ApiServer
from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.gate.cli_prompter import CliPrompter
from agentcrew.models.factory import ModelFactory, ModelFactoryError
from agentcrew.runtime.checkpoint import CheckpointStore
from agentcrew.runtime.ledger import Ledger


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()


# ── Factory ────────────────────────────────────────────────────────────────


class TestFactorySwitch:
    def test_set_default_alias_updates_bindings(self, settings):
        factory = ModelFactory(settings)
        before = factory.current_bindings()
        factory.set_default_alias("anthropic:claude-3-5-haiku-20241022")
        after = factory.current_bindings()
        assert before["default"] != "anthropic:claude-3-5-haiku-20241022"
        assert after["default"] == "anthropic:claude-3-5-haiku-20241022"
        assert after["role_bindings"] == {}

    def test_set_role_binding(self, settings):
        factory = ModelFactory(settings)
        factory.set_role_binding("planner", "anthropic:claude-3-5-haiku-20241022")
        bindings = factory.current_bindings()
        assert bindings["role_bindings"]["planner"] == (
            "anthropic:claude-3-5-haiku-20241022")
        # 默认模型不受影响
        assert bindings["default"] == settings.default_model

    def test_same_alias_is_noop(self, settings):
        factory = ModelFactory(settings)
        factory.set_default_alias(settings.default_model)
        assert factory.current_bindings()["default"] == settings.default_model

    def test_invalid_alias_rejected(self, settings):
        factory = ModelFactory(settings)
        with pytest.raises(ModelFactoryError):
            factory.set_default_alias("not-a-real-alias::nope")

    def test_cache_cleared_after_switch(self, settings):
        factory = ModelFactory(settings)
        factory.get("openai:gpt-4o-mini")
        assert len(factory._cache) >= 1
        factory.set_default_alias("anthropic:claude-3-5-haiku-20241022")
        assert factory._cache == {}

    def test_ledger_records_switch(self, settings, tmp_path):
        store = CheckpointStore.create(
            tmp_path, project_goal="g", config_snapshot={})
        record = store.load_record()
        ledger = Ledger(
            store.run_dir / "ledger.jsonl",
            run_id=record.run_id, secrets=())
        factory = ModelFactory(settings, ledger=ledger)
        factory.set_default_alias("anthropic:claude-3-5-haiku-20241022")
        factory.set_role_binding("planner", "openai:gpt-4o-mini")
        ledger.close()
        switched = [e for e in ledger.events() if e.event == "llm_model_switched"]
        assert len(switched) == 2
        assert switched[0].data["to_alias"] == "anthropic:claude-3-5-haiku-20241022"
        assert switched[0].data["scope"] == "default"
        assert switched[1].data["to_alias"] == "openai:gpt-4o-mini"
        assert switched[1].data["scope"] == "role:planner"


# ── Crew.switch_model ──────────────────────────────────────────────────────


def _wire_minimal_run(tmp_path, settings) -> tuple[Crew, Ledger]:
    """搭一个挂好 ``_run`` 的 Crew（不真跑），返回 (crew, ledger)。"""
    crew = Crew(settings)
    store = CheckpointStore.create(
        tmp_path, project_goal="g", config_snapshot={})
    record = store.load_record()
    ledger = Ledger(
        store.run_dir / "ledger.jsonl",
        run_id=record.run_id, secrets=())
    models = ModelFactory(settings, ledger=ledger)
    crew._run = SimpleNamespace(
        models=models, record=record, checkpoint=store, ledger=ledger,
    )
    return crew, ledger


class TestCrewSwitchModel:
    def test_switch_default(self, tmp_path, settings):
        crew, ledger = _wire_minimal_run(tmp_path, settings)
        bindings = crew.switch_model("openai:gpt-4o-mini")
        assert bindings["default"] == "openai:gpt-4o-mini"
        assert crew._settings.default_model == "openai:gpt-4o-mini"
        # config_snapshot 回写盘上 run.json
        on_disk = crew._run.checkpoint.load_record()
        assert on_disk.config_snapshot["default_model"] == "openai:gpt-4o-mini"
        ledger.close()

    def test_switch_role(self, tmp_path, settings):
        crew, ledger = _wire_minimal_run(tmp_path, settings)
        bindings = crew.switch_model(
            "anthropic:claude-3-5-haiku-20241022", role="planner")
        assert bindings["role_bindings"]["planner"] == (
            "anthropic:claude-3-5-haiku-20241022")
        ledger.close()

    def test_switch_without_run_raises(self, settings):
        crew = Crew(settings)
        with pytest.raises(RuntimeError):
            crew.switch_model("openai:gpt-4o-mini")


# ── HTTP API ───────────────────────────────────────────────────────────────


class _ModelCrew:
    """P1-6 测试用 crew 替身：带 switch_model / current_model_bindings。"""

    def __init__(self, run_id="model-run-1") -> None:
        self.run_id = run_id
        self.calls: list[tuple[str, str | None]] = []
        self._on_event = None

    def current_model_bindings(self):
        return {"default": "openai:gpt-4o-mini", "role_bindings": {}}

    def switch_model(self, alias, *, role=None):
        self.calls.append((alias, role))
        return {"default": alias, "role_bindings": {}}


async def _req(port, method, path, body=None):
    payload = json.dumps(body).encode() if body is not None else b""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if body is not None:
        request += f"Content-Length: {len(payload)}\r\n"
    request += "\r\n"
    writer.write(request.encode() + payload)
    await writer.drain()
    raw = await reader.read(65536)
    writer.close()
    await writer.wait_closed()
    head, _, text = raw.decode(errors="replace").partition("\r\n\r\n")
    status = int(head.split("\r\n")[0].split(" ")[1])
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, {}


class TestModelApi:
    @pytest.fixture
    async def server(self, tmp_path):
        srv = ApiServer(port=0, output_dir=tmp_path)
        await srv.start()
        try:
            yield srv
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_get_model(self, server):
        crew = _ModelCrew()
        server.active_crews["model-run-1"] = crew
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _req(port, "GET", "/runs/model-run-1/model")
        assert status == 200
        assert data["default"] == "openai:gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_put_model(self, server):
        crew = _ModelCrew()
        server.active_crews["model-run-1"] = crew
        port = server._server.sockets[0].getsockname()[1]
        status, data = await _req(
            port, "PUT", "/runs/model-run-1/model",
            {"alias": "anthropic:claude-3-5-haiku-20241022"})
        assert status == 200
        assert data["default"] == "anthropic:claude-3-5-haiku-20241022"
        assert crew.calls == [("anthropic:claude-3-5-haiku-20241022", None)]

    @pytest.mark.asyncio
    async def test_put_model_with_role(self, server):
        crew = _ModelCrew()
        server.active_crews["model-run-1"] = crew
        port = server._server.sockets[0].getsockname()[1]
        status, _ = await _req(
            port, "PUT", "/runs/model-run-1/model",
            {"alias": "openai:gpt-4o-mini", "role": "planner"})
        assert status == 200
        assert crew.calls == [("openai:gpt-4o-mini", "planner")]

    @pytest.mark.asyncio
    async def test_put_model_unknown_run_409(self, server):
        port = server._server.sockets[0].getsockname()[1]
        status, _ = await _req(
            port, "PUT", "/runs/nope/model", {"alias": "openai:gpt-4o-mini"})
        assert status == 409

    @pytest.mark.asyncio
    async def test_put_model_missing_alias_400(self, server):
        server.active_crews["model-run-1"] = _ModelCrew()
        port = server._server.sockets[0].getsockname()[1]
        status, _ = await _req(
            port, "PUT", "/runs/model-run-1/model", {"role": "planner"})
        assert status == 400


# ── CLI 菜单 ───────────────────────────────────────────────────────────────


class _MenuCrew:
    def __init__(self):
        self.switches: list[tuple[str, str | None]] = []

    def current_model_bindings(self):
        return {"default": "openai:gpt-4o-mini", "role_bindings": {}}

    def switch_model(self, alias, *, role=None):
        self.switches.append((alias, role))
        return {"default": alias, "role_bindings": {}}


class TestCliSwitchMenu:
    @pytest.mark.asyncio
    async def test_plan_menu_shows_m_with_crew(self):
        answers = iter([
            "m",                          # 切模型
            "anthropic:claude-3-5-haiku-20241022",  # 新别名
            "",                           # 角色留空
            "a",                          # 批准
        ])
        crew = _MenuCrew()
        prompter = CliPrompter(
            input_fn=lambda _p="": next(answers), crew=crew)
        decision = await prompter.ask_plan([])
        assert decision.action == "approve"
        assert crew.switches == [("anthropic:claude-3-5-haiku-20241022", None)]

    @pytest.mark.asyncio
    async def test_plan_menu_without_crew_no_m(self):
        # 老调用方（无 crew）：菜单里没有 m，喂 m 会被 _choose 拒收后落到 a。
        answers = iter(["m", "a"])
        prompter = CliPrompter(input_fn=lambda _p="": next(answers))
        decision = await prompter.ask_plan([])
        assert decision.action == "approve"
