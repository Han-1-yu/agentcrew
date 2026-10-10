# AgentCrew 状态

V6 借鉴 Magpie 供应商韧性增强全部完成（P0 故障转移 + P0.5 并发/RPM + P1 元数据 + P1.5 循环检测 + P2 预算软阈值），全量测试 1810 passed / 1 skipped，新增 34 测试。

V5 供应商模块深化全部完成（P1-P5），全量测试 1776 passed / 1 skipped，Studio e2e 27/27 通过。

V4 供应商与模型管理升级全部完成（A1-A6 引擎侧 + B1-B4 Studio 侧），全量测试 1682 passed / 1 skipped，Studio e2e 17/17 通过。

V3 三十四项增强全部完成（32 原始规划 + P2-11 Provider 健康监控 + P3-10 权重路由），全量测试 1632 passed / 1 skipped，离线 demo 验证 9/9 任务成功。

V2 七项增强全部完成，1286 passed / 1 skipped。
V1 原提示词七条验收 7/7，真实模型端到端 8/8（$0.10，产物实测可跑）。

```bash
python main.py run --demo --mock --auto --no-live   # 离线
python main.py run --goal "..." --auto --no-live     # 真实模型（.env 里配 Key）
python main.py serve --port 8765                     # HTTP API + Web UI
python main.py doctor                                 # 环境诊断（E-1）
python main.py diff <run_a> <run_b>                   # run 差异对比（E-2）
python -m pytest -q
```

## V2 七项增强（全部完成）
1. LLM 摘要式上下文压缩 — `context/summarizer.py`，未配置时 NullSummarizer 零开销
2. 按复杂度自动选模型 — `models/complexity.py`，env `AGENTCREW_TIER_LOW/MEDIUM/HIGH`
3. 外部 CLI 引擎接入 — `models/providers/cli.py`，provider_overrides 指定 command
4. 树状任务拆解 — `task/task.py` parent_id + `task/graph.py` 树查询
5. HTTP API + SSE — `api/server.py`，`python main.py serve` 启动
6. MCP 协议互操作 — `mcp/client.py`，stdio JSON-RPC 客户端
7. Web UI 面板 — `api/dashboard.py`，Kanban 看板 + 实时事件流

## V3 三十四项增强（全部完成）

### 阶段一 P0（4 项）
- [x] **P0-1** G2 评审 score 阈值放行 — `config.py` + `gate/gatekeeper.py`
- [x] **P0-2** Provider 故障转移 — `config.py` + `models/factory.py`
- [x] **P0-3** 成本预算硬上限 — `config.py` + `models/factory.py` + `crew/crew.py`，CLI `--max-cost` / `--max-llm-calls`
- [x] **P0-4** 可删除会话 — `runtime/checkpoint.py` + `main.py` + `api/server.py` + `api/dashboard.py`

### 阶段二 P1（6 项）
- [x] **P1-1** MCP 工具集成到 Agent 执行链路 — `tools/` 包 + ReAct 循环（`<tool_call>`/`<tool_result>`）
- [x] **P1-2** 并行 G2 评审 — `gate/reviewer.py`，多评审人 asyncio.gather
- [x] **P1-3** 优先级调度 — `task/task_board.py` take_ready() 按 (priority, id) 排序
- [x] **P1-4** 任务级超时 — `task/task.py` + `crew/scheduler.py`，timeout_seconds=300
- [x] **P1-5** 创建 run 的 REST API — `api/server.py`，POST /runs、cancel/resume、GET /runs
- [x] **P1-6** 切换大模型选项 — `models/factory.py` + `crew/crew.py` + API/CLI/Dashboard

