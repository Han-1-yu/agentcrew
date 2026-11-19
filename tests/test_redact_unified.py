"""R-A 去重：``utils/redact.redact_value`` 的统一语义特征化测试。

基准 = 原 ``runtime/ledger.py::_redact_value``（两处旧实现中最全的一支）。
本文件同时钉住：

1. 统一实现自身的语义矩阵（str / dict 键值 / list / tuple / 嵌套 / 非字符串
   标量 / 空密钥 / 固定规则）；
2. ledger 与 webhook 两个调用点接的是**同一个**实现；
3. webhook spool 合并后**显式采纳**的三处行为变更（sk-/Bearer 固定规则、
   dict 键脱敏、tuple 保持 tuple 且 JSON 落盘字节与 list 相同）。

mutant 敏感性：任何一个类型分支被跳过（如键不脱敏、tuple 不递归），
这里都有对应用例失败。
"""

from __future__ import annotations

import json

from agentcrew.runtime import ledger as ledger_mod
from agentcrew.runtime import webhook as webhook_mod
from agentcrew.runtime.webhook import PendingDelivery, WebhookSpool
from agentcrew.utils.redact import redact_value

SECRET = "supersecretvalue123"


def test_string_scalar_and_known_secret():
    assert redact_value(f"prefix {SECRET} suffix", [SECRET]) == "prefix *** suffix"
    assert redact_value("plain text", []) == "plain text"


def test_fixed_patterns_apply_even_without_secrets():
    # ledger 语义：sk- / Bearer 固定规则不依赖已知密钥列表。
    assert redact_value("sk-abc123456", []) == "***"
    assert redact_value("Authorization: Bearer tok-xyz", []) == "Authorization: ***"


def test_non_string_scalars_preserved():
    for value in (1, True, None, 1.5):
        assert redact_value(value, [SECRET]) is value


def test_dict_keys_and_values_both_redacted():
    data = {f"key-{SECRET}": f"val-{SECRET}", "n": 3}
    out = redact_value(data, [SECRET])
    assert out == {"key-***": "val-***", "n": 3}
    # 容器重建：不改调用方对象。
    assert f"key-{SECRET}" in data


def test_list_and_tuple_keep_their_types():
    assert redact_value([SECRET, 1], [SECRET]) == ["***", 1]
    out = redact_value((SECRET, 1), [SECRET])
    assert out == ("***", 1)
    assert isinstance(out, tuple)


def test_nested_containers():
    data = {"a": [{"b": (SECRET,)}], "c": {"d": SECRET}}
    assert redact_value(data, [SECRET]) == {"a": [{"b": ("***",)}], "c": {"d": "***"}}


def test_empty_secret_string_is_ignored():
    # 空串 pattern 会匹配一切：必须跳过（logger.redact 的既有约定）。
    assert redact_value("text", [""]) == "text"


def test_both_callsites_share_one_implementation():
    assert ledger_mod._redact_value is redact_value
    assert webhook_mod._redact_value is redact_value


def test_ledger_emit_redacts_nested_and_keys(tmp_path):
    ledger = ledger_mod.Ledger(
        tmp_path / "ledger.jsonl", run_id="r1", secrets=[SECRET])
    event = ledger.emit("probe", **{f"k-{SECRET}": [SECRET], "n": 2})
    assert event.data == {"k-***": ["***"], "n": 2}
    line = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8")
    assert SECRET not in line


def _delivery(payload: dict) -> PendingDelivery:
    return PendingDelivery(
        event="run_finished", payload=payload, delivery="d1",
        timestamp="1700000000", target_key="t" * 16, created_at=1.0)


def test_spool_now_masks_fixed_patterns_and_dict_keys(tmp_path):
    """合并后显式采纳的行为变更：spool 也吃固定规则、键也脱敏。"""
    spool = WebhookSpool(tmp_path, secrets=[SECRET])
    path = spool.add(_delivery({
        "token": "sk-abc123456",
        f"key-{SECRET}": "v",
        "nested": {"auth": "Bearer tok-xyz"},
    }))
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "sk-abc123456" not in text
    assert SECRET not in text
    assert "tok-xyz" not in text
    data = json.loads(text)
    assert data["payload"]["token"] == "***"
    assert data["payload"]["key-***"] == "v"


def test_spool_tuple_payload_json_identical_to_list(tmp_path):
    """tuple 保持 tuple 是内存语义；JSON 落盘字节必须与旧版（tuple→list）一致。"""
    spool = WebhookSpool(tmp_path, secrets=[SECRET])
    path = spool.add(_delivery({"items": (SECRET, 1)}))
    assert path is not None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["payload"]["items"] == ["***", 1]


def test_spool_short_secret_filter_stays_at_callsite(tmp_path):
    """len>=4 的密钥过滤是调用点业务判断，统一实现不接管。"""
    spool = WebhookSpool(tmp_path, secrets=["ab"])  # 短串：不误伤正常文本
    path = spool.add(_delivery({"text": "ab normal ab text"}))
    assert path is not None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["payload"]["text"] == "ab normal ab text"
