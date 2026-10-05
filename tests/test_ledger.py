import json
import logging
from datetime import datetime

import pytest
from pydantic import ValidationError

from agentcrew.runtime.ledger import Ledger, LedgerEvent


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.jsonl", run_id="R-1")


def test_emit_appends_one_json_line(ledger, tmp_path):
    ledger.emit("run_started", goal="g")
    ledger.emit("task_started", task_id="T-1")
    lines = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "run_started"
    assert json.loads(lines[1])["data"]["task_id"] == "T-1"


def test_events_filters_by_name(ledger):
    ledger.emit("a"); ledger.emit("b"); ledger.emit("a")
    assert [e.event for e in ledger.events("a")] == ["a", "a"]


def test_count(ledger):
    ledger.emit("a"); ledger.emit("a"); ledger.emit("b")
    assert ledger.count("a") == 2 and ledger.count("zzz") == 0


def test_secrets_redacted_in_data(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit("llm_call", api_key="supersecret", note="Bearer abc123def")
    raw = (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "supersecret" not in raw and "abc123def" not in raw


def test_secrets_redacted_in_event_name(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit("evt", payload="x")
    assert "supersecret" not in (tmp_path / "l.jsonl").read_text(encoding="utf-8")


def test_non_serializable_values_are_stringified(ledger, tmp_path):
    from pathlib import Path
    ledger.emit("x", p=Path("a/b"))
    line = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads(line)["data"]["p"]


def test_emit_after_close_raises(ledger):
    ledger.close()
    with pytest.raises(Exception):
        ledger.emit("x")


def test_each_line_flushed_immediately(ledger, tmp_path):
    ledger.emit("a")
    # 不 close 也能读到
    assert (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip() != ""


# ---- 以下为补充用例：钉住 brief 之外由决议明确的边界 ----


def test_emit_returns_ledger_event_with_metadata(ledger):
    before = datetime.now()
    ev = ledger.emit("run_started", goal="g")
    after = datetime.now()
    assert isinstance(ev, LedgerEvent)
    assert ev.run_id == "R-1"
    assert ev.event == "run_started"
    assert ev.data == {"goal": "g"}
    assert before <= ev.ts <= after


def test_events_round_trips_all_fields(ledger):
    emitted = ledger.emit("task_started", task_id="T-1", attempt=1)
    got = list(ledger.events())[0]
    assert (got.run_id, got.event, got.data) == ("R-1", "task_started", {"task_id": "T-1", "attempt": 1})
    assert got.ts == emitted.ts  # 微秒精度经 JSON 往返不丢失


def test_blank_lines_are_skipped(ledger, tmp_path):
    ledger.emit("a")
    with open(tmp_path / "ledger.jsonl", "a", encoding="utf-8") as file:
        file.write("\n   \n")
    ledger.emit("b")
    assert [e.event for e in ledger.events()] == ["a", "b"]
    assert ledger.count("a") == 1


def test_malformed_tail_is_skipped_not_raised(ledger, tmp_path, caplog):
    ledger.emit("a")
    with open(tmp_path / "ledger.jsonl", "a", encoding="utf-8") as file:
        file.write('{"partial":\n')  # 模拟崩溃留下的截断尾行
    with caplog.at_level(logging.WARNING):
        assert [e.event for e in ledger.events()] == ["a"]
        assert ledger.count("a") == 1
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_truncated_multibyte_tail_is_skipped_not_raised(ledger, tmp_path, caplog):
    ledger.emit("a", text="修复登录问题")
    with open(tmp_path / "ledger.jsonl", "ab") as file:
        file.write("修".encode("utf-8")[:2])  # 崩溃时写到一半的多字节字符
    with caplog.at_level(logging.WARNING):
        assert [e.event for e in ledger.events()] == ["a"]
        assert ledger.count("a") == 1
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_never_written_ledger_reads_as_empty(tmp_path):
    lg = Ledger(tmp_path / "absent.jsonl", run_id="R-1")
    assert list(lg.events()) == []
    assert list(lg.events("a")) == []
    assert lg.count("a") == 0


def test_wrong_shape_line_is_skipped_not_raised(ledger, tmp_path, caplog):
    ledger.emit("a")
    with open(tmp_path / "ledger.jsonl", "a", encoding="utf-8") as file:
        file.write('{"ts": "2026-01-01T00:00:00", "run_id": "R-1", "event": "b", "data": {}, "unexpected": 1}\n')
        file.write('{"nope": 1}\n')
    ledger.emit("c")  # 坏行之后的好行仍要读得出来
    with caplog.at_level(logging.WARNING):
        assert [e.event for e in ledger.events()] == ["a", "c"]
        assert len(caplog.records) == 2  # 单次遍历：每条坏行恰好一条警告
    assert ledger.count("a") == 1 and ledger.count("b") == 0 and ledger.count("c") == 1


def test_wrong_shape_line_excerpt_is_redacted(ledger, tmp_path, caplog):
    with open(tmp_path / "ledger.jsonl", "a", encoding="utf-8") as file:
        file.write('{"event": "x", "api_key": "sk-abc123456", "extra": 1}\n')
    with caplog.at_level(logging.WARNING):
        assert list(ledger.events()) == []
    assert "sk-abc123456" not in caplog.text
    assert "结构未知" in caplog.text


def test_count_agrees_with_len_events_across_bad_lines(ledger, tmp_path):
    ledger.emit("a")
    with open(tmp_path / "ledger.jsonl", "a", encoding="utf-8") as file:
        file.write('{"partial":\n')  # 无法解析（截断尾行）
        file.write('{"nope": 1}\n')  # 结构未知
        file.write("\n   \n")  # 空行
        file.write("修复\n")  # 合法 UTF-8 但非法 JSON
    ledger.emit("a")
    assert ledger.count("a") == len(list(ledger.events("a"))) == 2
    assert len(list(ledger.events())) == 2


def test_bad_line_warnings_distinguish_truncated_from_unknown_shape(ledger, tmp_path, caplog):
    with open(tmp_path / "ledger.jsonl", "a", encoding="utf-8") as file:
        file.write('{"partial":\n')
        file.write('{"nope": 1}\n')
    with caplog.at_level(logging.WARNING):
        assert list(ledger.events()) == []
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert "无法解析" in messages[0]
    assert "结构未知" in messages[1]


def test_malformed_line_excerpt_is_redacted(tmp_path, caplog):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    with open(tmp_path / "l.jsonl", "a", encoding="utf-8") as file:
        file.write('{"api_key": "supersecret",\n')  # 坏行里恰好含密钥
    with caplog.at_level(logging.WARNING):
        assert list(lg.events()) == []
    assert "supersecret" not in caplog.text


def test_nested_secret_values_are_redacted(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit(
        "llm_call",
        request={"headers": {"authorization": "Bearer abc123def"}, "prompts": ["supersecret"]},
    )
    raw = (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "supersecret" not in raw
    assert "abc123def" not in raw


def test_secret_in_dict_key_is_redacted(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit("x", payload={"supersecret": "v", "nested": {"sk-abc123456": 1}})
    raw = (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "supersecret" not in raw
    assert "sk-abc123456" not in raw


def test_secret_in_event_name_is_redacted(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit("leak-supersecret", payload="x")
    raw = (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "supersecret" not in raw
    assert json.loads(raw)["event"] == "leak-***"


def test_set_value_is_redacted_before_stringification(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit("x", as_set={"supersecret"})
    raw = (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "supersecret" not in raw


def test_bytes_value_is_redacted_before_stringification(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit("x", as_bytes=b"supersecret")
    raw = (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "supersecret" not in raw


def test_object_str_embedding_secret_is_redacted(tmp_path):
    class Payload:
        def __str__(self):
            return "Authorization: Bearer supersecret"

    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit("x", obj=Payload())
    raw = (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "supersecret" not in raw


def test_events_filter_applies_redaction_like_storage(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit("leak-supersecret", payload="x")
    assert [e.event for e in lg.events("leak-supersecret")] == ["leak-***"]
    assert lg.count("leak-supersecret") == 1


def test_run_id_secret_is_redacted_in_file(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="sk-abc123456", secrets=["sk-abc123456"])
    ev = lg.emit("a")
    raw = (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "sk-abc123456" not in raw
    assert ev.run_id == "***"


def test_returned_event_data_has_no_secret_for_non_serializable_values(tmp_path):
    class Payload:
        def __str__(self):
            return "Authorization: Bearer supersecret"

    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    ev = lg.emit("x", as_set={"supersecret"}, as_bytes=b"supersecret", obj=Payload())
    # 消费方（看板/报告）直接渲染 event.data：返回的事件必须与文件同样干净
    assert "supersecret" not in repr(ev.data)
    assert ev.data["as_set"] == "{'***'}"
    assert ev.data["as_bytes"] == "b'***'"
    assert ev.data["obj"] == "Authorization: ***"


def test_returned_event_data_agrees_with_file_for_nested_dict(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    ev = lg.emit("x", nested={"a": [1, {"k": "supersecret"}], "b": True})
    stored = json.loads((tmp_path / "l.jsonl").read_text(encoding="utf-8"))
    assert stored["data"] == ev.data
    assert ev.data == {"nested": {"a": [1, {"k": "***"}], "b": True}}


def test_redaction_preserves_non_string_types(ledger, tmp_path):
    ledger.emit("x", count=3, ratio=0.5, ok=True, nothing=None)
    data = json.loads((tmp_path / "ledger.jsonl").read_text(encoding="utf-8"))["data"]
    assert data == {"count": 3, "ratio": 0.5, "ok": True, "nothing": None}


def test_chinese_text_is_written_unescaped(ledger, tmp_path):
    ledger.emit("run_started", goal="修复登录问题")
    raw = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8")
    assert "修复登录问题" in raw


def test_emit_after_close_raises_runtime_error_with_message(ledger):
    ledger.close()
    with pytest.raises(RuntimeError, match="Ledger 已关闭"):
        ledger.emit("x")


def test_close_is_idempotent_and_reads_still_work(ledger):
    ledger.emit("a")
    ledger.close()
    ledger.close()
    assert ledger.count("a") == 1
    assert [e.event for e in ledger.events()] == ["a"]


def test_emit_fsyncs_each_line(ledger, monkeypatch):
    import agentcrew.runtime.ledger as ledger_module

    calls = []
    monkeypatch.setattr(ledger_module.os, "fsync", lambda fd: calls.append(fd))
    ledger.emit("a")
    assert len(calls) == 1


def test_ledger_event_forbids_extra_fields():
    with pytest.raises(ValidationError):
        LedgerEvent(ts=datetime.now(), run_id="R-1", event="x", data={}, extra=1)
