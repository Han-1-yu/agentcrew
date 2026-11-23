"""R70 SOUL.md / MEMORY.md（借 Orca「Bot Memory in Markdown」，离线、确定性）。

覆盖：companion 路径与非法 kind/key / 读取四态（缺失、空、超长截断、正常）/
读取失败不阻断 / AgentFactory 按档案注入 / 无档案不注入 / build_messages
空态逐字不变、非空两段出现且顺序正确 / YAML 保存不触碰 companion。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.agents.base_agent import BaseAgent
from agentcrew.agents.registry import AgentFactory
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextPack
from agentcrew.models.factory import ModelFactory
from agentcrew.profiles.store import (
    COMPANION_MAX_CHARS,
    ProfileStoreError,
    companion_path,
    read_companion,
    save_profile_file,
)
from agentcrew.roles.binding import AgentProfile
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── companion 路径与读取 ────────────────────────────────────────────────────

def test_companion_path_naming(tmp_path):
    assert companion_path(tmp_path, "fast", "SOUL") == tmp_path / "fast.SOUL.md"
    assert companion_path(tmp_path, "fast", "MEMORY") == tmp_path / "fast.MEMORY.md"
    with pytest.raises(ProfileStoreError, match="未知的 companion 类型"):
        companion_path(tmp_path, "fast", "HEART")
    with pytest.raises(ProfileStoreError, match="key 不合法"):
        companion_path(tmp_path, "Fast Writer", "SOUL")


def test_read_companion_missing_and_empty(tmp_path):
    assert read_companion(tmp_path, "fast", "SOUL") == ""
    _write(tmp_path / "fast.SOUL.md", "  \n\t\n")
    assert read_companion(tmp_path, "fast", "SOUL") == ""


def test_read_companion_strips_and_caps(tmp_path):
    _write(tmp_path / "fast.SOUL.md", "\n  严谨、先复述需求再动手。\n")
    assert read_companion(tmp_path, "fast", "SOUL") == "严谨、先复述需求再动手。"
    _write(tmp_path / "fast.MEMORY.md", "x" * (COMPANION_MAX_CHARS + 50))
    text = read_companion(tmp_path, "fast", "MEMORY")
    assert text.endswith("…（已截断）")
    assert len(text) <= COMPANION_MAX_CHARS + len("\n…（已截断）")
    # max_chars=0 显式关闭截断
    assert read_companion(tmp_path, "fast", "MEMORY", max_chars=0) == (
        "x" * (COMPANION_MAX_CHARS + 50))


def test_read_companion_unreadable_is_empty(tmp_path):
    # 目录占着同名路径 → 读取抛 OSError，按空处理而不是炸掉 run
    (tmp_path / "fast.SOUL.md").mkdir()
    assert read_companion(tmp_path, "fast", "SOUL") == ""


# ── AgentFactory 注入 ──────────────────────────────────────────────────────

def _factory(tmp_path: Path, settings: Settings) -> AgentFactory:
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=tmp_path)
    return AgentFactory(
        models, store, ledger, "目标", RoleCatalog(BUILTIN_ROLES),
        settings=settings, run_id="R")


def _settings(tmp_path: Path) -> Settings:
    directory = tmp_path / "agents.d"
    directory.mkdir()
    return Settings(
        default_model="mock:model",
        agent_profiles={"fast": AgentProfile(key="fast", title="快速档")},
        profiles_dir=directory)


def test_factory_injects_companions(tmp_path):
    settings = _settings(tmp_path)
    directory = settings.profiles_dir
    assert directory is not None
    _write(directory / "fast.SOUL.md", "你是快写手：先给结论再给细节。")
    _write(directory / "fast.MEMORY.md", "- 2026-11 项目 A 用 pnpm")
    factory = _factory(tmp_path, settings)
    agent = factory.get("backend_dev", "fast")
    assert isinstance(agent, BaseAgent)
    assert agent.agent_key == "fast"
    assert agent.soul_text == "你是快写手：先给结论再给细节。"
    assert agent.memory_text == "- 2026-11 项目 A 用 pnpm"


def test_factory_without_companions_is_empty(tmp_path):
    settings = _settings(tmp_path)
    factory = _factory(tmp_path, settings)
    agent = factory.get("backend_dev", "fast")
    assert isinstance(agent, BaseAgent)
    assert (agent.soul_text, agent.memory_text) == ("", "")
    # 未绑定档案的角色同样不注入
    plain = factory.get("backend_dev")
    assert isinstance(plain, BaseAgent)
    assert (plain.soul_text, plain.memory_text) == ("", "")


# ── build_messages：空态逐字不变，非空按序注入 ─────────────────────────────

def _bare_agent(tmp_path: Path) -> BaseAgent:
    settings = Settings(default_model="mock:model")
    ledger = Ledger(tmp_path / "l2.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "art2", run_id="R", ledger=ledger)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=tmp_path)
    spec = next(r for r in BUILTIN_ROLES if r.key == "backend_dev")
    return BaseAgent(spec, models=models, store=store, ledger=ledger,
                     project_goal="目标")


def _task() -> Task:
    return Task(id="T-1", title="t", description="d", role="backend_dev",
                acceptance_criteria=["ok"], deliverables=[])


def test_build_messages_empty_is_verbatim(tmp_path):
    agent = _bare_agent(tmp_path)
    task = _task()
    messages = agent.build_messages(task, ContextPack(project_goal="目标", task=task), ())
    system = messages[0]["content"]
    assert "SOUL.md" not in system and "MEMORY.md" not in system
    # 与「显式空串」逐字一致（关闭态不因新代码路径而变）
    assert system.startswith(agent.spec.system_prompt)
    assert "## 输出规范" in system


def test_build_messages_injects_soul_first_and_memory_last(tmp_path):
    agent = _bare_agent(tmp_path)
    agent.soul_text = "先复述需求，再动手。"
    agent.memory_text = "- 项目 A 用 pnpm"
    task = _task()
    messages = agent.build_messages(task, ContextPack(project_goal="目标", task=task), ())
    system = messages[0]["content"]
    soul_at = system.index("## 执行体人格（SOUL.md）")
    rules_at = system.index("## 输出规范")
    memory_at = system.index("## 执行体记忆（MEMORY.md）")
    assert soul_at < rules_at < memory_at
    assert system.endswith("- 项目 A 用 pnpm")
    assert "先复述需求，再动手。" in system


# ── YAML 保存不触碰 companion ──────────────────────────────────────────────

def test_save_profile_keeps_companions_untouched(tmp_path):
    directory = tmp_path / "agents.d"
    directory.mkdir()
    soul = _write(directory / "fast.SOUL.md", "人格原文")
    memory = _write(directory / "fast.MEMORY.md", "记忆原文")
    saved = save_profile_file(
        directory, AgentProfile(key="fast", title="改过标题"), overwrite=True)
    assert saved == directory / "fast.yaml"
    assert soul.read_text(encoding="utf-8") == "人格原文"
    assert memory.read_text(encoding="utf-8") == "记忆原文"
    body = saved.read_text(encoding="utf-8")
    assert "人格原文" not in body and "记忆原文" not in body
