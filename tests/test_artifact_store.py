"""``ArtifactStore`` 安全落盘与清单（Task 11）。

在 brief 用例之外补三类回归：目录联接（junction）父目录必须被拒（``Path.is_symlink()``
在 Windows 3.12 上对 junction 返回 False）、解析器原样透传的 ``./a.py``/``a\\b.py``
必须按契约命中、``ArtifactRecord.path`` 与 ``MANIFEST.json`` 里必须是规范化路径。
"""

import json
import os
import subprocess
import sys

import pytest

from agentcrew.artifacts.parser import parse_fenced_blocks
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import (
    ArtifactConflictError,
    ArtifactStore,
    ArtifactTooLargeError,
    UndeclaredArtifactError,
)
from agentcrew.task.task import Task
from agentcrew.utils.files import UnsafePathError, sha256_bytes


def mk_task(tid="T-1", role="backend_dev"):
    return Task(id=tid, title="t", description="d", role=role,
                acceptance_criteria=["a"], deliverables=[])


def spec(path="backend/app/main.py", **kw):
    return ArtifactSpec(path=path, kind="python", description="x", **kw)


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts", run_id="R-1")


class RecordingLedger:
    """记录 ``emit`` 调用的最小 ledger，结构上满足 ``LedgerLike``。"""

    def __init__(self):
        self.events = []

    def emit(self, event, **data):
        self.events.append((event, data))


def _make_dir_link(link, target):
    """建一个指向目录的链接：Windows 用 junction（mklink /J，无需提权）。"""
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


# ── 落盘与读取 ──

def test_commit_writes_file_and_returns_record(store, tmp_path):
    rec = store.commit("backend/app/main.py", "x = 1\n", spec=spec(), task=mk_task())
    written = tmp_path / "artifacts" / "backend" / "app" / "main.py"
    assert written.read_text(encoding="utf-8") == "x = 1\n"
    assert rec.path == "backend/app/main.py" and rec.size_bytes == 6
    assert rec.produced_by_task == "T-1" and len(rec.sha256) == 64


def test_commit_records_role_and_sha(store):
    task = mk_task(role="qa_engineer")
    rec = store.commit("a.py", "x = 1\n", spec=spec("a.py"), task=task)
    assert rec.produced_by_role == "qa_engineer"
    assert rec.sha256 == sha256_bytes(b"x = 1\n")
    assert rec.kind == "python"


def test_commit_creates_parent_dirs(store):
    s = ArtifactSpec(path="a/b/c/d.txt", kind="text", description="d")
    store.commit("a/b/c/d.txt", "x", spec=s, task=mk_task())
    assert store.exists("a/b/c/d.txt")


def test_chinese_path_roundtrip(store):
    s = ArtifactSpec(path="docs/接口文档.md", kind="markdown", description="d")
    store.commit("docs/接口文档.md", "# 接口\n", spec=s, task=mk_task())
    assert store.read("docs/接口文档.md") == "# 接口\n"


def test_read_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        store.read("nope.txt")


def test_read_truncates_to_max_bytes(store):
    s = ArtifactSpec(path="a.txt", kind="text", description="d")
    store.commit("a.txt", "0123456789", spec=s, task=mk_task())
    assert store.read("a.txt", max_bytes=4) == "0123"
    assert store.read("a.txt") == "0123456789"


def test_read_max_bytes_does_not_split_a_character(store):
    s = ArtifactSpec(path="c.md", kind="markdown", description="d")
    store.commit("c.md", "中文中文", spec=s, task=mk_task())
    truncated = store.read("c.md", max_bytes=4)
    assert truncated[0] == "中"  # 劈开处保留完整的“中”，残字节收成替换字符
    assert len(truncated) == 2
    assert store.read("c.md", max_bytes=6) == "中文"  # 恰好落在字符边界则无替换字符


# ── 路径安全 ──

def test_path_traversal_blocked(store, tmp_path):
    evil = ArtifactSpec(path="ok.txt", kind="text", description="d")
    with pytest.raises(Exception):
        store.commit("../outside.txt", "x", spec=evil, task=mk_task())
    assert not (tmp_path / "outside.txt").exists()


def test_absolute_path_blocked(store):
    with pytest.raises(Exception):
        store.commit("/etc/passwd", "x",
                     spec=ArtifactSpec(path="x", kind="text", description="d"),
                     task=mk_task())


