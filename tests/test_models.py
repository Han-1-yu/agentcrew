"""领域模型的行为测试（spec §5.1/§5.2/§5.3）。"""

import json
from datetime import datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from agentcrew.artifacts.spec import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactSpec,
    Manifest,
)
from agentcrew.task.task import (
    ReviewRecord,
    RunStats,
    StatusChange,
    Task,
    TaskResult,
    TaskStatus,
)


def mk_task(**overrides) -> Task:
    """字段齐全的 Task；``overrides`` 覆盖同名字段。"""
    fields = dict(
        id="T-001",
        title="x",
        description="d",
        role="backend_dev",
        acceptance_criteria=["a"],
        deliverables=[],
        dependencies=[],
    )
    fields.update(overrides)
    return Task.model_validate(fields)


def test_task_status_has_exactly_ten_members():
    assert len(TaskStatus) == 10
    assert TaskStatus("rework") is TaskStatus.REWORK


def test_task_status_member_values_match_spec():
    assert {member.value for member in TaskStatus} == {
        "pending",
        "ready",
        "running",
        "review",
        "rework",
        "succeeded",
        "failed",
        "blocked",
        "cancelled",
        "skipped",
    }


def test_task_status_is_str_enum():
    # 必须序列化成裸字符串（ledger / snapshot 依赖）
    assert TaskStatus.PENDING == "pending"
    assert json.dumps(TaskStatus.PENDING) == '"pending"'


def test_task_defaults():
    t = mk_task()
    assert t.status is TaskStatus.PENDING
    assert t.attempts == 0 and t.rework_count == 0
    assert t.max_attempts == 3 and t.max_reworks == 2
    assert t.priority == 5
    assert t.reviews == [] and t.status_history == []
    assert t.brief == "" and t.result is None


def test_task_timestamps_default_to_now():
    before = datetime.now()
    t = mk_task()
    after = datetime.now()
    assert before <= t.created_at <= after
    assert before <= t.updated_at <= after


def test_task_rejects_extra_fields():
    with pytest.raises(ValidationError):
        mk_task(bogus=1)


def test_task_rejects_unknown_status():
    with pytest.raises(ValidationError):
        mk_task(status="done")


def test_artifact_spec_rejects_unsafe_path():
    with pytest.raises(ValidationError):
        ArtifactSpec(path="../escape.py", kind="python", description="x")


def test_artifact_spec_accepts_chinese_path():
    s = ArtifactSpec(path="docs/接口文档.md", kind="markdown", description="x")
    assert s.path == "docs/接口文档.md"


def test_artifact_spec_normalizes_path():
    assert ArtifactSpec(path="./a.py", kind="python", description="x").path == "a.py"
    assert ArtifactSpec(path="a//b.py", kind="python", description="x").path == "a/b.py"


def test_artifact_spec_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        ArtifactSpec(path="a.txt", kind="brainfuck", description="x")


def test_artifact_spec_defaults():
    s = ArtifactSpec(path="a.py", kind="python", description="x")
    assert s.required is True
    assert s.min_bytes == 1
    assert s.must_contain == []
    assert s.max_bytes == 524288


def test_artifact_kind_has_exactly_twelve_values():
    assert get_args(ArtifactKind) == (
        "python",
        "json",
        "yaml",
        "toml",
        "markdown",
        "text",
        "html",
        "css",
        "js",
        "dockerfile",
        "sql",
        "shell",
    )


def test_priority_bounds():
    with pytest.raises(ValidationError):
        mk_task(priority=0)
    with pytest.raises(ValidationError):
        mk_task(priority=10)
    assert mk_task(priority=1).priority == 1
    assert mk_task(priority=9).priority == 9


def test_review_record_requires_issues_when_rejected():
    # 评审记录本身不做业务校验，但 round/verdict/layer 必须合法
    r = ReviewRecord(task_id="T-1", round=1, layer="static", verdict="reject",
                     issues=["缺失产物"], fix_hints=[], reviewer="static:contract",
                     created_at=datetime.now())
    assert r.verdict == "reject"
    assert r.score is None


def test_review_record_rejects_bad_layer_and_verdict():
    with pytest.raises(ValidationError):
        ReviewRecord(task_id="T-1", round=1, layer="vibes", verdict="approve",
                     reviewer="r", created_at=datetime.now())
    with pytest.raises(ValidationError):
        ReviewRecord(task_id="T-1", round=1, layer="static", verdict="maybe",
                     reviewer="r", created_at=datetime.now())


def test_status_change_captures_transition():
    at = datetime.now()
    c = StatusChange(from_status=TaskStatus.PENDING, to_status=TaskStatus.READY,
                     reason="deps met", at=at)
    assert c.from_status is TaskStatus.PENDING
    assert c.to_status is TaskStatus.READY
    assert c.reason == "deps met"
    assert c.at == at


def test_task_result_records_artifacts_and_duration():
    record = ArtifactRecord(path="a.py", kind="python", size_bytes=3,
                            sha256="0" * 64, produced_by_task="T-001",
                            produced_by_role="backend_dev",
                            written_at=datetime.now())
    started = datetime.now()
    r = TaskResult(task_id="T-001", role="backend_dev", summary="done",
                   artifacts=[record], notes="n", started_at=started,
                   finished_at=started, duration_ms=5)
    assert r.artifacts == [record]
    assert r.notes == "n" and r.duration_ms == 5


def test_manifest_holds_run_metadata_and_entries():
    record = ArtifactRecord(path="a.py", kind="python", size_bytes=3,
                            sha256="0" * 64, produced_by_task="T-001",
                            produced_by_role="backend_dev",
                            written_at=datetime.now())
    m = Manifest(run_id="20261005-120000-abcd", generated_at=datetime.now(),
                 entries=[record])
    assert m.entries == [record]


def test_run_stats_defaults_zero():
    s = RunStats()
    assert s.total_tasks == 0 and s.estimated_cost_usd == 0.0
    assert s.succeeded == 0 and s.failed == 0
    assert s.blocked == 0 and s.skipped == 0
    assert s.llm_calls == 0
    assert s.prompt_tokens == 0 and s.completion_tokens == 0
    assert s.started_at is None and s.finished_at is None


def _minimal_kwargs() -> list[tuple[type, dict]]:
    now = datetime.now()
    return [
        (Task, dict(id="T", title="t", description="d", role="r",
                    acceptance_criteria=[], deliverables=[])),
        (TaskResult, dict(task_id="T", role="r", summary="s", artifacts=[],
                          started_at=now, finished_at=now, duration_ms=0)),
        (ReviewRecord, dict(task_id="T", round=1, layer="static",
                            verdict="approve", reviewer="r", created_at=now)),
        (StatusChange, dict(from_status=TaskStatus.PENDING,
                            to_status=TaskStatus.READY, at=now)),
        (RunStats, dict()),
        (ArtifactSpec, dict(path="a.py", kind="python", description="x")),
        (ArtifactRecord, dict(path="a.py", kind="python", size_bytes=0,
                              sha256="0" * 64, produced_by_task="T",
                              produced_by_role="r", written_at=now)),
        (Manifest, dict(run_id="run", generated_at=now, entries=[])),
    ]


@pytest.mark.parametrize("model, kwargs", _minimal_kwargs(),
                         ids=lambda value: getattr(value, "__name__", None))
def test_every_model_forbids_extra_fields(model, kwargs):
    with pytest.raises(ValidationError):
        model(**kwargs, bogus=1)
