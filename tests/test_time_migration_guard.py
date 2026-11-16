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
