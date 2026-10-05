"""追加式审计日志（spec §13.2）。

一次 run 的全部审计事实都落在同一个 JSONL 文件里：任务跃迁、LLM 调用与 token
计数、产物写入、门禁裁决——Task 30 的终端看板与 Task 29 的运行报告都从这些事件
重建。设计约束：

- **只追加、不重写**：``atomic_write_text`` 那种「整文件替换」语义在这里是错的，
  历史事件不允许被覆盖。每行一个独立 JSON 对象，追加后 ``flush()`` +
  ``os.fsync()``（spec §13.2）——进程被 ``Ctrl-C`` 打断时，最后一条事件要么完整
  落盘、要么根本不出现。
- **崩溃后的截断尾行是预期状态**：:meth:`Ledger.events` / :meth:`Ledger.count`
  逐行读取，空行直接跳过，``json.JSONDecodeError`` 的坏行记 ``logging.warning``
  后跳过，绝不中断读取（那会让整个 run 的历史不可读）。
- **脱敏先于序列化**：``event`` 名、``data`` 里所有字符串（递归到嵌套容器）以及
  字典键都过 :func:`agentcrew.utils.logger.redact`；非字符串类型原样保留，不提前
  字符串化，否则 JSON 里的 int/bool 会变成字符串（spec 全局约束：API Key 绝不
  出现在日志、ledger、异常消息中）。坏行警告回显的行内容同样先脱敏再截断。
- **序列化**：``json.dumps(..., ensure_ascii=False, default=str)``。``ensure_ascii``
  保证中文在文件里可读；``default=str`` 让 ``Path``/``datetime`` 等不可直接序列化
  的值存活（``str`` 混入枚举如 ``TaskStatus`` 会被 json 原生按其值写出）。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentcrew.utils.logger import redact

_logger = logging.getLogger(__name__)

#: 坏行警告回显的最大字符数；先 ``redact`` 再截断，避免截出半截密钥。
_EXCERPT_LIMIT = 200


class LedgerEvent(BaseModel):
    """一条已写入 ledger 的事件。"""

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    run_id: str
    event: str
    data: dict[str, Any]


def _redact_value(value: Any, secrets: Sequence[str]) -> Any:
    """递归脱敏：字符串过 ``redact``，容器**重建**（不改调用方对象），其余原样。

    字符串之外的标量不转字符串：``emit`` 写盘时 int 仍写 int、bool 仍写 bool；
    键同样过一遍——键和值一样可能夹带密钥（如 ``{"sk-...": ...}``）。
    """
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, Mapping):
        return {
            _redact_value(key, secrets): _redact_value(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, secrets) for item in value)
    return value


class Ledger:
    """追加式 JSONL 审计日志。

    ``path`` 的父目录必须已存在（由 run 目录的创建方负责）；未 emit 过时文件
    不存在，此时读取会抛 ``FileNotFoundError``——空 run 应当在 ``run_started``
    就落盘，读不到文件说明 run 目录本身有问题。
    """

    def __init__(
        self, path: Path, *, run_id: str, secrets: Sequence[str] = ()
    ) -> None:
        """``secrets`` 冻结为元组，之后调用方的列表变更不影响本实例。"""
        self._path = Path(path)
        self._run_id = run_id
        self._secrets: tuple[str, ...] = tuple(secrets)
        self._closed = False

    def emit(self, event: str, **data: Any) -> LedgerEvent:
        """追加一条事件并返回它（已脱敏的版本）。

        每次调用 ``open(path, "a", encoding="utf-8")`` 独立追加一行，写后
        ``flush()`` 再 ``os.fsync()``：函数返回时事件已落盘，不依赖 ``close()``。
        返回的对象即写入内容，供看板等实时消费方使用——调用方拿到的应当是脱敏
        后的数据，不能因为「还没落盘」而泄露原值。
        """
        if self._closed:
            raise RuntimeError("Ledger 已关闭")
        record = LedgerEvent(
            ts=datetime.now(),
            run_id=self._run_id,
            event=redact(event, self._secrets),
            data=_redact_value(data, self._secrets),
        )
        line = json.dumps(record.model_dump(), ensure_ascii=False, default=str) + "\n"
        with open(self._path, "a", encoding="utf-8") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())
        return record

    def events(self, event: str | None = None) -> Iterator[LedgerEvent]:
        """逐行产出事件；``event`` 非 None 时只产出同名事件。

        空行跳过；``json.JSONDecodeError`` 的坏行（如崩溃留下的截断尾行）记
        ``logging.warning`` 后跳过——读取过程绝不因坏行中断。合法 JSON 但字段
        不符合 :class:`LedgerEvent` 结构的行会抛 pydantic 校验错误：文件只由
        :meth:`emit` 写入，那意味着有人手工改写了审计文件，应当暴露而非静默。
        """
        with open(self._path, "r", encoding="utf-8") as file:
            for number, line in enumerate(file, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    _logger.warning(
                        "跳过损坏的 ledger 行 %s:%d: %s",
                        self._path,
                        number,
                        redact(stripped, self._secrets)[:_EXCERPT_LIMIT],
                    )
                    continue
                parsed = LedgerEvent.model_validate(payload)
                if event is None or parsed.event == event:
                    yield parsed

    def count(self, event: str) -> int:
        """统计 ``event`` 的行数；与 :meth:`events` 同一套跳过规则，全程流式。"""
        return sum(1 for _ in self.events(event))

    def close(self) -> None:
        """标记关闭；其后的 :meth:`emit` 抛 ``RuntimeError``。可重复调用。

        读取不受影响：run 结束后正是靠 :meth:`events` / :meth:`count` 重建报告。
        """
        self._closed = True
