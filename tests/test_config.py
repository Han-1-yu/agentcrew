import os

from agentcrew.config import Settings


def test_defaults(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("AGENTCREW_"):
            monkeypatch.delenv(k)
    s = Settings.load()
    assert s.max_concurrency == 4
    assert s.context_budget_tokens == 24000
    assert s.plan_max_attempts == 2
    assert s.gate in ("both", "none")
    assert s.default_model == "openai:gpt-4o-mini"


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("AGENTCREW_MAX_CONCURRENCY", "7")
    assert Settings.load().max_concurrency == 7


def test_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("AGENTCREW_MAX_CONCURRENCY", "7")
    assert Settings.load(max_concurrency=2).max_concurrency == 2


def test_secret_values_only_nonempty(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    s = Settings.load()
    assert "sk-abc" in s.secret_values()
    assert "" not in s.secret_values()


def test_gate_degrades_to_none_when_not_a_tty(monkeypatch):
    monkeypatch.delenv("AGENTCREW_GATE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert Settings.load().gate == "none"


def test_output_dir_is_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "out"))
    assert Settings.load().output_dir.is_absolute()
