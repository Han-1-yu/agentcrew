# AgentCrew 项目状态

> **最后更新：2026-11-14** · 本文是仓库**唯一状态源**。
> 历次交接文档、验收/审查/测试报告、一次性工作提示词已在本日汇总并入本文后删除，
> 原文备份见 `.workbuddy/archive/reports-2026-11-02/`（git 亦可通过历史恢复）。

---

## 0. 一句话现状

**当前代码基线：R59–R61 已提交并推送（`c9cc47c`）+ §3.83 真机缺陷修复（`c9c4a60`），两个提交的 CI 均七 job 全绿；R62（时间旅行 `rewind`）在工作区、未提交。** 最近一次全量 **3373 passed / 0 failed / 6 skipped / 0 error**（175 文件 / 88 批次 / 439.8s），跑器报告 2 个批次进程崩溃后重试恢复、2 项「首次失败、重试通过」（`test_exec_verify` 隔离复跑 10/10 + 单例 1/1 全过，判为环境负载与同仓并发测试争用，非回归）。真实模型评测与 Studio 分项数字保留为历史实测证据，本次未逐项重跑；当前 CI 的 Studio job 为 success。R4 把编排内核从「静态 DAG + 同步 TTY 卡点」升级为「条件分支 + 可跨进程恢复的人工审批」；R5 把 `<message to>` 接成真实模型调用；R6 补委派治理、咨询上下文与咨询沉淀；R7 补上「谁派的活谁验收」的监督回路；R8 把委派被拒的原因回灌给模型，让它能改；R9 让经理也验收规划期的活；R10 让主管驳回后能换个人做；R11 把经验检索从单字计数换成中文二元组 + BM25；R12 补上出站 Webhook；R13 把 MCP 服务器接进生产工具链；R14 让 Webhook 支持多接收端与「只推失败」；R15 让未投出的通知落盘，在下一次启动自动补投；运维部署/排障手册已补入 `docs/OPERATIONS.md`；R16 补上官方 Python SDK 与示例；R17 给 run 加心跳、启动时僵尸收敛与跨进程 resume 锁；R18 让 CI 检查链首次全绿并补齐限流窗口配置通道；R19 给主管审核开了独立、不阻塞的 `suggestions` 建议通道；R20 补上单步/断点调试——每个任务派发前可挂起裁决，第二档功能项全部闭环；R21 给 API 契约装上守卫，openapi.yaml 与代码路由双向锁死、不再漏端点；R22 补上 resume 状态白名单与 MCP 管道显式关闭；R23 产出 2501 用例的模块/场景覆盖矩阵（`docs/TEST-REPORT.md`），验收报告遗留的最后一项小项闭环；R24 补上工具管理页与单工具试跑；R25 把共享 API Key 升级为多用户 RBAC（密钥/角色/作用域/归属/审计）；R26 补上拖拽式可视化编排（画布 + YAML 双向 + 图校验）；R27 给检索加向量通道（混合打分 + 磁盘缓存 + 本地退化）；R28 打通「画布 → run」直通规划；R29 让实体记忆也走向量混合（不加列、不迁移）；R30 给审批单加等待窗口与过期告警；R31 给条件边补否定/兜底写法；R32 把控制台 UI 重构成令牌化 + 明暗主题 + 侧栏布局；R33 把单层主管复核扩成可配的多级链；R34 修好本机测试环境（崩溃隔离跑器，一条命令出可信结论）；R50 按差距报告 69 条完成上线前加固——8 项 P0 全关、时钟容错让"前跳误杀/回拨死锁"不再可能、审批与预算的并发边界收口、`/metrics` 补齐可观测入口、Studio 断线可自愈（明细见 `docs/REMEDIATION-STATUS.md`）。R51 按能力路线第一批补上三根支柱的关键缺口：**能查**（`web_search`，四提供方、默认关、密钥只走环境变量）、**敢跑**（可插拔执行后端，默认 `local` 行为逐字不变，`docker` 可选：禁网/非 root/资源上限/只挂 run 目录，缺 docker 即报错）、**跑得出结论**（`run_tests` + G1 执行校验，失败并入既有返工通道），并把评测做成 mock/真实双轨（`eval --live` + 23 条真实小目标 + 夜间脚本）。R52 补上互操作与无人值守：**C7** 暴露为 MCP server（`mcp-serve`，5 个工具全走 HTTP API 复用 RBAC/审计）、**C6 第一步**借现成浏览器 MCP（配置模板 + 读优先白名单，零开发）、**C8** 入站 Webhook 触发（`POST /webhooks/trigger`，HMAC 或 API Key 两条认证路径，来源进 ledger）。R53 补上多模态输入（**C4**）：`run --image` / `POST /runs` 的 `images`（内联 base64，最多 4 张），provider 能力位 `supports_vision`（默认关）决定是否真发图，不支持时降级纯文本并落 `vision_unsupported`（降级可见），图片落盘留证。R54 补上文档 RAG（**C5**）：`knowledge/docs/` 下的团队手册/规范增量摄取分块，按相关性注入 PM 规划提示词与每个任务的上下文（复用 R11/R27 检索资产），`docs list/search` 管理入口。能力路线第二批至此全部落地。

| 指标 | 当前值 |
| --- | --- |
| 全量测试 | R62 工作区最新全量 **3373 passed / 0 failed / 6 skipped / 0 error**（175 文件 / 88 批次 / 439.8s，`scripts/test.py --batch 2 --retries 4 --retry-failed 3 --keep-pycache`）。跑器报告 2 个批次在进程崩溃后重试恢复；2 项「首次失败、重试通过」（`test_exec_verify`）已隔离复跑 10/10 + 1/1 全过，判为环境负载；6 个 skip 沿用跑器结果，其中 4 个 docker 真容器用例由 CI 覆盖 |
| 静态检查 | R59–R61 工作区重跑 `ruff check agentcrew/ main.py tests/`：**All checks passed**；`mypy agentcrew/`：**142 文件零错误** |
| 预设样例 | 3 套（`todo` 混合并发 / `linear` 严格串行 / `hierarchy` 两层并行），均 0 失败 0 阻塞 |
| 性能 | 峰值并发 1→2→4→4（= 图宽度），达效率 100%（理论上限 1.65×） |
| 稳定性 | 同进程 20 连跑 20/20 成功，RSS −2.3%、句柄 +1、线程不增长 |
| Studio | `npm run check` **73** 模块 / 42 分片通过、`test:unit` **18/18**、electron `e2e` **32/32**（2026-11-13 本机实跑） |
| 上线自检 | 本机可验 **9 项全通过**（doctor / 离线冒烟 / REST 认证 200·401 / `/metrics` / 无密钥退 3 / `clean` 干跑 / Studio 三连 / ruff+mypy）；目标机 10 项判据见 `docs/DEPLOY-CHECK-2026-11-13.md` |
| 收尾 | 交付说明见 **`docs/CLOSURE-2026-11-13.md`**；B 类外部环境项 **5/5 全部闭环**；云主机真实部署验收 10 项中 **7 项闭环**（TLS/SSE、告警规则、日志、密钥、成本、webhook、备份），仅剩独立数据盘与两项 Docker 依赖（§3.77、`docs/DEPLOYMENT-VM-2026-11-13.md`） |
| Git | `c9c4a60`（R61 真机修复）与 `c9cc47c`（R59–R61）均已推送到 `origin/master`，CI run **43453359080** / **43449592343** 的 **7 个 job 全部 success**（Python 3.10/3.11/3.12/3.13、Windows、Studio、Docker 真隔离）；验收报告 `docs/ACCEPTANCE-2026-11-12.md`；真实模型评测历史证据为 23 条中 22 通过（`bench/eval-live-2026-11-13.md`，本次未重跑真实模型评测） |
| 角色 | 8 个内置岗位角色 + 执行体（Agent Profile）可多绑 |

```bash
cd E:/agentcrew
python scripts/test.py                               # 全量回归（崩溃隔离跑器，一条命令；人工分批只作兜底，见 §6）
python main.py run --sample linear --no-live --auto  # 离线演示（无需 Key；非交互环境须加 --auto）
AGENTCREW_TOOLS_ENABLED=true python main.py run --sample linear --no-live --auto  # R3：带工具链跑
python main.py run --sample linear --no-live --gate plan --approval-mode deferred  # R4：跑到审批挂起
python main.py approvals <run_id>                    # R4：看待审批单
python main.py approve <run_id> <approval_id> --decision approve
python main.py resume <run_id> --sample linear --force --gate plan --approval-mode deferred
AGENTCREW_PEER_DIALOGUE_ENABLED=true AGENTCREW_MAX_PEER_MESSAGES=4 python main.py run --sample linear --no-live --auto  # R5：跨角色咨询接真调用
python main.py run --demo                            # 内置 Todo 目标（真实模型）
python main.py serve --port 8765                     # HTTP API + Web UI
python main.py doctor                                # 环境诊断
python main.py diff <run_a> <run_b>                  # run 差异对比
```

---

## 1. 系统形态

**两阶段协作**：

1. **规划**：`project_manager` 用一次受治理的 LLM 调用把目标拆成任务 DAG → 引擎补 `T-FINAL` → 结构校验（id 唯一、依赖无环且存在、角色已注册、产物路径不冲突、每任务有验收标准）→ 失败回灌 PM 重拆（`plan_max_attempts=2`）。
2. **执行**：确定性调度器按 DAG 派发、限流并发、组装上下文、跑状态机，对每个产物依次过 G1 静态校验 → G2 LLM 评审 → G3 人工卡点。**调度决策全程无 LLM**。

**分层依赖**（`tests/test_architecture.py` 强制）：

```
contracts.py（零依赖契约层：EventSink / TaskLike）
  ↑ models / task / runtime / artifacts / gate / tools / context
    ↑ crew（确定性引擎）
      ↑ agents（唯一持有 LLM 的层）
        ↑ api / cli / main.py
```

**不可违反的四条红线**：密钥绝不落盘 · 确定性引擎不变 · 配置合并顺序不变（代码默认 → `.env` → `agentcrew.yaml` → 环境变量 → CLI）· 测试只增不减。

---

## 2. 当前能力

| 层 | 能力 |
| --- | --- |
| 编排 | DAG 调度 + Kahn 环检测；`(priority, id)` 就绪排序；两级并发（任务槽位与门禁不嵌套，构造性防死锁）；动态子任务（`<subtask>` 围栏）；树状拆解（`parent_id`） |
| 编排（R4） | 静态 AND 依赖之上叠加**按结果标签的条件边**：Agent 用 `<outcome>` 声明结果、下游用 `when_outcomes` 选分支；未命中分支以「预期跳过」传播、互斥分支在汇合节点收敛。**持久化人工审批**：审批单 `pending → decided → consumed` 三态追加写，run 以 `awaiting_approval` 非终态挂起，CLI / API / Studio 三端跨进程裁决，`resume` 回原卡点继续 |
| 执行 | 8 角色 Agent；自我修正（`max_self_corrections=2`）；任务级超时 300s；停滞检测 300s（退出码 4）；任务级执行体覆盖。R51：代码执行有**可插拔后端**（`local` 默认 / `docker` 可选，后者禁网、非 root、cap 全丢、资源上限、只挂 run 目录） |
| 协作（R5） | `<message to="…">` 接成**目标角色的一次真实模型调用**（`BaseAgent.consult`，不带产物协议、不落盘、不建任务）；`peer_dialogue_enabled` 默认关闭；`max_peer_messages` 配额 + 同问题去重 + 问自己/未知角色零成本拒绝；mock 通道自动降级、Provider 异常不打断提问方 |
| 委派（R6） | 动态子任务四道闸：角色必须存在（拼错不再静默落通用兜底）/ `manager_only` 时只有经理能派 / 层数 ≤ `max_delegation_depth` / 总数 ≤ `max_dynamic_tasks`；拒绝留 `subtask_rejected_by_policy` 带原因、run 不停；`Task.delegated_by` 留痕派发者。咨询带上提问方任务背景（contextvar 透传，不改回调签名）；`peer_consult_capture_enabled` 可把真实问答沉淀进经验库 |
| 委派（R8） | 被拒原因**回灌模型**：`SubtaskDispatcher` 暴露 `last_rejection`，Agent 据此回灌并重问一次（`max_subtask_corrections`，默认 1）；裸闭包不回灌（调用次数不变）；只纠正一轮不套娃 |
| 门禁 | G1 静态校验（语法/危险导入/非空/契约）；G2 并行多评审 + Jaccard 去重（收敛默认关）；G3 人工卡点；`bump_rework` 唯一调用点在 Gatekeeper。R51：G1 增加**执行校验档**（`gate_config.exec_verify`，默认关）——在沙箱里跑任务自己交付的 pytest，失败并入 G1 issue 走既有返工通道 |
| 门禁（R7/R9/R10） | 主管审核：动态委派任务在 G1/G2 放行后，由**派活的人**再评一次（`layer="supervisor"`），复用同一套返工通道；不受 score / 收敛旁路保护。R9 起 `supervisor_scope=all` 时规划期任务回落到 `supervisor_role`（默认 PM）验收，主管已在 G2 评审人里则跳过。R10 起主管驳回可**换人**：按 `role_fallback` 挑替代角色、改 `task.role` 后仍走 REWORK，上限 `max_supervisor_reassignments` 且换人消耗返工额度。默认关闭、mock 通道明确跳过 |
| 模型 | 7 类 provider（openai 兼容 / anthropic / mock / cli / 三传输 MCP）；29 个预设（R60 新增 `claude-code` / `codex`）；四级别名解析；故障转移 + 冷却矩阵（402=30min，429=Retry-After 或指数退避，5xx=指数退避）；per-provider 并发信号量 + 60s RPM 滑动窗口；复杂度自动选模 + 动态校准；响应缓存（temperature=0）；预算硬上限 + 80%/90% 软阈值 |
| 可观测 | `ledger.jsonl` 全事件审计（run/task/llm_call/gate/error…）；contextvars 链路追踪（`utils/trace.py`，`llm_call` 自动带 `task_id`/`attempt`）；`log_level` + `AGENTCREW_LOG_JSON=1` 结构化日志（含脱敏）；`diff` / `status` / 统计端点 |
| 产物 | 契约校验（路径/类型/最小字节/必需符号）+ 命名空间隔离 + `MANIFEST.json` + sha256；安全落盘（双侧路径规范化） |
| 工具（R3/R13/R51/R61） | 7 个内置工具（`read_file` / `list_files` / `grep` / `run_python` / `run_tests` / `write_file` / `web_fetch`）+ `web_search`（C1，配置提供方才出现）+ **R13 起 MCP 服务器的工具**（`server__tool` 前缀防撞名、schema 原样透传、单服务器失败只跳过）+ **R61 的 `generate_image`**（配置 `image_provider` 才注册）；`tools_enabled` 默认关闭的 run 级自动装配 + `tool_permissions` 执行期下发；执行前 JSON Schema 子集强校验（失败带正确形状回灌、绝不执行）；`web_fetch` / `web_search` 全链路 SSRF 防护 |
| 知识（R3/R11） | 经验库（R11 起为中文二元组 + BM25 检索：IDF 压虚词、长度归一、单字命中不算相关）+ 实体记忆（纯规则抽取五类实体、跨 run 追加归并、加权检索注入 PM 上下文）；`entity_memory_enabled` 默认关闭 |
| 对外通知（R12/R14/R15） | 出站 Webhook：后台线程投递（不阻塞 run）、HMAC-SHA256 签名（时间戳纳入签名防重放）、`delivery` id 重试不变、失败重试有上限且不改 run 结局；默认推五个 run 级节点；密钥不导出/不进快照/进脱敏词表；默认关闭。R14 支持多接收端各自 `secret`/`events`/`only_failures`。R15 起每条「事件 × 接收端」入队前落盘、成功即删，进程退出或收尾超时的遗留记录由下一次启用 Webhook 的 run 补投（at-least-once，24h / 5 次淘汰），spool 不存 URL 与密钥 |
| 接口 | CLI（`run/resume/status/roles/agents/switch/samples/clean/cancel/delete/serve/doctor/diff/providers/models/config`）；HTTP API + SSE + Web UI；API Key 认证 + IP 限流；并发 run 上限 `AGENTCREW_MAX_CONCURRENT_RUNS=8` |
| 对外 SDK（R16） | `sdk/agentcrew_sdk`：零依赖同步 HTTP/SSE 客户端，独立可安装（`pip install -e ./sdk`）；API Key 认证、按状态码分型的异常、SSE 惰性生成器、ledger 自动翻页、`wait_for_run`；两个可直接运行的示例 |
| 生命周期加固（R17） | `runtime/lifecycle.py`：30s 心跳（独立文件、不碰 Ledger）、CLI `run`/`serve` 启动时把孤儿 `running` 收敛为 `interrupted` 并记 `run_reconciled`、`resume()` 跨进程 `O_EXCL` 锁（owner token + 陈旧接管）；`awaiting_approval` 不参与收敛 |
| 桌面端 | Studio 九页（运行列表 / 看板 / 事件流 / 任务详情 / 产物 / 图表 / 供应商 / 设置…）；明暗主题；e2e + 视觉回归 |

---

## 3. 最近变更（2026-11）

### 3.1 生产级加固（六层审计定点改造）

按「架构 / 状态 / 容错 / 可观测性 / 并发性能 / 工程规范」六层审计的真实短板做定点改造，业务逻辑与协作结果不变。

| 改造 | 位置 | 解决的问题 |
| --- | --- | --- |
| RPM 滑动窗口超卖竞态 | `models/factory.py` `_wait_window` | 并发协程基于同一份满窗快照 sleep 后同时入列，实际并发超 `max_rpm`（触发上游 429）；改为 per-key `asyncio.Lock` 串行化「清窗→检查→入列」，清窗条件 `<`→`<=`（60s 整点一致性） |
| 复杂度校准器会话隔离 | `models/complexity.py` + `crew/crew.py` + `crew/scheduler.py` | 全局单例在 API 并发 run 下互相清空/污染；改为 Crew 实例持有、构造时注入 ModelFactory/Scheduler，全局 `set/get_calibrator` 保留作兼容回退 |
| contextvars 链路追踪 | 新增 `utils/trace.py` + `agents/base_agent.py` + `models/factory.py` | `llm_call` 只有 role 级弱关联，并发任务无法归因；`BaseAgent.execute` 设置 `TraceContext`，记账自动补 `task_id`/`attempt`（零依赖模块，分层护栏友好） |
| `WorkerOutcome.error_type` | `crew/scheduler.py` | 异常类型名靠 `partition(":")` 从错误串反解析，消息带冒号即失真；显式成列，字符串格式不变 |
| `setup_logging` 接线 + JSON 日志 | `utils/logger.py` + `main.py` | 日志基建从未被调用、`log_level` 是死配置；CLI 入口统一接线，`AGENTCREW_LOG_JSON=1` 切 JSON 结构化（含脱敏），纯文本模式输出与改造前逐字节一致 |
| API 并发 run 上限 + 读端点下放线程 | `api/server.py` | N 个并发 run = N×max_concurrency 的无界 LLM 并发；超限 429；ledger 查询/统计 `to_thread`，不再卡事件循环 |
| 静默吞异常补日志 | `models/affinity.py` + `factory.py` 健康循环 | 亲和性持久化读写失败完全静默、探测循环兜底 `pass`；降级行为不变但留 warning/debug |

新增 `tests/test_hardening_2026_11.py`（12 例）钉住全部修复。**实测**：mock 样例 5/5 成功，ledger 中执行期 `llm_call` 全部携带 `task_id`（编排期调用正确地不带）。

### 3.2 R2 验收整改（三个 P1 阻塞项闭环）

| 验收项 | 落地方式 | 结果 |
| --- | --- | --- |
| 预设样例仅 1/3 | `examples/samples/` 新增 `linear`（严格串行 4 任务）与 `hierarchy`（两层并行 + 用例层汇合 6 任务）两套独立夹具；`main.py run --sample NAME` 一键 mock 运行（自动开 `--mock` 并挂夹具），新增 `main.py samples` 列举命令 | 三个样例均 0 失败、0 阻塞、产物 sha256 与清单一致；形状由 `tests/test_samples.py`（25 例）钉住 |
| 无性能测试报告 | `scripts/bench.py`：并发 1/2/4/8 × 3 次，从 ledger 的 `task_started`/`task_finished` 还原峰值并发与平均并发，按图上关键路径算理论上限，判据用**达效率**而非"快了几倍" | `bench/report.md`：成功率 100%，峰值并发 1→2→4→4（等于图宽度），达效率 100%（上限 1.65×） |
| 无稳定性测试报告 | `scripts/soak.py`：同进程连续 20 次 run + 两类故障注入（provider 瞬时失败 / 产物缺件被 G1 连续驳回） | `bench/stability.md`：20/20 成功，RSS −2.3%、句柄 +1、线程不增长；故障注入两项均通过（失败被隔离在受害任务与其下游） |

两个脚本的结论函数（图谱上限、并发还原、达效率夹取）另有 `tests/test_bench_tools.py`（10 例）钉住口径。

### 3.3 R1 双端重构（不改行为）

| 目标 | 结果 |
| --- | --- |
| 后端分层 | 新增零依赖契约层 `contracts.py`；`models` 不再依赖 `runtime` / `task`；`factory.py` 2040 → 1794 行，策略拆入 `models/{errors,health,routing,budget}.py` |
| 后端护栏 | `tests/test_architecture.py`（26 例）：契约层零依赖、基础设施层不反向依赖编排层 |
| 后端验证 | 全量 1911 passed / 1 skipped / 0 failed；新旧实现对跑（冷却、四种路由策略、failover 顺序、HTTP 报文与 SSE 帧字节）全部一致 |
| 前端结构 | `renderer.js` 3891 行 → `src/` 49 个 ESM 模块（最大 238 行）；`styles.css` 1503 行 → `styles/` 32 个分片 |
| 前端护栏 | `npm run check`（分层/导入完整性/语法/体量/入口收敛）；`npm run visual`（16 张 e2e 截图视觉回归） |
| 前端验证 | e2e 28/28、selftest 16/16（测试文件未改一行） |

### 3.4 Studio Agent 依赖图（以执行者为节点）

看板「任务依赖图」改为「Agent 依赖图」：把任务 DAG 折叠成执行者 DAG，读图先看「谁把活交给谁」，需要追单时切回任务视图。

| 项 | 结果 |
| --- | --- |
| 折叠规则 | 同角色任务合并为一个 Agent 节点；跨角色依赖按 `(上游 role → 下游 role)` 聚合成一条边，`weight` = 该方向上的任务数（线宽编码） |
| 分层 | DFS 标回边 + 拓扑最长路径；Agent 互相返工形成的环不丢，画成卡片下方虚线回路，层级不会被环无限拉长 |
| 视觉编码 | 角色色 = 身份（卡片描边/头像/连线），状态色 = 当前状态（胶囊/完成率），箭头 = 依赖方向，层级带标注「第 N 层」 |
| 交互 | 缩放/平移/适应/重置、悬停聚焦上下游全部保留；点 Agent 开 Agent 详情、点卡内任务 chip 开任务详情；新增「Agent 视图 / 任务视图」切换（原任务 DAG 完整保留） |
| 结构 | `dashboard/dag.js` 拆为 `dag-canvas.js`（共享件）/ `agent-graph.js`（折叠与分层）/ `dag-agent-view.js` / `dag-task-view.js`，最大 217 行，架构检查通过 |
| 顺带修复 | `detail/panel.js` 调用了未导入的 `renderAgents`，此前每次打开详情都会抛 `ReferenceError`（不影响面板展示） |
| 验证 | `npm run check` 通过；selftest 16/16、probe 24/24（含 DAG 缩放与详情打开用例） |

### 3.5 run 生命周期完整性定点修复（2026-11-04）

代码审查结合 `output/` 下 5 个永久 `running` 的死 run 反查出的同一条故障链；业务逻辑与协作结果不变。

| 缺陷 | 位置 | 修复 |
| --- | --- | --- |
| 非白名单异常让 `run.json` 永久停在 `RUNNING` | `crew/crew.py::_start` | 补 `except BaseException` 兜底封版 `FAILED` 后原样重抛（`CancelledError` 仍单独收口为 `INTERRUPTED`） |
| 空板 / 缺 `tasks.json` 的 resume 被判 `COMPLETED` 假成功 | `crew/crew.py::_start` resume 分支 + `_final_status` | 执行前 fail loud；`_final_status` 空板一律 `FAILED` 双保险 |
| `_finalize` 非幂等，异常链二次封版改写终态 / 向已关闭 ledger 再 emit | `crew/crew.py::_finalize` | 开头 guard + 先置位再动副作用 |
| `delete_run` 读不出 `run.json` 时当「安全」放行 `rmtree` | `runtime/checkpoint.py` | fail-closed：读不出 / 形状异常按 `RUNNING` 拒绝 |
| 强杀遗留的僵尸 `RUNNING` 永远清不掉 | `runtime/checkpoint.py` | 新增 `STALE_RUNNING_AFTER=24h` + `is_stale_running`，超时无心跳允许删除；活 run 每次状态跃迁刷新 `updated_at` |
| `clean` 会误删正在写盘的活 run | `runtime/checkpoint.py::clean` / `main.py` | 新增 `protect_running` 可选参数（库层默认关闭，保持 spec §13.5 契约），CLI `--protect-running` 显式开启 |
| API `POST /runs/{id}/resume` 异常路径泄漏并发槽位 → 所有 run 永久 429 | `api/server.py` | 补 `except Exception` 归还槽位并返回 500；create/resume 的 `CancelledError` 分支收口；`_run_crew` finally 对已取消 Future 的 `.exception()` 短路 |

新增 `tests/test_run_lifecycle_hardening.py`（10 例）钉住全部修复。历史 failed run 里的 `NameError: path_scope` 与动态子任务产物路径冲突均已在此前版本闭环，不属于本轮待修项。

### 3.6 第二轮：配置接线 / 路径域 / API 读链路 / 配额（2026-11-05）

继续排查发现的四条链，全部为带行号证据的确定性缺陷；业务逻辑与协作结果不变。

| 缺陷 | 位置 | 修复 |
| --- | --- | --- |
| `gate_config` 在真实 run 中从未接线，G1/G2 开关、评审人数、收敛阈值全回落硬编码 | `crew/crew.py::_build_gatekeeper` | 透传 `self._settings.gate_config` |
| `g3_enabled` 只有赋值无读取点，配了也关不掉人工卡点 | `gate/gatekeeper.py` | `g3_enabled=False` 时构造期不再要求 prompter，两个人工闸门直接放行（默认 True 行为不变） |
| `path_scope` 用字符串前缀判定且未规范化：`["backend"]` 放行 `backend_secret/`，`["./backend/"]` 拒掉全部合法产物 | `artifacts/store.py` / `roles/binding.py` / `agents/team.py` | 新增 `utils/files.py::path_in_scope`（路径段比较 + 双侧规范化），三处共用 |
| `check` 失败时异常分支无条件 `release`，抹掉他人并发预扣 → 配额超卖 | `models/factory.py::_attempt` | `check` 移到 `try`/`reserve` 之外；`reserve` 后 `try/finally` 保证任何异常（含 `CancelledError`）都归还 |
| 非白名单异常永久泄漏配额预扣；缓存命中被 `check` 误拒并触发无谓 failover | 同上 | 缓存查询前置到配额之前，命中直接返回不占配额 |
| `complete_structured` / `_structured_attempt` 无 `check`/`reserve`，PM 拆解与 G2 评审只记账不设防 | `models/factory.py::_structured_attempt` | 按阶梯最坏次数 `3 + repair_attempts` 一次性预扣，内层 `finally` 兜住归还 |
| `OSError` 穿透读端点与 `_handle_connection`，客户端拿到 0 字节空连接 | `api/server.py` | 顶层 `except Exception` 兜底 500；ledger/stats 端点扩 `except OSError`；`api/http.py::REASONS` 补 500/413 |
| `reader.read(n)` 单次读取截断跨段大 body → 合法 JSON 误判 400；且无 body 上限 | `api/server.py` / `api/http.py` | 先 `read` 再按剩余长度 `readexactly` 补读（1s 超时收口），新增 `MAX_BODY_BYTES=4MiB` → 413 |
| `gather` 异常时不取消兄弟 `to_thread`，后台继续读完整份 JSONL | `api/server.py::_get_ledger` | 改串行两次只读遍历 |
| `Ledger.events()` 只兜 `FileNotFoundError`，文件是目录/无权限时整条读链路崩 | `runtime/ledger.py` | 新增 `_safe_lines` 迭代保护 + `except OSError` 按零事件处理（坏行 warning 语义不变） |

新增 `tests/test_hardening_2026_11_05.py`（15 例）钉住全部修复。

### 3.7 第三轮：按板块补全（2026-11-06）

沿用「只读审查 → 定点改造 → 回归钉住」的口径，把 §7 遗留清单里不改架构即可闭环的项按板块一次做完；业务逻辑与协作结果不变。

| 板块 | 落地 | 解决的问题 |
| --- | --- | --- |
| 配置 / CLI | `config.py::_yaml_values` 把 `yaml.YAMLError` 收敛成 `ValueError`；`main.py::_invalid_numbers` 校验 `--max-cost` / `--max-llm-calls` / `--max-concurrency` / `--context-budget`（含 NaN / inf）；`serve` 端口范围与「开认证却无 Key」；`switch` 的 run_id 越界与损坏 `binding_overrides.json` 拒绝覆写 | 坏 YAML 让所有子命令栈崩；NaN 使预算上限静默失效、负值让首次调用即「预算耗尽」；越界 id 可写到 output 之外；损坏的覆盖文件被静默清空 |
| HTTP API | `_validate_run_options` 白名单 + 逐字段类型/范围校验；POST body 必须是 JSON object；`DELETE` 非法 run_id 收敛 400；`REASONS` 补 409/503/504；`/status` 内存无记录时回落磁盘；`/runs/` 尾斜杠与 query 兼容 | `model_copy(update=...)` 不过滤任意键，`{"mock":"false"}` 会把 mock 强制打开，且失败被静默吞掉、请求仍回 202；重启后 `/status` 404；越界 `ValueError` 被归成 500 |
| 引擎 / 工具 | `scheduler._transition` 刷新 `_last_progress_at`；`builtins._crosses_link` 逐段拒绝符号链接 / 重解析点（把写侧已有的边界补到读工具）；`RunPythonTool` 的 timeout 校验；`gatekeeper._max_review_attempts` 接线为 `min(任务 max_reworks, 全局上限)`（默认 3 > 2，旧行为不变） | 慢门禁（>300s）只有状态跃迁会被误判停滞；`link/file.txt` 可读出项目外文件；`timeout=abc` / `1e9` 直接穿透；死配置形同虚设 |
| 前端统一组件 | 新增 `src/ui/states.js`（加载中 / 空 / 错误三态 + `isStaleRender` 防异步结果写进已卸载 DOM）与 `src/lib/validate.js`（`readNumber` / `readUrl` / `readAlias` / `showFieldError`，返回 `{value,error}` 而非静默取 0），接线到 9 个页面；样式 `components/empty.css` 的 `.page-state*` | 各页状态文案与结构不一致、4 个页面无空态；`parseFloat('1e999')=Infinity`、`parseInt('1e999')=1`、`type=number min=0` 挡不住 `.value=-5` |

新增 `tests/test_module_completion_2026_11_06.py`（40 例，一次全绿）。**验证**：3.12.10 + pytest 9.1.1 全量 **2083 passed / 1 skipped / 0 failed**（133.68s）；按 99 个测试文件分 4 批重跑累计同为 2083 / 1；`npm run check` 62 模块 / 37 分片通过，Studio e2e **28/28**、selftest **16/16**（含两次 run 与取消链路）；三个离线样例 todo 9/9、linear 5/5、hierarchy 7/7 全部 `completed`。

> 本机 pytest 断言重写仍会间歇性发作：本轮 4 次执行中出现 1 次 EXIT 139、1 次 `test_crew.py` 交付卡点用例的偶发断言失败（单跑、整文件跑、组合跑、重跑均通过）。分批重跑是规避手段，口径与环境坑见 §6。

> 工作区里 11-02 的 Studio 表现层重构（Aurora Glass + 留白二轮，41 个前端文件）属于另一轮工作，设计说明见 `studio/docs/refactor-aurora-glass.md`，与本轮四类改动互不重叠。

### 3.8 R3：对标同类框架的「能力层」完善（2026-11-06）

按「GitHub 同类框架真实代码 → 本地差距 → 定点改造 → 回归钉住」的口径做的一轮
功能完善。参考源：CrewAI `lib/crewai/src/crewai/tools/base_tool.py`
（`_validate_kwargs` 执行前用 `args_schema` 强校验 + `build_schema_hint` 回灌）、
CrewAI entity memory / MetaGPT `metagpt/memory/brain_memory.py`（history /
knowledge / 摘要三层）。**不动确定性引擎、不引入任何第三方依赖**，新增开关一律
默认关闭。

| 项 | 落地 | 解决的问题 |
| --- | --- | --- |
| 发现死链路 | 全库只读探索定位 | `ToolRegistry` **从未在生产代码里实例化**（只出现在单测与定义处），`tool_permissions` / `mcp_servers` 是死配置，README 描述的 ReAct 工具链在真实 run 中根本不启用 |
| 工具链生产化 | `config.py` 加 `tools_enabled`（默认 False，YAML/env/API/export-import 全登记）+ `Crew._build_tool_registry()` + `AgentFactory` 下发 | 开启后每次 run 自动登记 6 个内置工具、绑定 ledger、下发角色白名单、记 `tool_registry_ready`；读工具以 `run_dir` 为根、写工具只在 `run_dir/workspace`，中间草稿与交付产物物理隔离 |
| 入参强校验 | 新增 `tools/schema.py`（零项目内依赖的 JSON Schema 子集）+ `BaseAgent._execute_tool_calls` 接线 | `input_schema` 过去只渲染进提示词；现在执行前校验，失败绝不运行工具、带 `schema_hint` 正确形状回灌供模型自我纠正，记 `tool_call(success=false)` + `tool_schema_invalid`；空/自由形状 schema 放行以兼容外部 MCP |
| 新增内置工具 | `WriteFileTool`（受控写、原子写、64 KiB 上限、逐段链接检查）与 `WebFetchTool`（标准库、SSRF 防护） | §7 的「内置仅 4 个本地工具」部分闭环。`web_fetch`：仅 80/443、拒绝私网/回环/链路本地/保留 IP 与本地主机、请求前 DNS 解析校验、不跟随重定向、只收文本类型、256 KiB / 10s 上限；`fetcher` 可注入，测试完全离线 |
| 实体记忆 | 新增 `knowledge/entity.py`（纯规则五类实体抽取 + JSONL 追加写 + 按名归并 + 加权检索）+ `Crew` 规划/沉淀接线 | §7 的「实体记忆」闭环；两个开关 `reuse_knowledge` / `entity_memory_enabled` 相互独立，只开前者时 PM 上下文与本轮之前逐字一致 |

