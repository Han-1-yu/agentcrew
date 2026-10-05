import hashlib
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from agentcrew.utils.files import (
    UnsafePathError,
    atomic_write_bytes,
    atomic_write_text,
    read_text,
    safe_relpath,
    sha256_bytes,
    slugify_run_id,
    tree,
)


@pytest.mark.parametrize("bad", [
    "", ".", "/etc/passwd", "C:\\Windows\\x", "\\\\server\\share\\x",
    "../outside.txt", "a/../../b.txt", "a/./../b",
    "CON", "con.txt", "aux.md", "NUL.py", "COM1.txt", "LPT9",
    "trailing ", "trailing.", "a/nul ", "has\x00null",
])
def test_rejects_unsafe_paths(bad):
    with pytest.raises(UnsafePathError):
        safe_relpath(bad)


@pytest.mark.parametrize("bad", [
    "./C:/x", ".\\C:\\x", "C:x", "C:x/dir", "c:D/x",
])
def test_rejects_dotted_or_drive_relative_drive_paths(bad):
    """盘符不只在首段精确出现时危险："./C:/x" 与 drive-relative 的 "C:x" 都逃逸 root。"""
    with pytest.raises(UnsafePathError):
        safe_relpath(bad)


@pytest.mark.parametrize("bad", [
    "12:30-notes.md", "NUL:", "nul:$DATA", "com:", "x.txt:$DATA", "a/b:",
])
def test_rejects_colon_bearing_segments(bad):
    """冒号在 Windows 上是数据流分隔符：放行它等于放行一个写不进去的名字。

    修复前 ``atomic_write`` 的临时名（``.12:30-notes.md.<hex>.tmp``）会被解释成
    数据流，写出的却是名为 ``.12`` 的基文件，``os.replace`` 抛裸 ``OSError``，
    ``unlink(missing_ok=True)`` 只删流，残渣 ``.12`` 留在输出目录里。
    """
    with pytest.raises(UnsafePathError):
        safe_relpath(bad)


def test_rejected_colon_name_leaves_directory_unchanged(tmp_path):
    (tmp_path / "keep.txt").write_text("x")
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(UnsafePathError):
        safe_relpath("12:30-notes.md")
    assert sorted(p.name for p in tmp_path.iterdir()) == before == ["keep.txt"]


@pytest.mark.parametrize("bad", ["a/NUL", "a/b/com1.txt", "sub/aux.md"])
def test_rejects_reserved_names_in_non_first_segments(bad):
    with pytest.raises(UnsafePathError):
        safe_relpath(bad)


@pytest.mark.parametrize("ok", [
    "README.md", "backend/app/main.py", "docs/接口文档.md",
    "a/b/c/d.txt", "deep/nested/dir/file.js", "UPPER/Case.MD",
])
def test_accepts_safe_paths(ok):
    assert safe_relpath(ok).as_posix() == Path(ok).as_posix()


def test_normalizes_backslashes_and_duplicate_separators():
    assert safe_relpath("a\\b//c.txt").as_posix() == "a/b/c.txt"


def test_atomic_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "sub" / "x.txt"
    atomic_write_text(p, "hello 世界")
    assert read_text(p) == "hello 世界"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    atomic_write_text(tmp_path / "x.txt", "a")
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*tmp*")) == []


def test_atomic_write_overwrites_existing(tmp_path):
    p = tmp_path / "x.txt"
    atomic_write_text(p, "old")
    atomic_write_text(p, "new")
    assert read_text(p) == "new"


def test_atomic_write_cleans_temp_when_replace_fails(tmp_path):
    target = tmp_path / "x.txt"
    target.mkdir()  # 目标是目录：os.replace 必然失败，临时文件必须被 finally 清掉
    with pytest.raises(OSError):
        atomic_write_bytes(target, b"data")
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*tmp*")) == []


def test_read_text_is_byte_symmetric_with_atomic_write_text(tmp_path):
    p = tmp_path / "x.txt"
    atomic_write_text(p, "a\r\nb")
    assert read_text(p) == "a\r\nb"


def _make_dir_link(link: Path, target: Path) -> bool:
    """建一个指向目录的链接：Windows 用 junction（mklink /J，无需提权），其余用 symlink。"""
    if os.name == "nt":
        return subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        ).returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return True


def test_tree_lists_relative_posix_paths(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.txt").write_text("x")
    assert tree(tmp_path) == ["a/b.txt"]


def test_tree_does_not_follow_directory_links(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "inside.txt").write_text("i")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    if not _make_dir_link(root / "link", outside):
        pytest.skip("当前平台无法创建目录链接/联接")
    assert tree(root) == ["inside.txt"]


# --- 以下覆盖 brief 接口清单中未被上面用例触及的产出 ---

def test_atomic_write_bytes_is_verbatim(tmp_path):
    p = tmp_path / "x.bin"
    atomic_write_bytes(p, b"\x00\x01\xffraw")
    assert p.read_bytes() == b"\x00\x01\xffraw"


def test_atomic_write_text_is_utf8_without_bom(tmp_path):
    p = tmp_path / "x.txt"
    atomic_write_text(p, "中文")
    raw = p.read_bytes()
    assert raw == "中文".encode()
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_tree_is_sorted_and_lists_files_only(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.txt").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "empty_dir").mkdir()
    assert tree(tmp_path) == ["a.txt", "b.txt", "sub/c.txt"]


def test_tree_respects_max_entries(tmp_path):
    for name in ("c.txt", "a.txt", "b.txt"):
        (tmp_path / name).write_text("x")
    assert tree(tmp_path, max_entries=2) == ["a.txt", "b.txt"]


def test_sha256_bytes_is_lowercase_hex_digest():
    data = "hello 世界".encode()
    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()
    assert sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_slugify_run_id_format():
    run_id = slugify_run_id(datetime(2026, 10, 5, 12, 0, 0))
    assert re.fullmatch(r"20261005-120000-[0-9a-f]{4}", run_id)
