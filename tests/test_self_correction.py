"""P2-8 Agent 自我修正循环。

覆盖：

- 首次输出格式错误 → 自我修正后成功；
- 达到 max_self_corrections 仍失败 → 抛 AgentExecutionError；
- max_self_corrections=0 时首次失败即抛错（向后兼容）；
- ledger 记录 agent_self_correction 事件；
- 自我修正的 LLM 调用被记账（llm_call 事件数增加）。
"""

from pathlib import Path

import pytest

from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import CompletionResult
from agentcrew.agents.base_agent import AgentExecutionError
from agentcrew.agents.generic import GenericAgent
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task


def role():
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件", system_prompt="你是后端工程师。")


class _ScriptedProvider:
    """按队列依次返回预设回复的假 provider：每次 complete() 弹一条。"""

    def __init__(self, responses: list[str]) -> None:
        from types import SimpleNamespace
        self._responses = list(responses)
        self.calls = 0
        # V6 P0.5：per-provider 信号量代码读 provider.binding.provider / .max_rpm。
        self.binding = SimpleNamespace(
            provider="scripted", max_rpm=None, max_concurrency=None,
            input_price=None, output_price=None)

    async def complete(self, messages, *, temperature, max_tokens,
                       response_format=None):
        self.calls += 1
        text = self._responses.pop(0)
        return CompletionResult(
            text=text, provider="scripted", model="x",
            prompt_tokens=10, completion_tokens=10, latency_ms=1,
        )


def install_scripted(models: ModelFactory, provider: _ScriptedProvider) -> str:
    alias = models.settings.default_model
    models._cache[alias] = provider  # 绕过真实 provider 构造
    return alias


GOOD = "```python file=a.py\ndef f():\n    return 1\n```\n"
# 无 file= 围栏 → parse 不出任何产物 → validate 报「缺少必需产物」。
BAD = "我觉得应该这么写，但我没给出围栏块。\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "false")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts", run_id="R", ledger=lg)
    spec = ArtifactSpec(path="a.py", kind="python", description="d",
                        must_contain=["def f"])
    task = Task(id="T-1", title="t", description="写 a.py", role="backend_dev",
                acceptance_criteria=["可运行"], deliverables=[spec])
    from agentcrew.task.task_board import TaskBoard
    board = TaskBoard([task])
    bus = ContextBus("目标", board, store)
    return dict(settings=s, ledger=lg, store=store, spec=spec, task=task, bus=bus,
                tmp_path=tmp_path)


async def test_bad_then_good_self_corrects(env):
    provider = _ScriptedProvider([BAD, GOOD])
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    install_scripted(models, provider)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         max_self_corrections=2)
    result = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["store"].read("a.py").startswith("def f()")
    assert provider.calls == 2  # 首次 + 一次自我修正
    events = list(env["ledger"].events("agent_self_correction"))
    assert len(events) == 1
    assert events[0].data["task_id"] == "T-1"
    assert events[0].data["attempt"] == 1
    # 两次 LLM 调用都被记账
    assert env["ledger"].count("llm_call") == 2


async def test_exhausts_corrections_then_raises(env):
    provider = _ScriptedProvider([BAD, BAD, BAD, BAD])
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    install_scripted(models, provider)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         max_self_corrections=2)
    with pytest.raises(AgentExecutionError):
        await agent.execute(env["task"], env["bus"].build(env["task"]))
    # 首次 1 次 + 2 次自我修正 = 3 次 LLM 调用
    assert provider.calls == 3
    assert env["ledger"].count("agent_self_correction") == 2


async def test_zero_corrections_raises_on_first_failure(env):
    """max_self_corrections=0：首次失败即抛错，不重调 LLM（向后兼容）。"""
    provider = _ScriptedProvider([BAD, GOOD])
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    install_scripted(models, provider)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         max_self_corrections=0)
    with pytest.raises(AgentExecutionError):
        await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert provider.calls == 1  # 没有重调
    assert env["ledger"].count("agent_self_correction") == 0


async def test_good_first_try_no_correction(env):
    provider = _ScriptedProvider([GOOD])
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    install_scripted(models, provider)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         max_self_corrections=2)
    result = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["store"].read("a.py").startswith("def f()")
    assert provider.calls == 1
    assert env["ledger"].count("agent_self_correction") == 0
