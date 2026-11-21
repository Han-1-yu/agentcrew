# AgentCrew Studio

AgentCrew 多智能体协作平台的桌面 GUI。Electron + 原生 HTML/CSS/JS，**零框架、零打包器**，
唯一 npm 依赖是 `electron`；与后端的全部交互只有两条通道——调用 `python main.py` CLI、读取 run 目录下的状态文件（`run.json` / `tasks.json` / `ledger.jsonl` / `artifacts/`）。

## 启动

```bash
cd studio
npm install        # 国内网络可用：npm install --registry=https://registry.npmmirror.com
npm start          # 启动 AgentCrew Studio
```

要求：Node ≥ 20（开发验证用 Node 24 / npm 11），本机可运行 `python main.py`（Python 3.10+）。

首次启动会自动探测 Python（`python` / `python3` / `py -3`），探测结果与全部偏好保存在
Electron userData 目录的 `settings.json`，可在「设置」里手动修改（Python 命令、仓库根目录、输出目录、默认参数、主题）。

## 功能

- **对话式执行流**（第 1 页「对话」）：把 run 的 ledger 事件重建成 WorkBuddy 式消息流——用户目标右对齐气泡、PM 规划系统消息、各 Agent 任务气泡（角色头像 + 状态 chip），内部聚合该任务的说明（超长折叠）/ 工具调用（折叠）/ 门禁评审（折叠）/ 产物卡片，运行中叠加 SSE 的 `agent_stream_delta` 流式正文；「回到最新」浮动按钮、贴近底部才自动跟随。全部模型输出走文本节点 / `pre`，零 `innerHTML` 拼接。
- **右侧结果区**（顶栏「结果」按钮）：概览 + 产物双 tab 一体化面板。概览 = 运行统计（任务/成功/失败/LLM 调用/Token/成本/已运行/预算）+ 任务状态分布条 + 三层门禁汇总；产物 = MANIFEST 文件清单 + 内联预览 + 零依赖语法高亮。
- **运行列表**：扫描输出目录（默认 `E:\agentcrew\output`，兼容 `output/<分组>/<run_id>` 两层结构），显示目标、状态、时间与任务统计，可搜索。
- **启动新运行 / 恢复 / 取消**：调用 `run --goal … --mock --auto --no-live`、`resume <run_id>`、终止进程并补 `cancel` 离线收尾。新建运行弹窗支持 Provider + Model 两级选择（组装 `<provider>:<model>` 别名传 `--model`）、最近使用目标下拉、高级选项折叠（预算 / max-llm-calls / 项目名 / 慢速）。
- **供应商管理**（第 9 页）：9 个内置 Provider 卡片列表（OpenAI / Anthropic / DeepSeek / 豆包 / 通义 / 文心 / Ollama / Mock / CLI），显示 base_url、密钥环境变量配置状态（只显示名字与已配置/未配置布尔，不接触 Key 值）、建议模型数；一键测试端点连通性（主进程 net.fetch 代理，10s 超时，显示延迟或错误）、展开模型列表并「设为默认」；「从预设添加」弹窗生成 `agentcrew.yaml` 自定义 Provider YAML 片段 + 一键复制。
- **实时任务看板**：10 种状态列（规格配色），运行中每 500ms 轮询 `ledger.jsonl` 新增字节（字节偏移增量读）与 `run.json`/`tasks.json` mtime 变化，边跑边刷新。
- **事件流**：13 种 ledger 事件实时滚动，按类型过滤 + 关键字搜索，最多渲染最近 500 条。
- **结束报告**：运行结束自动弹出统计（成功/失败/阻塞/跳过、LLM 调用、token、成本、耗时），数字与 `run.json` 的 `stats` 逐项一致。
- **任务详情**：描述、验收标准、交付物、逐轮评审记录、状态历史、执行结果。
- **产物浏览**：`MANIFEST.json` 驱动的文件列表（缺失标红），文本类产物（.py/.md/.json/.yaml/.html/.txt/.css/.js 等）内联预览、复制、打开位置；可打包导出 zip（内置纯 JS STORE 打包器）。
- **图表**：各角色 LLM 调用次数与成本条形图、门禁通过/驳回比例。
- **明暗主题**：默认浅色（WorkBuddy 扁平风格），顶栏一键循环 浅色 → 深色 → 跟随系统，持久化到 settings.json；深色模式是同一套扁平语言的深底版本，状态徽章与次要文字对比度达标；system 模式监听 nativeTheme 变化自动跟随。
- **设置页分组**：四组卡片——运行环境（Python 命令 + 即时探测版本 / 仓库根目录 / 输出目录）、模型与预算（默认模型选择器 / 预算上限）、连接（后端 API 地址 + 即时测试连通性）、外观（主题三选一 segmented control）。
- **键盘快捷键**：`Esc` 关闭弹窗/面板，`Ctrl/⌘+N` 新建运行，数字键 `1-9` 切换侧边栏前 9 个页面（1=对话、2=运行总览 … 8=产物、9=事件日志）。
- **健壮性**：CLI 失败 / 文件缺失 / JSON 解析失败均有可读提示，不白屏；关窗时有运行则弹出确认，确认后终止子进程，不留僵尸。

