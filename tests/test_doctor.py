"""``doctor`` 子命令测试（E-1）。

覆盖 brief 要求的六条：有效配置 ok=True、缺 Key 给 warn/fail、output_dir 不可写
fail、--json 输出合法 JSON、fail → 退出码 3、Provider 连通性（注入假 health_check）。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

import main as cli
from agentcrew.cli.doctor import (
    DoctorReport,
    print_report,
    run_diagnostics,
)
from agentcrew.config import Settings
from agentcrew.crew.crew import EXIT_CODES
from agentcrew.models.factory import HealthRecord

from tests.conftest import API_KEY_ENVS


def _healthy(alias: str, status: str = "healthy") -> HealthRecord:
    """构造一条假的健康探测结果。"""
    return HealthRecord(
        alias=alias, status=status, latency_ms=1.0,
        last_check=datetime.now(),
    )


# ── run_diagnostics 纯函数 ────────────────────────────────────────────────


async def test_valid_mock_config_is_ok(tmp_path):
    """有效配置（mock 模式）→ ok=True，全部检查 pass/warn、无 fail。"""
    settings = Settings(mock=True, output_dir=tmp_path / "o")

    async def fake_health(alias: str) -> HealthRecord:
        return _healthy(alias)

    report = await run_diagnostics(settings, health_check=fake_health)
    assert report.ok is True
    assert all(c.status in ("pass", "warn") for c in report.checks)
    names = {c.name for c in report.checks}
    assert "python" in names and "api_keys" in names and "output_dir" in names


async def test_missing_api_key_is_fail_or_warn(tmp_path, monkeypatch):
    """非 mock 且没有任何 Key → api_keys 检查为 warn/fail，整体不 ok。"""
    for env in API_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    settings = Settings(mock=False, output_dir=tmp_path / "o",
                        default_model="openai:gpt-4o-mini")

    async def fake_health(alias: str) -> HealthRecord:
        return _healthy(alias)

    report = await run_diagnostics(settings, health_check=fake_health)
    api = next(c for c in report.checks if c.name == "api_keys")
    assert api.status in ("warn", "fail")
    assert "OPENAI_API_KEY" in api.message
    assert report.ok is False


async def test_output_dir_being_a_file_is_fail(tmp_path):
    """output_dir 指向一个普通文件（不是目录）→ fail。"""
    bogus = tmp_path / "not-a-dir.txt"
    bogus.write_text("x", encoding="utf-8")
    settings = Settings(mock=True, output_dir=bogus)

    async def fake_health(alias: str) -> HealthRecord:
        return _healthy(alias)

    report = await run_diagnostics(settings, health_check=fake_health)
    out = next(c for c in report.checks if c.name == "output_dir")
    assert out.status == "fail"
    assert report.ok is False


async def test_output_dir_created_when_missing(tmp_path):
    """output_dir 不存在时自动创建 → pass。"""
    target = tmp_path / "fresh" / "dir"
    assert not target.exists()
    settings = Settings(mock=True, output_dir=target)

    async def fake_health(alias: str) -> HealthRecord:
        return _healthy(alias)

    report = await run_diagnostics(settings, health_check=fake_health)
    out = next(c for c in report.checks if c.name == "output_dir")
    assert out.status == "pass"
    assert target.is_dir()


async def test_custom_provider_default_model_recognized(tmp_path, monkeypatch):
    """A1 回归：default_model 指向自定义 provider 时 config 检查不得报「别名非法」。"""
    for env in API_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MYRELAY_KEY", "sk-relay-1")
    settings = Settings(
        mock=False, output_dir=tmp_path / "o",
        default_model="myrelay:v1",
        custom_providers={
            "myrelay": {
                "implementation": "openai_compat",
                "base_url": "http://localhost:9/v1",
                "api_key_env": "MYRELAY_KEY",
                "capabilities": {},
            }
        },
    )

    async def fake_health(alias: str) -> HealthRecord:
        return _healthy(alias)

    report = await run_diagnostics(settings, health_check=fake_health)
    cfg = next(c for c in report.checks if c.name == "config")
    assert cfg.status == "pass", cfg.message
    # Key 检查应认得自定义 provider 已配 Key，不再把它当未知 provider 跳过
    api = next(c for c in report.checks if c.name == "api_keys")
    assert api.status == "pass", api.message


# ── Provider 连通性（注入假 health_check）─────────────────────────────────


async def test_health_unavailable_maps_to_fail(tmp_path):
    """health_check 返回 unavailable → 该检查 fail、整体不 ok。"""
    settings = Settings(mock=True, output_dir=tmp_path / "o",
                       default_model="openai:down")

    async def fake_health(alias: str) -> HealthRecord:
        return HealthRecord(
            alias=alias, status="unavailable", latency_ms=5000.0,
            last_check=datetime.now(), error="ConnectionError: refused",
        )

    report = await run_diagnostics(settings, health_check=fake_health)
    pc = next(c for c in report.checks if c.name == "provider:openai:down")
    assert pc.status == "fail"
    assert report.ok is False


async def test_health_degraded_is_warn_but_not_blocking(tmp_path):
    """degraded → warn，不影响整体 ok（警告不阻断）。"""
    settings = Settings(mock=True, output_dir=tmp_path / "o",
                       default_model="openai:slow")

    async def fake_health(alias: str) -> HealthRecord:
        return HealthRecord(
            alias=alias, status="degraded", latency_ms=3000.0,
            last_check=datetime.now(),
        )

    report = await run_diagnostics(settings, health_check=fake_health)
    pc = next(c for c in report.checks if c.name == "provider:openai:slow")
    assert pc.status == "warn"
    assert report.ok is True


async def test_health_exception_maps_to_fail(tmp_path):
    """health_check 抛异常 → 记一条 fail，而不是把整个诊断崩掉。"""
    settings = Settings(mock=True, output_dir=tmp_path / "o",
                       default_model="openai:boom")

    async def fake_health(alias: str) -> HealthRecord:
        raise RuntimeError("network down")

    report = await run_diagnostics(settings, health_check=fake_health)
    pc = next(c for c in report.checks if c.name == "provider:openai:boom")
    assert pc.status == "fail"
    assert report.ok is False


# ── 终端输出 ───────────────────────────────────────────────────────────────


def test_print_report_uses_symbols(capsys):
    report = DoctorReport.model_validate({
        "ok": False,
        "checks": [
            {"name": "python", "status": "pass", "message": "Python 3.14"},
            {"name": "api_keys", "status": "fail", "message": "缺 OPENAI_API_KEY"},
        ],
    })
    print_report(report)
    out = capsys.readouterr().out
    assert "python" in out and "fail" not in out.split("api_keys")[0]
    assert "OPENAI_API_KEY" in out


# ── CLI 集成（退出码 / --json）────────────────────────────────────────────


def test_doctor_json_output_is_valid(tmp_path, monkeypatch, capsys):
    """``--json`` 输出可被 json.loads 解析；mock 模式下退出码 0。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    code = cli.main(["doctor", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)  # 不抛即合法 JSON
    assert data["ok"] is True
    assert any(c["name"] == "python" for c in data["checks"])
    assert code == 0


def test_doctor_fail_exits_3(tmp_path, monkeypatch, capsys):
    """有 fail 项（非 mock 且无 Key）→ 退出码 3（EXIT_CODES.config）。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "false")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    for env in API_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    code = cli.main(["doctor"])
    assert code == EXIT_CODES["config"] == 3
    out = capsys.readouterr().out
    assert "api_keys" in out


def test_doctor_human_readable_on_success(tmp_path, monkeypatch, capsys):
    """默认（非 --json）输出人类可读报告，退出码 0。"""
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    code = cli.main(["doctor"])
    assert code == 0
    out = capsys.readouterr().out
    assert "总体" in out
    assert "python" in out
