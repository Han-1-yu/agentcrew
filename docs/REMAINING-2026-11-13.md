# AgentCrew 剩余事项汇总（2026-11-13）

> **背景**：`完善/AgentCrew落地完善路线-2026-11-12.md` 的第一批（P0）与第二批（P1）
> 已由 R51–R55 全部落地并通过整批验收（`docs/ACCEPTANCE-2026-11-12.md`，总评通过）。
> 本文档汇总**当前所有未完成事项**，按「缺什么资源才能做」分类，每项给出恢复方式、
> 前置条件与预估规模。**没有一项是「代码能力缺口」**——所有 P0/P1 能力的代码均已落地。

## 总览

| 类别 | 项数 | 卡点 |
| --- | --- | --- |
| ~~A. 待 DeepSeek 充值~~ | ✅ **已闭环**（2026-11-13） | 余额已恢复，15 条补跑完成，全量 23 条 22 通过（见 `bench/eval-live-2026-11-13.md`） |
| B. 待外部环境 | 5 项 | docker / 多模态模型 / Claude Desktop / Playwright MCP / 真实文档集+embedding 端点 |
| C. 代码侧小项（本机可做） | 3 项 | C2 已闭环，余 3 项均为低优先旧账 |
| D. 待产品决策（P2 按需） | 4 项（C9/C10/C13/C14）+ 1 项评测口径 | 需产品/业务输入 |
| E. 依赖部署决策 | 1 项 | run 目录静态加密 |
| F. 环境问题（非代码） | 1 项 | 本机 Python 子进程偶发损坏 |

---

## A. ~~待 DeepSeek 充值~~｜✅ 已闭环（2026-11-13）

**原卡点**：账户 402 Insufficient Balance（2026-11-13 凌晨跑批中耗尽）。
**结果**：余额恢复后（`doctor` 6/6、`deepseek-chat` 1340ms healthy）补跑 15 条——
**14 通过 / 1 失败**，全量 23 条 **22 通过（95.7%）**，补跑成本 **$0.5983**。

| 子项 | 结果 |
| --- | --- |
| 剩余 15 条评测 | ✅ 已跑（A 4/5 · B 5/5 · C 4/5）；分块文件 `examples/evals/capability-live-rest*.jsonl` |
| `code-csv-summary` 重跑确认 | ✅ 通过（$0.0434）——确系当时 402 同因，非能力问题 |
| 结论回填 | ✅ `bench/eval-live-2026-11-13.md` + `docs/ACCEPTANCE-2026-11-12.md` C11 行 + `PROJECT-STATE.md` §3.70 / §0 指标表 |
| 遗留两条发现 | ①`doc-changelog`：文档类 `must_contain` 逐字匹配（语义等价不算命中）；②`reject-empty-json`：规划期路径冲突导致没走到 G1。**均需产品口径决策，见 D5** |

---

## B. 待外部环境（5 项）

| # | 事项 | 需要什么 | 恢复方式 | 规模 |
| --- | --- | --- | --- | --- |
| B1 | Docker 后端逃逸用例 | 本机或 CI 装 Docker | `AGENTCREW_SANDBOX_BACKEND=docker` 下跑 `tests/test_sandbox_backend.py` 的 3 条逃逸用例（出网/读宿主/资源耗尽）+ `run_tests` 真容器执行 | ~0.5 人日 |
| B2 | vision「截图→改前端」端到端 | 多模态模型（deepseek-chat 不支持图像；需 openai:gpt-4o / claude 等） | `run --image 截图.png`（provider 开 `supports_vision`）；`tests/test_multimodal.py` 已覆盖通道 | ~0.5 人日 |
| B3 | Claude Desktop ↔ `mcp-serve` 互通 | 装 Claude Desktop / Cursor | 配置块见 `docs/CAPABILITIES-P1-BATCH2.md` §1.3；协议层 13 例已测 | ~0.5 小时 smoke |
| B4 | Playwright MCP 真网页「提取/填表」 | `npx @playwright/mcp@latest` 可用 | `examples/agentcrew.browser.example.yaml` 模板即配即用；注意与 `untrusted_inputs` 互斥 | ~1 人日 |
| B5 | RAG 命中率对照（进 eval 矩阵） | 真实文档集 + embedding 端点 | 对照 `retrieval_vector_weight` 0 vs 0.5 的命中率；`docs_rag_*` 与 eval 框架均已就绪 | ~1 人日 |

