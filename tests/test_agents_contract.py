"""八个角色 Agent 的契约测试（spec §8.2/§7.0，Task 25）。

brief 给定的用例逐字保留；「本任务 resolutions 的补充用例」之下覆盖 brief 未
钉死的边界：

- 规划调用必须走 ``ModelFactory.complete_structured``（``purpose="plan"``、
  ``role="project_manager"``，计入工厂记账），而不是绕过限流的裸阶梯；
- 提示词必须带全部 8 个角色 key 与职责、T-FINAL / DELIVERY.md 的保留声明、
  产物路径全局唯一、无环、依赖引用已定义 id 与每任务 ``brief``；
- ``feedback`` 非空时逐条回灌「上次拆解被拒绝，原因」；
- 导入链无环：在全新解释器里分别以 ``roles.builtin`` 与 ``agents.registry``
  为先导入者，两种顺序都必须成功；``roles/spec.py`` / ``roles/catalog.py``
  不得反向依赖 agents 层，``agents/*.py`` 顶层不得导入 ``roles.builtin``。
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentcrew.agents.base_agent import BaseAgent
from agentcrew.agents.project_manager import ROLE as PM_ROLE
from agentcrew.agents.project_manager import ProjectManagerAgent
from agentcrew.agents.registry import AGENT_CLASSES
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.planner import PlanOutput


# ── brief 用例（逐字保留）────────────────────────────────────────────────
def test_all_eight_roles_have_agent_classes():
    assert set(AGENT_CLASSES) == {r.key for r in BUILTIN_ROLES}

@pytest.mark.parametrize("key", sorted({r.key for r in BUILTIN_ROLES}))
def test_agent_class_spec_matches_catalog(key):
    from agentcrew.roles.catalog import RoleCatalog
    c = RoleCatalog(BUILTIN_ROLES)
    assert AGENT_CLASSES[key].spec.key == key
    assert AGENT_CLASSES[key].spec == c.get(key)

def test_every_agent_is_a_base_agent_subclass():
    for cls in AGENT_CLASSES.values():
        assert issubclass(cls, BaseAgent) and cls is not BaseAgent

def test_technical_writer_prompt_mentions_readme():
    assert "README" in AGENT_CLASSES["technical_writer"].spec.system_prompt

def test_qa_prompt_mentions_test_cases():
    p = AGENT_CLASSES["qa_engineer"].spec.system_prompt
    assert "测试" in p

def test_devops_prompt_mentions_docker():
    assert "Docker" in AGENT_CLASSES["devops_engineer"].spec.system_prompt

def test_architect_boundaries_exclude_implementation():
    b = " ".join(AGENT_CLASSES["architect"].spec.boundaries)
    assert "实现" in b or "编码" in b

def test_each_role_declares_nonempty_boundaries():
    for key, cls in AGENT_CLASSES.items():
        assert cls.spec.boundaries, f"{key} 未声明能力边界"


# ── 本任务 resolutions 的补充用例 ────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[1]

#: 一份能通过 §7.2 全部校验的最小计划（不含 T-FINAL / DELIVERY.md）。
PLAN = {
    "tasks": [
        {
            "id": "T-1",
            "title": "实现后端 API",
            "description": "按接口契约实现 CRUD",
            "role": "backend_dev",
            "acceptance_criteria": ["接口可用"],
            "deliverables": [
                {"path": "backend/app/main.py", "kind": "python", "description": "API"}
            ],
            "dependencies": [],
            "priority": 3,
            "brief": "实现后端 API",
        }
    ]
}

AGENT_MODULES = (
    "architect.py", "backend_dev.py", "devops_engineer.py", "frontend_dev.py",
    "product_manager.py", "project_manager.py", "qa_engineer.py",
    "technical_writer.py",
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load()
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=ledger)
    return dict(settings=settings, ledger=ledger, store=store, tmp_path=tmp_path)


def write_plan(fx: Path, plan: dict | None = None) -> None:
    """把计划 JSON 写成 mock 的 ``plan_todo.json`` 夹具。"""
    fx.mkdir(parents=True, exist_ok=True)
    (fx / "plan_todo.json").write_text(
        json.dumps(plan if plan is not None else PLAN, ensure_ascii=False),
        encoding="utf-8")


def make_pm(env, fixtures_dir: Path | None = None):
    """真实工厂 + PM，返回 ``(agent, models)`` 供记账断言。"""
    models = ModelFactory(env["settings"], ledger=env["ledger"],
                          fixtures_dir=fixtures_dir)
    agent = ProjectManagerAgent(PM_ROLE, models=models, store=env["store"],
                                ledger=env["ledger"], project_goal="目标")
    return agent, models


class _SpyModels:
    """``ModelFactory`` 替身：记录调用参数并返回预设计划。

    ``get()`` 返回非 ``MockProvider``，PM 因此不会去 ``set_purpose``（同
    ``Reviewer`` 测试的替身思路）；``settings`` 供 ``_resolve_alias`` 使用。
    """

    def __init__(self, settings: Settings, plan: PlanOutput) -> None:
        self.settings = settings
        self._plan = plan
        self.calls: list[dict] = []

    def get(self, alias: str) -> object:
        return object()

    async def complete_structured(self, alias, messages, schema, *,
                                  temperature, max_tokens, purpose, role):
        self.calls.append(dict(alias=alias, messages=list(messages), schema=schema,
                               temperature=temperature, max_tokens=max_tokens,
                               purpose=purpose, role=role))
        return self._plan


def pm_with_spy(env, catalog=None):
    spy = _SpyModels(env["settings"], PlanOutput(tasks=[]))
    agent = ProjectManagerAgent(PM_ROLE, models=spy, store=env["store"],
                                ledger=env["ledger"], project_goal="目标",
                                catalog=catalog)
    return agent, spy


def _prompt_parts(spy: _SpyModels) -> tuple[str, str]:
    """第一次调用的 ``(system, user)`` 消息内容。"""
    messages = spy.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    return messages[0]["content"], messages[1]["content"]


# ── decompose：规划阶段的模型调用（spec §7.0）─────────────────────────────
async def test_pm_decompose_returns_parsed_plan(env, tmp_path):
    fx = tmp_path / "fx"
    write_plan(fx)
    agent, _ = make_pm(env, fx)
    plan = await agent.decompose("做一个 Todo 应用")
    assert isinstance(plan, PlanOutput)
    assert [task.id for task in plan.tasks] == ["T-1"]
    assert plan.tasks[0].role == "backend_dev"
    assert plan.tasks[0].brief == "实现后端 API"


async def test_pm_decompose_is_a_governed_factory_call(env, tmp_path):
    """必须走工厂入口：mock 回放按 plan 取夹具，并落一条 llm_call 账。"""
    fx = tmp_path / "fx"
    write_plan(fx)
    agent, models = make_pm(env, fx)
    await agent.decompose("做一个 Todo 应用")
    assert models.llm_calls == 1
    calls = list(env["ledger"].events("llm_call"))
    assert calls[-1].data["purpose"] == "plan"
    assert calls[-1].data["role"] == "project_manager"


async def test_pm_decompose_declares_plan_schema_and_purpose(env):
    agent, spy = pm_with_spy(env)
    await agent.decompose("做一个 Todo 应用")
    call = spy.calls[0]
    assert call["schema"] is PlanOutput
    assert call["purpose"] == "plan"
    assert call["role"] == "project_manager"


async def test_pm_prompt_lists_all_eight_roles_with_responsibilities(env):
    agent, spy = pm_with_spy(env)
    await agent.decompose("做一个 Todo 应用")
    system, _ = _prompt_parts(spy)
    for role in BUILTIN_ROLES:
        assert role.key in system, f"提示词缺少角色 key：{role.key}"
        for responsibility in role.responsibilities:
            assert responsibility in system, f"提示词缺少 {role.key} 的职责：{responsibility}"


#: 自定义角色（§8.3）：只在该用例里注册进目录，不进 BUILTIN_ROLES。
WIZARD = RoleSpec(
    key="wizard",
    title="向导",
    goal="把项目目标翻译成一份向导说明。",
    backstory="你擅长把复杂目标压缩成三步。",
    responsibilities=["产出向导说明"],
    boundaries=["不写代码"],
    output_contract="产出 docs/wizard.md（Markdown）。",
    system_prompt="你是向导，只输出向导说明。",
)


async def test_pm_prompt_menu_comes_from_live_catalog(env):
    """§8.3：注册的自定义角色必须进 PM 的可用角色菜单，否则永远不会被派发。

    旧实现只渲染 ``BUILTIN_ROLES``，自定义角色能注册却从不出现 —— 「角色是
    数据，不是代码」在提示词层面不成立。菜单改为读本次 run 的 ``RoleCatalog``
    后，此断言钉死该行为；内置角色不得因此被挤掉。
    """
    from agentcrew.roles.catalog import RoleCatalog

    catalog = RoleCatalog([*BUILTIN_ROLES, WIZARD])
    agent, spy = pm_with_spy(env, catalog=catalog)
    await agent.decompose("做一个 Todo 应用")
    system, _ = _prompt_parts(spy)
    assert "wizard" in system, "自定义角色未进入 PM 的可用角色菜单"
    assert "向导" in system
    for role in BUILTIN_ROLES:
        assert role.key in system, f"菜单挤掉了内置角色：{role.key}"


async def test_pm_prompt_reserves_final_task_and_delivery_md(env):
    """T-FINAL 由系统追加且独占 DELIVERY.md，PM 不能自行生成（否则白烧一轮）。"""
    agent, spy = pm_with_spy(env)
    await agent.decompose("做一个 Todo 应用")
    system, _ = _prompt_parts(spy)
    assert "不要生成 id 为 T-FINAL" in system
    assert "不要声明 DELIVERY.md" in system


async def test_pm_prompt_requires_unique_paths_acyclic_and_brief(env):
    agent, spy = pm_with_spy(env)
    await agent.decompose("做一个 Todo 应用")
    system, _ = _prompt_parts(spy)
    assert "全局唯一" in system            # §7.2 校验 5
    assert "无环" in system                # §7.2 校验 4
    assert "已定义的 id" in system         # §7.2 校验 3
    assert "brief" in system               # 一句话摘要，供下游上下文包


async def test_pm_goal_reaches_the_user_message(env):
    agent, spy = pm_with_spy(env)
    await agent.decompose("做一个 Todo 应用")
    _, user = _prompt_parts(spy)
    assert "做一个 Todo 应用" in user


async def test_pm_feedback_is_appended_verbatim(env):
    agent, spy = pm_with_spy(env)
    errors = [
        "任务 T-1 的依赖 T-99 不在本次拆解中",
        "产物路径 a.py 被 T-1 与 T-2 同时声明",
    ]
    await agent.decompose("做一个 Todo 应用", feedback=errors)
    _, user = _prompt_parts(spy)
    assert "上次拆解被拒绝" in user
    for error in errors:
        assert error in user


async def test_pm_empty_feedback_omits_the_section(env):
    agent, spy = pm_with_spy(env)
    await agent.decompose("做一个 Todo 应用", feedback=["   "])
    _, user = _prompt_parts(spy)
    assert "上次拆解被拒绝" not in user


# ── 角色提示词：devops 的 CI/CD（spec §8.2 的补充）───────────────────────
def test_devops_prompt_covers_ci_cd():
    """spec §8.2 把 CI/CD 列为核心职责，职责清单与提示词都要覆盖。"""
    spec = AGENT_CLASSES["devops_engineer"].spec
    assert any("CI/CD" in item for item in spec.responsibilities)
    assert "CI/CD" in spec.system_prompt
    assert "Docker" in spec.system_prompt        # 原有职责不被挤掉


def test_no_role_overrides_the_template_method():
    """``execute`` 的顺序、错误分类与记账归基类；角色差异只在提示词与数据。"""
    for key, cls in AGENT_CLASSES.items():
        assert cls.execute is BaseAgent.execute, f"{key} 覆写了模板方法 execute"


# ── 导入链方向与无环（本任务最脆弱的边界）───────────────────────────────
def _top_level_imports(path: Path) -> set[str]:
    """模块顶层 ``import x`` / ``from x import y`` 的模块名（函数级导入不算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


