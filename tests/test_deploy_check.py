"""目标机自检脚本的纯函数部分（不碰网络、不碰 docker）。

网络与容器那几项在本机/CI 不可控，所以这里只钉死「判据本身」：密钥形状扫描、
同盘判断、备份还原一致性，以及「没有前置条件时必须 SKIP 而不是假装 PASS」。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import deploy_check  # noqa: E402


def test_scan_secrets_catches_common_shapes():
    assert deploy_check._scan_secrets("正常日志：run started") == []
    assert deploy_check._scan_secrets("key=sk-abcdefghijklmnopqrstuvwx") != []
    assert deploy_check._scan_secrets(
        "Authorization: Bearer abcdefghijklmnop.qrstuvwx-yz") != []
    assert deploy_check._scan_secrets("api_key = 'abcdefghijklmnop1234'") != []


def test_missing_prerequisites_are_skipped_not_passed():
    """没给地址/密钥/容器/log 时必须是 SKIP——「没验」不能显示成「通过」。"""
    assert all(c.status == deploy_check.SKIP
               for c in deploy_check.check_auth("", ""))
    assert all(c.status == deploy_check.SKIP
               for c in deploy_check.check_metrics("", ""))
    assert all(c.status == deploy_check.SKIP
               for c in deploy_check.check_container(""))
    assert all(c.status == deploy_check.SKIP
               for c in deploy_check.check_log_redaction(None))


def test_plain_http_is_skipped_with_actionable_detail():
    checks = deploy_check.check_tls("http://example.com")
    assert checks[0].status == deploy_check.SKIP
    assert "反代" in checks[0].detail


def test_tls_address_honors_explicit_port_and_ipv6():
    """反代不一定在 443：自签/内网验收常用 8443 等端口。"""
    assert deploy_check._tls_address("https://localhost:18443") == ("localhost", 18443)
    assert deploy_check._tls_address("https://example.com/health") == ("example.com", 443)
    assert deploy_check._tls_address("https://[::1]:8443") == ("::1", 8443)


def test_separate_disk_detects_same_device(tmp_path):
    repo = tmp_path / "repo"
    data = repo / "output"
    data.mkdir(parents=True)
    checks = deploy_check.check_separate_disk(data, repo)
    assert checks[0].status == deploy_check.FAIL
    assert "同一个挂载点" in checks[0].detail


def test_backup_restore_roundtrip_on_fixture(tmp_path):
    """有 run 时备份还原要比对内容；还原后内容不一致必须判失败。"""
    data = tmp_path / "output"
    run = data / "20260101-000000-abcd"
    run.mkdir(parents=True)
    (run / "run.json").write_text('{"status": "completed"}', encoding="utf-8")
    (run / "ledger.jsonl").write_text('{"event": "run_started"}\n', encoding="utf-8")

    checks = deploy_check.check_backup_restore(data)
    assert checks[0].status == deploy_check.PASS
    assert "1 个 run" in checks[0].detail


def test_backup_restore_skips_when_no_runs(tmp_path):
    data = tmp_path / "empty-output"
    data.mkdir()
    checks = deploy_check.check_backup_restore(data)
    assert checks[0].status == deploy_check.SKIP
