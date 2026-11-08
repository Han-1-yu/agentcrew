# AgentCrew 项目状态

> **最后更新：2026-11-07** · 本文是仓库**唯一状态源**。
> 历次交接文档、验收/审查/测试报告、一次性工作提示词已在本日汇总并入本文后删除，
> 原文备份见 `.workbuddy/archive/reports-2026-11-02/`（git 亦可通过历史恢复）。

---

## 0. 一句话现状

**引擎与管线健康，无 P0/P1 阻断项。** 全量测试 **2632 passed / 1 skipped / 0 failed**（2633 用例收集，R34 后由 `scripts/test.py` 一次跑出），三个预设样例 100% 通过，真实模型通道可用（`doctor` 6/6 通过）；Studio 架构检查 / e2e 28/28 / selftest 16/16 全绿。R4 把编排内核从「静态 DAG + 同步 TTY 卡点」升级为「条件分支 + 可跨进程恢复的人工审批」；R5 把 `<message to>` 接成真实模型调用；R6 补委派治理、咨询上下文与咨询沉淀；R7 补上「谁派的活谁验收」的监督回路；R8 把委派被拒的原因回灌给模型，让它能改；R9 让经理也验收规划期的活；R10 让主管驳回后能换个人做；R11 把经验检索从单字计数换成中文二元组 + BM25；R12 补上出站 Webhook；R13 把 MCP 服务器接进生产工具链；R14 让 Webhook 支持多接收端与「只推失败」；R15 让未投出的通知落盘，在下一次启动自动补投；运维部署/排障手册已补入 `docs/OPERATIONS.md`；R16 补上官方 Python SDK 与示例；R17 给 run 加心跳、启动时僵尸收敛与跨进程 resume 锁；R18 让 CI 检查链首次全绿并补齐限流窗口配置通道；R19 给主管审核开了独立、不阻塞的 `suggestions` 建议通道；R20 补上单步/断点调试——每个任务派发前可挂起裁决，第二档功能项全部闭环；R21 给 API 契约装上守卫，openapi.yaml 与代码路由双向锁死、不再漏端点；R22 补上 resume 状态白名单与 MCP 管道显式关闭；R23 产出 2501 用例的模块/场景覆盖矩阵（`docs/TEST-REPORT.md`），验收报告遗留的最后一项小项闭环；R24 补上工具管理页与单工具试跑；R25 把共享 API Key 升级为多用户 RBAC（密钥/角色/作用域/归属/审计）；R26 补上拖拽式可视化编排（画布 + YAML 双向 + 图校验）；R27 给检索加向量通道（混合打分 + 磁盘缓存 + 本地退化）；R28 打通「画布 → run」直通规划；R29 让实体记忆也走向量混合（不加列、不迁移）；R30 给审批单加等待窗口与过期告警；R31 给条件边补否定/兜底写法；R32 把控制台 UI 重构成令牌化 + 明暗主题 + 侧栏布局；R33 把单层主管复核扩成可配的多级链；R34 修好本机测试环境（崩溃隔离跑器，一条命令出可信结论）。

