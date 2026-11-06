"""R5：Agent 间真实咨询（``<message to="…">``）的链路测试。

覆盖四层：

- ``BaseAgent.consult``：真的发一次模型调用、不落产物、不改任务状态；
  mock 通道自动降级、Provider 异常不打断提问方；
- ``PeerRouter``：问自己 / 未知角色不花调用，配额触顶给可继续文案，同问题去重；
- ``AgentFactory``：``message_router`` setter 必须刷到**已缓存**实例（否则等于没开）；
- 端到端：Agent 发 ``<message>`` → 目标角色真的作答 → 答复回灌进后续调用 → 仍产出交付物。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcrew.agents.generic import GenericAgent
from agentcrew.agents.peer import PeerRouter
from agentcrew.agents.registry import AgentFactory
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.models.config import parse_alias
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import CompletionResult
from agentcrew.models.providers.mock import MockProvider
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard


GOOD = "```python file=a.py\ndef f():\n    return 1\n```\n"
MSG_ARCH = '<message to="architect">\n数据模型字段叫什么？\n</message>\n'
ARCH_REPLY = "字段叫 TodoItem。"


def _role(key: str = "backend_dev", title: str = "后端") -> RoleSpec:
    return RoleSpec(
        key=key, title=title, goal="g", backstory="b",
        responsibilities=["写代码"], boundaries=["不写前端"],
        output_contract="输出 python 文件", system_prompt=f"你是{title}工程师。",
    )


class _ScriptedProvider:
    """按队列回放回复的假 provider；记录每次收到的 messages。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.seen: list[list[dict]] = []
        self.binding = SimpleNamespace(
            provider="scripted", max_rpm=None, max_concurrency=None,
            input_price=None, output_price=None)

    async def complete(self, messages, *, temperature, max_tokens,
                       response_format=None):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        return CompletionResult(
            text=self._responses.pop(0), provider="scripted", model="x",
            prompt_tokens=10, completion_tokens=10, latency_ms=1,
        )


class _BoomProvider(_ScriptedProvider):
    async def complete(self, messages, **kwargs):
        self.calls += 1
        raise RuntimeError("网络断了")


def _install(models: ModelFactory, provider) -> str:
    alias = models.settings.default_model
    models._cache[alias] = provider
    return alias


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "false")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts",
                          run_id="R", ledger=ledger)
    models = ModelFactory(settings, ledger=ledger)
    return dict(settings=settings, ledger=ledger, store=store, models=models,
                tmp_path=tmp_path)


# ── consult：真的发一次调用 ───────────────────────────────────────────────


async def test_consult_calls_model_and_emits(env):
    provider = _ScriptedProvider([ARCH_REPLY])
    _install(env["models"], provider)
    agent = GenericAgent(_role("architect", "架构师"), models=env["models"],
                         store=env["store"], ledger=env["ledger"],
                         project_goal="目标")

    reply = await agent.consult("字段叫什么？", from_role="backend_dev")

    assert reply == ARCH_REPLY
    assert provider.calls == 1
    # 咨询模式：只有 system + user 两条，不带产物协议与工具段
    system = provider.seen[0][0]["content"]
    assert "咨询模式" in system
    assert "file=" not in system
    assert "backend_dev" in provider.seen[0][1]["content"]
    events = list(env["ledger"].events("agent_consult"))
    assert len(events) == 1
    assert events[0].data["role"] == "architect"
    assert events[0].data["from_role"] == "backend_dev"
    assert "字段叫什么" in events[0].data["question"]


async def test_consult_does_not_touch_artifacts(env):
    """咨询不该产出产物：ArtifactStore 一个文件都不该多。"""
    provider = _ScriptedProvider([GOOD])  # 即使模型手滑写了围栏代码块
    _install(env["models"], provider)
    agent = GenericAgent(_role("architect", "架构师"), models=env["models"],
                         store=env["store"], ledger=env["ledger"],
                         project_goal="目标")

    await agent.consult("随便写点什么")

    assert env["store"].records() == []
    assert env["store"].exists("a.py") is False


async def test_consult_mock_degrades_without_calling(tmp_path, env):
    """离线 mock 没有 consult 夹具：直接给占位文案，绝不发调用。"""
    mock = MockProvider(parse_alias("mock:mock-model"),
                        fixtures_dir=tmp_path / "fixtures")
    _install(env["models"], mock)
    agent = GenericAgent(_role("architect", "架构师"), models=env["models"],
                         store=env["store"], ledger=env["ledger"],
                         project_goal="目标")

    reply = await agent.consult("字段叫什么？", from_role="backend_dev")

    assert "离线演示" in reply
    assert env["ledger"].count("agent_consult") == 0


