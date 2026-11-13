"""R60 外部 coding CLI agentic 执行体（离线、确定性）。

子进程一律 monkeypatch（不真起 Claude Code / Codex），解析函数是纯函数直接测。
覆盖：cwd 隔离（contextvar / 显式 / 缺失报错）、argv 先切分后替换、prompt 走 stdin、
claude-json / codex-jsonl / plain 三种解析、用量抽取与回退、超时 / 非零退出 /
可执行文件缺失的错误收敛。
"""

from __future__ import annotations

import asyncio
import json

import pytest

# ── 接线/桥接测试用的装配导入 ───────────────────────────────────────────────
from agentcrew.agents.base_agent import BaseAgent
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.models.config import (
    PROVIDER_PRESETS,
    ModelBinding,
    ProviderCapabilities,
    parse_alias,
)
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.cli import CliProvider
from agentcrew.models.providers.cli_agentic import (
    AgenticCliProvider,
    _parse_claude_json,
    _parse_codex_jsonl,
)
from agentcrew.models.providers.mock import MockProvider
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task
from agentcrew.utils.workspace import reset_workspace, set_workspace


def _binding(command: str = "claude -p --output-format json") -> ModelBinding:
    return ModelBinding(
        alias="claude-code:default", provider="claude-code", model="default",
        implementation="cli", base_url=None, api_key_env="",
        capabilities=ProviderCapabilities(), command=command)


class FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.stdin_written: bytes | None = None
        self.killed = False

    async def communicate(self, input=None):  # noqa: A002 - 对齐 subprocess API
        self.stdin_written = input
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _patch_exec(monkeypatch, proc: FakeProc) -> dict:
    """替换 create_subprocess_exec，记录 argv / cwd，返回捕获字典。"""
    captured: dict = {}

    async def fake_exec(*argv, cwd=None, stdin=None, stdout=None, stderr=None):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return captured


# ── 纯解析函数 ──────────────────────────────────────────────────────────────

def test_parse_claude_json_extracts_result_and_usage():
    out = json.dumps({"type": "result", "is_error": False, "result": "done",
                      "usage": {"input_tokens": 11, "output_tokens": 22}})
    text, i, o = _parse_claude_json(out)
    assert text == "done" and (i, o) == (11, 22)


def test_parse_claude_json_error_raises():
    out = json.dumps({"is_error": True, "result": "API Error: refused"})
    with pytest.raises(RuntimeError, match="refused"):
        _parse_claude_json(out)


def test_parse_claude_json_missing_usage_returns_none():
    text, i, o = _parse_claude_json(json.dumps({"result": "x"}))
    assert text == "x" and i is None and o is None


def test_parse_codex_jsonl_last_agent_message_and_usage():
    lines = [
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "first"}}),
        "not-json-noise",
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "final"}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 5, "output_tokens": 7}}),
    ]
    text, i, o = _parse_codex_jsonl("\n".join(lines))
    assert text == "final" and (i, o) == (5, 7)


def test_parse_codex_jsonl_no_events_falls_back_to_stdout():
    text, i, o = _parse_codex_jsonl("raw output")
    assert text == "raw output" and i is None and o is None


# ── complete：cwd / argv / stdin / 用量 ─────────────────────────────────────

async def test_complete_uses_contextvar_workspace_as_cwd(tmp_path, monkeypatch):
    proc = FakeProc(stdout=json.dumps({"result": "ok"}).encode())
    captured = _patch_exec(monkeypatch, proc)
    token = set_workspace(tmp_path)
    try:
        provider = AgenticCliProvider(_binding())
        result = await provider.complete(
            [{"role": "user", "content": "hi"}], temperature=0.0, max_tokens=100)
    finally:
        reset_workspace(token)
    assert captured["cwd"] == str(tmp_path)
    assert result.text == "ok"
    assert proc.stdin_written == b"[User]\nhi"


async def test_complete_explicit_workspace_overrides_contextvar(tmp_path, monkeypatch):
    proc = FakeProc(stdout=json.dumps({"result": "ok"}).encode())
    captured = _patch_exec(monkeypatch, proc)
    other = tmp_path / "explicit"
    provider = AgenticCliProvider(_binding(), workspace=other)
    await provider.complete([{"role": "user", "content": "x"}],
                            temperature=0.0, max_tokens=10)
    assert captured["cwd"] == str(other)


