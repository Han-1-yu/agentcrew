"""日志脱敏（spec 全局约束：API Key 不得进入日志、ledger、异常信息或 payload dump）。

``redact`` 是纯函数，未调用 ``setup_logging`` 时也可直接使用；
``setup_logging`` 在 root logger 上维护唯一一个 ``StreamHandler``，由挂在
handler 上的 filter 脱敏整条记录（渲染后的消息、异常文本与栈信息）——filter
必须挂在 handler 上，logger 级 filter 不作用于子 logger 传播上来的记录。
"""

import logging
import re
import sys
import traceback
from collections.abc import Mapping, Sequence

_REDACTED = "***"

#: OpenAI 风格密钥。计划原文写 ``{8,}``，但其测试值 ``sk-abc123`` 前缀后仅 6
#: 字符，取 6 才能满足该测试（测试是必须通过的可执行要求）。
_SK_KEY = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
#: ``Authorization: Bearer <token>`` 中的 token。
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_FIXED_PATTERNS: tuple[re.Pattern[str], ...] = (_SK_KEY, _BEARER)


def _compile_patterns(secrets: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    """固定规则在前，已知密钥字面量在后（按长度降序，短串先替换会破坏长串匹配）。

    空串跳过：空 pattern 会匹配一切。固定规则先跑，像 ``sk-`` 这样的规则会把
    含密钥的整段吃掉；若字面量先替换，会在 ``sk-supersecretkey123456`` 中间插入
    ``***``，剩余后缀就再也匹配不到 ``sk-`` 规则了。
    """
    unique = list(dict.fromkeys(secret for secret in secrets if secret))
    unique.sort(key=len, reverse=True)
    return (*_FIXED_PATTERNS, *(re.compile(re.escape(secret)) for secret in unique))


def _apply(text: str, patterns: Sequence[re.Pattern[str]]) -> str:
    for pattern in patterns:
        text = pattern.sub(_REDACTED, text)
    return text


def redact(text: str, secrets: Sequence[str]) -> str:
    """把 ``text`` 中的 ``sk-`` 密钥、Bearer token 与已知密钥值替换为 ``***``。"""
    return _apply(text, _compile_patterns(secrets))


class _RedactingFilter(logging.Filter):
    """改写日志记录中的密钥；``filter`` 挂在 handler 上以覆盖传播记录。

    对 ``getMessage()`` 渲染后的完整消息脱敏——非字符串参数、嵌套容器、以及
    ``"%s%s"`` 拆分拼接出来的密钥都能覆盖——再写回 ``msg`` 并清空 ``args``。
    异常文本与 ``stack_info`` 一并脱敏，避免 traceback 把密钥原样带出。
    """

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self.set_secrets(secrets)

    def set_secrets(self, secrets: Sequence[str]) -> None:
        self._patterns = _compile_patterns(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            # 格式串与参数不匹配：照旧交给 logging 的 handleError 报告，但先
            # 退回逐成员脱敏，避免错误转储（Message/Arguments）打印原始密钥。
            if isinstance(record.msg, str):
                record.msg = _apply(record.msg, self._patterns)
            record.args = _redact_args(record.args, self._patterns)
        else:
            record.msg = _apply(rendered, self._patterns)
            record.args = None
        self._redact_exception(record)
        return True

    def _redact_exception(self, record: logging.LogRecord) -> None:
        if record.exc_info:
            # Formatter 优先使用已设置的 exc_text，无需改动 Formatter；
            # rstrip 对齐 Formatter.formatException（它同样去掉末尾换行）
            text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = _apply(text, self._patterns).rstrip("\n")
        elif record.exc_text:
            record.exc_text = _apply(record.exc_text, self._patterns)
        if record.stack_info:
            record.stack_info = _apply(record.stack_info, self._patterns)


def _redact_args(args: object, patterns: Sequence[re.Pattern[str]]) -> object:
    """``record.args``：单映射时为 dict 本体，多参数时为 tuple（logging 语义）。"""
    if isinstance(args, Mapping):
        return {key: _redact_value(value, patterns) for key, value in args.items()}
    if isinstance(args, tuple):
        return tuple(_redact_value(value, patterns) for value in args)
    return args


def _redact_value(value: object, patterns: Sequence[re.Pattern[str]]) -> object:
    return _apply(value, patterns) if isinstance(value, str) else value


class _RedactingStreamHandler(logging.StreamHandler):
    """可重复配置的脱敏 handler：识别自身、更新密钥、跟随当前 stderr。"""

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__(sys.stderr)
        self._redacting_filter = _RedactingFilter(secrets)
        self.addFilter(self._redacting_filter)

    def set_secrets(self, secrets: Sequence[str]) -> None:
        self._redacting_filter.set_secrets(secrets)


def setup_logging(level: str, secrets: Sequence[str]) -> None:
    """在 root logger 上装好脱敏日志；可重复调用，handler 始终只有一个。

    ``StreamHandler`` 在构造时绑定当时的 ``sys.stderr``，因此每次调用都把流更新
    为当前 ``sys.stderr``（capsys 等会在测试间替换它）。
    """
    root = logging.getLogger()
    root.setLevel(level)
    handler = next(
        (h for h in root.handlers if isinstance(h, _RedactingStreamHandler)), None
    )
    if handler is None:
        handler = _RedactingStreamHandler(secrets)
        root.addHandler(handler)
    else:
        handler.set_secrets(secrets)
    if handler.stream is not sys.stderr:
        # 不用 StreamHandler.setStream()：它会先 flush 旧流，而旧流可能已被
        # capsys 之类的捕获器关闭（flush 抛 ValueError）。emit 每次写入后都已
        # flush，直接换流不会丢数据。
        handler.stream = sys.stderr
    handler.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """返回命名 logger（``logging.getLogger`` 的薄封装）。"""
    return logging.getLogger(name)
