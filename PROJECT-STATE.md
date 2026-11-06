# AgentCrew 项目状态

> **最后更新：2026-11-06** · 本文是仓库**唯一状态源**。
> 历次交接文档、验收/审查/测试报告、一次性工作提示词已在本日汇总并入本文后删除，
> 原文备份见 `.workbuddy/archive/reports-2026-11-02/`（git 亦可通过历史恢复）。

---

## 0. 一句话现状

**引擎与管线健康，无 P0/P1 阻断项。** 全量测试 **2277 passed / 1 skipped / 0 failed**（2278 用例收集，2026-11-06 R8 后分 5 批实测），三个预设样例 100% 通过，真实模型通道可用（`doctor` 6/6 通过）；Studio 架构检查 / e2e 28/28 / selftest 16/16 全绿。R4 把编排内核从「静态 DAG + 同步 TTY 卡点」升级为「条件分支 + 可跨进程恢复的人工审批」；R5 把 `<message to>` 接成真实模型调用；R6 补委派治理、咨询上下文与咨询沉淀；R7 补上「谁派的活谁验收」的监督回路；R8 把委派被拒的原因回灌给模型，让它能改。

| 指标 | 当前值 |
| --- | --- |
| 全量测试 | 2278 collected / 2277 passed / 1 skipped / 0 failed（3.12.10 + pytest 9.1.1，`--assert=plain`；R4 +38、R5 +16、R6 +21、R7 +14、R8 +9，本机断言重写 segfault 后按 §6 分 5 批累计） |
| 预设样例 | 3 套（`todo` 混合并发 / `linear` 严格串行 / `hierarchy` 两层并行），均 0 失败 0 阻塞 |
| 性能 | 峰值并发 1→2→4→4（= 图宽度），达效率 100%（理论上限 1.65×） |
| 稳定性 | 同进程 20 连跑 20/20 成功，RSS −2.3%、句柄 +1、线程不增长 |
| Studio | `npm run check` **63** 模块 / 37 分片通过、selftest 16/16、e2e 28/28、probe 24/24 |
| Git | `master` 领先 `origin/master` 161 个提交；**R3 / R4 / R5 / R6 / R7 五轮改动均尚未提交** |
| 角色 | 8 个内置岗位角色 + 执行体（Agent Profile）可多绑 |

