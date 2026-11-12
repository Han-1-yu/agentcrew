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

## 自测

```bash
npm run check        # 结构护栏：分层、导入完整性、单文件体量、入口收敛（秒级，无需 GUI）
npm run e2e          # 端到端：9 页导航 / 详情 / 运行切换 / 新建运行 / 成本 / 供应商全链路（28 项）
npm run selftest     # 真实 UI 驱动一次 --mock 运行：列表 → 看板 → 事件 → 产物 → 设置 → 启动/取消（16 项）
npm run verify:api          # 后端 API 代理：认证头注入 + SSE 命名帧解析（8 项，无需 GUI / 无需真后端）
npm run verify:transitions  # 状态切换：演示→真实运行 / 快速连点切 run / 单通道纪律（11 项）
npm run visual              # 视觉回归：把 e2e 的 16 张截图与本机基线比对（版式/配色变了会失败）
```

`verify:api` 自带一个要求 Bearer 认证的假后端，覆盖的是**可选的 HTTP API 通道**（e2e / selftest
都跑在默认的「CLI + 文件监听」路径上，碰不到它）。若宿主环境注入了 `ELECTRON_RUN_AS_NODE=1`，
Electron 会退化成纯 Node 并在 `main.js` 报 `app.getPath undefined`——先清掉该变量再跑任何 Electron 脚本。

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
