"""R34：本机测试跑器自身的纯逻辑（计数解析 / 环境净化 / 目录发现）。

跑器是「本机随机崩溃」的兜底，它自己算错就会把绿的报成红的（或反之），
所以这几条断言值得单独钉住。
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


def test_parse_counts_reads_pytest_summary():
    output = (
        "....................\n"
        "tests/test_x.py ..F\n"
        "1 failed, 18 passed, 2 skipped in 3.10s\n"
    )
    assert runner._parse_counts(output) == {
        "passed": 18, "failed": 1, "skipped": 2, "error": 0}


def test_parse_counts_handles_errors_and_empty():
    assert runner._parse_counts("2 errors, 5 passed in 1s")["error"] == 2
    assert runner._parse_counts("") == {
        "passed": 0, "failed": 0, "skipped": 0, "error": 0}
    # 只有点号（被杀在中间）时不乱猜数字
    assert runner._parse_counts("................................")["passed"] == 0


def test_child_env_drops_python_vars_and_forbids_bytecode(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("PYTHONHOME", "/elsewhere")
    env = runner._child_env("python")
    assert not [key for key in env if key.upper().startswith("PYTHON") and
                key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONUTF8",
                            "PYTHONIOENCODING"}]
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


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


def test_discover_filters_by_name():
    all_files = runner._discover(None)
    assert all_files and all(path.name.startswith("test_") for path in all_files)
    filtered = runner._discover("rbac")
    assert filtered and all("rbac" in path.stem.lower() for path in filtered)