async def test_consult_provider_error_does_not_raise(env):
    """Provider 挂了也不能打断提问方：给可继续的文案 + 失败事件。"""
    _install(env["models"], _BoomProvider([]))
    agent = GenericAgent(_role("architect", "架构师"), models=env["models"],
                         store=env["store"], ledger=env["ledger"],
                         project_goal="目标")

    reply = await agent.consult("字段叫什么？", from_role="backend_dev")

    assert "自行决定" in reply
    events = list(env["ledger"].events("agent_consult_failed"))
    assert len(events) == 1
    assert "RuntimeError" in events[0].data["error"]


# ── PeerRouter：边界 ─────────────────────────────────────────────────────


class _FakeFactory:
    def __init__(self, roles: dict[str, str]) -> None:
        self.roles = roles
        self.calls: list[tuple[str, str, str]] = []

    def has_role(self, key: str) -> bool:
        return key in self.roles

    async def consult(self, role, question, *, from_role="", context=""):
        self.calls.append((from_role, role, question, context))
        return self.roles[role]


def _router(tmp_path, factory, *, max_messages=8):
    ledger = Ledger(tmp_path / "r.jsonl", run_id="R")
    return PeerRouter(factory, ledger, max_messages=max_messages), ledger


async def test_router_rejects_self_and_unknown(tmp_path):
    factory = _FakeFactory({"architect": "x"})
    router, ledger = _router(tmp_path, factory)

    assert "不能向自己提问" in await router.ask("architect", "architect", "q")
    assert "没有 'ghost'" in await router.ask("architect", "ghost", "q")
    # 两种拒绝都不该消耗配额、也不该真去问
    assert router.used == 0
    assert factory.calls == []


async def test_router_quota_then_exhausted(tmp_path):
    factory = _FakeFactory({"architect": "x", "reviewer": "y"})
    router, ledger = _router(tmp_path, factory, max_messages=1)

    await router.ask("backend_dev", "architect", "问题一")
    out = await router.ask("backend_dev", "reviewer", "问题二")

    assert "配额已用尽" in out
    assert router.used == 1
    assert router.remaining == 0
    assert len(factory.calls) == 1
    assert ledger.count("agent_message_exhausted") == 1


async def test_router_dedupes_same_question(tmp_path):
    factory = _FakeFactory({"architect": "字段叫 TodoItem"})
    router, ledger = _router(tmp_path, factory)

    first = await router.ask("backend_dev", "architect", "字段叫什么？")
    second = await router.ask("backend_dev", "architect", "字段叫什么？")

    assert first == second
    assert len(factory.calls) == 1
    assert router.used == 1
    assert ledger.count("agent_message_deduped") == 1


async def test_router_zero_quota_blocks_everything(tmp_path):
    factory = _FakeFactory({"architect": "x"})
    router, _ = _router(tmp_path, factory, max_messages=0)

    assert "配额已用尽" in await router.ask("backend_dev", "architect", "q")
    assert factory.calls == []


# ── AgentFactory：setter 必须刷到缓存实例 ──────────────────────────────────


def test_factory_setter_pushes_router_to_cached_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts",
                          run_id="R", ledger=ledger)
    models = ModelFactory(settings, ledger=ledger)
    factory = AgentFactory(models, store, ledger, "目标",
                           RoleCatalog(BUILTIN_ROLES), settings=settings, run_id="R")

    cached = factory.get("backend_dev")
    assert cached._message_router is None

    factory.message_router = lambda to, content: "答复"

    assert factory.message_router is not None
    # 关键：同一个缓存实例必须已经拿到路由，否则对话能力等于没开
    assert cached._message_router is not None
    assert cached._message_router("architect", "q") == "答复"


def test_factory_has_role_reflects_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts",
                          run_id="R", ledger=ledger)
    factory = AgentFactory(ModelFactory(settings, ledger=ledger), store, ledger,
                           "目标", RoleCatalog(BUILTIN_ROLES), settings=settings, run_id="R")

    assert factory.has_role("architect") is True
    assert factory.has_role("ghost") is False


# ── 端到端：发问 → 目标角色作答 → 回灌 → 仍交付 ────────────────────────────