## V3 增强（对应后端 V3 改进）

- **成本预算控制**：新运行模态可设置 `--max-cost USD` 和 `--max-llm-calls N`；运行视图头部实时显示预算用量（成本/调用次数），超 90% 高亮警告；预算耗尽时结束报告显示原因横幅。
- **删除运行**：运行列表悬停显示删除按钮（运行中禁用，提示先取消），确认后调用 CLI `delete --force` 删除整个 run 目录；删除后自动刷新列表。
- **任务优先级**：看板任务卡片显示 `P1-P9` 优先级徽章（高优先级红色、低优先级灰色），与后端 `(priority, id)` 调度排序一致。
- **任务超时与执行配置**：任务详情抽屉新增「执行配置」区，显示优先级、单次超时（默认 300s）、执行重试上限、返工上限。
- **新事件类型**：事件流支持 `budget_exhausted`（预算耗尽）、`llm_failover`（Provider 故障转移）、`tool_loop_exhausted`（工具循环耗尽）等 V3 新事件的颜色与过滤。
- **故障转移统计**：结束报告显示 Provider 故障转移次数。

## V4-FE 前端完善

- **明暗主题真正落地**：styles.css 全文件颜色 token 化（:root + [data-theme="dark"] 双套 95 个 CSS 变量，正文零硬编码色值）；顶栏主题按钮循环 light/dark/system；main.js nativeTheme 监听系统主题变化推送渲染进程；持久化 settings.json。
- **模型选择链路修复**：COMMON_MODELS 重构为 BUILTIN_PROVIDERS 结构化常量（9 个内置 provider 逐一对齐后端 config.py）；新建运行弹窗 provider→model 两级选择，组装 `<provider>:<model>` 别名；main.js run:start 传 `--model`（弹窗优先→settings 回落）与 `--max-cost`。
- **供应商管理页（第 9 页）**：卡片列表 + 端点测试（主进程 net.fetch 代理绕开 CORS）+ 密钥配置状态（只回布尔）+ 模型列表展开/设为默认 + 从预设添加弹窗（生成 agentcrew.yaml YAML 片段 + 一键复制）；侧边栏导航与数字快捷键扩展为 1-9。
- **新建运行弹窗体验升级**：目标必填行内校验（描红+提示，不 toast 轰炸）；Demo 勾选置灰目标输入；最近使用目标下拉（持久化最近 5 条）；高级选项折叠区（预算/max-llm-calls/项目名/慢速）。
- **设置页分组重构**：四组卡片（运行环境/模型与预算/连接/外观），Python 探测与连接测试即时操作，主题 segmented control 即时生效。
- **结构修复**：补全 DAG 导航按钮（原残缺导致空白可点击项）；删除重复 fmtBytes/ROLE_ORDER；清理已被真实页面取代的 PLACEHOLDERS。

## V5-FE 前端设计语言重构（对齐 WorkBuddy）

