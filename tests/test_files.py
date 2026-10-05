import hashlib
import re
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
    atomic_write_text(p, "old"); atomic_write_text(p, "new")
    assert read_text(p) == "new"


def test_tree_lists_relative_posix_paths(tmp_path):
    (tmp_path / "a").mkdir(); (tmp_path / "a" / "b.txt").write_text("x")
    assert tree(tmp_path) == ["a/b.txt"]


# --- 以下覆盖 brief 接口清单中未被上面用例触及的产出 ---

def test_atomic_write_bytes_is_verbatim(tmp_path):
    p = tmp_path / "x.bin"
    atomic_write_bytes(p, b"\x00\x01\xffraw")
    assert p.read_bytes() == b"\x00\x01\xffraw"


def test_atomic_write_text_is_utf8_without_bom(tmp_path):
    p = tmp_path / "x.txt"
    atomic_write_text(p, "中文")
    raw = p.read_bytes()
    assert raw == "中文".encode("utf-8")
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
    data = "hello 世界".encode("utf-8")
    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()
    assert sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_slugify_run_id_format():
    run_id = slugify_run_id(datetime(2026, 10, 5, 12, 0, 0))
    assert re.fullmatch(r"20261005-120000-[0-9a-f]{4}", run_id)
