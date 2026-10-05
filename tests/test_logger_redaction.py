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


def test_exception_traceback_is_redacted(capsys):
    setup_logging("INFO", ["supersecret"])
    try:
        raise RuntimeError("boom sk-supersecretkey123456")
    except RuntimeError:
        get_logger("t").exception("request failed")
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = capsys.readouterr()
    assert "request failed" in out.err  # 消息本身仍正常输出
    assert "Traceback (most recent call last)" in out.err  # 异常文本确实被渲染
    assert "sk-supersecretkey123456" not in (out.out + out.err)


def test_exception_log_ends_with_single_newline(capsys):
    # Formatter.formatException 会去掉 traceback 末尾的换行；exc_text 若不照做，
    # 每条异常日志都会多出一个空行
    setup_logging("INFO", [])
    try:
        raise RuntimeError("plain failure")
    except RuntimeError:
        get_logger("t").exception("boom")
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = capsys.readouterr()
    assert out.err.endswith("RuntimeError: plain failure\n")


def test_overlapping_configured_secret_leaves_no_key_suffix(capsys):
    # 运行时 settings.secret_values() 常包含正在被脱敏的那把密钥；若字面量先替换，
    # 会在密钥中间插入 ***，sk- 规则就吃不掉剩余后缀
    setup_logging("INFO", ["supersecret"])
    get_logger("t").info("boom sk-supersecretkey123456")
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = capsys.readouterr()
    assert "sk-supersecretkey123456" not in out.err
    assert "key123456" not in out.err  # 后缀也不能残留
    assert out.err == "boom ***\n"  # 整条消息只剩占位符


def test_redacts_key_inside_non_str_arg(capsys):
    setup_logging("INFO", ["supersecret"])

    class Payload:
        def __str__(self):
            return "boom sk-supersecretkey123456"

    get_logger("t").error("failed: %s", Payload())
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = capsys.readouterr()
    assert "failed:" in out.err
    assert "sk-supersecretkey123456" not in (out.out + out.err)


def test_redacts_split_arg_secret(capsys):
    # 逐参数脱敏时 "sk-abc" 与 "123" 单独都不匹配，拼起来才是密钥
    setup_logging("INFO", ["supersecret"])
    get_logger("t").info("%s%s", "sk-abc", "123")
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = capsys.readouterr()
    assert "sk-abc123" not in (out.out + out.err)


def test_malformed_args_do_not_escape_filter(capsys, monkeypatch):
    # 渲染消息失败（格式串与参数不匹配）时，filter 不能把异常抛给调用方。
    # pytest 的 handler 会按 raiseExceptions 重抛 emit 失败，故关掉它来观察
    # logging 原生的“报告错误但不打断调用方”行为。
    monkeypatch.setattr(logging, "raiseExceptions", False)
    setup_logging("INFO", ["supersecret"])
    get_logger("t").info("%d", "supersecret")
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = capsys.readouterr()
    assert "supersecret" not in (out.out + out.err)


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
