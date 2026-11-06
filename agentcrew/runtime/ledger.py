"""追加式审计日志（spec §13.2）。

一次 run 的全部审计事实都落在同一个 JSONL 文件里：任务跃迁、LLM 调用与 token
计数、产物写入、门禁裁决——Task 30 的终端看板与 Task 29 的运行报告都从这些事件
重建。设计约束：

- **只追加、不重写**：``atomic_write_text`` 那种「整文件替换」语义在这里是错的，
  历史事件不允许被覆盖。每行一个独立 JSON 对象，追加后 ``flush()`` +
  ``os.fsync()``（spec §13.2）——进程被 ``Ctrl-C`` 打断时，最后一条事件要么完整
  落盘、要么根本不出现。
- **写入严、读取宽**：``emit`` 只写结构合法的行；:meth:`Ledger.events` /
  :meth:`Ledger.count` 则在任何历史状态下都读得出东西——文件不存在（从未 emit
  或 run 目录被清理）视为零事件；空行跳过；无法解析的行（崩溃截断的尾行、写到
  一半的多字节字符）与结构未知的行（历史版本字段增删）都记 ``logging.warning``
  后跳过，绝不中断读取：审计日志正是崩溃之后最需要能读的东西。两类坏行警告措辞
  不同，便于事后诊断。
- **脱敏先于序列化**：``event`` 名、``data`` 里所有字符串（递归到嵌套容器）以及
  字典键都过 :func:`agentcrew.utils.logger.redact`；非字符串类型原样保留，不提前
  字符串化，否则 JSON 里的 int/bool 会变成字符串（spec 全局约束：API Key 绝不
  出现在日志、ledger、异常消息中）。坏行警告回显的行内容同样先脱敏再截断。
- **序列化**：``json.dumps(..., ensure_ascii=False, default=...)``。``ensure_ascii``
  保证中文在文件里可读；不可直接序列化的值（``Path``/``datetime``/``set``/``bytes``/
  自定义对象）由 ``default`` 回调 ``str()`` 后**再脱敏一次**才落盘——递归脱敏只覆盖
  字符串与容器，这些值会绕过它，而审计文件是密钥最不该留下的地方。注意：
  ``set``/``bytes``/任意对象的安全**只由序列化器的这个兜底提供**，不在
  ``_redact_value`` 里；将来任何绕过 :meth:`Ledger.emit` 的导出路径若自行序列化
  ``record.data``，必须自带同样的 ``default`` 脱敏，否则这个口子会重新打开。
  ``str`` 混入枚举如 ``TaskStatus`` 被 json 原生按其值写出；``int``/``bool``/``None``
  保持原生 JSON 类型。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from agentcrew.utils.logger import redact

_logger = logging.getLogger(__name__)

#: 坏行警告回显的最大字符数；先 ``redact`` 再截断，避免截出半截密钥。
_EXCERPT_LIMIT = 200


def _safe_lines(handle: Any, path: Any) -> Iterator[str]:
    """逐行迭代；文件系统层错误（权限 / 目录 / I/O 中断）保留已读到的部分。

    ``errors="replace"`` 只兜解码错误，**不兜 OSError**。读取契约是"无论文件
    处于什么历史状态都读得出能读的部分"，迭代中途的 I/O 错误也不该让整份
    历史读不出来。JSON 坏行仍由调用方按原规则跳过并逐条记 warning。
    """
    try:
        for line in handle:
            yield line
    except OSError as exc:
        _logger.warning("ledger 读取中断，保留已读部分 %s: %s", path, exc)


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


def _error_location(exc: ValidationError, secrets: Sequence[str]) -> str:
    """校验错误的首个字段路径（脱敏后），供坏行警告诊断字段增删。"""
    errors = exc.errors()
    if not errors:
        return "<未知>"
    path = ".".join(str(part) for part in errors[0].get("loc", ()))
    return redact(path, secrets) or "<根字段>"


class Ledger:
    """追加式 JSONL 审计日志。

    ``path`` 的父目录必须已存在（由 run 目录的创建方负责）。从未 emit 过时文件
    不存在，这不是错误：读取方把缺失的文件当作零事件（容忍的首要场景就是拿到
    一个崩溃或未开始的 run）。
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
        返回的是**从刚写入的那一行重建**的事件：``run_id``、事件名、``data`` 与
        文件逐字节一致（``default`` 兜底与字符串化的效果也在其中）。消费方
        （看板、报告）直接渲染 ``event.data`` 不会打印文件里已被替换掉的原值。
        """
        if self._closed:
            raise RuntimeError("Ledger 已关闭")
        record = LedgerEvent(
            ts=datetime.now(),
            run_id=redact(self._run_id, self._secrets),
            event=redact(event, self._secrets),
            data=_redact_value(data, self._secrets),
        )
        # default 只接住不可序列化的值（set/bytes/自定义对象…）：它们绕过
        # _redact_value，必须在字符串化的这一刻补一次脱敏，否则密钥原样落盘。
        line = json.dumps(
            record.model_dump(),
            ensure_ascii=False,
            default=lambda value: redact(str(value), self._secrets),
        ) + "\n"
        with open(self._path, "a", encoding="utf-8") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())
        # 直接返回上面的 record 不够：它里面的 set/bytes/自定义对象仍是原值，
        # 只有序列化器兜底才把它们脱敏。重建自写入行，返回值与文件不可能漂移。
        return LedgerEvent.model_validate_json(line)

    def events(self, event: str | None = None) -> Iterator[LedgerEvent]:
        """逐行产出事件；``event`` 非 None 时只产出同名事件。

        写入严、读取宽：无论文件处于什么历史状态都读得出能读的部分——文件
        不存在视为零事件；空行跳过；两类坏行跳过且各记一条措辞不同的
        ``logging.warning``（"无法解析"= 崩溃截断的尾行或半截多字节字符；
        "结构未知"= 合法 JSON 但不符合 :class:`LedgerEvent`，例如历史版本增删
        过字段——读取端绝不因它拒绝整份历史）。坏行回显内容先脱敏再截断。

        ``event`` 过滤参数先过 ``redact`` 再与存储侧比较——写入时事件名已脱敏，
        拿原始参数去比会静默过滤成空。
        """
        if event is not None:
            event = redact(event, self._secrets)
        try:
            file = open(self._path, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return  # 从未 emit 过（或 run 目录被清理）：零事件是诚实的答案
        except OSError as exc:
            # ledger.jsonl 被误建成目录 / 权限被撤 / 路径链接循环时，
            # 不能让整条读链路崩掉；按零事件处理并留一条 warning。
            _logger.warning("ledger 无法打开，按零事件处理 %s: %s", self._path, exc)
            return
        with file:
            for number, line in enumerate(
                    _safe_lines(file, self._path), start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    _logger.warning(
                        "跳过无法解析的 ledger 行（可能是崩溃截断的尾行） %s:%d: %s",
                        self._path,
                        number,
                        redact(stripped, self._secrets)[:_EXCERPT_LIMIT],
                    )
                    continue
                try:
                    parsed = LedgerEvent.model_validate(payload)
                except ValidationError as exc:
                    _logger.warning(
                        "跳过结构未知的 ledger 行 %s:%d（字段 %s）: %s",
                        self._path,
                        number,
                        _error_location(exc, self._secrets),
                        redact(stripped, self._secrets)[:_EXCERPT_LIMIT],
                    )
                    continue
                if event is None or parsed.event == event:
                    yield parsed

    def count(self, event: str | None = None) -> int:
        """统计 ``event`` 的行数；与 :meth:`events` 同一套宽容规则、同样流式。"""
        return sum(1 for _ in self.events(event))

    def close(self) -> None:
        """标记关闭；其后的 :meth:`emit` 抛 ``RuntimeError``。可重复调用。

        读取不受影响：run 结束后正是靠 :meth:`events` / :meth:`count` 重建报告。
        """
        self._closed = True


def _event_to_dict(event: LedgerEvent) -> dict[str, Any]:
    """把一条事件转成可 JSON 序列化的 dict（``ts`` 转 ISO 字符串）。"""
    return {
        "ts": event.ts.isoformat(),
        "run_id": event.run_id,
        "event": event.event,
        "data": event.data,
    }


def _as_number(value: Any) -> float:
    """把 ledger data 里的数值字段安全转成 float；缺失 / 脏值一律按 0 计。"""
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class LedgerReader:
    """只读查询器：从 ``output_dir/<run_id>/ledger.jsonl`` 检索与统计事件（P2-9）。

    写入侧的宽容规则完全复用 :meth:`Ledger.events` —— 文件不存在视为零事件、
    空行跳过、坏行记 warning 后跳过；本类只读不写，审计日志永远不会被查询路径
    改动。run_id 会被直接拼进 ``output_dir`` 之下，故所有路径先 resolve 并校验
    必须位于 ``output_dir`` 之内（拒绝 ``../x``、绝对路径等越界输入），越界 / 非法
    run_id 一律抛 :class:`ValueError`。
    """

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = Path(output_dir)

    def _resolve(self, run_id: str) -> Path:
        """``output_dir/<run_id>`` 的安全绝对路径；越界抛 ``ValueError``。"""
        root = self._output_dir.resolve()
        candidate = (self._output_dir / run_id).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise ValueError(f"非法 run_id: {run_id!r}")
        return candidate

    def _ledger(self, run_id: str) -> Ledger:
        """绑定到该 run 的 ledger.jsonl 的只读 :class:`Ledger`（未 emit 过也能用）。"""
        return Ledger(self._resolve(run_id) / "ledger.jsonl", run_id=run_id)

    def run_exists(self, run_id: str) -> bool:
        """run 目录是否存在；越界 run_id 视为不存在（不抛给调用方）。"""
        try:
            return self._resolve(run_id).is_dir()
        except ValueError:
            return False

    def query_events(
        self,
        run_id: str,
        *,
        event: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按 ``event``（可选）过滤并分页返回事件 dict 列表（按写盘顺序）。

        ``offset`` 为负按 0 计；``limit`` 小于 1 时按 1 计。匹配事件的总数请用
        :meth:`count_events`，避免调用方先取全量再 ``len()``。
        """
        offset = max(0, offset)
        limit = max(1, limit)
        all_events = list(self._ledger(run_id).events(event))
        page = all_events[offset:offset + limit]
        return [_event_to_dict(item) for item in page]

    def count_events(self, run_id: str, *, event: str | None = None) -> int:
        """与过滤条件匹配的事件总数（分页接口的 ``total``）。"""
        return self._ledger(run_id).count(event)

    def event_stats(self, run_id: str) -> dict[str, Any]:
        """统计分析：事件分布、LLM 用量聚合、任务成功率与平均时长、工具调用。

        - ``event_counts``：各事件类型计数（写什么事件都在内，未来新增事件名
          无需改这里）。
        - ``llm``：从 ``llm_call`` 事件聚合调用数 / prompt / completion / 总
          token / 估算成本（USD）。
        - ``tasks``：``task_finished / task_started`` 即任务成功率（没有
          ``task_started`` 时为 ``None``，不拿 0/0 装作 100%）；平均任务时长取
          ``task_finished.duration_ms`` 的均值（没有时为 ``None``）。
        - ``tools``：``tool_call`` 事件的总数 / 成功 / 失败计数。
        """
        counts: dict[str, int] = {}
        llm_calls = 0
        prompt_tokens = 0.0
        completion_tokens = 0.0
        cost_usd = 0.0
        started = 0
        finished = 0
        durations: list[float] = []
        tool_calls = 0
        tool_success = 0
        for item in self._ledger(run_id).events():
            counts[item.event] = counts.get(item.event, 0) + 1
            data = item.data
            if item.event == "llm_call":
                llm_calls += 1
                prompt_tokens += _as_number(data.get("prompt_tokens"))
                completion_tokens += _as_number(data.get("completion_tokens"))
                cost_usd += _as_number(data.get("estimated_cost_usd"))
            elif item.event == "task_started":
                started += 1
            elif item.event == "task_finished":
                finished += 1
                durations.append(_as_number(data.get("duration_ms")))
            elif item.event == "tool_call":
                tool_calls += 1
                if data.get("success"):
                    tool_success += 1
        success_rate = finished / started if started else None
        avg_duration = sum(durations) / len(durations) if durations else None
        return {
            "event_counts": counts,
            "llm": {
                "calls": llm_calls,
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "total_tokens": int(prompt_tokens + completion_tokens),
                "cost_usd": round(cost_usd, 6),
            },
            "tasks": {
                "started": started,
                "finished": finished,
                "success_rate": (
                    round(success_rate, 4) if success_rate is not None else None),
                "avg_duration_ms": (
                    round(avg_duration, 2) if avg_duration is not None else None),
            },
            "tools": {
                "calls": tool_calls,
                "success": tool_success,
                "failed": tool_calls - tool_success,
            },
        }