**新增 4 个测试文件、96 例**：`test_tool_schema.py`（17）、
`test_new_builtin_tools.py`（45）、`test_tool_production_wiring.py`（19，含开启
工具链跑完整 `linear` 样例的端到端）、`test_entity_memory.py`（15）。
**验证**：3.12.10 + `--assert=plain` 全量 **2179 passed / 1 skipped / 0 failed**
（基线 2083 → +96，测试只增不减）；三个离线样例 todo 9/9、linear 5/5、
hierarchy 7/7 全部 completed；`npm run check` 62 模块 / 37 分片通过。

**刻意未做（写进 §7）**：`mcp_servers` 生产接线（异步连接 + 生命周期，不做半接线）、
`web_search`（需外部搜索 API 与密钥，与密钥红线和离线可测冲突）、工具级调用次数
上限（CrewAI `max_usage_count` 的对标点，当前已由 ReAct 全局 `max_tool_calls` 覆盖）、
出站 Webhook。

> 工程坑：本轮 `api/server.py` 被某进程独占锁住，常规写入连续报
> `PermissionError / ModifyBackup path index collision`；最终用「重命名原文件 →
> 原子替换新文件 → 删除旧备份」绕过，改动已落地、目录无残留。后续若再遇到，
> 别用反复重试写文件的方式耗时间。

### 3.9 R4：编排内核重构 —— Flow Runtime（2026-11-06）

用户要求「以功能需求为先，不要仅停留在表层实现；可按需修改底层代码并重构项目
结构」。重新通读 task / task_board / scheduler / crew / gate / checkpoint / api /
Studio 的实现，结合 `docs/superpowers/specs/*design.md` §19.2（「G3 已抽象为
『询问 → 决策』，换 UI 实现即可」）、需求原文 §九调研参考（LangGraph 状态图 /
MAF durable checkpoint / AutoGen `human_input_mode` / Web UI 审批）、以及 Studio
`enums.js` 里**早已定义但后端从未写入**的 `awaiting_approval`，确认两条结构性
短板并一次改透：

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 发现 | 只读探索 | 静态 `dependencies` 只有 AND 语义、无「按上游结果选下游」；G3 是同步 `input()` 且 `ApiServer._build_crew` 硬编码 `gate="none"`，人工决策在 HTTP 通道完全断开；`resume` 一律跳过卡点 |
| 领域模型 | `Task.when_outcomes` / `TaskResult.outcome` / `RunStatus.AWAITING_APPROVAL` / `Settings.approval_mode` | 全部可选且有默认值，既有 run 数据与契约 `schema_version=1` 不变 |
| 结构重构 | 新增 `task/conditions.py` 纯语义模块 | 把「边的有效性」从 `TaskGraph` 的纯拓扑算法里分离；结果标签是受约束 slug，不是表达式，调度全程无 LLM / 无 eval |
| 调度内核 | `TaskBoard.refresh()` 增加条件判定与汇合重算 | 未命中分支以 `condition-not-met` / `conditional-skip-propagated` 预期跳过并沿分支传播；汇合节点只要一条分支 SUCCEEDED 就继续；`Crew._final_status` 不再把预期跳过判成 FAILED |
| 审批持久化 | 新增 `runtime/approvals.py`（审批单 JSONL `create/decide/consume`，三态归并）与 `gate/deferred.py`（同一个 `Prompter` Protocol 的新实现） | 人工决策从「一次性交互」变成可跨进程恢复的一等实体；`consume` 保证裁决不被重复执行；未决时 resume 复用同一张单再次挂起 |
| 主链路 | `Crew._suspend` / 抽出 `_plan_gate` / `resume_from` 识别挂起卡点 / `_exit_code` / 删除守卫 | 挂起是**正常退出**（不发 `run_finished`、不封版、退出码 0）；`awaiting_approval` 的 resume 回原卡点，`INTERRUPTED` 的旧语义逐字不变；挂起 run 受 `clean` / `delete_run` 保护 |
| CLI | `--approval-mode`、`approvals` / `approve` 子命令、`_resolve_gate` deferred 分支 | 三个独立进程完成「跑 → 裁决 → 续跑」，有 CLI 端到端用例钉住 |
| HTTP API | `GET/POST /runs/{id}/approvals`、`options.approval_mode`、`on_event(run_paused)`、`_build_crew` 不再强制 gate=none | API 通道第一次真正允许人工卡点；审批单服务重启后仍可读 |
| Studio | 新增 `features/gates-page/approvals.js`，复用第 5 页；`enums.js` / `realtime.js` 接三个新事件 | 不新增第 10 页（避开 9 页导航与数字键断言）；真实模式走 `apiFetch`，演示模式只显示空态、不伪造审批 |

**验证**：新增 `tests/test_flow_runtime.py`（23 例）与
`tests/test_approval_interfaces.py`（15 例），共 38 例；全量 **2217 passed /
1 skipped / 0 failed**（本机断言重写 segfault 后按 §6 分 4 批累计）；三个离线样例
9/9、5/5、7/7；`npm run check` 63 模块 / 37 分片通过。契约文档
`schemas/task.json` / `schemas/run.json` / `state-enums.md` / `event-types.md` /
`openapi.yaml` 同步更新。

**红线遵守**：默认 `approval_mode=interactive` 且无 `<outcome>` / `when_outcomes`
时，`TaskBoard.refresh()` 四步结果、PM 提示词、`Crew._final_status` 与 R4 之前
逐字一致；`TaskStatus` 仍是 10 态、`ALLOWED_TRANSITIONS` 一行未改；
`EXIT_CODES` 精确字典未变（挂起复用 `ok=0`）；密钥不落盘；新增字段全部可选。

**刻意未做**（按 ROI 留给下一轮）：经理 Agent 动态委派 / 层级流程、向量化 RAG、
拖拽式可视化编排画布、出站 Webhook。

### 3.10 R5：Agent 间真实协作（2026-11-06）

R4 收尾时把「`<message to>` 路由的生产接线」列进了未做项。这一轮动手时先核实了现状，
发现它比「没接线」更糟：

| 事实 | 影响 |
| --- | --- |
| `Crew._route_message` 只回 `来自{title}的说明：{goal}` | 模型发问得到的是一句角色职责描述，不是答案 |
| 该方法 `message_router` 默认 `None`，全仓无调用方注入 | 等于**从未存在过**这条能力 |
| `MockProvider` 只认 `plan` / `review` / `execute` | 直接给 mock 加 purpose 会在离线样例里抛 `ValueError` |

所以本轮把它做成一等能力，并守住「咨询 ≠ 执行」这条界线：

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 执行体 | 新增 `BaseAgent.consult()`：单次模型调用 + 专用「咨询模式」系统提示词（去掉产物协议与长度预算，回复压到 1k token） | 咨询不该产出文件：不解析围栏、不落盘、不改任务状态、不进看板，避免队友答复被当交付物提交 |
| 路由协议 | `_obtain_final_text` 用 `inspect.isawaitable` 同时接受同步回调与协程 | 既有同步夹具零改动；真协作才需要 await |
| 路由实现 | 新增 `agents/peer.py::PeerRouter`：配额、去重、问自己/未知角色零成本拒绝 | 跨角色问答是指数级膨胀入口；模型拿到答复后常原样再问一遍，不去重就是重复计费 |
| 身份绑定 | `AgentFactory._bind_router(role_key)` + `message_router` setter 刷新缓存实例 | 路由是共享对象，不按角色绑定则所有发问都记到同一个 `from_role`；缓存实例拿旧值则能力等于没开 |
| 装配 | `Crew._start` 在开关打开时构造 `PeerRouter` 并发 `peer_dialogue_enabled` | 默认关闭：系统提示词里连这段能力都不出现，`<message>` 围栏不检测 |
| 通道 | `peer_dialogue_enabled` / `max_peer_messages` 登记 YAML / env / `config export-import` / `POST /runs` options（含负值与类型校验） | 四处通道对齐，不与 R3/R4 的开关走两套逻辑 |

**验证**：新增 `tests/test_peer_dialogue.py`（16 例），覆盖真调用与不落产物、mock 降级、
Provider 异常、配额/去重/自问/未知角色、setter 刷新缓存实例、以及「发问 → 真作答 →
答复回灌 → 仍产出 a.py」的进程内端到端；另有两条 subprocess 用例验证「开着跑完整
linear 样例仍 `completed` 且留下装配事件」「默认关闭时连事件都没有」。全量
**2233 passed / 1 skipped / 0 failed**；三个离线样例 9/9、5/5、7/7。

**红线遵守**：默认关闭时系统提示词、消息检测、Agent 构造参数与 R5 之前逐字一致；
咨询不进 `TaskBoard`、不改 `RunStatus`、不影响调度顺序（调度决策全程无 LLM）；
咨询 token 计入**提问方**任务预算；密钥不落盘。

**刻意未做**：经理 Agent 动态委派 / 层级流程（需要改任务分配权归属，属于拓扑级改动）、
向量化 RAG、拖拽式编排画布、出站 Webhook、MCP 服务器生产接线。

### 3.11 R6：协作深化——委派治理 / 咨询上下文 / 咨询沉淀（2026-11-06）

按 §7 的 ROI 顺序清理 R5 遗留的三项。动手前先核实现状，第①项同样是「比记录的更糟」：

| 事实 | 影响 |
| --- | --- |
| `_create_subtask` 直接 `role=str(spec.get("role") or "generic")` | 模型写错角色名会静默落到通用兜底：任务跑完、产物看着也在，质量塌一档且不报错 |
| 委派链没有任何深度或数量上限 | 一个任务可以无限拆下去，每层都真实花 token |
| `consult` 只发一句孤立问题 | 队友只能反问「你们在做什么、要交什么文件」，长对话反复问基础信息 |

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 委派治理 | `Scheduler._delegation_rejection` 四道闸（角色存在 → 权限 → 层数 → 总数），判断「谁在派」看**父任务角色**而非 Agent 自述 | 拼错角色名不再静默降级；`manager_only` 给出一条真正的 hierarchical 语义；层数与总数各自封顶 |
| 委派留痕 | `Task.delegated_by` + `subtask_rejected_by_policy`（带可读原因） | 父任务可能被返工丢弃、角色不会；拒绝不再只有一句「rejected」 |
| 咨询上下文 | `agents/peer.py::CONSULT_CONTEXT`（contextvar）+ `_consult_context_hint` | 用 contextvar 而不是改回调签名：`(目标角色, 问题)` 二参形式一行没动，既有同步夹具零影响 |
| 咨询沉淀 | `_consult_experience` 纯函数 + `on_consult` 回调注入 | `agents` 层被架构约束禁止 import `knowledge`，写库动作留到 Crew；占位答复不落库 |
| 配置通道 | `delegation_mode` / `max_delegation_depth` / `max_dynamic_tasks` / `peer_consult_capture_enabled` 登记 YAML / env / `config export-import` / `POST /runs` options（含枚举与负值校验） | 与 R3–R5 的开关走同一套，不再另起炉灶 |

**验证**：新增 `tests/test_delegation.py`（13 例：四道闸、`generic` 白名单、`delegated_by`
留痕、无 settings 时全跳过、被拒不入板、parent 环不死循环）与 `test_peer_dialogue.py`
的 7 例补充（上下文透传、占位不落库、去重不重复沉淀、沉淀失败不影响咨询）。
全量 **2254 passed / 1 skipped / 0 failed**；三个离线样例 9/9、5/5、7/7。

**红线遵守**：四项新配置默认值 = R6 之前的行为；`Scheduler` 的 `catalog` / `settings`
都是可选参数，不传时四道闸全跳过（库层用法与旧测试逐字不变）；`schema_version` 仍为
1、`TaskStatus` 仍 10 态、`ALLOWED_TRANSITIONS` 未改；调度决策仍无 LLM。

**刻意未做**：真正的 hierarchical 监督回路（经理派活后**审核**下属产出、不通过则退回，
需要新增任务-经理绑定与第二轮评审，属拓扑级改动）；向量化 RAG；拖拽编排画布；
出站 Webhook；MCP 服务器生产接线。

### 3.12 R7：主管审核——谁派的活谁验收（2026-11-06）

R6 的四道闸只解决了「谁能派」。`manager_only` 模式下经理把活派出去之后，下属的产出
仍然只由目录里静态配置的 G2 评审人把关——**派活的人对结果没有发言权**。这一轮补上
那条监督回路。

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 评审器 | `Reviewer.review` 新增可选 `reviewer_keys` / `layer` | 评审人来源从「目录静态」扩展为「可按任务实例指定」，且不妨碍既有 `reviewer_for` 语义 |
| 主管层 | 新增 `gate/supervisor.py::SupervisorReviewer` | 只对 `delegated_by` 非空的任务工作；自派自审与未知主管跳过并留痕（返回 `None`） |
| 门禁编排 | `Gatekeeper.evaluate` 在 G1/G2 放行后插入一次主管审核 | 插入点刻意在 score / 收敛旁路**之后**：那两个旁路是对机器评审意见的容错，派活的人说不行就是不行 |
| 返工 | 复用 `_reject` / `_to_rework` / `bump_rework` | 不新增第二条返工路径；主管的 `fix_hints` 并入返工提示，改到点子上 |
| 装配与通道 | `supervisor_review_enabled`（默认关）+ YAML / env / export-import / API options；mock 时明确跳过 | 离线样例不会因为缺 supervisor 夹具而"假装审过" |
| Studio | `data.js` 在有 supervisor 记录时才追加「主管复核」卡片 | 默认三层不变（既有布局与断言不受影响），开启后驳回不会被 G2 的"全通过"掩盖 |

**验证**：新增 `tests/test_supervisor_review.py`（12 例：非委派不审、自派自审跳过、
未知主管跳过、评审人与层名正确、主管驳回走 REWORK 且带自己的 fix_hints、**score 旁路
救不了主管驳回**、G2 已驳回时不再烧调用、未注入时行为不变、mock 通道跳过、真 Reviewer
落 `layer="supervisor"` 审计事件）。全量 **2268 passed / 1 skipped / 0 failed**；
三样例 9/9、5/5、7/7；Studio 架构 63 模块、e2e 28/28、selftest 16/16。

**红线遵守**：默认 `supervisor=None` → `evaluate` 路径与 R7 之前逐字一致；
`Reviewer.review` 的新参数都有默认值（不传即旧行为）；`ReviewRecord.layer` 只是
**扩枚举**（`static/llm/human/supervisor`）；`bump_rework` 唯一调用点未变；
`schema_version=1`、`TaskStatus` 仍 10 态。

**刻意未做**：跨任务的层级流程（经理对其他**规划期**任务的产出也验收——当前只覆盖
动态委派任务）；主管审核的 `suggestions` 通道（当前只有 `issues`）；AI 主管的自动重派
（驳回后指定别人做）。

### 3.13 R8：委派被拒的原因回灌模型（2026-11-06）

R6 的四道闸会拒绝，R7 让派活的人能验收，但**被拒的模型始终不知道自己错在哪**。它只
拿到「没建成」，于是下一轮往往换个同样错的名字继续试，或者干脆把这件活丢掉——四道闸
做了正确的拒绝，却没有人告诉被拒的一方原因。

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 原因通道 | Scheduler 维护 `_last_subtask_rejection`，新增 `SubtaskDispatcher` 可调用对象暴露 `last_rejection` | `subtask_creator` 的 `(spec) -> str \| None` 签名不变；绑定方法挂不上实例状态，所以换成对象 |
| Agent 侧 | `_maybe_create_subtasks` 返回原因列表；`_execute_traced` 在有原因时回灌并重问一次 | 只纠正一轮：要么换对角色名，要么接受「自己完成」，继续追问只会在同一件事上烧预算 |
| 兼容 | 裸闭包（无 `last_rejection`）不回灌；`max_subtask_corrections=0` 关闭 | 既有测试与库层用法传的都是 `lambda spec: None`，不该凭空多出一次模型调用 |
| 事件 | `subtask_rejected` 补 `reason`；新增 `subtask_rejection_fed_back` | 「为什么被拒」与「有没有回灌」都能从审计里回答 |

拒绝原因现在有三个出口，各司其职：`subtask_rejected_by_policy`（给人看的详细原因 +
可用角色清单）、`subtask_rejected`（Agent 侧通用留痕，带 reason）、
`subtask_rejection_fed_back`（确实回灌了模型）。

**验证**：新增 `tests/test_subtask_feedback.py`（9 例：有原因则回灌并用新回复、
提示词含具体原因、只纠正一轮不套娃、裸闭包不回灌、`0` 可关闭、建成时不回灌、
dispatcher 拒绝时设原因 / 成功时清空、Scheduler 注入的是对象而非绑定方法）。
全量 **2277 passed / 1 skipped / 0 failed**；三样例 9/9、5/5、7/7。

**红线遵守**：默认 `max_subtask_corrections=1` 但只有提供 ``last_rejection`` 的回调
才可能触发，裸闭包路径的调用次数与 R8 之前完全一致；自我修正轮里的子任务请求照建但
不追问（那层已有自己的轮次上限）；`schema_version=1`、`TaskStatus` 仍 10 态。

**刻意未做**：把拒绝原因也回灌给**调度器**自动重派（当前只回灌给模型自己）；
`<subtask>` 围栏的结构化纠错建议（当前只给文本原因）；向量化 RAG；
拖拽编排画布；出站 Webhook；MCP 服务器生产接线。

### 3.14 R9：跨任务层级——经理也验收规划期的活（2026-11-06）

R7 的主管审核只覆盖**动态委派**任务：`delegated_by` 是谁就由谁验收。但 PM 在规划
阶段拆出来的任务没有 `delegated_by`——它们的主管理应也是 PM，R7 完全没管。这是
hierarchical 缺的最后一环。

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 覆盖范围 | `supervisor_scope ∈ {delegated, all}`（默认 `delegated`） | 规划期任务也能被主管验收；默认不放开是因为每任务多一次真实调用，升级不该静默涨价 |
| 主管来源 | `supervisor_role`（默认 `project_manager`） | 没有 `delegated_by` 时回落到配置角色；有则派活的人优先 |
| 去重 | 主管已在 `catalog.reviewer_for(task.role)` 里时跳过 | `architect` 的任务 G2 本就是 PM 在审，再以主管身份审一遍是重复烧钱；判断依据是目录，不是猜 |
| 审计 | `supervisor_review_skipped` 的 reason 扩到五种 | `already_in_g2` / `no_supervisor_configured` 让「为什么没审」可回答 |

**验证**：`tests/test_supervisor_review.py` 补 6 例（`scope=all` 审规划期任务、
默认 scope 不管、主管已在 G2 时跳过、未知主管跳过、空角色跳过、派活优先于配置），
该文件共 20 例。全量 **2283 passed / 1 skipped / 0 failed**；三样例 9/9、5/5、7/7。

**红线遵守**：默认 `scope=delegated` + `role=project_manager` → 规划期任务完全不受
影响，R7 的 14 个用例原样通过；两个新键走 YAML / env / export-import / API options
四通道，枚举与空串当场 400；调度决策仍无 LLM。

**刻意未做**：AI 主管驳回后的自动重派（当前只能退回原执行者）；主管 `suggestions`
通道（当前只取 `issues`）；多级主管（只有一层）；向量化 RAG；拖拽编排画布；
出站 Webhook；MCP 服务器生产接线。

### 3.15 R10：主管驳回后换个人做（2026-11-06）

`Gatekeeper` 原来的驳回只有两条路：退回原执行者（`REWORK`）或额度用尽 `FAILED`。
主管对「这个人做不好」的判断**没有出口**——它能说不行，但不能说「换谁」。

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 状态机 | 只在返工**前**改 `task.role`，仍走 `REWORK → RUNNING` | 不需要新增 `REWORK → READY`，10 态契约与 `ALLOWED_TRANSITIONS` 一行未改 |
| 候选筛选 | 新增 `gate/reassign.py::ReassignPolicy`：按 `role_fallback` 顺序取第一个**真实存在 / 非当前角色 / 非主管本人**的 | 换给没有提示词的角色等于降质；换给主管自己会同时丢掉执行者与验收人 |
| 终止条件 | `max_supervisor_reassignments`（默认 1）+ 换人**同时消耗返工额度** | 同一份额度换一只手试，不是免费加次数；额度用尽直接 FAILED，不会「换了人还失败」 |
| 触发面 | 只有 `layer="supervisor"` 的驳回才换人 | G2 只说「产出不合标准」，没有「谁做得更好」的信息；让分数决定人事不是它该管的事 |
| 留痕 | `Task.reassign_count` + `supervisor_reassigned` 事件 | 事后能回答「这个活换过几次人、换给了谁、谁做的判断」 |

**验证**：新增 `tests/test_reassign.py`（13 例：候选筛选五项、上限与 0 关闭、主管驳回
换角色且状态仍是 REWORK、G2 驳回不换人、额度用尽不换人、未注入时行为不变、board
自增、配置默认值）。全量 **2296 passed / 1 skipped / 0 failed**；三样例 9/9、5/5、7/7。

**红线遵守**：默认 `role_fallback={}` → `pick()` 恒为 `None`，`_reject` 与 R10 之前
逐字一致；`bump_rework` 唯一调用点未变（`_to_rework`）；`schema_version=1`、
`TaskStatus` 仍 10 态；调度决策仍无 LLM。

**刻意未做**：让主管在驳回时**指定**具体人选（当前只按配置的优先序自动挑）；多级
主管链（当前只有一层）；换人后的经验归因（经验仍记在任务上，不区分是谁做的）；
向量化 RAG；拖拽编排画布；出站 Webhook；MCP 服务器生产接线。

### 3.16 R11：经验检索换中文二元组 + BM25（2026-11-06）

经验库与咨询沉淀都靠关键词检索，原来的做法是「按单字切分、数命中个数」。两个问题在
真实数据上都会咬人：**单字太粗**（「写代码」与「写文档」共享一个「写」就互相命中）、
**计数不分轻重**（「的」与「幂等」权重相同，长文本天然占优）。

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 分词 | 新增 `knowledge/retrieval.py::tokenize`：英文数字按词，CJK 单字**并补相邻二元组** | 「写代码」得到 `写/代/码/写代/代码`，只共享「写」时相似度自然很低；不引入 jieba |
| 打分 | `BM25Index`：TF 饱和 + 文档长度归一 + IDF | 高频虚词 df 高、IDF 低，自动被压下去，不必维护停用词表 |
| 降噪 | **只共享单字不算命中**（要求至少一个长度 > 1 的 token） | 这是实测踩出来的：查「量子引力弦论 M 理论」会因一个「理」字命中「整理需求文档」 |
| 接入 | `ExperienceStore.query` 改用 BM25 索引；`_haystack` 明确排除产物路径 | 签名、返回值、「零命中返回空」语义与 `inject_context` 的渲染格式全部不变 |

**为什么没做向量检索**：全仓没有 embedding 接口，接一个要新增 provider 方法、网络调用
与按量计费，而项目红线是「离线可跑、样例不依赖网络」——做了离线路径就得降级，等于用
更弱的兜底换一个用不上的能力。分词与打分已独立成模块，将来替换点只在
`ExperienceStore.query` 一处。

**验证**：新增 `tests/test_retrieval.py`（17 例：分词三条、单字共享不主导、词组优于
单字、长度归一、零命中、空索引、查询去重、单字重合不算命中、纯单字查询返空、经验库
端到端四条）。全量 **2313 passed / 1 skipped / 0 failed**；三样例 9/9、5/5、7/7。
原有 9 个经验库用例**一行未改**全部通过。

**红线遵守**：无新增配置、无新依赖；`ExperienceStore.query` 签名与语义不变；
实体记忆未动（它的 `+3 / min_score` 阈值语义是独立设计的，换分词会连带改变阈值含义，
不属于本轮收益）。

**刻意未做**：向量检索（缺 embedding 通道）；实体记忆改用 BM25（阈值语义要重新设计）；
跨库统一检索（经验 + 实体仍各自检索）；拖拽编排画布；出站 Webhook；MCP 服务器生产接线。

### 3.17 R12：出站 Webhook（2026-11-06）

这套编排器此前没有任何对外通知手段：run 结束、审批挂起、任务卡住，外部系统一概不知道，
只能人盯着终端。这一轮补上第一块对外能力。

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 投递器 | 新增 `runtime/webhook.py::WebhookDispatcher`：``queue.Queue`` + daemon 线程 | `on_event` 是同步回调，可能在事件循环里；直接发 HTTP 会让整个 run 卡在网络超时上 |
| 失败语义 | 重试有上限、退避上限 2 秒；收尾最多等 5 秒排空，超时记 `shutdown_timeout` | 通知是辅助设施，不能让主流程等它 |
| 线程边界 | 投递结果先进 `_results` 队列，由**主线程** drain 后交给 sink | `Ledger.emit` 没有锁（open/append/fsync），后台线程写会与主线程交错 |
| 安全 | 拒绝带用户信息的 URL；密钥不导出/不进快照/进脱敏词表；启用时只记打码 URL | `?token=…` 抄进 ledger、密钥随 run.json 落盘，都等于泄露 |
| 接口面 | `POST /runs` 的 `options` 只放 url / events / timeout / retries | 密钥不该经 HTTP 接口传 |
| 接入 | Crew 的 `_dispatch_event` 统一出口：先 `on_event`，再 Webhook；`_has_observer` 兼容「没传 on_event 但配了 webhook」 | 否则 CLI 场景的通知永远发不出去 |

**默认推送五个 run 级节点**（`run_started` / `run_finished` / `approval_requested` /
`run_stalled` / `deadlock`），刻意不含 `task_completed`——它每任务一次，9 个任务就是
9 个请求，多数接收方只关心「跑完没有、卡在哪」。

**实测踩到的两个坑**（都已修并加回归）：

1. **结果字典的键与 `Ledger.emit` 位置参数撞名**。用 `event` 作键展开成
   `ledger.emit(name, event=...)` → `TypeError: got multiple values for argument
   'event'`，而它发生在 `_dispatch_event` 里，直接把整个 run 打挂（实测 `exit_code=1`）。
   改键名为 `webhook_event`，并把 `TypeError` 一并纳入 sink 的兜底。
2. **`_finalize` 的幂等用例绕过 `__init__`**，半构造实例没有 `_webhook` 属性。
   `_has_observer` / `_close_webhook` 改用 `getattr` 兜住。

**验证**：新增 `tests/test_webhook.py`（21 例：配置校验四条、签名往返与三类拒绝、
真实 HTTP 投递的头与体、无密钥不签名、事件过滤、未启用零请求、重试后成功且 delivery
不变、重试耗尽记失败、连不上不抛、close 排空、close 后忽略、**投递结果能落进真
Ledger**）。端到端实跑一次 mock run，本地接收端收到 `run_started` + `run_finished`
（均带签名），ledger 记 `webhook_enabled` + 2 条 `webhook_delivered`，`exit_code=0`。
全量 **2334 passed / 1 skipped / 0 failed**；三样例 9/9、5/5、7/7。

**红线遵守**：默认 `webhook_url` 为空 → 分发器 `None`，`_has_observer` 退化为「有
`on_event` 吗」，与 R12 之前逐字一致；三套离线样例零网络请求；投递失败不改 run 结局
与退出码；`schema_version=1`、`TaskStatus` 仍 10 态。

**刻意未做**：投递的持久化重放队列（进程内退出即丢未投部分）→ 多接收端（当前单 URL）
→ 接收端回调触发补偿动作 → 事件级条件过滤（如「只推失败」）→ 拖拽编排画布。

### 3.18 R13：MCP 服务器生产接线（2026-11-06）

`agentcrew/mcp` 早就有完整的客户端：能连服务器、能列工具、能调工具。但**没有一条路把
MCP 工具交给 Agent**——`mcp_servers` 因此是死配置，Agent 永远只看得见 6 个内置工具。
`_build_tool_registry` 的 docstring 里也写着「需要异步连接与生命周期管理，不在这里
半接线」，这正是这一轮要清的那条。

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 适配器 | 新增 `mcp/adapter.py::MCPToolAdapter` + `adapt_tools` | `MCPTool` 只是数据模型（name/description/inputSchema），不是 `Tool` 实现，注册表收不下它 |
| 命名 | 注册名 `server__tool`（`qualified_tool_name` 统一小写并折叠非法字符） | 第三方工具名会与内置撞名，而 `register` 是「后登记覆盖先登记」——撞上等于第三方悄悄替换了内置工具 |
| 装配 | `_attach_mcp_tools` 在 `_build_tool_registry` 之后、`AgentFactory` 之前执行 | 工厂此刻才拿得到完整工具清单，晚一步则首轮提示词里没有 MCP 工具 |
| 容错 | 单服务器失败记 `mcp_failed` 后跳过 | 一个外挂坏了不该让整支团队停摆；`CancelledError` 不在此列（`BaseException`），取消照常穿透 |
| 生命周期 | `_close_mcp` 在 `run`/`resume` 的 `finally` 里 `await` | 异常路径不走 `_finalize`，不在这里关就留下孤儿子进程 |

`schema` 原样透传：R3 的强校验只认它认识的 JSON Schema 子集，其余按「未声明」放行——
比替服务端改写安全，也不会把合法工具判成非法。

**验证**：新增 `tests/test_mcp_wiring.py`（12 例：命名规范化四条、适配器转发用服务端
原名、`isError` 转错误文本、未绑定客户端、**真连 stdio mock 服务器的端到端**、坏命令
只跳过不拖垮、坏配置形状、`tools_enabled=false` 零连接、`_close_mcp` 幂等）。
端到端实跑一次 mock run：`mcp_connected{server: demo, tools: [demo__echo]}`、
无 `mcp_failed`、`exit_code=0`。全量 **2346 passed / 1 skipped / 0 failed**；
三样例 9/9、5/5、7/7。

**红线遵守**：默认 `mcp_servers={}` 或 `tools_enabled=false` → `_attach_mcp_tools`
立刻返回，零子进程、零网络；工具调用仍受 `tool_permissions` 约束、结果照常进 ledger。

**刻意未做**：MCP 服务器的热更新（run 中途变更配置无效）→ MCP 资源（resources/prompts）
支持（当前只用 tools）→ 反向暴露（把 agentcrew 自己做成 MCP server）→ 拖拽编排画布。

> **更正**：R13 最初把「权限细到单个 MCP 工具」列为遗留，这是误判——``tool_permissions``
> 走的是 ``is_allowed(role, tool_name)`` 精确匹配，而 MCP 工具的注册名（``fs__read_file``）
> 本身就是唯一的，配置里直接写全名即可精确授权，无需改动。

### 3.19 R14：Webhook 多接收端与「只推失败」（2026-11-06）

R12 的 Webhook 只能配一个地址、只能按事件名订阅。真实团队要的是「Slack 只收失败、
内部系统全收」——一条 `webhook_url` 表达不了这个。

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 多接收端 | `webhooks: [{name, url, secret, events, only_failures}]`；`WebhookConfig.resolved()` 统一展开 | 一个团队多个接收方；旧的 `webhook_url` 五个扁平字段原样保留作简写 |
| 条件过滤 | `only_failures` 与 `events` 是**与**关系 | `status ∈ {failed, cancelled}` 或多类告警事件才算坏消息；`cancelled` 也算——对接收方而言「没跑完」就该知道 |
| 独立投递 | 每个 target 单独重试、单独 `secret` | 一个挂了不拖累另一个；共用一个密钥等于把凭证发给所有人 |
| 台账 | `webhook_delivered`/`webhook_failed` 带 `target` 名（打码） | 「哪一条失败了」不用猜；不写完整 URL（查询串可能带 token） |

**实测抓到的 bug**：`Crew._build_webhook` 早退条件只看 `webhook_url`，只配 `webhooks`
时整条链路**静默失效**——单测全绿（`WebhookConfig` 的多 target 逻辑没问题），端到端实跑
才发现两个接收端一条都没收到。修法是把 `webhooks` 纳入早退判断，并补两条 Crew 层回归。

**验证**：`tests/test_webhook.py` 21 → 33 例（多 target 各收各的、delivery id 跨接收端
共享、事件过滤互不影响、只推失败、告警事件无 status 也算坏消息、targets 覆盖旧字段、
各自密钥签名、一坏一好不影响、配置形状与非法 scheme、Crew 只配 webhooks 能建出分发器）。
端到端实跑：`all` 收到 `run_started` + `run_finished`，`alerts`（only_failures）收到 0 条。
全量 **2358 passed / 1 skipped / 0 failed**；三样例 9/9、5/5、7/7。

**兼容性**：不配 `webhooks` 时行为与 R12 逐字一致（21 个原用例一行未改全过）；
`webhooks` 与 `webhook_url` 同时配时以后者为准，避免同一地址被推两遍；导出时逐条剔除
`secret`。

**刻意未做**：投递的持久化重放队列（进程退出即丢未投部分）→ 事件级表达式过滤
（当前只有「只推失败」这一个开关）→ 接收端回调触发补偿动作 → 拖拽编排画布。

### 3.20 R15：Webhook 持久化重放队列（2026-11-07）

R14 收口后按 §7 第一档清单接着做。R12/R14 的边界很明确：后台队列是**进程内**的，
收尾最多等 5 秒，进程被强杀或接收端长时间不可用时，没投出去的通知直接丢，只在
ledger 里留一条 `shutdown_timeout`。

| 层 | 落地 | 解决的问题 |
| --- | --- | --- |
| 落盘时机 | 新增 `runtime/webhook.py::WebhookSpool` + `PendingDelivery`；`emit` 在**入队之前**按「事件 × 接收端」各写一个 JSON 文件 | 后台线程还没处理就退出也不丢 |
| 文件模型 | 一记录一文件、`os.replace` 原子写、成功 `unlink` | 单 JSONL 追加文件删一行要重写整个文件，多 run 并发会互相覆盖 |
| 补投 | `Dispatcher.__init__` 里 `load()` 后按 `target_key` 匹配当前配置，重新放进**同一个**队列 | `delivery` / `timestamp` / 签名不变；不新增第二条投递路径，失败仍走同一套重试与落账 |
| 终止条件 | 24 小时或累计重放 5 次即丢弃，记 `replay_discarded` | 陈年事件不无限重放 |
| 安全 | 文件只存 `sha256(url)[:16]`，payload 按 `secret_values()` 递归脱敏 | 不绕过 Ledger 已有的脱敏；URL 与密钥都不落盘 |
| 装配 | `Crew._start` 传 `<output_dir>/_webhook_spool/`（所有 run 共享）；`_build_webhook(spool_dir=...)` 保持可选 | `_run_dirs` 只认含 `run.json` 的目录，`clean` / `delete_run` 不会误删；库层旧调用零落盘 |

**刻意选择 at-least-once**：多进程同时补投同一份 spool 可能重复到达，靠既有的
`X-AgentCrew-Delivery` 幂等，而不是引入跨进程文件锁——后者会让「补投」这条辅助路径
反而成为 run 启动的阻塞点。配置里的地址变了就丢弃旧记录（`replay_target_missing`），
不把旧事件发到新地址。

