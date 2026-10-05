"""``BaseAgent`` 模板方法与 ``AgentFactory`` 的行为测试（spec §8.1，Task 24）。

``test_agent_factory_falls_back_to_generic`` 及以上是 brief 给定的用例（逐字保留）；
``── 本任务 resolutions 的补充用例 ──`` 之下覆盖 brief 未钉死的边界：未声明路径同样
判失败且**先于落盘**、``./a.py`` 归一化不误判、说明文字同时进 ``TaskResult.notes``
与 ledger（含校验失败的任务）、语言标注与契约 kind 不符只记提示、系统提示词含 4
反引号嵌套指令、截断说明与返工意见进 user 消息、Provider 异常原样上抛、mock 回放
上下文由 Agent 自己设置（含返工轮次的夹具编号）。
"""

# 补充用例需要（brief 的 import 块保持原样）
from datetime import datetime
from pathlib import Path

import pytest

from agentcrew.agents.base_agent import AgentExecutionError, BaseAgent
from agentcrew.agents.generic import GenericAgent
from agentcrew.agents.registry import AGENT_CLASSES, AgentFactory
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import TRUNCATION_NOTICE, ContextBus
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.mock import set_purpose
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskResult
from agentcrew.task.task import TaskStatus as S
from agentcrew.task.task_board import TaskBoard


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
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


def role():
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件", system_prompt="你是后端工程师。")


def make_agent(env, fixtures_dir=None):
    models = ModelFactory(env["settings"], ledger=env["ledger"],
                          fixtures_dir=fixtures_dir)
    return GenericAgent(role(), models=models, store=env["store"],
                        ledger=env["ledger"], project_goal="目标")