---

## C. 代码侧小项（本机可做，均为旧账）

| # | 事项 | 为什么搁置 | 现在做值不值 | 规模 |
| --- | --- | --- | --- | --- |
| C1 | D-16「写盘注入 OSError」一致性用例 | 注入点与 `_finalize` 内部原子写耦合过紧 | **重构封版路径时顺手补**，单独做不划算 | ~1 人日 |
| C2 | ~~`stream_call` 设防~~ | ✅ **已闭环** | R56 部分用量记账 + R57 配额设防（见 `PROJECT-STATE.md` §3.68/§3.69；全量 3241 passed / 0 failed） | — |
| C3 | Webhook「恰好一次」投递 | at-least-once + 幂等头（`X-AgentCrew-Delivery`）已兜底 | **需接收端配合去重**，单方面做只能加落盘去重表（`delivery` 主键）——收益取决于接收端实现质量 | ~1 人日 |
| C4 | Webhook 事件级表达式过滤 | R14 的 `webhook_events` 列表 + 「只推失败」已覆盖主要场景 | **按需**：出现「只推某个任务的事件」类需求再做 | ~1 人日 |

---

## D. 待产品决策（P2 按需，路线文档已标）

| # | 事项 | 决策需要回答的问题 |
| --- | --- | --- |
| D1 | C9 A2A 互操作 | 目标是接哪个 A2A 生态？（LF v1.0 协议已有巨头背书） |
| D2 | C10 对话式入口 + run 中途引导 | Studio 对话页要从「只读展示」（R43）升级为「对话即入口」吗？ |
| D3 | C13 SDK 生态 | 需要哪种语言的 SDK？（TypeScript / Go / 异步 Python） |
| D4 | C14 容量与横向扩展 | 预期并发量级？多实例共享 `output_dir` 的方案（NFS vs 对象存储）选型 |
| D5 | 评测口径两处（2026-11-13 实测暴露） | ①~~文档类 `must_contain` 逐字匹配~~：✅ 已按「最优方案」处置——hint 改「逐字包含」、门槛不放宽（R58，`PROJECT-STATE.md` §3.72）；②驳回类用例如何区分「门禁拒绝」与「规划期失败」——仍需新 check kind，待定 |

---

## E. 依赖部署决策（1 项）

**run 目录静态加密**：ledger / artifacts 明文落盘是当前设计（密钥字段已脱敏）。
全盘加密属部署侧职责（LUKS / 云盘加密 / EFS 加密）。**决策点**：是否需要应用层加密，
若是，加密范围（整个 run 目录 vs 仅 artifacts）与密钥管理（KMS？）需要先定。

---

## F. 环境问题记录（非代码，1 项）

**本机 Python 子进程偶发内存损坏**：`SystemError: Negative size passed to
PyBytes_FromStringAndSize`（偶伴 STATUS_HEAP_CORRUPTION / pydantic 收集期 TypeError）。
特征：失败集合随机漂移、单跑稳定、批内偶发。`scripts/test.py` 的崩溃隔离 + 重试已覆盖
崩溃类；「子进程正常退出但输出损坏」类靠重跑全量消除。**不建议追修**——是
WorkBuddy 自带 Python 3.13 运行时的问题，等环境更新自愈。

---

## 建议的复工顺序

1. **A**（充值后跑评测）——最高杠杆：一次执行完成 C11 全部证据，其余全是顺带回填；
2. **B1 / B2**（docker + vision）——各半天，把验收报告剩余两个「功能级」待真机项清掉；
3. ~~**C2**（stream_call 设防）~~——✅ 已闭环（R56 + R57），代码侧不再有建议主动排期项；
4. **B3–B5 / C3–C4**——按需求出现时再做；
5. **D / E**——等产品与部署决策。
