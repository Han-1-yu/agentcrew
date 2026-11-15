# AgentCrew 剩余事项汇总（2026-11-13）

> **背景**：`完善/AgentCrew落地完善路线-2026-11-12.md` 的第一批（P0）与第二批（P1）
> 已由 R51–R55 全部落地并通过整批验收（`docs/ACCEPTANCE-2026-11-12.md`，总评通过）。
> 本文档汇总**当前所有未完成事项**，按「缺什么资源才能做」分类，每项给出恢复方式、
> 前置条件与预估规模。**没有一项是「代码能力缺口」**——所有 P0/P1 能力的代码均已落地。
>
> **更新（2026-11-15）**：D2「运行中实时引导」已闭环——C10 切片二补齐定向 / 过期 /
> 撤回 / 历史压缩，Studio 也补上指引输入与 R62 rewind 的「从此恢复」入口
> （`PROJECT-STATE.md` §3.87）。D 类现只剩 C9 / C13 / C14 三项待产品决策；C 类、
> E、F 不变。下文保留 2026-11-13 的原始判断作为历史记录，排期请以 §7.1「实际剩余清单」为准。

## 总览

| 类别 | 项数 | 卡点 |
| --- | --- | --- |
| ~~A. 待 DeepSeek 充值~~ | ✅ **已闭环**（2026-11-13） | 余额已恢复，15 条补跑完成，全量 23 条 22 通过（见 `bench/eval-live-2026-11-13.md`） |
| B. 待外部环境 | ✅ **5 项全部闭环** | B1 docker（CI 真容器）、B2 vision（智谱 GLM-4.5V 真实截图改前端）、B3 MCP 互通（官方 Node SDK）、B4 浏览器 MCP（系统 Edge）、B5 RAG 对照（自建 486 条查询）；证据见 `PROJECT-STATE.md` §3.73/§3.76 与 `docs/VISION-2026-11-13.md` |
| C. 代码侧小项（本机可做） | 3 项 | C2 已闭环，余 3 项均为低优先旧账 |
| D. 待产品决策（P2 按需） | 3 项（C9/C13/C14） | 需产品/业务输入；~~D2 运行中引导~~ 2026-11-15 闭环，~~D5 评测口径~~两项均已闭环（R58 hint + `rejected_by_gate`，§3.74） |
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

## B. 真机/外部环境项（5 项）

| # | 事项 | 状态 | 恢复方式 / 结果 | 规模 |
| --- | --- | --- | --- | --- |
| B1 | Docker 后端逃逸用例 | ✅ 已闭环（改在 CI 上做） | 本机与云主机都起不来容器（`docker pull` → `failed to register layer: unshare: operation not permitted`，bounding set 无 `cap_sys_admin`；rootless podman `newuidmap` 失败）。改为把 GitHub runner 当那台机器：新增 `tests/test_docker_isolation_live.py` 4 条真容器用例（能跑 python / 禁网 / 宿主文件不可见 / 非 root + 内存与进程上限），由 `ci.yml` 的 `sandbox-docker` job 在 `AGENTCREW_DOCKER_LIVE=1` 下执行；默认无开关一律 skip（不许「因为缺 docker 而绿」）。首轮真跑抓到 1 个真缺陷：容器以 `65534` 跑时写不进挂载的 workspace（`Permission denied: 'out.txt'`）→ 已修 `DockerBackend` 默认取宿主 uid（Windows 退回 nobody），并加「uid=0 构造期即拒」。修后 run `43399398936` 的 sandbox job 4/4 绿 |
| B2 | vision「截图→改前端」端到端 | ✅ 已闭环（智谱 GLM-4.5V） | 用户提供 key 后实测：run `20261113-194447-e27c`，4/4 任务成功、14 次真实调用、`images_attached=1`、`vision_unsupported=0`；产物保留标题/账号/密码框，按钮 `#22c55e` 变绿并新增忘记密码链接，二次截图人工比对通过。配置坑已固化到 `examples/agentcrew.vision.example.yaml`：环境变量默认模型会覆盖 YAML；`glm-4v-flash` 的 max_tokens 上限 1024。完整证据 `docs/VISION-2026-11-13.md` | 已完成 |
| B3 | Claude Desktop ↔ `mcp-serve` 互通 | ✅ 已闭环（用官方 SDK 代替桌面客户端） | 不装 Claude Desktop，用官方 `@modelcontextprotocol/sdk`（Node）当客户端：`initialize`（server=agentcrew 1.0.0）→ `tools/list` 5 工具 → `create_run`（`20261113-165919-5a0c`）→ `wait_for_run` = `completed`（6.2s）。取证脚本 `output/_eval/mcp-interop/probe.mjs` | ~0.5 小时 |
| B4 | Playwright MCP 真网页「提取/填表」 | ✅ 已闭环（浏览器用系统 Edge） | `npx -y @playwright/mcp@latest`（0.0.85 / Playwright 1.66.0-alpha）连得上、25 个浏览器工具；本机无 Chrome 且 Chromium 下载被网络挡住 → `--browser msedge`（系统 Edge 157）；`file:` 协议默认被拦 → 本地 `http.server` 起 `127.0.0.1:18095/probe.html`，导航成功、快照命中页面标题。探针 `output/_eval/mcp-interop/playwright-probe.mjs`（未命中即 exit 1，防假绿） | ~1 人日 |
| B5 | RAG 命中率对照（进 eval 矩阵） | ✅ 已闭环（离线自建语料）；**2026-11-15 更新**：真语义端点已接（C6，`HttpEmbedder` + `embedding_base_url` + 缓存键含维度），`scripts/rag_hitrate.py` 支持 `--embedding-model` 在固定 commit 上复跑真语义对照；当前语料口径是 37 篇 / 549 条，与当时 32 篇 / 486 条不可直接比 | `scripts/rag_hitrate.py`：仓库 `docs/*.md`（32 篇 / 1233 块）+ 二/三级标题当查询（486 条），权重 0 / 0.5 / 1.0 对照 → hit@1 **84.4% / 82.9% / 53.3%**；结论是「哈希兜底不是语义向量」，已改成兜底时实际权重压 0 并 emit `docs_vector_degraded`（`bench/rag-hitrate-2026-11-13.md`、`PROJECT-STATE.md` §3.73） | ~1 人日 |

