# 文档 RAG 命中率对照（2026-11-13）

**问题**：`docs_rag_vector_weight` 默认 0.5（BM25 与向量各占一半）。但向量通道在没有
真实 embedding 端点时会退化成 `HashEmbedder`（字符 n-gram 哈希），它对齐的是
**字面片段**而不是语义——那 0.5 这个默认值到底是帮忙还是帮倒忙？

**答案（可复现）**：帮倒忙。只有哈希兜底时，向量权重越高命中率越低。已改成
「哈希兜底 → 实际权重压到 0，并留一条 `docs_vector_degraded` 事件」。

## 怎么测的

```bash
python scripts/rag_hitrate.py --docs-dir docs --limit 5 \
    --json-out output/_eval/rag-hitrate.json
```

- **语料**：仓库自己的 `docs/*.md`，32 篇 / 1233 块（在临时目录里建索引，不写
  `docs/` 的 manifest，不污染仓库）；
- **问题集**：每篇文档的二/三级标题（去掉编号），共 **486 条**，正确答案＝标题
  所在文档；
- **指标**：文档级 hit@1 / hit@3 / MRR / 未命中率；
- **对照**：同一批查询、同一语料，只改 `vector_weight`（0 / 0.5 / 1.0）。

## 结果

| 权重 | hit@1 | hit@3 | MRR | 未命中 |
| --- | --- | --- | --- | --- |
| **0.0（纯 BM25）** | **84.4%** | **95.7%** | **0.897** | **3.7%** |
| 0.5（旧默认） | 82.9% | 94.0% | 0.885 | 3.9% |
| 1.0（纯向量） | 53.3% | 77.2% | 0.655 | 15.4% |

结论直白：把哈希向量按 50% 混进去，hit@1 掉 1.5pp、hit@3 掉 1.7pp；纯向量更是
掉到 53.3%。**哈希向量在这里是噪声，不是补充。**

## 处置

| 改动 | 说明 |
| --- | --- |
| `agentcrew/knowledge/embeddings.py` | 新增 `is_semantic_embedder()`：识别 `HashEmbedder` 与 `CachedEmbedder(HashEmbedder)`（缓存外壳不骗人） |
| `agentcrew/crew/crew.py` | `_docs_effective_weight()`：兜底通道下把实际权重压到 0，并 emit `docs_vector_degraded`（`configured_weight` / `effective_weight` / `reason`），**降级可见** |
| `tests/test_docs_rag.py` | 新增 2 例：`is_semantic_embedder` 的三种形态；Crew 层「哈希 → 0 + 留事件」「真语义 → 照配置、不留事件」 |

没有改的：配置项本身（默认仍是 0.5，配了真实 embedding 端点时它就该是 0.5）、
BM25 实现、判定门槛。

## 这个指标的边界（别把它读大）

1. **标题即查询**：标题词通常也出现在正文里，所以这是「能不能把自己文档里的东西
   找回来」的下界偏乐观测法，不代表「理解用户口语提问」的能力；
2. **哈希不是语义**：本结论只说明「哈希兜底 + 0.5 有害」，**不能**外推成
   「真实 embedding 端点也无用」——那需要真端点重跑同一脚本；
3. **语料是我们自己的文档**：换业务语料（术语密度、同义词分布不同）结论可能变化，
   所以脚本留着，换语料重跑即可。

复跑命令即页面顶部那条；JSON 明细在 `output/_eval/rag-hitrate.json`（未入版本库）。