```bash
cd E:/agentcrew
python -m pytest -q --assert=plain                   # 全量回归（**须用 3.12.10 解释器**，路径见 §6）
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
| 门禁（R7） | 主管审核：动态委派任务在 G1/G2 放行后，由**派活的人**再评一次（`layer="supervisor"`），复用同一套返工通道；不受 score / 收敛旁路保护；自派自审与未知主管跳过并留痕；默认关闭、mock 通道明确跳过 |
| 模型 | 7 类 provider（openai 兼容 / anthropic / mock / cli / 三传输 MCP）；27 个预设；四级别名解析；故障转移 + 冷却矩阵（402=30min，429=Retry-After 或指数退避，5xx=指数退避）；per-provider 并发信号量 + 60s RPM 滑动窗口；复杂度自动选模 + 动态校准；响应缓存（temperature=0）；预算硬上限 + 80%/90% 软阈值 |
| 可观测 | `ledger.jsonl` 全事件审计（run/task/llm_call/gate/error…）；contextvars 链路追踪（`utils/trace.py`，`llm_call` 自动带 `task_id`/`attempt`）；`log_level` + `AGENTCREW_LOG_JSON=1` 结构化日志（含脱敏）；`diff` / `status` / 统计端点 |
| 产物 | 契约校验（路径/类型/最小字节/必需符号）+ 命名空间隔离 + `MANIFEST.json` + sha256；安全落盘（双侧路径规范化） |
| 工具（R3） | 6 个内置工具（`read_file` / `list_files` / `grep` / `run_python` / `write_file` / `web_fetch`）；`tools_enabled` 默认关闭的 run 级自动装配 + `tool_permissions` 执行期下发；执行前 JSON Schema 子集强校验（失败带正确形状回灌、绝不执行）；`web_fetch` 全链路 SSRF 防护 |
| 知识（R3） | 经验库（关键词匹配）+ 实体记忆（纯规则抽取五类实体、跨 run 追加归并、加权检索注入 PM 上下文）；`entity_memory_enabled` 默认关闭 |
| 接口 | CLI（`run/resume/status/roles/agents/switch/samples/clean/cancel/delete/serve/doctor/diff/providers/models/config`）；HTTP API + SSE + Web UI；API Key 认证 + IP 限流；并发 run 上限 `AGENTCREW_MAX_CONCURRENT_RUNS=8` |
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
`<subtask>` 围栏的结构化纠错建议（当前只给文本原因）；跨任务层级流程；向量化 RAG；
拖拽编排画布；出站 Webhook；MCP 服务器生产接线。

---

## 4. 历史报告汇总（已并入本文，原文归档）

| 报告 | 时间 | 核心结论 | 现状 |
| --- | --- | --- | --- |
| **结项验收评审报告** | 2026-10-11 | 综合 **75.3 分**，结论「限期整改后复验」；3 个 P1 阻塞：①缺性能/稳定性/功能测试报告 ②预设样例仅 1/3 ③无拖拽编排画布。一票否决项均未触发；离线 9/9、API 10/10、真实模型 8/8 全通过 | ①②已由 R2 闭环（`bench/` + `linear`/`hierarchy`）；③仍在路线图（P2，8 人日）。遗留风险 R1–R8 见 §7 |
| **Studio 前端测试与 Bug 修复** | 2026-10-11 | 修复 3 个确定性 Bug：数字快捷键 8/9 与侧边栏顺序互换（P2）、模态打开时数字键仍切底层页（P1）、预算/调用次数/亲和 TTL 接受负值（P2）；修复后 selftest 16/16、e2e 28/28、probe 24/24，无残留进程 | 全部闭环，回归用例在 `studio/` e2e |
| **HANDOFF**（初版交接） | 2026-10-06 | SDD 会话完成 34 个任务实现/评审，并用真实 DeepSeek 跑 6 次端到端验收，每次暴露一个 mock 无法暴露的缺陷并修掉 | 已被 11-01 交接取代，结论并入本文 |
| **V7-HANDOFF** | 2026-10-10 | V7 增强落地：P0 数据脱敏（`redact.py`）、配额、循环检测等，21 项脱敏用例全绿 | 能力已并入 §2，P2.5 脱敏已实施 |
| **HANDOFF-2026-11-01** | 2026-11-01 | 自包含交接：架构、模块地图、命令、退出码、环境坑、13 项待办；声明"护城河是测试" | 本文继承其结论；待办见 §7 |
| **HANDOVER-BASELINE** | 2026-11-01 | 实测 1946 passed / 1 skipped；两个风险：Git 提交清单不完整（真实 168 文件而非文档所说 65）、真实模型 Key 失效；查明 segfault 实为 `PYTHONPATH` 污染而非解释器不稳 | Key 已恢复（`doctor` 6/6 通过）；提交随 R1/R2 推进（现领先远端 157） |
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
| R8 | 委派被拒原因回灌：SubtaskDispatcher + 一次纠正轮（见 §3.13） | **2277（当前）** |

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
| 全量回归 `Segmentation fault`（EXIT 139 / `access violation`），偶发 `TypeError: required field "id" missing from Name`、偶发单条断言失败 | **pytest 断言重写的 AST 编译在本机不稳定**，崩在 `ast.iter_fields` / `ast.walk`；3.13.12+pytest9.2.0 与 3.12.10+pytest9.1.1 均复现，与 `PYTHONPATH` / shim 无关（清空并绕过 `sitecustomize.py` 后仍崩）。11-06 实测 3.12.10 上 4 次执行有 1 次 EXIT 139、1 次 `test_crew.py` 交付卡点用例的偶发断言失败（单跑 / 整文件 / 组合 / 重跑均通过）→ 间歇性、非项目代码 | **全量回归统一用 `<3.12.10 python> -m pytest -q --assert=plain`**；本机解释器为 `C:/Users/33793/AppData/Local/Microsoft/WindowsApps/python.exe`（3.12.10 + pytest 9.1.1）。清空环境变量用 `PYTHONPATH= PYTHONDONTWRITEBYTECODE= python ...` 前缀——**别用 `env -u`**，本机 `env` 是 `/c/Users/33793/.local/bin/env` shim，会静默吞掉整条命令（零输出、退出码 0）。崩溃时按文件分 4 批重跑覆盖，累计口径与全量一致；单点调试仍可用默认模式拿断言详情。`PYTHONDONTWRITEBYTECODE=1` 是放大器；3.13 上关掉重写仍崩，勿用 |
| 默认 PyPI 源不可达 | 网络环境 | `--index-url https://mirrors.ustc.edu.cn/pypi/simple/` |
| Electron 启动 / e2e 报 `app is undefined` | 继承了 `ELECTRON_RUN_AS_NODE=1`；**把它设成空字符串仍会触发** | `scripts/launch-studio.bat` 已清空该变量；手动跑用 `unset ELECTRON_RUN_AS_NODE && npm run e2e` |
| Windows 重定向 stdout 崩在状态符号 | GBK 无 `✓`/`⟳` 码位 | 入口已包 `_EncodingSafeStream`，勿删 |