**验证**：新增 `tests/test_webhook_spool.py`（18 例）：spool 往返 / 脱敏 / 坏文件 /
过期 / 超次数、不传 `spool_dir` 时零落盘、成功删文件、失败留盘、模拟崩溃后补投的
`delivery` + 签名不变、只补投失败的那个接收端、Crew 装配，以及**两次真实 CLI run**
的端到端（第一次对端全 500 留盘，第二次启动补投并以 2xx 到达）。全量
**2376 passed / 1 skipped / 0 failed**；三样例 9/9、5/5、7/7。契约
`event-types.md` 同步补 `spool_enabled` / `replayed` 与两种新 `error` 取值。

**红线遵守**：`spool_dir=None`（库层与既有单测）时行为与 R14 逐字一致，零文件零扫描；
未启用 Webhook 时分发器仍为 `None`；密钥不落盘；`schema_version=1`、`TaskStatus` 仍
10 态；投递失败不改 run 结局与退出码。

**刻意未做**：跨进程文件锁下的「恰好一次」（收益不抵复杂度）→ 事件级表达式过滤
（当前只有「只推失败」）→ 接收端回调触发补偿动作 → 拖拽编排画布。

### 3.21 R16：官方 Python SDK + 示例（2026-11-07）

第一档清单最后一项。`openapi.yaml`（由 `api/server.py` 逆向得到）稳定后，外部系统
不必再手拼 HTTP 请求与 SSE 帧。

| 层 | 落地 | 理由 |
|---|---|---|
| 落点 | `sdk/` 顶层独立包 `agentcrew_sdk`，自带 `pyproject.toml` 可 `pip install -e ./sdk`；**不 import `agentcrew`** | 架构守卫只扫 `agentcrew/` 包；SDK 是消费方，不该把服务端依赖与分层约束传染给调用方 |
| 依赖 | **零新增**：标准库 `urllib` / `json` / `time` | 不把 openai / pydantic / rich 拖进调用方环境；Python 3.10+ |
| 契约 | 按 `openapi.yaml` 封装 17 个方法 + `request()` 逃生口 | R21 起契约有守卫（§3.26）：`/providers` 族等端点已补入，SDK 扩封时契约先行 |
| 错误 | `TransportError` / `APIError` 两族，401/404/409/429 各有子类，429 带 `Retry-After`；2xx 非 JSON 记 `ResponseError` | 调用方不必解析 `{"error": ...}` 原始体 |
| SSE | `iter_events` 惰性生成器，跳过 `: ping`，`finally` 关响应 | 长连接不缓存整条流、不泄漏句柄 |
| 便利 | `iter_ledger` 按 offset/limit 自动翻页；`wait_for_run` 轮询到终态（`awaiting_approval` 明确不算终态） | 运维脚本最常见的两件事 |
| 示例 | `sdk/examples/quickstart.py`（默认只读巡检，`--goal` 才创建 run）与 `stream_events.py` | 默认对生产无害、无需模型 Key |

**刻意未做**：异步 / 线程池客户端（调用方自己开线程，每线程一个实例）· 强类型响应
模型（契约里大量可选字段与自由形状，加字段就失真）· 隐式重试（会把一次
`POST /runs` 变成可能创建多个 run）· 封装 `openapi.yaml` 之外的端点（用
`request()` 逃生口）。

**验证**：`tests/test_sdk.py` 34 例，全部跑真实本地 HTTP 服务（请求方法 / 路径 /
查询串 / 头 / body / 错误映射 / SSE 帧 / 翻页 / 等待终态 / 示例 `--help`），外加
**一次真 `main.py serve` + 真 mock run 的端到端**（health → create → wait → ledger
→ stats → approvals）。全量 **2410 passed / 1 skipped / 0 failed**；`ruff check`
对新增文件全绿（根仓库另有 14 项**既存**告警，见 §7，本轮未扩大改动面；R18 已全部闭环）。

**端到端踩坑（值得记住）**：从 pytest 进程继承 `os.environ` 起子进程，宿主机注入的
`PYTHONPATH` shim 会让子进程 `import ssl` 直接崩（`_TLSMessageType` 只读），
`HTTP_PROXY` 又会把 127.0.0.1 的健康检查拖到超时（一次用例跑 3 分 51 秒）。
子进程改用**白名单环境**（剔除所有 `PYTHON*` / `*PROXY*`）+ 主进程设
`NO_PROXY=127.0.0.1,localhost` 后 3.19 秒通过。R15 的 CLI 端到端不受影响是因为
`main.py run` 路径没有触发同一组合。

**红线遵守**：不 import `agentcrew`、不新增第三方依赖、不改配置合并顺序、
`tests/test_architecture.py` 的分层表未动；`tests/conftest.py` 仅加一行把 `sdk/`
加入测试搜索路径（CI 直接跑仓库根 pytest，不会先装 SDK）。

### 3.22 R17：run 生命周期加固——心跳 / 僵尸收敛 / resume 锁（2026-11-07）

第二档的两项稳定性欠账合成一轮。此前只有 `is_stale_running`（`run.json.updated_at`
+ 24h）这一个**被动**判据，仅用于删除守卫：进程被强杀后 run 永远停在 `running`，
`status` / `/runs` 都显示「正在跑」；同时没有任何机制阻止两个进程 `resume` 同一个
run 写坏 checkpoint。

| 层 | 落地 | 理由 |
|---|---|---|
| 心跳 | `runtime/lifecycle.py::RunHeartbeat`，daemon 线程每 30s 原子写 `heartbeat.json`（pid + 时间） | 不碰 `Ledger.emit`（无锁，后台线程写会交错）、不改 `run.json`（不与调度器状态落盘竞争）。长任务没有状态跃迁时 `updated_at` 会很旧，心跳是独立存活证据 |
| 孤儿判定 | `is_orphaned_running`：有心跳只看心跳（15min 阈值）；**没有心跳的老 run 回退原 24h 判据** | 升级后第一次启动不能把历史 run 全改成 interrupted；`awaiting_approval` 与读不出 `run.json` 一律不收敛（fail-closed） |
| 收敛 | `reconcile_stale_runs` 在 CLI `run` / `serve` 启动时扫一次，把孤儿 `running` 改成 `interrupted` + 记 `run_reconciled` | 只改 `run.json` 的 status/updated_at，不动 `tasks.json` / 产物；库层 `Crew.run()` 不做（程序化调用不该有隐性副作用） |
| resume 锁 | `ResumeLock` 用 `O_CREAT\|O_EXCL` 原子占锁，`resume()` 的 `finally` 释放；带 owner token，release 只删自己的 | `exists()+write()` 两步有竞态；异常路径不解锁会让这个 run 永远无法续跑 |
| 陈旧锁 | 无新鲜心跳且锁超过 15min 可接管；**有新鲜心跳一律不接管** | 进程强杀留下的锁必须能自愈，但不能抢活进程的锁 |
| 跨平台 | **不用 `os.kill(pid, 0)` 判存活**：Windows 上它走 `TerminateProcess`，检查可能直接把进程杀掉；`pid` 只写进文件供人工排查 | 存活一律看心跳时间戳 |
| 入口 | CLI `resume` 把 `ResumeLockError` 转可读提示（退出码 3）；API `/runs/{id}/resume` 转 409（与「run 已在运行中」同语义） | 不让用户对着 traceback 猜，也不把冲突伪装成 500 |

**验证**：新增 `tests/test_run_lifecycle.py` 24 例——心跳读写/损坏/线程清理、无心跳回退
24h、新鲜心跳压过旧 `updated_at`、过期心跳判孤儿、五种非 running 状态与坏 `run.json`
不收敛、dry-run、只改孤儿并写 ledger 事件、坏 schema 上报不抛、锁互斥/幂等释放/
陈旧接管/心跳保护/不删别人的锁/异常上下文释放、`Crew.resume()` 在 `_start` 抛异常时
解锁，以及**一次真 CLI 启动**的端到端（造一个孤儿 + 一个带新鲜心跳的活 run，验证只
收敛孤儿、新 run 的心跳文件被正常删除）。相关既有用例 191 例全过；全量
**2434 passed / 1 skipped / 0 failed**；三样例 9/9、5/5、7/7。契约
`event-types.md`（`run_reconciled`）、`state-enums.md`（新增收敛路径）、
`openapi.yaml`（resume 409 语义）同步。

**实测彩蛋**：第一次冒烟时，启动 reconcile 真的把仓库里两个 10 月遗留的
`running` run 收敛成了 `interrupted`——不是模拟数据，是这条路径生效的直接证据。

**红线遵守**：`schema_version=1` 与 10 态 TaskStatus 未动、配置合并顺序未动（没有加
新配置项）、密钥不落盘（心跳/锁文件只有 pid / 时间 / owner）、`Ledger.emit` 仍只在
主线程调用、测试只增不减。reconcile 固定启用且无 LLM 调用，不改变成本结构，因此
没有加「默认关闭」的开关。

**刻意未做**：分布式锁（项目是单进程 asyncio，不引入外部协调服务）· 按 pid 精确判活
（跨平台代价与风险不值）· 收敛时自动 `resume`（状态修正与重跑是两件事，不该替用户
决定）· 心跳写进 `run.json`（会与状态落盘竞争）。

### 3.23 R18：CI 就绪与限流配置通道（2026-11-07）

R16 发现全仓 `ruff check .` 有 14 项历史告警，但「CI 首次实跑」一直没做。本轮装上
CI 同版本 `mypy==2.4.1` 后又暴露 9 项错误（其中 1 项是 R17 新增 `lifecycle.py` 的
`__exit__` 注解）。两项一起收口，才谈得上 CI 可用。

| 类 | 数量 | 处理 |
|---|---|---|
| ruff I001 / F401 / UP037 | 12 | `ruff check . --fix` 自动修（import 排序、未用导入、去引号注解）；逐个复核 diff，无循环导入风险（相关文件都有 `from __future__ import annotations`） |
| ruff UP028 | 1 | `_safe_lines` 改 `yield from handle`；文件句柄不接收 send/throw，`try/except OSError` 语义不变 |
| ruff B905 | 2 | `zip(deps, statuses, strict=True)`；`statuses` 由 `deps` 推导长度必等，加 strict 只是把不变量写出来 |
| mypy 类型错误 | 9 | 见下：全部是类型标注 / 结构 / narrowing 调整，**不改运行时逻辑** |

mypy 的 9 项分别落在：`lifecycle.__exit__`（`bool` → `None`，不吞异常）·
`task_board` 列表推导的 Optional narrowing（展开成显式循环）· `crew` 的 `prompter`
None guard 与 `message_router` 类型（实际接受 `PeerRouter` 这类**非 Callable** 的
duck-type 对象，字段/getter/setter 放宽为 `Any | None`）· `registry` 的
`subtask_creator.setter` 移到 getter 相邻（mypy 才识别成可写 property，顺带消掉
`scheduler` 的「property 只读」连带错误）· `reviewer.layer` 改用
`Literal["static","llm","human","supervisor"]` · `builtins._crosses_link` 的 `rel`
改收 `PurePath`（`safe_relpath` 返回 `PurePosixPath`）。

**限流配置通道**：`api_rate_limit_window` 此前既无 `AGENTCREW_*` 映射、也不在
`agentcrew.yaml` 标量白名单，运维写进 YAML 会被静默忽略。本轮按 R3 的「4 通道」
口径补齐：env 映射、YAML 标量白名单（同组 `enabled` / `max` 一并补）、
`config export`（仅偏离默认时导出）、`config import` 顶层合并键；字段加
`Field(default=60, ge=1)`，非法窗口在加载期就报错，`serve` 启动期的兜底校验保留。
`tests/test_rate_limit_config.py` 10 例覆盖 env / YAML / env 覆盖 YAML / 非法值 /
默认值不变 / export / 默认导出无新增键 / export→import 往返。

`scripts/ci-check.ps1` 也钉成 `ruff==0.17.3` + `mypy==2.4.1`，与 `ci.yml` 一致，
避免「本地绿、CI 红」。

**验证**：`ruff check .` 全仓全绿；`mypy agentcrew/` 114 文件零错误；全量
**2444 passed / 1 skipped / 0 failed**；三样例 9/9、5/5、7/7。相关既有用例
（config / api_auth / 契约文档守卫）一并复跑通过。

**刻意未做**：3.10 / 3.11 / 3.13 的矩阵**仍需 GitHub Actions 实跑**（本机只有
3.12.10）；`zip(strict=)` 要求 3.10+，与 `pyproject.toml` 的下界一致。CI 的
`pytest -q` 是单进程全量；本机 Windows 用 `python scripts/test.py`（崩溃自动重试），人工分批只作兜底。

**刻意未做（下一轮再定）**：SDK 扩封新端点（契约已由 R21 守卫锁死）·
异步客户端 · 发布到 PyPI。

### 3.24 R19：主管 `suggestions` 通道（2026-11-07）

R7/R9 给主管加了「谁派的活谁验收」，但主管除了 approve/reject 之外的改进意见
（「命名可以再统一」「建议补类型注解」）一直**没有出口**：它们混在
`gate_result.suggestions` 里，而该事件 G2 / 人工 / 主管三层都写，订阅方捞不出
「派活的人说了什么」；且契约刻意不让 suggestions 进 `ReviewRecord`，等于白写。

R19 在 `Reviewer._finish()` 落主管层裁决时，若 `suggestions` 非空（approve /
reject 都发），额外 emit 一条独立事件 `supervisor_suggestions`。恪守三件刻意不做的
事：**不改 `verdict`**（建议不是驳回理由）、**不进 `GateDecision.fix_hints` /
`ReviewRecord.issues`**（不会变成返工指令）、**不碰返工额度**。空建议不发，零开销。

`python main.py status <run_id>` 作为查看 run 的第一入口，汇总最近 5 条建议；
完整记录始终在 ledger 的 `supervisor_suggestions` 事件里，便于下游订阅与审计。
契约同步：`event-types.md` 新增 `supervisor_suggestions` 条目（含触发时机、
data 字段、三件刻意不做的事）、`gate.json` 的 `suggestions` 描述放宽到
`layer=llm / supervisor`。

**验证**：`tests/test_supervisor_review.py` 新增 4 例（建议独立事件、空建议不发、
G2 建议不误标、驳回时建议照发但不混进 issues/fix_hints）+ `test_status_shows_
supervisor_suggestions`（端到端验 `status` 汇总）。相关门禁 / 评审 / 契约守卫用例
一并复跑通过；`ruff` 与 `mypy agentcrew/` 维持全绿。全量回归见 §6 表末。

### 3.25 R20：单步/断点调试（2026-11-07）

第二档最后一项。R4 的「卡点挂起 → 跨进程恢复」只覆盖 plan/delivery 两个阶段
边界；本轮把同一套审批纪律搬到**任务粒度**：`debug_step` 开启时（CLI
`--debug-step` / env `AGENTCREW_DEBUG_STEP` / YAML `debug_step`），调度器每次
派发新任务前过一道断点门，无未放行候选就建 `step` 审批单挂起为
`awaiting_approval`；`approve --decision step` 只放行断点上的任务，
`--decision run` 解除断点跑到终局，`--decision abort` 取消 run。

| 组件 | 改动 |
|---|---|
| `runtime/approvals.py` | `ApprovalCheckpoint` 增加 `"step"`、`STEP_ACTIONS = {step, run, abort}`（存储层按卡点校验，外部通道乱传不污染引擎）；`consumed_steps()` 汇总已放行任务 id 与终局指令 |
| `task/task_board.py` | 新增 `peek_ready(limit)`——`take_ready` 的只读版；抽出共享候选计算 `_dispatch_candidates`，两边顺序永不漂移 |
| `gate/stepper.py`（新） | `StepGate`：镜像 `DeferredPrompter` 的三步纪律（建单 → 跨进程裁决 → resume 消费）；pending 单复用不重复建单 |
| `crew/scheduler.py` | 可选 `step_gate` 参数；主循环在 `take_ready` **之前**、无在跑 worker、非取消时过门——断点上下文必须是「还没派发」的任务 |
| `crew/crew.py` | `_make_step_gate`（`debug_step` 关闭时 `None` 零参与；`run` 裁决已消费则不再武装）、`_apply_step_decision`（resume 重入前消费裁决，`abort` → `CANCELLED` + `RunCancelled`，与 plan cancel 同一条收尾路径）、`resume_from` 从 `config_snapshot` 恢复 `debug_step` |
| `config.py` / `main.py` | `debug_step` 字段 + env / YAML / `config export` / `config import` 四通道；`run --debug-step`；`approve --decision` 增 `step` / `run` |

四条刻意边界：**断点在派发之前**（挂起时任务还是 `READY`、attempts 未扣）·
**默认关闭零参与**（执行路径逐字不变）· **并发语义是攒批**（只在无在跑 worker
时停留，避免带病挂起留 RUNNING 孤儿；`max_concurrency > 1` 时已放行任务凑批
派发，断点的单位是「任务的派发权」而非「时间片」）· **断点属于 run 不属于
进程**（开关随快照落盘，resume 自动恢复；提前放行用 `--decision run`）。

契约同步：`state-enums.md` 的 `checkpoint ∈ {plan, delivery, step}`、
`event-types.md` 的 `run_paused` / `approval_requested` 枚举、`openapi.yaml`
approve 端点的 step 卡点裁决说明。

**验证**：新增 `tests/test_debug_step.py` **17 例**（裁决校验 ×6、consumed
汇总 ×3、peek_ready ×2、StepGate ×4、Crew 端到端 ×5，含真 resume 的
挂起-放行-解除断点三段式与 abort 取消）；审批 / 面板 / 调度 / Crew / 契约
九个邻近套件 332 例全过；`ruff` 与 `mypy agentcrew/`（115 文件）全绿。

### 3.26 R21：API 契约守卫（2026-11-07）

R16 封装 SDK 时发现 `openapi.yaml` 缺 `/providers` 族与执行体切换端点，当时
以「刻意未做」记录在案——缺口静默存在了四轮，因为契约是手写的，没有机制强制
它与代码同步。本轮两步收口：

- **`server.API_ROUTES`**：路由清单常量（21 条 `(方法, 路径模板)`），对外契约
  的唯一事实源。
- **`tests/test_api_contract.py`**：清单 ↔ openapi `paths` 双向集合比对 +
  `ApiServer(port=0)` 上对每条路由发真实请求（断言响应不是未匹配哨兵
  `404 {"error": "Not Found"}`）。探测用不存在的资源名 + 空 body，零副作用；
  SSE 端点读响应头即断开，经 bus 连发事件让处理器撞 RST 退出（规避 3.12
  `wait_closed()` 等在途连接把 teardown 拖到 60s 的坑，实测 0.2s）。

补进契约的 5 条端点：`GET /providers`、`GET /providers/{name}/models`、
`POST /providers/{name}/test`、`POST /runs/{id}/agents`（角色执行体热切换）、
`POST /runs/{id}/tasks/{task_id}/agent`（任务级覆盖）。

**验证**：新增 **24 例**全过（清单比对、哨兵钉死、21 条路由逐条探测、405
抽查）；API 四套件 96 例全过；`ruff` / `mypy agentcrew/` 全绿。全量见 §6 表末。

### 3.27 R22：resume 状态白名单 + MCP 管道关闭（2026-11-07）

两个六层审计遗留小项一起收口：

- **resume 状态白名单**（R17 只补了进程锁一半）：`Crew.resume_from` 拒绝
  completed（终局成功，续跑无意义）与 running 且**心跳文件新鲜**（R17+ 活进程
  每 30s 写心跳——「正被另一进程跑着」的明确信号，此前 resume 会双写坏
  checkpoint 且无任何拦截）。其余放行：**failed 也是恢复路径**（未处理异常的
  兜底封版把崩溃当场记成 FAILED），无心跳老 run 保留既有行为（与 reconcile
  24h 判据同口径）。拒绝走 `ValueError`，CLI / API 复用既有错误通道。
  教训：第一版把 FAILED 也拒了，17 个既有用例当场炸——读 flows 才发现「兜底
  封版 FAILED → resume 恢复」是真实支持的路径，白名单随之收窄。
- **MCP stdio 管道关闭**：`StdioTransport.close()` 此前只 terminate 子进程，
  三条管道的 asyncio 传输句柄留给 GC（每次 run 收尾报 unclosed transport）；
  现在显式关闭。stdout/stderr 是 `StreamReader`，asyncio 子进程包装无公开
  关闭口，经底层 `_transport` 关闭（与 asyncio 内部实现同源）。

**验证**：新增 10 例（`tests/test_resume_whitelist.py` 9 例 + stdio 关闭 1 例）；
test_crew 五个「拿跑完的 run 当夹具」的用例补 `_mark_interrupted` 保持自洽。
`ruff` / `mypy agentcrew/` 全绿；全量见 §6 表末。

### 3.28 R23：功能测试报告（2026-11-07）

验收报告遗留的最后一个小项。`docs/TEST-REPORT.md` 两份矩阵：

- **模块矩阵**：`pytest --collect-only` 按文件计 120 个测试文件、2501 用例，
  用 AST 分析各文件的主要导入把用例聚合到 17 个子系统（一个文件只计入一组，
  跨子系统端到端按被测主线归组），每行附主要覆盖点；
- **场景矩阵**：17 类业务动作 → 用例来源 + 关键断言（全链路编排、三层门禁、
  审批挂起恢复、断点调试、生命周期、委派协作、契约校验、API/SDK/CLI、
  Webhook、MCP、成本配额、安全脱敏、确定性 mock、架构守卫）。

如实声明的边界：**没有行覆盖率数字**（项目不依赖 coverage，矩阵是
用例-模块-场景口径）、mock 与真实模型的行为差、容量未压测、CI 覆盖 Linux
四版本而 Windows 仅本机 3.12 验证。README 文档索引、HANDOVER 文档地图同步接入。

### 3.29 R24：工具管理页与单工具试跑（2026-11-07）

第三档「可视化工具注册与在线调试」。tool 注册表此前只服务 Agent 的 ReAct
循环，想单独验证一个工具只能跑整轮 run。本轮三个入口共用
`agentcrew/tools/lab.py`：

| 入口 | 形态 |
|---|---|
| CLI | `tools`（清单）· `tool <name> --args JSON [--json]`（试跑，退出码 0 成功 / 1 工具报错 / 3 未开或入参非法） |
| HTTP | `GET /tools`（清单 + 开关）· `POST /tools/{name}/test`（body `{"args": {...}}`） |
| 看板 | 首页新增「工具」标签页：清单 + 参数模板 + 试跑结果（耗时/沙箱路径） |

四条边界：**同一条校验**（`validate_tool_args`，与 Agent `<tool_call>` 一致）·
**沙箱隔离**（`<output>/_tool_trial/<时间戳-id>/`，读以沙箱为根、写只写其
`workspace/`）· **默认关闭**（`tools_enabled=false` 时清单可看、试跑拒绝：
CLI 退出码 3 / API 409——调试页不扩大执行面）· **失败分流**（未知工具 404 /
入参非法 400 / 运行时失败 200+`ok=false`）。

契约同步：`openapi.yaml` 两个端点 + `API_ROUTES` 清单（R21 守卫自动比对与
逐条探测，本轮新增 2 条路由自动纳入）。**验证**：`tests/test_tool_lab.py` 9 例
+ `tests/test_api_tools.py` 8 例；CLI 与看板 JS 语法实跑核对。

### 3.30 R25：多用户与 RBAC（2026-11-07）

第三档「多用户与 RBAC」按最广范围落地。此前一个共享 Key，谁都能删别人的 run。

- **密钥库** `agentcrew/api/rbac.py`：`<output>/_auth/keys.json`，只存
  `scrypt(salt)` 哈希（0600，明文仅创建时打印一次）；按 mtime 失效重读，
  CLI 建/吊销密钥对运行中的服务立即生效。
- **三层模型**：密钥（名称/角色/附加作用域/run 白名单/过期/吊销）→ 角色
  （viewer / operator / admin / custom）→ 9 个作用域。
- **授权**：`required_scope(method, path)` 路由表声明所需作用域，中间件统一校验
  （缺权限 403），主体经 ContextVar 传给 handler（每连接独立 task，不串）。
- **归属**：API 建的 run 写 `owner.json`；非 admin 只见自己的 / 白名单里的 run，
  别人的 run 回 **404**（不泄露存在性）；CLI 建的 run 视作 local，仅 admin 可见。
- **审计**：`decided_by` 取密钥身份名（覆盖 body 自由文本）；越权 / 建 / 吊销写
  安全事件（不含密钥；密钥只参与哈希比对）。
- **入口**：`keys add|list|revoke` CLI、`GET/POST /keys` 与 `DELETE /keys/{id}`、
  看板「密钥」标签页（admin）。
- **兼容**：legacy `api_keys` 等价 admin；无密钥库时 `/keys` 回 503——
  单密钥部署不需要改任何配置。

**验证**：新增 `tests/test_rbac.py` 26 例（存储层 5 / 作用域表 11 / API 集成 7 /
归属与审计 3）；契约守卫自动纳入 3 条新路由。

### 3.31 R26：拖拽式可视化编排（2026-11-07）

第三档「拖拽式编排画布」按文档范围落地：节点 + 连线 + 属性面板 + YAML 双向。

- **后端** `agentcrew/flow/spec.py`：`FlowSpec` 的 `tasks` 直接复用规划草案
  `TaskDraft`——画布 YAML 与引擎同一份 schema，不需要第二份校验；图校验
  （重复 id / 悬空依赖 / 自依赖 / Kahn 成环报路径 / 无入口）给出中文可读理由；
  落盘 `<output>/_flows/<name>.yaml`，**坏流程不写入**；名字白名单防目录穿越。
- **API**：`GET/POST /flows`、`GET/DELETE /flows/{name}`、
  `POST /flows/validate`（不落盘，回显规范化 YAML——导入导出唯一通道，
  前端因此不引 YAML 库）；作用域复用 runs:read/write/admin。
- **前端**：看板「编排」标签页——SVG 画布（拖拽 / 端口连线 / 连线删除 /
  自动分层布局）、属性面板（id/标题/角色/优先级/依赖/条件标签/产物/验收标准）、
  校验/保存/载入/示例/YAML 面板。
- **CLI**：`flow sample|validate|list`（校验不合法退出码 2）。

**已知边界**（R28 已补上起跑通道，见 §3.33）：画布现在可一键起跑；剩余边界是
流程暂不支持 `if/else` 表达式（条件边只认 `<outcome>` 标签）与循环节点。

**验证**：新增 `tests/test_flow_spec.py` 20 例（YAML 往返 / 校验 / 落盘 / HTTP）；
看板 JS 用 `node --check` 校验；契约守卫自动纳入 5 条新路由。

### 3.32 R27：向量检索（2026-11-07）

第三档「向量检索」。检索从 BM25 单通道升级为混合打分，**默认关闭**。

- **通道** `agentcrew/knowledge/embeddings.py`：`OpenAIEmbedder`（OpenAI 兼容
  `/v1/embeddings`，provider/密钥复用既有目录解析）+ `HashEmbedder`（本地确定性
  退化：字符 n-gram → 512 维、L2 归一，只捕捉字面重合）+ `CachedEmbedder`
  （`sha256(model+text)` 落盘缓存 `<output>/_cache/embeddings.jsonl`）。
- **混合打分**：`ExperienceStore.query(..., doc_vectors, query_vector,
  vector_weight)`——BM25 分按本批最大值归一到 [0,1] 后与余弦加权；权重 0 与
  R11 逐字一致，缺向量自动退回纯 BM25。
- **接线**：`Crew._prepare_knowledge_context` 变 async，`_retrieval_vectors`
  算经验库与查询向量；端点失败降级并留 `embedding_fallback` 事件（带原因），
  成功留 `embedding_used`（通道名 / 文档数 / 维度）。
- **配置**：`embedding_enabled` / `embedding_model` / `embedding_dim` /
  `retrieval_vector_weight`，env 与 YAML 双通道。
- **CLI**：`recall <query> [--role] [--limit] [--json]`——与 run 内同一套
  embedder 与权重，输出标明实际通道。

**已知边界**：哈希退化路径不具语义能力（近义不命中），生产请配真实模型；
实体记忆的向量化由 R29 补齐（见 §3.34）。

**验证**：新增 `tests/test_vector_retrieval.py` 11 例；既有 experience /
knowledge_reuse / retrieval 三套件全过（33 例）。

### 3.33 R28：从画布起跑（2026-11-07）

R26 的边界「画布只能产出定义、不能起跑」在这一轮补上。

- **规划直通**：`Crew(preset_tasks=...)` 跳过 PM 调用，但仍走
  `with_final_task` → `validate_plan` → 评分 → `plan_created`
  （`planner_attempts=0`、`source="flow"`）与 `flow_plan_loaded` 留痕；
  校验失败直接 `PlanFailedError`，**不回灌重试**（人写的流程不该被模型重拆）。
- **预留资源前置**：`flow.validate_flow` 新增两条——声明 `T-FINAL` 或
  `DELIVERY.md` 直接报错（此前要跑到交付任务才因路径冲突失败）。
- **入口**：`run --flow NAME|FILE`（缺省目标取流程描述）、
  `POST /runs` 的 `options.flow`（内联）与 `options.flow_name`（流程库），
  画布「起跑」按钮；两者互斥、类型在 API 入口校验。
- **顺手修掉**：R26 示例流程里的两处真实缺陷——角色名写错
  （`tech_writer` → `technical_writer`）、产物与引擎保留的 `DELIVERY.md`
  抢路径（改为 `docs/DELIVERY-NOTES.md`）。CLI 冒烟第一次跑就暴露了它们，
  说明「示例能被真正执行」比「示例看起来对」重要。

**验证**：新增 `tests/test_flow_run.py` 7 例（含「PM 一次都不许被调用」的桩、
坏流程快速失败、API 202/400、CLI 文件/名字两条加载路径）+
`test_flow_spec.py` 1 例保留资源检查。

### 3.34 R29：实体记忆向量化（2026-11-07）

R27 标注的边界落地。**不加列、不迁移**：向量按 `name + kind + context`
（`EntityMemoryStore.haystack`）现算，缓存与经验库共用
`<output>/_cache/embeddings.jsonl`（键含通道名 + 文本），`entities.jsonl` 格式不变。

- `EntityMemoryStore.query/inject_context` 接受 `doc_vectors` / `query_vector` /
  `vector_weight`：关键词分归一到 [0,1] 后与余弦加权；
- **召回规则比经验库多一条**：关键词没过 `min_score=2` 阈值但余弦 ≥ 0.15 的
  实体也会被召回（换说法的近义命中），这是向量通道的核心价值；
- Crew 的 `_retrieval_vectors` 泛化为「任意带 `all()` + `haystack()` 的库」，
  `embedding_used` / `embedding_fallback` 事件带 `kind`（experience / entity）
  区分来源；实体嵌入失败时同样降级回纯关键词，不炸 run。

**验证**：`tests/test_entity_memory.py` 新增 5 例（haystack、权重 0 等价、向量召回
阈值外的实体、向量缺失回退、Crew 接线与事件 kind）。

### 3.35 R30：审批单超时与告警（2026-11-07）

R4 标注的遗留「挂起 run 的超时与主动告警」。此前审批单可以无限期 `pending`。

- `approval_timeout_seconds`（默认 0 = 永不超时，与之前逐字一致），env / YAML 双通道；
- `ApprovalRequest` 新增 `expires_at` / `expired_at`，状态加 `expired`
  （追加可选字段 + 新 op，**旧 run 的审批单不加迁移即可读**）；
- `ApprovalStore.is_expired` / `expire_overdue`：只把「仍 pending 且过窗口」的单
  标记过期，已裁决/已消费不受影响；`decide` 对过期单给可操作提示；
- 结算点：`status` 命令与 `serve` 启动（`_expire_overdue_approvals`），每张过期单
  写 `approval_expired` 到该 run 的 ledger（Webhook 可据此告警）；
- **run 结局不变**：过期只表示「这张单不能用了」，重跑由人决定；
- `approvals` 输出剩余秒数 / 超时时刻。

**验证**：新增 `tests/test_approval_timeout.py` 6 例（无超时、有超时、批量结算
幂等、裁决拒绝、配置通道、启动扫描 + ledger 事件）。

### 3.36 R31：条件边的否定与兜底（2026-11-07）

R4 遗留「条件表达式」的**受限解**：不引表达式引擎，只给标签加两种写法。

- `!label`：没有任何上游声明该标签 → 命中（「除了 X 都走」）；
- `*`：只要有上游 → 命中（兜底 / else 分支）；
- 正标签之间、正与否定之间是 OR；**多个否定项之间是 AND**（`!a`+`!b`）；
- `normalize_condition` 与 `normalize_outcome` 分开：结果标签本身不允许否定或
  通配；非法写法（`!`）在 `validate_plan` 报「非法条件」。

**边界**：仍不是表达式语言——不能写 `a and b`、阈值比较或循环；需要那类语义时
应显式扩枚举（可测、确定性），而不是引入 eval。

**验证**：`tests/test_flow_runtime.py` 新增 5 例（否定命中/未命中、多否定 AND、
正负 OR、兜底、校验接受与拒绝）。

### 3.37 R32：控制台 UI 重构（2026-11-07）

看板此前「一路加出来」：顶部 tab、颜色写死在几十处内联样式、只有深色。

- **设计令牌** `:root` + `:root[data-theme="light"]` 两套（bg/surface/border/text/
  accent/success/danger/warn/radius/mono），页面与 SVG 画布全部引用令牌；
- **明暗主题**：默认跟随 `prefers-color-scheme`，侧栏「明 / 暗」手动切换并写
  `localStorage`；head 里先初始化避免闪色；
- **布局**：左栏（品牌 + 6 导航项）+ 主区，窄屏折叠横向；`.shell/.sidebar/.main`；
- **组件化**：`.toolbar` `.badge` `.card` `.card-head/.card-meta/.card-desc`
  `.empty` `.hint` `.ok-text/.err-text/.warn-text`；动态节点改用类，不再写颜色；
- **可访问性**：`:focus-visible` 描边 + `color-scheme` 让原生控件跟随主题；
- **零接口改动**：所有 id / `switchTab` 名称 / 接口路径不变，功能回归由
  `test_dashboard_ops.py` 10 例继续覆盖。

**验证**：新增 `tests/test_dashboard_ui.py` 6 例（关键 id 齐全、六面板与导航对齐、
两套令牌键集合一致、主题切换接线、令牌表外无写死颜色、侧栏结构）；
`test_dashboard_ops.py` 10 例全过；JS 过 `node --check`；本地 `serve` 实跑核对页面
（HTTP 200、侧栏渲染）。

### 3.38 R33：多级主管链（2026-11-07）

R9 遗留「多级主管链（当前只有一层）」。

- `supervisor_chain: list[str]`（默认空 = 单层，与 R7/R9 逐字一致），env
  （逗号分隔）/ YAML / `POST /runs` options 三通道；
- `SupervisorReviewer` 逐级复核：直接主管 → chain[0] → …，**任一级驳回立即短路**，
  全部放行返回最上层记录；
- 每级留 `supervisor_level`（level / levels / supervisor / verdict）；被跳过的层级
  留 `supervisor_review_skipped`（duplicate_level / self_delegated /
  unknown_supervisor / already_in_g2）——跳过要有据可查；