### 阶段三 P2（11 项，含新增 P2-11）
- [x] **P2-1** 运行时动态子任务 — `task/task.py` + `base_agent.py`（`<subtask>` 围栏）
- [x] **P2-2** 超时停滞检测 — `crew/scheduler.py`，stall_timeout_seconds=300，退出码 4
- [x] **P2-3** 评审意见去重与收敛检测 — `gate/reviewer.py` + `gate/gatekeeper.py`（Jaccard>0.8 合并，收敛默认关闭）
- [x] **P2-4** G1 扩展代码可执行性校验 — `gate/static_checks.py`（python 语法/危险导入/非空检查）
- [x] **P2-5** 门禁配置化 — `config.py` gate_config 字段，G1/G2/G3 开关
- [x] **P2-6** 计划质量评分 — `task/planner.py`，四维加权（coverage/specificity/dependency_quality/deliverable_clarity）
- [x] **P2-7** 增量计划编辑 — `task/planner.py` + CLI `[e]` 子菜单（add/remove/modify）
- [x] **P2-8** Agent 自我修正循环 — `agents/base_agent.py`，max_self_corrections=2
- [x] **P2-9** Ledger 查询与分析 API — `runtime/ledger.py` + `api/server.py`，GET /runs/{id}/ledger、/stats
- [x] **P2-10** Web UI 操作化 — `api/dashboard.py`，取消/删除按钮、事件日志、统计标签页
- [x] **P2-11** Provider 主动健康监控 — `models/factory.py`，HealthRecord + 后台 30s 探测 + failover 健康感知

### 阶段四 P3（10 项，含新增 P3-10）
- [x] **P3-1** 复杂度动态校准 — `models/complexity.py`，按历史成功率/attempts 调整级别
- [x] **P3-2** LLM 响应缓存 — `models/cache.py`，SHA-256 键 LRU，仅 temperature==0 缓存
- [x] **P3-3** 流式输出 — `models/providers/` + `agents/base_agent.py`，StreamChunk + stream_call
- [x] **P3-4** 多轮 Agent 对话 — `agents/base_agent.py`，`<message to=...>` 围栏 + message_router
- [x] **P3-5** 成功经验沉淀 — `knowledge/experience.py`，run COMPLETED 后提取经验，全局共享
- [x] **P3-6** 产物正文智能摘要 — `artifacts/summarizer.py`，按 kind 分策略（markdown/python/json 等）
- [x] **P3-7** 跨 run 知识复用 — `knowledge/experience.py` + `crew/crew.py`，规划前注入历史经验
- [x] **P3-8** 增量 Checkpoint — `runtime/checkpoint.py`，JSONL 增量 + 50 次自动 compaction
- [x] **P3-9** MCP SSE/HTTP 传输 + 工具权限管控 — `mcp/transports.py` + `tools/registry.py`
- [x] **P3-10** 权重优先级路由 — `models/factory.py`，model_failover 支持 weight 配置 + weighted random

### 阶段五 CLI 与工程化（3 项）
- [x] **E-1** doctor 命令 — `cli/doctor.py`，诊断配置/环境/依赖/Provider 连通性
- [x] **E-2** diff 命令 — `cli/diff.py`，对比 run 间配置/任务/产物/事件差异
- [x] **E-3** API 认证与速率限制 — `api/auth.py` + `api/server.py`，API Key 认证 + IP 滑动窗口限流

## V4 供应商与模型管理升级（全部完成）

### 阶段 A：引擎侧（6 项）
- [x] **A1** 自定义 Provider 注册表 — `config.py` + `models/config.py`，`models.custom_providers` 段、`build_directory()` 合并视图、`parse_alias`/`ModelFactory` 查合并目录、CLI `providers`、密钥脱敏
- [x] **A2** 预设库扩充 — `models/config.py`，PROVIDER_PRESETS 从 9 增至 15（+openrouter/siliconflow/moonshot/zhipu/groq/together），ProviderPreset 新增 `suggested_models` 字段
- [x] **A3** 模型发现服务 — `models/discovery.py`，`async fetch_models()`、URL 候选回退、10s 超时、错误分类、anthropic 适配、CLI `models`
- [x] **A4** 测速与健康整合 — `factory.py` + `api/server.py`，`POST /providers/{name}/test`、HealthRecord 延迟/时间戳
- [x] **A5** API 供应商目录 — `api/server.py`，`GET /providers`（合并目录+健康+延迟，绝不含密钥值）、`GET /providers/{name}/models`（60s 缓存）
- [x] **A6** 配置导入导出与版本化 — `main.py`，`config export/import`、写前备份 `.bak`、`config_version` 顶层字段

