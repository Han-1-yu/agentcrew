"""R34：本机测试跑器自身的纯逻辑（归类 / 计数解析 / 环境净化 / 发现）。

跑器是「本机随机崩溃」的兜底，它自己判错就会把绿的报成红的（更糟：把红的报成绿的），
所以这几条断言值得单独钉住——尤其是「崩溃只认进程被杀」「零用例不算通过」两条。
"""

from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _load() -> SimpleNamespace:
    """执行 scripts/test.py 并拿到它定义的函数。

    用 ``runpy`` 而不是 importlib：文件名是 ``test.py``，与 pytest 的模块发现规则
    撞名，按普通 import 加载容易被当成测试模块解析。``run_name`` 不是 ``__main__``，
    因此脚本的 ``main()`` 不会被执行。
    """
    namespace = runpy.run_path(
        str(ROOT / "scripts" / "test.py"), run_name="ac_test_runner")
    return SimpleNamespace(**namespace)


runner = _load()
Outcome = runner.Outcome


# ── 计数解析 ───────────────────────────────────────────────────────────

def test_parse_counts_reads_summary_line():
    stdout = (
        "....................\n"
        "tests/test_x.py ..F\n"
        "1 failed, 18 passed, 2 skipped in 3.10s\n"
    )
    assert runner._parse_counts(stdout) == {
        "passed": 18, "failed": 1, "skipped": 2, "error": 0}


def test_parse_counts_ignores_log_lines_without_timing():
    """日志里出现「3 passed」但没有 `in X.XXs` 尾缀 → 不能当汇总。"""
    stdout = (
        "some test log says 3 passed quickly\n"
        "5 passed in 1.20s\n"
    )
    assert runner._parse_counts(stdout)["passed"] == 5


def test_parse_counts_takes_last_summary_line():
    stdout = (
        "1 failed in 0.10s\n"          # 中间某次运行的残留
        "9 passed in 2.00s\n"          # 真正的汇总在最后
    )
    counts = runner._parse_counts(stdout)
    assert (counts["passed"], counts["failed"]) == (9, 0)


def test_parse_counts_empty_and_killed_midrun():
    assert runner._parse_counts("") == {
        "passed": 0, "failed": 0, "skipped": 0, "error": 0}
    # 被杀在中间只有点号：不能凭空猜出通过数
    assert runner._parse_counts("................")["passed"] == 0


# ── 归类：崩溃只认进程被杀 ─────────────────────────────────────────────

def test_classify_crash_codes_only():
    zero = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    assert runner._classify(139, zero) == Outcome.CRASH
    assert runner._classify(-1073741819, zero) == Outcome.CRASH
    assert runner._classify(124, zero) == Outcome.CRASH
    # 2/3/4 是真问题（收集错误 / 内部错误 / 用法错误），不能算「环境崩了」
    for code in (2, 3, 4):
        assert runner._classify(code, zero) == Outcome.ERROR


def test_classify_failed_success_and_no_tests():
    hit = {"passed": 3, "failed": 0, "skipped": 0, "error": 0}
    none = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    assert runner._classify(0, hit) == Outcome.OK
    assert runner._classify(1, dict(hit, failed=1)) == Outcome.FAILED
    assert runner._classify(5, none) == Outcome.NO_TESTS
    # 退出码 0 但一个用例都没跑（-k 过滤为空）→ 不是 OK
    assert runner._classify(0, none) == Outcome.NO_TESTS


# ── FileResult 的通过判定 ──────────────────────────────────────────────

def test_ok_requires_actually_running_tests():
    empty = runner.FileResult(path="x", outcome=Outcome.NO_TESTS)
    assert empty.ok is False
    # 全 skip 的文件算通过（确实跑到了用例，只是被跳过）
    skipped = runner.FileResult(path="x", skipped=2)
    assert skipped.ok is True
    crashed = runner.FileResult(path="x", outcome=Outcome.CRASH, passed=3)
    assert crashed.ok is False


def test_recovered_from_failure_only_marks_suspicious():
    plain = runner.FileResult(path="x", passed=3)
    assert plain.recovered_from_failure is False
    flaky = runner.FileResult(path="x", passed=3, attempts=2,
                              first_failure_detail=["FAILED x::t"])
    assert flaky.recovered_from_failure is True


# ── 环境净化与发现 ─────────────────────────────────────────────────────

def test_child_env_drops_python_and_proxy_vars(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy")
    env = runner._child_env()
    assert "PYTHONPATH" not in env
    assert "HTTPS_PROXY" not in env
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    # 必须指向**不存在**的配置，避免子进程读到仓库根的真实 agentcrew.yaml
    assert not Path(env["AGENTCREW_CONFIG"]).exists()


def test_short_detail_prefers_failed_lines():
    output = (
        "noise line\n"
        "FAILED tests/test_a.py::test_one - assert 1 == 2\n"
        "Windows fatal exception: access violation\n"
        "1 failed in 2s\n"
    )
    detail = runner._short_detail(output)
    assert detail[0].startswith("FAILED tests/test_a.py")
    assert any("access violation" in line for line in detail)


def test_discover_is_recursive_and_filters():
    all_files = runner._discover(None)
    assert all_files and all(path.name.startswith("test_") for path in all_files)
    filtered = runner._discover("rbac")
    assert filtered and all("rbac" in path.stem.lower() for path in filtered)
    assert runner._discover("no-such-file-xyz") == []
