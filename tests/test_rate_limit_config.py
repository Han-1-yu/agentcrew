"""R18：`api_rate_limit_window`（及同组两个限流参数）的配置通道回归。

此前 `api_rate_limit_window` 既没有 `AGENTCREW_*` 映射、也不在 YAML 标量白名单，
运维在 `agentcrew.yaml` 里写它会被静默忽略。这里验证四条通道都通：env、YAML、
`config export`、`config import`，以及非法值在加载期就被挡住。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentcrew.config import Settings


def _clean_env(**extra: str) -> dict[str, str]:
    """子进程用白名单环境：宿主机的 PYTHONPATH shim / 代理会干扰启动。"""
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON") and "PROXY" not in key.upper()
    }
    env.update({"PYTHONDONTWRITEBYTECODE": "1", **extra})
    return env


def test_env_variable_sets_window(monkeypatch):
    monkeypatch.setenv("AGENTCREW_API_RATE_LIMIT_WINDOW", "30")

    assert Settings.load().api_rate_limit_window == 30


def test_yaml_scalar_sets_all_three_params(tmp_path, monkeypatch):
    config = tmp_path / "agentcrew.yaml"
    config.write_text(
        "api_rate_limit_enabled: true\n"
        "api_rate_limit_max: 7\n"
        "api_rate_limit_window: 45\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTCREW_CONFIG", str(config))

    settings = Settings.load()

    assert settings.api_rate_limit_enabled is True
    assert settings.api_rate_limit_max == 7
    assert settings.api_rate_limit_window == 45


def test_env_overrides_yaml(tmp_path, monkeypatch):
    config = tmp_path / "agentcrew.yaml"
    config.write_text("api_rate_limit_window: 45\n", encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(config))
    monkeypatch.setenv("AGENTCREW_API_RATE_LIMIT_WINDOW", "20")

    assert Settings.load().api_rate_limit_window == 20


@pytest.mark.parametrize("raw", ["0", "-1", "not-a-number"])
def test_invalid_window_is_rejected_at_load(monkeypatch, raw):
    monkeypatch.setenv("AGENTCREW_API_RATE_LIMIT_WINDOW", raw)

    with pytest.raises(ValueError):
        Settings.load()


def test_defaults_unchanged():
    settings = Settings()

    assert settings.api_rate_limit_enabled is False
    assert settings.api_rate_limit_max == 100
    assert settings.api_rate_limit_window == 60


def test_config_export_includes_non_default_window(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "agentcrew.yaml"
    config.write_text(
        "api_rate_limit_enabled: true\n"
        "api_rate_limit_max: 7\n"
        "api_rate_limit_window: 45\n",
        encoding="utf-8",
    )
    env = _clean_env(AGENTCREW_CONFIG=str(config))
    result = subprocess.run(
        [sys.executable, "main.py", "config", "export"],
        cwd=root, env=env, capture_output=True, text=True,
        timeout=60, errors="replace",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "api_rate_limit_enabled: true" in result.stdout
    assert "api_rate_limit_max: 7" in result.stdout
    assert "api_rate_limit_window: 45" in result.stdout


def test_default_export_omits_rate_limit_keys(tmp_path):
    """默认配置的导出文档与 R17 之前一致，不凭空多出三个键。"""
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "agentcrew.yaml"
    config.write_text("config_version: 1\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "main.py", "config", "export"],
        cwd=root, env=_clean_env(AGENTCREW_CONFIG=str(config)),
        capture_output=True, text=True, timeout=60, errors="replace",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "api_rate_limit" not in result.stdout


def test_config_export_import_roundtrip(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "agentcrew.yaml"
    config.write_text(
        "api_rate_limit_enabled: true\n"
        "api_rate_limit_max: 7\n"
        "api_rate_limit_window: 45\n",
        encoding="utf-8",
    )
    env = _clean_env(AGENTCREW_CONFIG=str(config))
    exported = tmp_path / "exported.yaml"
    export = subprocess.run(
        [sys.executable, "main.py", "config", "export", "--output", str(exported)],
        cwd=root, env=env, capture_output=True, text=True,
        timeout=60, errors="replace",
    )
    assert export.returncode == 0, export.stdout + export.stderr
    assert exported.is_file()

    target = tmp_path / "merged.yaml"
    env2 = _clean_env(AGENTCREW_CONFIG=str(target))
    imported = subprocess.run(
        [sys.executable, "main.py", "config", "import", str(exported)],
        cwd=root, env=env2, capture_output=True, text=True,
        timeout=60, errors="replace",
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr
    merged = target.read_text(encoding="utf-8")
    assert "api_rate_limit_window: 45" in merged
    assert "api_rate_limit_max: 7" in merged
