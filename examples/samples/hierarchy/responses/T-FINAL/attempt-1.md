汇总本次六个任务的交付成果，输出 DELIVERY.md：完成清单、验收结论、遗留问题与运行方式。

````markdown file=DELIVERY.md
# 交付总结：内部知识库检索服务

## 完成清单

| 任务 | 角色 | 交付物 |
|---|---|---|
| T-001 需求定义 | 产品经理 | `docs/PRD.md` |
| T-002 架构与分层设计 | 架构师 | `docs/design.md` |
| T-003 检索服务接口 | 后端开发 | `kb/api.py` |
| T-004 倒排索引实现 | 后端开发 | `kb/index.py` |
| T-005 索引与检索用例 | 测试工程师 | `tests/test_index.py` |
| T-006 接入与使用文档 | 文档工程师 | `README.md` |

共 6 个计划产物全部落盘，另有本文件与 `MANIFEST.json` 作为收尾记录。

## 验收结论

- 全部 7 个任务（含本收尾任务）通过 G1 静态校验与 G2 评审，无失败、无阻塞。
- 本次是**层级管理**形状：T-002 之后分出两条并行实现路径（索引层 / 服务层），
  两条路径都完成才解锁用例任务，最后汇入本收尾任务 —— 汇合点由
  `test_api_search_matches_index_order` 用例守住。
- PRD 的 AC-01 至 AC-05 在 `tests/test_index.py` 中逐条对应用例，无遗漏。

## 遗留问题

- 索引为纯内存结构，进程重启需重建；持久化不在本期范围。
- 不引入 TF-IDF 权重，多文档同分时按 id 排序 —— 语料规模上万后需重新评估排序质量。
- 中文按双字切分，未接入分词模型；专有名词的召回率未做量化评估。

## 运行方式

```bash
python -m pytest tests/test_index.py -q
python -c "from kb.api import create_app; from kb.index import InvertedIndex; i = InvertedIndex(); i.add('d1', '消息队列 选型'); print(create_app(i)['search']('消息队列'))"
```

检索语义见 `docs/PRD.md` 第 3 节，分层约束见 `docs/design.md` 第 1 节。
````

DELIVERY.md 已按收尾契约产出，完成清单与用例覆盖可逐项核对。
