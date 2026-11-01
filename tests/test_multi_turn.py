"""P3-4 多轮 Agent 对话测试。

覆盖：

- 检测 ``<message to="…">`` 围栏并经 message_router 发问；
- 路由回复以 ``<message_reply from="…">`` 回灌进对话历史（第二次 LLM 调用可见）；
- 多轮（跨多个目标角色）对话后最终产出产物；
- ``message_router=None`` 时不检测、不对话（向后兼容）；
- 最大对话轮次限制（触顶降级并记 ``message_loop_exhausted``）；
- ledger 记录 ``agent_message`` 事件。
"""


import pytest

from agentcrew.agents.base_agent import AgentExecutionError
from agentcrew.agents.generic import GenericAgent
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.base import CompletionResult, Message
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard


def role():
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件", system_prompt="你是后端工程师。")


GOOD = "```python file=a.py\ndef f():\n    return 1\n```\n"


class _ScriptedProvider:
    """按队列依次返回预设回复的假 provider；记录每次 complete 收到的 messages。"""

    def __init__(self, responses: list[str]) -> None:
        from types import SimpleNamespace
        self._responses = list(responses)
        self.calls = 0
        self.seen_messages: list[list[Message]] = []
        # V6 P0.5：per-provider 信号量代码读 provider.binding.provider / .max_rpm。
        self.binding = SimpleNamespace(
            provider="scripted", max_rpm=None, max_concurrency=None,
            input_price=None, output_price=None)

    async def complete(self, messages, *, temperature, max_tokens,
                       response_format=None):
        self.calls += 1
        self.seen_messages.append([dict(m) for m in messages])
        text = self._responses.pop(0)
        return CompletionResult(
            text=text, provider="scripted", model="x",
            prompt_tokens=10, completion_tokens=10, latency_ms=1,
        )


def install(models: ModelFactory, provider: _ScriptedProvider) -> str:
    alias = models.settings.default_model
    models._cache[alias] = provider
    return alias


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
    board = TaskBoard([task])
    bus = ContextBus("目标", board, store)
    return dict(settings=s, ledger=lg, store=store, spec=spec, task=task,
                board=board, bus=bus, tmp_path=tmp_path)


MSG_ARCH = '<message to="architect">\n数据模型字段叫什么？\n</message>\n'
MSG_REVIEW = '<message to="reviewer">\n验收标准有几条？\n</message>\n'


async def test_sends_message_and_routes(env):
    provider = _ScriptedProvider([MSG_ARCH, GOOD])
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    install(models, provider)
    seen: list[tuple[str, str]] = []

    def router(to: str, content: str) -> str:
        seen.append((to, content))
        return "字段叫 TodoItem"

    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         message_router=router)
    result = await agent.execute(env["task"], env["bus"].build(env["task"]))

    assert result.task_id == "T-1"
    assert env["store"].read("a.py").startswith("def f()")
    # router 收到了目标角色与消息内容
    assert seen == [("architect", "数据模型字段叫什么？")]
    # 两次 LLM 调用：先发问，拿到回复后再产出
    assert provider.calls == 2
    # ledger 记录 agent_message
    events = list(env["ledger"].events("agent_message"))
    assert len(events) == 1
    assert events[0].data["from_role"] == "backend_dev"
    assert events[0].data["to_role"] == "architect"
    assert "TodoItem" not in events[0].data["content"]  # content 是发出的问题


async def test_reply_appended_to_conversation_history(env):
    """第二次 LLM 调用收到的 messages 里必须含 <message_reply> 回灌。"""
    provider = _ScriptedProvider([MSG_ARCH, GOOD])
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    install(models, provider)

    def router(to: str, content: str) -> str:
        return "字段叫 TodoItem"

    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         message_router=router)
    await agent.execute(env["task"], env["bus"].build(env["task"]))

    # 第二次调用的消息序列
    second_messages = provider.seen_messages[1]
    roles = [m["role"] for m in second_messages]
    assert "assistant" in roles and "user" in roles
    joined = "\n".join(m["content"] for m in second_messages)
    assert '<message_reply from="architect">' in joined
    assert "TodoItem" in joined


async def test_multi_turn_across_two_roles(env):
    """连续问两个角色后再产出：两轮对话。"""
    provider = _ScriptedProvider([MSG_ARCH, MSG_REVIEW, GOOD])
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    install(models, provider)

    def router(to: str, content: str) -> str:
        return {"architect": "TodoItem", "reviewer": "两条"}[to]

    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         message_router=router)
    await agent.execute(env["task"], env["bus"].build(env["task"]))

    assert env["store"].read("a.py").startswith("def f()")
    assert provider.calls == 3
    events = list(env["ledger"].events("agent_message"))
    assert [e.data["to_role"] for e in events] == ["architect", "reviewer"]


async def test_no_router_no_detection_backward_compat(env):
    """message_router=None：即使回复里带 <message> 围栏也不对话、不循环。"""
    # 一条回复里既有 message 围栏又有合法产物 —— 没有 router 时直接解析产物，
    # message 围栏被当作普通文本忽略。
    combined = MSG_ARCH + "\n" + GOOD
    provider = _ScriptedProvider([combined])
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    install(models, provider)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标")  # 无 router
    await agent.execute(env["task"], env["bus"].build(env["task"]))

    assert env["store"].read("a.py").startswith("def f()")
    assert provider.calls == 1  # 没有回灌再问
    assert env["ledger"].count("agent_message") == 0
    assert env["ledger"].count("message_loop_exhausted") == 0


async def test_max_message_rounds_limits_loop(env):
    """模型永远发问 → 触顶 max_message_rounds 后降级，记 message_loop_exhausted。"""
    # 每次回复都是 message 围栏（永不产出合法产物），且没有更多产物可给。
    provider = _ScriptedProvider([MSG_ARCH, MSG_ARCH, MSG_ARCH, MSG_ARCH])
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    install(models, provider)
    router_calls = 0

    def router(to: str, content: str) -> str:
        nonlocal router_calls
        router_calls += 1
        return "已回答"

    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标",
                         message_router=router, max_message_rounds=2,
                         max_self_corrections=0)
    with pytest.raises(AgentExecutionError):
        await agent.execute(env["task"], env["bus"].build(env["task"]))

    # max_message_rounds=2：路由最多触发 1 次（第 2 轮检测到时触顶降级）
    assert router_calls == 1
    assert env["ledger"].count("message_loop_exhausted") == 1