- `POST /runs` 校验 `supervisor_chain` 是「非空字符串数组」。

**验证**：`tests/test_supervisor_review.py` 新增 7 例（逐级放行与最上层记录、
首级驳回短路、同角色与自身跳过、未知角色跳过、空链单层、env 通道、API 类型校验）。

### 3.39 R34：本机环境修复——崩溃隔离跑器（2026-11-07）

**问题**：本机跑测试出现随机进程级崩溃（`access violation` / `Segmentation fault` /
退出码 139 / 偶发 `MemoryError` 与 `.pyc` 写坏导致的 `SystemError`）。诊断结论：

- 换解释器（Store 版 3.12 → 独立 CPython 3.13）**同样崩**，排除 Store Python；
- 崩溃发生在**收集期**且栈顶是 `Garbage-collecting`，逐文件收集时崩的文件每次不同、
  重跑又全过 → 与代码无关，是进程级随机故障（安全软件 / 沙箱注入的典型表现）；
- 短时间连续起大量 Python 进程更易触发（`test_cli.py` 在 128 进程连跑时连续崩，
  单独跑 3/3 通过）。

**方案** `scripts/test.py`（纯标准库、跨平台）：清 `__pycache__` + 固定
`PYTHONDONTWRITEBYTECODE/PYTHONUTF8` → 按批（默认 4 文件）起子进程 → 崩溃重试（默认
2 次）→ 汇总；新增 `--retry-failed N` 把「首次失败、重试通过」与「真失败」分开打印。
退出码只反映最终结论。

**实测**（默认 `--batch 4`，崩溃自动重试）：129 个文件 / 33 批 / 约 210s，
2632 passed / 0 failed / 1 skipped，1 批靠崩溃重试恢复，**一次命令得到可信
结论**——此前需要人工分 5 批再手工加总。

**验证**：新增 `tests/test_test_runner.py` 11 例（计数解析、环境净化、详情摘取、
文件发现、空输入不乱猜）。

### 3.40 R35：按开源对标方案做安全止血（2026-11-08）

依据 `完善/AgentCrew开源对标调研与完善方案.md`（P0 安全止血 → P1 可信基线）。
本轮只做 P0 与 P1 中「不引重型依赖、可确定性验证」的部分，架构与调度语义不变。

| 项 | 改动 | 解决的问题 |
|---|---|---|
| **P0-1 路径规范化** | 新增 `api/paths.py`；`_handle_connection` 只解码一次，规范路径同时喂给授权中间件与路由分发；四个 handler 不再各自 `unquote` | 授权端用未解码 path 匹配 `_ROUTE_SCOPES`、分发端用解码 path 路由，`POST /%6beys` 被解成 `/keys` → **viewer 能建 admin 密钥**。现在 `%2f`/`%5c`/`%00`/`.`/`..`/多层编码一律 400，同一条路径只有一种解释 |
| **P0-2 SSE 分桶** | `EventBus` 由 `set` 改 `dict[queue, run_id]`，`publish` 按 `data.run_id` 投递，`_handle_sse` 订阅时带上 run_id | 此前全量广播：A run 的 goal / 产物摘要 / 审批内容推到 B run 的连接上（同进程跨 run 泄露） |
| **P0-3 认证默认开** | `api_auth_enabled` 默认 `True`；`serve` 按「有密钥 → 必开 / 无密钥且认证开（默认）→ 退 3（含回环）/ 无密钥且显式关认证 → 仅回环放行并警告，非回环仍退 3」 | 旧默认 `False` = 忘了配就匿名暴露在 0.0.0.0。第一版对回环「警告放行」，被既有用例打回：`AGENTCREW_API_AUTH_ENABLED=1` 无密钥时会把进程挂在启动等待上，而不是退错——改了语义就必须 fail-closed |
| **P1 CLI 引擎** | `create_subprocess_shell` + `format` → `shlex.split` 先切 argv、再**逐 token** 替换占位符 + `create_subprocess_exec` | 去掉 shell 解释层：模板里的 `|`/`&&`/`$(...)` 不再生效，替换值永远只是「一个参数」。按方案把严重度从 P0 降为 P1（模板来自配置文件、prompt 走 stdin，模型注入不成立），但按 P1 一次修到位 |
| **P1 run_python 默认关** | 新增 `code_exec_enabled`（默认 `False`，env `AGENTCREW_CODE_EXEC_ENABLED`）；`Crew._build_tool_registry`、工具实验台、CLI 工具清单三处**同口径** | 它有 `-I`/裁剪环境/cwd/超时隔离，但没有网络与系统调用沙箱（可出网）。默认不注册，打开时边界写进 `OPERATIONS.md §3.3` |
| **P1-11 单点校验** | `_validate_run_options` 抽到 `api/run_options.py`（`ALLOWED_RUN_OPTIONS` + `validate_run_options` + `apply_run_options`），`api` 包导出；server 保留兼容别名 | 白名单/类型校验只有一份，库层调用方也能用同一套规则，不再「HTTP 拒、库放行」 |
| **P1-12 配置期校验** | `max_concurrency`/`context_budget_tokens`/`plan_max_attempts ≥ 1`、`mock_latency_ms ≥ 0`、`review_score_threshold ∈ (0,1]` 走 `field_validator` | 此前 `max_concurrency: 0` 能加载成功，到建信号量时才炸 → 表现为「run 直接 FAILED」，与引擎 bug 长得一样。现在加载即退 3 |
| **P1-2 看板 XSS** | `dashboard.py` 渲染全改 DOM/`textContent`（run 卡片、ledger、日志、统计、任务卡、流程属性面板、SVG marker）；新增 `el()`/`clearNode()`；HTML 响应加 CSP + nosniff + Referrer-Policy | `card.innerHTML = ...${r.goal}...` 让持 key 的调用方能在其他运营者看板里执行脚本。攻击面是「低权限用户 → 运营者」而非匿名反射，但仍是真 XSS |
| **P1 编码** | 非 TTY 的 stdout/stderr 先尝试 `reconfigure(encoding="utf-8")`，失败才回退字符替换 | 重定向到文件时不再把 `✓`/`⟳` 降级成 `?`，日志里的中文与符号完整 |
| **文档** | `OPERATIONS.md`（认证三分支表、`run_python` 边界、路径/SSE 边界、上线清单）、`README.md`（CLI 模板 argv 语义）、`openapi.yaml`（`code_exec_enabled`） | 方案里的「边界必须写清」不能只写在代码注释里 |

**明确未做（按方案 §5.4 与本轮取舍）**：Checkpoint v2 / pending writes、HITL resume、OTel GenAI、
evals、guardrails、原生 function calling、MCP 协议升级、`uv lock`、`THREAT_MODEL.md`——
这些属 P2/P3，需各自立项；`uv`/`mypy` 不在本机环境，未强行引入。

**验证**：新增 `tests/test_security_hardening_2026_11_08.py`（64 例，覆盖编码变体 403、
非法路径 400、SSE 不串台、回环判定矩阵、serve 退 3、CLI argv 注入字面化、`run_python` 默认关、
配置期校验、`options` 单点校验、看板无 `innerHTML` 拼接、CSP 头）；改写 4 个既有断言
（工具数 6→5、`api_auth_enabled` 默认值、`_handle_get` 签名）。`ruff check` 全绿。

### 3.41 P2-1：原生 function calling（2026-11-08）

方案 P2 的第一项。目标不是「换一套协议」，而是**两条协议共用同一个消费端**，
让「原生与回退同任务同结果」这条验收标准在结构上成立。

| 层 | 改动 |
|---|---|
| 数据 | `CompletionResult.tool_calls: list[ToolCallRequest]`（默认空，向后兼容）；`Message` 增加可选 `tool_calls` / `tool_call_id` 与 `role="tool"`（用 `total=False` 基类继承，保持 `role`/`content` 仍必填） |
| Provider | `OpenAICompatProvider.complete` 接受 `tools`/`tool_choice`，非空才写进请求体；解析 `message.tool_calls`（坏 JSON → `arguments_error`，不抛异常）；`_prepare_messages` 透传 tool 字段。Anthropic / CLI / Mock 签名同形、忽略这两个参数 |
| 工厂 | `call(..., tools, tool_choice)` → `_attempt` → `provider.complete`；**带工具时禁用响应缓存**（缓存键不含工具声明，命中会把 ReAct 变成单次调用）；不带工具时**连关键字都不传**，既有实现一行不改 |
| Agent | `_run_agent_loop`：能力位支持时优先 `completion.tool_calls`，为空回退围栏；原生回灌用 `assistant(tool_calls)` + 逐条 `role="tool"(tool_call_id)`，围栏回灌保持 `<tool_result>` 块；`_execute_tool_calls` 改为返回**逐条**结果，两条路径共用执行/校验/权限/留痕/错误回灌 |
| 提示词 | 原生模式给「直接调函数」，围栏模式逐字保留原文；流式（`stream=True`）暂时强制围栏——`stream_call` 还不解析 `delta.tool_calls`，两套协议不能半接 |
| 留痕 | `tool_call` 事件新增 `mode`（native / fence），运维能看出实际走了哪条 |

**开关**：`capabilities.supports_tool_calls`，默认全 `False`（兼容网关不认 `tools`
的不少，默认打开会让本来能跑的部署 400）。YAML 覆写即启用：

```yaml
models:
  providers:
    openai:
      capabilities: {supports_tool_calls: true}
```

**工具链**：`scripts/test.py` 新增 `--fail-on-recovered-crash`（崩溃后重试通过也判
失败）——本机环境噪音下「这一轮确实没一次跑通」不该被当成绿灯；CI/发版前用它。

**验证**：新增 `tests/test_native_tool_calls_2026_11_08.py` 13 例——请求体带/不带
tools 的差异、tool_call 解析（含坏 JSON、数组、缺 name、None）、tool 字段透传、
能力位默认值、**原生 vs 围栏同任务同结果（产物逐字节相同、工具都真的执行、
ledger 除 mode 外一致）**、坏 arguments 回灌不执行工具、提示词按协议切换。
回归 `crew / end_to_end_mock / multi_turn / supervisor / delegation / streaming /
agent_tools / openai_compat / models / factory` 共 273 例全过。

### 3.42 P2-2：super-step 标记 + pending tool writes（2026-11-08）

方案 §4.2 的判断：已有「原子 checkpoint + delta + 节流压缩」走在正确的轻量 durable
路上，缺的只是**pending 状态**——「正在调工具时崩溃」无迹可寻。所以只借
LangGraph super-step 的**语义**，不引 Temporal / 外部 durable 服务。

**问题定位**：`run.json` / `tasks.json` 都是「事件落地后」写。进程被强杀时两种半步
丢失：①「一批任务已派发、还没收尾」（R17 的 `run_reconciled` 兜住了状态，但看不出
派发了谁）；②「工具已开始执行、结果还没回灌」——**完全不可见**，重跑
`write_file` / `run_python` 时没人知道上一次已经动过手。

**做法**（新增 `agentcrew/runtime/superstep.py`）：

| 件 | 作用 |
|---|---|
| `superstep.json` | 每轮调度开始写「第 N 轮 + 在跑哪些任务」，本轮正常结束改 `idle`。原子写（临时文件 + `os.replace`）。看到 `phase=dispatch` 就是「上次停在半路」 |
| `tool_call_started` 事件 | Agent 在工具执行**前**记一条；既有 `tool_call` 作为「完成」标记，两者新增 `call_id` / `fence_index` 关联键 |
| `pending_tool_calls()` | 扫 ledger 推出「有 started 没配对的 finished」= 被中断的调用。原生按 `call_id`、围栏按 `fence_index`、升级前的老事件按工具名兜底 |
| `_recover_superstep()` | 启动/续跑时若有 `dispatch` 或 pending，记 `superstep_recovered`（含 `last_phase` / `running_tasks` / `pending_tool_calls` / `pending_count`）；首次运行与正常收尾**不发事件** |

**为什么不另存一份 pending 状态**：ledger 已经是「写入严（逐行 fsync）、读取宽
（容忍崩断尾行）」的追加日志，再存一份就多了一个会漂移的事实源。**为什么不做结果
重放**：`write_file` / `run_python` 的副作用不可回滚，重放要求工具幂等；宁可如实
标注「这次被中断，任务将重跑」。任务本身仍由既有 `repair_statuses`（RUNNING →
READY）重派 —— 本项补的是「可见」，不是另造一套恢复引擎。

**验证**：新增 `tests/test_superstep_2026_11_08.py` 13 例——标记原子写/自增/坏文件
不炸、`superstep_begin` 留痕、pending 三种配对键（call_id / fence_index / 工具名）、
上限裁剪、首次运行不冒事件、崩在半步上发 `superstep_recovered` 且字段齐全、正常
idle 不发事件、文档事件名同步。回归 `scheduler / scheduler_deadlock / crew /
checkpoint / incremental_checkpoint / resume_whitelist / run_lifecycle(+hardening) /
end_to_end_mock` **175 例**、`ledger(+api) / board_view / webhook / approval_timeout /
agent_tools / native_tool_calls / superstep / security` **200 例**全过。
真实 run 冒烟：5 轮调度落 `superstep.json`（index=5, phase=idle），全新 run 无
`superstep_recovered`，与设计一致。

### 3.43 P2-3：OTel GenAI 语义 + 零依赖 OTLP 导出（2026-11-08）

方案 §4.3 的结论是「借 `gen_ai.*` 语义、先 OTLP 输出、不早期自建重型后端」。
官方 `opentelemetry-sdk` 会拖进 api/sdk/exporter + protobuf 一串依赖，而本仓对
依赖白名单极严（HTTP 服务、SDK、工具链全手写），所以只实现一条够用的通道：
**OTLP/HTTP + JSON 编码**，`urllib` + `json` 就够。

| 件 | 内容 |
|---|---|
| `agentcrew/runtime/otel.py`（新） | `OTLPTracer`：一次 run 一个 `traceId`，缓冲 span，`flush()` 一次性 POST `{endpoint}/v1/traces`；`encode_attribute` 按 OTLP 规则编码（`intValue` 必须是字符串） |
| 语义 | `chat {model}` 带 `gen_ai.operation.name` / `system` / `request.model` / `response.model` / `usage.input_tokens` / `usage.output_tokens` / `request.temperature` / `request.max_tokens` / `response.finish_reasons`；`execute_tool {name}` 带 `gen_ai.tool.name` / `tool.call.id`。**不自造新的 `gen_ai.*` 键** |
| 接线 | Crew 开根 span（run_id/goal/模型/开关）→ 调度器每任务一个 `agentcrew.task` span → 工厂在 **`_account`**（唯一记账口）记 LLM span → Agent 记工具 span |
| 默认 | `otel_enabled=false`、`otel_capture_content=false`：不采集内容，也不在没有 collector 时白发请求 |
| 失败 | 导出在 `asyncio.to_thread` 里做（3s 超时），失败只 `logger.warning`；run 的退出码/产物不受影响 |

**踩到的坑（已修）**：第一版把 LLM span 记在 `call()` 里，冒烟一看 12 次调用只出
5 个 span——plan / review 走 `complete_structured` 的阶梯，压根不经过 `call()`。
改到 `_account`（docstring 明写「所有调用路径共用」）后 12/12 对齐，`temperature` /
`max_tokens` 也从 `_attempt` / `_structured_attempt` 透传进去。

**验证**：新增 `tests/test_otel_2026_11_08.py` 18 例——resource/span 结构、一 run 一
trace 与 parent 链、属性编码、丢 span 计数、`gen_ai.*` 齐全、**默认不含内容 / 打开后
含且截断**、工具 span 的 error 状态、异常进 span 标 error 并重抛、真起本地端点收
POST（路径 `/v1/traces` + body 可解析 + 缓冲清空）、端点不可达返回 `ok=false` 不抛、
配置默认关与 env 开关、`Crew._build_tracer` 开关。端到端：一次 mock run 导出
**1 条 trace / 18 span（1 run + 5 task + 12 chat）**，与运行报告的 12 次 LLM 调用
完全对齐。

### 3.44 P2-4：工具级 guardrails —— 副作用前拦截（2026-11-08）

本项是 P2 里唯一**直接减风险**（而不是加能力）的一条。做之前先把「哪些判断可信」
想清楚：只有能确定性判断（正则 / 长度 / 字段位置）的规则才放进来——**不用 LLM 判**，
用模型判断模型既不稳定又贵，失败模式恰好是「该拦的没拦」。

| 件 | 内容 |
|---|---|
| `agentcrew/tools/guard.py`（新） | `ToolGuard` + `GuardHit`：`check_input(tool, args)` / `check_output(tool, text)`，规则按 `tools` / `field` / `phase` 过滤，命中返回 `block` 或 `warn` |
| 三态 | `block`（拒绝执行 / 拒绝回灌）/ `warn`（照常执行但留痕，灰度用）/ `allow`（显式放行，把「我们知道这个模式」写进配置供评审） |
| 两相位 | `input` = 工具执行**前**（拦副作用）；`output` = 结果回灌**前**（拦泄漏）。前者才是安全边界，后者防「把 .env 读进上下文再发给模型」 |
| 执行链 | `BaseAgent._execute_tool_calls`：schema 校验之后、`tool.run()` 之前查 input；`tool.run()` 之后、回灌之前查 output。`block` 的工具**一次都不执行**，原因与规则名一起回灌让模型自己改 |
| 配置 | `Settings.tool_guards`（YAML `tool_guards:` 段）+ 加载期校验：缺 name / phase 拼错 / action 拼错 / 正则不合法 / 无 `pattern` 且无 `max_length` 全部退 3 |
| 留痕 | 每次命中记 `tool_guard_triggered`（rule / phase / action / field / reason）；规则自身抛异常记 `tool_guard_error` 并按**放行**处理（安全组件不许把正常调用变成失败） |

**验证**：新增 `tests/test_tool_guard_2026_11_08.py` 20 例。最关键的一条是
`test_block_prevents_tool_execution`：用带调用计数的工具断言 **`hits == 0`**——
「文本说被拦了、其实已经写了文件」是这类功能最容易出现的假绿。其余覆盖 warn
照常执行、allow 显式放行、phase 隔离、`field` 定位、`max_length`、输出相位拦泄漏
（断言 `sk-live-SECRET` 不在回灌文本里）、无规则时零行为变化、7 种非法配置。
回归 `agent_tools / native_tool_calls / tool_wiring / tool_permissions / tool_lab /
config / crew` **141 例**全过。

### 3.45 P2-5：evals —— 离线确定性回归 + pass^k（2026-11-08）

前四项都在改**行为面**。「测试还是绿的」不够：单测钉的是函数，不是「一次 run 出来
的东西还对不对」。evals 补的就是这一层——固定输入跑完整 run，断言**产物内容**，
把「改了提示词 / 换了模型 / 调了上限，效果变好还是变坏」变成可复现的数字。

| 件 | 内容 |
|---|---|
| `agentcrew/evals/case.py` | `EvalCase`（id / sample / goal / checks / options）、`CaseOutput`（产物全文 + 任务终态 + 事件计数）、JSONL 加载器（坏 JSON 带行号、id 去重、必须给 sample 或 goal） |
| `agentcrew/evals/checks.py` | 11 种确定性断言：产物存在/含/不含/正则/行数区间/数量、任务终态、事件次数、run 终态、无失败任务。未知 kind **直接判失败并列出可用类型**（否则「全绿」是假的） |
| `agentcrew/evals/runner.py` | 每条 case 一次独立 `Crew.run`（自己的临时 output 目录，避免产物互相污染）；`EvalReport` 给出 `pass@1` 与 `pass^k`（k 次全过才算过） |
| CLI | `python main.py eval <dataset> --repeat K [--json] [--output-dir DIR]`，退出码与测试同口径（全过 0 / 有失败 1 / 数据集非法 3） |
| 数据集 | `examples/evals/smoke.jsonl` 4 条 case，覆盖三个样例（产物齐全 / 无模板残留 / 并行汇合 / 事件数下限） |

三条设计取舍：**离线确定性优先**（case 绑 mock 夹具，不碰网络与 token，CI 能天天跑）；
**不用 LLM 判**（判不动的写成人看，不伪装成自动化）；**指标不自欺**（`pass^k` 与
`pass@1` 并列报，波动 case 在 `pass^k` 上会被明确扣分）。

**踩到的坑**：数据集第一版写成「带缩进的多行 JSON」，加载器直接报错——JSONL 定义
就是一行一条。这一条也顺手写进了测试（`test_dataset_reports_line_number_for_broken_json`），
因为手写数据集最常见的错误就是这个。

**验证**：新增 `tests/test_evals_2026_11_08.py` 40 例——数据集加载（坏 JSON 行号、
重复 id、空文件、id 含分隔符）、22 条断言矩阵、未知 kind 报错、运行失败时全部判失败
（防「artifact_not_contains 恰好通过」的假绿）、`pass^k` 的波动扣分数学、端到端跑真
run（正例 + **故意写错断言必须红** + 样例名不存在时不崩）、`--output-dir` 留账、
报告可 JSON 序列化。CLI 实测：`smoke.jsonl` 4 条 case `pass@1 = 100%`，`--repeat 2`
时 `pass^2 = 100%`。

### 3.46 P2-6：真实流式 + 流式工具调用（2026-11-08）

补的是 P2-1 留下的那半句：「流式暂时强制走围栏」。做完这条，流式与原生工具调用
不再是互斥的两条路。

| 件 | 内容 |
|---|---|
| `StreamChunk` | 新增 `tool_call_deltas: list[ToolCallDelta]`（`index` / `id` / `name` / 半截 `arguments`） |
| `assemble_tool_calls()` | 按 `index` 合并分片、`arguments` 字符串相加后**一次性**解析；坏 JSON 留 `arguments_error` 不抛；乱序到达也正确（结果按 index 排序） |
| `OpenAICompatProvider.stream_complete` | 真 SSE：`stream=True` + 逐帧解析 `delta.content` / `delta.tool_calls`；**建流在重试引擎内、消费不重试**（已吐块无法回滚）；无 delta 的用量/keepalive 帧不产出空块 |
| 工厂 `stream_call` | 新增 `tools` / `tool_choice` 透传（不带工具时仍一个关键字都不传） |
| Agent | `_invoke_model` 流式分支收集工具增量 → `assemble_tool_calls` → `CompletionResult.tool_calls`；`_native_tools_enabled` **去掉「流式降级」的闸门** |

为什么「只在流结束后解析一次」：`arguments` 是 JSON 文本的片段，一次切分可能落在
字符串字面量中间（`"写了一半的内容"` 就是被切开的那类）。逐片 `json.loads` 必然
在片边界失败，而失败后重试会把「模型写对了」变成「解析不出来」。

**验证**：新增 `tests/test_streaming_tool_calls_2026_11_08.py` 14 例——分片拼装
（含「切开字符串字面量」的真实边界）、并行调用按 index 分流、乱序到达、坏 JSON /
缺 name / 空输入、`parse_tool_call_deltas` 对 None 与 dict 形状的容错、Provider
真的把 `stream=True`+`tools` 发出去、文本与工具增量按序产出、空帧被跳过、Agent
流式路径的 `mode=native` 与工具真执行、无工具增量时仍是纯文本、无工具时不传
`tools`。回归 `streaming / openai_compat / agent_tools / native_tool_calls /
model_factory / models` **156 例**、`crew / e2e / superstep / evals` **116 例**全过。

### 3.47 P2-7：HITL 审批的幂等与恢复语义（2026-11-08）

方案 P2 的「HITL `request → pending → resume(checkpoint, response)` 幂等、部分审批、
进程重启后仍可审批恢复」——机制本身 R4（持久化审批单）+ R30（等待窗口）已经给了，
缺的是**把语义钉住**：此前只有一条 CLI 往返测试间接覆盖，重复裁决 / 过期裁决 /
重启后仍在 pending 这三件事没有断言。

三个必须成立的点（也是本轮的 8 条用例）：

| 语义 | 断言 |
|---|---|
| 重复裁决被拒、**第一份裁决不被覆盖** | 第二次 `decide` 抛 `ApprovalError("已 decided，不能重复裁决")`，文件里 `decision`/`decided_by` 仍是第一个人写的 |
| 已消费的单不能再裁决 | `consume()` 之后 `decide` 抛「已 consumed」——否则 resume 会把同一张单执行两次 |
| 过期后裁决被拒且提示可操作 | 错误里带 `approval_timeout_seconds`，不是一句「已 expired」 |
| 重启后未裁决的单仍是 pending | 新建 `ApprovalStore` 实例读到同一张单与同一份 `context` |
| 追加日志被截断不丢历史 | 手工写入半行后，前面已落盘的审批单仍可读（ledger 同款「写入严、读取宽」） |

**为什么这条重要**：审批是唯一「人说了算」的地方。两个人同时一个点批准、一个点
驳回时，如果结果是「谁后写文件谁赢」，那这套审批等于没有——所以拒绝重复裁决比
接受覆盖更重要，哪怕这会让人多操作一步。

**验证**：新增 `tests/test_hitl_2026_11_08.py` 8 例；回归 `approval_interfaces /
approval_timeout / resume_whitelist` 共 **38 例**全过。

### 3.48 P3-1：`THREAT_MODEL.md` —— 把散落的安全结论收成可评审的威胁模型（2026-11-08）

P0–P2 补了十几处安全边界（路径规范化、认证默认开、SSE 分桶、guardrails、
`run_python` 默认关、看板 CSP…），但结论分散在三处：代码注释、`OPERATIONS` 的边界表、
测试文件。三处都不回答评审会问的那个问题——**「所以你们到底防不住什么？」**

新增 `docs/THREAT_MODEL.md`（310 行），结构按「能被质询」来设计：

| 节 | 内容 | 为什么这么写 |
|---|---|---|
| §2 系统模型 | 组件数据流图 + **5 条信任边界**（B1 网络→进程 / B2 进程→LLM / B3 进程→工具 / **B4 模型输出→进程行为** / B5 配置→执行）+ 不可信输入清单 | B4 是本类系统特有边界；不单独列它，威胁模型就退化成普通 Web 服务的安全文档 |
| §3 资产 | 按「丢了最疼」排序，含落盘目录与敏感度 | 判断影响面时先看丢了什么 |
| §4 行为者 | A1 无凭证者 / A2 低权限 key / **A3 能影响模型输入的人** / A4 同机用户 / A5 供应链 / A6 运维误操作 | 每类标「是否覆盖」，A3 诚实标「部分覆盖」 |
| §5 威胁 | **46 条**（T-01…T-46；T-46 由 P3-2 的 MCP 流式传输增补），八域：认证授权 / 请求解析 / 跨 run 隔离 / 提示词注入 / 工具执行 / 秘密泄露 / 供应链 / 可用性 | 每条四列：场景、当前控制、**残留风险**。有残留的一律标 ⚠️ 并指向缺口编号 |
| §6 缺口 | **15 条**（G-1…G-15），含「影响 / 为什么接受 / 补偿或触发条件」 | 上线签字的正题：G-1 提示词注入无法根除、G-2 配置即 RCE、G-9 `run_python` 无隔离，都必须由部署方明确接受 |
| §7 证据映射 | 12 个域 → 测试文件 → 关键断言 | 「控制」不是声明而是断言，指不到测试的视为无效 |
| §8 上线清单 | 10 项，与 `OPERATIONS §8` 互为正反面 | 命令在运维手册，判断在这里 |
| §9 维护规则 | 5 条「什么时候必须更新」 | 防文档腐化 |

**顺带修掉的三处文档事实错误**（写的时候逐个回代码核对才发现）：知识库目录名不是
`_kb/` 而是 `knowledge/`（`experiences.jsonl` + `entities.jsonl`）；成本上限不是配置项而是
`AgentProfile` 的 ctor 参数（`cost_ceiling_usd` / `run_cost_ceiling_usd` /
`max_calls_per_task` / `max_rpm`，默认 `None`）；`web_fetch` 的测试在
`tests/test_new_builtin_tools.py` 而不是某个不存在的 `test_web_fetch.py`。

**给文档本身加了测试**（`tests/test_threat_model_2026_11_08.py` 8 例），因为威胁模型最
容易烂掉的方式不是写错，而是**慢慢与现实脱节**：引用的测试文件被删、缺口编号改了一半、
上线清单悄悄少一条。断言四条：必需章节齐全、**正文点名的每个 `tests/test_*.py` 真实
存在**、`T-xx` 连续无空洞且 `G-x` 引用都能落到缺口表、上线清单仍点名 G-1/G-2/G-9、
文档写死的安全默认值与 `Settings.model_validate({})` 一致（用 model_validate 而非
`load()`，否则断言变成环境测试）。

**验证**：`tests/test_threat_model_2026_11_08.py` 8 例全过；`ruff check` 全绿。
`README` 与 `OPERATIONS §0/§3.3` 已加入指向本文档的入口。

### 3.49 P3-2：MCP streamable HTTP（会话 / 协议版本 / SSE 响应 / 会话重建）（2026-11-08）

P3-9 的 `HTTPTransport` 是「POST 一次拿一个 JSON」。对照官方 streamable HTTP，缺四件事，
**每一件在真实服务端上都会直接失败**——这也是威胁模型 G-15 原来记的那个缺口。

| 语义 | 之前 | 现在 |
|---|---|---|
| 会话 | 不认 `Mcp-Session-Id` | 从 `initialize` 响应头记下，之后每个请求回带 |
| 协议版本 | 不发送 | 握手**之后**的请求带 `MCP-Protocol-Version`，值取服务端协商的 `protocolVersion`（默认 `2025-06-18`，**不硬编码**） |
| SSE 响应 | 只按 JSON 解析，遇到 `text/event-stream` 会挂到超时 | 按 `Transfer-Encoding` 解块 → 跨块缓冲成行 → 只取 `data:`、跳过注释 → 拿到匹配 `id` 即停；坏 JSON 跳过不中断流 |
| 会话失效 | 只报 `HTTP 404` | 抛 `MCPSessionExpired`，`MCPClient` 重握手**一次**后重试；握手期的 `notifications/initialized` 同样适用，全局只给一次额度 |

**给自己踩出来的一个坑**：第一版只在 `_request` 里处理会话失效，测试立刻打回——
`notifications/initialized` 走的是 `_notify`，它把 `MCPSessionExpired` 包成了普通
`MCPError`，于是「会话刚建好就被丢掉」变成整次 `start()` 失败。现在 `_notify` 原样
上抛该异常，`_initialize` 用一次性额度重建。**另一个更隐蔽的问题**：响应是靠 id 匹配
结束的，服务端只要一直推别人的通知就永远不会结束——原实现会永久阻塞在读流上，表现为
「run 卡死且不报错」。补了 `MAX_RESPONSE_BYTES`（8 MiB）与 `MAX_SSE_EVENTS`（1000）
双上限，撞线即抛错。

**顺带修掉的一个真 bug**：重写 body 读取时先写成「读到连接关闭」，对 keep-alive 的
服务端会挂住——`Content-Length` 分支被补回，并有一条用例专门盯它。

**验证**：新增 `tests/test_mcp_streamable_http_2026_11_08.py` 15 例——会话捕获与回带、
协商版本优先于本地默认（假服务端故意返回更旧的 `2025-03-26`）、自定义头透传、SSE 先
通知后响应、chunk 边界切在 JSON 中间仍能重组、坏 JSON 跳过、404 只重建一次且重试用新
会话、握手都握不上时报错不重试、重试又 404 时如实报错、通知 202 不阻塞、`https` 拒绝
且提示反代、`Content-Length` 不等关闭、无限 SSE 撞事件上限、超大 body 拒绝。回归
`mcp_transports / mcp_client / mcp_wiring` 共 **51 例**全过。

### 3.50 R37：真实 provider 端到端验证 + 计划校验误伤修复（2026-11-08）

此前全部验证都跑在 mock 夹具上。本轮第一次用**真实 DeepSeek**（`.env` 已配、`doctor`
healthy）做三档实跑，暴露并修掉一个会拒绝合法计划的误伤。

**发现的 bug**：计划校验第 8 条（「要求文档的验收标准必须有文档类交付物」）用裸子串
匹配关键词，把动词「说明」当成名词「文档」——PM 写「add 函数带有**说明参数与返回值**
的 docstring」，被判定为「要求一份 markdown」，而该任务只有 `.py` 交付物 → 合法计划
被判退出码 2；重拆几次措辞仍类似，最终整条 run 死掉。根因与历史两次修正同源：这条
检查是针对真实死锁类（「提供启动命令与依赖说明」却只有代码交付物 → G1/G2 双向堵死）
的定向 lint，但关键词是启发式。

**修复**（`agentcrew/task/planner.py`）：新增 `_demands_document()`——`说明` 后接宾语
（参数/返回值/用法/字段…）时判为动词、跳过；标准里出现 `docstring`/`注释` 时跳过
（代码内说明由代码本身满足）。名词用法（「启动说明」「使用说明」，说明在句尾）照旧命中，
原死锁防护不丢。回归 `tests/test_planner.py` +3（docstring 标准不误伤、说明动词不误伤、
名词用法仍拦）。

**三档实跑**（`--max-cost`/`--max-llm-calls` 兜底，产物在仓库外、已清理）：

| 档 | 结果 |
|---|---|
| 基线（无工具） | 3 任务 / 10 调用 / $0.01 / 36s；产物 `slugify.py` 自带 pytest **18 例全过**，交互边界（空串/纯中文/多余空白）正确 |
| 工具链（fence） | 5 任务 / 16 调用 / $0.03；ledger 7 次 `tool_call(mode=fence)` 全成功 |
| **原生 function calling** | 5 任务 / 30 调用 / $0.04；ledger **60 次 `mode=native`、带真实 `call_id`**——`assistant(tool_calls)` + `role="tool"(tool_call_id)` 回传被 DeepSeek 接受（此前只有单测覆盖，从未对真端点验证） |
| **流式** | 4 任务 / 11 调用 / $0.01；ledger **3727 个 `agent_stream_chunk`**，产物 `greet('World') → Hello, World!`。此前 `BaseAgent(stream=True)` 只有单测在传，生产无入口（见 §3.51） |

### 3.51 R38：流式补成真配置 + 不可信输入预设 + 计划校验再收窄（2026-11-08）

三件事，前两件是上一轮「下一步建议」的 ①②，第三件是实跑又撞出来的。

**1) 流式从「只有单测传参」补成「可从配置打开」**：查证发现 ``BaseAgent(stream=True)``
在**生产 run 路径没有任何入口**——`AgentFactory` 不接 `stream`，等于一个「测过但从未
跑过」的能力。补了 `Settings.stream`（env `AGENTCREW_STREAM` / YAML `stream`），沿
`Crew → AgentFactory → BaseAgent` 一路下发。`tests/test_stream_config_2026_11_08.py`
5 例。实跑：4 任务 / 3727 chunk / 产物正确。**附带发现**：流式每块都落一条 ledger
事件（逐行 fsync），一个 4 任务的小 run 要 1m29s——实时性有了、审计体积也上去了，这
正是「默认关」的理由。