- **设计语言**：从「Aurora Glass」深色玻璃拟态改为 WorkBuddy 浅色扁平 —— 暖灰页底（`#F1EFE8`）+ 纯白卡片 + 0.5px 细边框 + 12px 圆角，无渐变 / 无模糊 / 无光晕 / 无投影；强调色改为品牌紫 `#534AB7`，语义色走 c-blue / c-green / c-amber / c-red 色板（50 底 + 600 字）。
- **排版**：正文 13px/400/1.6，标题 15/14px，全局只用 400 与 500 两档字重；标签 sentence case（去掉全部 uppercase）；动效只留 transform / opacity 且 ≤200ms。
- **默认主题**：`boot-theme.js` / `main.js` / `shell.js` 的回退值由 `dark` 改为 `light`；三态切换与持久化逻辑不变，深色主题同步改为扁平深底（`#2C2C2A` 页底 / `#444441` 卡片）。
- **交互统一**：hover 只加深边框 + 浅灰底（无位移缩放），焦点环 2px 紫色，导航 active = 紫浅底 + 左侧 2px 紫条；命令输入框对齐 WorkBuddy 对话框形态（居中限宽白卡 + 实心紫发送键）。
- **改动范围**：仅渲染层 —— `styles/` 全部分片、`index.html`（删极光层与标签文案）、主题默认值；`main.js` 除默认主题外、`preload.js` / `backend-api.js` / `src/` 全部 JS 业务逻辑零改动。
- **回归**：`npm run check` 通过（64 JS / 38 CSS 分片）、`npm run e2e` 28/28、`npm run selftest` 16/16，`npm run visual:update` 重建 18 张视觉基线。
- 方案与组件清单见 `docs/前端重构方案-WorkBuddy风格.md`；旧版设计说明 `studio/docs/refactor-aurora-glass.md` 仅作历史留档。

## V6-FE 交互对齐 WorkBuddy（对话流 / 结果区 / 任务列表）

- **对话式执行流**：新增「对话」页（侧边栏首位，数字键 1）。`src/features/conversation/index.js` 的 `buildMessages()` 把 `S.dash.events` 按 `task_id` 聚合为消息流（user / system / agent 三类）；`realtime.js` 把 SSE `agent_stream_delta` 接入对话流的 `S.convStreams`（运行中实时、刷新不回溯）。样式 `styles/features/conversation.css`。
- **右侧结果区**：顶栏「结果」按钮打开 `src/features/result/index.js` 的面板（复用 `#detail-panel`），概览 + 产物双 tab。产物 tab 为「同步挂载 + 异步填充」，预览复用 `artifacts/index.js` 的 `highlightCode`。样式 `styles/features/result.css`。
- **侧边栏任务列表增强**：`shell.js::renderSidebarTasks` 增加搜索框 + 状态筛选 chips（全部/进行中/待处理/完成/失败）+ 按角色分组（前 12 个 + 「还有 N 个」）。
- **后端联动（Ledger observer）**：`runtime/ledger.py` 的 `Ledger` 新增 `observer` 回调（`emit` 落盘后同步推「事件名 + 脱敏 data」）；`crew/crew.py` 用 `_make_ledger_observer` 把明细事件注入 `run_id` 后推 SSE，`_LEDGER_SSE_EXCLUDED` 排除已走 `_dispatch_event`/`_notify` 的事件与高频 `agent_stream_chunk`。使 SSE 模式不再靠 500ms 快照轮询拿明细。
- **细节打磨**：「回到最新」浮动按钮、消息/折叠项进入动效（`transform/opacity ≤200ms`）、长 `task_notes` 折叠、焦点环沿用全局 `:focus-visible`。
- **回归**：后端 `scripts/test.py` 2887 passed / 0 failed；`npm run check` 69 JS / 42 CSS、`npm run e2e` 32/32、`npm run selftest` 16/16、`npm run visual` 20 张一致。
- 方案细则见 `docs/WorkBuddy前端交互-实施方案细则.md`（含 13 项遗漏与边界清单）。

## V7-FE 对话闭环补全（P0 批次，2026-11-21）

按 `docs/STUDIO-FEATURE-COMPLETION-2026-11-21.md` 批次 1 落地。会话模型采用 **conversation = run（1:1）**：列表/重命名/置顶/归档是 run 的前端元数据层（settings.json `runMeta`），运行中追问走 guidance，分支/重做走 rewind，零后端改动。

