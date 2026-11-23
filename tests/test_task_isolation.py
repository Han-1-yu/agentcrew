"""R70 任务级工作区隔离（借 Orca Parallel Worktrees，离线、确定性）。

子进程一律 monkeypatch（不真起 coding CLI）：fake_exec 在捕获的 cwd 里写
交付物与草稿，模拟 agentic CLI 的自主文件 IO。覆盖：默认关逐字不变 / 开且
agentic 时按任务建目录 / 上游产物播种（含缺件与非法路径跳过、二进制字节）/
读回走隔离目录 / 并发两任务互不见草稿 / 非 agentic 与 mock 通道不隔离 /
返工复用目录且重新播种 / 配置四通道 / run options 布尔校验。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agentcrew.agents.base_agent import BaseAgent
from agentcrew.api.run_options import validate_run_options
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextPack, UpstreamDigest
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.utils.workspace import current_workspace, reset_workspace, set_workspace


def _agent(tmp_path: Path, *, isolation: bool,
           model: str = "claude-code:default", mock: bool = False) -> BaseAgent:
    settings = Settings(default_model=model, mock=mock)
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=tmp_path)
    spec = next(r for r in BUILTIN_ROLES if r.key == "backend_dev")
    return BaseAgent(spec, models=models, store=store, ledger=ledger,
                     project_goal="目标", task_isolation=isolation)


def _task(tid: str = "T-1", path: str = "a.py") -> Task:
    return Task(id=tid, title="t", description="d", role="backend_dev",
                acceptance_criteria=["ok"],
                deliverables=[ArtifactSpec(path=path, kind="python",
                                           description="d")])


def _upstream_task() -> Task:
    return Task(id="T-0", title="u", description="d", role="architect",
                acceptance_criteria=["ok"], deliverables=[])


def _pack(task: Task, files: list[str] | None = None) -> ContextPack:
    upstream = []
    if files is not None:
        upstream.append(UpstreamDigest(
            task_id="T-0", role="architect", title="设计",
            brief="b", summary="s", files=files))
    return ContextPack(project_goal="目标", task=task, upstream=upstream)


class _FakeProc:
    def __init__(self, stdout: bytes):
        self._stdout = stdout
        self.returncode = 0

    async def communicate(self, input=None):  # noqa: A002 - 对齐 subprocess API
        return self._stdout, b""

    def kill(self):
        pass

    async def wait(self):
        return 0


def _patch_exec(monkeypatch, *, write_scratch: bool = False) -> dict:
    """fake_exec：在 cwd 里写 a.py（内容带目录名），可选写同名草稿。"""
    captured: dict = {"cwds": []}
    payload = json.dumps({"result": "done",
                          "usage": {"input_tokens": 1, "output_tokens": 2}})

    async def fake_exec(*argv, cwd=None, stdin=None, stdout=None, stderr=None):
        captured["cwds"].append(cwd)
        directory = Path(cwd)
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("a.py", "b.py"):
            (directory / name).write_text(
                f"print('from {directory.name}')\n", encoding="utf-8")
        if write_scratch:
            (directory / f"scratch-{directory.name}.txt").write_text(
                "draft", encoding="utf-8")
        return _FakeProc(payload.encode("utf-8"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


def _events(tmp_path: Path) -> str:
    return (tmp_path / "l.jsonl").read_text(encoding="utf-8")


# ── 默认关：逐字不变 ────────────────────────────────────────────────────────

def test_default_off_keeps_run_workspace(tmp_path, monkeypatch):
    agent = _agent(tmp_path, isolation=False)
    run_ws = tmp_path / "ws"
    run_ws.mkdir()
    captured = _patch_exec(monkeypatch)
    token = set_workspace(run_ws)
    try:
        result = asyncio.run(agent.execute(_task(), _pack(_task())))
    finally:
        reset_workspace(token)
    assert captured["cwds"] == [str(run_ws)]
    assert not (run_ws / "tasks").exists()
    assert "task_workspace_isolated" not in _events(tmp_path)
    assert result.artifacts and agent._store.read("a.py") == "print('from ws')\n"


# ── 开且 agentic：隔离 + 播种 + 读回 ────────────────────────────────────────

def test_on_agentic_isolates_and_seeds(tmp_path, monkeypatch):
    agent = _agent(tmp_path, isolation=True)
    upstream_task = _upstream_task()
    agent._store.commit(
        "design.md", "# design v1\n",
        spec=ArtifactSpec(path="design.md", kind="markdown", description="d"),
        task=upstream_task)
    run_ws = tmp_path / "ws"
    run_ws.mkdir()
    captured = _patch_exec(monkeypatch)
    task = _task()
    token = set_workspace(run_ws)
    try:
        result = asyncio.run(agent.execute(task, _pack(task, ["design.md"])))
    finally:
        reset_workspace(token)
    task_dir = run_ws / "tasks" / "T-1"
    assert captured["cwds"] == [str(task_dir)]
    # 上游产物被播种进隔离目录
    assert (task_dir / "design.md").read_text(encoding="utf-8") == "# design v1\n"
    # 交付物从隔离目录读回，run 级工作区不被污染
    assert agent._store.read("a.py") == "print('from T-1')\n"
    assert not (run_ws / "a.py").exists()
    assert result.artifacts
    lines = [json.loads(line) for line in _events(tmp_path).splitlines()
             if line.strip()]
    iso = [e for e in lines if e.get("event") == "task_workspace_isolated"]
    assert len(iso) == 1
    assert iso[0]["data"]["seeded"] == 1
    assert iso[0]["data"]["skipped"] == 0


def test_on_without_workspace_is_noop(tmp_path):
    agent = _agent(tmp_path, isolation=True)
    assert agent._maybe_isolate_workspace(_task(), _pack(_task())) is None
    assert not (tmp_path / "ws").exists()


def test_non_agentic_not_isolated(tmp_path):
    agent = _agent(tmp_path, isolation=True, model="cli:x")
    run_ws = tmp_path / "ws"
    run_ws.mkdir()
    token = set_workspace(run_ws)
    try:
        assert agent._maybe_isolate_workspace(_task(), _pack(_task())) is None
    finally:
        reset_workspace(token)
    assert not (run_ws / "tasks").exists()


def test_mock_channel_never_isolates(tmp_path):
    agent = _agent(tmp_path, isolation=True, mock=True)
    run_ws = tmp_path / "ws"
    run_ws.mkdir()
    token = set_workspace(run_ws)
    try:
        assert agent._maybe_isolate_workspace(_task(), _pack(_task())) is None
    finally:
        reset_workspace(token)
    assert not (run_ws / "tasks").exists()


# ── 并发隔离 ────────────────────────────────────────────────────────────────

def test_concurrent_tasks_do_not_share_drafts(tmp_path, monkeypatch):
    agent = _agent(tmp_path, isolation=True)
    run_ws = tmp_path / "ws"
    run_ws.mkdir()
    _patch_exec(monkeypatch, write_scratch=True)

    async def run_both():
        token = set_workspace(run_ws)
        try:
            return await asyncio.gather(
                agent.execute(_task("T-1", "a.py"), _pack(_task("T-1", "a.py"))),
                agent.execute(_task("T-2", "b.py"), _pack(_task("T-2", "b.py"))))
        finally:
            reset_workspace(token)

    asyncio.run(run_both())
    d1 = run_ws / "tasks" / "T-1"
    d2 = run_ws / "tasks" / "T-2"
    assert (d1 / "scratch-T-1.txt").exists()
    assert (d2 / "scratch-T-2.txt").exists()
    assert not (d1 / "scratch-T-2.txt").exists()
    assert not (d2 / "scratch-T-1.txt").exists()
    assert (d1 / "a.py").read_text(encoding="utf-8") == "print('from T-1')\n"
    assert (d2 / "b.py").read_text(encoding="utf-8") == "print('from T-2')\n"


# ── 返工：复用目录 + 重新播种 ───────────────────────────────────────────────

def test_rework_reuses_dir_and_reseeds_upstream(tmp_path, monkeypatch):
    agent = _agent(tmp_path, isolation=True)
    upstream_task = _upstream_task()
    spec = ArtifactSpec(path="design.md", kind="markdown", description="d")
    agent._store.commit("design.md", "v1\n", spec=spec, task=upstream_task)
    run_ws = tmp_path / "ws"
    run_ws.mkdir()
    _patch_exec(monkeypatch, write_scratch=True)
    task = _task()

    async def run_twice():
        token = set_workspace(run_ws)
        try:
            await agent.execute(task, _pack(task, ["design.md"]))
            # 上游返工后出了新版；本任务也返工（同一 task.id）
            agent._store.commit("design.md", "v2\n", spec=spec,
                                task=upstream_task)
            await agent.execute(task, _pack(task, ["design.md"]))
        finally:
            reset_workspace(token)

    asyncio.run(run_twice())
    task_dir = run_ws / "tasks" / "T-1"
    assert (task_dir / "design.md").read_text(encoding="utf-8") == "v2\n"
    # 自己上一轮的草稿仍在（目录复用）
    assert (task_dir / "scratch-T-1.txt").exists()


# ── 播种的边界 ──────────────────────────────────────────────────────────────

def test_seed_skips_missing_and_unsafe(tmp_path):
    agent = _agent(tmp_path, isolation=True)
    task_dir = tmp_path / "iso"
    task_dir.mkdir()
    pack = _pack(_task(), ["gone.py", "../evil.py", ""])
    seeded, skipped = agent._seed_upstream(task_dir, pack)
    assert (seeded, skipped) == (0, 3)
    assert list(task_dir.iterdir()) == []
    assert not (tmp_path / "evil.py").exists()


def test_seed_copies_binary_bytes(tmp_path):
    agent = _agent(tmp_path, isolation=True)
    upstream_task = _upstream_task()
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 64
    agent._store.commit_bytes(
        "logo.png", data,
        spec=ArtifactSpec(path="logo.png", kind="image", description="d"),
        task=upstream_task)
    task_dir = tmp_path / "iso"
    task_dir.mkdir()
    seeded, skipped = agent._seed_upstream(task_dir, _pack(_task(), ["logo.png"]))
    assert (seeded, skipped) == (1, 0)
    assert (task_dir / "logo.png").read_bytes() == data


def test_unsafe_task_id_falls_back_to_run_workspace(tmp_path):
    agent = _agent(tmp_path, isolation=True)
    run_ws = tmp_path / "ws"
    run_ws.mkdir()
    bad = Task(id="../escape", title="t", description="d", role="backend_dev",
               acceptance_criteria=["ok"], deliverables=[])
    token = set_workspace(run_ws)
    try:
        assert agent._maybe_isolate_workspace(bad, _pack(bad)) is None
    finally:
        reset_workspace(token)
    assert current_workspace() is None  # token 已复位
    assert not (tmp_path / "escape").exists()


# ── 配置四通道 + run options ────────────────────────────────────────────────

def test_config_default_off():
    assert Settings().agentic_task_isolation is False


def test_config_env_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_AGENTIC_TASK_ISOLATION", "true")
    assert Settings.load().agentic_task_isolation is True


def test_config_yaml_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    cfg = tmp_path / "agentcrew.yaml"
    cfg.write_text("agentic_task_isolation: true\n", encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(cfg))
    assert Settings.load().agentic_task_isolation is True


def test_config_snapshot_carries_field(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    snapshot = Settings.load().config_snapshot()
    assert snapshot["agentic_task_isolation"] is False


def test_options_accepts_bool_and_rejects_others():
    assert validate_run_options({"agentic_task_isolation": True}) is None
    assert validate_run_options({"agentic_task_isolation": False}) is None
    assert validate_run_options({"agentic_task_isolation": "yes"}) == (
        "options.agentic_task_isolation 必须是布尔值")
    assert validate_run_options({"agentic_task_isolation": 1}) == (
        "options.agentic_task_isolation 必须是布尔值")
    assert validate_run_options({"agentic_task_isolation": None}) == (
        "options.agentic_task_isolation 不能为 null")