**2) 不可信输入预设（G-1 的场景化收口）**：`Settings.untrusted_inputs`（env
`AGENTCREW_UNTRUSTED_INPUTS` / YAML `untrusted_inputs`）。打开后 run 内工具表**只留
只读工具**（read_file / list_files / grep），`write_file` / `run_python` / `web_fetch`
一律不下发（`code_exec_enabled` 也压不住），`mcp_servers` 全跳过（记
`mcp_skipped_untrusted`）。处理不可信第三方文档时，「能读不能写」是提示词注入面前唯一
可靠的边界。`tests/test_tool_production_wiring.py` +3。威胁模型 G-1 补偿措施、OPERATIONS
§3.3 边界表、README env 表同步。

**3) 计划校验第 8 条又撞出一种误伤**（第二次，同为「说明」歧义）：这次是
「greet('World') 返回**需求说明中约定**的问候语」「按需求说明约定处理」——「说明」是
名词「需求规格」，指目标里已有的需求文本，不是要产出一份文档。第一次修的是动词用法
（说明参数），这次是「引用既有上下文」。判据升级为：只有「**产出动词**（提供/编写/给出/
输出…）+ 说明」或「**句尾的说明**」才算要求文档；「需求说明 / 规格说明」或「说明中/里
约定」都判引用、跳过。`tests/test_planner.py` +2（需求说明引用、产出动词仍拦）。

**验证**：`test_stream_config` 5 例、`test_tool_production_wiring` 22 例（+3）、
`test_planner` 55 例（+2）全过；`ruff` 全绿。实跑：流式（见上表）全绿。

### 3.52 R39：MCP `outputSchema`/`structuredContent` + ADR（2026-11-08）

按方案收尾两条 P2-11/P3 的明确缺口。

**1) MCP 结构化输出兼容**（方案 P2-11 的「补 `outputSchema`/`structuredContent`」）：
此前 `MCPToolResult.text` 只拼 `content` 里的 `type=text` 块——MCP 2025-06-18 起工具
可以**只回 `structuredContent`**，那种结果会被整个丢掉，模型看到空串。现在：
`MCPTool` 增加 `outputSchema` 字段；`MCPToolResult` 增加 `structuredContent` 字段，
`text` 的优先级为「text 块 → structuredContent 序列化 JSON → 非文本块类型提示 → 空串」。
`tests/test_mcp_client.py` +4。协议版本仍用协商制（默认 `2025-06-18`，服务端可协商成
更新版本），不硬编码方案里提到的版本号——版本是协商值不是常量。

**2) ADR（方案 P3「THREAT_MODEL + ADR」的后半）**：新增 `docs/adr/`（README 索引 +
7 条），把本轮有取舍的关键决策落成记录：认证 fail-closed、`run_python` 默认关、
原生 FC 与围栏共用消费端、OTel 零依赖 OTLP、guardrails 不用 LLM 判、evals 不用 LLM
judge、MCP streamable HTTP 仅 http。每条四段（状态/背景/决策/后果），并写明「何时重审」。
`README` / `OPERATIONS §0` / `THREAT_MODEL` 头部加了指向 ADR 的入口。

**验证**：`test_mcp_client / mcp_transports / mcp_streamable_http / mcp_wiring`
**55 例**全过；`ruff` 全绿。

### 3.53 R40：流式增量正文上 SSE（P3-3 → UI 收尾）（2026-11-08）

方案「真实流式（Provider SSE → Settings → UI）」的最后一段。此前流式只把
``agent_stream_chunk``（``delta_length``）写进 ledger——正文不落审计（避免膨胀），但
看板也看不到实时正文。

| 层 | 改动 |
|---|---|
| Agent | `BaseAgent(stream_sink=…)` 新增出口 `(task_id, role, delta)`；流式循环里每个非空增量块在记完 ledger 后回调它 |
| Crew | `_make_stream_sink(run_id)` 造闭包 → `on_event("agent_stream_delta", {run_id/task_id/role/delta})`；**刻意不走 `_dispatch_event`**（正文只上 SSE，不落 ledger、不触发 Webhook——几千个块不该打爆审计与接收端） |
| 工厂 | `AgentFactory` 透传 `stream_sink`（与 `stream` 同路） |
| 看板 | 看板「看板」页加「流式输出」面板：`agent_stream_delta` 事件按 task 分段累积，`textContent` 追加（XSS 安全），连新 run 时清空 |

**验收**（方案的「`curl -N` 看到增量」）：新增 `tests/test_stream_delta_2026_11_08.py`
5 例——Agent 逐块 sink、Crew 包成 `agent_stream_delta` 推给 on_event、无 on_event 时
no-op、看板有面板与处理器、**端到端**（mock+stream 跑完整 run，`on_event` 收到带正文
的 `agent_stream_delta`，拼接 >20 字符且 run_id 一致）。回归 `streaming / dashboard /
api_server / crew` **124 例**全过；`ruff` 全绿。

### 3.54 R41：覆盖率基线 + 门槛（P1 收尾）（2026-11-08）

方案 P1 里「覆盖率基线 + 门槛」的最后一项（之前卡在「本机无 coverage」）。装了
`pytest-cov` 后落地：

| 件 | 内容 |
|---|---|
| 配置 | `pyproject.toml` 加 `[tool.coverage.run] source=["agentcrew"]` 与 `[tool.coverage.report]`（precision、exclude_lines：pragma/TypeChecking/NotImplemented/abstractmethod/overload） |
| 跑器 | `scripts/test.py --coverage`：每批改由 `coverage run --parallel-mode` 启动，最后 `coverage combine --keep` + `coverage report`；`--cov-fail-under N` 设门槛；`.coverage*` 进 gitignore |
| 基线 | **58.6%**（14675 语句，分批隔离采得——单进程全量会 segfault，本机已知噪音） |

**过程中又抓到并修掉两个真问题**：

1. **R36 认证 fail-closed 打破了 `tests/test_sdk.py`**（我的分批回归一直没覆盖到它）：
   它的端到端用例起 `main.py serve` 不带 key，现在 serve 直接退 3 → 「serve 未就绪」。
   用例本意是验 SDK 装配、不是验认证，已给 serve 加 `--api-key` 且客户端带同 key。
2. **`coverage combine` 默认合并后删源文件**，触发了沙箱「批量删除确认」守卫 → 改成
   `--keep`；**`coverage report` 在 CPython 3.13 下偶发 `SystemError: Unmatched paren
   in format`**（tokenize 抖动，退出码非零但 TOTAL 行可能已打印）→ 改为解析 TOTAL 行、
   拿不到重试，不以退出码为准。

**验证**：`_parse_coverage_total` 单测（`test_test_runner.py` +1，共 12 例）；
`test_sdk.py` 34 例全过；`scripts/test.py --coverage -k test_stream_delta` 全流程
（combine → report → 阈值判定）通过。`ruff` 全绿。

### 3.55 R42：uv lock + Windows CI（P1「环境重建 + uv lock + windows CI」收尾）（2026-11-09）

方案 P1 的最后一组。此前卡在「本机无 uv」，现在 `pip install uv` 后落地：

| 件 | 内容 |
|---|---|
| 依赖声明 | `pyproject.toml` 补 `[project].dependencies`（6 个运行时）与 `[dependency-groups].dev`（pytest / pytest-asyncio / pytest-cov / ruff==0.17.3 / mypy==2.4.1）；与 `requirements.txt` 保持同一份清单 |
| 锁文件 | `uv lock` 生成 `uv.lock`（46 包，1548 行），`uv lock --check` 通过 |
| Windows CI | `ci.yml` 加 `windows` job：`astral-sh/setup-uv` + `uv lock --check` + `uv sync --all-groups` + `uv run ruff` + `uv run pytest`（Python 3.12）。与既有 ubuntu 的「单进程 pytest -q」互补：本机用分批跑器，CI 用单进程 |
| 一致性测试 | `tests/test_config.py` +2：`test_runtime_deps_match_between_pyproject_and_requirements`（两处声明不一致 CI 必红）、`test_uv_lock_exists`（锁文件已提交） |

**为什么 CI 里 Windows 用 uv、ubuntu 仍用 pip**：ubuntu 那条要的是「requirements.txt
路径 + 多 Python 版本矩阵」；Windows 这条要的是「锁文件可复现 + 本项目实际运行的平台
单进程也能绿」。两条各验一条依赖路径，改依赖时任何一处不一致都会红。

**验证**：`uv lock --check` 通过；`test_config.py` 32 例（+2）；`ci.yml` YAML 解析通过。
`ruff` 全绿。`uv` / `uv.lock` 均已纳入，未在 CI 实跑（本地无 GitHub Actions runner）。

### 3.56 R43：前端交互对齐 WorkBuddy（对话流 / 结果区 / 任务列表）（2026-11-09）

调研 WorkBuddy 四区域布局（顶栏 / 侧边栏任务列表 / 对话区 / 右侧结果区）后，把三处核心交互差异落到 Studio，并加一处后端联动：

| 件 | 内容 |
|---|---|
| Ledger observer（后端） | `runtime/ledger.py::Ledger` 加可选 `observer` 回调（`emit` 落盘后同步推「事件名 + 脱敏 data」）；`crew/crew.py::_make_ledger_observer` 注入 `run_id` 后推 `on_event`（SSE），`_LEDGER_SSE_EXCLUDED` 排除已走 `_dispatch_event`/`_notify` 的事件与高频 `agent_stream_chunk`。明细事件不再靠 500ms 快照轮询 |
| 对话式执行流 | Studio 新增「对话」页（侧边栏首位、数字键 1）：`buildMessages()` 把 ledger 事件聚合为消息流（user 目标 / system 规划 / agent 任务气泡 + 折叠的工具调用·评审·产物 + SSE 流式正文） |
| 右侧结果区 | 顶栏「结果」按钮打开「概览 + 产物」双 tab 面板（复用 `#detail-panel`）：概览 = 统计/任务状态分布/门禁，产物 = 文件清单 + 内联预览 |
| 侧边栏任务列表 | `shell.js` 增加搜索 + 状态筛选 chips（5 组）+ 按角色分组 |
| 细节打磨 | 「回到最新」按钮、进入动效（≤200ms）、长文本折叠、全局焦点环 |

**验证**：后端 `scripts/test.py` **2887 passed / 0 failed**（新增 `test_ledger.py` +4、`test_stream_delta_2026_11_08.py` +1）；前端 `npm run check` 69 JS / 42 CSS、`e2e` 32/32、`selftest` 16/16、`npm run visual` 20 张一致。

**方案细则与遗漏**：`docs/WorkBuddy前端交互-实施方案细则.md`（五环节 × 步骤/输入输出/边界/验收 + 13 项遗漏清单，其中「流式正文不回溯」「markdown 不渲染」需单独设计）。

### 3.57 R44：多执行体「可运营性」补全 —— 账算得清 / 复核闭环 / 计划可查（2026-11-09）

对「不同 agent 分别调用不同模型、协同完成一件共同任务」这条主线做定点审查后（报告见
`docs/AUDIT-2026-11-09.md`），结论是**不重构**：C1–C8 八层能力里只有三处短板，全部
以「新增字段 + 一个分支 + 一个只读端点」补齐，未改任何既有函数签名与事件形状。

| 件 | 内容 |
|---|---|
| 执行体归因（C6） | `TraceContext` 增 `profile`；`BaseAgent.execute` 写入 `agent_key`（未绑定时落 `default`）；`_trace_fields()` 把它补进 `llm_call`；`event_stats()` 增 `llm.by_profile`（无 `profile` 的历史事件与非执行类调用归 `unknown`，**不丢弃**，保证分项和 = 总量）。**修掉一处实测缺陷**：`TeamAgent` 的复核/裁判直连模型、不经 `BaseAgent`，此前既无追踪也不进 `agent_budget`，导致 `run.json` 的 `agent_costs` **漏记复核花费**（实测 15 次调用只认 2 次）。新增 `_call_as()` 同时绑定追踪与预算，两本账合一 |
| 复核闭环（C4） | `Aggregation.review_rounds`（默认 `0`，上限 3）。`0` 保持旧行为；`1..3` 时复核报出问题就把意见作为 `fix_hints` 重跑主笔再复核，落 `agent_revise` 事件。「未发现问题」与空回复不触发，避免白跑 |
| 计划可查（C7） | `GET /runs/{run_id}/agents`：取 ledger 最后一条 `run_plan_resolved`，返回 alias / duty / tools / path_scope；无该事件的历史 run 返回 `source: none` + 空数组，**不拿配置猜测冒充运行事实**。契约同步进 `openapi.yaml` |

**为什么这三处是短板**：其余能力（执行体建模、1:N 绑定、四种汇总模式、四级别名解析、
执行体级预算/并发/RPM、路径域写保护、启动期 fail fast）都已在位；缺的是「便宜档到底省没省」
（账算不清）、「复核意见有没有改变产出」（不闭环）、「这次到底哪个 agent 用了哪个模型」
（不可查）。三者都属运营面，不补则多执行体只能"配得出来"、不能"管得起来"。

**验证**：`test_agent_binding.py` 30 passed（+4：修订回灌、默认不回灌、无问题不重跑、轮数上界）；
`test_ledger_api.py` 13 passed（+4：by_profile 归因、计划读端点 ×3）；`test_hardening_2026_11.py`
13 passed（+1：`llm_call` 带 profile）；`test_api_contract.py` + `test_api_server.py` 64 passed
（路由清单 ↔ openapi 双向锁定）。`ruff` 改动文件全绿。

**端到端**（`linear` 样例 + `be_primary`/`be_reviewer` 双执行体 + `review_rounds=1`，mock 通道）：
任务 5/5 成功，报告出现 `执行体 be_primary 0.00/2次 · be_reviewer 0.00/2次`（修复前只有前者），
`llm.by_profile` 分项 2+2+4+7 = 15 = `llm.calls`，账平。

**环境注记**：本机全量跑仍偶发进程级段错误（`enum.py` 编译期 SyntaxError、`Segmentation fault`
退出码 139、`sitecustomize` 注入期崩溃），同一文件连跑三次「过、过、崩」——与代码无关，
正是 `scripts/test.py` 文档里记录的老问题；`scripts/test.py` 自身的 `__pycache__` 清理会被
安全策略拦截，故本轮改用「小批直跑 + 崩溃重试」取证。

### 3.58 R45：改善方案 P0+P1 落地 —— 复核治理 / 门禁归因 / 执行体熔断 / 成本感知路由（2026-11-10）

按 `docs/IMPROVEMENT-PLAN-2026-11-09.md` 执行，四项全部**默认关闭**、无数据迁移。

| 件 | 内容 |
|---|---|
| 裁决契约统一（P0） | 新增 `gate/verdict.py`：`ReviewVerdict` / `CriterionJudgement` / `derive_review` / `is_malformed`，G2 与角色内复核共用。`gate/reviewer.py` 改为导入（行为不变），测试一行未改 |
| 复核接治理通道（P0） | `TeamAgent._review_once` 改走 `ModelFactory.complete_structured`（受信号量 + 记账），不再自由文本 + `"未发现问题"` 子串匹配；`_collect_reviews` 只认派生结论；格式错误记 `agent_degraded` 且**不触发返工** |
| 复核的 mock 回放 | 直连模型时显式 `set_purpose(..., "review", task_id=...)`，`reviews.json` 夹具才真正生效（此前离线跑一直拿不到夹具） |
| 修订记录不丢 | `review_rounds>0` 时跨轮**累加**意见并标注轮次；此前第二轮会覆盖第一轮，"改了什么"在报告里消失 |
| 门禁评审归因（P1.3） | `Reviewer` 评审调用挂 `TraceContext(profile="gate:<角色>")`。实测 `unknown` 7→1，`by_profile` 出现 `gate:architect` 等桶 |
| 执行体级熔断（P1.1） | `agents/team.py::_Circuit`：按"连续失败"计数（成功即清零），达阈值后 `_primary_pool` 剔除该执行体并落 `agent_circuit_open`；全熔断时退回原池子让失败如实发生。开关 `agent_circuit_breaker_enabled`（默认 false）/ `..._threshold`（3） |
| 成本感知路由（P1.2） | `budget.under_headroom()` 纯函数 + `ModelFactory._maybe_shift_by_headroom()`：执行体剩余额度 < `cost_aware_headroom`（默认 0.3）时本次调用改走更便宜备别名，落 `routing_budget_shift`（每个执行体只报一次）。无状态、不碰全局降级标志。开关 `cost_aware_routing`（默认 false） |

**为什么熔断按"连续"而不是"累计"**：偶尔被长上下文噎住不该被判死，成功一次即恢复；
provider 级冷却（`models/health.py`）管的是模型不可用，管不了"这个执行体本身有问题"
（工具权限配错、路径域写不进去、输出协议总不合）。

**验证**：`test_agent_binding.py` 36（+7）、`test_cost_aware_routing.py` 9（新）、
`test_reviewer+mock+e2e` 58、`test_architecture` 26、`test_crew` 52、`test_api_contract+server` 64、
`test_model_factory` 52、`test_config+config_io` 38、`test_base_agent+hardening` 98，全绿。
端到端 5/5 成功，`by_profile` 分项 1+1+4+6+1 = 13 = `llm.calls`。

### 3.59 R46：配置 A/B 矩阵评测 —— `eval --variant`（2026-11-10）

R45 加了几个"省钱/换人"的旋钮，但**没有工具能回答"这套绑定比上一套好还是坏"**。
本轮补上：同一批 case × 多套配置，逐变体跑一遍并给相对差值。

| 件 | 内容 |
|---|---|
| 变体文件 | 与 `agentcrew.yaml` **同形**（`agents.profiles` / `roles` / `models`…），只写要覆盖的段。归一化复用 `config.load_yaml_config`（新抽的公共入口），含 profile key 自动回填——用户不必学第二种格式 |
| 矩阵执行 | `evals/runner.py::run_matrix`：首个变体作基线，每变体独立子目录（共享目录会让上一次产物污染下一次，对比全失真）；`MatrixReport.to_dict()` 给出 `Δpass^k / Δcalls / Δcost / Δrevise_rounds` |
| 过程指标 | `CaseOutput` / `CaseResult` 新增 `cost_usd`、`llm_calls`、`revise_rounds`（默认 0，旧调用方零改动） |
| CLI | `eval --variant NAME=FILE`（可重复）+ `--matrix-out PATH`；退出码只看**基线**的 pass^k，对照变体不参与判定 |
| 入参防御 | 未知字段当场拒绝（`model_copy` 会静默加属性，本仓踩过"死字段"）；覆盖后走 `model_validate` 全量重建，嵌套约束（`review_rounds ≤ 3`）才真的生效 |

**实测**：`smoke.jsonl`（4 case）× `base` / `rounds1` 两变体 → 两者 pass^k 均 100%、
calls 62、cost $0.0803、修订 0。相同结果本身是结论：这批 case 复核全部通过，
`review_rounds=1` 既没多花钱也没多收益——要看到差异需要一批"产物确实缺验收标准"的 case。

**P2.2 共享工作区未做**：方案要求 `split_by_path` 的成员读同任务其它成员的 scratch，
但该模式是 `asyncio.gather` 并行的，"其它成员已写入"这个前提不成立。要么串行（放弃并行收益），
要么引入**任务内 DAG**（新特性，不是"扩 ContextBus"）。判断与最小形态记在
`docs/IMPROVEMENT-PLAN-2026-11-09.md` §10.2，不在此处草率实现。

**验证**：`test_eval_matrix.py` 8 例（变体归一化 / 未知字段 / 嵌套校验 / 差值 / 汇总 / 入参防御）、
`test_evals_2026_11_08.py` 41 例全过；`eval` 单配置路径未受影响（4/4 通过）。

### 3.60 R47：让"修订回路"在离线评测里可被观测 —— 轮次夹具 + 驳回样例 + 变体严格校验（2026-11-10）

R46 的矩阵工具是对的，但**没有数据能显形**：所有样例的复核都判「通过」，`review_rounds`
永远不触发，A/B 结论恒为"完全相同"。

| 件 | 内容 |
|---|---|
| 轮次夹具 | `MockProvider._review_text` 支持 `"<task_id>@<轮次>"` 键（先查带轮次、再退回 `<task_id>`、最后 `_default`）。没有它，"第 1 轮驳回、修订后通过"在回放里无法表达——要么永远通过，要么改到轮数用尽也不收敛 |
| 门禁避让 | G2 评审改用 `attempt=0` 取夹具：门禁不分轮次，若沿用默认 1 会误读角色内复核的 `"T-003@1"`，把好产物驳回 |
| 驳回样例 | 新增 `examples/samples/revision/`（复制 linear，仅加 `"T-003@1"` 首条标准不满足）与 `examples/evals/revision.jsonl`（2 条 case，**不断言"变好了"**——回放里主笔前后同一份产物，钉它等于自欺） |
| 变体严格校验 | `config.load_yaml_config(strict=True)` + `unknown_yaml_keys()`：无法识别的顶层段当场报错。**这是被真实事故逼出来的**——第一版变体文件我按扁平字段名写了 `agent_profiles` / `role_agents`，归一化静默丢弃，矩阵给出了"两套配置完全相同"的假结论 |

**实测（终于有差异了）**：

```
配置矩阵 · 2 条 case × 2 个变体
  base      pass^k=100%  calls=24  cost=$0.0161  修订=0
  rounds1   pass^k=100%  calls=30  cost=$0.0208  修订=2
            Δcalls +6 · Δcost $+0.0047 · Δ修订 +2 · Δpass^k 0
```

结论：**开修订在这批 case 上多花 29% 成本、通过率不变**。正确解读是"这批 case 不值得
开修订"，而不是"修订没用"——两者要靠真实模型通道区分，mock 只验证管线。

**验证**：`test_eval_matrix` 10 例（+2：扁平字段名 / 段名拼错必须报错）、
`test_agent_binding` 37 例（+1：真工厂 + mock 回放的收敛端到端）、
`test_mock_fixtures` + `test_reviewer` + `test_end_to_end_mock` 59 例、
`test_samples` 31 例、`test_evals` 41 例，全绿。

### 3.61 R48：真实通道验证复核回路 —— 抓到"复核材料被大文件挤掉"的误判（2026-11-10）

用 DeepSeek 真跑两次（7 任务 / 32–37 次调用 / $0.11–0.16），回答"修订到底有没有用"。

**结论**：修订确实改写产物（`artifact_written.sha256` 同一路径前后全变，T-03 五文件、
T-04 三文件），但也暴露一个 mock 永远测不出来的缺陷。

| 件 | 内容 |
|---|---|
| **误判根因** | `_artifact_excerpt` 预算**先到先得**：前面的 cli.py / renderer.py 吃完 1800 token 后，README 连文件名都进不了材料。复核于是判「未提供 README 片段」——而 `README.md` 在 ledger 里有 4 条 `artifact_written`。这类意见**靠修订永远修不掉**：文件不缺，缺的是"给复核看" |
| 修复 | 预算按文件数**均分**（每份保底 180 token），超出截断并标注；材料同时纳入 `store.by_task()`（本任务实际写过的文件），不再只看契约声明的交付物 |
| 复核可复盘 | `agent_review` 新增 `detail`（截断 400 字）。此前只有 `issues` 条数，事后只能看到"2 条不满足"、不知道是哪 2 条 |

**对照验证（同一目标、同一配置，PM 拆解随机故任务数 7 vs 8）**：

| | 修复前 | 修复后 |
|---|---|---|
| 任务 / 调用 / 成本 | 7 / 32 / $0.11 | 8 / 28 / $0.10 |
| 角色内复核 | 6 次（4 驳 2 过） | 3 次（1 驳 2 过） |
| 修订 | 3 次 | 1 次 |
| **"指控文件不存在"型意见** | **4 条**（全部误判） | **0 条** |

修复后驳回改为承认「产物节选被截断，无法核对 docs/PRD.md」，一轮修订即通过——
前者不可修复（只能白跑），后者可补材料。

**附带修掉规划期误判**：验收标准里「与**设计文档**样例一致」「按**设计文档**策略处理」
是**引用**另一份产物，旧检查只看关键词就判定"要求交付文档却没声明"，把正常目标拦成
`exit 2`（实测两次）。修复：关键词前后加引用标记判定（前后窗口各 4 字），
同词多次出现时只要有一次不是引用仍判为要求文档。`test_planner` 58 例全绿。

**两个需要产品决定的发现**（记录，不擅自实现）：

1. **复核比门禁严格，但只有门禁改任务状态**：同一次跑里 T-03 复核判 2 条不满足，任务仍 `succeeded`（G2 放行）。若要让复核能拦交付，需要"两票制"，属 `Aggregation` 新模式。
2. **目标描述与拆解能力有落差**：目标里写了"附单元测试/文档"、PM 拆出的交付物却没有对应文件，规划期一致性校验当场拒绝起跑（`exit 2`）。校验是对的，但用户换个措辞就跑不起来。

**未完成的验证**：想做"修复前后对照"，三次尝试分别被拆解校验（×2）与本机解释器崩溃（`PydanticUseDefault` SchemaError，单独 import 又正常）挡住。因此"误判是否减少"目前只有单测证明（三文件都在材料里）与机理推断，**没有对照数据**——如实记为待验证。

**验证**：`test_agent_binding` 38 例（+1：超大首文件不得挤掉后续文件）；`test_crew` 52 例；ruff 全绿。

### 3.62 R49：分工依赖（`intra_deps`）—— 共享工作区的最小形态（2026-11-10）

方案 P2.2 原文要求"`split_by_path` 的成员能读同任务其它成员已写的 scratch"，但该模式是
`asyncio.gather` 并行的，"其它成员已写入"不成立。R49 用"**显式声明先后**"替代"共享目录"。

| 件 | 内容 |
|---|---|
| 配置 | `Aggregation.intra_deps`：`profile → 它要先看谁的产物`。默认空 = 并行，与之前逐字一致 |
| 执行 | 非空时该角色走**拓扑序**；依赖方上下文多一条 `UpstreamDigest`（对方摘要 + 产物路径，文件在盘上可直读） |
| 失败语义 | 依赖项失败**不阻塞**下游，由 `quorum` 判整体成败——否则一个 section 挂了整条链静默停摆 |
| 配置期校验 | 只能指向本角色 section、不能自依赖、**不能成环**、只允许 `split_by_path`（成环不是慢，是永远跑不起来） |
| 运行期兜底 | 手工构造绕过校验时仍遇环 → 退回原序继续，绝不死循环 |

**为什么不做 scratch 目录**：真正的需求是"谁先谁后"，共享目录只是手段之一。顺序表达出来后，
前序产物本就在 `ArtifactStore` 里，再造一层 scratch 只多一个状态源和一类不一致。

**验证**：`test_agent_binding` 42 例（+4：非 split 模式拒绝 / 自依赖·未知·成环 / 拓扑序 +
上游摘要注入 / 依赖失败不阻塞），`test_crew` + `test_config` + `test_architecture` 110 例全绿；
`examples/agentcrew.multi-agent.example.yaml` 增注释示例，配置校验仍通过。

### 3.63 R50：差距报告 69 条整改 —— 从"能跑"到"能上线"（2026-11-11 ~ 11-12）

按 `docs/GAP-ANALYSIS-2026-11-10.md` 的 69 条登记，分 8 个批次逐条落地。处置明细见
**`docs/REMEDIATION-STATUS.md`**（逐条状态 + 批次 + 验证方式）。按主题归纳：

| 主题 | 关键改动 | 为什么重要 |
| --- | --- | --- |
| **安全边界**（P0 全 8 项） | Webhook 只走 http/https 且禁重定向/内网；事件名白名单；`X-Forwarded-For` 仅可信代理可取；500 不再回显 `str(exc)`；Studio 主进程只认自己落盘的 `apiBaseUrl`、独立无凭据 `api:probe` | 这 8 项都是「能被直接利用」的口子，不是理论风险 |
| **崩溃一致性** | 封版顺序与 worker 取消对齐、MANIFEST 事务化、`record_review`/`bump_rework` 立即落盘（`on_persist` 钩子）、checkpoint 原子写 | 硬杀后 resume 看到的盘上状态必须自洽 |
| **时钟容错**（A-7） | 新增 `runtime/clock.py`：`utc_now` / `parse_stamp` / `elapsed_since`；心跳与 resume 锁改 aware UTC；三处墙钟判定在时钟倒挂时 **fail-closed** | 时钟前跳会把活 run 判成孤儿并改写状态；回拨则孤儿永不收敛。宁可留僵尸也不误杀 |
| **并发正确性** | 审批 `decide` 读-改-写加锁 + 落盘复核；预算硬上限判定提到两个 emit 之前 | 并发裁决后写覆盖先写；observer 异常会整段跳过预算检查 |
| **契约与可观测** | `GET /metrics`（Prometheus 文本，不扫盘）；契约测试补 39 条断言（逐路由 401 穷举、429、413、响应形状）；openapi 补 `securitySchemes` 与 401/413/429 | 此前契约测试只断言「不是 404」，认证失效也能无声通过 |
| **工程化** | `.env.example` 重写（去行内注释——dotenv 会把注释读成值）、`agentcrew.example.yaml`、Docker 三件套（非 root + 健康检查 + 挂卷）、前端 CI job、覆盖率门槛、回滚手册 | 「本地能跑」到「别人能部署」的差距 |
| **Studio** | 弹窗定时器可取消、测速期间快照防错位、路由切换释放 flow、`escapeHtml` 补引号、CSP 收紧、SSE 断线指数退避重连、刷新代次校验、`spawnSync` 全改异步 | 断线后实时面板会彻底静止；删除大 run 会冻结窗口 15 秒 |

**验证**：全量 **3115 passed / 0 failed / 2 skipped**；`ruff check agentcrew/ tests/ sdk/` 全绿；
Studio 架构检查 73 模块 / 42 分片通过、`test:unit` 18 passed。

**最初刻意留到后面的一件已完成，另一件有明确归属**：

- **D-14 时间戳全量迁 UTC**：✅ **已在本批完成**（2026-11-12 追加）。10 处落盘点全部改
  `utils/clock.utc_now()`，9 处时长计算改 `seconds_between()`，`clock` 从 `runtime` 移到
  `utils`（`models` 层禁止依赖 `runtime`）。迁移顺手抓到两个真实缺陷：`Approvals.is_expired`
  用 naive now 比 aware `expires_at`（直接 `TypeError`）、Studio 按字符串比较 ISO 串
  （混存时字典序与时刻不一致，会丢新快照）。守规基线已降为 **0**。
- **D-16 的「写盘注入 OSError」**：注入点与 `_finalize` 内部原子写耦合过紧，维护成本高于收益；
  已覆盖心跳被删 / 时钟回拨 / 锁互斥 / 状态矩阵四类更常见的边界。

### 3.64 R51：能力补齐第一批 —— 联网搜索 / 沙箱后端 / 执行验证 / 评测证据（2026-11-12）

按 `完善/AgentCrew落地完善路线-2026-11-12.md` §5「第一批（P0）」落地。三条判断贯穿全批：
**默认关闭**（不配就没有这些能力）、**local 行为逐字不变**（离线可测不被破坏）、
**fail loud**（选了 docker 却没有 docker 就报错，不静默降级）。

| 能力 | 落地方式 | 边界 / 证据 |
| --- | --- | --- |
| **C1 联网搜索** | 新增 `tools/search.py`：`SearchProvider` 接口 + `tavily` / `brave` / `duckduckgo` / `mock` 四实现 + `WebSearchTool`。`search_provider` 为空时**不注册**；密钥只从 `TAVILY_API_KEY` / `BRAVE_API_KEY` / `AGENTCREW_SEARCH_API_KEY` 读 | 端点固定公网 https、不跟随重定向；结果按 URL 去重并逐个过 `validate_fetch_url`（私网 / `file://` / 带凭据一律丢弃）；输出 8000 字符截断。`tests/test_web_search.py` 17 例 |
| **C2 沙箱执行后端** | 新增 `tools/sandbox.py`：`ExecutionBackend` 接口 + `LocalBackend`（与 R35 逐字一致）+ `DockerBackend`（禁网、非 root 65534、`--cap-drop ALL`、`no-new-privileges`、只读根 + `/tmp` tmpfs、`--memory`/`--cpus`/`--pids-limit`、只挂 `<run_dir>:/workspace`）。`run_python` / `run_tests` / G1 执行校验共用 | 配置 `sandbox_backend` / `sandbox_image` / `sandbox_memory_mb` / `sandbox_cpus` / `sandbox_allow_network`；`build_backend` 在构造期探测 docker，缺失即 `SandboxError`。`tests/test_sandbox_backend.py` 17 例（含 argv 逐项断言、缺 docker 报错、启动崩溃重试一次） |
| **C3 执行-测试-修正** | 新增 `tools/testing.py`（`run_tests`：只接受 `path`/`timeout`，不写 `.pytest_cache`，输出保留尾部）+ `gate/execute_verify.py`（G1 执行校验档，`gate_config.exec_verify` 默认关）。失败并入 G1 issue/hint，走**既有** `REWORK` 通道 | 校验目录是 `<run_dir>/verify/<task_id>/` 的产物副本；无测试文件记 `skipped`、mock 通道记 `g1_exec_check_skipped`；本地/容器后端同一份。`tests/test_run_tests.py` 9 例 + `tests/test_exec_verify.py` 10 例（含与 Gatekeeper 的接线与默认关闭） |
| **C11 评测证据** | `eval --live`（真实模型）+ `--max-cost` 硬上限；新增断言 `event_field` / `artifact_any_contains` / `artifact_any_matches`；`CaseOutput.events` 事件快照；两份数据集（`capabilities.jsonl` 4 条 mock 接线、`capability-live.jsonl` 23 条真实小目标：代码 8 / 文档 6 / 数据 6 / 驳回 3）；`scripts/nightly_eval.py` 夜间跑并落盘 `bench/eval-*.json` | mock 能力接线 **4/4 通过**（见 `bench/eval-capabilities-offline-2026-11-12.json`）；真实 23 条需密钥，尚未跑（§7） |
| **C12 发布证据** | 全量回归 + ruff/mypy 复核；`docs/CAPABILITIES-C1-C3.md` 落地说明；OPERATIONS / THREAT_MODEL / example.yaml 同步 | 见下方验证；Docker 与推送两项本机不可验证，如实记在 §7 |

**新增测试 62 例**（沙箱 17 / 搜索 17 / run_tests 9 / 执行校验 10 / 配置通道 8 /
工具实验台 +1），全量 **3177 passed / 0 failed / 2 skipped**（160 文件 / 40 批次，
`python scripts/test.py` 一次跑出）。
`ruff check agentcrew/ main.py tests/` 全绿；`mypy agentcrew/` 137 文件**零新增错误**
（CI 两轮全绿；R52 新增 MCP server / 入站触发 / 浏览器模板三项、R53 新增多模态输入，见 §3.65–§3.66。首次 CI 实跑暴露的 5 条随 e4add0f 带入、从未被 CI 检出的既存错误，已在本批
顺手修掉：`flow/spec.py` 三处 `TaskDraft` 显式补 `timeout_seconds=300`、
`api/server.py` 两处 Optional 访问补空值守卫；Studio job 的
`package-lock.json` 被 `studio/.gitignore` 忽略导致 `npm ci` 无锁可用，已改为
跟踪 lock 文件）。