async def test_execute_writes_contract_artifact(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    agent = make_agent(env, fx)
    result = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert result.task_id == "T-1" and result.role == "backend_dev"
    assert env["store"].read("a.py").startswith("def f()")
    assert result.summary


async def test_execute_raises_when_contract_path_missing(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=wrong.py\nx = 1\n```\n", encoding="utf-8")
    agent = make_agent(env, fx)
    with pytest.raises(AgentExecutionError):
        await agent.execute(env["task"], env["bus"].build(env["task"]))


async def test_execute_raises_on_unparseable_output(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n", encoding="utf-8")     # 未闭合
    agent = make_agent(env, fx)
    with pytest.raises(AgentExecutionError):
        await agent.execute(env["task"], env["bus"].build(env["task"]))


async def test_execute_records_notes_from_prose(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "我实现了 f。\n```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    agent = make_agent(env, fx)
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert "我实现了 f" in r.notes      # 说明文字进 notes，不落盘
    assert not env["store"].exists("我实现了 f。")


async def test_fix_hints_reach_the_model(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    agent = make_agent(env, fx)
    captured = []
    orig = agent.build_messages
    agent.build_messages = lambda t, p, h: (captured.append(list(h)), orig(t, p, h))[1]
    await agent.execute(env["task"], env["bus"].build(env["task"]),
                        fix_hints=["补上输入校验"])
    assert captured[0] == ["补上输入校验"]


async def test_duration_and_timestamps_set(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    agent = make_agent(env, fx)
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert r.duration_ms >= 0 and r.finished_at >= r.started_at


async def test_subclass_hook_is_used(env, tmp_path):
    class Upper(BaseAgent):
        def parse_output(self, raw, task):
            from agentcrew.artifacts.parser import ParsedOutput
            return ParsedOutput(files={"a.py": "def f():\n    return 1\n"}, notes="hooked")
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text("whatever", encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    agent = Upper(role(), models=models, store=env["store"],
                  ledger=env["ledger"], project_goal="目标")
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert r.notes == "hooked"


def test_agent_factory_falls_back_to_generic(env):
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    f = AgentFactory(models, env["store"], env["ledger"], "目标", RoleCatalog())
    assert isinstance(f.get("nobody"), GenericAgent)


def test_agent_factory_caches(env):
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    f = AgentFactory(models, env["store"], env["ledger"], "目标", RoleCatalog())
    assert f.get("x") is f.get("x")


def test_reviewer_agent_for_returns_none_when_no_reviewers(env):
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    f = AgentFactory(models, env["store"], env["ledger"], "目标", RoleCatalog())
    assert f.reviewer_agent_for("orphan") is None


# ── spec §12.2：模型别名三级优先级 ──
@pytest.mark.parametrize("role_model,role_bindings,default,expected", [
    ("anthropic:claude-sonnet-5", {"backend_dev": "qwen:qwen-max"},
     "openai:gpt-4o-mini", "anthropic:claude-sonnet-5"),          # 角色级最高
    ("default", {"backend_dev": "qwen:qwen-max"},
     "openai:gpt-4o-mini", "qwen:qwen-max"),                      # 次之 role_bindings
    ("default", {}, "openai:gpt-4o-mini", "openai:gpt-4o-mini"),  # 最后 default
])
async def test_model_alias_precedence(env, tmp_path, monkeypatch,
                                      role_model, role_bindings, default, expected):
    s = env["settings"].model_copy(update={"role_bindings": role_bindings,
                                           "default_model": default})
    fx = tmp_path / "fx"
    (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    models = ModelFactory(s, ledger=env["ledger"], fixtures_dir=fx)

    seen: list[str] = []
    orig = models.call
    async def spy(alias, *a, **kw):
        seen.append(alias)
        return await orig(alias, *a, **kw)
    monkeypatch.setattr(models, "call", spy)

    r = role(); r.model = role_model
    agent = GenericAgent(r, models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标")
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert seen == [expected]


# ══════════════ 本任务 resolutions 的补充用例 ══════════════

def write_fixture(fx: Path, text: str, *, attempt: int = 1, task_id: str = "T-1") -> None:
    """写一个 mock 的原始回复夹具（``responses/<task_id>/attempt-<n>.md``）。"""
    directory = fx / "responses" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"attempt-{attempt}.md").write_text(text, encoding="utf-8")


GOOD_A = "```python file=a.py\ndef f():\n    return 1\n```\n"


# ── 契约校验：两个方向都要拦，且先于落盘 ──

async def test_execute_raises_on_undeclared_artifact(env, tmp_path):
    """未声明的路径同样判失败，且**一个字节都不落盘**（校验先于 commit）。"""
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A + "\n```python file=secret.py\nx = 1\n```\n")
    agent = make_agent(env, fx)
    with pytest.raises(AgentExecutionError) as exc:
        await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert "secret.py" in str(exc.value)
    assert not env["store"].exists("a.py")


async def test_execute_accepts_dot_slash_path(env, tmp_path):
    """``file=./a.py`` 与契约的 ``a.py`` 是同一条规范化，不得误判为未声明。"""
    fx = tmp_path / "fx"
    write_fixture(fx, "```python file=./a.py\ndef f():\n    return 1\n```\n")
    agent = make_agent(env, fx)
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert [record.path for record in r.artifacts] == ["a.py"]
    assert env["store"].read("a.py").startswith("def f()")


async def test_missing_optional_deliverable_is_allowed(env, tmp_path):
    """非必需产物不产出不算失败；产出的仍按契约落盘。"""
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)
    task = env["task"].model_copy(update={"deliverables": [
        env["spec"],
        ArtifactSpec(path="b.py", kind="python", description="可选", required=False),
    ]})
    agent = make_agent(env, fx)
    r = await agent.execute(task, env["bus"].build(task))
    assert [record.path for record in r.artifacts] == ["a.py"]


def test_validate_output_raises_on_unsafe_path(env, tmp_path):
    """不安全的解析路径按「未声明」报出，绝不让 ``UnsafePathError`` 逃逸。"""
    from agentcrew.artifacts.parser import ParsedOutput
    agent = make_agent(env)
    parsed = ParsedOutput(files={"a.py": "def f():\n    return 1\n",
                                 "../a.py": "x = 1\n"})
    with pytest.raises(AgentExecutionError) as exc:
        agent.validate_output(parsed, env["task"])
    assert "../a.py" in str(exc.value)


# ── notes：进 TaskResult，也进 ledger ──

async def test_notes_are_emitted_to_the_ledger(env, tmp_path):
    fx = tmp_path / "fx"
    write_fixture(fx, "我实现了 f。\n" + GOOD_A)
    agent = make_agent(env, fx)
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    events = list(env["ledger"].events("task_notes"))
    assert len(events) == 1
    assert "我实现了 f" in events[0].data["notes"]
    assert events[0].data["task_id"] == "T-1"


async def test_notes_reach_ledger_even_when_validation_fails(env, tmp_path):
    """校验失败的任务没有 ``task_finished``，说明文字必须更早落 ledger。"""
    fx = tmp_path / "fx"
    write_fixture(fx, "说明：可能被截断的 README。\n```python file=wrong.py\nx = 1\n```\n")
    agent = make_agent(env, fx)
    with pytest.raises(AgentExecutionError):
        await agent.execute(env["task"], env["bus"].build(env["task"]))
    notes = [event.data["notes"] for event in env["ledger"].events("task_notes")]
    assert any("可能被截断" in text for text in notes)


async def test_lifecycle_events_are_emitted(env, tmp_path):
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)
    agent = make_agent(env, fx)
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    started = list(env["ledger"].events("task_started"))
    finished = list(env["ledger"].events("task_finished"))
    assert started[0].data == {"task_id": "T-1", "role": "backend_dev", "attempt": 0}
    assert finished[0].data["duration_ms"] == r.duration_ms
    assert finished[0].data["role"] == "backend_dev"
    # 空 notes 不发噪声事件
    assert env["ledger"].count("task_notes") == 0


async def test_execute_does_not_write_task_state(env, tmp_path):
    """执行只产出 ``TaskResult``；状态流转是调度器的单点写入（spec §7.3）。"""
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)
    agent = make_agent(env, fx)
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["task"].status is S.PENDING
    assert env["task"].attempts == 0 and env["task"].result is None


# ── 语言标注交叉检查（normalize_lang 的消费者）──

async def test_language_kind_mismatch_is_a_note_not_a_failure(env, tmp_path):
    """模型把 python 文件标成 json：记一条提示，照常落盘，不判失败。"""
    fx = tmp_path / "fx"
    write_fixture(fx, "```json file=a.py\ndef f():\n    return 1\n```\n")
    agent = make_agent(env, fx)
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert env["store"].read("a.py").startswith("def f()")
    assert "a.py" in r.notes and "json" in r.notes and "python" in r.notes


async def test_matching_language_adds_no_note(env, tmp_path):
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)
    agent = make_agent(env, fx)
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert r.notes == ""


async def test_four_backtick_nested_example_adds_no_note(env, tmp_path):
    """4 反引号 README 里的嵌套示例不是真实块，不得被误读成语言标注。

    真实块在前、嵌套示例在后：按「后写覆盖前写」的行扫描会误判 ``a.py`` 是
    ``json``，只有按围栏规则跳过 4 反引号块体内的行才不会有假提示。
    """
    fx = tmp_path / "fx"
    write_fixture(
        fx,
        "```python file=a.py\ndef f():\n    return 1\n```\n"
        "\n"
        "````markdown file=README.md\n"
        "# 用起来\n"
        "```json file=a.py\n"
        '{"nested": "example"}\n'
        "```\n"
        "````\n",
    )
    task = env["task"].model_copy(update={"deliverables": [
        env["spec"],
        ArtifactSpec(path="README.md", kind="markdown", description="说明"),
    ]})
    agent = make_agent(env, fx)
    r = await agent.execute(task, env["bus"].build(task))
    assert env["store"].read("README.md").startswith("# 用起来")
    assert r.notes == ""


# ── 提示词：system 块与渲染后的上下文包 ──

def test_system_message_teaches_artifact_protocol(env):
    agent = make_agent(env)
    messages = agent.build_messages(env["task"], env["bus"].build(env["task"]), ())
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    assert "你是后端工程师。" in system        # RoleSpec.system_prompt 逐字
    assert "输出 python 文件" in system        # RoleSpec.output_contract 逐字
    assert "file=" in system                  # 产物格式说明
    assert "顶格" in system                    # 围栏必须行首起（解析器的硬规则）
    assert "4 个反引号" in system and "````" in system   # 嵌套示例规则
    assert "未声明" in system                  # 只许写契约声明的路径


def test_user_message_renders_goal_task_and_contract(env):
    agent = make_agent(env)
    messages = agent.build_messages(env["task"], env["bus"].build(env["task"]), ())
    user = messages[1]["content"]
    assert "目标" in user
    assert "写 a.py" in user and "可运行" in user
    assert "a.py" in user and "python" in user and "def f" in user


def test_render_pack_appends_truncation_notice_and_fix_hints(env):
    agent = make_agent(env)
    pack = env["bus"].build(env["task"]).model_copy(update={"truncated": True})
    messages = agent.build_messages(env["task"], pack, ["补上输入校验"])
    user = messages[1]["content"]
    assert TRUNCATION_NOTICE in user
    assert "上次评审意见，请修正" in user and "补上输入校验" in user


# ── 错误分类与 mock 回放上下文 ──

async def test_provider_failure_propagates_as_is(env, monkeypatch):
    """Provider 异常不包成 ``AgentExecutionError``：重试语义归调度器与 provider 层。"""
    class Boom(RuntimeError):
        """模拟 provider 层错误（网络/鉴权）。"""

    models = ModelFactory(env["settings"], ledger=env["ledger"])

    async def boom(alias, *args, **kwargs):
        raise Boom("连接中断")

    monkeypatch.setattr(models, "call", boom)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标")
    with pytest.raises(Boom):
        await agent.execute(env["task"], env["bus"].build(env["task"]))


async def test_execute_overwrites_stale_mock_replay_context(env, tmp_path):
    """purpose/task_id/attempt 由 Agent 层设置（spec §16.1、Task 21 的裁定）。

    夹具**只放** attempt-2.md，且先把 provider 的上下文设成错的：
    只有 Agent 覆盖了 purpose、task_id 并算出「第 2 次执行」才会命中它 ——
    顺手钉死返工轮次的夹具编号口径（``attempts + rework_count``）。
    """
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A, attempt=2)
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    set_purpose(models.get(env["settings"].default_model), "review",
                role="someone_else", task_id="WRONG", attempt=9)
    agent = GenericAgent(role(), models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标")
    task = env["task"].model_copy(update={"attempts": 1, "rework_count": 1})
    await agent.execute(task, env["bus"].build(task))
    assert env["store"].read("a.py").startswith("def f()")
    assert env["ledger"].count("mock_fallback") == 0


# ── summarize 钩子 ──

async def test_summarize_hook_is_used(env, tmp_path):
    class Fancy(BaseAgent):
        def summarize(self, result):
            return f"自定义摘要：{len(result.artifacts)} 个产物"

    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    agent = Fancy(role(), models=models, store=env["store"],
                  ledger=env["ledger"], project_goal="目标")
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert r.summary == "自定义摘要：1 个产物"


def test_summarize_defaults_to_notes_then_artifacts(env):
    agent = make_agent(env)
    now = datetime.now()
    long_notes = "长" * 1000
    result = TaskResult(task_id="T-1", role="backend_dev", summary="", artifacts=[],
                        notes=long_notes, started_at=now, finished_at=now, duration_ms=0)
    assert agent.summarize(result) == long_notes[:400]
    empty = result.model_copy(update={"notes": ""})
    assert agent.summarize(empty)          # 空 notes 回落到产物清单，绝不空串


# ── AgentFactory：缓存、注册与评审人 ──

def test_agent_factory_distinct_keys_get_distinct_agents(env):
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    f = AgentFactory(models, env["store"], env["ledger"], "目标", RoleCatalog())
    assert f.get("x") is not f.get("y")


def test_agent_factory_uses_catalog_spec_when_registered(env):
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    spec = RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件", system_prompt="你是后端工程师。")
    f = AgentFactory(models, env["store"], env["ledger"], "目标", RoleCatalog([spec]))
    agent = f.get("backend_dev")
    assert agent.spec is spec


def test_register_overrides_class_and_invalidates_cache(env):
    class Special(GenericAgent):
        """占位子类：只用于验证 register 生效。"""

    models = ModelFactory(env["settings"], ledger=env["ledger"])
    f = AgentFactory(models, env["store"], env["ledger"], "目标", RoleCatalog())
    # 用未登记的自定义 key：8 个内置 key 由 Task 25 的 AGENT_CLASSES 解析到各自的
    # 角色类（见 tests/test_agents_contract.py），不再是 GenericAgent 兜底。
    assert type(f.get("custom_role")) is GenericAgent
    f.register("custom_role", Special)
    assert type(f.get("custom_role")) is Special


def test_register_does_not_pollute_module_registry(env):
    """注册是 run 级行为，不得写进模块级 ``AGENT_CLASSES``（跨 run 泄漏）。"""
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    f = AgentFactory(models, env["store"], env["ledger"], "目标", RoleCatalog())
    before = dict(AGENT_CLASSES)

    class Special(GenericAgent):
        """占位子类。"""

    f.register("devops_engineer", Special)
    assert AGENT_CLASSES == before


def test_reviewer_agent_for_returns_first_reviewer(env):
    def spec(key, reviewer_for=()):
        return RoleSpec(key=key, title=key, goal="g", backstory="b",
                        responsibilities=["r"], boundaries=["b"],
                        output_contract="o", system_prompt="s",
                        reviewer_for=list(reviewer_for))

    catalog = RoleCatalog([spec("backend_dev", ["architect", "qa_engineer"]),
                           spec("architect"), spec("qa_engineer")])
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    f = AgentFactory(models, env["store"], env["ledger"], "目标", catalog)
    reviewer = f.reviewer_agent_for("backend_dev")
    assert reviewer is not None and reviewer.spec.key == "architect"
    assert f.reviewer_agent_for("backend_dev") is reviewer


async def test_undeclared_failure_names_both_sides(env, tmp_path):
    """实测真实模型会既按契约产出 app.js、又按上游设计文档多加一个 server.js。

    错误只说「多出 server.js」时，模型分不清是「名字写错了」还是「不该多写」——
    连续三次尝试原地打转。消息必须同时给出**允许写的清单**。
    """
    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A + "\n```python file=server.py\nx = 1\n```\n")
    agent = make_agent(env, fx)
    with pytest.raises(AgentExecutionError) as exc:
        await agent.execute(env["task"], env["bus"].build(env["task"]))
    msg = str(exc.value)
    assert "server.py" in msg      # 多出来的
    assert "只允许写" in msg        # 以及允许写的清单
    assert "a.py" in msg           # 契约里的那个