def test_symlinked_parent_rejected(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Windows 建符号链接需管理员权限")
    root = tmp_path / "artifacts"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    os.symlink(outside, root / "link")
    st = ArtifactStore(root, run_id="R-1")
    s = ArtifactSpec(path="link/pwned.py", kind="python", description="d")
    with pytest.raises(Exception):
        st.commit("link/pwned.py", "x", spec=s, task=mk_task())
    assert not (outside / "pwned.py").exists()


def test_junction_parent_rejected(tmp_path):
    """junction 也是重解析点：``is_symlink()`` 返回 False，必须单独识别。"""
    root = tmp_path / "artifacts"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    if not _make_dir_link(root / "link", outside):
        pytest.skip("当前平台无法创建目录链接/联接")
    st = ArtifactStore(root, run_id="R-1")
    s = ArtifactSpec(path="link/pwned.py", kind="python", description="d")
    with pytest.raises(UnsafePathError):
        st.commit("link/pwned.py", "x", spec=s, task=mk_task())
    assert list(outside.iterdir()) == []


def test_junction_to_inside_root_is_also_rejected(tmp_path):
    """指向 root 内部的链接同样拒绝：逐段检查不区分链接的目标。"""
    root = tmp_path / "artifacts"; root.mkdir()
    (root / "real").mkdir()
    if not _make_dir_link(root / "inner", root / "real"):
        pytest.skip("当前平台无法创建目录链接/联接")
    st = ArtifactStore(root, run_id="R-1")
    with pytest.raises(UnsafePathError):
        st.commit("inner/a.py", "x = 1\n",
                  spec=ArtifactSpec(path="inner/a.py", kind="python", description="d"),
                  task=mk_task())


@pytest.mark.parametrize("bad", ["../x", "/abs.txt", "a/../../b",
                                 "C:\\Windows\\x", "a/b\x00c"])
def test_resolve_rejects_unsafe_paths(store, bad):
    with pytest.raises(UnsafePathError):
        store.resolve(bad)


def test_resolve_is_public_and_returns_path_under_root(store, tmp_path):
    assert store.resolve("a/b.py") == (tmp_path / "artifacts" / "a" / "b.py").resolve()


def test_exists_rejects_unsafe_path(store):
    with pytest.raises(UnsafePathError):
        store.exists("../x")


def test_exists_false_for_missing_file(store):
    assert not store.exists("nope.txt")


# ── 契约比对（路径规范化）──

def test_undeclared_path_rejected(store):
    with pytest.raises(UndeclaredArtifactError) as ei:
        store.commit("hack.py", "x", spec=spec("declared.py"), task=mk_task())
    assert ei.value.path == "hack.py"
    assert ei.value.task_id == "T-1"


def test_dot_slash_path_matches_declared_contract(store):
    """解析器原样透传 ``./a.py``，commit 必须规范化后再比较，否则会被误判未声明。"""
    rec = store.commit("./a.py", "x = 1\n", spec=spec("a.py"), task=mk_task())
    assert rec.path == "a.py"


def test_backslash_path_matches_posix_contract(store):
    rec = store.commit("a\\b.py", "x = 1\n", spec=spec("a/b.py"), task=mk_task())
    assert rec.path == "a/b.py"
    assert store.exists("a/b.py")


def test_parsed_output_commits_against_contract(store):
    """解析器 → commit 端到端：``file=./a.py`` 必须落地为契约里的 ``a.py``。"""
    parsed = parse_fenced_blocks("```python file=./a.py\nx = 1\n```\n")
    assert list(parsed.files) == ["./a.py"]
    for rel, content in parsed.files.items():
        rec = store.commit(rel, content, spec=spec("a.py"), task=mk_task())
    assert rec.path == "a.py" and store.read("a.py") == "x = 1\n"


# ── 尺寸校验 ──

def test_oversize_rejected(store):
    s = ArtifactSpec(path="big.txt", kind="text", description="d", max_bytes=10)
    with pytest.raises(ArtifactTooLargeError) as ei:
        store.commit("big.txt", "x" * 100, spec=s, task=mk_task())
    assert (ei.value.path, ei.value.size, ei.value.limit) == ("big.txt", 100, 10)


def test_undersize_rejected(store):
    s = ArtifactSpec(path="a.py", kind="python", description="d", min_bytes=5)
    with pytest.raises(ValueError):
        store.commit("a.py", "x", spec=s, task=mk_task())


def test_size_measured_in_utf8_bytes(store):
    """尺寸按编码后字节数：3 个汉字 = 9 字节，不能按字符数判。"""
    s = ArtifactSpec(path="c.md", kind="markdown", description="d",
                     min_bytes=9, max_bytes=9)
    store.commit("c.md", "中文中", spec=s, task=mk_task())


# ── 冲突与返工 ──

def test_conflict_when_other_task_already_wrote_path(tmp_path):
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-1")
    s = ArtifactSpec(path="shared.py", kind="python", description="d")
    st.commit("shared.py", "a=1\n", spec=s, task=mk_task("T-1"))
    with pytest.raises(ArtifactConflictError) as ei:
        st.commit("shared.py", "b=2\n", spec=s, task=mk_task("T-2"))
    assert ei.value.existing_task == "T-1" and ei.value.new_task == "T-2"
    assert ei.value.path == "shared.py"


def test_same_task_may_rewrite_its_own_path(tmp_path):
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-1")
    s = ArtifactSpec(path="a.py", kind="python", description="d")
    st.commit("a.py", "a=1\n", spec=s, task=mk_task("T-1"))
    st.commit("a.py", "a=2\n", spec=s, task=mk_task("T-1"))
    assert st.read("a.py") == "a=2\n"


def test_rewrite_updates_record_not_duplicates(tmp_path):
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-1")
    s = ArtifactSpec(path="a.py", kind="python", description="d")
    st.commit("a.py", "a=1\n", spec=s, task=mk_task("T-1"))
    st.commit("a.py", "a=22\n", spec=s, task=mk_task("T-1"))
    recs = [r for r in st.records() if r.path == "a.py"]
    assert len(recs) == 1 and recs[0].size_bytes == 5


def test_rewrite_updates_manifest_sha(tmp_path):
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-1")
    s = ArtifactSpec(path="a.py", kind="python", description="d")
    st.commit("a.py", "a=1\n", spec=s, task=mk_task("T-1"))
    st.commit("a.py", "a=22\n", spec=s, task=mk_task("T-1"))
    entries = json.loads(st.write_manifest().read_text(encoding="utf-8"))["entries"]
    assert [e["sha256"] for e in entries] == [sha256_bytes(b"a=22\n")]


# ── 全或无 ──

def _rejection_case(case):
    """(契约 spec, 待写 rel, 内容)：三类必被拒的输入。"""
    if case == "undeclared":
        return spec("sub/dir/declared.py"), "sub/dir/hack.py", "x = 1\n"
    if case == "oversize":
        s = ArtifactSpec(path="sub/dir/big.txt", kind="text", description="d",
                         max_bytes=3)
        return s, "sub/dir/big.txt", "xxxx"
    s = ArtifactSpec(path="sub/dir/a.py", kind="python", description="d",
                     min_bytes=10)
    return s, "sub/dir/a.py", "x = 1\n"


@pytest.mark.parametrize("case", ["undeclared", "oversize", "undersize"])
def test_rejected_commit_leaves_nothing_behind(tmp_path, case):
    """任何一项校验失败：不建目录、不写文件、不留记录。"""
    root = tmp_path / "artifacts"
    st = ArtifactStore(root, run_id="R-1")
    declared, rel, content = _rejection_case(case)
    with pytest.raises(ValueError):
        st.commit(rel, content, spec=declared, task=mk_task())
    assert not root.exists()
    assert st.records() == []


def _record_fields(records):
    """记录的稳定字段快照：记录对象本身可变，取值比较才有区分度。"""
    return [(r.path, r.size_bytes, r.sha256, r.produced_by_task) for r in records]


def test_conflict_leaves_existing_file_and_record_untouched(tmp_path):
    root = tmp_path / "artifacts"
    st = ArtifactStore(root, run_id="R-1")
    s = ArtifactSpec(path="a.py", kind="python", description="d")
    st.commit("a.py", "a=1\n", spec=s, task=mk_task("T-1"))
    before = _record_fields(st.records())
    with pytest.raises(ArtifactConflictError):
        st.commit("a.py", "b=2\n", spec=s, task=mk_task("T-2"))
    assert (root / "a.py").read_text(encoding="utf-8") == "a=1\n"
    assert _record_fields(st.records()) == before


def test_rejected_commit_does_not_touch_existing_manifest(tmp_path):
    root = tmp_path / "artifacts"
    st = ArtifactStore(root, run_id="R-1")
    s = ArtifactSpec(path="a.py", kind="python", description="d")
    st.commit("a.py", "a=1\n", spec=s, task=mk_task("T-1"))
    manifest = st.write_manifest()
    before = manifest.read_bytes()
    with pytest.raises(ArtifactConflictError):
        st.commit("a.py", "b=2\n", spec=s, task=mk_task("T-2"))
    assert manifest.read_bytes() == before


# ── 检查顺序（先报哪个错由控制器裁定钉死）──

def test_unsafe_path_wins_over_undeclared(store):
    """先过 ``safe_relpath`` 再比契约：``../`` 报 UnsafePathError 而不是未声明。"""
    with pytest.raises(UnsafePathError):
        store.commit("../x.py", "y", spec=spec("declared.py"), task=mk_task())


def test_undeclared_wins_over_conflict(store):
    store.commit("a.py", "a=1\n", spec=spec("a.py"), task=mk_task("T-1"))
    with pytest.raises(UndeclaredArtifactError):
        store.commit("a.py", "b=2\n", spec=spec("b.py"), task=mk_task("T-2"))


def test_conflict_wins_over_size(store):
    s = ArtifactSpec(path="a.py", kind="python", description="d", max_bytes=4)
    store.commit("a.py", "a=1\n", spec=s, task=mk_task("T-1"))
    with pytest.raises(ArtifactConflictError):
        store.commit("a.py", "x" * 100, spec=s, task=mk_task("T-2"))


# ── 记录查询 ──

def test_by_task_filters_records(store):
    store.commit("a.py", "a=1\n", spec=spec("a.py"), task=mk_task("T-1"))
    store.commit("b.py", "b=1\n", spec=spec("b.py"), task=mk_task("T-2"))
    assert [r.path for r in store.by_task("T-1")] == ["a.py"]
    assert [r.path for r in store.by_task("T-2")] == ["b.py"]
    assert store.by_task("T-404") == []


def test_records_returns_a_copy(store):
    store.commit("a.py", "a=1\n", spec=spec("a.py"), task=mk_task())
    snapshot = store.records()
    snapshot.clear()
    assert len(store.records()) == 1


# ── 清单与文件树 ──

def test_manifest_roundtrip(store, tmp_path):
    store.commit("a.py", "x=1\n", spec=spec("a.py"), task=mk_task())
    p = store.write_manifest()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["run_id"] == "R-1"
    assert [e["path"] for e in data["entries"]] == ["a.py"]


def test_manifest_path_is_normalized(tmp_path):
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-1")
    st.commit("./a.py", "x = 1\n", spec=spec("a.py"), task=mk_task())
    data = json.loads(st.write_manifest().read_text(encoding="utf-8"))
    assert [e["path"] for e in data["entries"]] == ["a.py"]
    assert [r.path for r in st.records()] == ["a.py"]


def test_manifest_written_to_root_and_keeps_chinese_unescaped(tmp_path):
    root = tmp_path / "artifacts"
    st = ArtifactStore(root, run_id="R-1")
    st.commit("docs/接口.md", "# 接口\n",
              spec=ArtifactSpec(path="docs/接口.md", kind="markdown", description="d"),
              task=mk_task())
    p = st.write_manifest()
    raw = p.read_text(encoding="utf-8")
    assert p == root / "MANIFEST.json"
    assert "docs/接口.md" in raw and "\\u63a5" not in raw


def test_write_manifest_on_empty_store(tmp_path):
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-9")
    data = json.loads(st.write_manifest().read_text(encoding="utf-8"))
    assert data["run_id"] == "R-9" and data["entries"] == []
    assert isinstance(data["generated_at"], str)


def test_file_tree_lists_committed_files(store):
    store.commit("a/b.py", "x=1\n", spec=spec("a/b.py"), task=mk_task())
    assert store.file_tree() == ["a/b.py"]
    store.write_manifest()
    assert store.file_tree() == ["MANIFEST.json", "a/b.py"]


# ── ledger 事件 ──

def test_ledger_receives_artifact_written(tmp_path):
    ledger = RecordingLedger()
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-1", ledger=ledger)
    st.commit("./a.py", "x = 1\n", spec=spec("a.py"), task=mk_task("T-7"))
    assert ledger.events == [(
        "artifact_written",
        {"task_id": "T-7", "path": "a.py", "size_bytes": 6,
         "sha256": sha256_bytes(b"x = 1\n")},
    )]


def test_ledger_silent_on_rejected_commit(tmp_path):
    ledger = RecordingLedger()
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-1", ledger=ledger)
    with pytest.raises(UndeclaredArtifactError):
        st.commit("hack.py", "x", spec=spec("declared.py"), task=mk_task())
    assert ledger.events == []
