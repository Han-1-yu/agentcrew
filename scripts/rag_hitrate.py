"""B5：文档 RAG 的命中率对照（混合权重 0 / 0.5 / 1.0，离线可跑）。

**为什么能离线跑。** 文档检索是 BM25 + 向量两路加权，而向量路在没有真实
embedding 端点时会退化成 :class:`HashEmbedder`（字符 n-gram 哈希，纯 CPU、
确定性）。所以「``docs_rag_vector_weight`` 取多少」这件事不需要买 key 就能量：
本脚本在临时目录里建索引（不碰 ``docs/``，也不会写 manifest 进仓库），
对同一批查询分别用不同权重检索，比较命中率。

**语料与问题集从哪来。** 缺的从来不是「真实文档」，而是「有标注的问答对」。
这里用仓库自己的 ``docs/*.md`` 当语料，用每篇文档的二级/三级标题当查询、
标题所在文档当正确答案——标注免费且与文档同步，缺点也写明：标题词通常
出现在正文里，所以该指标偏乐观，量的是「能不能把自己文档里的东西找回来」，
不是「能不能理解用户的口语提问」。

用法::

    python scripts/rag_hitrate.py --docs-dir docs --limit 5

退出码：跑通 0；语料为空 2。结果表写进 stdout，``--json-out`` 可另存。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentcrew.knowledge.docs import DocStore  # noqa: E402
from agentcrew.knowledge.embeddings import HashEmbedder  # noqa: E402

# 这些标题太泛，任何文档里都有，留着只会把命中率变成噪声。
_SKIP_HEADINGS = {
    "目录", "背景", "目标", "现状", "概述", "摘要", "前言", "附录", "参考",
    "参考文档", "相关文档", "变更记录", "更新记录", "验证", "结论", "总结",
    "用法", "示例", "说明", "设计", "实现", "问题", "风险", "待办", "路线图",
}

_HEADING_RE = re.compile(r"^#{2,3}\s+(.+?)\s*$")
_NUMBERING_RE = re.compile(r"^[\d.、一二三四五六七八九十()（）]+\s*")


def _iter_docs(docs_dir: Path) -> list[Path]:
    return sorted(p for p in docs_dir.rglob("*.md") if p.is_file())


def _queries_from(docs_dir: Path, docs: list[Path]) -> list[tuple[str, str]]:
    """(查询词, 正确文档相对路径) —— 取二/三级标题，去掉编号与符号。"""
    out: list[tuple[str, str]] = []
    for path in docs:
        relative = path.relative_to(docs_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            match = _HEADING_RE.match(line)
            if not match:
                continue
            query = _NUMBERING_RE.sub("", match.group(1)).strip(" ：:-")
            if len(query) < 4 or query in _SKIP_HEADINGS:
                continue
            out.append((query, relative))
    return out


def _rank_of(hits: list, expected: str) -> int:
    """正确文档第一次出现在第几位（1 起；没出现返回 0）。"""
    for index, hit in enumerate(hits, start=1):
        if hit.chunk.doc == expected:
            return index
    return 0


async def _measure(
    staged: Path, queries: list[tuple[str, str]], *, weight: float, limit: int,
) -> dict[str, float]:
    store = DocStore(staged, embedder=HashEmbedder())
    summary = await store.ingest()
    ranks: list[int] = []
    for query, expected in queries:
        hits = store.query_sync(query, limit=limit, vector_weight=weight)
        ranks.append(_rank_of(hits, expected))
    total = len(ranks) or 1
    return {
        "docs": float(summary.docs),
        "chunks": float(summary.chunks),
        "queries": float(len(ranks)),
        "hit@1": sum(1 for r in ranks if r == 1) / total,
        "hit@3": sum(1 for r in ranks if 1 <= r <= 3) / total,
        "miss": sum(1 for r in ranks if r == 0) / total,
        "mrr": sum((1.0 / r) for r in ranks if r) / total,
    }


async def _run(args: argparse.Namespace) -> int:
    docs_dir = Path(args.docs_dir).resolve()
    docs = _iter_docs(docs_dir)
    if not docs:
        print(f"语料为空：{docs_dir} 下没有 .md", file=sys.stderr)
        return 2
    queries = _queries_from(docs_dir, docs)
    print(f"语料 {len(docs)} 篇 · 查询 {len(queries)} 条（标题即查询）")

    weights = [float(item) for item in args.weights.split(",")]
    rows: list[dict[str, float | str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "docs"
        staged.mkdir()
        for path in docs:
            target = staged / path.relative_to(docs_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        for weight in weights:
            stats = await _measure(
                staged, queries, weight=weight, limit=args.limit)
            row: dict[str, float | str] = {"weight": weight, **stats}
            rows.append(row)
            print(
                f"weight={weight:.2f} · {int(stats['docs'])} 篇 / "
                f"{int(stats['chunks'])} 块 · hit@1={stats['hit@1']:.1%} · "
                f"hit@3={stats['hit@3']:.1%} · MRR={stats['mrr']:.3f} · "
                f"未命中={stats['miss']:.1%}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "docs_dir": str(docs_dir),
            "queries": len(queries),
            "limit": args.limit,
            "rows": rows,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 写入 {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs-dir", default="docs", help="语料目录（默认 docs）")
    parser.add_argument("--weights", default="0,0.5,1",
                        help="逗号分隔的混合权重，默认 0,0.5,1")
    parser.add_argument("--limit", type=int, default=5, help="每次检索的条数")
    parser.add_argument("--json-out", default="", help="把结果写成 JSON")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