- **A1 新建对话**：侧栏「新对话」按钮 → 空态对话页（最近目标 + 内置模板 chips，点击填入输入框），发送即直发创建 run；切 run 自动退出新对话态。
- **A2 输入框语义分流**：`lib/composer-intent.js` 判定 create / guidance / stop 三态——空态/终态直发创建 run（REST `POST /runs` 优先、CLI 兜底，超时绝不自动重发）；running/awaiting_approval 直发 `POST /runs/{id}/guidance`（202 乐观入列、4s 与 GET 列表对账）；发送键在 running 且输入为空时切换为「停止整个运行」（确认后 cancel）。
- **A3 指引气泡**：`lib/messages.js` 聚合 `guidance_injected/expired/withdrawn/compacted` 审计事件 + GET 列表/乐观插入项；气泡 chip 表达「待吸收 / 已被 T-xxx 吸收 / 已撤回 / 已过期」。
- **A4/K1 消息级操作**：用户目标气泡「编辑后重发」（预填新建弹窗）、Agent 气泡「从此重做」（复用 rewind dry-run → resume 弹窗）。
- **A5 图片附件**：composer 与新建弹窗共用附件区（≤4 张、单张 ≤2.6MB，主进程选图/读取校验）；CLI 通道传 `--image`、REST 通道传 base64 `images`；用户目标气泡显示输入图片缩略图（`context/images/`）。
- **B1/B2/B3 会话列表**：侧栏「对话」升级为全量列表——时间分组（置顶/今天/昨天/近 7 天/更早）+ 搜索（别名/目标/runId，归档可搜不可见）+ 状态筛选 chips + 归档开关 + 本地分页（每页 30）；条目「⋯」菜单：重命名（本地别名）/置顶/归档/删除/对比/打开目录；`runMeta` 契约（alias/pinned/archived）乐观写盘、失败回滚，deleteRun 后裁剪幽灵键。
- **C1/C2 启动编排**：`new-run/launch.js` 统一 REST 优先 / CLI 兜底（composer 与弹窗共用）；REST options 按 `run_options.py` 白名单映射（mock/default_model/max_cost_usd/max_llm_calls/approval_mode），ask 与 demo 恒走 CLI；新建弹窗接入附件区。
- **I1/I2/E1 审批闭环**：批准/接受后自动 `POST resume`（409 如实显示「已裁决，待恢复」+ 重试按钮，不假装成功）；审批卡带上下文（计划卡点 = 任务/角色数 + 查看计划；交付卡点 = T-FINAL 状态与产物摘要 + 查看产物）；计划审查页挂起时给「去审批」直达；对话页审批系统消息跳门禁页。
- **回归**：`npm run check` 110 JS / 50 CSS；`npm run test:unit` 91/91（新增 composer-intent / run-meta / guidance 消息聚合用例）；`npm run verify:api` 18/18（新增 guidance 直发对账、审批自动 resume 409、REST 创建单发、分流判定四断言）；`npm run selftest` 16/16、`npm run e2e` 36/36；`npm run visual:update` 重建 23 张基线后一致。

## V8-FE 管理与观测补全（P1 批次一，2026-11-21）

按 `docs/STUDIO-FEATURE-COMPLETION-2026-11-21.md` 批次 2 先落地投入产出比最高的部分，Python 后端一行未改。

- **F+ 流程库页**（第 14 页）：R26/R28 后端能力的前端入口——列表（名称/任务数/校验错误徽标/更新时间）、查看（图视图节点表 + 规范化 YAML + 校验错误）、新建/导入（YAML → `POST /flows/validate` 校验 → `POST /flows` 落盘）、删除（确认后 `DELETE /flows/{name}`）、**从当前 run 另存为流程**（tasks.json → FlowSpec）、**用此流程启动**（预填新建弹窗）；新建弹窗高级区新增流程下拉（`flow_name`，REST options / CLI `--flow` 两通道）。
- **P1 通知中心**：聚合实时增量事件（审批请求/裁决、预算预警与耗尽、运行结束/取消/失败、错误）→ 面板列表 + 未读角标 + 点击跳转（审批→门禁页、结束→对话页等）+ 窗口失焦时的系统通知（HTML5 Notification，可关）；去重键 watchDir+event+ts+id；历史回放不触发。
- **B4 Ctrl/Cmd+K 命令面板**：搜 14 页导航、运行切换（含 runMeta 别名）、当前 run 任务（回车开详情）；↑↓ 选择、Enter 执行、Esc 关闭。
- **G1 deriveAgents 补全**：从 `llm_call` 事件按角色聚合模型（最高频别名）与成本（此前硬编码 `'—'/0`），快照与 SSE 两条更新链路都传事件。
- **A6 对话导出**：工具栏「导出」按钮 → 主进程保存对话框 → `.md` 文件（内容仍来自 `buildTranscript`）。
- **Q1 多密钥管理**：设置 → 连接 的「多密钥管理…」弹窗——`GET /keys` 清单（名称/ID/角色/作用域/状态）+ 按角色新建（明文只显示一次 + 复制）+ 吊销；未启用多密钥时 503 如实提示。
- **回归**：`check` 115 模块 / 54 分片；`test:unit` 91/91；`verify:api` 18/18；`selftest` 16/16（14 页导航）；`e2e` **38/38**（新增命令面板、通知中心两探针）；`visual` 基线重建 24 张后一致。

