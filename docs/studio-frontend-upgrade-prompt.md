# AgentCrew Studio 前端完善 — 工作提示词（V4-FE）

> 本文档是给 AI 编码代理的**纯前端**工作提示词。目标：参考成熟项目 **cc-switch** 的桌面端前端
> 设计模式，系统性完善 AgentCrew Studio（Electron 应用）的 UI/UX：落地明暗主题、修复模型选择
> 断裂链路、新增「供应商管理」页、升级新建运行与设置体验。
>
> 参考源码（只读，**不得写入**）：`E:\deepseek idea\cc-switch-main\cc-switch-main`（Tauri + React，
> 前端在 `src/`）。工作目录：`E:\agentcrew\studio`。
> 引擎侧的配套任务（自定义 provider、模型发现 API 等）见同目录
> `ccswitch-provider-upgrade-prompt.md`；本提示词的每项功能在**后端端点不存在时必须优雅降级**，
> 不得阻塞等待后端先行。

---

## 一、cc-switch 前端值得借鉴的模式（分析结论）

| # | cc-switch 模式 | 参考位置 | 移植到 Studio |
|---|---|---|---|
| 1 | **供应商卡片列表**：ProviderCard（图标+名称+当前标记+悬浮操作区）/ ProviderList / AddProviderDialog / ProviderEmptyState，职责分离 | `src/components/providers/` | F3 供应商页的卡片结构 |
| 2 | **预设库分组与搜索**：预设带 family/plan/region 分类字段 + 搜索别名表，弹窗内快速过滤 | `src/config/presetFamilies.ts`、`presetSearchAliases.ts` | F3「从预设添加」弹窗的搜索与分类 |
| 3 | **模板变量表单**：`templateValues: {label, placeholder, defaultValue}` 驱动动态占位与校验 | `src/config/claudeProviderPresets.ts:7-12` | F3 添加弹窗的 base_url / api_key_env 字段 |
| 4 | **端点候选与测速展示**：`endpointCandidates` 多端点 + speedtest 延迟直接标在卡片上 | `services/speedtest.rs` + ProviderCardActions | F3 卡片「测试」按钮 + 延迟徽标 |
| 5 | **主题跟随系统 + 手动切换**，明暗双套 token | `src/` 主题上下文 | F1（Studio README 已承诺但未实现） |
| 6 | **轻量 UI 基件**（dialog / segmented-control / search-field / notice / badge）统一交互语言 | `src/components/ui/` | F5/F6 借**模式**不引**库**（Studio 零框架红线） |
| 7 | **设置分组**：通用 / 外观 / 网络代理 / 数据管理分区卡片 | `src/components/settings/` | F5 设置页分组 |

**明确不搬**：i18n 四语言包、zustand 状态库、shadcn/radix 依赖链、赞助商推广位。Studio 的
「零框架、零打包器、唯一依赖 electron」（`studio/package.json`）与单语言中文界面是既定设计。

---

## 二、Studio 现状快照与分析发现的问题（改代码前必读）

**架构**：Electron 主进程 `main.js`（782 行：设置持久化 userData/settings.json、Python 探测、
run 目录扫描、CLI 子进程、ledger 增量轮询、IPC）+ `preload.js`（contextBridge 暴露 `window.studio`，
约 30 个通道）+ `renderer.js`（2432 行 vanilla JS：全局状态对象 `S` + `h()` DOM 构建 + 8 页导航 +
全量重渲染）+ `styles.css`（802 行，iOS 浅色风格硬编码色值）+ `e2e.js`（15 项检查）/ `selftest.js`。
数据通道三条：CLI 子进程、run 目录文件读取、可选 HTTP/SSE（设置页 apiBaseUrl，SSE 见
`renderer.js:1952` 起）。

**已确认的 bug 与幽灵功能（本次必修）**：

1. **index.html:80 DAG 导航按钮残缺**：`<button class="nav-item" data-nav="dag" type="button">`
   没有图标、没有文字、没有闭合标签，下一行直接是 agents 按钮。HTML 解析器遇到嵌套 `<button>`
   会隐式闭合前者 → 侧边栏渲染出一个**空白可点击项**。e2e 第 2 项只验证 `active` class 切换所以
   没拦住——这是「测试通过但肉眼可见的坏」，修按钮的同时要给 e2e 补「导航按钮非空」断言。
2. **明暗主题是幽灵功能**：README 声称「明暗主题：顶栏一键切换并持久化」，`main.js:46` 默认
   `theme: 'dark'`，但 `styles.css` 全文只有浅色硬编码色值（`#F2F2F7`、`#1C1C1E`、`#007AFF`…），
   顶栏没有主题按钮，`S.settings.theme` 无人消费。
