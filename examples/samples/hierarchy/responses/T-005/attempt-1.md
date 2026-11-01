按 PRD 的验收标准写了索引与检索链路的用例，覆盖多词交集、空查询、增删后一致性三类边界，全部离线可跑。

```python file=tests/test_index.py
"""索引层与检索链路的用例（PRD AC-01 ~ AC-05）。"""

from kb.api import create_app
from kb.index import InvertedIndex, tokenize


def build() -> InvertedIndex:
    idx = InvertedIndex()
    idx.add("d1", "消息队列 选型 复盘 为什么 不用 kafka")
    idx.add("d2", "消息队列 容量 评估 kafka 吞吐")
    idx.add("d3", "值班 手册 故障 处理 流程")
    return idx


def test_tokenize_splits_chinese_into_bigrams():
    assert tokenize("消息队列") == ["消息", "息队", "队列"]
    assert tokenize("Kafka 吞吐") == ["kafka", "吞吐"]


def test_single_term_returns_all_matching_docs():
    """AC-01：单关键词返回全部包含该词的文档。"""
    idx = build()
    got = {doc_id for doc_id, _ in idx.query(["消息队列"])}
    assert got == {"d1", "d2"}


def test_multi_term_uses_and_semantics():
    """AC-02：多关键词只返回同时包含全部关键词的文档。

    「kafka」只出现在 d1、d2；「容量」只出现在 d2 —— 交集必须只剩 d2，
    若实现退化成「或」语义，这里会多出 d1。
    """
    idx = build()
    assert [doc_id for doc_id, _ in idx.query(["kafka", "容量"])] == ["d2"]


def test_empty_query_returns_empty_list():
    """AC-04：空查询返回空列表，不能退化成返回全部文档。"""
    assert build().query([]) == []
    assert build().query(["   "]) == []


def test_ranking_is_stable_by_hits_then_doc_id():
    """AC-03：命中词数降序、同分按文档 id 升序 —— 结果必须可复现。"""
    idx = InvertedIndex()
    idx.add("b", "队列 复盘")
    idx.add("a", "队列 复盘")
    idx.add("c", "队列")
    assert idx.query(["队列"]) == [("a", 1), ("b", 1), ("c", 1)]


def test_remove_makes_doc_unfindable():
    """AC-05：删除后再次查询不应再出现该文档。"""
    idx = build()
    assert idx.remove("d2") is True
    assert [d for d, _ in idx.query(["kafka"])] == ["d1"]
    assert idx.remove("d2") is False


def test_readd_clears_stale_terms():
    """重新索引同一 id 时不能留下旧词条，否则会搜到已经改掉的内容。"""
    idx = InvertedIndex()
    idx.add("d1", "旧的 关键词")
    idx.add("d1", "新的 描述")
    assert idx.query(["旧的"]) == []
    assert [d for d, _ in idx.query(["新的"])] == ["d1"]


def test_api_search_matches_index_order():
    """服务层的检索结果顺序必须与索引层一致 —— 两条实现路径的汇合点。"""
    idx = build()
    app = create_app(idx)
    results = app["search"]("kafka")
    assert [r["doc_id"] for r in results] == [d for d, _ in idx.query(["kafka"])]
    assert all({"doc_id", "hits", "title", "snippet"} <= set(r) for r in results)
```

八条用例覆盖 PRD 的五条验收标准，另加分词、重复索引与两层顺序一致性三条回归保护。
