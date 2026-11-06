# AgentCrew 项目状态

> **最后更新：2026-11-06** · 本文是仓库**唯一状态源**。
> 历次交接文档、验收/审查/测试报告、一次性工作提示词已在本日汇总并入本文后删除，
> 原文备份见 `.workbuddy/archive/reports-2026-11-02/`（git 亦可通过历史恢复）。

---

## 0. 一句话现状

**引擎与管线健康，无 P0/P1 阻断项。** 全量测试 **2083 passed / 1 skipped / 0 failed**（2084 用例收集，2026-11-06 实测），三个预设样例 100% 通过，真实模型通道可用（`doctor` 6/6 通过）；Studio 架构检查 / e2e / selftest 全绿。

| 指标 | 当前值 |
| --- | --- |
| 全量测试 | 2084 collected / 2083 passed / 1 skipped / 0 failed（3.12.10 + pytest 9.1.1，`--assert=plain`） |
| 预设样例 | 3 套（`todo` 混合并发 / `linear` 严格串行 / `hierarchy` 两层并行），均 0 失败 0 阻塞 |
| 性能 | 峰值并发 1→2→4→4（= 图宽度），达效率 100%（理论上限 1.65×） |
| 稳定性 | 同进程 20 连跑 20/20 成功，RSS −2.3%、句柄 +1、线程不增长 |
| Studio | `npm run check` 62 模块 / 37 分片通过、selftest 16/16、e2e 28/28、probe 24/24 |
| Git | `master` 领先 `origin/master` 161 个提交（2026-11-06 归档 4 个提交：docs / studio / engine / tests）；工作区干净 |
| 角色 | 8 个内置岗位角色 + 执行体（Agent Profile）可多绑 |

```bash
cd E:/agentcrew
python -m pytest -q --assert=plain                   # 全量回归（**须用 3.12.10 解释器**，路径见 §6）
python main.py run --sample linear --no-live --auto  # 离线演示（无需 Key；非交互环境须加 --auto）
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
| 执行 | 8 角色 Agent；自我修正（`max_self_corrections=2`）；任务级超时 300s；停滞检测 300s（退出码 4）；多轮 Agent 对话（`<message to>`）；任务级执行体覆盖 |
| 门禁 | G1 静态校验（语法/危险导入/非空/契约）；G2 并行多评审 + Jaccard 去重（收敛默认关）；G3 人工卡点；`bump_rework` 唯一调用点在 Gatekeeper |
| 模型 | 7 类 provider（openai 兼容 / anthropic / mock / cli / 三传输 MCP）；27 个预设；四级别名解析；故障转移 + 冷却矩阵（402=30min，429=Retry-After 或指数退避，5xx=指数退避）；per-provider 并发信号量 + 60s RPM 滑动窗口；复杂度自动选模 + 动态校准；响应缓存（temperature=0）；预算硬上限 + 80%/90% 软阈值 |
| 可观测 | `ledger.jsonl` 全事件审计（run/task/llm_call/gate/error…）；contextvars 链路追踪（`utils/trace.py`，`llm_call` 自动带 `task_id`/`attempt`）；`log_level` + `AGENTCREW_LOG_JSON=1` 结构化日志（含脱敏）；`diff` / `status` / 统计端点 |
| 产物 | 契约校验（路径/类型/最小字节/必需符号）+ 命名空间隔离 + `MANIFEST.json` + sha256；安全落盘（双侧路径规范化） |
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
| 2026-11-06 | 按板块补全：配置/CLI、HTTP API、引擎/工具、前端统一组件（见 §3.7） | **2083（当前）** |

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

1. 工具参数 JSON Schema 强校验（`input_schema` 目前只渲染进提示词）
2. MCP SSE/HTTP transport 资源关闭（消除 unclosed warning）
3. CI 首次实跑（remote 已配，本地领先 157 提交；需验证 3.10–3.13 矩阵）
4. 功能测试报告汇总（2083 用例按模块/场景出覆盖矩阵）

**大项（建议单独立项）**

| 事项 | 工作量 | 说明 |
| --- | --- | --- |
| 拖拽式可视化编排 | 8 人日 | Studio Flow Designer：节点 + 连线 + 属性面板，YAML 双向导入导出（验收报告 P1③） |
| 多用户与 RBAC | 8 人日 | 当前为共享 API Key |
| 单步/断点调试 | 5 人日 | 事件链路上暂停/单步与变量查看 |
| 可视化工具注册与在线调试 | 5 人日 | 工具管理页 + 单工具试跑 |
| 实体记忆 | 3 人日 | 当前只有短期/长期两级，缺实体抽取与检索 |
| 官方 SDK + 示例 | 3 人日 | 基于 `openapi.yaml` 封装 Python client |
| web_search / web_fetch / 受控 file_write | 2 人日 | 内置仅 4 个本地工具 |
| 出站 Webhook | 2 人日 | 事件外推、签名与失败重试 |
| 运维部署/排障手册 | 1.5 人日 | serve 部署、配置项、doctor 诊断 |

**遗留风险（验收报告 R1–R8，摘其要）**

- 真实模型输出非确定、无固定种子（仅 temperature=0 走缓存）→ 关键产物做结构化 diff，保留 mock 确定性基线
- 无向量库/RAG（经验库仅关键词匹配）；工具入参无 schema 强校验
- run 目录无静态加密、平台侧无传输加密；`serve` 单进程多 Crew 容量未压测
- 第三方 MCP 工具安全边界依赖人工配置；无主动告警通道；CI 未实跑

**六层审计遗留建议（本轮未做，按 ROI 排序）**

类型化调度事件协议 → `save_task_delta` 增量 checkpoint 接线（基础设施已实现未接线）→ 工具入参 schema 校验 → Agent 级熔断（观测先行、开关默认关）→ resume 锁文件（防双进程同跑一板）→ 过期 run 自动 reconcile（本轮已做「僵尸 `RUNNING` 可删」兜底，仍缺启动时自动标记与心跳字段）。

**本轮已闭环**：配额 `reserve` 异常泄漏与 `complete_structured` 无配额（见 §3.6）。

**仍未纳入修复**（按 ROI，需各自单独设计）：
`stream_call` 仍是「只记账不设防」（async generator + failover 循环，预扣/归还配对复杂）；`RunPythonTool` 无文件 / 导入隔离（本轮只补了 timeout 校验）；`resume` 缺状态白名单与进程锁（多进程共写同一 run 目录）；过期 run 启动时自动 reconcile 仍缺（只有「僵尸 `RUNNING` 可删」兜底）。

**11-06 已闭环**（见 §3.7）：`_max_review_attempts` 死字段接线、状态跃迁刷新停滞计时、内置工具逐段符号链接检查、`run_python` timeout、ledger 读端点越界归 400、`POST /runs` options 白名单、CLI 数值开关与坏 YAML、前端表单取值与三态反馈统一。

**功能边界（影响对产物的信任方式）**

不执行生成的代码（G1 只静态校验）· token 估算为启发式 · 单进程 asyncio 无分布式 · mock 产物不是模型生成的 · HTTP API 为内部控制面设计（请求体上限 4 MiB，开认证前勿暴露公网）· G3 人工闸门仅 CLI 交互。

---

## 8. 文档索引

**保留**

| 文档 | 用途 |
| --- | --- |
| `README.md` | 项目介绍、8 角色说明、配置与用法全文 |
| `PROJECT-STATE.md` | 本文，唯一状态源 |
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