### 阶段 B：Studio 侧（4 项）
- [x] **B1** 修复两条链路 bug — `renderer.js` COMMON_MODELS 改 provider:model 结构化别名；`main.js` run:start 传 `--model`（弹窗优先→settings 回落）与 `--max-cost`
- [x] **B2** 新建运行弹窗模型选择改造 — `renderer.js`，provider 下拉→模型下拉→组装别名，后端不可用时手输降级
- [x] **B3** 新增第 9 页「供应商」 — `index.html` + `renderer.js`，卡片列表、测速/拉模型、从预设添加弹窗（生成 YAML 片段），快捷键 1-9
- [x] **B4** e2e/selftest 同步 — `e2e.js` 扩展为 9 页导航 + 供应商页检查（17/17 通过）

### 红线遵守
- 密钥绝不落盘：custom_providers 只存 `api_key_env` 名；`config_snapshot()` 脱敏同步扩展；GET /providers 响应测试断言不含密钥值
- 确定性引擎不变：发现/测速均为按需调用，无常驻网络轮询
- 配置合并顺序不变：custom_providers 接入 `_yaml_values`/`_env_values`（AGENTCREW_CUSTOM_PROVIDERS JSON）/CLI
- 向后兼容：9 个旧预设 key/语义不变；现有 agentcrew.yaml 照常加载；PUT /runs/{id}/model 不破坏
- 测试守卫不删：全量 1682 passed 只增不减

### 阶段 C：Studio 前端完善 V4-FE（7 项，全部完成）

> 独立于引擎侧的纯前端升级，只动 `studio/` 下 8 个文件，零新增 npm 依赖。参考 cc-switch 桌面端设计模式。

- [x] **F0 结构修复** — 补全 index.html DAG 导航按钮（原残缺致空白可点击项）；删除 renderer.js 重复 fmtBytes 与 ROLE_ORDER 局部副本；清理已被真实页面取代的 PLACEHOLDERS 占位文案
- [x] **F1 明暗主题真正落地** — styles.css 全文件颜色 token 化（:root + [data-theme="dark"] 双套 95 个 CSS 变量，正文零硬编码 hex/rgba）；顶栏主题按钮循环 light→dark→system；main.js nativeTheme 监听系统主题变化推送渲染进程；持久化 settings.json；e2e 第 16 项
- [x] **F2 模型选择链路修复** — COMMON_MODELS 重构为 BUILTIN_PROVIDERS 结构化常量（9 个内置 provider 逐一对齐后端 config.py，含 base_url/api_key_env/suggested_models）；新建运行弹窗 provider→model 两级选择组装 `<provider>:<model>` 别名；main.js run:start 传 `--model`（弹窗优先→settings 回落）与 `--max-cost`（settings 回落）
- [x] **F3 供应商管理页（第 9 页）** — 侧边栏第 9 个导航 + 快捷键 1-9；卡片列表（首字母圆标/名称/base_url/内置徽标/密钥环境变量名+配置状态点/模型数摘要）；测试按钮（主进程 IPC provider:test 用 net.fetch 发 GET /models，10s 超时，内联显示延迟/错误）；模型展开+设为默认；「从预设添加」弹窗（5 个额外预设+空白，生成 agentcrew.yaml models.custom_providers YAML 片段+一键复制，Studio 不直接写 yaml）；数据源三级降级（后端API→常量+keyStatus→demo）；CSP connect-src 精确放行 localhost
- [x] **F4 新建运行弹窗体验升级** — 目标必填行内校验（描红+提示，不 toast 轰炸）；Demo 勾选置灰目标输入（--demo/--goal 互斥 UI 表达）；最近使用目标下拉（主进程持久化最近 5 条到 settings.recentGoals）；高级选项折叠区（预算上限/max-llm-calls/项目名/慢速，默认收起）
- [x] **F5 设置页分组重构** — 四组卡片：运行环境（Python 命令+即时探测版本/仓库根目录/输出目录）、模型与预算（默认模型选择器/预算上限）、连接（apiBaseUrl+即时测试连通性走 provider:test）、外观（主题三选一 segmented control 即时生效）；保存 toast 回执
- [x] **F6 细节打磨 + F7 测试文档** — 异步加载区骨架/空态/错误态三态复核；toasts 统一；Esc 覆盖新弹窗；conn-indicator 三态语义复核；e2e 扩展至 21 项（含供应商页/暗色主题/导航按钮非空/模型下拉）；selftest 页面枚举同步；README 与 PROJECT-STATE 更新

