按设计实现服务层：create_app 暴露一个检索路由，打分与排序全部委托给索引层，本文件不实现任何检索逻辑。

```python file=kb/api.py
"""服务层：应用工厂与检索路由。

设计约束（docs/design.md §1）：本模块依赖 kb.index，反向不成立；
因此这里只做参数解析与序列化，任何打分逻辑都属于索引层。
"""

from __future__ import annotations

from collections.abc import Callable

from kb.index import InvertedIndex


def _serialize(index: InvertedIndex, doc_id: str, hits: int) -> dict:
    """把一条命中结果序列化成对外结构。

    PRD §3 规定每个结果必须有：文档 id、命中关键词数、标题与摘要片段。
    标题取正文首行，摘要取前 120 个字符 —— 检索路径上不做额外 IO。
    """
    text = index.document(doc_id) or ""
    title = text.strip().splitlines()[0] if text.strip() else doc_id
    return {
        "doc_id": doc_id,
        "hits": hits,
        "title": title,
        "snippet": text.strip()[:120],
    }


def create_app(index: InvertedIndex | None = None) -> dict:
    """构造一个可挂载的应用对象。

    返回字典而不是框架应用：本项目不引入 Web 框架，宿主（HTTP 服务、CLI、
    测试）自己决定怎么把 ``search`` 暴露出去。这样用例层不必启动进程就能
    验证「路由返回的顺序 == 索引层给的顺序」。
    """
    idx = index if index is not None else InvertedIndex()

    def search(query: str) -> list[dict]:
        """检索入口：空白分隔的关键词按「与」语义求交。"""
        terms = query.split()
        if not terms:
            # PRD AC-04：空查询返回空列表，不能退化成「返回全部」
            return []
        ranked = idx.query(terms)
        return [_serialize(idx, doc_id, hits) for doc_id, hits in ranked]

    def routes() -> dict[str, Callable[..., object]]:
        return {"search": search, "index": idx}

    return {"index": idx, "search": search, "routes": routes}
```

服务层只做序列化，检索与排序全部落在索引层，符合设计文档的分层约束。
