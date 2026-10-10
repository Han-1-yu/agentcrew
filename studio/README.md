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

- **运行列表**：扫描输出目录（默认 `E:\agentcrew\output`，兼容 `output/<分组>/<run_id>` 两层结构），显示目标、状态、时间与任务统计，可搜索。
- **启动新运行 / 恢复 / 取消**：调用 `run --goal … --mock --auto --no-live`、`resume <run_id>`、终止进程并补 `cancel` 离线收尾。新建运行弹窗支持 Provider + Model 两级选择（组装 `<provider>:<model>` 别名传 `--model`）、最近使用目标下拉、高级选项折叠（预算 / max-llm-calls / 项目名 / 慢速）。
- **供应商管理**（第 9 页）：9 个内置 Provider 卡片列表（OpenAI / Anthropic / DeepSeek / 豆包 / 通义 / 文心 / Ollama / Mock / CLI），显示 base_url、密钥环境变量配置状态（只显示名字与已配置/未配置布尔，不接触 Key 值）、建议模型数；一键测试端点连通性（主进程 net.fetch 代理，10s 超时，显示延迟或错误）、展开模型列表并「设为默认」；「从预设添加」弹窗生成 `agentcrew.yaml` 自定义 Provider YAML 片段 + 一键复制。
- **实时任务看板**：10 种状态列（规格配色），运行中每 500ms 轮询 `ledger.jsonl` 新增字节（字节偏移增量读）与 `run.json`/`tasks.json` mtime 变化，边跑边刷新。
- **事件流**：13 种 ledger 事件实时滚动，按类型过滤 + 关键字搜索，最多渲染最近 500 条。
- **结束报告**：运行结束自动弹出统计（成功/失败/阻塞/跳过、LLM 调用、token、成本、耗时），数字与 `run.json` 的 `stats` 逐项一致。
- **任务详情**：描述、验收标准、交付物、逐轮评审记录、状态历史、执行结果。
- **产物浏览**：`MANIFEST.json` 驱动的文件列表（缺失标红），文本类产物（.py/.md/.json/.yaml/.html/.txt/.css/.js 等）内联预览、复制、打开位置；可打包导出 zip（内置纯 JS STORE 打包器）。
- **图表**：各角色 LLM 调用次数与成本条形图、门禁通过/驳回比例。
- **明暗主题**：顶栏一键循环 浅色 → 深色 → 跟随系统，持久化到 settings.json；深色模式下降饱和提亮度，状态徽章与次要文字对比度达标；system 模式监听 nativeTheme 变化自动跟随。
- **设置页分组**：四组卡片——运行环境（Python 命令 + 即时探测版本 / 仓库根目录 / 输出目录）、模型与预算（默认模型选择器 / 预算上限）、连接（后端 API 地址 + 即时测试连通性）、外观（主题三选一 segmented control）。
- **键盘快捷键**：`Esc` 关闭弹窗/面板，`Ctrl/⌘+N` 新建运行，数字键 `1-9` 切换 9 个页面。
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

## 自测

```bash
npm run selftest   # electron . --selftest
```

驱动真实 UI 走完：列表 → 看板（含 v2verify 失败/阻塞数据）→ 任务详情 → 事件流 → 产物预览 → 图表 →
设置 → 启动一次 `--mock` 运行（验证运行中实时更新与结束报告）→ 取消运行（验证进程终止）。
截图与 `results.json` 写入 `studio/selftest/`，全部通过时进程退出码为 0。

## 目录

```
studio/
├── package.json     # 依赖只有 electron；scripts: start / selftest / e2e
├── main.js          # 主进程：设置、Python 探测、run 扫描、CLI 子进程、ledger 轮询、IPC（含 provider:test/keyStatus）、nativeTheme、关窗保护
├── preload.js       # contextBridge 暴露 window.studio（约 35 个通道）
├── index.html       # 主窗口骨架（9 页侧边栏导航 + CSP 精确放行 localhost）
├── renderer.js      # 渲染进程（原生 JS，h()/s() DOM 构建 + 全量重渲染）
├── styles.css       # 样式（明暗双主题，95 个 CSS 变量，正文零硬编码色值）
├── zip.js           # 纯 JS ZIP（STORE）导出
├── e2e.js           # E2E 自动化测试（21 项检查，--e2e 时加载）
├── selftest.js      # 自测驱动（--selftest 时加载）
├── e2e-out/         # E2E 产物（截图 + results.json，gitignore）
└── selftest/        # 自测产物（截图 + results.json，gitignore）
```

## 设计边界

- 不修改 `agentcrew/` 与 `main.py` 任何代码；后端升级只要 CLI 与文件格式不变，Studio 无需改动。
- 零框架、零打包器：唯一 npm 依赖是 `electron`，沿用原生 HTML/CSS/JS + h()/s() DOM 构建。
- 密钥零接触：Studio 不读取、不显示、不存储 API Key 的值；只显示环境变量名与「已配置/未配置」布尔（主进程读 process.env 后只回传布尔）。
- 可选 HTTP API：设置页配置 apiBaseUrl 后启用 SSE 实时推送与供应商列表拉取；端点测试走主进程 net.fetch 代理（绕开渲染进程 CORS）；CSP 的 connect-src 精确放行 `http://localhost:*` 与 `http://127.0.0.1:*`。未配置时完整降级为文件监听 + 演示模式。