---

## 7. 已知缺口与路线图

**小项（0.2–0.5 人日，不动架构即可清）**

1. ✅ **R3 已闭环**：工具参数 JSON Schema 强校验（见 §3.8）
2. MCP SSE/HTTP transport 资源关闭（消除 unclosed warning）；`mcp_servers` 仍未接入生产 run
3. CI 首次实跑（remote 已配，本地领先 161 提交；需验证 3.10–3.13 矩阵）
4. 功能测试报告汇总（2277 用例按模块/场景出覆盖矩阵）

**大项（建议单独立项）**

| 事项 | 工作量 | 说明 |
| --- | --- | --- |
| 拖拽式可视化编排 | 8 人日 | Studio Flow Designer：节点 + 连线 + 属性面板，YAML 双向导入导出（验收报告 P1③） |
| 多用户与 RBAC | 8 人日 | 当前为共享 API Key |
| 单步/断点调试 | 5 人日 | ✅ R4 已实现「卡点挂起 → 跨进程恢复」这一半；仍缺任意任务处暂停/单步与变量查看 |
| 可视化工具注册与在线调试 | 5 人日 | 工具管理页 + 单工具试跑 |
| ~~实体记忆~~ | — | ✅ **R3 已闭环**：纯规则实体抽取 + 全局库归并检索（见 §3.8）；向量化 / RAG 仍缺 |
| 官方 SDK + 示例 | 3 人日 | 基于 `openapi.yaml` 封装 Python client |
| `web_search` | 1 人日 | ✅ `web_fetch`（SSRF 防护）与 `write_file`（受控工作区）已由 R3 闭环；`web_search` 需外部搜索 API 与密钥，待单独设计 |
| 出站 Webhook | 2 人日 | 事件外推、签名与失败重试 |
| 运维部署/排障手册 | 1.5 人日 | serve 部署、配置项、doctor 诊断 |

**遗留风险（验收报告 R1–R8，摘其要）**

- 真实模型输出非确定、无固定种子（仅 temperature=0 走缓存）→ 关键产物做结构化 diff，保留 mock 确定性基线
- 无向量库/RAG（经验库与实体记忆均为关键词 / 规则匹配）；工具入参强校验已由 R3 闭环，但只覆盖声明的标准 schema 子集
- run 目录无静态加密、平台侧无传输加密；`serve` 单进程多 Crew 容量未压测
- 第三方 MCP 工具安全边界依赖人工配置；无主动告警通道；CI 未实跑

**六层审计遗留建议（本轮未做，按 ROI 排序）**

类型化调度事件协议 → `save_task_delta` 增量 checkpoint 接线（基础设施已实现未接线）→ ~~工具入参 schema 校验~~（R3 已闭环）→ Agent 级熔断（观测先行、开关默认关）→ resume 锁文件（防双进程同跑一板）→ 过期 run 自动 reconcile（本轮已做「僵尸 `RUNNING` 可删」兜底，仍缺启动时自动标记与心跳字段）。

**本轮已闭环**：配额 `reserve` 异常泄漏与 `complete_structured` 无配额（见 §3.6）。

**仍未纳入修复**（按 ROI，需各自单独设计）：
`stream_call` 仍是「只记账不设防」（async generator + failover 循环，预扣/归还配对复杂）；`RunPythonTool` 无文件 / 导入隔离（本轮只补了 timeout 校验）；`resume` 缺状态白名单与进程锁（多进程共写同一 run 目录）；过期 run 启动时自动 reconcile 仍缺（只有「僵尸 `RUNNING` 可删」兜底）。

**11-06 已闭环**（见 §3.7）：`_max_review_attempts` 死字段接线、状态跃迁刷新停滞计时、内置工具逐段符号链接检查、`run_python` timeout、ledger 读端点越界归 400、`POST /runs` options 白名单、CLI 数值开关与坏 YAML、前端表单取值与三态反馈统一。

**R3 已闭环**（见 §3.8）：工具链生产化（死链路修复）、工具入参 JSON Schema 强校验、`web_fetch` / `write_file` 两个内置工具、实体记忆。