**明确不做**：不引 docker SDK（拼 CLI 参数即可）、不实现 gVisor/microVM（留
`ExecutionBackend` 接口）、不把 `sandbox_backend` 开进 API options（防隔离降级）、
### 3.65 R52：能力补齐第二批 —— MCP server / 浏览器接入 / 事件触发（2026-11-12）

按路线文档 §5「第二批（P1）」交付三项：C7（暴露为 MCP server，杠杆最大）、
C6 第一步（借现成浏览器 MCP，本仓零开发）、C8 入站触发（定时运行此前已由
`main.py schedule` 交付）。贯穿口径：**不造第二条权限语义**——MCP 与 Webhook
触发都复用现有 API 层的 RBAC / 限流 / 审计。

| 能力 | 落地方式 | 边界 / 证据 |
| --- | --- | --- |
| **C7 MCP server** | 新增 `mcp/server.py`：`ApiBridge`（urllib，零依赖）把 5 个工具（create_run / run_status / list_approvals / approve / wait_for_run）翻译成对 HTTP API 的请求；`run_stdio` 换行分隔 JSON-RPC 主循环；CLI `mcp-serve` | RBAC / 限流 / 审计由 API 侧执行——客户端拿到什么角色的 key 就只能做什么。协议面刻意窄（initialize/tools/list/tools/call/ping），streamable HTTP 传输等有真实远程调用方再做。`tests/test_mcp_server.py` 13 例（协议 + 对真实 in-process ApiServer 的桥接端到端） |
| **C6 浏览器·第一步** | `examples/agentcrew.browser.example.yaml`：接 Playwright MCP（`mcp_servers`）+ 按角色「读优先」白名单（`playwright__browser_navigate/snapshot` 等）；写类工具需显式放开 | 零开发（R13 客户端既有能力）。`untrusted_inputs` 与 MCP 工具互斥这条边界写进模板并有测试钉住。`tests/test_browser_mcp.py` 4 例；真实浏览器端到端待装 Playwright MCP 后补 |
| **C8 入站触发** | 新端点 `POST /webhooks/trigger`：两条认证路径——HMAC 签名（复用 R12 语义，±300s 防重放，给不持 key 的外部系统）或 API Key（无签名头时与全站同形，无凭证 401）；新作用域 `webhook:trigger`；触发主体只有 `runs:write`；来源以 `run_triggered` 事件进 run ledger | 配置 `webhook_trigger_secret`（默认空=关闭 503；不进快照、参与脱敏）；`_apply_middleware` 用「无作用域占位主体」区分两条路径，防 anonymous-admin 兜底骗过签名。`tests/test_webhook_trigger.py` 8 例 |

**新增测试 27 例**，全量 **3204 passed / 0 failed / 2 skipped**（163 文件 / 41 批次）。
ruff 全绿；mypy 138 文件零错误。`openapi.yaml` / `THREAT_MODEL v1.2`（新增 T-50）/
`OPERATIONS` / `README` / `agentcrew.example.yaml` 同步。

**明确不做**：MCP server 不做 resources/prompts/sampling；HTTP 传输不做；
不内置 playwright（`agentcrew[browser]` 留待按需）；签名重放不引入 nonce
### 3.66 R53：C4 多模态输入 —— 图片从入口到 provider 请求的完整链路（2026-11-12）

第二批的第三块（C4）。贯穿口径与 R51/R52 一致：**默认关**（`supports_vision`
能力位全 False，纯文本路径逐字节不变）、**降级可见**（不支持时丢图 + 落事件，
绝不静默假装看过）、**入口校验单一事实源**。

| 层 | 落地方式 | 证据 |
| --- | --- | --- |
| Provider | `Message` 增加可选 `images` 字段（`{"mime", "data"}` 内联 base64）；OpenAI 兼容端点在 `supports_vision` 时转 `image_url` 内容块（data URI / URL），Anthropic 转 `image` source 块（base64 / url）；关闭时带图消息与纯文本**逐字节一致**（`tests/test_multimodal.py` 两两对拍钉住） | 12 例单测；真实模型「截图→改前端」端到端待 vision 模型环境补 |
| 模型工厂 | `supports_vision(alias)` 能力探测（与 `supports_native_tools` 同构）；能力位 `ProviderCapabilities.supports_vision` 默认 False，经 `provider_overrides.<provider>.capabilities` 开启 | 能力位全 False → 存量部署零变化 |
| Agent | `BaseAgent` 接收 `images`（AgentFactory 下发）；`build_messages` 在有能力时把图挂到 user 消息，无能力时不挂并落 `vision_unsupported`（每 Agent 一次，不刷屏） | 降级必须可见：否则「截图改前端」无声变盲写 |
| 入口 | `POST /runs` body 可选 `images`（`validate_images`：≤4 张、单张 base64 ≤3.5M 字符、MIME 白名单、拒 URL 形式防代抓外链的 SSRF 面）；CLI `run --image PATH`（可重复） | 坏图 400，不会带着半截 base64 起跑 |
| 留证 | 图片写 `<run_dir>/context/images/NN.<ext>` + ledger `images_attached(count/total_bytes/files)`，随 run 保留可 diff | 落盘失败只记 warning 不炸 run |

**新增测试 14 例**，全量 **3218 passed / 0 failed / 2 skipped**（164 文件 / 41 批次）。
ruff 全绿；mypy 138 文件零错误。openapi `CreateRunRequest.images` 已登记。

**追加（2026-11-13）：Studio 结果区「输入图片」预览**——Electron 主进程新增
`images:list` / `images:read` IPC（文件名白名单 `<NN>.<ext>` 防路径穿越，不走 HTTP），
结果区有图才显示缩略图、点击放大（一次性遮罩）。Studio 架构检查 73 模块合规、
`test:unit` 18 passed。C4 的产品体验闭环至此补齐。

**明确不做**：不代抓 URL 图片；不做截图工具闭环（属浏览器工具组）；图片不计入
context token 预算（预算管文本）。

---

### 3.67 R54：C5 文档 RAG —— 团队手册/规范注入规划与执行上下文（2026-11-12）

第二批收尾项。完全复用既有检索资产（R11 的 BM25、R27 的混合打分与 embedding
通道），新增的只有「文档摄取」这一条管线。

| 层 | 落地方式 | 证据 |
| --- | --- | --- |
| 摄取 | 新增 `knowledge/docs.py`：`DocStore` 扫描 `<output_dir>/knowledge/docs/`（`.md/.txt`），按「Markdown 标题分节 + 超长段硬切（700 字符/80 重叠）」分块；manifest 按 sha256 **增量**——没变的文档复用块、删掉的文档出索引；ledger `docs_ingested(docs/chunks/rebuilt/skipped)` | `tests/test_docs_rag.py` 摄取/增量/删除 4 例 |
| 检索 | BM25 + R27 混合打分（异步 `query` 支持真实 embedding 端点；同步 `query_sync` 对真实端点退化为纯 BM25——上下文组装是同步路径，不偷偷起线程，如实写进配置 docstring） | 同步/异步对拍钉住（哈希向量下结果一致） |
| 注入 | 规划期：`_prepare_knowledge_context` 末尾追加「团队文档」块（ledger `docs_context_used`）；执行期：ContextBus 新增 `docs_retriever` 回调（级 5.5，`docs:<来源>` 节选，同一 token 预算记账，超预算标记 truncated） | 不传 retriever 时行为与 C5 之前逐字一致（向后兼容钉住） |
| 入口 | CLI `main.py docs list / search`（list 打印每篇块数、search 用 run 内同一套打分做检索演示）；配置 `docs_rag_enabled / docs_rag_limit / docs_rag_vector_weight`（env/YAML/快照同源，默认全关） | CLI smoke（空目录 list 0 篇不炸） |

**新增测试 13 例**，全量 **3231 passed / 0 failed / 2 skipped**（165 文件 / 42 批次）。
ruff 全绿；mypy 139 文件零错误。THREAT_MODEL v1.3（§2.3 补两类输入源）同步。

**明确不做**：文档写接口（来源是用户与 git）；跨目录/外部源摄取；向量数据库
（复用内存 BM25 + 磁盘向量缓存，经验库同款）。

**整批验收**：R51–R54 的逐项验收结论（对标路线文档验收标准、区分已验证/待真机）
汇总在 `docs/ACCEPTANCE-2026-11-12.md`——总评**通过**，P0/P1 共 9 项能力全部落地，
待真机 6 项挂账 §7。

**真实模型评测首批实测（2026-11-13）**：DeepSeek 真实通道 8 条（代码 6 / 文档 1 /
数据 1 / 驳回 1）**8/8 通过**——驳回类 case 证明 G1 空文件检查在真实通道下真实拦截。
余 15 条因 DeepSeek 账户 402 余额耗尽中断（chunk04 起 2 条 FAIL 均为 402，非能力
问题），充值后 `python main.py eval examples/evals/capability-live.jsonl --live
--max-cost 0.25 --output-dir output/_eval` 直接续跑。明细见
`bench/eval-live-2026-11-13.md`；试点子集 `examples/evals/capability-live-pilot.jsonl`。

---

### 3.68 R56：C2 部分用量记账——流被打断时预算不再漏算（2026-11-13）

**缺口**：``stream_call`` 原本只在流**完整消费完**时记账。调用方 break / 取消 /
已 yield 后抛错时，已吐出的块是确定的 token 消耗，但调用数、成本、配额全不落
——取消后重跑 = 预算计数悄悄偏低，可能无限逼近上限却永不触发（R50 时搁置的
stream_call 设防项，C2 剩余里唯一有真实风险敞口的）。

| 改动 | 说明 |
| --- | --- |
| ``_record_usage``（新） | 从 ``_account`` 拆出「累加 + 落 ``llm_call`` + usage/配额/agent 级记账 + span」，**不做硬上限检查**——单一记账口保持（两套累加迟早漂移）；``partial=True`` 时事件多一个 ``partial`` 键（正常路径字段集与 spec §13.2 七项契约逐字节一致） |
| ``_account`` | 改为调 ``_record_usage`` + 三层预算决策（硬上限/软阈值/降级），语义不变 |
| ``stream_call`` finally | ``not success and parts`` 时落一条部分用量：估算 prompt + 已收文本 token；**不抛** ``BudgetExhaustedError``（异常正在传播，不能掩盖；下次调用的 ``_guard_run_cost`` 会按累计值拦截）；记账自身失败只 warning |
| 测试 | ``tests/test_stream_partial_accounting.py`` 4 例：正常流无 partial 键；打断后 partial 事件 + 计数/ token 入账；部分用量计入 ``max_llm_calls``（第二次调用被拦）；记账异常不掩盖原异常 |

**取舍**：曾尝试给 ``llm_call`` 事件加 ``finish_reason`` 字段，被
``test_model_factory`` 的七项契约守卫拦下（「多一个少一个都算漂移」是刻意的）——
撤掉，保持审计形状不变。

定向回归：预算/流式/记账/配额/成本 218 例 + model_factory 56 例全绿；
ruff 全绿；mypy 139 文件零错误。全量由 CI 仲裁（本机当日 Python 进程
不稳定：反复段错误 / ``SystemError``，前半 76% 崩溃——环境问题，非回归，
本地定向子集均通过）。**注意**：本机默认 ``python``（3.13.12）的依赖在
2026-11-13 00:57 被环境重置清空，后续一律用 ``./.venv/Scripts/python.exe``。

---

### 3.69 R57：C2 配额设防——流式调用不再绕过配额（2026-11-13）

**缺口**：R56 让被打断的流也记账，但 ``stream_call`` 自始至终**不检查配额**：配额
耗尽的别名照样能起流，预扣机制对并发流式调用完全空转（``call`` /
``complete_structured`` 都已有「check + reserve + release」两段式，只有流式没有）。
R50 起一直记为「只记账不设防」。

| 改动 | 说明 |
| --- | --- |
| 起流前 ``check`` + ``reserve`` | 估算与 ``_attempt`` 同口径（请求体 + ``max_tokens``）。``check`` 与 ``reserve`` 之间**没有 await**，所以「检查 → 预扣」在事件循环内原子——并发流不能同时通过检查 |
| 配额耗尽走故障转移 | ``QuotaExhaustedError`` 本就在 ``_PROVIDER_UNAVAILABLE_TYPES`` 里，这里手工沿 ``model_failover`` 切换（此时尚未吐块，不会产生两半不同来源的文本）；单别名时原样上抛 |
| 任意退出路径 ``release`` | 正常结束由 ``_account`` 结算实际、被打断由部分记账结算、首块前失败什么都不结算——预扣一律在 ``finally`` 归还，绝不留在窗口里 |
| 为什么 ``check`` 不在 try 里 | 与 ``_attempt`` 同一个坑：若与 ``reserve`` 同处 try，异常分支会把「本次从未预扣」误 release，抹掉别的并发流的预扣 → 并发超卖 |
| 测试 | ``tests/test_stream_quota_2026_11_13.py`` 5 例：单别名耗尽不起流 / 多别名转移且备用只入账一次 / **in-flight 流的预扣拦住并发第二条流**（设防核心，此前必放行）/ 完整流只结算一次（预扣归零后第二条能过、第三条被拦）/ 首块前失败预扣不泄漏 |

**顺带修掉一个跨平台测试缺陷**：``test_cli_provider::test_basic_echo_command`` 用
``echo hello`` 当可执行文件——``echo`` 是 shell 内建，Windows 上
``shutil.which("echo")`` 为 ``None``，本机全量报 ``FileNotFoundError``（只有 Git 的
``usr/bin`` 恰好在 PATH 时才碰巧通过）。改为解释器自身打印固定文本，与同文件另两个
用例的跨平台写法一致；Linux 行为不变。

**验证**：新增 5 例全绿；定向回归 91 例（stream 系列 + ``test_quota_v7`` +
``test_model_factory``）与 144 例（budget / cost-routing / agent-binding / crew /
e2e / architecture）全绿；``ruff`` 全绿；``mypy agentcrew/`` 139 文件零错误。
全量 ``scripts/test.py``（167 文件 / 42 批次 / 454.5s）**3241 passed · 0 failed ·
2 skipped**（崩溃重试恢复 2 个进程级崩溃）。首次失败后重试通过的那条
（``test_approval_interfaces::test_cli_approve_rejects_unknown_request``）已单独连跑
3 次全过，确认是环境抖动、非回归。

**仍属环境**：本机前两次全量分别以「3240 passed / 1 failed（即上面那个 echo 用例）」
和「3191 passed / 0 failed + 4 文件在 collection 阶段被损坏的字节码拦下（Error 内容是
pydantic ``fields.py`` 与 ``registry.py`` 的 ``SyntaxError``）」收场；后者单跑
59 passed 全过。这类崩溃与代码无关（§6「环境与工具链类」有记录），跑器重试可收敛。

---

### 3.70 C11 真实模型评测全量闭环 + 前端链路复验（2026-11-13）

**缺口**：C11「真实模型」验收项此前只跑到 8/8——DeepSeek 账户余额耗尽后余下 15 条
一直挂账（`bench/eval-live-2026-11-13.md` 旧版）。余额恢复后（`doctor` 6 项通过、
`provider:deepseek:deepseek-chat: healthy 1340ms`）补跑完毕。

| 事项 | 结果 |
| --- | --- |
| 剩余 15 条分块 | `examples/evals/capability-live-rest.jsonl`（15 条）+ `-rest-a/-b/-c.jsonl`（各 5 条，UTF-8 无 BOM），便于中断后分块重跑 |
| 补跑结果 | **14 通过 / 1 失败**（A 4/5、B 5/5、C 4/5）→ 全量 23 条 **22 通过（pass@1 = 95.7%）** |
| 补跑成本 | A $0.2367（含 `doc-runbook` 重跑 $0.0670 / 21 calls）+ B $0.2081 + C $0.1535 = **$0.5983** |
| 驳回类证据 | `reject-empty-py`：模型三轮都写出 0 字节 `src/empty.py`，static 层每轮以「为空文件」「仅含占位内容」reject，run 终态 failed——**门禁在真实通道真的拦**（连同首批 `reject-empty-file` 的 markdown 变体，两种产物类型均有证据） |
| 前端链路 | `cd studio && npm run check`（73 模块 / 42 分片）、`npm run test:unit` 18/18、`npm run e2e` **32/32**（截图 20 张 → `studio/e2e-out`） |
| 文档回填 | `bench/eval-live-2026-11-13.md` 全量重写；`docs/ACCEPTANCE-2026-11-12.md` C11 行、本文 §0 指标表、§7「剩下的」表同步 |

**发现 1（真实失败，非回归）：文档类契约的 `must_contain` 是逐字匹配。**
`doc-changelog` 的 PM 把 `迁移步骤` 写进 `must_contain`，G1 用
`needle.lower() in content.lower()`（`agentcrew/gate/static_checks.py:128-138`）判字面包含；
模型产出的是「## 5. 分步迁移路径」+「| 步骤 | 动作 | …」表，语义上有迁移步骤但缺这四个
连续字，返工 3 轮全是「在 … 中定义 迁移步骤」的 hint，模型每轮改写措辞，最终 T2/T3/T4/
T5/T-FINAL 未成功、run failed。**性质**：门禁按契约办事、判定可解释；坏在「自然语言
文档 + 字面符号契约」这一组合，以及「定义」式 hint 对文档不贴切。**已处置**（R58，见
§3.72）：hint 改为「逐字包含 {needle}」，判定仍是字面 `in`，门槛一点没放宽。

**发现 2（证据缺口）：规划期 fail-fast 挡住了驳回类用例。**
`reject-empty-json` 两次 `plan_rejected`——①T2 验收标准要求文档而交付物无文档类产物（标准
自相矛盾）；②`产物路径冲突：data/blank.json 被 T1 与 T2 同时声明`——run `exit_code=2`，
**没走到 G1**，用例想验的「0 字节 JSON 被拦」没取到证据。两条拒绝本身都是正确的 fail-fast；
缺口在用例设计（goal 应明确「单任务、不产出文档」）。同族证据已由 markdown / py 变体覆盖。

**新增取证工具**：`scripts/regrade_eval.py` —— 本机进程级崩溃（`Internal CLR error`
连坐 python）会让「已跑完的 case」结论随汇总一起丢，该脚本按 `run_case` 同一口径读盘
（`run.json` / `ledger.jsonl` / `artifacts/`）重算断言，**不重新调模型、零成本**。两个必须
守住的点已写进实现：①`--since` 过滤同一 case 的历史 run 目录（否则取到旧结论，工具自身
踩过）；②把 run 级 `error` 带回 `CaseOutput`——`evaluate_checks` 见 error 即全判失败，
少了这一步，规划期就崩掉的 reject 用例会被误判通过（也踩过）。

**环境噪声（不记回归）**：本轮两次中断——A 块首跑 PowerShell 宿主 `Internal CLR error
(0x80131506)` 退出码 `-1073741819`，连带 python 被杀（`doc-runbook` 停在 `running`，
重跑后通过）；C 块之前另有 `0xC0000005` 访问冲突记录。输出重定向改用
`Start-Process -RedirectStandardOutput/-RedirectStandardError` 后未再复现。

---

### 3.71 上线自检：本机可验 9 项全过 + `doctor` 可选依赖误报修复（2026-11-13）

**目的**：把「代码写完了」推进到「按上线清单验过」。逐项对照 `docs/OPERATIONS.md`
§8，只记本机真跑出来的结果（完整记录见 `docs/DEPLOY-CHECK-2026-11-13.md`）。

| # | 检查 | 结果 |
| --- | --- | --- |
| 1 | `main.py doctor --json` | `ok=true`，6 项全 pass（含 `provider:deepseek:deepseek-chat` healthy 401ms） |
| 2 | `run --sample todo --no-live --auto` | 9 任务 9 成功 / 0 失败 / 0 阻塞，返工 1 次，4s |
| 3-5 | `serve --api-key <临时>` 下 `GET /health` / 无 key `GET /health` / `GET /metrics` | **200 / 401 / 200**（metrics 首行 `agentcrew_up 1`） |
| 6 | 无密钥 `serve` | **退出码 3**，stderr 给三条修法，与文档一致 |
| 7 | `clean --dry-run --protect-running` | 列出「将删除 225 个 run（保留最近 5 个）」，未真删 |
| 8 | `cd studio`：`check` / `test:unit` / `e2e` | 73 模块 / 42 分片 · **18/18** · **32/32**（截图 20 张） |
| 9 | `ruff` / `mypy` | 全绿 / 139 文件零错误 |

**默认安全姿态核实**（`agentcrew/config.py` 默认值，逐个看过）：`api_auth_enabled=True`、
`tools_enabled=False`、`code_exec_enabled=False`、`sandbox_backend=local`、
`search_provider=""`（关）、`gate_config.exec_verify` 缺省关；`max_cost_usd` /
`max_llm_calls` 默认 **None（不限制）**——这是唯一必须在部署侧设死的项，已写进清单。

**修掉一处误报**：`doctor` 的可选依赖清单里列着 `aiohttp`，但**全仓没有任何 aiohttp
导入**（API/SSE 是自研实现，见 `agentcrew/api/server.py` 模块注释），导致每个新环境
都稳定收到一条「可选依赖缺失」warn。误报会让运维习惯性忽略黄色输出，比漏报更坏——
已把 `_OPTIONAL_DEPS` 清空并留注释说明「将来真有可选依赖再逐项加回」；
`dependencies` 由 warn 变 pass，`tests/test_doctor.py` + `tests/test_cli.py` 共 55 例
全绿。

**仍需目标机做**（本机无法证，已逐条列判据）：TLS 反代与 SSE 缓冲设置、监控两条告警、
独立数据盘、`AGENTCREW_LOG_JSON` 采集与脱敏确认、密钥管理、成本上限、webhook 接收端
幂等、容器挂卷与 healthcheck、备份恢复演练、docker 后端下 `g1_exec_check` 真跑一次
`passed`。全部在 `docs/DEPLOY-CHECK-2026-11-13.md` §4。

**顺带修掉一个真实测试缺陷（SDK 端到端就绪等待）**：`tests/test_sdk.py::
test_end_to_end_against_real_serve` 起真 `serve` 后只给约 5 秒就绪预算（50×0.1s），
本机并发跑多个 pytest 子进程时 serve 光导入 pydantic 就可能超 5 秒——于是「还在启动」
被记成「未就绪」，全量里假失败过一次。改为 **30s 预算 + 盯 `process.poll()`**（子进程
已退出就立刻判死、把退出码与 serve 日志带进断言消息），既不放宽任何业务断言，也让
失败信息从「未就绪」升级为可定位的进程崩溃。实测：单跑 6/6、整文件 3 次里 2 次通过
1 次**进程级崩溃**（`-1073741819`，子进程 import pydantic 时 heap corruption，属 §6
环境噪声），组合跑 3 次里 2 通过 1 次抓到同一崩溃的完整栈——确认是环境而非用例缺陷。

**最终回归**：`scripts/test.py --batch 4 --retries 3 --retry-failed 2`（167 文件 /
42 批次 / 621.2s）→ **3241 passed · 0 failed · 2 skipped · 0 error**，崩溃重试恢复
8 个（进程被杀），**没有「首次失败后重试通过」的待核项**。`ruff` 全绿、`mypy` 139 文件
零错误。

---

### 3.72 R58：G1 返工 hint 改为「逐字包含」——修提示，不放宽门槛（2026-11-13）

**起因**：§3.70 发现 1。`must_contain` 里既有代码符号（`def f`），也有自然语言短语
（文档类 case 的「迁移步骤」），而返工 hint 一律写成「在 {path} 中**定义** {needle}」。
真实模型评测里 `doc-changelog` 三轮返工都收到这句话，每轮都**改写措辞**（写成「分步迁移
路径」「| 步骤 |」表格），始终没让这四个连续字出现，run 直接 failed——提示把人指错了方向。

| 改动 | 说明 |
| --- | --- |
| `agentcrew/gate/static_checks.py` | hint 改 `在 {path} 中逐字包含 {needle}`；判定逻辑一行未动（仍是 `needle.lower() in content.lower()`），issue 文案不变 |
| `tests/test_gatekeeper.py:239/307` | 两处断言的 hint 文案同步 |
| `tests/test_static_checks.py` 新增 1 例 | 用文档类 needle（`迁移步骤`）钉死「字面没有 → 仍 reject」+「hint 必须说逐字包含」，防止将来有人把文案改回收敛不了的那种 |

**为什么不算放宽门槛**：`docs/THREAT_MODEL.md` 与 §7 的口径是「守卫不删、判定不松」。
这次只换返工方向说明，产物不合契约照样 reject（新用例第一行断言就是 reject）；同时
大小写不敏感的宽容仍保留（真实模型写 `Flask`、契约写 `flask` 的坑，R57 之前已修）。

**验证**：`tests/test_static_checks.py` + `tests/test_gatekeeper.py` **56 passed**；
全量 `scripts/test.py --batch 4 --retries 3 --retry-failed 2`（167 文件 / 42 批次 /
486.0s）→ **3242 passed · 0 failed · 2 skipped · 0 error**（崩溃重试恢复 5 个，无待核项）；
ruff 全绿；mypy 139 文件零错误。**CI 仲裁**：run **43392129252** 六 job 全 success
（Python 3.10/3.11/3.12/3.13 + Windows(uv) + Studio Node 20）。

---

### 3.73 创造条件：三条「待真机」项不再等外部资源 + 两个实测缺陷（2026-11-13）

**这一节的立场**：所谓「待外部环境」，先问一句「这条证据必须在那台机器上产生吗」。
B1（docker 逃逸）、B3（MCP 互通）、B4（浏览器 MCP）三条都不是——缺的是「一台能跑
容器的机器」和「一个桌面客户端」，两者都能用别的东西替代。只有 B2（vision）真的
缺一个买不到就做不了的 key。

#### B1 docker 真隔离 → 用 GitHub runner 当那台机器

先取证「这台机器确实做不到」，再换路：

- 云主机（Linux，2C/3GB）：装 `docker.io 29.1.3` 能起 daemon（`--iptables=false
  --bridge=none --storage-driver=vfs`），但 `docker pull python:3.12-slim` 报
  `failed to register layer: unshare: operation not permitted`；`capsh --print` 的
  bounding set 里没有 `cap_sys_admin`；`sudo unshare -m true` 同样 EPERM；
  rootless podman 报 `newuidmap: exit status 1`。**容器在这台机器上不可能跑起来。**
- 本机 Windows 无 docker daemon。

于是把 CI 当宿主机（runner 有 docker、有网络）：

| 改动 | 说明 |
| --- | --- |
| `tests/test_docker_isolation_live.py`（新增） | 4 条**真容器**用例：能跑 python 且 workspace 可写 / `--network none` 出不了网 / 宿主文件在容器里不存在 / 非 root 且内存与 `pids.max` 落到 cgroup 上。默认 skip（缺 `AGENTCREW_DOCKER_LIVE=1` 一律不跑，不许「因为环境缺 docker 而绿」） |
| `.github/workflows/ci.yml` 新增 `sandbox-docker` job | `docker pull python:3.12-slim` → `AGENTCREW_DOCKER_LIVE=1 pytest -q tests/test_docker_isolation_live.py` |

**首轮真跑就抓到一个真缺陷**（run `43398109892`：4 条里 1 条失败，3 条过）：
容器以 `--user 65534:65534` 跑时，写不进挂载进来的 `/workspace`——
`PermissionError: [Errno 13] Permission denied: 'out.txt'`。`/workspace` 是宿主目录、
属主是宿主用户，容器 uid 与它不一致就写不了；也就是说模型**连自己的产物都存不下**，
`run_tests` / `exec_verify` 在这条路径上会全线失败。修法：

| 改动 | 说明 |
| --- | --- |
| `agentcrew/tools/sandbox.py` | 新增 `default_container_user()`：POSIX 上取宿主 `uid:gid`（非 root，且与 `/workspace` 属主一致），Windows 无 uid 概念退回 `65534:65534`；`DockerBackend` 新增 `user` 参数，**uid 部分为 0/root 时构造期即 `SandboxError`**（沙箱里不许 root，这条是硬约束） |
| `tests/test_sandbox_backend.py` | 参数断言从「等于 65534」改为**性质断言**「不是 root 且带 gid」——原断言把实现细节钉死了，换 uid 就假红；另新增 2 例：拒 root、默认值跟宿主一致 |
| `tests/test_docker_isolation_live.py` | uid 断言同样改成「不是 0」（值随机器的宿主 uid 变化） |

二轮 run **`43399398936`**：7 个 job 全 success（Python 3.10/3.11/3.12/3.13 +
Windows(uv) + Studio + **Sandbox·docker 真隔离**）。B1 由此闭环，`docs/REMAINING`
里那条「本机或 CI 装 Docker」不再是阻塞项。

#### B3 MCP 互通 → 用官方 Node SDK 代替桌面客户端

不装 Claude Desktop / Cursor，用官方 `@modelcontextprotocol/sdk` 直接当客户端
（`output/_eval/mcp-interop/probe.mjs`）：`StdioClientTransport` 启动
`.venv\Scripts\python.exe main.py mcp-serve --api-url … --api-key …`，实测

```
initialize      → server = {"name":"agentcrew","version":"1.0.0"}
tools/list      → 5 个工具（create_run / run_status / list_approvals / approve / wait_for_run）
tools/call      → create_run 得 run_id 20261113-165919-5a0c
tools/call      → wait_for_run = completed，全程 6.2s
interop PASS
```

两个坑记下来：① `serve` 冷启动（导入 pydantic）会超过 8s，脚本必须**轮询就绪**而不是
固定 sleep；② 首版探针把 key 生成在另一个 shell 里，mcp-serve 拿到的 key 与 serve 不一致
→ `create_run` 返回 `{"error":"Unauthorized","status":401}`；「401 但 initialize 成功」
正是协议面与业务面分别鉴权的表现，值得在排障手册里留一句。

#### B4 Playwright MCP → 用系统 Edge 代替 Chromium

`npx -y @playwright/mcp@latest`（0.0.85，server `Playwright 1.66.0-alpha-2026-11-09`）
`initialize` 通过、`tools/list` **25 个**浏览器工具。三个障碍逐一绕过：

1. 本机没装 Chrome，`npx playwright install chromium` 下载失败（`Failed to download
   Chrome for Testing … code=1`，网络受限）→ 改用系统自带 Edge：`--browser msedge`
   （`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` 存在，157.0）；
2. Playwright MCP 默认**拦截 `file:` 协议** → 用 `python -m http.server 18095
   --bind 127.0.0.1` 起本地页，导航 `http://127.0.0.1:18095/probe.html`；
3. 首版探针「没命中标题也打印 PASS」是**假绿** → 改成命中失败即 `process.exit(1)`。

最终输出：`Page Title: AgentCrew 探针页面`、`browser_snapshot 命中标题=true · 长度=325`、
`playwright interop PASS`。

#### 默认分支长期落后 → fast-forward + 自动同步 workflow

`main` 停在 2026-11-09 的 `7d46321`（491 文件），`master` 已是 `de60f07`（582 文件）。
「clone 拿到旧代码」这种错没人会注意到，因为两个分支都在 CI 里触发、红绿一样。

- 想改仓库默认分支：`gh api -X PATCH … default_branch=master` → **403**
  （token 无 admin 权限）；
- 退而求其次且不重写历史：`git rev-list --left-right --count origin/main…origin/master`
  显示 main 侧的独有提交为 **0**，于是 `git push origin master:main` 做成
  **fast-forward**（`7d46321..de60f07`），两个分支现指向同一 commit；
- 再加 `.github/workflows/sync-main.yml`：master 每次推送后，若 `main` 能快进就自动
  推平；若 main 上出现 master 没有的提交（有人直接往 main 提交），**显式失败**并列出
  那些提交，交给人合并——静默跳过比失败更坏。

#### B5 RAG 命中率对照 → 语料和问题集都能自建

缺的是「有标注的问答对」，不是「真实文档」。做法：`scripts/rag_hitrate.py` 用仓库
自己的 `docs/*.md`（32 篇 / 1233 块）当语料，用每篇的二/三级标题当查询、标题所在
文档当答案（486 条），在临时目录建索引，只改 `vector_weight` 做对照：

| 权重 | hit@1 | hit@3 | MRR | 未命中 |
| --- | --- | --- | --- | --- |
| **0.0（纯 BM25）** | **84.4%** | **95.7%** | **0.897** | 3.7% |
| 0.5（旧默认） | 82.9% | 94.0% | 0.885 | 3.9% |
| 1.0（纯向量） | 53.3% | 77.2% | 0.655 | 15.4% |

**哈希向量在这里是噪声而不是补充**，所以不能只写进报告：

| 改动 | 说明 |
| --- | --- |
| `agentcrew/knowledge/embeddings.py` | 新增 `is_semantic_embedder()`，识别 `HashEmbedder` 与 `CachedEmbedder(HashEmbedder)`（缓存外壳骗不过去） |
| `agentcrew/crew/crew.py` | `_docs_effective_weight()`：只有兜底通道时把**实际**权重压到 0，并 emit `docs_vector_degraded`（含配置值、实际值、原因）——降级可见，不静默 |
| `agentcrew/config.py` | `docs_rag_vector_weight` 的 docstring 补上这条实测与降级行为（默认值本身不改：配了真端点时 0.5 是对的） |
| `tests/test_docs_rag.py` | 新增 2 例：通道形态识别；Crew 层「哈希 → 0 + 留事件」「真语义 → 照配置、不留事件」 |
| `bench/rag-hitrate-2026-11-13.md` | 方法、结果、处置、以及**指标边界**（标题即查询偏乐观；不能外推为「真 embedding 也无用」） |

#### B2 vision 仍是唯一真外部依赖

`.env` 里 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 为空串，`ARK` / `DASHSCOPE` /
`QIANFAN` 是占位短串（len 11 / 6 / 13）；`providers --json` 中仅 `deepseek`
`key_configured=true` 且 `supports_vision=false`。通道本身已由
`tests/test_multimodal.py` 覆盖，拿到任一多模态 key 即可 `run --image` 收工。
这条不再反复尝试，写进 `docs/DEPLOY-CHECK-2026-11-13.md` §5。

#### 验证

`tests/test_docs_rag.py` + `test_capability_config.py` + `test_crew.py` **76 passed**；
`tests/test_sandbox_backend.py` + `test_docker_isolation_live.py` **19 passed / 4
skipped**（本机无 docker，skip 是正确结果）；`ruff` 全绿、`mypy` 139 文件零错误。

**全量回归**：`scripts/test.py --batch 2 --retries 4 --retry-failed 3`（168 文件 /
84 批次 / 459.1s）→ **3246 passed · 0 failed · 6 skipped · 0 error**，崩溃重试恢复
10 个。跑器列出 4 条「首次失败、重试通过（未证实为环境）」的待核项，已逐条人工复核：
`test_exec_verify::test_verify_replaces_stale_workspace` 与
`test_cli_deferred_approval_roundtrip` 两个文件单独连跑 **4 次**——3 次 25 passed、
1 次宿主进程崩溃（`-1073741819`），无断言失败复现，确认属 §6 环境噪声而非回归。
CI：run `43399398936` 七个 job 全 success。