**红线遵守**：零新增 npm 依赖（package.json devDependencies 仅 electron）；不修改 agentcrew/、main.py 及任何 Python 文件；CSP connect-src 精确到 localhost:* / 127.0.0.1:* 无 `*`；密钥零接触（provider:keyStatus 只回布尔，api_key 仅环境变量名）；演示模式 buildMockRun/simTick/window.__st 原样保留；styles.css 正文零硬编码色值。

**验收**：`npm run e2e` 21/21 通过退出码 0；`npm run selftest` 退出码 0；grep 色值仅 token 定义区命中；grep api_key 仅环境变量名；git status 无 studio/ 外文件变更。

## V5 供应商模块深化（全部完成）

参考 cc-switch provider 模块设计（provider.rs 数据模型、端点候选回退、流式连通检测、ProviderCard 信息展示），在 V4 基础上深化扩充。

### P1：预设库扩充 + 元数据增强
- `ProviderPreset` / `ModelBinding` 新增 5 个可选字段：`category`（aggregator/national/international/local/specialized）、`family`、`search_aliases`、`endpoint_candidates`、`api_key_url`
- 预设从 15 扩充到 **27 个**（+fireworks/deepinfra/replicate/huggingface/baichuan/minimax/stepfun/lingyi/xinghuo/gemini/mistral/cohere）
- 现有 15 个预设回填 category/family/search_aliases/api_key_url
- `GET /providers` 响应包含全部新字段
- 新增测试 46 个（`tests/test_provider_presets_v5.py`）

### P2：模型发现增强 + 流式检测
- `discovery.py` `candidate_urls()` 扩展为"端点候选 × 路径变体"矩阵回退；无 endpoint_candidates 时行为逐字节不变
- 新增 `agentcrew/models/stream_check.py`：`async check_streaming()` 发 stream=true 最小请求，验证 SSE 第一个 data 帧
- `HealthRecord` 新增 `stream_ok: bool | None`；`ModelFactory.health_check_stream()` 按需探测
- `POST /providers/{name}/test?mode=stream` 支持流式检测；默认 mode=completion 不变
- 新增测试 22 个（`tests/test_discovery_stream_v5.py`）

### P3：Studio 供应商页增强
- `BUILTIN_PROVIDERS` 从 14 扩充到 27，删除 EXTRA_PRESETS 合并，与引擎对齐
- 卡片增强：category 徽标、「🔗 获取Key」链接（shell:openExternal IPC）、端点候选数量、流式状态
- 搜索框（name/label/search_aliases，200ms 防抖）+ 6 个 category 筛选按钮，AND 组合
- 编辑 provider：卡片「编辑」按钮→弹窗，内置生成 `models.providers.<name>:` 覆盖片段，自定义生成 `models.custom_providers.<name>:`，一键复制；Studio 仍不直接写 yaml
- 新增 `shell:openExternal` IPC + preload 暴露
- e2e 新增 4 项（13d-13g），总计 27/27 通过

### P4：配置与 CLI 增强
- `providers` 输出增加 category/endpoints/api_key_url；`--json` 完整元数据
- `providers --category <cat>` 按分类过滤；`providers --search <kw>` 按 name/search_aliases 搜索（支持中文别名）；可 AND 组合
- `models <provider> --verbose` 显示端点候选尝试过程；JSON 含 endpoints_tried/successful_endpoint
- `doctor` 集成流式检测状态（生产路径真实调用，测试注入时跳过）
- 新增测试 14 个（`tests/test_cli_providers_v5.py`）