3. **模型选择断链（两处）**：`renderer.js:1589` `COMMON_MODELS` 是裸模型名（`'gpt-4o'`、
   `'deepseek-chat'`…），引擎别名格式要求 `<provider>:<model>`（无冒号直接报错）——设置页选出的
   默认模型引擎无法解析；`main.js:543-550` 构造 `run:start` 参数时**从不传 `--model`**，设置页的
   `defaultModel` 与 `maxCostUsd` 对新建运行不生效（弹窗显式填写的才传）。
4. **CSP 可能挡住 API/SSE**：`index.html:5` 的 CSP 只有 `default-src 'self'`，跨源
   `EventSource`/`fetch`（如 `http://localhost:8765`）会被 connect-src 回落规则拦住。SSE 功能
   （V3 已交付）在部分启动方式下可能静默失效。
5. **过期文案与死代码**：`renderer.js:1009` `PLACEHOLDERS` 仍写着 tasks/agents/gates/artifacts/
   events/settings「正在开发中」，而这些页面都已有真实渲染函数（e2e 第 9-13 项在验真实内容）；
   设置页描述还写着「主题配置正在开发中」。另有重复定义：`fmtBytes`（158 与 1207 两处）、
   `ROLE_ORDER`（619 与 1586 两处）。

**体验短板**（与 cc-switch 对比）：设置页 5 个平铺输入框无分组、无 Python 探测状态展示；新建运行
弹窗无校验与历史目标；无供应商管理视图（用户不知道有哪些 provider、key 是否配好、端点通不通）。

---

## 三、红线（违反任何一条即返工）

1. **零框架**：不新增任何 npm 依赖（`package.json` 的 devDependencies 保持只有 `electron`）；
   不引入构建步骤；沿用 `h()` / `s()` DOM 构建与全量重渲染模式。
2. **不修改 `agentcrew/`、`main.py` 与任何 Python 文件**；本提示词只动 `studio/` 下 8 个文件
   （main.js / preload.js / index.html / renderer.js / styles.css / e2e.js / selftest.js / README.md）。
3. **CSP 不放宽到无谓范围**：如需放行后端 API，connect-src 精确到 `http://localhost:*` 与
   `http://127.0.0.1:*`，禁止 `*`。
4. **密钥零接触**：任何新代码不得读取、显示、存储 API Key 的**值**；环境变量只显示**名字**与
   「已配置/未配置」布尔（主进程读 `process.env` 后只回传布尔）。
5. **演示模式不破坏**：`buildMockRun`/`simTick` 模拟链路、`window.__st` 遥测（selftest 依赖，
   `renderer.js:101-114`）原样保留；无后端、无 run 时应用仍可完整浏览。
6. **e2e 既有 15 项检查保持通过**，selftest 退出码 0；`styles.css` 顶部的设计语言注释
   （控件高度三档/图标两档/间距刻度）继续成立——新组件遵守同一刻度。
7. 不修改 cc-switch 参考目录。

---

## 四、任务清单（按序实施，每项独立可验收、独立 commit）

### F0. 结构修复（先行，半小时量级）

- `index.html:80` 补全 DAG 导航按钮：与相邻按钮同构的 SVG 图标（节点连线风格）+ `<span>任务图</span>`
  + 闭合标签。
- 删除重复的 `fmtBytes`（保留一份，`renderer.js:158`）与 `ROLE_ORDER`（保留一份），确认无引用断裂。
- 审计 `switchNav`（`renderer.js:1777`）的页面注册表与 `PLACEHOLDERS`：已被真实页面取代的条目删除；
  确属占位的（若有）更新文案，设置页描述去掉「主题配置正在开发中」。

### F1. 明暗主题真正落地（最大单项，机械但面广）

- `styles.css` 颜色 token 化：顶部新建 `:root` 与 `[data-theme="dark"]` 两套 CSS 自定义属性，
  覆盖：背景三层（页面/面板/悬浮）、边框、文字两级、强调色、10 种任务状态色、成功/警告/危险、
  图表色板。全文件 802 行的硬编码 hex/rgba 替换为 `var(--…)`；状态色在两套主题下分别调亮度
  （深色下降饱和、提亮度，参考 cc-switch 的暗色适配）。