**R3 新增遗留**（按 ROI）：`mcp_servers` 生产接线（异步连接 + 生命周期 + 资源关闭，当前仍走库层显式装配）→ 工具级调用次数上限（对标 CrewAI `max_usage_count`，现由 ReAct 全局 `max_tool_calls` 覆盖）→ `web_search`（需外部搜索 API）→ 出站 Webhook（事件外推 / 签名 / 失败重试）→ 实体记忆的向量化检索。

**R4 已闭环**（见 §3.9）：按上游结果的条件分支与互斥分支汇合、持久化人工审批单三态、`awaiting_approval` 挂起与跨进程 resume 回原卡点、CLI / HTTP API / Studio 三端审批、契约与状态文档同步。

**R4 新增遗留**（按 ROI）：审批单的审批人身份与 RBAC 绑定（当前 `decided_by` 只是自由文本）→ 条件表达式 / 循环边（当前只支持结果标签枚举）→ 挂起 run 的超时与主动告警。

**R5 已闭环**（见 §3.10）：`<message to>` 从「只回一句角色职责」改为**目标角色的一次真实模型调用**；`PeerRouter` 配额 / 去重 / 问自己与未知角色零成本拒绝；`AgentFactory` 按角色绑定 `from_role` 并在 setter 里刷新已缓存实例；`peer_dialogue_enabled` / `max_peer_messages` 四处通道打通。

**R5 新增遗留**（按 ROI）：咨询的历史上下文（当前 `consult` 只带问题本身与角色提示词，不带提问方的任务上下文，长对话会重复问基础信息）→ 咨询结果的经验沉淀（目前只进 ledger，不进经验库）。

**R6 已闭环**（见 §3.11）：动态委派四道闸（角色存在 / `manager_only` 权限 / 层数 / 总数）+ `Task.delegated_by` 与带原因的拒绝事件；咨询带上提问方任务背景；`peer_consult_capture_enabled` 把真实问答沉淀进经验库。

**R6 新增遗留**（按 ROI）：委派拒绝原因回灌模型（当前只进 ledger，模型下一轮看不到为什么被拒）→ 咨询的向量化检索（仍走关键词匹配）→ 向量化 RAG → 拖拽编排画布 → 出站 Webhook → MCP 服务器生产接线。

**R7 已闭环**（见 §3.12）：`Reviewer.review` 可指定评审人与层名；`SupervisorReviewer` 按「派活的人」验收动态委派任务；`Gatekeeper` 在旁路之后插入主管层，驳回走同一套返工通道；默认关闭、mock 明确跳过；Studio 门禁页按记录出现「主管复核」卡片。

**R7 新增遗留**（按 ROI）：跨任务层级流程（经理对**规划期**任务也验收——当前只覆盖动态委派任务）→ AI 主管驳回后的自动重派（当前只能退回原执行者）→ 主管 `suggestions` 通道（当前只取 `issues`）。

**R8 已闭环**（见 §3.13）：委派被拒的原因通过 `SubtaskDispatcher.last_rejection` 回灌给模型，给它一次调整机会（`max_subtask_corrections`，默认 1）；裸闭包路径零影响。

**R8 新增遗留**（按 ROI）：拒绝原因同时回灌给**调度器**做自动重派 → `<subtask>` 围栏的结构化纠错建议（当前只给文本原因）→ 向量化 RAG → 拖拽编排画布 → 出站 Webhook → MCP 服务器生产接线。

**功能边界（影响对产物的信任方式）**

不执行生成的代码（G1 只静态校验）· token 估算为启发式 · 单进程 asyncio 无分布式 · mock 产物不是模型生成的 · HTTP API 为内部控制面设计（请求体上限 4 MiB，开认证前勿暴露公网）· G3 默认 CLI 交互，`deferred` 模式才走可跨进程恢复的审批单 · 条件分支只支持结果标签枚举、不支持表达式或循环边 · `web_fetch` 仅公网 80/443 且拒绝私网/重定向 · 实体记忆为规则抽取而非语义检索 · 跨角色咨询默认关闭、mock 下只降级不真答、单次 run 至多 `max_peer_messages` 次 · 委派拒绝原因只回灌给**模型自己**（一次），不回灌给调度器做自动重派 · `delegation_mode=manager_only` 只限制「谁能派」，主管审核（`supervisor_review_enabled`）也只覆盖**动态委派**任务，规划期任务仍只走 G2。

---

## 8. 文档索引

**保留**

| 文档 | 用途 |
| --- | --- |
| `README.md` | 项目介绍、8 角色说明、V2–V5 与 R3–R8 能力完善、配置与用法全文 |
| `PROJECT-STATE.md` | 本文，唯一状态源 |
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