### P5：测试与文档
- 全量测试 **1776 passed / 1 skipped**（基线 1695 + 新增 82，只增不减）
- `ruff check` V5 修改文件零错误
- `mypy` 新模块零错误
- 密钥安全自查通过（config_snapshot 脱敏、GET /providers 不含密钥值）
- README.md 新增 V5 章节（含 27 预设表格、元数据说明、流式检测、Studio/CLI 增强）
- PROJECT-STATE.md 新增 V5 小节

### 红线遵守
- 密钥绝不落盘：仅存 api_key_env 环境变量名；config_snapshot 脱敏；GET /providers 响应不含密钥值；stream_check 只从参数收 key
- 向后兼容：现有 15 个预设 key/base_url/api_key_env/implementation 完全不变；现有 API 响应只增字段；custom_providers 格式不变；无 endpoint_candidates 时 discovery 行为逐字节一致；/test 默认 mode 不变
- 配置合并顺序不变：代码默认 → .env → agentcrew.yaml → 环境变量 → CLI
- 测试守卫不删：全量 1695→1776 只增不减
- Studio 零框架：不新增 npm 依赖，沿用 vanilla JS + h()
- 确定性引擎不变：发现/测速/流式检测均为按需调用，无常驻网络轮询
- 不修改 cc-switch 参考目录

## V6 借鉴 Magpie：供应商韧性与元数据增强（全部完成）

借鉴 Magpie（Go AI 网关，`E:\magpie-main\magpie-main`）的故障转移、冷却矩阵、并发限速、循环检测、预算软阈值等机制，完善 AgentCrew 供应商模块。深度阅读 18 个 Magpie 核心源码文件后产出对比分析 `docs/MAGPIE_COMPARISON.md`，按 ROI 排序实施 6 项。

### P0：模型调用故障转移（核心）
- **402 余额不足触发故障转移**：`openai_compat.classify_http_error` 将 402 归入 `TransientError`（工厂层据此切换），但 Provider 内部不重试（`_should_retry` 过滤），翻译后的异常上挂 `status_code`
- **冷却矩阵**（借鉴 Magpie `routing.go restAfterMarked`）：402=固定 30min；429=优先 Retry-After，否则 1min 起步指数退避封顶 30min；5xx=1min 起步指数退避封顶 10min；404=1min；其他=1min。冷却中的别名自动排到尝试列表末尾
- **preset fallback_models**：`ProviderPreset.fallback_models` 自动派生 `provider:model` 备用别名，优先级 `settings.model_failover` > preset 自动派生 > 无
- **流式故障转移**：`stream_call` 在首个 chunk 前可切换别名，首块 yield 后锁定通道（借鉴 Magpie `holdWriter` 思想）
- `retry_async` 新增 `retry_filter` 参数，支持"归类为可重试但不内部退避"的精细控制

### P0.5：per-provider 并发与 RPM 限速
- **per-provider 信号量**：`_provider_semaphores` 替代全局信号量，按 provider 名独立限流；未配置 `max_concurrency` 时回退 `settings.max_concurrency`
- **60 秒滑动窗口 RPM**：`_rpm_timestamps` 记录请求时间戳，超限时等待到最早时间戳+60s；`max_rpm=None` 不限制
- `ProviderPreset` / `ModelBinding` 新增 `max_concurrency` / `max_rpm` 字段

### P1：模型元数据增强
- `ProviderPreset` / `ModelBinding` 新增 `input_price` / `output_price`（USD/百万 token）、`context_window` / `max_output_tokens` 字段
- **27 个预设全部填充**价格/窗口数据（openai/anthropic/deepseek 等已知价格精确填写，聚合平台/未知模型留 None）
- `_account()` 优先用 preset 价计算成本，回退到 `pricing.MODEL_PRICING` 表
- `GET /providers` 响应暴露 8 个新元数据字段