async def test_complete_without_workspace_raises(monkeypatch):
    _patch_exec(monkeypatch, FakeProc())
    provider = AgenticCliProvider(_binding())
    with pytest.raises(ValueError, match="未设置 run 工作区"):
        await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0.0, max_tokens=10)


async def test_complete_without_command_raises(tmp_path, monkeypatch):
    _patch_exec(monkeypatch, FakeProc())
    provider = AgenticCliProvider(_binding(command=""), workspace=tmp_path)
    provider.binding.command = None
    with pytest.raises(ValueError, match="缺少 command"):
        await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0.0, max_tokens=10)


async def test_complete_argv_split_then_substitute(tmp_path, monkeypatch):
    proc = FakeProc(stdout=json.dumps({"result": "ok"}).encode())
    captured = _patch_exec(monkeypatch, proc)
    binding = _binding(command='claude -p --model {model} --permission-mode acceptEdits')
    provider = AgenticCliProvider(binding, workspace=tmp_path)
    await provider.complete([{"role": "user", "content": "x"}],
                            temperature=0.0, max_tokens=10)
    assert captured["argv"] == [
        "claude", "-p", "--model", "default", "--permission-mode", "acceptEdits"]


async def test_complete_claude_usage_populates_tokens(tmp_path, monkeypatch):
    out = json.dumps({"result": "body",
                      "usage": {"input_tokens": 100, "output_tokens": 200}})
    _patch_exec(monkeypatch, FakeProc(stdout=out.encode()))
    provider = AgenticCliProvider(_binding(), workspace=tmp_path)
    result = await provider.complete([{"role": "user", "content": "x"}],
                                     temperature=0.0, max_tokens=10)
    assert (result.prompt_tokens, result.completion_tokens) == (100, 200)


async def test_complete_codex_jsonl_format(tmp_path, monkeypatch):
    out = "\n".join([
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "codex done"}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 3, "output_tokens": 4}}),
    ])
    _patch_exec(monkeypatch, FakeProc(stdout=out.encode()))
    provider = AgenticCliProvider(
        _binding(command="codex exec --json"), output_format="codex-jsonl",
        workspace=tmp_path)
    result = await provider.complete([{"role": "user", "content": "x"}],
                                     temperature=0.0, max_tokens=10)
    assert result.text == "codex done"
    assert (result.prompt_tokens, result.completion_tokens) == (3, 4)


async def test_complete_plain_format_estimates_tokens(tmp_path, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(stdout=b"plain text"))
    provider = AgenticCliProvider(
        _binding(command="some-cli"), output_format="plain", workspace=tmp_path)
    result = await provider.complete([{"role": "user", "content": "x"}],
                                     temperature=0.0, max_tokens=10)
    assert result.text == "plain text"
    assert result.prompt_tokens > 0 and result.completion_tokens > 0


# ── 错误收敛 ────────────────────────────────────────────────────────────────

async def test_complete_nonzero_exit_raises_with_stderr(tmp_path, monkeypatch):
    _patch_exec(monkeypatch, FakeProc(stderr=b"boom", returncode=2))
    provider = AgenticCliProvider(_binding(), workspace=tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0.0, max_tokens=10)


async def test_complete_missing_executable_raises_valueerror(tmp_path, monkeypatch):
    async def fake_exec(*argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    provider = AgenticCliProvider(_binding(), workspace=tmp_path)
    with pytest.raises(ValueError, match="可执行文件不存在"):
        await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0.0, max_tokens=10)


async def test_complete_timeout_kills_and_raises(tmp_path, monkeypatch):
    class HangProc(FakeProc):
        async def communicate(self, input=None):  # noqa: A002
            raise asyncio.TimeoutError

    proc = HangProc()
    _patch_exec(monkeypatch, proc)
    provider = AgenticCliProvider(_binding(), workspace=tmp_path, timeout_seconds=5)
    with pytest.raises(TimeoutError, match="超时"):
        await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0.0, max_tokens=10)
    assert proc.killed


# ── 配置四通道 / 预设 / 工厂选择 ────────────────────────────────────────────

