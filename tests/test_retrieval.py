"""R11：中文二元组 + BM25 检索。

原来的做法是「按单字切分、数命中个数」，两个问题在真实数据上都会咬人：

- **单字太粗**：「写代码」与「写文档」共享一个「写」就互相命中。
- **计数不分轻重**：「的」与「幂等」权重相同，长文本天然占优。

这一版：分词补 CJK 相邻二元组、打分换 BM25（TF 饱和 + 长度归一 + IDF）。
覆盖分词规则、打分特性，以及经验库端到端的「近似问题能命中、无关问题不命中」。
"""

from __future__ import annotations

import pytest

from agentcrew.knowledge.retrieval import BM25Index, tokenize

# ── 分词 ──────────────────────────────────────────────────────────────────


def test_tokenize_adds_cjk_bigrams():
    tokens = tokenize("后端")

    assert "后" in tokens and "端" in tokens
    assert "后端" in tokens


def test_tokenize_keeps_english_words_whole():
    tokens = tokenize("CRUD todo 接口")

    assert "crud" in tokens and "todo" in tokens
    assert "接口" in tokens


def test_tokenize_does_not_span_punctuation():
    """二元组不跨标点：否则「接口，实现」会拼出一个不存在的「口，」。"""
    tokens = tokenize("接口，实现")

    assert "接口" in tokens and "实现" in tokens
    assert "口，" not in tokens
    assert "，实" not in tokens


def test_tokenize_ignores_empty_text():
    assert tokenize("") == []
    assert tokenize("   ") == []


# ── 打分：单字共享不再等于相关 ────────────────────────────────────────────


def test_shared_single_char_does_not_dominate():
    """核心改进：都含「写」但主题不同的两篇，必须由二元组区分开。"""
    documents = ["写代码实现待办接口", "写文档整理需求说明"]
    index = BM25Index(documents)

    hits = index.search("写代码", limit=2)

    assert hits, "至少应命中一篇"
    assert hits[0][0] == 0, "「写代码」应命中代码那篇"
    scores = dict(hits)
    if 1 in scores:  # 文档 1 只共享一个「写」，分数必须明显更低
        assert scores[1] < scores[0] / 2


def test_phrase_match_beats_single_char():
    """词组命中要显著优于只沾单字。"""
    documents = ["实现后端接口", "把文档整理成册"]
    index = BM25Index(documents)

    hits = index.search("后端", limit=2)

    assert hits[0][0] == 0
    assert len(hits) == 1, "只共享单字的那篇不该被算作命中"


def test_length_normalization_favours_precise_document():
    """同样含目标词，啰嗦的文档不该因为更长而占优。"""
    short = "待办接口"
    long = "待办接口" + "，另外还有很多不相关的说明文字" * 6
    index = BM25Index([short, long])

    hits = dict(index.search("待办接口", limit=2))

    assert hits[0] >= hits[1]


def test_no_hit_returns_empty():
    index = BM25Index(["写代码实现待办接口"])

    assert index.search("量子引力弦论", limit=3) == []


def test_empty_index_and_zero_limit():
    assert BM25Index([]).search("任何东西", limit=3) == []
    assert BM25Index(["待办"]).search("待办", limit=0) == []


def test_query_tokens_are_deduplicated():
    """问题里重复同一个词不代表它更重要——TF 由文档侧决定。"""
    index = BM25Index(["待办接口"])

    once = index.search("待办", limit=1)[0][1]
    repeated = index.search("待办 待办 待办", limit=1)[0][1]

    assert once == pytest.approx(repeated)


# ── 经验库端到端 ──────────────────────────────────────────────────────────


def _store(tmp_path):
    from datetime import datetime

    from agentcrew.knowledge.experience import Experience, ExperienceStore

    store = ExperienceStore(tmp_path)
    for index, (description, role) in enumerate([
        ("实现待办事项的 CRUD 接口", "backend_dev"),
        ("整理产品需求文档与验收标准", "product_manager"),
        ("搭建前端任务列表页面", "frontend_dev"),
    ]):
        store.add(Experience(
            id=f"e{index}", task_description=description, role=role,
            solution_summary=f"针对「{description}」的做法",
            artifacts=[], key_decisions=[], success=True,
            run_id="R", created_at=datetime(2026, 1, 1 + index),
        ))
    return store


def test_experience_query_finds_close_task(tmp_path):
    store = _store(tmp_path)

    hits = store.query("给待办事项加增删改查接口")

    assert hits
    assert hits[0].role == "backend_dev"


def test_experience_query_respects_role_filter(tmp_path):
    store = _store(tmp_path)

    hits = store.query("待办接口", role="frontend_dev")

    assert all(hit.role == "frontend_dev" for hit in hits)


def test_experience_query_rejects_unrelated(tmp_path):
    store = _store(tmp_path)

    assert store.query("量子引力弦论 M 理论") == []


def test_experience_query_empty_store(tmp_path):
    from agentcrew.knowledge.experience import ExperienceStore

    assert ExperienceStore(tmp_path).query("任何东西") == []


def test_experience_inject_context_unchanged_format(tmp_path):
    """检索实现换了，注入的文本格式不能变（下游提示词依赖它）。"""
    store = _store(tmp_path)

    text = store.inject_context("待办 CRUD 接口", limit=1)

    assert text.startswith("## 历史经验（来自之前的成功 run）")
    assert "### 经验 1：" in text
    assert "- 角色：" in text


def test_single_char_overlap_is_not_a_hit():
    """真实踩到的坑：「量子引力弦论 M 理论」会因为一个「理」字命中「整理需求文档」。

    单字命中在中文语料里几乎必然发生，把它当相关等于把无关经验端进提示词。
    """
    index = BM25Index(["整理产品需求文档"])

    assert index.search("量子引力弦论 M 理论", limit=3) == []


def test_single_char_query_returns_empty():
    """纯单字查询不足以说清要找什么：宁可返回空，也不要按单字乱捞。"""
    index = BM25Index(["待办接口"])

    assert index.search("办", limit=3) == []
