# AgentCrew 能力补齐验收报告（R51–R54 · 2026-11-12）

> **被验收对象**：`完善/AgentCrew落地完善路线-2026-11-12.md`（C1–C14）的第一批（P0）
> 与第二批（P1）。P2 按需项（C9/C10/C13/C14）不在本次范围，状态见文末。
>
> **总评**：**通过**。P0/P1 共 9 项能力全部落地，新增 116 例测试，全量
> **3231 passed / 0 failed / 2 skipped**，CI 连续四轮全绿（六 job：四版本矩阵
> + Windows uv + Studio Node 20）。每项的「验收标准」逐条对标如下——
> **已验证**指本机可复现的测试/运行证据；**待真机**指需要 Docker、真实模型
> 密钥或第三方客户端的环境，已在 PROJECT-STATE §7 挂账，不冒充已验证。

---

## 1. 逐项验收（对标路线文档 §2 的验收标准）

### C1 联网搜索（R51）｜✅ 通过（本机）

| 验收标准（路线文档） | 结果 | 证据 |
| --- | --- | --- |
| mock 端到端进 linear 样例 | ✅ | `examples/evals/capabilities.jsonl` 4 条 mock 接线 case 全过（`bench/eval-capabilities-offline-2026-11-12.json`）；`tests/test_web_search.py` 17 例 |
| 四通道登记齐全 | ✅ | env / YAML / export-import / `GET /tools`（`search_provider`）；`sandbox_backend` 刻意不开进 API options（防隔离降级），`tests/test_capability_config.py` 8 例 |
| 真实通道 smoke 一次 | ⏳ 待真机 | 需 TAVILY/Brave 密钥；`main.py tool web_search` 已就绪 |

### C2 沙箱执行后端（R51）｜✅ 通过（本机）

| 验收标准 | 结果 | 证据 |
| --- | --- | --- |
| local 模式既有用例一行不改通过 | ✅ | `tests/test_sandbox_backend.py` 17 例（行为逐字节对拍）+ 全量回归零失败 |
| 逃逸用例（出网/读宿主/资源耗尽）在 docker 模式被拦 | ⏳ 待真机 | DockerBackend 参数逐项有测试（禁网/非root/cap全丢/只读根/资源上限/只挂 run 目录）；本机无 docker，真跑挂账 |

### C3 执行-测试-修正闭环（R51）｜✅ 通过（本机）

| 验收标准 | 结果 | 证据 |
| --- | --- | --- |
| ledger 可见完整闭环 | ✅（mock 层） | `tests/test_run_tests.py` 9 例 + `tests/test_exec_verify.py` 10 例：失败并入 G1 issue → REWORK → 重新执行，`g1_exec_check` 事件全程留痕；无测试记 `skipped`；mock 通道显式跳过记 `g1_exec_check_skipped` |
| 真实模型「生成函数+测试→首跑失败→自修正→通过」 | ⏳ 待真机 | 需模型密钥；`capability-live.jsonl` 的 8 条代码类 case 即为此设计 |

### C4 多模态输入（R53）｜✅ 通过（本机）

| 验收标准 | 结果 | 证据 |
| --- | --- | --- |
| 无 vision 能力的 provider 明确降级提示 | ✅ | 丢图 + 每 Agent 一次 `vision_unsupported` ledger 事件；带图消息与纯文本路径逐字节对拍钉住（`tests/test_multimodal.py` 14 例） |
| 真实模型「截图→复刻/修改前端」端到端 | ✅ 通过（2026-11-13） | 智谱 GLM-4.5V，run `20261113-194447-e27c`：4/4 任务成功、14 次真实调用、`vision_unsupported=0`；蓝按钮改绿并新增忘记密码链接，产物二次截图比对通过（`docs/VISION-2026-11-13.md`） |

### C5 文档 RAG（R54）｜✅ 通过（本机）

| 验收标准 | 结果 | 证据 |
| --- | --- | --- |
| 离线 HashEmbedder 退化可跑 | ✅ | `tests/test_docs_rag.py` 13 例（摄取增量/删除、混合检索、同步退化、ContextBus 注入、向后兼容） |
| 真实文档集命中率对照纯 BM25 有可测提升（进 eval 矩阵） | ⏳ 待真机 | 需真实 embedding 端点 + 真实文档集；异步 `query` 已支持端点，命中对照可加进 eval 矩阵 |

### C6 浏览器操作（R52，第一步）｜✅ 通过（零开发路径）

| 验收标准 | 结果 | 证据 |
| --- | --- | --- |
| 与 `untrusted_inputs` 只读预设联动 | ✅ | 互斥边界写进模板并有测试钉住（`tests/test_browser_mcp.py` 4 例） |
| 真实网页「提取信息/填表」端到端各一次 | ⏳ 待真机 | 装 Playwright MCP 后补；模板 `examples/agentcrew.browser.example.yaml` 已就绪 |

### C7 暴露为 MCP server（R52）｜✅ 通过（本机）

| 验收标准 | 结果 | 证据 |
| --- | --- | --- |
| 越权调用被 RBAC 拒绝并留痕 | ✅ | 工具调用全走 HTTP API——RBAC/限流/审计由 API 侧执行；桥接端到端对真实 in-process ApiServer（`tests/test_mcp_server.py` 13 例） |
| 官方 MCP 客户端互通实跑 | ⏳ 待真机 | 协议层 initialize/tools/list/tools/call/ping 全测；stdio 帧格式与自家 `StdioTransport` 一致（客户端可连自己），Claude Desktop 互通待环境 |

