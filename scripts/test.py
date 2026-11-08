#!/usr/bin/env python3
"""本机测试跑器：逐批隔离 + 崩溃重试 + 汇总（并严格区分「环境噪音」与「真失败」）。

**为什么需要它**：本机（Windows + 安全软件 / 沙箱注入）跑大型测试会出现**随机进程级
崩溃**（`access violation`、`Segmentation fault`、退出码 139 / 0xC0000005），崩溃点每次
不同、单文件重跑又都能过——这是环境问题，不是回归。把整个套件塞进一个进程里跑，一次
崩溃就把整轮结果作废，于是只能人工分批、再手工加总，既慢又容易漏。

这个跑器把「一个进程跑全部」换成「一个进程跑一批文件」：

- 崩溃只影响那一批，自动重试；重试后仍崩才计为「不稳定」；
- 非崩溃的失败**不洗白**：默认直接判失败；`--retry-failed N` 时恢复的批次单列为
  「未证实为环境」，`--strict` 下仍然退出非 0；
- 失败批次若含多个文件，会自动**逐文件归因**（重跑那一批里的每个文件，只用于定位），
  避免「4 个文件」这种无法定位的报告；
- 一个用例都没跑到（`-k` 写错、文件空）**不算通过**——那是覆盖为零；
- 自动清理 `__pycache__`、固定 `PYTHONDONTWRITEBYTECODE=1`（本机曾多次读到写坏的
  .pyc，报 `SystemError: Negative size passed to PyUnicode_New`）。

用法：

    python scripts/test.py                    # 逐批跑全部（默认每批 4 个文件）
    python scripts/test.py --batch 1          # 每文件一个进程（隔离最彻底）
    python scripts/test.py -k rbac            # 只跑文件名匹配的测试文件
    python scripts/test.py --retry-failed 1 --strict
    python scripts/test.py --python .venv/Scripts/python.exe -- -x

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

#: pytest 汇总行：必须带 `in X.XXs` 才算（否则会把日志里碰巧出现的
#: 「3 passed」当成汇总，得出相反结论）。
_SUMMARY_RE = re.compile(
    r"(\d+) (passed|failed|skipped|error|errors|xfailed|xpassed|deselected)")
_SUMMARY_TAIL_RE = re.compile(r"in \d+(?:\.\d+)?s\b")

#: 真正的「进程被杀」退出码。**只认这些**：
#: 139/-1073741819/3221225477 = SIGSEGV / 0xC0000005；124 = 我们自己的超时。
#: 2/3/4（收集错误 / 内部错误 / 用法错误）是**真的有问题**，必须当失败而非环境噪音。
_CRASH_CODES = frozenset({139, -1073741819, 3221225477, 124})

#: 需要跳过的目录（虚拟环境 / 前端依赖 / 运行产物）。
_PRUNE_DIRS = frozenset({
    "studio", "node_modules", ".git", "output", "venv", "env",
    "site-packages", ".tox", ".mypy_cache", ".pytest_cache",
})


class Outcome:
    """一次子进程运行的归类。"""

    OK = "ok"                 # 全部通过
    FAILED = "failed"         # 有失败用例
    NO_TESTS = "no-tests"     # 一个用例都没跑（-k 过滤、空文件）
    CRASH = "crash"           # 进程被杀 / 超时
    ERROR = "error"           # 收集错误 / 内部错误 / 用法错误


@dataclass
class FileResult:
    """一个测试文件（或一批文件）的结果。"""

    path: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    outcome: str = Outcome.OK
    attempts: int = 1
    seconds: float = 0.0
    first_failure_detail: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)

    @property
    def crashed(self) -> bool:
        return self.outcome == Outcome.CRASH

    @property
    def ok(self) -> bool:
        """通过 = 没崩、没失败、没错误，且**真的跑到用例**。

        ``passed + skipped`` 必须大于 0：一个用例都没跑（例如 ``-k`` 写错）绝不能
        被当成通过——那是「覆盖为零」而不是「没有问题」。
        """
        return (self.outcome == Outcome.OK
                and self.failed == 0 and self.errors == 0
                and (self.passed + self.skipped) > 0)

    @property
    def recovered_from_failure(self) -> bool:
        """首次失败、重试后通过——**未证实**是环境问题。"""
        return self.ok and bool(self.first_failure_detail)


def _clean_pycache() -> int:
    """删掉 `__pycache__`；返回实际删除成功的目录数。

    用 ``os.walk`` 并在遍历时剪枝：``rglob`` 会把 ``.venv`` / ``node_modules`` 里
    几万个目录也走一遍，纯属浪费。删除失败（.pyc 被占用）不计入，否则打印的数字
    不可信——而「清不掉」正是本机读到坏 .pyc 的原因，不能假装成功。
    """
    removed = 0
    for current, dirs, _files in os.walk(ROOT):
        here = Path(current)
        if any(part in _PRUNE_DIRS or part.startswith(".venv") for part in here.parts):
            dirs[:] = []
            continue
        if "__pycache__" in dirs:
            try:
                shutil.rmtree(here / "__pycache__")
                removed += 1
            except OSError:
                pass
            dirs.remove("__pycache__")
        dirs[:] = [name for name in dirs
                   if name not in _PRUNE_DIRS and not name.startswith(".venv")]
    return removed


def _child_env() -> dict[str, str]:
    """子进程环境：去 PYTHON* 污染、禁写字节码、强制 UTF-8 管道、隔离配置。

    ``AGENTCREW_CONFIG`` 指向一个**确定不存在**的路径：仓库根只要有人建了
    ``agentcrew.yaml``，子进程在收集期就会读到它（此时 conftest 的隔离还没生效）。
    """
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("PYTHON")
           and not key.upper().endswith("_PROXY") and key.upper() != "PROXY"}
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "AGENTCREW_CONFIG": str(ROOT / ".pytest-isolated" / "absent-agentcrew.yaml"),
    })
    return env


def _classify(code: int, counts: dict[str, int]) -> str:
    """退出码 + 计数 → 归类。**崩溃只认进程被杀**，2/3/4 一律算真问题。"""
    if code in _CRASH_CODES or code < 0:
        return Outcome.CRASH
    if code == 0:
        return Outcome.OK if (counts["passed"] + counts["skipped"]) > 0 else Outcome.NO_TESTS
    if code == 5:
        return Outcome.NO_TESTS
    if code == 1:
        return Outcome.FAILED
    return Outcome.ERROR


def _parse_counts(stdout: str) -> dict[str, int]:
    """只从 **stdout** 的最后一条汇总行取计数。

    只看 stdout：stderr 里混着日志与子进程输出，其中出现的「3 passed」会把结论
    颠倒。要求整行以 ``in X.XXs`` 收尾，且取**最后**一条（pytest 的汇总行总在末尾）。
    """
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for line in reversed(stdout.splitlines()):
        if not _SUMMARY_TAIL_RE.search(line):
            continue
        found = _SUMMARY_RE.findall(line)
        if not found:
            continue
        for number, label in found:
            key = "error" if label.startswith("error") else label
            if key in counts:
                counts[key] += int(number)
        return counts
    return counts


def _run_once(python: str, targets: list[str], extra: list[str],
              timeout: int) -> tuple[int, dict[str, int], str, float]:
    command = [python, "-m", "pytest", *targets, "-q", "--assert=plain",
               "-p", "no:cacheprovider", *extra]
    empty = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command, cwd=ROOT, env=_child_env(), capture_output=True,
            text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or exc.stderr or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return 124, empty, output, time.perf_counter() - started
    except (FileNotFoundError, OSError) as exc:
        # 解释器路径写错不该让整个脚本崩掉、把已跑完的结论一起丢掉。
        message = f"无法启动解释器 {python}: {exc}"
        return 127, empty, message, time.perf_counter() - started
    elapsed = time.perf_counter() - started
    return (completed.returncode, _parse_counts(completed.stdout or ""),
            (completed.stdout or "") + (completed.stderr or ""), elapsed)


def _short_detail(output: str, limit: int = 6) -> list[str]:
    """摘出「失败用例 / 致命错误」的关键行，避免刷屏。"""
    lines: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("FAILED ", "ERROR ")):
            lines.append(stripped[:160])
        elif "Fatal Python error" in stripped or "Windows fatal exception" in stripped:
            lines.append(stripped[:160])
        elif stripped.startswith("E   ") and len(lines) < limit:
            lines.append(stripped[:160])
        if len(lines) >= limit:
            break
    if not lines:
        lines = [item.strip()[:160] for item in output.strip().splitlines()[-3:]
                 if item.strip()]
    return lines


def _discover(pattern: str | None) -> list[Path]:
    """递归找 ``test_*.py``；``pattern`` 匹配文件名（不含扩展名）。"""
    files = sorted(TESTS.rglob("test_*.py"))
    if pattern:
        lowered = pattern.lower()
        files = [path for path in files if lowered in path.stem.lower()]
    return files


def _attempt(python: str, targets: list[str], extra: list[str], timeout: int,
             retries: int, retry_failed: int) -> FileResult:
    """跑一批文件：崩溃重试 ``retries`` 次，失败重试 ``retry_failed`` 次。"""
    label = targets[0] if len(targets) == 1 else f"{len(targets)} 个文件"
    attempt = 0
    crash_retries = 0
    failure_retries = 0
    first_failure: list[str] = []
    while True:
        attempt += 1
        code, counts, output, seconds = _run_once(python, targets, extra, timeout)
        outcome = _classify(code, counts)
        detail = [] if outcome == Outcome.OK else _short_detail(output)
        if outcome == Outcome.CRASH and crash_retries < retries:
            crash_retries += 1
            continue
        if outcome in (Outcome.FAILED, Outcome.ERROR) and failure_retries < retry_failed:
            failure_retries += 1
            if not first_failure:
                first_failure = detail
            continue
        return FileResult(
            path=label, passed=counts["passed"], failed=counts["failed"],
            skipped=counts["skipped"], errors=counts["error"],
            outcome=outcome, attempts=attempt, seconds=seconds, detail=detail,
            first_failure_detail=first_failure)


def _attribute(python: str, files: list[Path], extra: list[str],
               timeout: int, retries: int) -> list[FileResult]:
    """把一个失败批次拆成逐文件结果，用于定位「哪个文件真的有问题」。

    归因**也走重试**：本机随机崩不分场合，若归因单跑不重试，偶发崩溃会被
    误标成「重试后仍崩」，把环境噪音固化成结论。
    """
    return [
        _attempt(python, [str(path.relative_to(ROOT))], extra, timeout,
                 retries, retry_failed=0)
        for path in files
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="逐批隔离跑本机测试（崩溃自动重试并汇总）")
    parser.add_argument("--python", default=os.environ.get("AGENTCREW_TEST_PYTHON")
                        or sys.executable,
                        help="用哪个解释器跑 pytest（默认当前解释器）")
    parser.add_argument("--batch", type=int, default=4,
                        help="每批几个文件（默认 4：本机每起一个进程都有极小概率"
                             "随机崩溃，批太小反而更容易撞上；要最彻底隔离用 --batch 1）")
    parser.add_argument("--retries", type=int, default=2,
                        help="**崩溃**后的重试次数（默认 2）")
    parser.add_argument("--retry-failed", type=int, default=0, metavar="N",
                        help="用例**失败**后也重试 N 次（默认 0）。恢复的批次会单列为"
                             "「未证实为环境」，--strict 下仍判失败")
    parser.add_argument("--strict", action="store_true",
                        help="失败后重试通过也算失败（不让疑似回归被洗成绿色）")
    parser.add_argument("--timeout", type=int, default=600,
                        help="单个子进程超时秒数（默认 600）")
    parser.add_argument("-k", "--filter", dest="pattern", default=None,
                        help="只跑**文件名**包含该子串的测试文件")
    parser.add_argument("--keep-pycache", action="store_true",
                        help="不清理 __pycache__")
    parser.add_argument("--verbose", action="store_true",
                        help="逐个批次打印结果（默认只打印异常批次）")
    parser.add_argument("pytest_args", nargs="*",
                        help="透传给 pytest 的额外参数（放在 -- 之后）")
    args = parser.parse_args(argv)

    if args.retries < 0 or args.retry_failed < 0:
        print("--retries / --retry-failed 不能为负", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout 必须 > 0", file=sys.stderr)
        return 2
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

    size = max(1, args.batch)
    batches = [files[index:index + size] for index in range(0, len(files), size)]
    results: list[FileResult] = []
    started_all = time.perf_counter()

    for index, batch in enumerate(batches, start=1):
        targets = [str(path.relative_to(ROOT)) for path in batch]
        result = _attempt(args.python, targets, args.pytest_args, args.timeout,
                          args.retries, args.retry_failed)
        # 多文件批次出错 → 逐文件归因：不能用「4 个文件」当结论。
        if not result.ok and len(batch) > 1:
            attributed = _attribute(args.python, batch, args.pytest_args,
                                    args.timeout, args.retries)
            results.extend(attributed)
            if args.verbose or not result.ok:
                print(f"[{index}/{len(batches)}] 批次归因（{len(batch)} 个文件）：")
                for item in attributed:
                    if not item.ok:
                        print(f"    {item.path}  {item.outcome}  "
                              f"passed={item.passed} failed={item.failed}")
            continue
        results.append(result)
        if args.verbose or not result.ok:
            print(f"[{index}/{len(batches)}] {result.path}  {result.outcome}  "
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
    broken = [item for item in results if not item.ok and not item.crashed]
    no_tests = [item for item in results if item.outcome == Outcome.NO_TESTS]
    recovered = [item for item in results if item.attempts > 1 and item.ok]
    suspicious = [item for item in results if item.recovered_from_failure]
    elapsed = time.perf_counter() - started_all

    print("\n" + "=" * 70)
    print(f"文件 {len(files)} 个 / 批次 {len(batches)} 个 · 用时 {elapsed:.1f}s")
    print(f"用例：{total_passed} passed · {total_failed} failed · "
          f"{total_skipped} skipped · {total_errors} error")
    if recovered:
        print(f"崩溃重试后恢复：{len(recovered)} 个（进程被杀，非回归）")
    if suspicious:
        print("首次失败、重试通过（**未证实**为环境，请人工核对）：")
        for item in suspicious:
            print(f"  - {item.path}")
            for line in item.first_failure_detail[:3]:
                print(f"      {line}")
    if broken:
        print("失败：")
        for item in broken:
            print(f"  - {item.path}（{item.outcome}：failed={item.failed} "
                  f"error={item.errors}）")
            for line in item.detail[:4]:
                print(f"      {line}")
    if no_tests:
        print("没有跑到任何用例（-k 过滤为空 / 文件里没有测试）：")
        for item in no_tests:
            print(f"  - {item.path}")
    if unstable:
        print("重试后仍崩（环境问题，建议单独再跑一次该文件）：")
        for item in unstable:
            print(f"  - {item.path}")
    passed = (not broken and not unstable and not no_tests
              and not (args.strict and suspicious))
    print("结果：" + ("通过" if passed else "未通过"))
    print("=" * 70)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
