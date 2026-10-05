import io
import logging
import sys

from agentcrew.utils.logger import redact, setup_logging, get_logger


def test_redacts_sk_prefix():
    assert "sk-abc123" not in redact("key=sk-abc123 done", [])


def test_redacts_bearer_token():
    assert "xyz789" not in redact("Authorization: Bearer xyz789", [])


def test_redacts_configured_secret_values():
    assert "hunter2" not in redact("token is hunter2 here", ["hunter2"])


def test_leaves_ordinary_text_alone():
    assert redact("hello world", []) == "hello world"


def test_logger_output_is_redacted(capsys, monkeypatch):
    setup_logging("DEBUG", ["supersecret"])
    get_logger("t").info("using supersecret now")
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = capsys.readouterr()
    assert "supersecret" not in (out.out + out.err)


def test_longer_secret_is_replaced_before_shorter():
    # 短密钥先替换会把 "abc" 换成 ***，长密钥 "abcdef" 不再匹配，泄露 "def"
    assert redact("value is abcdef", ["abc", "abcdef"]) == "value is ***"


def test_redacts_secret_in_log_args(capsys):
    setup_logging("INFO", ["supersecret"])
    get_logger("t").info("using %s now", "supersecret")
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = capsys.readouterr()
    assert "supersecret" not in (out.out + out.err)


def test_redacts_secret_in_dict_log_args(capsys):
    setup_logging("INFO", ["supersecret"])
    get_logger("t").info("using %(v)s now", {"v": "supersecret"})
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = capsys.readouterr()
    assert "supersecret" not in (out.out + out.err)


def test_empty_secret_is_ignored():
    # 空串若被编译成 pattern 会匹配一切，把整段文本替换掉
    assert redact("hello", ["", "hunter2"]) == "hello"


def test_setup_logging_reuses_handler_and_follows_stderr(monkeypatch):
    root = logging.getLogger()
    first, second = io.StringIO(), io.StringIO()

    monkeypatch.setattr(sys, "stderr", first)
    setup_logging("INFO", ["sec1"])
    count = len(root.handlers)
    get_logger("t").info("sec1")

    monkeypatch.setattr(sys, "stderr", second)
    setup_logging("INFO", ["sec1"])
    assert len(root.handlers) == count  # 重复调用不叠加 handler
    assert root.level == logging.INFO
    get_logger("t").info("sec1")

    assert "sec1" not in first.getvalue() + second.getvalue()
    assert "***" in second.getvalue()  # handler 的流已切到当前 sys.stderr
