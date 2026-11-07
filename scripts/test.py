#!/usr/bin/env python3
"""本机测试跑器：逐文件隔离 + 崩溃自动重试 + 汇总。

**为什么需要它**：本机（Windows + 某些安全软件 / 沙箱注入）在跑大型测试时会出现
**随机进程级崩溃**（`access violation`、`Segmentation fault`、退出码 139 /
0xC0000005），而崩溃点每次都不一样、单文件重跑又都能过——这是环境问题，不是回归。
把整个套件塞进一个进程里跑，一次崩溃就把整轮结果作废，于是只能人工分 5 批、再手工
加总，既慢又容易漏。

这个跑器把「一个进程跑全部」换成「一个进程跑一个文件」：

- 崩溃只影响那一个文件，重试后仍崩才计为「不稳定」（并且**不会**让其它文件的结果
  丢失）；
- 每轮结果按文件汇总成一行，最后给总计与退出码；
- 自动清理 `__pycache__`、固定 `PYTHONDONTWRITEBYTECODE=1`（本机曾多次读到写坏的
  .pyc 报 `SystemError: Negative size passed to PyUnicode_New`）。

用法：

    python scripts/test.py                    # 逐文件跑全部
    python scripts/test.py --batch 3          # 每 3 个文件一个进程（更快）
    python scripts/test.py -k rbac           # 只跑文件名/用例名匹配的
    python scripts/test.py --python .venv/Scripts/python.exe
    python scripts/test.py -- -x             # 透传给 pytest 的额外参数

CI 里仍然用 `pytest -q`（Linux 单进程稳定、且要的就是「一次跑完」）；这个脚本是
**本机的兜底路径**。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

#: pytest 汇总行的计数（顺序无关）。
_COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped|error|errors|deselected|xfailed|xpassed)")
#: 需要清掉的字节码 / 缓存目录（venv 与前端依赖目录不动）。
_PRUNE_DIRS = {"studio", "node_modules", ".git", ".venv", ".venv-ci", "output"}

CRASH_EXIT_CODES = {139, 3221225477, -1073741819, 1, 2}  # 仅用于文案提示


@dataclass
class FileResult:
    """一个测试文件（或一批文件）的结果。"""

    path: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    crashed: bool = False
    attempts: int = 1
    seconds: float = 0.0
    first_attempt_failed: bool = False   # 首次失败、重试后通过 = 疑似环境
    detail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.crashed and self.failed == 0 and self.errors == 0


def _clean_pycache() -> int:
    """删掉 `__pycache__`；返回清掉的数量（写坏的 .pyc 是本机老问题）。"""
    removed = 0
    for path in ROOT.rglob("__pycache__"):
        if any(part in _PRUNE_DIRS or part.startswith(".venv") for part in path.parts):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def _child_env(python: str) -> dict[str, str]:
    """子进程环境：去 PYTHONPATH 污染、禁写字节码、强制 UTF-8 管道。"""
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("PYTHON") and "PROXY" not in key.upper()}
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        # 测试用的输出目录由各用例自己给；这里只固定「不读仓库根配置」。
        "AGENTCREW_CONFIG": str(ROOT / "agentcrew.yaml"),
    })
    return env


def _parse_counts(text: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for line in reversed(text.splitlines()):
        found = _COUNT_RE.findall(line)
        if not found:
            continue
        parsed: dict[str, int] = {}
        for number, label in found:
            key = "error" if label.startswith("error") else label
            parsed[key] = parsed.get(key, 0) + int(number)
        counts.update({key: value for key, value in parsed.items() if key in counts})
        return counts
    return counts


def _run_once(python: str, targets: list[str], extra: list[str],
              timeout: int) -> tuple[int, str, float]:
    command = [python, "-m", "pytest", *targets, "-q", "--assert=plain",
               "-p", "no:cacheprovider", *extra]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command, cwd=ROOT, env=_child_env(python), capture_output=True,
            text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return 124, output if isinstance(output, str) else "", (
            time.perf_counter() - started)
    return (completed.returncode, (completed.stdout or "") + (completed.stderr or ""),
            time.perf_counter() - started)


def _short_detail(output: str, limit: int = 6) -> list[str]:
    """从输出里摘出「失败用例 / 致命错误」的关键行，避免刷屏。"""
    lines: list[str] = []
    for line in output.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            lines.append(line.strip()[:160])
        elif "Fatal Python error" in line or "Windows fatal exception" in line:
            lines.append(line.strip()[:160])
        if len(lines) >= limit:
            break
    if not lines:
        tail = [item.strip() for item in output.strip().splitlines()[-3:] if item.strip()]
        lines = tail
    return lines


def _discover(pattern: str | None) -> list[Path]:
    files = sorted(TESTS.glob("test_*.py"))
    if pattern:
        lowered = pattern.lower()
        files = [path for path in files if lowered in path.stem.lower()]
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="逐文件隔离跑本机测试（崩溃自动重试并汇总）")
    parser.add_argument("--python", default=os.environ.get("AGENTCREW_TEST_PYTHON")
                        or sys.executable,
                        help="用哪个解释器跑 pytest（默认当前解释器）")
    parser.add_argument("--batch", type=int, default=4,
                        help="每个子进程跑几个文件（默认 4：实测本机每起一个进程都有"
                             "极小概率随机崩溃，批太小反而更容易撞上；"
                             "要最彻底隔离用 --batch 1）")
    parser.add_argument("--retries", type=int, default=2,
                        help="崩溃 / 超时后的重试次数（默认 2）")
    parser.add_argument("--retry-failed", type=int, default=0, metavar="N",
                        help="用例失败后也重试 N 次（默认 0 = 不重试）。"
                             "本机随机故障有时表现为断言失败，开启后能区分"
                             "「疑似环境」与「真失败」——两者都会打印出来")
    parser.add_argument("--timeout", type=int, default=600,
                        help="单个子进程超时（秒，默认 600）")
    parser.add_argument("-k", "--filter", dest="pattern", default=None,
                        help="只跑文件名包含该子串的文件")
    parser.add_argument("--keep-pycache", action="store_true",
                        help="不清理 __pycache__")
    parser.add_argument("--verbose", action="store_true",
                        help="逐个文件打印结果（默认只打印异常文件）")
    parser.add_argument("pytest_args", nargs="*",
                        help="透传给 pytest 的参数（放在 -- 之后）")
    args = parser.parse_args(argv)

    if not TESTS.is_dir():
        print(f"找不到测试目录：{TESTS}", file=sys.stderr)
        return 2
    if not args.keep_pycache:
        removed = _clean_pycache()
        if args.verbose and removed:
            print(f"已清理 {removed} 个 __pycache__")

    files = _discover(args.pattern)
    if not files:
        print("没有匹配的测试文件", file=sys.stderr)
        return 2

    batches = [files[index:index + max(1, args.batch)]
               for index in range(0, len(files), max(1, args.batch))]
    results: list[FileResult] = []
    started_all = time.perf_counter()

    for index, batch in enumerate(batches, start=1):
        targets = [str(path.relative_to(ROOT)) for path in batch]
        label = targets[0] if len(targets) == 1 else f"{len(targets)} 个文件"
        attempt = 0
        failed_once = False
        crash_retries = 0
        failure_retries = 0
        while True:
            attempt += 1
            code, output, seconds = _run_once(
                args.python, targets, args.pytest_args, args.timeout)
            counts = _parse_counts(output)
            result = FileResult(
                path=label, passed=counts["passed"], failed=counts["failed"],
                skipped=counts["skipped"], errors=counts["error"],
                crashed=code not in (0, 1, 5), attempts=attempt, seconds=seconds,
                first_attempt_failed=failed_once)
            if result.crashed:
                # 崩溃 / 超时：重试（本机随机崩溃的典型表现）
                result.detail = _short_detail(output)
                if crash_retries < args.retries:
                    crash_retries += 1
                    if args.verbose:
                        print(f"[{index}/{len(batches)}] {label} 崩溃，重试 "
                              f"{crash_retries}/{args.retries} ...")
                    continue
            elif not result.ok:
                # 失败也重试（可选）：本机随机故障有时表现为断言失败。
                result.detail = _short_detail(output)
                if failure_retries < args.retry_failed:
                    failure_retries += 1
                    failed_once = True
                    if args.verbose:
                        print(f"[{index}/{len(batches)}] {label} 失败，重试 "
                              f"{failure_retries}/{args.retry_failed} ...")
                    continue
            results.append(result)
            break

        if args.verbose or not result.ok:
            status = ("OK" if result.ok else
                      ("不稳定（重试后仍崩）" if result.crashed else "失败"))
            print(f"[{index}/{len(batches)}] {label}  {status}  "
                  f"passed={result.passed} failed={result.failed} "
                  f"skipped={result.skipped} error={result.errors} "
                  f"({result.seconds:.1f}s, 第 {result.attempts} 次)")
            for line in result.detail:
                print(f"    {line}")

    total_passed = sum(item.passed for item in results)
    total_failed = sum(item.failed for item in results)
    total_skipped = sum(item.skipped for item in results)
    total_errors = sum(item.errors for item in results)
    unstable = [item for item in results if item.crashed]
    failed = [item for item in results if not item.crashed and not item.ok]
    retried = [item for item in results if item.attempts > 1 and item.ok]
    flaky_failures = [item for item in results
                      if item.ok and item.first_attempt_failed]
    elapsed = time.perf_counter() - started_all

    print("\n" + "=" * 66)
    print(f"文件 {len(files)} 个 / 进程 {len(batches)} 个 · 用时 {elapsed:.1f}s")
    print(f"用例：{total_passed} passed · {total_failed} failed · "
          f"{total_skipped} skipped · {total_errors} error")
    if retried:
        print(f"重试后恢复：{len(retried)} 个文件（本机随机崩溃，非回归）")
    if flaky_failures:
        print("首次失败、重试通过（疑似环境抖动，仍列出以便核对）：")
        for item in flaky_failures:
            print(f"  - {item.path}")
            for line in item.detail[:3]:
                print(f"      {line}")
    if failed:
        print("失败文件：")
        for item in failed:
            print(f"  - {item.path}（failed={item.failed} error={item.errors}）")
    if unstable:
        print("重试后仍崩（环境问题，建议单独再跑一次）：")
        for item in unstable:
            print(f"  - {item.path}")
    print("结果：" + ("通过" if not failed and not unstable else "未通过"))
    print("=" * 66)
    return 0 if not failed and not unstable else 1


if __name__ == "__main__":
    raise SystemExit(main())
