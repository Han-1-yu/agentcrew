"""``ToolRegistry`` 与内置沙箱工具的行为测试（P1-1）。

覆盖：register/get/list/filter_by_allowed（``["*"]`` 与白名单）；read_file /
list_files / grep / run_python 的基本功能与边界；run_python 的超时与异常捕获；
read_file 的路径穿越防护。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcrew.tools import (
    GrepTool,
    ListFilesTool,
    ReadFileTool,
    RunPythonTool,
    ToolRegistry,
)
from agentcrew.tools.tool import Tool


class _EchoTool(Tool):
    """回显入参的占位工具：测试注册表行为用。"""

    description: str = "回显参数"

    async def run(self, **kwargs):
        return "echo:" + ",".join(sorted(kwargs))


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """一个含少量文件的临时项目根目录。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    print('hi')\n",
                                              encoding="utf-8")
    (tmp_path / "src" / "util.py").write_text("HELLO = 'world'\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("项目说明\n", encoding="utf-8")
    return tmp_path


# ── ToolRegistry ──────────────────────────────────────────────────────

def test_register_get_list():
    reg = ToolRegistry()
    a = _EchoTool(name="alpha")
    b = _EchoTool(name="beta")
    reg.register(a)
    reg.register(b)
    assert reg.get("alpha") is a
    assert reg.get("missing") is None
    assert {t.name for t in reg.list_tools()} == {"alpha", "beta"}


def test_filter_by_allowed_star_means_all():
    reg = ToolRegistry()
    reg.register(_EchoTool(name="a"))
    reg.register(_EchoTool(name="b"))
    assert {t.name for t in reg.filter_by_allowed(["*"])} == {"a", "b"}
    assert {t.name for t in reg.filter_by_allowed([])} == {"a", "b"}


def test_filter_by_allowed_whitelist():
    reg = ToolRegistry()
    reg.register(_EchoTool(name="a"))
    reg.register(_EchoTool(name="b"))
    reg.register(_EchoTool(name="c"))
    names = {t.name for t in reg.filter_by_allowed(["a", "c"])}
    assert names == {"a", "c"}


# ── read_file ─────────────────────────────────────────────────────────

async def test_read_file_returns_content(proj: Path):
    tool = ReadFileTool(workdir=proj)
    out = await tool.run(path="src/app.py")
    assert "def main()" in out


async def test_read_file_rejects_path_traversal(proj: Path):
    tool = ReadFileTool(workdir=proj)
    for bad in ("../etc/passwd", "..\\etc\\passwd", "/etc/passwd", "a/../../b"):
        out = await tool.run(path=bad)
        assert "错误" in out and "越界" in out, bad


async def test_read_file_missing_file(proj: Path):
    tool = ReadFileTool(workdir=proj)
    out = await tool.run(path="nope.txt")
    assert "错误" in out and "不存在" in out


# ── list_files ───────────────────────────────────────────────────────

async def test_list_files_lists_one_level(proj: Path):
    tool = ListFilesTool(workdir=proj)
    out = await tool.run(path=".")
    assert "src/" in out and "notes.txt" in out


# ── grep ──────────────────────────────────────────────────────────────

async def test_grep_finds_matches(proj: Path):
    tool = GrepTool(workdir=proj)
    out = await tool.run(pattern="print\\(")
    assert "src/app.py" in out and "print" in out


async def test_grep_bad_regex(proj: Path):
    tool = GrepTool(workdir=proj)
    out = await tool.run(pattern="([unclosed")
    assert "错误" in out and "正则" in out


# ── run_python ────────────────────────────────────────────────────────

async def test_run_python_returns_stdout(proj: Path):
    tool = RunPythonTool(workdir=proj)
    out = await tool.run(code="print(2 + 3)")
    assert "5" in out and "exit_code=0" in out


async def test_run_python_captures_stderr_and_nonzero(proj: Path):
    tool = RunPythonTool(workdir=proj)
    out = await tool.run(code="import sys; print('oops', file=sys.stderr); sys.exit(2)")
    assert "stderr:" in out and "oops" in out and "exit_code=2" in out


async def test_run_python_timeout_kills_process(proj: Path):
    tool = RunPythonTool(workdir=proj, default_timeout=1.0)
    out = await tool.run(code="import time\ntime.sleep(10)")
    assert "超时" in out


async def test_run_python_missing_code(proj: Path):
    tool = RunPythonTool(workdir=proj)
    out = await tool.run(code="")
    assert "错误" in out and "code" in out