### C8 事件触发 / 定时运行（R52）｜✅ 通过（本机）

| 验收标准 | 结果 | 证据 |
| --- | --- | --- |
| 伪造签名被拒 | ✅ | `tests/test_webhook_trigger.py` 8 例：坏签名 401 + `webhook_signature_invalid` 留痕、时间戳过期 401、无凭证 401（认证不变量）、未启用 503；定时触发此前已交付（`main.py schedule`） |
| 定时与事件触发各端到端一次 | ✅（事件）/ ⏳（定时） | 事件触发见上；定时触发由 R44 之前的 schedule 交付，本批未改动 |

### C11 评测证据（R51）｜✅ 通过（本机）

| 验收标准 | 结果 | 证据 |
| --- | --- | --- |
| mock 离线 | ✅ | `capabilities.jsonl` 4/4 通过 + `smoke.jsonl` 全量回归 |
| 真实模型 | ✅ **通过（2026-11-13 全量实测）** | DeepSeek 真实通道 23 条全部跑过、**22 通过（pass@1 = 95.7%）**；驳回类在 markdown / py 两个变体上均有「0 字节产物被 G1 拦」的真实证据。唯一失败 `doc-changelog` 属「文档 + 字面符号契约」的产品边界（非回归），`reject-empty-json` 因规划期路径冲突未取到门禁证据——两条均已记录并挂决策（`bench/eval-live-2026-11-13.md`、`PROJECT-STATE.md` §3.70） |

### C12 发布证据（R51–R54）｜✅ 通过

- 全量 **3241 passed / 0 failed / 2 skipped**（崩溃隔离跑器一次跑出，167 文件 / 42 批次，2026-11-13 R57 后复跑）
- ruff 全绿；mypy **139 文件零错误**（与 CI 同口径）
- CI 连续四轮全绿：43215021310（R51）/ 43224838619（R52）/ 43234922972（R53）/ 43240197970（R54）
- 覆盖率门槛（`--cov-fail-under 50`）随 CI Linux job 通过
- Studio：`npm run check` 73 模块 / 42 分片、`test:unit` 18/18、`e2e` 32/32（2026-11-13 本机实跑）

---

## 2. 文档与审计同步（C12 的一部分）

| 文档 | 更新 |
| --- | --- |
| `docs/CAPABILITIES-C1-C3.md` / `docs/CAPABILITIES-P1-BATCH2.md` | 落地说明：怎么开、边界、已验证/待真机 |
| `docs/OPERATIONS.md` | §3.2 生产清单、§3.3 边界表（sandbox/search/run_tests/执行校验/mcp-serve/webhooks/trigger）、§8 上线清单 |
| `docs/THREAT_MODEL.md` v1.3 | 新增 T-48（搜索投毒/SSRF）、T-49（执行校验持久化）、T-50（入站触发伪造）；G-9 改写为「local 缺口 + docker 补偿」；§2.3 补三类输入源 |
| `artifacts/contracts/openapi.yaml` | 新路由 `/webhooks/trigger` 与 `CreateRunRequest.images` 已登记（契约测试双向比对） |
| `PROJECT-STATE.md` | §3.64–§3.67 四章 + 指标表 + 路线图状态 |

---

## 3. 明确不做（本批边界，均为刻意取舍）

1. **MCP server 不做** resources / prompts / sampling 与 HTTP 传输（协议面收窄，等真实远程调用方）；
2. **不代抓 URL 图片**（`images` 只收内联 base64，服务端不替调用方出网——SSRF 面）；
3. **签名重放不引入 nonce**（窗口内可重放一次的代价远小于 nonce 存储，与 R12 同口径）；
4. **文档 RAG 不引入向量数据库**（复用内存 BM25 + 磁盘向量缓存，与经验库同款）；
5. **`sandbox_backend` 不开进 API options**（隔离等级不允许被单个 run 降级）。

## 4. 待真机验证清单（挂账于 PROJECT-STATE §7）

1. Docker 后端真跑 `run_tests` + 三条逃逸用例（出网/读宿主/资源耗尽）；
2. 真实模型 23 条评测（`python scripts/nightly_eval.py --max-cost 1.50`），pass^k / 成本 / 失败归因回填；
3. vision 模型「截图→改前端」端到端；
4. 真实文档集 RAG 命中率对照（进 eval 矩阵）；
5. 真实网页「提取/填表」各一次（Playwright MCP）；
6. Claude Desktop ↔ `mcp-serve` 互通 smoke。

## 5. P2 按需项状态（不在本次范围）

| 项 | 状态 | 备注 |
| --- | --- | --- |
| C9 A2A 互操作 | ⬜ 按需 | 需真实 A2A 客户端 |
| C10 对话式入口 + run 中途引导 | ⬜ 产品决策 | Studio 对话页已有只读展示（R43） |
| C13 SDK 生态（多语言/异步） | ⬜ 按需 | Python 同步 SDK 已有（R16） |
| C14 容量与横向扩展 | ⬜ 按需 | 单进程 asyncio 前提下未压测 |