## V9-FE 工具试跑页（P1 批次二，2026-11-21）

按 `docs/STUDIO-FEATURE-COMPLETION-2026-11-21.md` §Q2 落地。R24 工具实验台的后端
（`agentcrew/tools/lab.py`：`GET /tools`、`POST /tools/{name}/test`）一直在，此前只有
CLI 入口；本页把「清单 / 开关口径 / 单工具试跑」搬到桌面端，Python 后端一行未改。

- **工具试跑页**（第 15 页）：`GET /tools` 的清单按卡片列出（名称、类别徽标「文件 /
  网络 / 沙箱」、必填/可选入参摘要、说明），顶部一排口径 chips 回答「为什么清单里没有
  某个工具」——工具执行未开启 / 代码执行未开启（不列 `run_python` / `run_tests`）/
  未配置搜索（不列 `web_search`）/ `local` 沙箱标黄（有沙箱和没沙箱是两回事）/ 沙箱目录。
- **试跑弹窗**：按 `input_schema` 只预填**必填**字段的 JSON 模板（可选项留空，避免模板
  看起来「已经填好」）+ 重置模板 / 格式化；`POST /tools/{name}/test` 90s 超时、每次在
  `<output>/_tool_trial/` 下新建独立沙箱；结果区区分三种信源——200+`ok=true` 通过、
  200+`ok=false` 工具自身失败、409/400/404 请求被拒（不把「没允许跑」画成「跑了但失败」）；
  输出纯文本进 `pre`（零 `innerHTML`），可一键复制。
- **未连后端**：页头仍出标题，正文给诚实空态（配置后端地址的指引），不伪造清单。
- **回归**：`check` 119 模块 / 55 分片；`test:unit` **108/108**（E3 后增至 115/115，新增 `tool-lab`
  八例：入参模板只填必填、JSON 解析错误可读、耗时两档、类别中文、口径 chips 说明
  「清单为什么缺工具」、`ok=false` 与请求错误分开叙述）；`verify:api` **21/21**
  （新增 `GET /tools` 清单可读、`POST /tools/{name}/test` args 原样透传、工具未开启
  409 如实带回）；`selftest` 16/16（15 页导航）；`e2e` **39/39**（导航 15 页 + 2c
  新增「工具试跑」页头渲染）；`visual:update` 重建 24 张后一致。

## V10-FE 运行对比的任务级 diff（P1 批次二续，2026-11-21）

按 `docs/STUDIO-FEATURE-COMPLETION-2026-11-21.md` §E3 落地，纯前端增量。

- **对比弹窗加任务表**：指标差异下方按任务 id 对齐两份 `tasks.json`，出
  「任务差异 · N 新增 · M 移除 · K 变更」表——任务（id + 标题）/ 当前状态 / 对比状态 /
  差异说明；新增 / 移除 / 变更三类行分别用绿 / 红 / 黄安静底色，长列表 34vh 内滚动，只读。
- **比较口径**（`lib/task-diff.js`）：只比标题 / 角色 / 状态 / 优先级 / 依赖五项；描述、
  验收标准、brief 不进 diff（两次 mock 的措辞差异是噪声）；`passed` 与 `succeeded`
  归一后比较（引擎历史同义状态，不归一每次都会出假差异）；依赖按集合比；id 自然序。
- **回归**：`check` 120 模块 / 55 分片；`test:unit` **115/115**（新增 `task-diff` 八例）；
  `verify:api` 21/21；`selftest` 16/16；`e2e` 39/39；`visual:update` 重建 24 张后一致。

## 自测

