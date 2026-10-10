"""A6：配置导入导出与版本化测试。

覆盖：
- config export 输出 YAML，含 config_version: 1
- config export --output 写文件
- config import 合并写入，生成 .bak 备份
- 导入文件含疑似密钥字段（api_key 作为值）被拒绝
- 版本过高报错（已在 test_config.py 覆盖）
- 导出→导入往返不丢字段
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(args: list[str], cwd: Path, env: dict | None = None) -> tuple[int, str, str]:
    """跑 main.py CLI，返回 (returncode, stdout, stderr)。"""
    import os
    full_env = dict(os.environ)
    full_env["PYTHONPATH"] = str(ROOT)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        capture_output=True, text=True, cwd=str(cwd), env=full_env,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_config_export_writes_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agentcrew.yaml").write_text(
        "models:\n  default: openai:gpt-4o-mini\n", encoding="utf-8")
    code, out, err = _run_cli(
        ["config", "export"], tmp_path,
        env={"AGENTCREW_CONFIG": str(tmp_path / "agentcrew.yaml"),
             "AGENTCREW_DEFAULT_MODEL": "openai:gpt-4o-mini"})
    assert code == 0, err
    doc = yaml.safe_load(out)
    assert doc["config_version"] == 1
    assert doc["models"]["default"] == "openai:gpt-4o-mini"


def test_config_export_to_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out_file = tmp_path / "exported.yaml"
    code, out, err = _run_cli(
        ["config", "export", "--output", str(out_file)], tmp_path,
        env={"AGENTCREW_CONFIG": str(tmp_path / "agentcrew.yaml")})
    assert code == 0, err
    assert out_file.is_file()
    doc = yaml.safe_load(out_file.read_text(encoding="utf-8"))
    assert doc["config_version"] == 1


def test_config_import_creates_backup_and_merges(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "agentcrew.yaml"
    existing.write_text(
        "models:\n  default: openai:gpt-4o-mini\n", encoding="utf-8")
    import_file = tmp_path / "import_me.yaml"
    import_file.write_text(
        yaml.safe_dump({
            "config_version": 1,
            "models": {
                "custom_providers": {
                    "myrelay": {
                        "implementation": "openai_compat",
                        "base_url": "http://localhost:9/v1",
                        "api_key_env": "MYRELAY_KEY",
                        "capabilities": {},
                    }
                }
            },
        }, allow_unicode=True),
        encoding="utf-8")
    code, out, err = _run_cli(
        ["config", "import", str(import_file)], tmp_path,
        env={"AGENTCREW_CONFIG": str(existing)})
    assert code == 0, err
    # 备份存在
    assert (tmp_path / "agentcrew.yaml.bak").is_file()
    # 合并后的文件含原有 default + 新 custom_providers
    merged = yaml.safe_load(existing.read_text(encoding="utf-8"))
    assert merged["models"]["default"] == "openai:gpt-4o-mini"
    assert "myrelay" in merged["models"]["custom_providers"]


def test_config_import_rejects_secret_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "agentcrew.yaml"
    existing.write_text("models:\n", encoding="utf-8")
    bad_import = tmp_path / "bad.yaml"
    bad_import.write_text(
        yaml.safe_dump({
            "models": {
                "providers": {
                    "openai": {"api_key": "sk-leaked-secret-value"}
                }
            },
        }, allow_unicode=True),
        encoding="utf-8")
    code, out, err = _run_cli(
        ["config", "import", str(bad_import)], tmp_path,
        env={"AGENTCREW_CONFIG": str(existing)})
    assert code != 0
    assert "api_key" in (err + out) or "密钥" in (err + out)
    # 原文件未被修改（仍是空 models）
    merged = yaml.safe_load(existing.read_text(encoding="utf-8"))
    assert not (merged.get("models") or {}).get("providers")


def test_config_import_rejects_key_value_in_api_key_env(tmp_path, monkeypatch):
    """回归（红线）：用户把真实密钥误填进 api_key_env（应填环境变量名）时，
    import 必须拒绝，绝不把密钥明文写进 agentcrew.yaml。"""
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "agentcrew.yaml"
    existing.write_text("models:\n  default: openai:gpt-4o-mini\n", encoding="utf-8")
    bad_import = tmp_path / "bad_envname.yaml"
    bad_import.write_text(
        yaml.safe_dump({
            "models": {
                "custom_providers": {
                    "myrelay": {
                        "implementation": "openai_compat",
                        "base_url": "http://x/v1",
                        "api_key_env": "sk-LEAKED-REAL-KEY-123",  # 误填成密钥值
                        "capabilities": {},
                    }
                }
            },
        }, allow_unicode=True),
        encoding="utf-8")
    code, out, err = _run_cli(
        ["config", "import", str(bad_import)], tmp_path,
        env={"AGENTCREW_CONFIG": str(existing)})
    assert code != 0
    # 原文件未被污染：仍只有 default，无 custom_providers
    merged = yaml.safe_load(existing.read_text(encoding="utf-8"))
    assert not (merged.get("models") or {}).get("custom_providers")
    # 报错不得回显该疑似密钥的值
    assert "sk-LEAKED-REAL-KEY-123" not in (out + err)


def test_config_import_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = _run_cli(
        ["config", "import", str(tmp_path / "nonexistent.yaml")], tmp_path)
    assert code != 0