def test_deliverables_section_declares_itself_authoritative(env):
    """契约节必须写明它是文件清单的唯一权威 —— 上下文里同时有架构文档的文件结构。"""
    from agentcrew.agents.base_agent import _render_deliverables

    text = _render_deliverables(env["task"])
    assert "唯一权威" in text
    assert "以本清单为准" in text


# ── 输出截断：与「模型写砸了」必须区分开 ──

def test_is_truncated_recognises_both_provider_spellings():
    from types import SimpleNamespace

    from agentcrew.agents.base_agent import _is_truncated

    assert _is_truncated(SimpleNamespace(finish_reason="length"))       # OpenAI 兼容
    assert _is_truncated(SimpleNamespace(finish_reason="max_tokens"))   # Anthropic
    assert _is_truncated(SimpleNamespace(finish_reason="LENGTH"))       # 大小写
    assert not _is_truncated(SimpleNamespace(finish_reason="stop"))
    assert not _is_truncated(SimpleNamespace(finish_reason=None))
    assert not _is_truncated(SimpleNamespace())                         # 替身没有该字段


async def test_execute_reports_truncation_as_truncation_not_parse_failure(env, tmp_path):
    """实测架构师连续 3 次停在 8192 token，每次都被判「解析失败」，重试三次全废。

    截断必须报成截断 —— 否则同上限重试只会再截断一次，模型也无从知道要精简。
    """

    from agentcrew.models.providers.base import CompletionResult

    fx = tmp_path / "fx"
    write_fixture(fx, GOOD_A)
    agent = make_agent(env, fx)

    async def fake_call(alias, messages, **kw):
        return CompletionResult(
            text="```python file=a.py\ndef f():\n",   # 明显未闭合
            provider="mock", model="m", prompt_tokens=1, completion_tokens=8192,
            latency_ms=0, finish_reason="length")

    agent._models.call = fake_call
    with pytest.raises(AgentExecutionError) as exc:
        await agent.execute(env["task"], env["bus"].build(env["task"]))
    msg = str(exc.value)
    assert "截断" in msg and "8192" in msg
    assert "未闭合" not in msg      # 不能报成格式错误
