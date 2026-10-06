"""产物正文智能摘要测试（P3-6）。

覆盖：markdown / python / json 专项策略、未知 kind 回落、``summarize_records``
批量拼接，以及 :meth:`BaseAgent.summarize` 在有产物时使用产物摘要、无产物时
回落到 notes（向后兼容）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from agentcrew.agents.base_agent import BaseAgent
from agentcrew.artifacts.summarizer import (
    summarize_artifact,
    summarize_records,
)
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskResult


# ── summarize_artifact：按 kind 的专项策略 ────────────────────────────


def test_markdown_extracts_heading_structure():
    content = (
        "# 架构设计\n"
        "\n"
        "这是系统的总体设计文档，描述分层与数据流。\n"
        "\n"
        "## 调度层\n"
        "### 任务板\n"
    )
    summary = summarize_artifact("docs/ARCH.md", content, kind="markdown")
    assert summary.startswith("[markdown 摘要] docs/ARCH.md:")
    assert "架构设计" in summary
    assert "调度层" in summary
    assert "任务板" in summary
    assert "总体设计" in summary  # 首段被摘进摘要


def test_python_extracts_defs_and_docstring():
    content = (
        '"""工具注册表。"""\n'
        "\n"
        "class ToolRegistry:\n"
        "    def register(self, tool):\n"
        "        pass\n"
        "\n"
        "def get_tool(name):\n"
        "    return None\n"
    )
    summary = summarize_artifact("src/registry.py", content, kind="python")
    assert summary.startswith("[python 摘要] src/registry.py:")
    assert "类ToolRegistry" in summary
    assert "函数register" in summary
    assert "函数get_tool" in summary
    assert "工具注册表" in summary  # docstring 摘录


def test_json_extracts_top_level_keys():
    content = '{"name": "agentcrew", "version": "1.0", "count": 3, "tags": ["a", "b"]}'
    summary = summarize_artifact("pkg.json", content, kind="json")
    assert summary.startswith("[json 摘要] pkg.json:")
    assert "name" in summary
    assert "version" in summary
    assert "count" in summary
    assert "tags" in summary


def test_broken_json_falls_back_to_lines():
    summary = summarize_artifact("bad.json", "{ not json", kind="json")
    assert "bad.json" in summary
    # 回落路径应含行数 / 字符数统计，而不是抛异常。
    assert "行" in summary


def test_unknown_kind_falls_back_to_head_lines():
    content = "\n".join(f"line {i}" for i in range(30))
    summary = summarize_artifact("notes.txt", content, kind="text")
    assert summary.startswith("[text 摘要] notes.txt:")
    assert "line 0" in summary
    assert "30 行" in summary


def test_summary_respects_max_length():
    content = "# " + "x" * 1000
    summary = summarize_artifact("a.md", content, kind="markdown", max_length=80)
    assert len(summary) <= 80


# ── summarize_records：批量拼接 ────────────────────────────────────────


def _make_store(tmp_path: Path) -> ArtifactStore:
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    return ArtifactStore(tmp_path / "artifacts", run_id="R", ledger=lg)


def _commit_python(store: ArtifactStore, tmp_path: Path, path: str) -> None:
    task = Task(id="T-1", title="t", description="d", role="backend_dev",
                acceptance_criteria=["ok"],
                deliverables=[ArtifactSpec(path=path, kind="python", description="d")])
    store.commit(
        path,
        '"""模块说明。"""\nclass A:\n    def run(self):\n        pass\n',
        spec=task.deliverables[0],
        task=task,
    )


def test_summarize_records_batch_concatenates(tmp_path: Path):
    store = _make_store(tmp_path)
    _commit_python(store, tmp_path, "a.py")
    _commit_python(store, tmp_path, "b.py")
    text = summarize_records(store.records(), store)
    assert "a.py" in text
    assert "b.py" in text
    assert "[python 摘要]" in text


def test_summarize_records_empty_returns_empty(tmp_path: Path):
    store = _make_store(tmp_path)
    assert summarize_records([], store) == ""


def test_summarize_records_truncates_to_total(tmp_path: Path):
    store = _make_store(tmp_path)
    for i in range(5):
        _commit_python(store, tmp_path, f"m{i}.py")
    text = summarize_records(store.records(), store, max_total=200)
    assert len(text) <= 200


# ── BaseAgent.summarize 钩子 ────────────────────────────────────────────


class _PlainAgent(BaseAgent):
    """最小具体 Agent：只测 summarize 钩子，不跑 execute。"""

    spec = RoleSpec(
        key="backend_dev", title="后端", goal="g", backstory="b",
        responsibilities=["r"], boundaries=["b"], output_contract="c",
        system_prompt="你是后端工程师。",
    )


def _make_plain_agent(store: ArtifactStore) -> _PlainAgent:
    return _PlainAgent(
        _PlainAgent.spec,
        models=None,  # summarize 不触碰 models
        store=store,
        ledger=Ledger(Path(store.root).parent / "l.jsonl", run_id="R"),
        project_goal="目标",
    )


def test_summarize_uses_artifact_summary_when_artifacts(tmp_path: Path):
    store = _make_store(tmp_path)
    _commit_python(store, tmp_path, "a.py")
    record = store.records()[0]
    now = datetime.now()
    result = TaskResult(
        task_id="T-1", role="backend_dev", summary="", artifacts=[record],
        notes="", started_at=now, finished_at=now, duration_ms=0,
    )
    agent = _make_plain_agent(store)
    summary = agent.summarize(result)
    # 产物正文摘要：应含 python 结构摘要，而非旧的「产出 N 个产物」措辞。
    assert "[python 摘要]" in summary
    assert "a.py" in summary
    assert "类A" in summary or "定义" in summary


def test_summarize_merges_notes_after_artifact_summary(tmp_path: Path):
    store = _make_store(tmp_path)
    _commit_python(store, tmp_path, "a.py")
    record = store.records()[0]
    now = datetime.now()
    result = TaskResult(
        task_id="T-1", role="backend_dev", summary="", artifacts=[record],
        notes="补充说明文字", started_at=now, finished_at=now, duration_ms=0,
    )
    agent = _make_plain_agent(store)
    summary = agent.summarize(result)
    artifact_pos = summary.index("[python 摘要]")
    notes_pos = summary.index("补充说明文字")
    # 产物摘要在前，notes 在后。
    assert artifact_pos < notes_pos


def test_summarize_falls_back_to_notes_without_artifacts(tmp_path: Path):
    """向后兼容：无产物时回落到原 notes 截断逻辑。"""
    store = _make_store(tmp_path)
    now = datetime.now()
    long_notes = "长" * 1000
    result = TaskResult(
        task_id="T-1", role="backend_dev", summary="", artifacts=[],
        notes=long_notes, started_at=now, finished_at=now, duration_ms=0,
    )
    agent = _make_plain_agent(store)
    assert agent.summarize(result) == long_notes[:400]

    empty = result.model_copy(update={"notes": ""})
    assert agent.summarize(empty) == "执行完成，未产出文件"