---

### 3.74 D5② 收口：`rejected_by_gate` + 目标机自检脚本（2026-11-13）

**D5② 的问题**：驳回类用例（「交付 0 字节文件」）本来就该失败，但
`run_status: failed` 分不清两件事——**门禁真把不合格产物拒了**，还是**流程在规划期
旁路崩了**。两者在报表上长得一模一样，等于这条验收证据是虚的。

证据其实一直在 ledger 里，只是没有断言类型去读：`reject-empty-json` 是
`plan_rejected` 两次（「T2 验收标准要文档但交付物无文档产物」「产物路径冲突」），
`reject-empty-py` 是 `gate_result` 三轮 `verdict=reject`（`issues: ["src/empty.py
为空文件"]`）。

| 改动 | 说明 |
| --- | --- |
| `agentcrew/evals/checks.py` | 新增 check kind **`rejected_by_gate`**：`stage: plan|gate|any`（默认 any）+ 可选 `reason_contains`。**只认拒绝证据**（`plan_rejected` 事件、`gate_result` 且 `verdict=reject`）；`output.error` 非空、状态不是 `failed`、没有任何拒绝证据，三者任一都判失败并写明原因 |
| `_evaluate` 求值顺序 | 该类型先于「error 一律失败」的短路求值——否则驳回 case 永远读不到证据（这正是 D5② 卡住的机制原因） |
| `examples/evals/capability-live.jsonl` | `reject-empty-json` 加 `{"stage":"plan"}`、`reject-empty-py` 加 `{"stage":"gate","reason_contains":"为空文件"}`（`run_status: failed` 保留） |
| `tests/test_evals_2026_11_08.py` | 新增 4 例：规划期证据 / 任务层证据 + 理由匹配 / **崩了≠被拒**（error、无证据、completed 三种反例）/ stage 与 reason 的负例 |
| 验证（零成本） | `scripts/regrade_eval.py … --since 2026-11-13T00:00` 用盘上既有证据重算：`reject-empty-json` **2/2 PASS**、`reject-empty-py` **2/2 PASS**（不重新调用任何模型） |

**目标机自检脚本**：§4 那 10 项此前是一张表，人照着敲。「敲过了」与「真的过了」在
记录上没区别——所以 `scripts/deploy_check.py` 把能自动判的做成 `PASS/FAIL/SKIP`
三态，判不了的（反代细节、告警规则、密钥托管、webhook 幂等）明确 SKIP 并打印人工
判据。覆盖：REST 认证（带 key 200 / 无 key 401）、`/metrics` 含 `agentcrew_up`、
TLS 证书剩余天数、数据盘是否独立（`st_dev`）、`max_cost_usd`、日志脱敏抽查、
备份恢复演练（备份→异目录还原→sha256 比对）、容器挂卷 + HEALTHCHECK。退出码有
FAIL 为 1，可直接接进部署流水线。开发机实跑：**4 PASS / 2 FAIL / 6 SKIP**，两条
FAIL 是真实状态（产物与代码同盘、未配成本上限），不是脚本坏了。
`tests/test_deploy_check.py` 6 例钉死「没验不等于通过」（缺前置条件必须 SKIP）。

**验证**：`tests/test_evals_2026_11_08.py` 45 passed、`tests/test_deploy_check.py`
6 passed；全量 `scripts/test.py --batch 2 --retries 4 --retry-failed 3`（169 文件 /
85 批次 / 432.6s）→ **3256 passed · 0 failed · 6 skipped · 0 error**。跑器列出 2 条
「首次失败、重试通过（未证实为环境）」待核项（`test_cli_deferred_approval_roundtrip`
/ `test_e2e_sample_run_with_tools_enabled`），两个文件单独连跑 **4 次 37 passed**，
未见断言复现，确认环境噪声。

---

### 3.75 收尾（2026-11-13）

交付说明收在 **`docs/CLOSURE-2026-11-13.md`**：状态数字、提交清单、验收方照着跑
的入口命令、剩余外部依赖、以及环境噪声判据。此处只登记结论，不重复细节。

- `docs/OPERATIONS.md` §8 上线检查清单已改为「**先跑 `scripts/deploy_check.py`，
  再对照清单**」，判不了的 4 项由脚本明确打印 SKIP + 人工判据；
- `docs/REMAINING-2026-11-13.md` 总览已同步：A 闭环、B 类 5 项里 4 项闭环
  （B1/B3/B4/B5）仅剩 B2、D 类评测口径两项均闭环；
- 我起的后台进程（`serve:18090` 探针、`http.server:18095` 探针页）已清理，不留给
  用户当「下一个环境的坑」。

---

### 3.76 B2 多模态真实端到端闭环（2026-11-13）

用户提供智谱 key 后，**「截图→改前端」已用 GLM-4.5V 真实跑通**，不再是外部依赖。

- 直连探针：`glm-4v-flash` 与 `glm-4.5v` 都能正确读出登录页截图的标题、输入框和
  蓝色按钮；最终选 GLM-4.5V，给 HTML 与 reasoning tokens 留输出预算。
- 最终 run `20261113-194447-e27c`：**4/4 任务成功、0 返工、14 次真实 GLM-4.5V
  调用、6m11s、估算 $0.06**；`images_attached{count:1,total_bytes:15177}`，
  **`vision_unsupported=0`**。
- 产物 `index.html` 保留标题/账号/密码框，按钮改为 `#22c55e`（hover `#16a34a`），
  新增「忘记密码？」链接；Edge headless 二次截图后人工比对通过。
- 两个配置边界已写进 `examples/agentcrew.vision.example.yaml` 与
  `docs/VISION-2026-11-13.md`：①`.env` 的 `AGENTCREW_DEFAULT_MODEL` 会覆盖 YAML
  默认模型（DeepSeek 对照组产生 6 条 `vision_unsupported`，不算通过证据）；
  ②`glm-4v-flash` 的 `max_tokens` 上限为 1024，完整 run 会 HTTP 400，需换模型并
  配置 `max_output_tokens`。
- 非阻断观察：模型把小字「B2 探针」误读为「62 探针」；不在本次验收项内，但严格
  UI 复刻任务应单独验收小字 OCR。

至此 `docs/REMAINING-2026-11-13.md` 的 **B 类 5 项全部闭环**；剩余上线工作只有
真实目标机及运维配置。

---

### 3.77 云主机真实部署验收：10 项中 7 项闭环（2026-11-13）

云主机不再只是「新鲜克隆冒烟」环境，已实际运行 **回环 serve `127.0.0.1:8765` +
nginx TLS 反代 `https://localhost:18443`**。完整证据与复跑入口见
`docs/DEPLOYMENT-VM-2026-11-13.md`，可复用配置收在 `deploy/`。

- 自动自检最终 **6 PASS / 1 FAIL / 5 SKIP**：REST 认证、metrics、TLS、成本上限、
  日志脱敏、备份恢复通过；唯一 FAIL 是数据目录与仓库同盘。备份覆盖 3 run / 6 文件。
- 四个人工 SKIP 已专项补齐：SSE 经反代实际流出 4,567 字节且带
  `X-Accel-Buffering: no`；两条 Prometheus 告警用真正 promtool 2.55.1 测得触发与
  不触发均 SUCCESS；webhook 真实接收 2 个事件后按原 delivery id 重投，
  `processed=2 / duplicates=1`；密钥文件 0600 且 key 不在 ps / 快照 / 运行文件。
- 验收抓到并修复两个真缺陷：`19a55c5` 让 TLS 自检尊重 URL 显式端口；
  `af3336d` 让 `config_snapshot.api_keys` 只落 `***`。后者不是理论审查，而是实际
  在 `run.json` 抓到明文后修复；云主机已轮换临时 key 并用 run
  `20261113-202149-4fe3` 复验。
- 剩余 **3 项硬阻塞**：独立数据盘、容器挂卷/healthcheck、Docker 下
  `g1_exec_check`。没有用 tmpfs/loopback 冒充独立盘，也没有把 CI 的 Docker 证据
  冒充目标机 g1 验收。
- 验收实例只监听回环地址、使用本地测试 CA；公网正式证书、Alertmanager、外部日志
  聚合和 systemd 启用仍是生产切换动作，未写成已完成。webhook 测试接收端已停止，
  serve 与 nginx 保留供复核。

---

### 3.78 状态复核：以当前现场重跑为准（2026-11-13）

本次不是按历史结论抄状态，而是分别重查 Windows 仓库、GitHub Actions 和云主机：

- 全量测试重新执行 `scripts/test.py --batch 2 --retries 4 --retry-failed 3`：
  **3258 passed / 0 failed / 6 skipped / 0 error**，169 文件 / 85 批次 / 509.6s。
  跑器报告 3 个批次进程崩溃后重试恢复；没有输出“首次失败、重试通过”的待核项。
  这里只记录跑器事实，不额外推断三次进程崩溃的根因。
- 静态检查重新执行：ruff `All checks passed!`；mypy `139 source files` 零错误。
- `git fetch` 后 `HEAD`、`origin/master`、`origin/main` 均为 `fd455ca`，工作区干净。
  CI run `43425986756` 的 7 个 job 逐一查询均为 success；Sync main run
  `43425986731` 为 success。`19a55c5` 的 CI 曾被后续提交取消，不写成成功；
  其代码由后续 `af3336d`、`fd455ca` 的成功 CI 覆盖。
- 云主机仓库已快进到 `fd455ca`。`127.0.0.1:8765` 与 `127.0.0.1:18443`
  仍在监听，webhook 测试端口 18096 未监听。环境文件权限仍为 0600。
- 云主机重新执行 `deploy_check.py`，结果仍是 **6 PASS / 1 FAIL / 5 SKIP**，
  退出码 1；唯一 FAIL 仍是数据目录与仓库同挂载点。备份恢复仍为 3 run / 6 文件。
  原始输出为 `logs/deploy-check-reverify.txt`。
- 数据目录和仓库经 `findmnt` 均位于 `/home/user` 的 `fs-913d972175601020`
  （`hpvs_fs`）；`docker` 可执行文件不存在。因此独立数据盘、容器挂卷/healthcheck、
  Docker 下 `g1_exec_check` 仍不能判通过。
- promtool 2.55.1 重跑：`check rules` 识别 2 条规则，`test rules` 为 SUCCESS。
  SSE 证据文件仍为 4,567 字节，响应头含 `Content-Type: text/event-stream` 与
  `X-Accel-Buffering: no`。
- webhook 原始 `webhook-requests.jsonl` 重新解析为 3 条记录、2 个不同 delivery id，
  duplicate flags 为 `[false, false, true]`，即处理 2 次、重复拦截 1 次。
  修复后的 run `20261113-202149-4fe3` 快照字段重新读取仍为 `api_keys=['***']`。

本次没有重跑真实模型评测、RAG 对照、MCP/Playwright 或 Studio 分项测试；这些仍按
各自报告作为历史证据，不包装成本次新结果。当前 CI 的 Studio 与 Docker 真隔离 job
已重新确认成功。

---

### 3.79 R59：规划期灵活指派执行体——任务分好后任意指派，不写死角色（2026-11-13）

**起因**：用户愿景是「把需求拆成任务后，**每个任务灵活指派给任意执行体**」（例：豆包管
文本/图、Claude Code 管代码、Codex 统筹，但**不固定**）。此前 PM 拆解只产出 `role`，
执行体只能靠角色默认绑定或运行时人工覆盖（`set_task_override`），规划期无法自主指派。
R59 把「指派」前移到规划期，复用既有 `agent_for_task` 派发机制，**确定性引擎不变**
（PM 的选择是数据，引擎按固定优先级解析，调度决策全程无 LLM）。

| 改动 | 说明 |
| --- | --- |
| `agentcrew/task/task.py` / `task/planner.py` | `Task.agent` / `TaskDraft.agent`（默认空=沿用角色默认）；`to_tasks` 映射；进 JSON Schema；`_EDITABLE_TASK_FIELDS` 加 `agent` |
| `agentcrew/roles/binding.py` | `AgentProfile.description` / `capabilities`（给 PM 看的能力说明，非硬路由约束），纯数据层向后兼容 |
| `agentcrew/agents/project_manager.py` | 池非空时 PM 系统提示词追加「可用执行体」菜单（`_agent_menu`）；池空时提示词**逐字不变** |
| `agentcrew/task/planner.py` | `validate_plan(..., profiles=)` 校验指派必须存在于池；**池为 None 或空=默认关闭，跳过校验**（绝不因 PM 偶然吐 agent 打回计划） |
| `agentcrew/agents/registry.py` | `agent_for_task(..., assigned=)` 优先级「运行时人工覆盖 > 规划指派 > 角色默认」；失效指派记 `task_agent_assignment_dropped` 并回退角色默认 |
| `agentcrew/crew/scheduler.py` | 派发传 `assigned=task.agent`；`task_dispatched` 留痕 `agent`（审计）；动态子任务 spec 透传 `agent` |
| `agentcrew/api/dashboard.py` | 运行看板任务卡显示指派执行体（`→ key`，空不显示） |
| `examples/agentcrew.heterogeneous.example.yaml`（新增） | 异构池示例：planner/coder/writer 三执行体 + 角色默认绑定，注释标明 R60/R61 前瞻位 |

**默认关闭与四通道**：不配 `agents.profiles` 时菜单为空、`agent` 恒空，行为与 R59 前逐字
一致；空池即便带 agent 也只优雅降级。`description`/`capabilities` 为 pydantic 字段，
YAML/env/export-import/API 四通道自动往返（已实测）。密钥仍只走环境变量。

**顺带修复一个既有前端缺陷（非 R59 引入）**：`dashboard.py` 的 `.join("\n")`/`.split("\n")`
处在非 raw Python 串里被解析成**真实换行**，注入浏览器即 JS 语法错误，导致**整块看板脚本
不执行**（函数全 undefined、看板列不生成）。改为 `\\n` 转义（4 处）后脚本恢复。已用浏览器
实测：修复前 `updateTask` 等全 undefined，修复后全部 function、看板列生成、指派行正确渲染。

**验证**：`tests/test_task_assignment.py` **21 例**（数据/校验/派发/提示词/委派/默认关/派发留痕）；
R59 触及面合并回归 **387 passed**（planner/crew/scheduler/binding/architecture/config/
delegation/subtask/checkpoint/dashboard/api）；ruff 全绿、mypy 139 文件零错误；
异构示例配置经 `load_yaml_config`+`Settings` 实测加载与往返；看板指派行浏览器实测
（指派显示 `→ coder`、未指派不显示）。**未做**：端到端 eval case（eval runner 的
`model_copy` 不 coerce 嵌套 `agent_profiles`，属既有基础设施问题，不在本轮扩scope）。

**后续**：R60 把外部 coding CLI（Claude Code/Codex）接成真 agentic 执行体（cli provider
以 run 工作区为 cwd 自主多步 + 改动文件桥接产物）；R61 补图像生成工具 + 二进制产物通道。
两者落地后异构示例的前瞻位即可启用。

---

### 3.80 R60：外部 coding CLI 接成真 agentic 执行体（2026-11-13）

**起因**：R59 让规划期能灵活指派执行体，但「Claude Code 管代码」这类愿景此前落不了地——
既有 `cli` provider 只是**一次性 stdin→stdout 桥**（把 prompt 喂进去、拿一段文本回来），
不会自主多步读写文件，算不上 agentic 执行体。R60 补上这一层，且**默认关闭**：不显式选用
agentic 别名时，所有既有路径逐字不变。

| 改动 | 说明 |
| --- | --- |
| `agentcrew/utils/workspace.py`（新增） | run 工作区 contextvar（零依赖，同 `utils/trace` 模式）：crew 写入、provider 读取作 cwd |
| `agentcrew/models/providers/cli_agentic.py`（新增） | `AgenticCliProvider`：cwd=run 工作区（缺失明确报错，绝不退回服务进程 cwd）、claude-json / codex-jsonl / plain 三种结构化解析、用量抽取（缺失回退启发式）、可配置超时（默认 900s）、继承 cli 的「先切分后替换、不经 shell」加固 |
| `agentcrew/models/config.py` | `ProviderPreset`/`ModelBinding` 加 `cli_agentic`/`cli_output`/`cli_timeout_seconds`（pydantic 四通道自动往返）；新增 `claude-code`/`codex` 预设，命令模板自带安全权限默认（acceptEdits / workspace-write） |
| `agentcrew/models/factory.py` | `implementation=="cli"` 且 `cli_agentic` 才构造 `AgenticCliProvider`；`is_agentic_cli()` 以**解析出的 Provider 类型**为准（mock 通道恒 False） |
| `agentcrew/crew/crew.py` | run 入口 `set_workspace(run_dir/workspace)`、finally `reset_workspace`；只声明路径不建目录（非 agentic run 零 IO、布局不变） |
| `agentcrew/agents/base_agent.py` | `parse_output` 桥接：agentic 别名 + 有工作区时改从工作区**按契约**读回交付物（只读声明路径 + 二次防逃逸），CLI 总结文本作 notes；缺文件→必需产物缺失走返工；非 agentic/无工作区逐字走围栏解析 |
| `examples/agentcrew.heterogeneous.example.yaml` | 补 claude_code/codex 可选执行体注释块（需本机装 CLI 并登录） |

**安全与默认关闭**：权限/沙箱旗标写在预设命令模板里；cwd 隔离到 run 工作区；未注入工作区
时 agentic provider 明确报错而非乱写；mock 通道下 `is_agentic_cli` 恒 False（离线回放仍走
围栏）。密钥仍只走环境变量（Claude Code/Codex 自带认证，平台不持有其密钥）。

**验证**：`tests/test_cli_agentic.py` **26 例**（纯解析 / cwd / argv / stdin / 用量 / 超时 /
错误收敛 / 四通道 / 预设安全默认 / 工厂选择 / mock 覆盖 / 工作区桥接四态）；
`test_model_config.py` 预设清单同步 +2；引擎默认关回归 **~410 passed**（crew/scheduler/
binding/assignment/config/delegation/subtask/checkpoint/cli/provider/architecture）；
ruff 全绿、mypy 141 文件零错误。**真机证据待补**：本机 Claude Code headless 冒烟此前报
`connection_failed`（网络/代理阻断，同 git push 受阻一类），故 agentic 真跑证据留待网络恢复
或 CI；离线 mock 已覆盖装配与解析全链路。

**后续**：R61 补图像生成工具 + 二进制产物通道（`ArtifactStore` 现仅 str，需 bytes 通道与
image kind），落地后异构示例的 image 前瞻位即可启用。

---

### 3.81 R61：图像生成工具 + 二进制产物通道（2026-11-13）

**起因**：用户愿景里「豆包管图画生成」此前完全缺位——平台只有文本产物通道
（`ArtifactStore.commit` 仅 str、`ArtifactKind` 无 image），Agent 既不能产图、产了也
落不成受契约保护的交付物。R61 补齐「产图 → 二进制落盘 → 门禁 → 预览」整链，默认关闭。

| 改动 | 说明 |
| --- | --- |
| `agentcrew/tools/image.py`（新增） | `generate_image` 工具 + 可插拔 `ImageProvider`（ark Seedream / openai / mock），密钥只走环境变量、端点写死公网 https 不跟随重定向、落盘路径过 `safe_relpath` 限工作区；mock 回放固定 PNG 离线可测；`image_generated` 留痕（含 size/mime/provider/cost） |
| `agentcrew/artifacts/spec.py` | `ArtifactKind` 加 `image`；新增 `BINARY_KINDS`（二进制 kind 集合，无围栏语言名） |
| `agentcrew/artifacts/store.py` | `commit` 重构为薄包装 + 共享 `_commit_data`；新增 `commit_bytes`（字节原样落盘，共用校验/冲突/清单/事件） |
| `agentcrew/gate/static_checks.py` | G1 对 `BINARY_KINDS` 只判尺寸下界（`_binary_content_findings`），跳过文本解析与 `must_contain` |
| `agentcrew/agents/base_agent.py` | 二进制桥 `_workspace_binary_files`：**不**要求 agentic CLI（generate_image 是通用 ReAct 工具），只要注入工作区且契约声明二进制 kind 即读回字节；`validate_output` 计入、`_commit` 走 `commit_bytes` |
| `agentcrew/config.py` + `crew/crew.py` | `image_provider`/`image_timeout_seconds`（YAML/env/快照四通道，默认空=关）；crew 仅在配置时注册 `generate_image`，不可信输入下不下发 |
| `studio/main.js` + `src/features/{artifacts,result}/index.js` | 图像产物预览：`readArtifact` 对图像扩展名回 base64（≤2MB），渲染端用 `<img src=data:…>`（不经 innerHTML，无注入面） |
| `examples/agentcrew.heterogeneous.example.yaml` | image 执行体与 `image_provider` 开关改为「已落地、可选启用」注释 |

**默认关闭与安全**：不配 `image_provider` 就没有 `generate_image`（模型看不见）；密钥绝不
落盘；二进制产物仍过跨任务冲突/路径逃逸/`max_bytes` 护栏；G1 尺寸判据保留（欠尺寸走返工）。

**验证**：`tests/test_image_tool.py` **17 例** + `tests/test_image_artifacts.py` **13 例**
（提供方工厂/密钥 env/b64 解析/落盘与路径安全/错误收敛/commit_bytes 上限与冲突/G1 image
三态/非 agentic 二进制桥/门控默认关与不可信只读/四通道）；`test_artifact_parser.py` 语言表
覆盖改为「文本 kind 全覆盖 + image 属二进制」；受影响面回归 **~290 passed**（parser/store/
static/gatekeeper/config/agent/cli_agentic）；ruff 全绿、mypy 142 文件零错误；Studio
`npm run check` 73 模块/42 分片 + `test:unit` 18/18 通过（electron e2e 本轮未重跑）。
**真机证据待补**：真实产图需 ARK/OPENAI Key 与网络，离线 mock 已覆盖全链路。

至此 R59（规划期灵活指派）/ R60（coding CLI agentic 执行体）/ R61（图像生成 + 二进制通道）
三批全部落地，用户「拆解 → 灵活指派任意执行体（文本/代码/图像/统筹）→ 汇总」的愿景在
配置与代码层面闭环；真实模型/真机证据留待网络恢复或 CI。

---

### 3.82 R59–R61 契约哨兵同步：4 条硬编码守卫按新事实更新（2026-11-13）

**起因**：R59–R61 落地时只做了受影响面子集回归，没有跑全量；补跑全量后
**3338 passed / 4 failed**，四条既有哨兵仍按旧事实断言。这不是随机失败，正是
「新增能力必须被主动决定」的守卫在起作用。

| 失败用例 | 原因 | 处置 |
| --- | --- | --- |
| `test_contract::test_every_kind_is_explicitly_classified` | `image` 既不在结构解析器、也不在「无结构」名单，`KIND_PARSERS["image"]` 静默落到 `_always_ok` —— 正是该哨兵要拦的降级 | `contract._NO_STRUCTURE_KINDS` 显式加入 `image`（二进制无文本结构，G1 走 `BINARY_KINDS` 尺寸判据）；模块 docstring「其余 8 个」改 9 |
| `test_models::test_artifact_kind_has_exactly_twelve_values` | `ArtifactKind` 由 12 增至 13 | 更名 `..._thirteen_values` 并补 `image`；顺带把 `test_kind_parsers_covers_all_twelve_kinds` 改成不带计数的 `..._all_kinds`，避免下次再漂 |
| `test_provider_metadata::test_all_presets_load` | 预设由 27 增至 29（R60 新增 `claude-code` / `codex`） | 断言、docstring、分节注释同步为 29 |
| `test_provider_presets_v5::test_presets_have_api_key_url_when_expected` | 两个新预设是 CLI 预设：认证由 CLI 自带，平台不持有密钥 | 加入 `_NO_API_KEY_URL` 白名单并注明原因 |

**性质区分**：第一条是**真缺口**（新 kind 的校验方式没被决策，靠 `.get` 默认值滑过；
实际影响被 G1 的二进制尺寸校验兜住）；后三条是能力扩展后按新事实更新哨兵，**门槛未放宽**。

**验证**：四条用例单跑与四文件合跑 **266 passed**；`ruff` **All checks passed**；
`mypy` **142 文件零错误**；全量 **3342 passed / 0 failed / 6 skipped / 0 error**
（173 文件 / 87 批次 / 444.1s，`--keep-pycache`）。跑器标 2 项「首次失败、重试通过」，
已人工核实为环境负载：`crew.py` 报的瞬时解析错误 `py_compile` 通过、
`test_run_tests_passes` 隔离连跑 5 次全过，均非回归。文档同步：§0 指标与 §2 预设数
（27→29）。**R59–R61 仍未提交**，本轮未推送。

---

### 3.83 R61 真机端到端：三个只有真跑才暴露的缺陷（2026-11-13）

**做法**：用真实模型（`deepseek:deepseek-chat`）+ `image_provider=mock`（离线字节）跑完整链路，
命令 `main.py run --flow examples/flows/image-e2e.yaml`（固定契约，避开 PM 随机阈值）。第一次真跑就把单测覆盖不到
的三处打断点全暴露出来，逐个修完后 **2/2 任务成功、G1/G2 全过**。

| # | 现象（真实输出） | 根因 | 处置 |
| --- | --- | --- | --- |
| 1 | 产图成功、`artifact_written` 70 字节，随后 `gate.evaluate` 抛 `UnicodeDecodeError: 0x89` | `Gatekeeper._run_extended_checks` 对所有 kind 都调 `store.read()`（UTF-8 解码）；同类调用还散在 G2 评审、上下文组装、团队评审、摘要器——R61 只接了 G1 | 新增 `ArtifactStore.kind_of` / `read_bytes` / `read_for_prompt`（二进制返回一行元数据，不解码）；扩展检查**显式跳过** `BINARY_KINDS`；7 处提示词/评审读取点改走 `read_for_prompt`；多执行体草稿的合并/晋升改走字节通道 |
| 2 | 模型用 markdown 围栏「交付」PNG，或干脆不调工具，任务报「缺少必需产物」 | 执行体提示词只有「每个交付物用一个围栏代码块输出」，二进制产物没有对应指令 | 新增 `_BINARY_ARTIFACT_SECTION`：**仅当**任务确有二进制交付物且 `generate_image` 真的可用时注入，明确「调工具、path 逐字一致、不要再写 `file=` 围栏」；工具不可用时提示词逐字不变（不承诺假能力） |
| 3 | 模型输出 `<｜｜DSML｜｜ calls>` 私有标记，该轮被判「没有工具调用」直接退出，报告只显示缺产物 | `deepseek-chat` 在未开原生 tools 时会漏出自家标记；解析器只认 `<tool_call>` | `_extract_tool_calls` 增加同义解析（invoke/parameter，支持整包 `args` JSON 与逐参数两种形状）；不改对外协议、不动其他 provider |

**通过证据**：run `20261113-232015-e726`（flow `image-e2e`，`min_bytes=1`）——
任务 2/2 成功、G1 `approve`、G2 `approve`（score 0.8，suggestions 还指出「70 字节对插画偏小」，
说明评审真的看到了尺寸）、T-FINAL 亦 approve；`MANIFEST.json` 条目
`kind=image / size_bytes=70 / sha256=497790…1581` 与磁盘文件逐字节一致（PNG 魔数校验通过）。

**边界（不包装成已验）**：图像字节来自 `MockImageProvider`（1×1 PNG），**真实产图仍需
ARK / OpenAI key**——`.env` 里 `ARK_API_KEY`、`OPENAI_API_KEY` 均为空（只有中文注释），
本轮未做真实出图。链路（工具注册 → 模型调用 → 字节落工作区 → 二进制桥 → G1 → G2 →
MANIFEST → Studio 预览）已真跑；像素内容尚未真验。

**另一条不是缺陷的现象**：先用 `--goal` 自由规划时 PM 给图片定了 `min_bytes=1024`，
mock 的 70 字节被 G1 判「为空文件」——这是契约判据正确生效，不是 bug；改用固定流程
（`min_bytes=1`）才拿到链路证据。

**回归**：新增 `tests/test_dsml_tool_parsing.py` 6 例、`test_image_artifacts.py` +4 例、
`test_agent_tools.py` +2 例；全量 **3354 passed / 0 failed / 6 skipped / 0 error**
（174 文件 / 87 批次 / 508.7s）。跑器标 3 项「首次失败、重试通过」，已人工核对均为环境：
`test_sdk` 端到端（`serve` 子进程 `0xC0000005`）、`rich` 源码瞬时语法损坏、
`test_webhook_spool` 端到端；前两项隔离复跑 2/2、webhook 1/1 全过，非回归。

### 3.84 R62：run 时间旅行（回滚到任意任务并重跑）（2026-11-14）

**起因**：对标主流 Agent 产品——Claude Code 的 `/rewind`、LangGraph 的 time travel、
Devin 的 replan 都允许「不改整条历史、只从某一步重来」。AgentCrew 此前只有 `resume`：
它把**中断**的 run 接着跑完，失败任务仍然失败、已成功的产物无法在保留上游成果的前提下
重做；想改一个中间任务的交付，只能整个 run 从头再跑（重花全部 token）。本轮补上这一层，
其余不动（不碰调度、不碰状态机的合法边集合）。

| 改动 | 说明 |
| --- | --- |
| `agentcrew/crew/rewind.py`（新增） | `affected_task_ids`：影响集 = 依赖图传递后代 ∪ `parent_id` 子任务（运行时动态委派不进 DAG）∪ `T-FINAL`；`rewind_run`：状态守卫 → `apply_deltas` → 影响集 → 快照 → 删产物 → 重置 → 落盘 → 审计；`dry_run` 只读不算写 |
| `agentcrew/task/task_board.py` | 新增 `reset_for_rewind`：**第二处**显式绕过状态机（第一处 `reset_failed`），补 `reason="rewind"` 的 `StatusChange`；`attempts`/`rework_count` 归零、`result`/`reviews` 清空、`status_history` 只增 |
| `main.py` | `rewind <run_id> --task ID [--instruction TEXT] [--dry-run] [--json]`，退出码与其它维护命令同表 |
| `agentcrew/api/server.py` | `POST /runs/{run_id}/rewind`（与 CLI 同一实现）：running→409、body 非法/任务不存在→400、无 settings→503、锁冲突→409 |
| `agentcrew/api/rbac.py` | 补登记 `POST /runs/{id}/rewind → runs:write`：漏登记会落到「登录即可用」（`required_scope` 返回 `None`），等于拿到只读主体的密钥就能改 run 状态与产物清单 |
| `artifacts/contracts/openapi.yaml` | 新路径 + `RewindRequest`/`RewindResponse`；`test_api_contract` 双向比对 + 真服务逐条探测 |
| `tests/test_rewind.py`（新增） | 17 例：影响集三形态与未知 id、只重置子树、快照逐字节 sha256、清单/磁盘一致性、`interrupted` 与 `run_rewound`、dry-run 零写盘、delta 陷阱、指令契约、rewind→resume 端到端、三种拒绝路径、CLI 接线、HTTP 端点两例 |
| `README.md` / `docs/TUTORIAL.md` §4.4 | 命令速查一行 + 行为说明一段 + 教程一节（先看影响集、再真回滚、后 resume） |

**四条不变量**（写在模块 docstring 里，也是用例的判据）：

1. **不改状态机**：终态回 `PENDING` 没有合法边，与 `reset_failed` 同样显式绕过，但历史里
   必须留下真实 `from_status` 与 `reason` —— 绕过要可见，不能伪装成正常跃迁。
2. **先快照后删除**：旧产物先复制到 `history/rewind-NNN/`（同目录 `rewind.json` 记影响集、
   旧任务快照与逐文件 sha256），复制失败就清掉半成品；此时 checkpoint 一个字都没改。
   （删除阶段遇到 `OSError` 如实抛出、不吞：任务状态与 `run.json` 尚未改写，重试即可。）
3. **不猜下游**：动态子任务只挂 `parent_id`、不进依赖图，漏掉它就会出现「父任务重做了、
   子任务还拿着旧结果」的脏状态。
4. **不吃旧增量**：`tasks.delta.jsonl` 里可能留着回滚前的任务快照，`resume` 会把它叠回
   `tasks.json` 之上、把刚重置的状态盖回去；动手前先 `apply_deltas()` 合并清空（专项用例
   直接伪造一份陈旧 delta 钉住这条）。

**语义边界**：只接受已停下来的 run（`running` / `awaiting_approval` 拒绝，提示先
`cancel` / 先裁决）；与 `resume` 共用同一把跨进程锁；不调模型、不发 webhook、不动审批单；
`--instruction` 同时写入目标任务描述与验收标准（G2 据此评审），只作用于目标任务。

**验证**：新增 17 例全过；受影响面回归 **245 passed**（task_board / state_machine /
architecture / cli / checkpoint / incremental_checkpoint / resume_consistency /
end_to_end_mock / dynamic_subtasks / static_checks）；`test_api_contract` 合跑 **95 passed**；
`ruff check` 改动文件 **All checks passed**；`mypy` 改动 3 模块 **零错误**；`test_rbac`
新增 `POST /runs/R-1/rewind → runs:write` 用例通过（漏登记会让写端点退化成「登录即可用」）；
全量 **3373 passed / 0 failed / 6 skipped / 0 error**（175 文件 / 88 批次 / 439.8s）。真机冒烟
（`linear` 样例）：completed → `rewind --task T-002`（影响 4 任务 / 移除 5 个产物 / 快照
5+1 文件 / run 置 `interrupted`）→ `resume` 重新 completed，产物集合与首次一致、任务契约
里保留新指令。

**边界（不包装成已验）**：① Studio 前端入口未做——工作区正有一批未提交的 V6-FE 重构，
此时改动 Studio 会撞车，留待下一轮；② 回滚后 `attempts` 归零意味着「重做」有完整预算，
若反复回滚同一任务会重复消耗预算，暂不做总额度审计；③ 全量回归已按上表口径跑完，
两项「首次失败、重试通过」隔离复跑全过；④ 并发会话的证据仅来自「同机两个测试跑器同时运行」
（`test_exec_verify` 在争用下偶发、隔离即过），不构成代码缺陷。

**未提交**：本轮改动留在工作区（`git status` 可见），未 commit、未 push；Studio 的一批
V6-FE 改动不属于本轮，未纳入。

---

## 4. 历史报告汇总（已并入本文，原文归档）