@pytest.mark.parametrize("name", ["spec.py", "catalog.py"])
def test_role_layer_modules_never_import_agents(name):
    """``roles/spec.py`` 与 ``roles/catalog.py`` 不得依赖 agents 层（反向即环）。"""
    assert "agentcrew.agents" not in _top_level_imports(
        ROOT / "agentcrew" / "roles" / name)


@pytest.mark.parametrize("name", AGENT_MODULES)
def test_agent_modules_do_not_import_builtin_at_module_level(name):
    """``agents/*.py`` 顶层不得导入 ``roles.builtin``，否则与组装方向成环。

    PM 的提示词需要 ``BUILTIN_ROLES``，只能在 ``decompose`` 内以函数级导入取用
    （见 ``agents/project_manager.py`` 模块 docstring）：那时 builtin 已完整加载。
    """
    assert "agentcrew.roles.builtin" not in _top_level_imports(
        ROOT / "agentcrew" / "agents" / name)


@pytest.mark.parametrize("first", ["agentcrew.roles.builtin", "agentcrew.agents.registry"])
def test_import_chain_is_acyclic_in_fresh_interpreter(first):
    """全新解释器里交换先导入者，两种顺序都必须能加载完整注册表。"""
    code = (
        f"import {first}\n"
        "import agentcrew.roles.builtin as builtin\n"
        "import agentcrew.agents.registry as registry\n"
        "assert len(builtin.BUILTIN_ROLES) == 8\n"
        "assert set(registry.AGENT_CLASSES) == "
        "{role.key for role in builtin.BUILTIN_ROLES}\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr


def test_builtin_roles_are_the_agent_module_roles():
    """``BUILTIN_ROLES`` 必须是各模块 ``ROLE`` 的同一对象，不得有第二份副本。"""
    from agentcrew.agents import (
        architect,
        backend_dev,
        devops_engineer,
        frontend_dev,
        product_manager,
        project_manager,
        qa_engineer,
        technical_writer,
    )
    modules = (project_manager, product_manager, architect, backend_dev,
               frontend_dev, qa_engineer, devops_engineer, technical_writer)
    assert BUILTIN_ROLES == tuple(module.ROLE for module in modules)
    for role, module in zip(BUILTIN_ROLES, modules, strict=True):
        assert role is module.ROLE
