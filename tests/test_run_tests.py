"""C3：``run_tests`` 工具——在沙箱里跑 pytest，把真实结果（含报错）带回来。

这里用真实 pytest 子进程（不 mock 执行器）：这个工具的全部价值就是「跑的是真的」，
把执行也 mock 掉，等于只测了自己拼的字符串。用例都很小（两个文件、一个断言），
秒级完成。
"""

from __future__ import annotations

from pathlib import Path

from agentcrew.tools.sandbox import ExecutionResult
from agentcrew.tools.testing import RunTestsTool


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def test_run_tests_passes(tmp_path):
    _write(tmp_path / "calc.py", "def add(a, b):\n    return a + b\n")
    _write(tmp_path / "test_calc.py",
           "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    tool = RunTestsTool(workdir=tmp_path)
    text = await tool.run()
    assert text.startswith("pytest 通过")
    assert "1 passed" in text


async def test_run_tests_reports_failure_with_assertion(tmp_path):
    _write(tmp_path / "calc.py", "def add(a, b):\n    return a - b\n")
    _write(tmp_path / "test_calc.py",
           "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    tool = RunTestsTool(workdir=tmp_path)
    text = await tool.run()
    assert text.startswith("pytest 失败")
    assert "assert" in text
    assert "1 failed" in text


async def test_run_tests_targets_subdirectory(tmp_path):
    _write(tmp_path / "ok" / "test_ok.py", "def test_ok():\n    assert True\n")
    _write(tmp_path / "bad" / "test_bad.py", "def test_bad():\n    assert False\n")
    tool = RunTestsTool(workdir=tmp_path)
    text = await tool.run(path="ok")
    assert "pytest 通过" in text
    assert "路径=ok" in text


async def test_run_tests_rejects_path_escape(tmp_path):
    tool = RunTestsTool(workdir=tmp_path)
    assert "路径越界" in await tool.run(path="../outside")
    assert "路径越界" in await tool.run(path="C:/Windows")


async def test_run_tests_missing_path(tmp_path):
    tool = RunTestsTool(workdir=tmp_path)
    assert "测试路径不存在" in await tool.run(path="nope")


async def test_run_tests_timeout_validation(tmp_path):
    tool = RunTestsTool(workdir=tmp_path)
    assert "timeout 必须是数值" in await tool.run(timeout="soon")
    assert "(0, 300]" in await tool.run(timeout=0)
    assert "(0, 300]" in await tool.run(timeout=10_000)


async def test_run_tests_no_cache_pollution(tmp_path):
    """``.pytest_cache`` 不该留进 run 目录（它是长期保留、要 diff 的目录）。"""
    _write(tmp_path / "test_x.py", "def test_x():\n    assert True\n")
    await RunTestsTool(workdir=tmp_path).run()
    assert not (tmp_path / ".pytest_cache").exists()


async def test_run_tests_uses_backend_python(tmp_path):
    """换后端只改 ``backend.python``，工具不自己判断后端。"""

    class _FakeBackend:
        name = "fake"
        python = "python-in-container"

        def __init__(self) -> None:
            self.argv: list[str] = []
            self.workdir: Path | None = None

        async def run(self, argv, *, workdir, timeout):  # noqa: ANN001
            self.argv = list(argv)
            self.workdir = workdir
            return ExecutionResult(exit_code=0, stdout="1 passed", backend="fake")

    backend = _FakeBackend()
    tool = RunTestsTool(workdir=tmp_path, backend=backend)
    text = await tool.run()
    assert backend.argv[0] == "python-in-container"
    assert backend.argv[1:4] == ["-m", "pytest", "-q"]
    assert "-p" in backend.argv and "no:cacheprovider" in backend.argv
    assert backend.workdir == tmp_path
    assert "pytest 通过" in text


async def test_run_tests_reports_missing_pytest_hint(tmp_path):
    class _NoPytest:
        name = "fake"
        python = "python3"

        async def run(self, argv, *, workdir, timeout):  # noqa: ANN001
            return ExecutionResult(
                exit_code=1,
                stderr="/usr/bin/python3: No module named pytest",
                backend="fake",
            )

    text = await RunTestsTool(workdir=tmp_path, backend=_NoPytest()).run()
    assert "No module named pytest" in text
    assert "sandbox_image" in text