- 切换机制：`document.documentElement.dataset.theme`；`main.js` 设置的 `theme` 支持
  `'light' | 'dark' | 'system'`（system 用 `nativeTheme.shouldUseDarkColors` 并监听其 changed 事件
  推送渲染进程）；顶栏加主题按钮（循环三态，图标随态），持久化进 settings.json。
- e2e 补检查：切换后 `documentElement.dataset.theme` 生效且 body 背景色变化。

### F2. 模型选择链路修复（对齐引擎别名格式）

- `COMMON_MODELS` 重构为结构化常量 `BUILTIN_PROVIDERS`：按后端 `agentcrew/models/config.py` 的
  9 个内置 provider 逐一对齐（openai/anthropic/deepseek/doubao/qwen/ernie/ollama/mock/cli），
  每项 `{ key, label, base_url, api_key_env, suggested_models: [...] }`——**这份常量同时是 F3 的
  离线数据源**，放 renderer.js 顶部独立区块。
- 新建运行弹窗（`openNewRunModal`，`renderer.js:2362`）加模型选择区：provider 下拉（来自
  `GET {apiBaseUrl}/providers`，失败/未配置时回落 `BUILTIN_PROVIDERS`）→ 模型下拉（suggested_models
  + 可手输）→ 组装 `<provider>:<model>` 别名只读展示。**mock 勾选优先于模型选择**（--mock 时别名
  不参与）。
- `main.js` 运行参数接线（`:543-550`）：弹窗模型 > 设置页 defaultModel 的优先级传 `--model`；
  设置页 `maxCostUsd > 0` 且弹窗未显式填写时传 `--max-cost`。空值行为不变。
- e2e 补检查：弹窗含 provider/model 下拉；`run:start` 收到的参数含 `--model <provider>:<model>`
  （可通过 mock 数据或参数快照断言）。

### F3. 第 9 页「供应商管理」（本提示词的核心增量，参考 cc-switch ProviderCard 模式）

- 导航：`index.html` 侧边栏加第 9 个 `data-nav="providers"` 按钮（图标：插头/交换箭头），键盘
  快捷键扩展为 1-9（`renderer.js:2297` 的 pages 数组与设置页快捷键说明同步）。
- 数据源三级降级（沿用 Studio 既有降级风格）：
  1. 配置了 apiBaseUrl → `GET {apiBaseUrl}/providers`（若后端 V4 已实现则含健康状态/延迟）；
  2. 未配置或 404 → `BUILTIN_PROVIDERS` 常量 + 主进程 IPC `provider:keyStatus` 补「已配置 ✓/未配置」
     （主进程读 `process.env[api_key_env]` 只回布尔）；
  3. demo 模式 → 纯常量，顶部沿用「演示模式」徽标。
- 卡片列表（每 provider 一张卡）：图标占位（首字母圆标）、名称与 `base_url`、内置/自定义徽标、
  `api_key_env` 名 + 配置状态点、模型数摘要。卡片操作按钮：
  - **测试**：走主进程新 IPC `provider:test`（用 Electron `net.fetch` 在主进程发
    `GET {base_url}/models`，10s 超时，返回 `{ok, latency_ms, error}`）——主进程代理天然绕开
    渲染进程 CORS 与 CSP，**这是 CSP 只需放行本地后端的原因**；结果内联展示（延迟徽标/错误条），
    连续点击防抖。
  - **模型**：同通道拉取模型列表，展开为标签组展示，可「设为默认」（写设置 defaultModel 并 toast）。
- 「从预设添加」弹窗（自定义 provider 引导）：选内置预设或「空白」→ 表单（名称、base_url 预填、
  api_key_env，占位文案学 cc-switch `templateValues`：明确写「环境变量名，如 MYRELAY_KEY；真实
  Key 请写入 .env 或系统环境变量」）→ 生成 `agentcrew.yaml` 的 `models.custom_providers:` YAML
  片段 + 一键复制按钮。**Studio 不直接写 agentcrew.yaml**（保持只读仓库文件的现状），弹窗明示
  「复制后粘贴到项目根目录 agentcrew.yaml」。
- 空状态与错误态按 cc-switch `ProviderEmptyState` 模式：未配置 apiBaseUrl 且常量不可用时给出
  「到设置页配置后端地址」引导按钮。
- e2e 补检查：第 9 页可达、卡片 ≥ 9、测试按钮存在、添加弹窗可开合。

### F4. 新建运行弹窗体验升级

- 目标输入必填校验（空值时输入框描红 + 行内提示，不 toast 轰炸）；`--demo` 与 `--goal` 互斥的
  现有规则在 UI 上显式表达（勾选 Demo 置灰目标输入）。