```bash
npm run check        # 结构护栏：分层、导入完整性、单文件体量、入口收敛（秒级，无需 GUI）
npm run e2e          # 端到端：15 页导航（含回滚重跑/流程库/工具试跑）/ 详情 / 运行切换 / 新建运行 / 成本 / 供应商全链路 / 命令面板 / 通知中心（39 项）
npm run selftest     # 真实 UI 驱动一次 --mock 运行：列表 → 看板 → 事件 → 产物 → 设置 → 启动/取消（16 项）
npm run verify:api          # 后端 API 代理：认证头 + SSE 帧 + C10 指引/R62 rewind/审批自动 resume/REST 创建/工具试跑的 REST 路径（21 项，无需 GUI / 无需真后端）
npm run verify:transitions  # 状态切换：演示→真实运行 / 快速连点切 run / 单通道纪律（11 项）
npm run visual              # 视觉回归：把 e2e 的 24 张截图与本机基线比对（版式/配色变了会失败）
```

`verify:api` 自带一个要求 Bearer 认证的假后端，覆盖的是**可选的 HTTP API 通道**（e2e / selftest
都跑在默认的「CLI + 文件监听」路径上，碰不到它）。当前 21 项里，除认证头注入与 SSE 命名帧外，
还逐条验证 `GET/POST /runs/{id}/guidance`、`DELETE .../guidance/{item_id}`、运行中压缩返回 409、
`POST /runs/{id}/rewind` 的 dry-run 影响集、`GET /tools` 与 `POST /tools/{name}/test`（含
「工具执行未开启 → 409」不被包装成成功）——调用时必须按 preload 的形状传 `{ path, options }`，
否则 method / body 会被当成默认值。若宿主环境注入了 `ELECTRON_RUN_AS_NODE=1`，
Electron 会退化成纯 Node 并在 `main.js` 报 `app.getPath undefined`——先清掉该变量再跑任何 Electron 脚本。

**C10 指引 / R62 回滚（2026-11-15）**：门禁页顶部是「运行中指引」区块（发指引、定向到任务、
设过期、撤回未吸收项、终态压缩历史），列表每 4 秒按内容签名差异自动回显（切页停表、标签页
隐藏时跳过，不重绘就不打断输入焦点）。侧边栏第 8 项是「回滚重跑」独立页，任务详情里的
「从此恢复」是同一套流程的弹窗版——两者都先 dry-run 预览影响集，确认后才 rewind + resume。
全部走 REST：需要 `apiBaseUrl` 与 `runs:admin`（指引）/ `runs:write`（回滚）作用域；没连
后端时只显示空态，不会伪造动作。可测逻辑抽在 `src/lib/guidance.js` / `src/lib/rewind.js`，
单测在 `tests/guidance.test.mjs` / `rewind.test.mjs`。

导航页由 12 增至 **13**（新增 `rewind`），`e2e.js` / `selftest.js` 的 pages 数组同步；
数字键仍是 1-9（`boot.js` 的 pages 未改），新页用侧边栏点击进入。

**连接后端 serve**：在「设置 → 连接」填 API 地址，并保存 `serve` 的 API Key（R36 起认证默认强制开启，
没有 Key 的请求一律 401）。Key 用系统安全存储加密后只放主进程，不进 `settings.json`、不回显。
所有 HTTP / SSE 请求由主进程 `net.fetch` 代理（后端没有 CORS 头，渲染进程直连会被浏览器拦下）；
SSE 也走代理，因为原生 `EventSource` 无法携带 `Authorization` 头。SSE 连不上时自动回退文件监听。

视觉基线依赖本机 DPI 与字体，因此 `visual-baseline/` 不入库：**首次** `npm run visual:update` 建立本机基线，
之后改样式跑 `npm run e2e && npm run visual` 即可发现"顺手改坏了别的页面"。实测：把强调色从铜换成蓝，16 张里 9 张会被拦下
（单个 16px 分块平均差 42.4，阈值 12）。

`e2e` 与 `selftest` 的截图和 `results.json` 分别写入 `studio/e2e-out/` 与 `studio/selftest/`，全部通过时退出码为 0。

## 目录

