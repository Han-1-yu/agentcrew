"""R61 二进制产物通道 + 图像交付集成（离线、确定性）。

覆盖：``commit_bytes`` 落盘/清单/上限/冲突；G1 对 image 只判尺寸（跳过文本解析与
must_contain）；BaseAgent 二进制桥（**非** agentic 也适用，generate_image 是通用
工具）；crew 工具门控默认关闭 / 不可信输入只读；image_provider 四通道。
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from agentcrew.agents.base_agent import BaseAgent
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.gate.static_checks import run_static
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.tools.image import _MOCK_PNG_B64
from agentcrew.utils.workspace import reset_workspace, set_workspace

_PNG = base64.b64decode(_MOCK_PNG_B64)


def _store(tmp_path) -> tuple[ArtifactStore, Ledger]:
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    return ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger), ledger


def _image_task(min_bytes: int = 1, must_contain=()) -> Task:
    return Task(
        id="T-1", title="t", description="d", role="backend_dev",
        acceptance_criteria=["ok"],
        deliverables=[ArtifactSpec(
            path="cover.png", kind="image", description="封面图",
            min_bytes=min_bytes, must_contain=list(must_contain))])


# ── commit_bytes ───────────────────────────────────────────────────────────

def test_commit_bytes_writes_and_records(tmp_path):
    store, ledger = _store(tmp_path)
    record = store.commit_bytes("cover.png", _PNG, spec=_image_task().deliverables[0],
                                task=_image_task())
    assert record.kind == "image"
    assert record.size_bytes == len(_PNG)
    assert (store.root / "cover.png").read_bytes() == _PNG
    assert any(r.path == "cover.png" for r in store.records())
    assert (store.root / "MANIFEST.json").is_file()
    assert list(ledger.events("artifact_written"))


def test_commit_bytes_enforces_max_bytes(tmp_path):
    store, _ = _store(tmp_path)
    spec = ArtifactSpec(path="cover.png", kind="image", description="d",
                        max_bytes=4)
    from agentcrew.artifacts.store import ArtifactTooLargeError
    with pytest.raises(ArtifactTooLargeError):
        store.commit_bytes("cover.png", _PNG, spec=spec, task=_image_task())


def test_commit_bytes_cross_task_conflict(tmp_path):
    store, _ = _store(tmp_path)
    spec = _image_task().deliverables[0]
    store.commit_bytes("cover.png", _PNG, spec=spec, task=_image_task())
    other = Task(id="T-2", title="t", description="d", role="backend_dev",
                 acceptance_criteria=["ok"], deliverables=[spec])
    from agentcrew.artifacts.store import ArtifactConflictError
    with pytest.raises(ArtifactConflictError):
        store.commit_bytes("cover.png", _PNG, spec=spec, task=other)


# ── G1：image 只判尺寸 ─────────────────────────────────────────────────────

def test_g1_image_approves_when_size_ok(tmp_path):
    store, _ = _store(tmp_path)
    task = _image_task()
    store.commit_bytes("cover.png", _PNG, spec=task.deliverables[0], task=task)
    review = run_static(task, store, round_no=1)
    assert review.verdict == "approve"


def test_g1_image_rejects_when_too_small(tmp_path):
    store, _ = _store(tmp_path)
    task = _image_task(min_bytes=10_000)
    store.commit_bytes("cover.png", _PNG, spec=task.deliverables[0], task=task)
    review = run_static(task, store, round_no=1)
    assert review.verdict == "reject"
    assert any("cover.png" in issue for issue in review.issues)


def test_g1_image_ignores_must_contain(tmp_path):
    store, _ = _store(tmp_path)
    task = _image_task(must_contain=["不可能出现在二进制里"])
    store.commit_bytes("cover.png", _PNG, spec=task.deliverables[0], task=task)
    review = run_static(task, store, round_no=1)
    assert review.verdict == "approve"  # 二进制不做 must_contain


# ── 文本读取链路：二进制产物不得按 UTF-8 解码 ──────────────────────────────
#
# 2026-11-13 真实模型端到端抓到的缺陷：generate_image 已成功落盘，但 G1 之后的
# 扩展检查 / G2 评审 / 上下文组装仍用 ``store.read`` 把 PNG 当文本解码，
# ``UnicodeDecodeError`` 直接把任务判失败。以下用例钉住「二进制只出元数据」。

def _gatekeeper(tmp_path, store, ledger):
    from agentcrew.gate.gatekeeper import Gatekeeper, NullPrompter
    from agentcrew.gate.reviewer import Reviewer
    from agentcrew.roles.catalog import RoleCatalog
    from agentcrew.task.task_board import TaskBoard

    models = ModelFactory(Settings(default_model="mock:model"),
                          ledger=ledger, fixtures_dir=tmp_path)
    reviewer = Reviewer(models, RoleCatalog(BUILTIN_ROLES), store, ledger)
    return Gatekeeper(reviewer, store, TaskBoard([]), gate="none",
                      ledger=ledger, prompter=NullPrompter())


def test_read_for_prompt_keeps_text_content(tmp_path):
    store, _ = _store(tmp_path)
    task = Task(id="T-2", title="t", description="d", role="backend_dev",
                acceptance_criteria=["ok"],
                deliverables=[ArtifactSpec(path="a.md", kind="markdown",
                                           description="d")])
    store.commit("a.md", "# 标题\n正文", spec=task.deliverables[0], task=task)
    assert store.read_for_prompt("a.md") == "# 标题\n正文"


def test_read_for_prompt_describes_binary_without_decoding(tmp_path):
    store, _ = _store(tmp_path)
    task = _image_task()
    store.commit_bytes("cover.png", _PNG, spec=task.deliverables[0], task=task)
    text = store.read_for_prompt("cover.png")
    assert "二进制产物 image" in text
    assert "cover.png" in text and str(len(_PNG)) in text
    assert store.kind_of("cover.png") == "image"
    assert store.read_bytes("cover.png") == _PNG


def test_extended_checks_skip_binary_artifact(tmp_path):
    """修复前这里抛 UnicodeDecodeError，整个 task 被判失败。"""
    store, ledger = _store(tmp_path)
    task = _image_task()
    store.commit_bytes("cover.png", _PNG, spec=task.deliverables[0], task=task)
    result = _gatekeeper(tmp_path, store, ledger)._run_extended_checks(task)
    assert result.errors == [] and result.warnings == []


def test_reviewer_excerpt_uses_metadata_for_image(tmp_path):
    store, ledger = _store(tmp_path)
    task = _image_task()
    store.commit_bytes("cover.png", _PNG, spec=task.deliverables[0], task=task)
    reviewer = _gatekeeper(tmp_path, store, ledger)._reviewer
    blocks = reviewer._render_artifacts(task)
    joined = "\n".join(blocks)
    assert "二进制产物 image" in joined and "cover.png" in joined


# ── BaseAgent 二进制桥（非 agentic 也适用）─────────────────────────────────

def _agent(tmp_path) -> BaseAgent:
    settings = Settings(default_model="mock:model")
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=tmp_path)
    spec = next(r for r in BUILTIN_ROLES if r.key == "backend_dev")
    return BaseAgent(spec, models=models, store=store, ledger=ledger,
                     project_goal="目标")


def test_binary_bridge_commits_image_for_non_agentic(tmp_path):
    agent = _agent(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "cover.png").write_bytes(_PNG)
    task = _image_task()
    token = set_workspace(workspace)
    try:
        parsed = agent.parse_output("summary", task)   # mock → 围栏（空）
        agent.validate_output(parsed, task)           # 二进制桥计入已产出
        records = agent._commit(parsed, task)
    finally:
        reset_workspace(token)
    assert [r.path for r in records] == ["cover.png"]
    assert records[0].kind == "image"
    assert (agent._store.root / "cover.png").read_bytes() == _PNG


def test_binary_bridge_missing_image_fails_validate(tmp_path):
    agent = _agent(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = _image_task()
    token = set_workspace(workspace)
    try:
        parsed = agent.parse_output("summary", task)
        with pytest.raises(Exception, match="缺少必需产物"):
            agent.validate_output(parsed, task)
    finally:
        reset_workspace(token)


# ── crew 工具门控 ──────────────────────────────────────────────────────────

def _stub_checkpoint(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(run_dir=tmp_path / "run")


def test_generate_image_default_off(tmp_path):
    settings = Settings.load(tools_enabled=True)
    registry = Crew(settings)._build_tool_registry(
        _stub_checkpoint(tmp_path), Ledger(tmp_path / "l.jsonl", run_id="R"))
    assert registry.get("generate_image") is None


def test_generate_image_registered_when_configured(tmp_path):
    settings = Settings.load(tools_enabled=True, image_provider="mock")
    registry = Crew(settings)._build_tool_registry(
        _stub_checkpoint(tmp_path), Ledger(tmp_path / "l.jsonl", run_id="R"))
    assert registry.get("generate_image") is not None


def test_generate_image_blocked_under_untrusted_inputs(tmp_path):
    settings = Settings.load(tools_enabled=True, image_provider="mock",
                             untrusted_inputs=True)
    registry = Crew(settings)._build_tool_registry(
        _stub_checkpoint(tmp_path), Ledger(tmp_path / "l.jsonl", run_id="R"))
    assert registry.get("generate_image") is None


# ── 配置四通道 ─────────────────────────────────────────────────────────────

def test_image_provider_env_channel(monkeypatch):
    monkeypatch.setenv("AGENTCREW_IMAGE_PROVIDER", "mock")
    monkeypatch.setenv("AGENTCREW_IMAGE_TIMEOUT_SECONDS", "30")
    settings = Settings.load()
    assert settings.image_provider == "mock"
    assert settings.image_timeout_seconds == 30.0


def test_image_provider_default_off_and_snapshot():
    settings = Settings.load()
    assert settings.image_provider == ""
    assert settings.config_snapshot()["image_provider"] == ""