def test_parse_alias_carries_agentic_fields():
    cc = parse_alias("claude-code:default")
    assert cc.cli_agentic is True and cc.cli_output == "claude-json"
    cx = parse_alias("codex:default")
    assert cx.cli_agentic is True and cx.cli_output == "codex-jsonl"
    plain = parse_alias("cli:x")
    assert plain.cli_agentic is False and plain.cli_output == "plain"


def test_presets_use_safe_permission_defaults():
    assert "acceptEdits" in (PROVIDER_PRESETS["claude-code"].command or "")
    assert "workspace-write" in (PROVIDER_PRESETS["codex"].command or "")


def test_parse_alias_override_can_set_agentic():
    binding = parse_alias(
        "cli:x", overrides={"cli": {"cli_agentic": True, "cli_output": "codex-jsonl",
                                    "cli_timeout_seconds": 42}})
    assert binding.cli_agentic is True
    assert binding.cli_output == "codex-jsonl"
    assert binding.cli_timeout_seconds == 42


def _factory(default_model: str, mock: bool = False) -> ModelFactory:
    settings = Settings(default_model=default_model, mock=mock)
    return ModelFactory(settings)


def test_factory_selects_agentic_provider():
    factory = _factory("claude-code:default")
    assert isinstance(factory.get("claude-code:default"), AgenticCliProvider)
    assert factory.is_agentic_cli("claude-code:default") is True


def test_factory_plain_cli_stays_one_shot():
    factory = _factory("cli:x")
    provider = factory.get("cli:x")
    assert isinstance(provider, CliProvider)
    assert not isinstance(provider, AgenticCliProvider)
    assert factory.is_agentic_cli("cli:x") is False


def test_factory_mock_overrides_agentic():
    factory = _factory("claude-code:default", mock=True)
    assert isinstance(factory.get("claude-code:default"), MockProvider)
    assert factory.is_agentic_cli("claude-code:default") is False


# ── BaseAgent 工作区 → 产物桥接 ─────────────────────────────────────────────

def _agent(tmp_path, default_model: str) -> BaseAgent:
    settings = Settings(default_model=default_model)
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger)
    models = ModelFactory(settings, ledger=ledger, fixtures_dir=tmp_path)
    spec = next(r for r in BUILTIN_ROLES if r.key == "backend_dev")
    return BaseAgent(spec, models=models, store=store, ledger=ledger,
                     project_goal="目标")


def _task() -> Task:
    return Task(id="T-1", title="t", description="d", role="backend_dev",
                acceptance_criteria=["ok"],
                deliverables=[ArtifactSpec(path="a.py", kind="python",
                                           description="d")])


def test_bridge_reads_workspace_deliverables(tmp_path):
    agent = _agent(tmp_path, "claude-code:default")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("print('hi')\n", encoding="utf-8")
    token = set_workspace(workspace)
    try:
        parsed = agent.parse_output("CLI summary", _task())
    finally:
        reset_workspace(token)
    assert parsed.files == {"a.py": "print('hi')\n"}
    assert parsed.notes == "CLI summary"


def test_bridge_missing_file_yields_empty(tmp_path):
    agent = _agent(tmp_path, "claude-code:default")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    token = set_workspace(workspace)
    try:
        parsed = agent.parse_output("summary", _task())
    finally:
        reset_workspace(token)
    assert parsed.files == {}  # 必需产物缺失 → validate_output 判失败走返工


def test_bridge_inactive_without_workspace(tmp_path):
    agent = _agent(tmp_path, "claude-code:default")
    raw = '```python file=a.py\nprint(1)\n```'
    parsed = agent.parse_output(raw, _task())
    assert parsed.files == {"a.py": "print(1)\n"}  # 回退围栏解析


def test_bridge_inactive_for_non_agentic(tmp_path):
    agent = _agent(tmp_path, "mock:model")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("from workspace\n", encoding="utf-8")
    token = set_workspace(workspace)
    try:
        raw = '```python file=a.py\nprint(2)\n```'
        parsed = agent.parse_output(raw, _task())
    finally:
        reset_workspace(token)
    assert parsed.files == {"a.py": "print(2)\n"}  # 非 agentic 仍走围栏，不读工作区