```
studio/
├── package.json          # 依赖只有 electron；scripts: start / check / e2e / selftest / verify:api / visual
├── main.js               # 主进程：设置、Python 探测、run 扫描、CLI 子进程、ledger 轮询、IPC
├── backend-api.js        # 主进程后端 API 代理：认证头注入 + SSE 帧解析（绕开 CORS）
├── preload.js            # contextBridge 暴露 window.studio（IPC 通道）
├── index.html            # 窗口骨架：只引 styles/index.css 与 src/app/boot.js
├── src/
│   ├── contracts/        # 契约：enums（状态/角色/事件元数据）、providers（内置供应商目录）
│   ├── lib/              # 通用能力：dom（h/s/$/$$）、format（数字/时间/字节）、store（全局状态）、api（本机 HTTP）
│   ├── ui/               # 原子组件：toast、modal、icons
│   ├── app/              # 装配层：boot（启动）、router（导航）、shell（顶栏/主题/快捷键）、
│   │   └── mock/         #   realtime（命名事件 SSE + 文件监听）、data（真实数据适配）
│   │                     #   mock.js 是对外入口，fixtures（假快照）/ simulator（状态推演）在内部分工
│   └── features/         # 按域拆分的功能页
│       ├── dashboard/    #   看板：index（组合）、dag（依赖图）、status-bar / agents / gates / cost / events / stream（流式正文）
│       ├── detail/       #   右侧详情抽屉（任务 / Agent）
│       ├── providers/    #   供应商页：index、card、forms、modals、models（切换器）
│       ├── settings/     #   设置页 + 配额弹窗
│       ├── events/       #   事件日志页 + 导出（JSONL / 报告）
│       ├── artifacts/  plan/  dag-page/  agents-page/  gates-page/  runs/  new-run/
├── styles/               # 样式分片，index.css 按层叠顺序 @import
│   ├── tokens.css        #   设计令牌（WorkBuddy 扁平语言）：深色取值 + 两主题共用字阶/间距/圆角/动效
│   ├── base.css          #   重置与全局排版
│   ├── layout/           #   shell / topbar / sidebar
│   ├── components/       #   按钮、区块、状态条、成本、DAG、Agent、门禁、事件、详情、模态、Toast、空态
│   └── features/         #   各功能页样式（providers / settings / events / plan / artifacts …）
├── tools/
│   ├── check-architecture.js  # npm run check：结构护栏
│   └── verify-api-bridge.js   # npm run verify:api：API 代理 + SSE 帧解析（自带假后端）
├── zip.js                # 纯 JS ZIP（STORE）导出
├── e2e.js / selftest.js  # 自动化测试驱动（--e2e / --selftest 时加载）
├── e2e-out/  selftest/   # 测试产物（截图 + results.json，gitignore）
└── design/               # （可选）视觉走查产物
```

## 代码组织约定

- **分层单向**：`contracts → lib → ui → app → features`。下层不得 import 上层；`npm run check` 会拦住。
- **状态只在 store**：`S` 由 `lib/store.js` 导出，写入口集中在 `app/` 下的装配代码；视图函数只读、只渲染。
- **数据映射在 app/data**：后端 `run.json` / `tasks.json` / `ledger.jsonl` 到 UI 领域模型的转换只有一处，
  新增字段先改 `data.js`，页面不直接读原始 JSON 结构。
- **样式归属**：组件样式进 `styles/components/`，页面私有样式进 `styles/features/`，跨页复用的只有令牌与基础层。
- **体量上限**：单 JS 模块 ≤ 320 行、单 CSS 分片 ≤ 300 行，超过即拆（`npm run check` 校验）。

## 设计边界

- 不修改 `agentcrew/` 与 `main.py` 任何代码；后端升级只要 CLI 与文件格式不变，Studio 无需改动。
- 零框架、零打包器：唯一 npm 依赖是 `electron`，沿用原生 HTML/CSS/JS + h()/s() DOM 构建。
- 密钥分两类，存储方式**不同**（C-21 前这里的表述把两者混为一谈）：
  - **供应商 Key**（`ARK_API_KEY` 等）：在设置页填写的值会**明文写入项目根 `.env`** 并注入主进程 `process.env`。这是刻意的——CLI 与 Studio 共用同一份 `.env`，用户填完就能直接在终端跑 `main.py`。**Studio 是本机开发工具，前提是仓库目录本身可信**；不要把 Studio 指向共享盘或他人可写的目录。列表与卡片只显示「已配置/未配置」布尔，不读回明文值。
  - **后端 API Key**（连远程 `serve` 用）：走 Electron `safeStorage` 加密后存在主进程用户数据目录，不落明文、不回传渲染端。
- 可选 HTTP API：设置页配置 apiBaseUrl 后启用 SSE 实时推送与供应商列表拉取；端点测试走主进程 net.fetch 代理（绕开渲染进程 CORS）；CSP 的 connect-src 精确放行 `http://localhost:*` 与 `http://127.0.0.1:*`。未配置时完整降级为文件监听 + 演示模式。