### P1.5：循环检测（借鉴 Magpie `loop_guard.go`）
- 新增 `agentcrew/models/loop_guard.py`：`UnitRun` 环形缓冲（256 slots）+ FNV-1a 64 位哈希计数，O(1) 维护窗口内 distinct unit 数；窗口满且 distinct ≤ 24 即判循环
- 双通道独立检测（reasoning / text），按句末标点切 unit，无字母 unit 跳过
- `stream_call` 集成：`loop_guard_enabled=True` 时每个 chunk 喂检测器，触发抛 `LoopDetectedError` 并落 `loop_detected` 事件
- **默认关闭**（`loop_guard_enabled=False`），既有流式调用零影响

### P2：预算软阈值增强
- `Settings` 新增 `budget_warning_threshold=0.8` / `budget_downgrade_threshold=0.9`
- 累计成本达 80% 时落 `budget_warning` 事件（一次性）
- 达 90% 时若有 input 单价更低的 fallback 模型，自动降级并落 `budget_downgrade` 事件
- `max_cost_usd=None` 时完全空转；`reset_stats()` 一并复位警告/降级状态

### 测试与验证
- 全量测试 **1810 passed / 1 skipped**（V5 基线 1776 + 新增 34，只增不减）
- 新增测试文件：`test_fallback_enhanced.py`（7）、`test_provider_limits.py`（8）、`test_provider_metadata.py`（7）、`test_loop_guard.py`（8）、`test_budget_enhanced.py`（4）
- `ruff check` V6 修改文件零错误
- `mypy` 新增代码零错误（剩余 5 个均为 V5 及之前预存）
- 修复 9 个集成测试失败（`_ScriptedProvider` 补充 `.binding` mock 属性）

### 红线遵守
- 向后兼容：所有新字段有默认值；现有 `agentcrew.yaml` / `run.json` 照常加载；未配置 failover 时 `_failover_aliases` 返回 `[alias]` 行为不变
- 故障转移 opt-in：preset fallback_models 有合理默认但不改变单模型调用行为；循环检测默认关闭
- 密钥安全：不在任何响应/日志/文件中泄露 API key
- 只修改 `agentcrew/` 下 Python 代码和测试，不碰 Magpie 项目（只读参考）
- 测试只增不减：1776→1810

### 未实施（ROI 较低，留待后续）
- P2.5 数据脱敏（Magpie `redact` 模块的双向确定性占位符机制）——对比分析中 ROI 排序第 6，需新增完整子系统
- 会话亲和性（Magpie `affinity.go`）——前提是 AgentCrew 做多 key 池聚合，当前单 key 场景收益有限
- 配额感知路由（Magpie `Allowance` 多窗口配额）——需对接订阅账号体系，超出当前供应商模块范围

## 改代码前必读
- **两级并发不得嵌套**：门禁必须在任务槽位释放后执行。守卫是 `test_scheduler_deadlock.py` 的结构性检查，**别删**。
- `bump_rework` 全仓只有 `Gatekeeper` 一处调用，删掉 → 返工回路无界。
- 人工闸门构造期强制 `ValueError`；**别在 `Crew` 里补默认 `NullPrompter`**。
- `has_placeholder` 三常量共同标定，长度单位是**信息量**（CJK ×3）非字符数。
- `ArtifactStore.commit` 路径比较必须**双侧**规范化。
- `reviews.json` 夹具的判断数必须等于该任务验收标准条数。
- P2-3 收敛检测默认关闭（convergence_threshold=None），开启会改变 e2e 向后兼容。

## 已知缺口
- Python 仅语法层覆盖 3.10+（CI 配置了 3.10–3.13 矩阵，但需推到 GitHub 实跑验证）
- 仓库无 git remote，CI 尚未在 GitHub Actions 实际运行过
- 前端 studio/ 为独立项目，与后端通过 API 交互

## 文档
规格 `docs/superpowers/specs/2026-10-05-agentcrew-design.md` · 真实运行证据 `output/realrun{2..8}/` · V2 验证 `output/v2verify/`