---

## C. 代码侧小项（本机可做，均为旧账）

| # | 事项 | 为什么搁置 | 现在做值不值 | 规模 |
| --- | --- | --- | --- | --- |
| C1 | ~~D-16「写盘注入 OSError」一致性用例~~ | — | ✅ **复核发现早已闭环**：`tests/test_finalize_atomicity.py` 5 例覆盖硬失败 / 瞬时钟 / 重试用尽 / 恢复 / tasks-record 分叉一致性；此前只是台账未回填（2026-11-15 更正） | — |
| C2 | ~~`stream_call` 设防~~ | ✅ **已闭环** | R56 部分用量记账 + R57 配额设防（见 `PROJECT-STATE.md` §3.68/§3.69；全量 3241 passed / 0 failed） | — |
| C3 | ~~Webhook 补投去重~~ | — | ✅ **已闭环（2026-11-15，§3.88）**：`DeliveryDedup` 落盘 `(delivery, target_key)` 追加表，堵住多进程同时补投同一 spool 文件的双投；仍是 at-least-once，接收端幂等头为最终防线 | — |
| C4 | ~~Webhook 事件级表达式过滤~~ | — | ✅ **已闭环（2026-11-15，§3.88）**：`match_filter` 逗号分隔等值 AND，无 eval / 正则；`webhook_filter` 与 `webhooks[].filter` 两条配置通道 | — |

> **更新（2026-11-15）**：C 类四项现已全部闭环；E 类 run 目录静态加密与 F 类环境
> 问题按用户指令「无服务器资源」从本机排期移出 / 保留为长期观察，见
> `PROJECT-STATE.md` §7.1。

---

## D. 待产品决策（P2 按需，路线文档已标）

| # | 事项 | 决策需要回答的问题 |
| --- | --- | --- |
| D1 | C9 A2A 互操作 | ✅ **已决策并由第一批落地（2026-11-15）**：复用 MCP 双向打通（不另起 LF A2A 协议）。首批完成入站配置契约（字符串 `command` + `args`、`env` 合并）、`wait_for_run` 单次 25s 上限、本仓 client↔server 回环测试，见 `PROJECT-STATE.md` §3.91。**两步已全部收尾（2026-11-15）**：产物读取 `GET /runs/{id}/artifacts` + `agentcrew_list_artifacts` / `agentcrew_read_artifact`（工具 5 → 7，openapi / RBAC / 断言同批改）；`initialize` 在 `{2024-11-05, 2025-03-26, 2025-06-18}` 内回显客户端版本。C9 闭环，见 §3.91 |
| D2 | ~~C10 对话式入口 + run 中途引导~~ | ✅ **已闭环（2026-11-15）**：切片一的 CLI / REST 中途指引 + 切片二的定向 / 过期 / 撤回 / 历史压缩 + Studio 指引输入入口均已落盘；R62 `rewind` 也补上 Studio「从此恢复」弹窗。**仍待产品决策**的只剩「对话页是否升级为对话即入口」与指引流式回显（见 §7.1） |
| D3 | C13 SDK 生态 | 需要哪种语言的 SDK？（TypeScript / Go / 异步 Python） |
| D4 | C14 容量与横向扩展 | 预期并发量级？多实例共享 `output_dir` 的方案（NFS vs 对象存储）选型 |
| D5 | 评测口径两处（2026-11-13 实测暴露） | ①~~文档类 `must_contain` 逐字匹配~~：✅ 已按「最优方案」处置——hint 改「逐字包含」、门槛不放宽（R58，`PROJECT-STATE.md` §3.72）；②~~驳回类用例如何区分「门禁拒绝」与「规划期失败」~~：✅ 已加 check kind `rejected_by_gate`（`stage: plan|gate|any` + `reason_contains`），只认 `plan_rejected` / `gate_result(verdict=reject)` 证据，`run` 级 error 或非 failed 状态一律判失败；两条驳回 case 已改用该断言并用离线 regrade 验证 2/2 PASS（§3.74） |

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