| 报告 | 时间 | 核心结论 | 现状 |
| --- | --- | --- | --- |
| **验收报告 R44–R49** | 2026-11-10 | 六项交付（复核治理 / 熔断 / 成本路由 / 门禁归因 / A/B 矩阵 / 分工依赖）**全部通过**；2933 passed（5 失败归因环境）；真模型 3 次跑批 $0.37，复核误判 4→0 | 见 `docs/ACCEPTANCE-2026-11-10.md`；遗留：前端未验、评测数据单薄、复核不参与任务状态 |
| **结项验收评审报告** | 2026-10-11 | 综合 **75.3 分**，结论「限期整改后复验」；3 个 P1 阻塞：①缺性能/稳定性/功能测试报告 ②预设样例仅 1/3 ③无拖拽编排画布。一票否决项均未触发；离线 9/9、API 10/10、真实模型 8/8 全通过 | ①②已由 R2 闭环（`bench/` + `linear`/`hierarchy`）；③仍在路线图（P2，8 人日）。遗留风险 R1–R8 见 §7 |
| **Studio 前端测试与 Bug 修复** | 2026-10-11 | 修复 3 个确定性 Bug：数字快捷键 8/9 与侧边栏顺序互换（P2）、模态打开时数字键仍切底层页（P1）、预算/调用次数/亲和 TTL 接受负值（P2）；修复后 selftest 16/16、e2e 28/28、probe 24/24，无残留进程 | 全部闭环，回归用例在 `studio/` e2e |
| **HANDOFF**（初版交接） | 2026-10-06 | SDD 会话完成 34 个任务实现/评审，并用真实 DeepSeek 跑 6 次端到端验收，每次暴露一个 mock 无法暴露的缺陷并修掉 | 已被 11-01 交接取代，结论并入本文 |
| **V7-HANDOFF** | 2026-10-10 | V7 增强落地：P0 数据脱敏（`redact.py`）、配额、循环检测等，21 项脱敏用例全绿 | 能力已并入 §2，P2.5 脱敏已实施 |
| **HANDOFF-2026-11-01** | 2026-11-01 | 自包含交接：架构、模块地图、命令、退出码、环境坑、13 项待办；声明"护城河是测试" | 本文继承其结论；待办见 §7 |
| **HANDOVER-BASELINE** | 2026-11-01 | 实测 1946 passed / 1 skipped；两个风险：Git 提交清单不完整（真实 168 文件而非文档所说 65）、真实模型 Key 失效；查明 segfault 实为 `PYTHONPATH` 污染而非解释器不稳 | Key 已恢复（`doctor` 6/6 通过）；提交随 R1–R22 推进（2026-11-07 已全量推送） |
| **V4 质量审查报告** | 2026-10-10 | 引擎侧修 6 项：密钥环境变量名硬编码、模型发现错误结果被缓存 60s 无法重试等 | 全部闭环 |
| **V4 深度审查报告** | 2026-10-10 | 追加 15 项（D-01…D-15）：`api_key_env` 形状校验、空 model id 收录、重名 provider、CSP、弹窗状态清理等，逐项独立 commit | 全部闭环 |
| **V4-FE 只读问题清单** | 2026-10-10 | 前端边界问题（深色模式 DAG 文字不可见、内联边框硬编码、主题缓存闪烁等），只读审查不改码 | 已由后续 F4–F7 提交修复 |
| **后端 / 前端重构说明** | 2026-11-01 | `factory.py` 2040→1794、`base_agent.py` 1184→1001、`crew.py` 1242→1178；`renderer.js` 3891→49 模块、`styles.css` 1503→32 分片 | 数据已并入 §3.3 |
| **4 份一次性工作提示词**（studio / zcode / ccswitch / studio-fe） | 2026-10 | 给编码代理的执行指令，任务均已执行完毕 | 无保留价值，已删除 |

---

## 5. 版本演进（测试数只增不减）

| 版本 | 内容 | 全量测试 |
| --- | --- | --- |
| V1 | 原提示词七条验收 | 7/7 验收；真实模型 8/8（$0.10） |
| V2 | 七项增强（上下文压缩 / 复杂度选模 / CLI 引擎 / 树状拆解 / HTTP API / MCP / Web UI） | 1286 |
| V3 | 三十四项增强（预算硬上限 / 并行评审 / 超时 / 健康监控 / 权重路由…） | 1632 |
| V4 | 供应商与模型管理（自定义 provider / 27 预设 / 模型发现 / 供应商页） | 1682 |
| V5 | 供应商模块深化（元数据 / 流式检测 / 搜索筛选 / CLI 增强） | 1776 |
| V6 | 借鉴 Magpie：故障转移 / 冷却矩阵 / RPM / 循环检测 / 预算软阈值 | 1810 |
| V7 | 脱敏 / 配额 / 多窗口限流 / 亲和性 | 1883（评审方实测） |
| R1 | 双端结构重构（契约层 + 前端模块化） | 1911 |
| R2 | 验收整改（样例 ×2 + bench + soak） | 1946 |
| 2026-11 | 六层审计定点加固 | 2018 |
| 2026-11-04 | run 生命周期完整性定点修复（见 §3.5） | 2028 |
| 2026-11-05 | 配置接线 / 路径域 / API 读链路 / 配额（见 §3.6） | 2043 |
| 2026-11-06 | 按板块补全：配置/CLI、HTTP API、引擎/工具、前端统一组件（见 §3.7） | 2083 |
| R3 | 对标同类框架的能力层完善：工具链生产化 / 入参强校验 / 两个内置工具 / 实体记忆（见 §3.8） | 2179 |
| R4 | 编排内核重构：条件分支 + 可跨进程恢复的人工审批（见 §3.9） | 2217 |
| R5 | 跨角色咨询接真调用：`BaseAgent.consult` + `PeerRouter` 配额/去重/身份绑定（见 §3.10） | 2233 |
| R6 | 协作深化：委派四道闸 + 咨询带上下文 + 咨询沉淀（见 §3.11） | 2254 |
| R7 | 主管审核：谁派的活谁验收（见 §3.12） | 2268 |
| R8 | 委派被拒原因回灌：SubtaskDispatcher + 一次纠正轮（见 §3.13） | 2277 |
| R9 | 跨任务层级：经理验收规划期任务（见 §3.14） | 2283 |
| R10 | 主管驳回后换人做（见 §3.15） | 2296 |
| R11 | 经验检索换 BM25 + 中文二元组（见 §3.16） | 2313 |
| R12 | 出站 Webhook：关键节点推给外部（见 §3.17） | 2334 |
| R13 | MCP 服务器接进生产工具链（见 §3.18） | 2346 |
| R14 | Webhook 多接收端 + 只推失败（见 §3.19） | 2358 |
| R15 | Webhook 持久化重放队列：入队前落盘、启动补投（见 §3.20） | 2376 |
| R16 | 官方 Python SDK + 示例：零依赖 HTTP/SSE 客户端（见 §3.21） | 2410 |
| R17 | run 生命周期加固：心跳 / 僵尸收敛 / resume 锁（见 §3.22） | 2434 |
| R18 | CI 就绪 + 限流窗口配置通道（见 §3.23） | 2444 |
| R19 | 主管 `suggestions` 通道（见 §3.24） | 2449 |
| R20 | 单步/断点调试（见 §3.25） | 2466 |
| R21 | API 契约守卫（见 §3.26） | 2490 |
| R22 | resume 白名单 + MCP 管道关闭（见 §3.27） | 2500 |
| R23 | 功能测试报告：覆盖矩阵（见 §3.28，无新增用例） | 2500 |
| R24 | 工具管理页与单工具试跑（见 §3.29） | 2519 |
| R25 | 多用户与 RBAC（见 §3.30） | 2548 |
| R26 | 拖拽式可视化编排（见 §3.31） | 2573 |
| R27 | 向量检索（见 §3.32） | 2584 |
| R28 | 从画布起跑（见 §3.33） | 2592 |
| R29 | 实体记忆向量化（见 §3.34） | 2597 |
| R30 | 审批单超时与告警（见 §3.35） | 2603 |
| R31 | 条件边否定/兜底（见 §3.36） | 2608 |
| R32 | 控制台 UI 重构（见 §3.37） | 2614 |
| R33 | 多级主管链（见 §3.38） | 2621 |
| R34 | 本机环境修复·崩溃隔离跑器（见 §3.39） | **2632（当前）** |

---

## 6. 改代码前必读

**行为契约类**（改动会破坏测试或语义）：

- **两级并发不得嵌套**：门禁必须在任务槽位释放后执行。守卫是 `test_scheduler_deadlock.py` 的结构性检查，**别删**。
- `bump_rework` 全仓只有 `Gatekeeper` 一处调用，删掉 → 返工回路无界。
- 人工闸门构造期强制 `ValueError`；**别在 `Crew` 里补默认 `NullPrompter`**。
- `has_placeholder` 三常量共同标定，长度单位是**信息量**（CJK ×3）非字符数。
- `ArtifactStore.commit` 路径比较必须**双侧**规范化。
- `reviews.json` 夹具的判断数必须等于该任务验收标准条数。
- P2-3 收敛检测默认关闭（`convergence_threshold=None`），开启会改变 e2e 向后兼容。
- `loop_guard_enabled` 默认关闭；`budget_warning_threshold` 未配 `max_cost_usd` 时完全空转。

**本轮新增的三条坑**：

- `_wait_window` 的锁引用是 `from asyncio import Lock as _AsyncLock`（直接绑定）：有测试把整个 `asyncio` 模块替换成只含 `sleep` 的桩，模块属性查找会拿到桩。
- `setup_logging` 纯文本模式**必须无格式前缀**（`setFormatter(None)`）：`test_logger_redaction.py` 逐字节比对 stderr。
- `TraceContext` 放在 `utils/`（零依赖层）：`models` 层禁止 import `runtime`，放错位置会被 `test_architecture.py` 拦下。

**R3 新增的两条坑**：

- 新增 `Settings` 字段要让它**在所有通道生效**，必须同时登记 `_YAML_SCALAR_KEYS`（YAML 顶层标量）、`_ENV_TO_FIELD`（环境变量）、`main.py` 的 `config export/import` 白名单、`api/server.py::_validate_run_options` 的 `allowed` 集合 —— 少一处就是「配了不生效」或「往返丢字段」；而 `extra="forbid"` 让 `model_copy(update=未知键)` 也不报错，排查时极具迷惑性。
- `tools_enabled` / `entity_memory_enabled` 必须保持默认 `False`：`conftest.py` 的离线 mock run 与 `test_agent_tools.py::test_registry_none_is_unchanged_behavior` 都依赖「不注入 registry = 单次调用、不改提示词」这条行为契约。

**R4 新增的三条坑**：

- `_plan_gate` 里**不要**无条件写 `self._board = board`：构造期守卫（配了人工卡点却没 prompter）会在下一行抛 `ValueError`，此时若 `self._board` 已设置，`_finalize` 会顺手写 `MANIFEST.json`，破坏「一个产物都不落盘」的契约（`test_crew.py::test_human_gate_without_prompter_raises`）。只在 `ApprovalPending` 挂起路径赋值。
- `TaskBoard.refresh()` 的 `conditional_skips` 必须在**同一轮内动态追加**：本轮刚产生的条件性 SKIPPED 若不在集合里，会被「非条件性 SKIPPED」分支误判成 `upstream-skipped`，把汇合节点一起拖成失败。
- 样例 run 挂起后 `resume` **必须挂回 `--sample NAME`**：否则夹具不生效、会去连真实模型并长时间卡住；CLI 子进程测试一律加 `timeout`。

**环境与工具链类**（本机必踩）：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `pytest` 裸命令找不到 | PATH 无 pytest 入口 | 统一 `python -m pytest` |
| 全量回归 `Segmentation fault`（EXIT 139 / `access violation`），偶发 `TypeError: required field "id" missing from Name`、偶发单条断言失败 | **pytest 断言重写的 AST 编译在本机不稳定**，崩在 `ast.iter_fields` / `ast.walk`；3.13.12+pytest9.2.0 与 3.12.10+pytest9.1.1 均复现，与 `PYTHONPATH` / shim 无关（清空并绕过 `sitecustomize.py` 后仍崩）。11-06 实测 3.12.10 上 4 次执行有 1 次 EXIT 139、1 次 `test_crew.py` 交付卡点用例的偶发断言失败（单跑 / 整文件 / 组合 / 重跑均通过）→ 间歇性、非项目代码 | **全量回归统一用 `python scripts/test.py`**（R34 崩溃隔离跑器：自动清 `__pycache__` + 分批 + 崩溃重试，人工分批只作兜底）；本机解释器为 `C:/Users/33793/AppData/Local/Microsoft/WindowsApps/python.exe`（3.12.10 + pytest 9.1.1）。清空环境变量用 `PYTHONPATH= PYTHONDONTWRITEBYTECODE= python ...` 前缀——**别用 `env -u`**，本机 `env` 是 `/c/Users/33793/.local/bin/env` shim，会静默吞掉整条命令（零输出、退出码 0）。崩溃重试由跑器自动完成，累计口径与全量一致；单点调试仍可用默认模式拿断言详情。`PYTHONDONTWRITEBYTECODE=1` 是放大器；3.13 上关掉重写仍崩，勿用 |
| 默认 PyPI 源不可达 | 网络环境 | `--index-url https://mirrors.ustc.edu.cn/pypi/simple/` |
| Electron 启动 / e2e 报 `app is undefined` | 继承了 `ELECTRON_RUN_AS_NODE=1`；**把它设成空字符串仍会触发** | `scripts/launch-studio.bat` 已清空该变量；手动跑用 `unset ELECTRON_RUN_AS_NODE && npm run e2e` |
| Windows 重定向 stdout 崩在状态符号 | GBK 无 `✓`/`⟳` 码位 | 入口已包 `_EncodingSafeStream`，勿删 |

---

## 7. 已知缺口与路线图

**小项（0.2–0.5 人日，不动架构即可清）**

1. ✅ **R3 已闭环**：工具参数 JSON Schema 强校验（见 §3.8）
2. ~~MCP SSE/HTTP transport 资源关闭~~：✅ **R22 已闭环**（stdio 三条管道显式关闭，见 §3.27）；`mcp_servers` 接入生产 run 已由 R13 闭环
3. CI 首次实跑：本机 3.12 已跑通等价检查链（ruff / mypy / pytest 全绿）；178 个提交已于 2026-11-07 推送，**GitHub Actions 3.10–3.13 四版本矩阵随之首次触发**（结果待查：github.com/Han-1-yu/agentcrew/actions）
4. ~~功能测试报告汇总（2500 用例按模块/场景出覆盖矩阵）~~：✅ **R23 已闭环**（`docs/TEST-REPORT.md`：17 个子系统模块矩阵 + 17 类业务场景矩阵 + 质量门与已知边界，见 §3.28）
5. ~~CI 前清理 `ruff check .` 的 14 项既存告警~~：✅ **R18 已闭环**（见 §3.23）；装 CI 同版本 mypy 后一并清掉 9 项类型错误，现在 `ruff check .` 全绿、`mypy agentcrew/` 114 文件零错误
6. ~~`api_rate_limit_window` 配置通道~~：✅ **R18 已闭环**（env / YAML / `config export` / `config import` 四通道 + `ge=1` 校验，见 §3.23） |

**大项（建议单独立项）**

| 事项 | 工作量 | 说明 |
| --- | --- | --- |
| ~~拖拽式可视化编排~~ | — | ✅ **R26 已闭环**（见 §3.31）：看板 SVG 画布（拖拽/连线/属性面板/自动布局）+ YAML 双向（复用 TaskDraft schema）+ 图校验 + 流程库；CLI `flow` |
| ~~多用户与 RBAC~~ | — | ✅ **R25 已闭环**（见 §3.30）：多密钥 + 角色/作用域 + run 归属 + 审计身份；legacy 单密钥部署零改动 |
| ~~单步/断点调试~~ | — | ✅ **R20 已闭环**（见 §3.25）：`debug_step` 开启时每个任务派发前挂起为 `step` 审批单，`approve --decision step/run/abort` + `resume` 逐个放行 / 解除断点 / 取消；变量查看由 `status` / `approvals` / ledger 覆盖 |
| ~~可视化工具注册与在线调试~~ | — | ✅ **R24 已闭环**（见 §3.29）：`tools`/`tool` 命令 + `GET /tools`、`POST /tools/{name}/test` + 看板「工具」标签页；沙箱隔离、开关默认关闭 |
| ~~实体记忆~~ | — | ✅ **R3 已闭环**：纯规则实体抽取 + 全局库归并检索（见 §3.8）；向量化 / RAG 仍缺 |
| ~~官方 SDK + 示例~~ | — | ✅ **R16 已闭环**（见 §3.21）：`sdk/agentcrew_sdk` 零依赖同步客户端 + 两个示例 + 34 例回归（含真 serve 端到端） |
| `web_search` | 1 人日 | ✅ `web_fetch`（SSRF 防护）与 `write_file`（受控工作区）已由 R3 闭环；`web_search` 需外部搜索 API 与密钥，待单独设计 |
| ~~出站 Webhook~~ | — | ✅ **R12 / R14 / R15 已闭环**：后台投递 + HMAC 签名 + 重试上限（§3.17）、多接收端与只推失败（§3.19）、持久化重放队列（§3.20）；仍缺投递的「恰好一次」保证与事件级表达式过滤 |
| ~~MCP 服务器生产接线~~ | — | ✅ **R13 已闭环**：`tools_enabled` 时连接 `mcp_servers`、发现工具、带前缀登记、收尾关连接（见 §3.18） |
| ~~运维部署/排障手册~~ | — | ✅ **已闭环**：`docs/OPERATIONS.md`（部署形态 / 配置与密钥边界 / 备份扩容 / 监控告警 / 升级 / 按症状排障 / 上线清单） |

**遗留风险（验收报告 R1–R8，摘其要）**

- 真实模型输出非确定、无固定种子（仅 temperature=0 走缓存）→ 关键产物做结构化 diff，保留 mock 确定性基线
- 无向量库/RAG（经验库已由 R11 升级为 BM25 + 中文二元组，实体记忆仍为单字加权匹配；两者都不是向量检索）；
- ~~出站 Webhook 无持久化重放队列~~：R15 已闭环（§3.20），进程退出遗留的通知会补投；语义为 at-least-once，靠 `X-AgentCrew-Delivery` 幂等，不保证恰好一次。工具入参强校验已由 R3 闭环，但只覆盖声明的标准 schema 子集
- run 目录无静态加密、平台侧无传输加密；`serve` 单进程多 Crew 容量未压测
- 第三方 MCP 工具安全边界依赖人工配置；无主动告警通道；CI 未实跑

**六层审计遗留建议（本轮未做，按 ROI 排序）**

类型化调度事件协议 → `save_task_delta` 增量 checkpoint 接线（基础设施已实现未接线）→ ~~工具入参 schema 校验~~（R3 已闭环）→ Agent 级熔断（观测先行、开关默认关）→ ~~resume 锁文件~~ / ~~过期 run 自动 reconcile~~（均由 R17 闭环，见 §3.22）。

**本轮已闭环**：配额 `reserve` 异常泄漏与 `complete_structured` 无配额（见 §3.6）。

**R50 之后（2026-11-12）真正剩下的**：

| 事项 | 状态 | 下一步 |
| --- | --- | --- |
| ~~时间戳全量迁 UTC（D-14）~~ | ✅ **已完成**（2026-11-12） | 落盘全量 aware UTC；守规基线 = 0；见 `docs/TIME-MIGRATION-PLAN.md` |
| 「写盘注入 OSError」一致性用例（D-16 剩余） | 🟡 其余四类边界已覆盖 | 若重构封版路径，同步补该注入点 |
| ~~覆盖率门槛 50%~~ | ✅ 已生效 | CI Linux 单进程 job 在 `--cov-fail-under 50` 下通过（run 43215021310） |
| Docker 三件套 | 已交付，本机无 docker | 在 CI/目标机 `docker compose build`，验非 root 对 `output/` 写权限 |
| C2 docker 执行后端 | ✅ 已交付（`sandbox_backend=docker`），参数逐项有测试，**容器未真跑** | 目标机跑一次 `run_tests` + 三条逃逸用例（出网 / 读宿主 / 资源耗尽），结果补进 THREAT_MODEL |
| C3 执行校验 | ✅ 已交付（默认关，`gate_config.exec_verify`） | 真实模型端到端一次「生成函数+测试 → 首跑失败 → 自修正 → 通过」，确认 ledger 里 `g1_exec_check` 与返工闭环可见 |
| ~~CI 首次实跑~~ | ✅ **全绿** | run 43215021310：四版本矩阵 + Windows(uv) + Studio job 全部 success；首跑（43213290659）暴露的 5 条 mypy 与 Studio 缺 lock 已由 `2a25718` 修复 |
| ~~`web_search` 工具~~ | ✅ **R51 已交付** | 四提供方（tavily/brave/duckduckgo/mock）+ SSRF 过滤 + 默认关 + 离线可测，见 §3.64 |
| ~~真实评测报告（C11）~~ | ✅ **已闭环**（2026-11-13） | 23 条全部跑过、22 通过（95.7%）；补跑成本 $0.5983；两条发现与证据缺口见 §3.70 与 `bench/eval-live-2026-11-13.md` |
| 上线自检（本机可验项） | ✅ **9 项全通过**（2026-11-13） | doctor / 离线冒烟 / REST 认证 200·401 / metrics / 无密钥退 3 / clean 干跑 / Studio 三连 / 静态检查；逐项命令与结果见 `docs/DEPLOY-CHECK-2026-11-13.md` |
| 目标机上线项（TLS 反代、监控、数据盘、日志、密钥、成本上限、webhook 幂等、备份演练） | ⏳ 待部署侧 | 判据清单见 `docs/DEPLOY-CHECK-2026-11-13.md` §4 与 `docs/OPERATIONS.md` §8 |
| ~~docker 后端真隔离（B1）~~ | ✅ **已闭环**（2026-11-13） | 本机/云主机都起不了容器（`unshare: operation not permitted`），改在 CI 上跑 4 条真容器用例；首轮抓到「容器 uid 与宿主不一致 → 写不进 workspace」的真缺陷并修掉（§3.73） |
| ~~MCP 互通（B3/B4）~~ | ✅ **已闭环**（2026-11-13） | 官方 `@modelcontextprotocol/sdk` 客户端跑通 `mcp-serve` 全链路；Playwright MCP 25 工具 + 系统 Edge 导航成功（§3.73） |
| ~~RAG 命中率对照（B5）~~ | ✅ **已闭环**（2026-11-13） | 486 条查询离线对照：哈希兜底下 0.5 权重反而更差 → 兜底时压到 0 并留 `docs_vector_degraded`；`bench/rag-hitrate-2026-11-13.md` |
| vision 端到端（B2） | ⛔ 仍缺多模态 key（唯一真外部依赖） | 通道由 `tests/test_multimodal.py` 覆盖；`.env` 里除 deepseek 外均为空/占位（§3.73） |
| 向量检索 / RAG | 部分完成：BM25 + 哈希兜底已可用并做过对照 | 真语义通道需 embedding 端点（配置已就绪，未接） |
| run 目录静态加密 | 未做 | 依赖部署侧决策（LUKS / 云盘加密） |

**仍未纳入修复**（按 ROI，需各自单独设计）：
~~`stream_call` 只记账不设防~~（✅ R57 已闭环：起流前 check + reserve、任意退出路径 release，见 §3.69）；`RunPythonTool` 无文件 / 导入隔离（本轮只补了 timeout 校验）；~~`resume` 缺状态白名单与进程锁~~（均闭环：进程锁 R17，状态白名单 R22 §3.27）；~~过期 run 启动时自动 reconcile 仍缺~~（R17 已闭环，见 §3.22）。

**11-06 已闭环**（见 §3.7）：`_max_review_attempts` 死字段接线、状态跃迁刷新停滞计时、内置工具逐段符号链接检查、`run_python` timeout、ledger 读端点越界归 400、`POST /runs` options 白名单、CLI 数值开关与坏 YAML、前端表单取值与三态反馈统一。

**R3 已闭环**（见 §3.8）：工具链生产化（死链路修复）、工具入参 JSON Schema 强校验、`web_fetch` / `write_file` 两个内置工具、实体记忆。

**R3 新增遗留**（按 ROI）：`mcp_servers` 生产接线（异步连接 + 生命周期 + 资源关闭，当前仍走库层显式装配）→ 工具级调用次数上限（对标 CrewAI `max_usage_count`，现由 ReAct 全局 `max_tool_calls` 覆盖）→ `web_search`（需外部搜索 API）→ 出站 Webhook（事件外推 / 签名 / 失败重试）→ 实体记忆的向量化检索。

**R4 已闭环**（见 §3.9）：按上游结果的条件分支与互斥分支汇合、持久化人工审批单三态、`awaiting_approval` 挂起与跨进程 resume 回原卡点、CLI / HTTP API / Studio 三端审批、契约与状态文档同步。

**R4 新增遗留**（按 ROI）：审批单的审批人身份与 RBAC 绑定（✅ R25 已闭环：`decided_by` 取自密钥身份）→ 条件表达式 / 循环边（当前只支持结果标签枚举，仍未做）→ 挂起 run 的超时与主动告警（✅ R30 已闭环：`approval_timeout_seconds` + `expired` + `approval_expired` 事件）。

**R5 已闭环**（见 §3.10）：`<message to>` 从「只回一句角色职责」改为**目标角色的一次真实模型调用**；`PeerRouter` 配额 / 去重 / 问自己与未知角色零成本拒绝；`AgentFactory` 按角色绑定 `from_role` 并在 setter 里刷新已缓存实例；`peer_dialogue_enabled` / `max_peer_messages` 四处通道打通。

**R5 新增遗留**（按 ROI）：咨询的历史上下文（当前 `consult` 只带问题本身与角色提示词，不带提问方的任务上下文，长对话会重复问基础信息）→ 咨询结果的经验沉淀（目前只进 ledger，不进经验库）。

**R6 已闭环**（见 §3.11）：动态委派四道闸（角色存在 / `manager_only` 权限 / 层数 / 总数）+ `Task.delegated_by` 与带原因的拒绝事件；咨询带上提问方任务背景；`peer_consult_capture_enabled` 把真实问答沉淀进经验库。

**R6 新增遗留**（按 ROI）：委派拒绝原因回灌模型（当前只进 ledger，模型下一轮看不到为什么被拒）→ 咨询的向量化检索（仍走关键词匹配）→ 向量化 RAG → 拖拽编排画布 → 出站 Webhook → MCP 服务器生产接线。

**R7 已闭环**（见 §3.12）：`Reviewer.review` 可指定评审人与层名；`SupervisorReviewer` 按「派活的人」验收动态委派任务；`Gatekeeper` 在旁路之后插入主管层，驳回走同一套返工通道；默认关闭、mock 明确跳过；Studio 门禁页按记录出现「主管复核」卡片。

**R7 新增遗留**（按 ROI）：跨任务层级流程（经理对**规划期**任务也验收——当前只覆盖动态委派任务）→ AI 主管驳回后的自动重派（当前只能退回原执行者）→ 主管 `suggestions` 通道（当前只取 `issues`）。

**R8 已闭环**（见 §3.13）：委派被拒的原因通过 `SubtaskDispatcher.last_rejection` 回灌给模型，给它一次调整机会（`max_subtask_corrections`，默认 1）；裸闭包路径零影响。

**R8 新增遗留**（按 ROI）：拒绝原因同时回灌给**调度器**做自动重派 → `<subtask>` 围栏的结构化纠错建议（当前只给文本原因）→ 向量化 RAG → 拖拽编排画布 → 出站 Webhook → MCP 服务器生产接线。

**R9 已闭环**（见 §3.14）：`supervisor_scope=all` 让规划期任务也由 `supervisor_role` 验收（默认 `delegated` 不变）；主管已在 G2 评审人里时自动跳过，不重复烧钱。

**R9 新增遗留**（按 ROI）：主管 `suggestions` 通道（当前只取 `issues`）→ 多级主管链（✅ R33 已闭环：`supervisor_chain` + 逐级短路）→ 拒绝原因回灌调度器做自动重派 → `<subtask>` 结构化纠错建议 → 向量化 RAG（✅ R27）→ 拖拽编排画布（✅ R26）→ 出站 Webhook（✅ R12/R14/R15）→ MCP 服务器生产接线（✅ R13）。

**R10 已闭环**（见 §3.15）：主管驳回后按 `role_fallback` 换人做，只改 `role` 不改状态机，上限 `max_supervisor_reassignments` 且换人消耗返工额度；G2 驳回不换人。

**R10 新增遗留**（按 ROI）：主管驳回时**指定**人选（当前只按配置优先序自动挑）→ 换人后的经验归因（经验仍记在任务上）→ 多级主管链 → 主管 `suggestions` 通道 → 拒绝原因回灌调度器 → `<subtask>` 结构化纠错建议 → 拖拽编排画布 → 出站 Webhook → MCP 服务器生产接线。

**R11 已闭环**（见 §3.16）：经验检索从「单字命中计数」换成中文二元组 + BM25，IDF 压虚词、长度归一、单字重合不算命中；无新增依赖，原有 9 个经验库用例一行未改全过。

**R11 新增遗留**（按 ROI）：向量检索（缺 embedding 通道，分词/打分已独立成模块，替换点单点）→ 实体记忆改用同一套 BM25（阈值语义需重新设计）→ 经验与实体跨库统一检索 → 拖拽编排画布 → 出站 Webhook → MCP 服务器生产接线。

**R12 已闭环**（见 §3.17）：出站 Webhook 把五个 run 级节点推给外部系统；后台线程投递不阻塞 run、HMAC 签名含时间戳、重试有上限且失败不改 run 结局；密钥三处防泄露（不导出/不进快照/进脱敏词表）。

**R12 新增遗留**（按 ROI）：投递的持久化重放队列（进程内退出即丢未投部分）→ 多接收端（当前单 URL）→ 事件级条件过滤（如只推失败）→ 拖拽编排画布 → MCP 服务器生产接线 → 多用户 RBAC / 单步调试 / 官方 SDK。**前两项已由 R15 / R14 闭环，「只推失败」由 R14 闭环。**

**R14 / R15 已闭环**（见 §3.19 / §3.20）：多接收端与只推失败；未投出的通知入队前落盘、下次启动补投，24h / 5 次淘汰，spool 不存 URL 与密钥。

**R13 已闭环**（见 §3.18）：`tools_enabled` 时连接 `mcp_servers` 里的每个服务器、发现工具、以 `server__tool` 前缀登记、收尾统一关闭；单服务器失败只跳过。

**R13 新增遗留**（按 ROI）：MCP 配置热更新（run 中途变更无效）→ 权限细到单个 MCP 工具（当前按角色白名单整体放行）→ MCP resources / prompts 支持（当前只用 tools）→ 把 agentcrew 自身暴露成 MCP server → 拖拽编排画布 → 多用户 RBAC / 单步调试 / 官方 SDK。

**功能边界（影响对产物的信任方式）**

不执行生成的代码（G1 只静态校验）· token 估算为启发式 · 单进程 asyncio 无分布式 · mock 产物不是模型生成的 · HTTP API 为内部控制面设计（请求体上限 4 MiB，开认证前勿暴露公网）· G3 默认 CLI 交互，`deferred` 模式才走可跨进程恢复的审批单 · 条件分支只支持结果标签枚举、不支持表达式或循环边 · `web_fetch` 仅公网 80/443 且拒绝私网/重定向 · 实体记忆为规则抽取而非语义检索 · 跨角色咨询默认关闭、mock 下只降级不真答、单次 run 至多 `max_peer_messages` 次 · 委派拒绝原因只回灌给**模型自己**（一次），不回灌给调度器做自动重派 · `delegation_mode=manager_only` 只限制「谁能派」；主管审核默认只覆盖**动态委派**任务（`supervisor_scope=all` 才含规划期任务），且只有一层主管、驳回后按配置优先序换人（主管不能指定人选）· 出站 Webhook 为 **at-least-once**（R15 起遗留通知会补投，但多进程并发补投可能重复到达，接收方必须用 `X-AgentCrew-Delivery` 幂等），失败不改 run 结局。

---

## 8. 文档索引

**保留**

| 文档 | 用途 |
| --- | --- |
| `README.md` | 项目介绍、8 角色说明、V2–V5 与 R3–R34 能力完善、配置与用法全文 |
| `PROJECT-STATE.md` | 本文，唯一状态源 |
| `docs/HANDOVER.md` | **接手必读**：当前状态、代码从哪读起、已知的坑、开发者视角排障、剩余工作 |
| `docs/REMEDIATION-STATUS.md` | **差距报告 69 条的逐条处置表**（状态 / 批次 / 验证方式 / 剩余项），交接与复审入口 |
| `docs/TIME-MIGRATION-PLAN.md` | D-14 时间戳 UTC 迁移方案（三步 + 验证清单 + 回滚），配套守规 `tests/test_time_migration_guard.py` |
| `docs/GAP-ANALYSIS-2026-11-10.md` | 六层审计的原始差距登记（**保留原文作为审计痕迹**，处置状态见上一行） |
| `docs/OPERATIONS.md` | **运维部署/排障手册**：五种运行形态（CLI / serve / Studio / 库 / MCP 客户端）、配置三通道与生产必配、密钥与网络边界、`output_dir` 数据与备份、监控告警、升级、按症状排障、上线前检查清单 |
| `sdk/README.md` · `sdk/agentcrew_sdk/` · `sdk/examples/` | **R16 官方 Python SDK**：安装与方法表、异常层次、SSE、刻意不做的事与已知限制；客户端源码与两个可运行示例 |
| `artifacts/contracts/state-enums.md` · `event-types.md` · `openapi.yaml` · `schemas/*.json` | R4 同步后的状态枚举、事件、HTTP 接口与落盘结构契约 |
| `docs/role-agent-binding-design.md` | 「角色 = 职责 + 权限，执行体可多绑」设计方案（v1 草案，部分已落地） |
| `docs/MAGPIE_COMPARISON.md` | 供应商韧性设计参考（故障转移/冷却/限流/循环检测的来源与取舍） |
| `docs/reference/original-prompt.md` | 需求原文（验收口径的最终依据） |
| `docs/superpowers/specs/*.md` | 设计规格与验收整改任务书（`test_architecture` 与验收矩阵的依据） |
| `docs/superpowers/plans/*.md` | V1 实施计划（历史过程材料，备查） |
| `studio/docs/refactor-aurora-glass.md` | Studio 表现层重构（Aurora Glass）与留白二轮的设计说明（11-02） |
| `bench/report.md` · `bench/stability.md` | 性能与稳定性实测证据（R2 验收项） |
| `artifacts/` · `output/` | 运行证据与产物样例 |

**本次删除**（原文在 `.workbuddy/archive/reports-2026-11-02/`，亦可用 git 恢复）

```
AgentCrew-结项验收评审报告.md      Studio-前端测试与Bug修复报告.md
HANDOFF.md                        V7-HANDOFF.md
HANDOFF-2026-11-01.md             HANDOVER-BASELINE-2026-11-01.md
docs/ccswitch-provider-upgrade-prompt.md   docs/studio-frontend-upgrade-prompt.md
docs/studio-prompt.md             docs/zcode-handoff-and-studio-prompt.md
docs/refactor-agentcrew-2026-10.md         docs/studio-frontend-refactor.md
docs/v4-quality-review-report.md  docs/v4-deep-review-report.md
docs/v4-fe-review-findings.md
```

恢复单个文件：`git checkout HEAD -- <path>`（若删除尚未提交且历史中不存在，则从归档目录复制）。
