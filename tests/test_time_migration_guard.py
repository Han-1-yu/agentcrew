"""D-14 守护：禁止新增 naive 时间落盘。

**背景**：`run.json` / `review` / 事件里的时间戳目前仍是 `datetime.now()`
（naive 本地时间）。全量迁移到 UTC 需要贯穿 RunRecord / RunStats / TaskResult /
ReviewRecord / ApprovalRequest / Ledger 六个模型，并同步改掉所有「相减」的位置
（`report.py` 的时长、`board_view.py` 的耗时、`crew.py` 的 duration_ms）——
一次做完回归面太大，所以按批次推进。

**这个测试的作用**：把当前的落盘点钉成一份**只减不增**的清单。新增一处
`x.updated_at = datetime.now()` 会让它失败，迁移掉一处则可以把期望值调小。
这样「时间统一」这件长期工作不会在赶功能时被悄悄回退。

R64 起扫描范围**包含 `main.py`**：CLI 是唯一在 run 之外改写 `run.json` 的入口
（`cancel`），此前不在扫描范围内，于是它的 `updated_at = datetime.now()` 一直
没被发现——实跑里表现为同一个 `run.json` 内「创建是 UTC、更新是本地」。

2026-11-18 审计 P2-2：守卫只认**字面**写法，`entity.py` 用局部变量中转
（`now = datetime.now()` → `updated_at=now`）就整条绕过去了。本文件因此加了
第二道扫描 `_scan_indirect()`（见下），把「变量桥」这种形式也钉住。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _ROOT / "agentcrew"

#: 包外但**同样会写落盘时间**的入口：CLI 是唯一在 run 之外改写 ``run.json`` 的
#: 地方（``cancel``），长期不在扫描范围内 —— 于是它用 ``datetime.now()`` 写
#: ``updated_at`` 一直没被发现（R64 实跑抓到：同一文件里 created_at 是 UTC、
#: updated_at 是本地时间，东八区看着「更新比创建晚 8 小时」）。
_CLI_ENTRY = _ROOT / "main.py"

#: 落盘时间字段的赋值模式：``x.updated_at = datetime.now()`` 与 ``created_at=datetime.now()`` 两种写法。
_WRITE_PATTERN = re.compile(
    r"(?:\.(?:updated_at|created_at|started_at|finished_at|decided_at|expires_at"
    r"|written_at)\s*="
    r"|(?:updated_at|created_at|started_at|finished_at|decided_at|expires_at"
    r"|written_at)\"?\s*[=:])"
    r"\s*datetime\.now\(\)"
)

#: 基线已是 0：D-14 于 2026-11-12 完成迁移。任何新增的 naive 落盘都直接失败。
BASELINE = 0

#: 允许的文件——只统计这些文件里的匹配，避免把 `datetime.now()` 的纯业务用法
#: （如 run_id 生成、日志时间）算进来。
_ALLOWED_FILES = frozenset({
    "crew/crew.py",
    "gate/gatekeeper.py",
    "gate/reviewer.py",
    "gate/static_checks.py",
    "runtime/checkpoint.py",
    "runtime/approvals.py",
    "artifacts/store.py",
})


def _scan() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    targets: list[tuple[Path, str]] = []
    for path in _PACKAGE.rglob("*.py"):
        rel = path.relative_to(_PACKAGE).as_posix()
        if rel in _ALLOWED_FILES:
            targets.append((path, rel))
    if _CLI_ENTRY.is_file():
        targets.append((_CLI_ENTRY, "main.py"))
    for path, rel in targets:
        hits = [
            (lineno, line.strip())
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1)
            if _WRITE_PATTERN.search(line)
        ]
        if hits:
            found[rel] = hits
    return found


def _scanned_files() -> set[str]:
    """当前扫描范围（相对路径）。守卫有效性断言用它，避免「改了范围却没人测」。"""
    names = {
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if path.relative_to(_PACKAGE).as_posix() in _ALLOWED_FILES
    }
    if _CLI_ENTRY.is_file():
        names.add("main.py")
    return names


def test_naive_timestamp_writes_do_not_grow():
    """落盘点不得超过基线。新增时间字段时请用 ``clock.utc_now()``。"""
    found = _scan()
    assert "main.py" in _scanned_files(), "CLI 入口必须进扫描范围（cancel 就在这里）"
    total = sum(len(v) for v in found.values())
    detail = "\n".join(
        f"  {rel}: {len(hits)} 处 → " + "; ".join(f"L{n}" for n, _ in hits)
        for rel, hits in sorted(found.items())
    )
    assert total <= BASELINE, (
        f"新增了 naive 时间落盘（{total} > 基线 {BASELINE}）。\n"
        f"新代码请用 agentcrew.runtime.clock.utc_now()：\n{detail}"
    )


def test_guard_pattern_matches_known_write_forms():
    """守卫自身有效性：正则必须命中各种落盘写法，否则它是个假守卫。

    基线为 0 之后不能再靠「真实代码里有匹配」来自检，改为直接喂样例字符串。
    """
    assert _WRITE_PATTERN.search("record.updated_at = datetime.now()")
    assert _WRITE_PATTERN.search("created_at=datetime.now()")
    assert _WRITE_PATTERN.search("written_at=datetime.now()")
    assert _WRITE_PATTERN.search('"decided_at": datetime.now()')
    # 纯局部变量（run_id 生成、日志时间）不算落盘，不应误报
    assert not _WRITE_PATTERN.search("now = datetime.now()")


def test_clock_module_is_the_designated_entry():
    """A-7 引入的统一入口必须存在且可导入（迁移的落点）。"""
    from agentcrew.utils.clock import (  # noqa: F401
        elapsed_since,
        parse_stamp,
        utc_now,
    )
    assert utc_now().tzinfo is not None


# ── 第二道扫描：变量中转换走的 naive 落盘（P2-2 / P3-3）─────────────────────

#: ``now = datetime.now()``（可带类型标注）。命中行只是「桥」，不是落盘。
_LOCAL_CLOCK_ASSIGN = re.compile(
    r"^[ \t]*(?P<var>[A-Za-z_]\w*)[ \t]*(?::[ \t]*[\w.\[\] |]+[ \t]*)?"
    r"=[ \t]*datetime\.now\(\)[ \t]*$"
)

#: 落盘时间字段（与 ``_WRITE_PATTERN`` 保持一致）。
_TIME_FIELD = (
    r"(?:updated_at|created_at|started_at|finished_at|decided_at"
    r"|expires_at|written_at)"
)

#: 已知存量（只减不增）。``tasks.json`` 这一族时间戳整体仍是 naive 本地时间：
#: ``Task.created_at/updated_at`` 的 ``default_factory`` 就是 ``datetime.now``，
#: ``TaskResult.started_at/finished_at`` 也由同一族代码写入。它与 D-14 的
#: ``run.json`` 迁移不是同一批——只改其中一处会让同一文件 naive / aware 混用，
#: 反而制造 P2-2 那类 TypeError。故在本表登记，等专门一批迁移时清零。
_INDIRECT_DEBT: dict[str, int] = {
    "agents/team.py": 6,
    "task/task_board.py": 4,
}


def _indirect_hits(text: str) -> list[tuple[int, str, int]]:
    """``text`` 里「变量赋值 datetime.now()、随后写进时间字段」的位置。

    返回 ``[(桥所在行号, 变量名, 落盘行号), ...]``（行号从 1 起）。

    只扫**同一文件内赋值之后**的代码：跨文件、跨函数的别名追踪不做——守卫
    的目标是拦住「顺手写成本地时间」这种回归，不是做数据流分析。
    """
    lines = text.splitlines()
    hits: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines):
        matched = _LOCAL_CLOCK_ASSIGN.match(line)
        if matched is None:
            continue
        var = matched.group("var")
        tail = "\n".join(lines[index:])
        pattern = re.compile(
            rf"(?:\.{_TIME_FIELD}\s*=|{_TIME_FIELD}[\"']?\s*[=:])\s*"
            rf"{re.escape(var)}\b")
        found = pattern.search(tail)
        if found is not None:
            hits.append((index + 1, var, index + 1 + tail[:found.start()].count("\n")))
    return hits


def _scan_indirect() -> dict[str, list[tuple[int, str, int]]]:
    found: dict[str, list[tuple[int, str, int]]] = {}
    for path in _PACKAGE.rglob("*.py"):
        hits = _indirect_hits(path.read_text(encoding="utf-8"))
        if hits:
            found[path.relative_to(_PACKAGE).as_posix()] = hits
    if _CLI_ENTRY.is_file():
        hits = _indirect_hits(_CLI_ENTRY.read_text(encoding="utf-8"))
        if hits:
            found["main.py"] = hits
    return found


def test_no_new_indirect_naive_timestamp_writes():
    """变量中转换道也不得新增（entity.py 的漏洞形态）。"""
    found = _scan_indirect()
    detail = "\n".join(
        f"  {rel}: {len(hits)} 处 → 桥 L{hit[0]}（{hit[1]}）落盘 L{hit[2]}"
        for rel, hits in sorted(found.items())
        for hit in hits
        if len(hits) > _INDIRECT_DEBT.get(rel, 0)
    )
    grown = {
        rel: len(hits) for rel, hits in found.items()
        if len(hits) > _INDIRECT_DEBT.get(rel, 0)
    }
    assert not grown, (
        "新增了「变量中转」形式的 naive 时间落盘。\n"
        "新代码请用 agentcrew.utils.clock.utc_now()：\n" + detail)


def test_indirect_guard_catches_variable_bridge():
    """守卫自身有效性：能认出 P2-2 的真实形态，也不冤枉纯计时变量。"""
    bridged = (
        "def f(rec):\n"
        "    now = datetime.now()\n"
        "    return rec.model_copy(update={'updated_at': now})\n"
    )
    assert _indirect_hits(bridged) == [(2, "now", 3)]

    typed = (
        "before = datetime.now()\n"
        "record.updated_at = before\n"
    )
    assert _indirect_hits(typed) == [(1, "before", 2)]

    # 纯计时（只用于算耗时，不落盘）不算落盘，不应误报
    timing_only = (
        "started = datetime.now()\n"
        "elapsed = datetime.now() - started\n"
    )
    assert _indirect_hits(timing_only) == []