async def test_end_to_end_peer_consult_feeds_reply_back(env):
    """一次 execute 内：发问拿真答复，答复进下一次调用，最终仍产出 a.py。"""
    provider = _ScriptedProvider([MSG_ARCH, ARCH_REPLY, GOOD])
    _install(env["models"], provider)
    spec = ArtifactSpec(path="a.py", kind="python", description="d",
                        must_contain=["def f"])
    task = Task(id="T-1", title="t", description="写 a.py", role="backend_dev",
                acceptance_criteria=["可运行"], deliverables=[spec])
    store = env["store"]
    bus = ContextBus("目标", TaskBoard([task]), store)

    architect = GenericAgent(_role("architect", "架构师"),
                             models=env["models"], store=store,
                             ledger=env["ledger"], project_goal="目标")
    factory = _FakeFactory({"architect": "unused"})

    async def consult(role, question, *, from_role="", context=""):
        return await architect.consult(
            question, from_role=from_role, context=context)

    factory.consult = consult
    router = PeerRouter(factory, env["ledger"], max_messages=4)

    worker = GenericAgent(_role(), models=env["models"], store=store,
                          ledger=env["ledger"], project_goal="目标",
                          message_router=router.bind("backend_dev"))
    result = await worker.execute(task, bus.build(task))

    assert result.task_id == "T-1"
    assert store.read("a.py").startswith("def f")
    # 三次调用：发问 → 咨询 → 产出
    assert provider.calls == 3
    # 第三次调用的历史里含真实答复，而不是角色职责描述
    third = provider.seen[-1]
    assert any(ARCH_REPLY in m["content"] for m in third)
    assert any("message_reply" in m["content"] for m in third)
    # 咨询事件与发问事件都在
    assert env["ledger"].count("agent_message") == 1
    assert env["ledger"].count("agent_consult") == 1
    # R6：被问角色收到的是「带背景的问题」，不是一句孤立提问——
    # 否则它只能反问「你们在做什么、要交什么文件」。
    consult_prompt = provider.seen[1][1]["content"]
    assert "任务：t" in consult_prompt
    assert "需交付：a.py" in consult_prompt
    assert "写 a.py" in consult_prompt


async def test_consult_without_context_keeps_old_prompt(env):
    """没有上下文时文案与 R5 一致：不多出「任务背景」段。"""
    provider = _ScriptedProvider([ARCH_REPLY])
    _install(env["models"], provider)
    agent = GenericAgent(_role("architect", "架构师"), models=env["models"],
                         store=env["store"], ledger=env["ledger"],
                         project_goal="目标")

    await agent.consult("字段叫什么？", from_role="backend_dev")

    user = provider.seen[0][1]["content"]
    assert "任务背景" not in user
    assert user.startswith("来自 backend_dev 的提问：")


# ── 配置 ──────────────────────────────────────────────────────────────────


def test_peer_dialogue_defaults_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.delenv("AGENTCREW_PEER_DIALOGUE_ENABLED", raising=False)
    settings = Settings.load()

    assert settings.peer_dialogue_enabled is False
    assert settings.max_peer_messages == 8


def test_peer_dialogue_env_enables(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_PEER_DIALOGUE_ENABLED", "true")
    monkeypatch.setenv("AGENTCREW_MAX_PEER_MESSAGES", "3")
    settings = Settings.load()

    assert settings.peer_dialogue_enabled is True
    assert settings.max_peer_messages == 3


def test_peer_switch_visible_in_snapshot(tmp_path, monkeypatch):
    """开关必须进 run.json 的配置快照，否则事后无从判断这次 run 开没开。"""
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    snapshot = Settings.load().config_snapshot()

    assert snapshot["peer_dialogue_enabled"] is False
    assert snapshot["max_peer_messages"] == 8


# ── 端到端装配：Crew 真的把路由下发到生产 run ─────────────────────────────

ROOT = Path(__file__).resolve().parents[1]


def _run_sample(tmp_path: Path, *, peer: bool) -> tuple[int, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("AGENTCREW_")
    }
    env["PYTHONPATH"] = str(ROOT)
    env["AGENTCREW_OUTPUT_DIR"] = str(tmp_path / "out")
    if peer:
        env["AGENTCREW_PEER_DIALOGUE_ENABLED"] = "true"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"),
         "run", "--sample", "linear", "--no-live", "--auto"],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
        encoding="utf-8", timeout=180,
    )
    ledgers = sorted((tmp_path / "out").glob("*/ledger.jsonl"))
    text = "\n".join(p.read_text(encoding="utf-8") for p in ledgers)
    return proc.returncode, text


def test_sample_runs_clean_with_peer_enabled(tmp_path):
    """开着跨角色咨询跑完整样例：必须照旧 completed，且留下装配事件。

    样例夹具里没有 ``<message>`` 围栏，所以这里验证的是「开了也不改变行为」+
    「装配确实发生了」，真正的发问链路由上面的进程内用例覆盖。
    """
    code, ledger = _run_sample(tmp_path, peer=True)

    assert code == 0
    assert "peer_dialogue_enabled" in ledger
    assert "run_finished" in ledger