| 指标 | 当前值 |
| --- | --- |
| 全量测试 | 2633 collected / 2632 passed / 1 skipped / 0 failed（3.12.10 + pytest 9.1.1，`--assert=plain`；R4 +38、R5 +16、R6 +21、R7 +14、R8 +9、R9 +6、R10 +13、R11 +17、R12 +21、R13 +12、R14 +12、R15 +18、R16 +34、R17 +24、R18 +10、R19 +5、R20 +17、R21 +24、R22 +10、R23 +0、R24 +19、R25 +26（含契约守卫 3 条新路由探测，合计 +29）、R26 +25（含契约守卫 5 条新路由探测）、R27 +11、R28 +8、R29 +5、R30 +6、R31 +5、R32 +6、R33 +7、R34 +11，由 `scripts/test.py` 一次跑出（崩溃自动重试）） |
| 静态检查 | `ruff check .` **全绿**（全仓，14 项历史告警清零）；`mypy agentcrew/` **114 文件零错误**（2.4.1）。CI 等价检查链在本机 3.12 首次跑通 |
| 预设样例 | 3 套（`todo` 混合并发 / `linear` 严格串行 / `hierarchy` 两层并行），均 0 失败 0 阻塞 |
| 性能 | 峰值并发 1→2→4→4（= 图宽度），达效率 100%（理论上限 1.65×） |
| 稳定性 | 同进程 20 连跑 20/20 成功，RSS −2.3%、句柄 +1、线程不增长 |
| Studio | `npm run check` **63** 模块 / 37 分片通过、selftest 16/16、e2e 28/28、probe 24/24 |
| Git | `master` 与 `origin/master` 同步（`792503d`；178 个提交于 2026-11-07 推送，含 CI 3.10 修复与三次 API 补推）；**工作区干净** |
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
| 执行 | 8 角色 Agent；自我修正（`max_self_corrections=2`）；任务级超时 300s；停滞检测 300s（退出码 4）；任务级执行体覆盖 |
| 协作（R5） | `<message to="…">` 接成**目标角色的一次真实模型调用**（`BaseAgent.consult`，不带产物协议、不落盘、不建任务）；`peer_dialogue_enabled` 默认关闭；`max_peer_messages` 配额 + 同问题去重 + 问自己/未知角色零成本拒绝；mock 通道自动降级、Provider 异常不打断提问方 |
| 委派（R6） | 动态子任务四道闸：角色必须存在（拼错不再静默落通用兜底）/ `manager_only` 时只有经理能派 / 层数 ≤ `max_delegation_depth` / 总数 ≤ `max_dynamic_tasks`；拒绝留 `subtask_rejected_by_policy` 带原因、run 不停；`Task.delegated_by` 留痕派发者。咨询带上提问方任务背景（contextvar 透传，不改回调签名）；`peer_consult_capture_enabled` 可把真实问答沉淀进经验库 |
| 委派（R8） | 被拒原因**回灌模型**：`SubtaskDispatcher` 暴露 `last_rejection`，Agent 据此回灌并重问一次（`max_subtask_corrections`，默认 1）；裸闭包不回灌（调用次数不变）；只纠正一轮不套娃 |
| 门禁 | G1 静态校验（语法/危险导入/非空/契约）；G2 并行多评审 + Jaccard 去重（收敛默认关）；G3 人工卡点；`bump_rework` 唯一调用点在 Gatekeeper |
| 门禁（R7/R9/R10） | 主管审核：动态委派任务在 G1/G2 放行后，由**派活的人**再评一次（`layer="supervisor"`），复用同一套返工通道；不受 score / 收敛旁路保护。R9 起 `supervisor_scope=all` 时规划期任务回落到 `supervisor_role`（默认 PM）验收，主管已在 G2 评审人里则跳过。R10 起主管驳回可**换人**：按 `role_fallback` 挑替代角色、改 `task.role` 后仍走 REWORK，上限 `max_supervisor_reassignments` 且换人消耗返工额度。默认关闭、mock 通道明确跳过 |
| 模型 | 7 类 provider（openai 兼容 / anthropic / mock / cli / 三传输 MCP）；27 个预设；四级别名解析；故障转移 + 冷却矩阵（402=30min，429=Retry-After 或指数退避，5xx=指数退避）；per-provider 并发信号量 + 60s RPM 滑动窗口；复杂度自动选模 + 动态校准；响应缓存（temperature=0）；预算硬上限 + 80%/90% 软阈值 |
| 可观测 | `ledger.jsonl` 全事件审计（run/task/llm_call/gate/error…）；contextvars 链路追踪（`utils/trace.py`，`llm_call` 自动带 `task_id`/`attempt`）；`log_level` + `AGENTCREW_LOG_JSON=1` 结构化日志（含脱敏）；`diff` / `status` / 统计端点 |
| 产物 | 契约校验（路径/类型/最小字节/必需符号）+ 命名空间隔离 + `MANIFEST.json` + sha256；安全落盘（双侧路径规范化） |
| 工具（R3/R13） | 6 个内置工具（`read_file` / `list_files` / `grep` / `run_python` / `write_file` / `web_fetch`）+ **R13 起 MCP 服务器的工具**（`server__tool` 前缀防撞名、schema 原样透传、单服务器失败只跳过）；`tools_enabled` 默认关闭的 run 级自动装配 + `tool_permissions` 执行期下发；执行前 JSON Schema 子集强校验（失败带正确形状回灌、绝不执行）；`web_fetch` 全链路 SSRF 防护 |
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

---
## 4. 历史报告汇总（已并入本文，原文归档）

| 报告 | 时间 | 核心结论 | 现状 |
| --- | --- | --- | --- |
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

**仍未纳入修复**（按 ROI，需各自单独设计）：
`stream_call` 仍是「只记账不设防」（async generator + failover 循环，预扣/归还配对复杂）；`RunPythonTool` 无文件 / 导入隔离（本轮只补了 timeout 校验）；~~`resume` 缺状态白名单与进程锁~~（均闭环：进程锁 R17，状态白名单 R22 §3.27）；~~过期 run 启动时自动 reconcile 仍缺~~（R17 已闭环，见 §3.22）。

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