- 「最近使用的目标」下拉（主进程 IPC 持久化最近 5 条到 settings.json，`run:start` 成功时记录）。
- 高级选项折叠区（预算上限、max-llm-calls、项目名、慢速），默认收起——现状是全部平铺。

### F5. 设置页分组重构（参考 cc-switch settings 分区）

- 四组卡片：**运行环境**（Python 命令 + 「探测」按钮内联显示探测结果版本、仓库根目录、输出目录）/
  **模型与预算**（默认模型选择器复用 F2 组件、预算上限）/ **连接**（apiBaseUrl + 「测试连接」
  按钮走 `provider:test` 同款 IPC 打后端 `/providers`、结果显示延迟）/ **外观**（主题三选一
  segmented control）。
- 保存按钮改为分组级即时保存或统一保存均可，但保存后必须 toast 回执并实际持久化（现有
  `setSettings` 链路）。

### F6. 细节打磨（穿插在各任务中，不单独立项验收）

- 所有异步加载区补骨架/空态/错误态三态（现状部分页面直接白板等待）。
- toasts 统一时长与位置（现 `toast()` 已统一，检查新代码沿用）。
- 键盘：Esc 关闭抽屉/弹窗的现有逻辑覆盖新弹窗；数字键 1-9 与 F3 同步。
- 连接指示器（`conn-indicator`）三态语义复核：SSE 已连 / 文件监听 / 演示模式，与 F3 数据源
  徽标共用视觉语言。

### F7. 测试与文档收尾

- `e2e.js`：新增检查项追加到 results（预期 15 → 21 项左右），截图补供应商页与暗色主题各一张；
  检查 2 的导航遍历覆盖 9 页并新增「导航按钮含可见文字」断言（防 F0 回归）。
- `selftest.js`：页面枚举同步 9 页；若其导航检查硬编码 8 则更新。
- `studio/README.md`：功能清单更新（供应商页、主题真落地、快捷键 1-9、目录说明），删除与实现
  不符的表述。

---

## 五、验收标准（全部满足才算完成）

1. `cd studio && npm run e2e` 退出码 0，results.json 含既有 15 项 + 新增项全部 passed；
   `npm run selftest` 退出码 0。
2. 手测清单（demo 模式即可完成）：
   - 侧边栏 9 个导航项全部有图标+文字，无空白按钮；数字键 1-9 可切换。
   - 顶栏主题按钮循环 light→dark→system，暗色下看板/DAG/弹窗/设置页无可读性破绽（对比度抽查
     状态徽章与次要文字）；重启后主题保持。
   - 新建运行弹窗选 provider+model → 确认后（可用 `--mock` 短跑或参数快照）`--model` 出现在
     进程参数中，格式为 `<provider>:<model>`。
   - 供应商页：无后端时 9 张内置卡片可见、key 状态与主进程 env 一致（临时 set 一个 env 验证）；
     「测试」对不可达端点显示可读错误而非挂死；「从预设添加」生成的 YAML 片段可直接粘贴使用。
3. `grep -nE '#[0-9A-Fa-f]{3,8}|rgba\(' styles.css` 的命中只允许出现在文件顶部 token 定义区
   （`[data-theme]` 块内）；其余区域零硬编码色值。
4. 密钥自查：新增代码 `grep -rn "api_key" studio/*.js` 的所有命中只涉及环境变量**名**；无任何
   把 env 值写入 settings.json / DOM / 日志的路径。
5. Python 侧零改动：`git status` 不出现 `studio/` 之外的文件变更（README/docs 除外）。

---

## 六、工作方式约定

- 先通读本提示词第二节的文件行号定位，再读 `studio/README.md`、`preload.js` 全文、`renderer.js`
  的 `switchNav`/`openNewRunModal`/`renderSettingsPage` 三段与 `main.js` 的 `registerIpc`/`spawnCli`
  两段，然后动手；cc-switch 参考文件按第一节表格按需查阅，不整目录通读。
- 小步提交：每个 F 任务一个 commit，格式沿用仓库惯例（`feat(studio): …` / `fix(studio): …`，
  中文描述）；F0 与 F1、F2 必须分开提交。
- 主题迁移（F1）是一次纯机械大改，先在分支/独立 commit 完成色值替换并跑 e2e，再叠加交互逻辑，
  便于回滚与 diff 审查。
- 拿不准「降级到什么程度」时，以「demo 模式完整可用」为底线；与红线冲突的捷径宁可功能少做。
- 完成后更新 `studio/README.md` 与根目录 `PROJECT-STATE.md`（V4-FE 小节）。
