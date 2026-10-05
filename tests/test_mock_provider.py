import json

import pytest

from agentcrew.models.config import parse_alias
from agentcrew.models.providers.mock import MockProvider, set_purpose


@pytest.fixture
def fixtures(tmp_path):
    d = tmp_path / "fixtures"
    (d / "responses" / "T-001").mkdir(parents=True)
    (d / "responses" / "T-001" / "attempt-1.md").write_text(
        "```python file=a.py\nx = 1\n```\n", encoding="utf-8")
    (d / "responses" / "T-002").mkdir(parents=True)
    (d / "responses" / "T-002" / "attempt-2.md").write_text(
        "```python file=a.py\nz = 3\n```\n", encoding="utf-8")
    (d / "responses" / "T-003").mkdir(parents=True)
    (d / "responses" / "T-003" / "attempt-1.md").write_text("bad\n", encoding="utf-8")
    (d / "responses" / "T-003" / "attempt-2.md").write_text(
        "```python file=a.py\ny = 2\n```\n", encoding="utf-8")
    (d / "plan_todo.json").write_text(
        json.dumps({"tasks": [{"id": "T-001", "title": "t", "description": "d",
                               "role": "backend_dev", "acceptance_criteria": ["a"],
                               "deliverables": [], "dependencies": []}]}),
        encoding="utf-8")
    (d / "reviews.json").write_text(json.dumps({"T-001": {"verdict": "approve",
                                                          "score": 0.9,
                                                          "issues": [], "fix_hints": []}}),
                                    encoding="utf-8")
    return d


@pytest.fixture
def provider(fixtures):
    return MockProvider(parse_alias("mock:mock-model"), fixtures_dir=fixtures)


async def test_returns_task_attempt_fixture(provider, fixtures):
    set_purpose(provider, "execute", role="backend_dev", task_id="T-001", attempt=1)
    r = await provider.complete([{"role": "user", "content": "go"}],
                                temperature=0, max_tokens=100)
    assert "x = 1" in r.text
    assert r.provider == "mock" and r.latency_ms == 0


async def test_second_attempt_reads_different_fixture(provider):
    set_purpose(provider, "execute", role="qa_engineer", task_id="T-003", attempt=1)
    a = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    set_purpose(provider, "execute", role="qa_engineer", task_id="T-003", attempt=2)
    b = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert a.text != b.text and "y = 2" in b.text


async def test_falls_back_to_attempt_1_when_later_missing(provider):
    set_purpose(provider, "execute", role="r", task_id="T-001", attempt=5)
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert "x = 1" in r.text


async def test_plan_purpose_reads_plan_fixture(provider):
    set_purpose(provider, "plan")
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert "T-001" in r.text


async def test_review_purpose_reads_reviews_fixture(provider):
    set_purpose(provider, "review", role="architect", task_id="T-001", attempt=1)
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert "approve" in r.text


async def test_missing_fixture_falls_back_and_reports(provider):
    seen = []
    provider._on_fallback = seen.append
    set_purpose(provider, "execute", role="r", task_id="T-999", attempt=1)
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert r.text
    assert seen and "T-999" in seen[0]


async def test_tokens_are_estimated_not_zero(provider):
    set_purpose(provider, "execute", role="r", task_id="T-001", attempt=1)
    r = await provider.complete([{"role": "user", "content": "hello there"}],
                                temperature=0, max_tokens=10)
    assert r.prompt_tokens > 0 and r.completion_tokens > 0


async def test_latency_is_configurable(fixtures):
    p = MockProvider(parse_alias("mock:m"), fixtures_dir=fixtures, latency_ms=1)
    set_purpose(p, "execute", role="r", task_id="T-001", attempt=1)
    r = await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert r.latency_ms >= 1


async def test_only_later_attempt_exists_serves_it_without_report(provider):
    """仅存在 attempt-2.md 时，任何 attempt 都返回同一内容且不上报（spec §16.1）。"""
    seen = []
    provider._on_fallback = seen.append
    for attempt in (1, 2, 5):
        set_purpose(provider, "execute", role="r", task_id="T-002", attempt=attempt)
        r = await provider.complete([{"role": "user", "content": "x"}],
                                    temperature=0, max_tokens=10)
        assert "z = 3" in r.text
    assert seen == []


async def test_exact_attempt_wins_and_attempt_1_substitution_reports(provider):
    seen = []
    provider._on_fallback = seen.append
    set_purpose(provider, "execute", role="r", task_id="T-003", attempt=2)
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert "y = 2" in r.text and seen == []          # 精确命中，不上报
    set_purpose(provider, "execute", role="r", task_id="T-003", attempt=5)
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert r.text == "bad\n"                          # 回退到 attempt-1.md
    assert seen and "T-003" in seen[0] and "attempt-1" in seen[0]


async def test_no_attempt_files_returns_placeholder_and_reports(provider):
    seen = []
    provider._on_fallback = seen.append
    set_purpose(provider, "execute", role="r", task_id="T-999", attempt=1)
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert r.text == "# 占位回复（fixture 缺失）\ntask=T-999 attempt=1\n"
    assert len(seen) == 1 and "T-999" in seen[0]