def test_sample_defaults_to_no_peer_wiring(tmp_path):
    """默认关闭：连装配事件都不该有（与改造前逐字一致）。"""
    code, ledger = _run_sample(tmp_path, peer=False)

    assert code == 0
    assert "peer_dialogue_enabled" not in ledger


# ── R6：咨询结果的经验沉淀 ────────────────────────────────────────────────


def test_consult_experience_skips_placeholders():
    """降级文案不是知识，不该进经验库；真实答复才转成 Experience。"""
    from agentcrew.crew.crew import _consult_experience

    assert _consult_experience(
        "backend_dev", "architect", "字段叫什么？", "字段叫 TodoItem。",
        run_id="R1",
    ) is not None
    assert _consult_experience(
        "backend_dev", "architect", "q", "（离线演示：未接真实模型）",
        run_id="R1",
    ) is None
    assert _consult_experience(
        "backend_dev", "architect", "q", "（对方未能作答：请自行决定）",
        run_id="R1",
    ) is None
    assert _consult_experience("a", "b", "q", "   ", run_id="R1") is None


async def test_router_invokes_on_consult(tmp_path):
    """真实咨询成功后回调收到完整四元组。"""
    factory = _FakeFactory({"architect": "字段叫 TodoItem"})
    ledger = Ledger(tmp_path / "r.jsonl", run_id="R")
    seen: list[tuple[str, str, str, str]] = []
    router = PeerRouter(factory, ledger, max_messages=4,
                        on_consult=lambda *a: seen.append(a))

    await router.ask("backend_dev", "architect", "字段叫什么？")

    assert seen == [("backend_dev", "architect", "字段叫什么？", "字段叫 TodoItem")]


async def test_router_dedupe_does_not_capture_twice(tmp_path):
    """去重命中的是缓存，不是一次新咨询——不该重复沉淀。"""
    factory = _FakeFactory({"architect": "字段叫 TodoItem"})
    ledger = Ledger(tmp_path / "r.jsonl", run_id="R")
    seen: list[tuple] = []
    router = PeerRouter(factory, ledger, max_messages=4,
                        on_consult=lambda *a: seen.append(a))

    await router.ask("backend_dev", "architect", "字段叫什么？")
    await router.ask("backend_dev", "architect", "字段叫什么？")

    assert len(seen) == 1


async def test_capture_failure_does_not_break_consult(tmp_path):
    """沉淀失败不能影响这次咨询的返回值。"""
    factory = _FakeFactory({"architect": "字段叫 TodoItem"})
    ledger = Ledger(tmp_path / "r.jsonl", run_id="R")

    def boom(*_args):
        raise OSError("磁盘满了")

    router = PeerRouter(factory, ledger, max_messages=4, on_consult=boom)

    reply = await router.ask("backend_dev", "architect", "字段叫什么？")

    assert reply == "字段叫 TodoItem"
    assert ledger.count("consult_capture_failed") == 1


def test_capture_writes_experience_and_emits(tmp_path, monkeypatch):
    """Crew 层：开关打开时落盘一条经验并记事件。"""
    from types import SimpleNamespace

    from agentcrew.crew.crew import Crew

    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    crew = Crew(Settings.load())
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R1")
    run = SimpleNamespace(
        record=SimpleNamespace(run_id="R1"), ledger=ledger)

    crew._record_consult_experience(
        run, "backend_dev", "architect", "字段叫什么？", "字段叫 TodoItem。")

    store_path = tmp_path / "o" / "knowledge" / "experiences.jsonl"
    assert store_path.is_file()
    lines = [ln for ln in store_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "TodoItem" in lines[0]
    assert ledger.count("consult_experience_captured") == 1


def test_capture_defaults_off_and_skips_placeholder_write(tmp_path, monkeypatch):
    """占位答复走到 Crew 层也不落盘（不产生空经验）。"""
    from types import SimpleNamespace

    from agentcrew.crew.crew import Crew

    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    crew = Crew(Settings.load())
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R1")
    run = SimpleNamespace(record=SimpleNamespace(run_id="R1"), ledger=ledger)

    crew._record_consult_experience(
        run, "backend_dev", "architect", "q", "（离线演示：未接真实模型）")

    assert not (tmp_path / "o" / "knowledge" / "experiences.jsonl").exists()
    assert ledger.count("consult_experience_captured") == 0


def test_consult_capture_defaults_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    assert Settings.load().peer_consult_capture_enabled is False
