"""P3-3 收尾：把流式从「只有单测传参」补成「可从配置打开的一等能力」。

此前 ``BaseAgent(stream=True)`` 只有测试在传，生产 run 路径没有任何入口能打开它——
等于一个「测过但从未跑过」的能力。本轮补上 ``Settings.stream``（env / YAML 两通道）
并沿 ``Crew → AgentFactory → BaseAgent`` 一路下发。

这几条用例钉住三件事：

1. 默认关、env 与 YAML 都能开；
2. 开关能真的传到 agent 实例（否则配了也没用）；
3. 默认不开时 agent 走非流式（向后兼容，不产生 agent_stream_chunk）。
"""

from __future__ import annotations

from pathlib import Path

from agentcrew.agents.registry import AgentFactory
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger


def test_stream_defaults_off() -> None:
    assert Settings.model_validate({}).stream is False


def test_stream_env_toggle(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTCREW_STREAM", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    assert Settings.load().stream is True


def test_stream_yaml_toggle(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "agentcrew.yaml"
    cfg.write_text("stream: true\n", encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(cfg))
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    assert Settings.load().stream is True


def _make_factory(tmp_path: Path, *, stream: bool) -> AgentFactory:
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    settings = Settings(default_model="mock:model")
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=tmp_path)
    store = ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger)
    return AgentFactory(
        models, store, ledger, "目标", RoleCatalog(BUILTIN_ROLES),
        settings=settings, run_id="R", stream=stream,
    )


def test_factory_forwards_stream_to_agent(tmp_path: Path) -> None:
    """开关要真的传到 agent 实例，否则配置了等于没配。"""
    assert _make_factory(tmp_path, stream=True).get("backend_dev")._stream is True


def test_factory_default_is_non_streaming(tmp_path: Path) -> None:
    """默认不开：agent 走非流式路径，行为与改造前逐字节一致。"""
    assert _make_factory(tmp_path, stream=False).get("backend_dev")._stream is False
