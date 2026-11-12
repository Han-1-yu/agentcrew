"""C1 / C2 的配置通道：YAML / 环境变量 / export-import / API options。

「四通道登记」是仓库对所有新能力定的红线（见路线文档 §6）：只在代码里加一个
字段、不接配置，等于给了一个**永远打不开**的能力。这里逐条钉住：

- 默认值必须是「关闭 / local」，升级不能静默改变成本与安全承诺；
- 环境变量能覆盖 YAML（五级合并顺序不变）；
- `config export` 能导出、`config import` 能往返；
- API options 只开放安全的键（`search_provider`），**不开放** `sandbox_backend`。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from agentcrew.api.run_options import validate_run_options
from agentcrew.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(args: list[str], cwd: Path, env: dict[str, str]):
    full_env = {k: v for k, v in os.environ.items()
                if not k.startswith("AGENTCREW_")}
    full_env["PYTHONPATH"] = str(ROOT)
    full_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        capture_output=True, text=True, cwd=str(cwd), env=full_env,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_defaults_are_off_and_local():
    settings = Settings.load()
    assert settings.sandbox_backend == "local"
    assert settings.sandbox_image == "python:3.12-slim"
    assert settings.sandbox_memory_mb == 512
    assert settings.sandbox_cpus == 1.0
    assert settings.sandbox_allow_network is False
    assert settings.search_provider == ""
    assert settings.search_max_results == 5
    assert settings.search_timeout_seconds == 15.0


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("AGENTCREW_SANDBOX_BACKEND", "docker")
    monkeypatch.setenv("AGENTCREW_SANDBOX_IMAGE", "sandbox:test")
    monkeypatch.setenv("AGENTCREW_SANDBOX_MEMORY_MB", "1024")
    monkeypatch.setenv("AGENTCREW_SANDBOX_CPUS", "2.5")
    monkeypatch.setenv("AGENTCREW_SANDBOX_ALLOW_NETWORK", "true")
    monkeypatch.setenv("AGENTCREW_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("AGENTCREW_SEARCH_MAX_RESULTS", "9")
    monkeypatch.setenv("AGENTCREW_SEARCH_TIMEOUT_SECONDS", "20")
    settings = Settings.load()
    assert settings.sandbox_backend == "docker"
    assert settings.sandbox_image == "sandbox:test"
    assert settings.sandbox_memory_mb == 1024
    assert settings.sandbox_cpus == 2.5
    assert settings.sandbox_allow_network is True
    assert settings.search_provider == "tavily"
    assert settings.search_max_results == 9
    assert settings.search_timeout_seconds == 20.0


def test_yaml_values_and_env_precedence(tmp_path, monkeypatch):
    cfg = tmp_path / "agentcrew.yaml"
    cfg.write_text(
        "sandbox_backend: docker\n"
        "sandbox_memory_mb: 768\n"
        "search_provider: brave\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCREW_CONFIG", str(cfg))
    settings = Settings.load()
    assert settings.sandbox_backend == "docker"
    assert settings.sandbox_memory_mb == 768
    assert settings.search_provider == "brave"

    # 红线：环境变量仍压过 YAML
    monkeypatch.setenv("AGENTCREW_SANDBOX_BACKEND", "local")
    assert Settings.load().sandbox_backend == "local"


def test_unknown_search_provider_fails_at_assembly_not_in_config():
    """配置层只存字符串；拼错的名字在装配期报错（可读的「可选值」提示）。"""
    from agentcrew.tools.search import build_search_provider

    settings = Settings.load(search_provider="google")
    assert settings.search_provider == "google"
    try:
        build_search_provider(settings.search_provider)
    except ValueError as exc:
        assert "tavily" in str(exc)
    else:  # pragma: no cover - 不该走到这里
        raise AssertionError("未知提供方必须报错，不能静默降级")


def test_config_snapshot_records_sandbox_backend():
    """审计要能回答「这次的代码在哪跑的」：快照里必须有后端名。"""
    snapshot = Settings.load(sandbox_backend="docker").config_snapshot()
    assert snapshot["sandbox_backend"] == "docker"
    assert snapshot["search_provider"] == ""


def test_export_import_roundtrip_keeps_capability_fields(tmp_path):
    """``config export`` → ``config import`` 后值不丢（四通道里的第三条）。"""
    source = tmp_path / "agentcrew.yaml"
    source.write_text(
        "sandbox_backend: docker\n"
        "sandbox_memory_mb: 768\n"
        "search_provider: tavily\n"
        "search_max_results: 7\n"
        "docs_rag_enabled: true\n"
        "docs_rag_limit: 6\n",
        encoding="utf-8",
    )
    code, out, err = _run_cli(
        ["config", "export"], tmp_path, {"AGENTCREW_CONFIG": str(source)})
    assert code == 0, err
    doc = yaml.safe_load(out)
    assert doc["sandbox_backend"] == "docker"
    assert doc["sandbox_memory_mb"] == 768
    assert doc["search_provider"] == "tavily"
    assert doc["search_max_results"] == 7
    assert doc["docs_rag_enabled"] is True
    assert doc["docs_rag_limit"] == 6

    exported = tmp_path / "exported.yaml"
    exported.write_text(out, encoding="utf-8")
    target = tmp_path / "target.yaml"
    target.write_text("models:\n  default: mock:mock\n", encoding="utf-8")
    code, out, err = _run_cli(
        ["config", "import", str(exported)], tmp_path,
        {"AGENTCREW_CONFIG": str(target)})
    assert code == 0, err
    merged = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert merged["sandbox_backend"] == "docker"
    assert merged["search_provider"] == "tavily"
    assert merged["docs_rag_enabled"] is True
    assert merged["docs_rag_limit"] == 6


def test_default_export_does_not_emit_capability_fields(tmp_path):
    """默认配置的导出文档应与之前一致：不写入一堆「关」值。"""
    source = tmp_path / "agentcrew.yaml"
    source.write_text("models:\n  default: mock:mock\n", encoding="utf-8")
    code, out, err = _run_cli(
        ["config", "export"], tmp_path, {"AGENTCREW_CONFIG": str(source)})
    assert code == 0, err
    doc = yaml.safe_load(out)
    for key in ("sandbox_backend", "sandbox_image", "search_provider",
                "search_max_results"):
        assert key not in doc


def test_api_options_allow_search_but_not_sandbox_backend():
    assert validate_run_options({"search_provider": "brave"}) is None
    assert validate_run_options({"docs_rag_enabled": True}) is None
    assert validate_run_options({"search_max_results": 3}) is None
    assert validate_run_options({"search_max_results": 99}) is not None
    # 隔离等级不允许被单个 run 降级
    error = validate_run_options({"sandbox_backend": "local"})
    assert error is not None and "不支持的键" in error


def test_api_options_allow_docs_rag_with_bounds():
    """C5：文档 RAG 是「上下文多带几段手册」，可以按 run 覆盖——但参数有界。"""
    assert validate_run_options({"docs_rag_enabled": True}) is None
    assert validate_run_options({"docs_rag_limit": 0}) is None
    assert validate_run_options({"docs_rag_limit": 50}) is None
    assert validate_run_options({"docs_rag_limit": 99}) is not None
    assert validate_run_options({"docs_rag_limit": -1}) is not None
    assert validate_run_options({"docs_rag_vector_weight": 1.0}) is None
    assert validate_run_options({"docs_rag_vector_weight": 1.5}) is not None
    assert validate_run_options({"docs_rag_enabled": "yes"}) is not None
