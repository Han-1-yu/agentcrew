"""R3：工具链从「只在单测里」接进真实 run，以及两个能力开关的配置通道。

覆盖：默认关闭（向后兼容）、env/YAML 开关与五级合并顺序、Crew 自动装配 6 个工具
与工作区隔离、角色权限白名单接线、``tool_registry_ready`` 留痕、显式注入 override
优先级、AgentFactory 把注册表下发到每个 Agent、配置导出/导入往返、API options
白名单。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agentcrew.agents.registry import AgentFactory
from agentcrew.api.server import _validate_run_options
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.tools import ReadFileTool, ToolRegistry, WriteFileTool

ROOT = Path(__file__).resolve().parents[1]


def _stub_checkpoint(tmp_path: Path) -> SimpleNamespace:
    """``_build_tool_registry`` 只用到 ``checkpoint.run_dir``。"""
    return SimpleNamespace(run_dir=tmp_path / "run")


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "l.jsonl", run_id="R")


def _run_cli(args: list[str], cwd: Path, env: dict | None = None):
    full_env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("AGENTCREW_")
    }
    full_env["PYTHONPATH"] = str(ROOT)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        capture_output=True, text=True, cwd=str(cwd), env=full_env,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout, proc.stderr


# ── 配置通道 ───────────────────────────────────────────────────────────

def test_switches_default_off():
    settings = Settings.load()
    assert settings.tools_enabled is False
    assert settings.entity_memory_enabled is False
    # 密钥绝不落盘：两个布尔开关不进脱敏链路，但必须在快照里可见
    assert settings.config_snapshot()["tools_enabled"] is False
    assert settings.config_snapshot()["entity_memory_enabled"] is False


def test_switches_from_env(monkeypatch):
    monkeypatch.setenv("AGENTCREW_TOOLS_ENABLED", "true")
    monkeypatch.setenv("AGENTCREW_ENTITY_MEMORY_ENABLED", "true")
    settings = Settings.load()
    assert settings.tools_enabled is True
    assert settings.entity_memory_enabled is True


def test_switches_from_yaml(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "agentcrew.yaml"
    cfg.write_text(
        "tools_enabled: true\nentity_memory_enabled: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("AGENTCREW_CONFIG", str(cfg))
    settings = Settings.load()
    assert settings.tools_enabled is True
    assert settings.entity_memory_enabled is True


def test_merge_order_env_over_yaml_unchanged(tmp_path: Path, monkeypatch):
    """红线：五级合并顺序不变 —— 环境变量仍压过 YAML 顶层标量。"""
    cfg = tmp_path / "agentcrew.yaml"
    cfg.write_text("tools_enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(cfg))
    monkeypatch.setenv("AGENTCREW_TOOLS_ENABLED", "false")
    assert Settings.load().tools_enabled is False


def test_config_export_import_roundtrip_keeps_switches(tmp_path: Path, monkeypatch):
    """开启态的开关能 export 出来、再 import 回去；默认态不污染导出文档。"""
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "agentcrew.yaml"
    cfg.write_text(
        "tools_enabled: true\nentity_memory_enabled: true\n", encoding="utf-8"
    )
    env = {"AGENTCREW_CONFIG": str(cfg)}
    code, out, err = _run_cli(["config", "export"], tmp_path, env=env)
    assert code == 0, err
    exported = yaml.safe_load(out)
    assert exported["tools_enabled"] is True
    assert exported["entity_memory_enabled"] is True

    imported = tmp_path / "roundtrip.yaml"
    imported.write_text(out, encoding="utf-8")
    target = tmp_path / "fresh.yaml"
    target.write_text("models:\n  default: mock:mock\n", encoding="utf-8")
    code, out, err = _run_cli(
        ["config", "import", str(imported)], tmp_path,
        env={"AGENTCREW_CONFIG": str(target)},
    )
    assert code == 0, err
    merged = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert merged["tools_enabled"] is True
    assert merged["entity_memory_enabled"] is True


def test_default_export_does_not_emit_switches(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "agentcrew.yaml"
    cfg.write_text("models:\n  default: openai:gpt-4o-mini\n", encoding="utf-8")
    code, out, err = _run_cli(
        ["config", "export"], tmp_path, env={"AGENTCREW_CONFIG": str(cfg)}
    )
    assert code == 0, err
    doc = yaml.safe_load(out)
    assert "tools_enabled" not in doc
    assert "entity_memory_enabled" not in doc


# ── Crew 装配 ──────────────────────────────────────────────────────────

def test_registry_not_built_when_disabled(tmp_path: Path):
    crew = Crew(Settings.load())
    ledger = _ledger(tmp_path)
    assert crew._build_tool_registry(_stub_checkpoint(tmp_path), ledger) is None
    assert ledger.count("tool_registry_ready") == 0
    assert not (tmp_path / "run" / "workspace").exists()


def test_registry_assembly_when_enabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_TOOLS_ENABLED", "true")
    ledger = _ledger(tmp_path)
    registry = Crew(Settings.load())._build_tool_registry(
        _stub_checkpoint(tmp_path), ledger
    )
    assert isinstance(registry, ToolRegistry)
    names = [tool.name for tool in registry.list_tools()]
    # P1：run_python 默认不注册（无网络/系统沙箱，见 docs/OPERATIONS.md）
    assert names == ["read_file", "list_files", "grep", "write_file", "web_fetch"]
    run_dir = tmp_path / "run"
    workspace = run_dir / "workspace"
    assert workspace.is_dir(), "工作区目录必须随装配创建"
    assert registry.get("read_file").workdir == run_dir
    assert isinstance(registry.get("write_file"), WriteFileTool)
    assert registry.get("write_file").workdir == workspace, "写工具必须与产物目录隔离"
    events = list(ledger.events("tool_registry_ready"))
    assert len(events) == 1
    assert events[0].data["tools"] == names
    assert events[0].data["workspace"] == "workspace"


def test_role_permissions_are_wired(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_TOOLS_ENABLED", "true")
    settings = Settings.load(
        tool_permissions={"backend_dev": ["read_file", "write_file"]}
    )
    registry = Crew(settings)._build_tool_registry(
        _stub_checkpoint(tmp_path), _ledger(tmp_path)
    )
    assert registry.is_allowed("backend_dev", "read_file") is True
    assert registry.is_allowed("backend_dev", "run_python") is False
    assert registry.is_allowed("qa_engineer", "run_python") is True  # 未配置 = 全放行
    event = list(_ledger(tmp_path).events("tool_registry_ready"))[0]
    assert event.data["permissioned_roles"] == ["backend_dev"]


def test_injected_registry_wins_over_disabled_config(tmp_path: Path):
    """库调用方可绕过配置，直接注入自定义注册表。"""
    custom = ToolRegistry()
    custom.register(ReadFileTool(workdir=tmp_path))
    ledger = _ledger(tmp_path)
    crew = Crew(Settings.load(), tool_registry=custom)
    assert crew._build_tool_registry(_stub_checkpoint(tmp_path), ledger) is custom
    assert custom._ledger is ledger, "override 也要绑定本次 run 的 ledger 留痕"
    assert [tool.name for tool in custom.list_tools()] == ["read_file"]
    assert ledger.count("tool_registry_ready") == 0  # 不自动装配、不冒发事件


# ── AgentFactory 下发 ──────────────────────────────────────────────────

def _factory(tmp_path: Path, settings: Settings, registry: ToolRegistry | None):
    ledger = Ledger(tmp_path / "factory.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "artifacts", run_id="R", ledger=ledger)
    models = ModelFactory(settings, ledger=ledger)
    return AgentFactory(
        models, store, ledger, "目标", RoleCatalog(),
        settings=settings, run_id="R", tool_registry=registry,
    )


def test_agent_factory_down_registry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    settings = Settings.load()
    registry = ToolRegistry()
    registry.register(ReadFileTool(workdir=tmp_path))
    factory = _factory(tmp_path, settings, registry)
    agent = factory.get("backend_dev")
    assert agent._tool_registry is registry
    assert "read_file" in agent._tools_section(), "下发后 Agent 应能看见工具说明"


def test_agent_factory_defaults_to_none(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    settings = Settings.load()
    factory = _factory(tmp_path, settings, None)
    assert factory.get("backend_dev")._tool_registry is None


# ── API options 白名单 ─────────────────────────────────────────────────

@pytest.mark.parametrize("options", [
    {"tools_enabled": True},
    {"entity_memory_enabled": False},
    {"tools_enabled": True, "entity_memory_enabled": True},
])
def test_run_options_accept_new_switches(options):
    assert _validate_run_options(options) is None


@pytest.mark.parametrize("options", [
    {"tools_enabled": "false"},         # 字符串 truthy 会静默打开工具链
    {"entity_memory_enabled": 1},
    {"tools_enabled": None},
])
def test_run_options_reject_bad_switch_values(options):
    assert _validate_run_options(options) is not None


# ── 端到端：真实 run 里工具链确实被装配 ────────────────────────────────

def test_e2e_sample_run_with_tools_enabled(tmp_path: Path):
    """开启开关跑一整套 mock run：工具链进生产链路且交付结果不变。

    样例夹具的回复不含 ``<tool_call>``，ReAct 循环第一轮就结束 —— 这正好验证
    「工具链开启但模型不用工具」时，确定性调度与产物交付不受影响。
    """
    output_dir = tmp_path / "o"
    code, out, err = _run_cli(
        ["run", "--sample", "linear", "--no-live", "--auto"], ROOT,
        env={
            "AGENTCREW_TOOLS_ENABLED": "true",
            "AGENTCREW_OUTPUT_DIR": str(output_dir),
            "AGENTCREW_GATE": "none",
        },
    )
    assert code == 0, f"stdout={out[-1500:]}\nstderr={err[-1500:]}"
    ledgers = sorted(output_dir.glob("*/ledger.jsonl"))
    assert len(ledgers) == 1, f"run 目录异常：{list(output_dir.iterdir())}"
    text = ledgers[0].read_text(encoding="utf-8")
    assert '"tool_registry_ready"' in text
    assert '"web_fetch"' in text, "装配事件应列出全部 6 个内置工具"
    record = json.loads((ledgers[0].parent / "run.json").read_text(encoding="utf-8"))
    assert str(record["status"]).lower() == "completed", record["status"]
    # 工作区随装配创建，且不与产物目录混用
    assert (ledgers[0].parent / "workspace").is_dir()
    assert (ledgers[0].parent / "artifacts").is_dir()
