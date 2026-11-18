"""R15 多接收端密钥的落盘脱敏与日志脱敏。

R15 引入 ``webhooks`` 列表时只处理了顶层标量 ``webhook_secret``：
每条接收端的 ``secret`` 会明文进 ``config_snapshot``（随后写进 ``run.json``），
且 ``secret_values()`` 也没收集它 —— 日志/事件脱敏同样看不见这个密钥。

这里钉住两件事，防止回归：

1. 快照里**每一条** ``webhooks[*].secret`` 都是 ``***``，非密钥字段原样保留；
2. ``secret_values()`` 收集这些密钥（去重），使 ledger / 日志的 ``redact`` 生效。
"""

from __future__ import annotations

import json

from agentcrew.config import REDACTED, Settings


def test_snapshot_redacts_every_webhook_secret():
    settings = Settings(webhooks=[
        {"name": "slack", "url": "https://hooks.example.com/a",
         "secret": "slack-secret-1", "events": ["run.failed"]},
        {"name": "内部系统", "url": "https://inner.example.com/b",
         "secret": "inner-secret-2", "only_failures": True},
    ])

    snapshot = settings.config_snapshot()

    # 条目数与非密钥字段保留，便于 resume 对比配置差异
    assert len(snapshot["webhooks"]) == 2
    assert snapshot["webhooks"][0]["url"] == "https://hooks.example.com/a"
    assert snapshot["webhooks"][0]["events"] == ["run.failed"]
    assert snapshot["webhooks"][1]["name"] == "内部系统"
    assert snapshot["webhooks"][1]["only_failures"] is True
    # 每个 secret 都抹掉
    assert [item["secret"] for item in snapshot["webhooks"]] == [
        REDACTED, REDACTED]
    rendered = json.dumps(snapshot, ensure_ascii=False)
    assert "slack-secret-1" not in rendered
    assert "inner-secret-2" not in rendered


def test_empty_webhook_secret_stays_empty():
    """空 secret 用空串表示（没配签名），不该被抹成 ``***``。"""
    settings = Settings(webhooks=[{"url": "https://hooks.example.com/a",
                                  "secret": ""}])
    assert settings.config_snapshot()["webhooks"][0]["secret"] == ""


def test_secret_values_collects_webhook_secrets_and_dedups():
    settings = Settings(webhooks=[
        {"url": "https://hooks.example.com/a", "secret": "dup-secret"},
        {"url": "https://hooks.example.com/b", "secret": "dup-secret"},
        {"url": "https://hooks.example.com/c", "secret": "other-secret"},
        {"url": "https://hooks.example.com/d", "secret": "  "},
    ])

    values = settings.secret_values()

    assert "dup-secret" in values
    assert "other-secret" in values
    assert values.count("dup-secret") == 1      # 去重
    assert "" not in values and "  " not in values


def test_snapshot_does_not_mutate_original_targets():
    """脱敏必须重建对象：改快照不能把内存里的真实密钥改成 ``***``。"""
    target = {"url": "https://hooks.example.com/a", "secret": "keep-me"}
    settings = Settings(webhooks=[target])

    settings.config_snapshot()

    assert settings.webhooks[0]["secret"] == "keep-me"
