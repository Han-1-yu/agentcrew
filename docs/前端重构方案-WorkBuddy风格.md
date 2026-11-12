# AgentCrew Studio 前端重构方案：对齐 WorkBuddy 设计风格

> 状态：**已实施**（2026-11-09）
> 范围：studio/ 渲染层（CSS + index.html + 主题默认值），不触碰任何数据与业务逻辑
> 验收结果：`npm run check` 通过（64 JS / 38 CSS 分片）、`npm run e2e` 28/28、`npm run selftest` 16/16、`npm run visual:update` 重建 18 张基线
>
> 实施说明（与下方方案的差异，均为落地时的等价简化）：
> - 未新建 `tokens-dark.css`：保留原有 `tokens.css`（:root 深色扁平）+ `tokens-light.css`（:root[data-theme="light"] 浅色扁平）双文件结构，仅重写取值；默认主题由 `boot-theme.js` / `main.js` / `shell.js` 的回退值改为 light 实现，架构与护栏完全不变。
> - `--glass-*` / `--blur-*` / `--aurora-*` / `--glow-*` / `--shadow-*` 变量名保留但值置为扁平/透明/0/none，组件可继续引用而不产生旧效果；极光 DOM 层已从 `index.html` 删除。
> - 全局清扫的实测数字：600/700 字重 63 处、uppercase 16 处、blur 15 处、1px 边框 94 处、硬编码 rgba 1 处，全部归零或收敛到 token。
> - 落地后修正：`--bg-subtle` 由与页底同色的 `#F1EFE8` 调整为略深一档的 `#E8E6DE`，使指标卡与 hover 面可见。

---

## 1. 背景与目标

### 1.1 现状（"Aurora Glass"）

studio 当前视觉方向是深色玻璃拟态：深蓝黑渐变底 + 三团极光光晕、面板半透明毛玻璃
（backdrop-filter blur 20–32px）、1px 白色低透明描边、14–26px 大圆角、标题 600 字重、
小号全大写 eyebrow 标签（19 处 `text-transform: uppercase`）。

### 1.2 目标（WorkBuddy 设计语言）

WorkBuddy 是**浅色扁平**风格，核心特征：

| 维度 | WorkBuddy 规范 |
|---|---|
| 表面 | 纯白卡片（`background-primary`）、浅灰页面底、**无渐变 / 无模糊 / 无光晕 / 无玻璃** |
| 边框 | 0.5px 实线，按透明度分三级：0.15（默认）/ 0.30（hover）/ 0.40（强调） |
| 圆角 | 8px（md，控件）/ **12px（lg，卡片首选）** / 16px（xl，大容器）/ 胶囊仅用于 chips |
| 字体 | 正文 13px / 400 / 行高 1.6；标题 h1=15 h2=14 h3=13 全部 **500**；**全局只用 400 与 500 两档字重** |
| 大小写 | **Sentence case**，禁止 Title Case 与 ALL CAPS |
| 配色 | 9 条色板 × 7 级（50 浅底 → 900 深字），浅色模式用「50 底 + 600 描边/强调 + 800 标题」 |
| 语义色 | info / success / warning / danger 四语义，底色用 50/100 级，文字用 600/800 级 |
| 阴影 | 默认无阴影，仅保留功能性焦点环（2px 实线强调色） |
| 节奏 | 垂直节奏用 rem（1 / 1.5 / 2rem），组件内间距用 8/12/16px |
| 指标卡 | 浅灰底无边框圆角 8，上 muted 13px 标签、下 24px/500 数字 |

### 1.3 重构原则

1. **只改表现层**：所有 Python CLI 调用、run 扫描、ledger 轮询、SSE、store、router、
   preload/main 的 IPC 一律不动。
2. **Token 优先**：studio 的 95 个 CSS 变量体系保留变量名，**重写取值**。组件分片里
   绝大多数样式已经引用变量，改两个 token 文件即可让全站换肤；剩余少量硬编码
   （blur 15 处、rgba 1 处、600/700 字重 68 处、uppercase 19 处）逐个清扫。
3. **双主题机制保留**：light / dark / system 三态切换与持久化是既有业务功能，保留；
   但 light 改为 WorkBuddy 浅色扁平（默认主题），dark 改为对应的扁平深色
   （800 级底 + 200 级描边/文字），两套都是"同一套扁平语言"。
4. **架构护栏不破**：分层单向、单文件 ≤300 行、入口收敛
   （`npm run check` 持续通过）。

---

## 2. 视觉映射表（旧 → 新）

中性色采用 WorkBuddy 暖灰 ramp（`c-gray`），强调色采用品牌紫 ramp（`c-purple`）。

| 语义 | 旧值（Aurora Glass 深色） | 新值（WorkBuddy 浅色，默认） | 新值（WorkBuddy 扁平深色） |
|---|---|---|---|
| 页面底 `--bg-page` | #070B16 + 极光渐变 | **#F1EFE8**（纯色，无渐变） | #2C2C2A |
| 面板 `--bg-panel` | rgba(255,255,255,.042) 玻璃 | **#FFFFFF**（实底） | #444441 |
| 内嵌底 `--bg-subtle` / `--bg-inset` | 白低透明 | #F1EFE8（gray-50） | #444441 / rgba(241,239,232,.06) |
| 浮层 `--bg-float` | #121A2E | #FFFFFF | #444441 |
| 正文 `--text` | #EDF2FF | **#2C2C2A**（gray-900） | #F1EFE8 |
| 次要文字 `--text-sub` | 白 64% | #5F5E5A（gray-600） | #B4B2A9（gray-200） |
| 提示文字 `--text-ter` | 白 38% | #888780（gray-400） | rgba(241,239,232,.45) |
| 边框 `--border` | 白 9.5% | rgba(44,44,42,.15) | rgba(241,239,232,.15) |
| 边框 hover `--border-input` | 白 14.5% | rgba(44,44,42,.30) | rgba(241,239,232,.30) |
| 强调 `--accent` | #6E8CF7 靛蓝 | **#534AB7**（purple-600） | #AFA9EC（purple-200） |
| 强调浅底 `--accent-bg` | 靛蓝 14% | **#EEEDFE**（purple-50 实色） | rgba(175,169,236,.12) |
| 成功 | #4ADE80 | 字 #3B6D11 / 底 #EAF3DE | 字 #97C459 / 底 rgba(151,196,89,.12) |
| 警告 | #E2B45C | 字 #854F0B / 底 #FAEEDA | 字 #EF9F27 / 底 rgba(239,159,39,.12) |
| 危险 | #F0706B | 字 #A32D2D / 底 #FCEBEB | 字 #F09595 / 底 rgba(240,149,149,.12) |
| 信息（运行中/就绪） | #5AA8FF / #6C9BFF | 字 #185FA5 / 底 #E6F1FB | 字 #85B7EB / 底 rgba(133,183,235,.12) |
| 圆角 | 9/14/18/22/26 | **6/8/12/16**，pill 仅 chips | 同左 |
| 阴影 | 三级投影 + glow | **无**（浮层仅 mask + 0.5px 边框） | 同左 |
| 玻璃/模糊 | blur 20–32px + saturate | **全部删除** | 同左 |
| 极光层 | 3 团 radial + blur(72px) | **删除 DOM 与 CSS** | — |

任务状态（10 种）芯片统一映射到「50 底 + 600 字」：

| 状态 | ramp |
|---|---|
| running / ready | c-blue |
| succeeded | c-green |
| failed | c-red |
| review / rework / blocked | c-amber |
| pending / skipped / cancelled | c-gray |

---

## 3. 需要改动的模块 / 页面 / 组件清单

### 3.1 全局层（收益最大，先做）

| 文件 | 改动 |
|---|---|
| `styles/tokens.css` | **整文件重写取值**：按 §2 映射表改为 WorkBuddy 浅色（`[data-theme]` 无值时默认浅色）；删除 aurora / glass / blur / glow / scrim 变量组；圆角 token 收敛为 6/8/12/16 + pill；阴影 token 删除或置空 |
| `styles/tokens-light.css` | 重写为同语言的浅色微调版（若默认即浅色，此文件改为存放「浅色专属微调」，变量名不变，保住 ≤300 行护栏与加载顺序） |
| `styles/base.css` | h1–h4 字重 600→**500**；`.eyebrow` 去掉 uppercase 改 12px 次要色 sentence case；滚动条 thumb 改 gray-200/300；`::selection` 改 purple-50/紫字 |
| `boot-theme.js` | 默认主题 `'dark'` → `'light'` |
| `main.js` | `defaultSettings.theme` `'dark'` → `'light'`（**唯一**触碰 main.js 的一行） |
| `index.html` | 删除 `<div class="aurora">`；`sb-group-label` 文案 `Projects/Recents` → `项目/最近`；顶栏指标条加 metric 卡语义 class |

### 3.2 布局层（3 个文件）

| 文件 | 改动 |
|---|---|
| `styles/layout/shell.css` | 删除 aurora 渐变背景与 `.aurora` 规则；`.layout` 改纯色 `--bg-page`；`.main-col::after` scrim 渐隐带改纯色同底色淡出或删除；页面 padding 改 1.5rem 垂直节奏 |
| `styles/layout/sidebar.css` | 侧边栏改**白底 + 右侧 0.5px 边框**（去掉玻璃 blur 12 行）；导航 active 态 = purple-50 实底 + gray-900 字 + 左侧 2px 紫条；品牌区/分组标签按新排版；底部运行元信息改 metric 卡语言 |
| `styles/layout/topbar.css` | 顶栏改白底 + 底部 0.5px 边框；指标条改 WorkBuddy metric 卡（muted 13px 标签 + 500 字重数字）；图标按钮改 ghost（透明底 0.5px 边框，hover 边框加深） |

### 3.3 组件层（styles/components，13 个文件）

| 文件 | 改动 |
|---|---|
| `buttons.css` | 全部按钮扁平化：默认 ghost（透明底 + 0.5px border + radius 8）；`btn-primary` 改 purple-600 实底白字（无渐变无投影）；`btn-icon` ghost 化；hover 只加深边框与浅灰底 |
| `blocks.css` | 卡片：白底、0.5px 边框、**radius 12**、padding 1rem 1.25rem；删除 backdrop-filter 两行；block-title 字重 500 |
| `status-bar.css` | 运行状态条改白卡；状态芯片按 §2 状态色表换 50 底 600 字；统计数字 500 字重 |
| `agents.css` | Agent 卡白底 0.5px 边框；头像圆改 purple-50 底 + purple-600 字（WorkBuddy 头像规范：44px 圆） |
| `gates.css` | 门禁卡片：通过 c-green、驳回 c-red 的 50 底语义卡 |
| `cost.css` | 成本条形图配色改 ramp（每角色一条 ramp 或统一 purple 分级） |
| `events.css` | 事件行 hover 改浅灰底；事件类型 chips 按语义色 50 底 |
| `dag.css` | 节点改**纯色扁平**：agent 色 50 底 + 600 描边 0.5px + 800 标题/600 副题；连线 0.5px gray-400，热路径 purple-600；删除 glow 与玻璃注释 |
| `composer.css` | 命令输入框对齐 WorkBuddy 聊天框：白卡 0.5px 边框 radius 12、聚焦 2px purple-600 ring；发送键改 purple-600 实心方形圆角（删白色渐变键与投影）；限宽居中（max-width ~760px） |
| `detail.css` | 详情抽屉：白底 0.5px 左边框；mask 改 gray-900 32% 纯色；删 blur 两处 |
| `modal.css` | 模态：白卡 radius 12 + 0.5px 边框 + 轻投影（唯一允许的例外，或纯 mask）；mask 去 blur |
| `empty.css` | 空态插画色改 gray-400/purple；删 blur |
| `toast.css` | Toast 改语义色 50 底 + 600 字（success/warning/error/info 四档），白底深字，去深色底 |

### 3.4 功能页层（styles/features，15 个文件）

`agents-page / artifacts / dag-page / events / flow / gates-page / plan / providers(+extra) /
runs / settings / new-run / agents-matrix / agent-switch / model-switcher` ——
统一动作相同（机械清扫为主）：

- 删除残余 blur / 玻璃 / 投影 / 渐变引用（grep 置零）；
- 边框统一 0.5px token、卡片 radius 12；
- 字重 600/700 → 500（占全站 68 处的大头）；
- uppercase 19 处 → sentence case；
- 状态/语义色换 ramp 芯片；
- 设置页 segmented control、供应商卡片、新建运行表单对齐 WorkBuddy 表单规范
  （裸标签 + 0.5px 边框输入框、radius 8）。

### 3.5 JS 侧（不改逻辑，仅两处例外）

- `src/` 下**零逻辑改动**（grep 确认无 `eyebrow` 类引用、无内联硬编码色）；
- 唯一允许：若个别 JS 生成的 label 文案是 ALL CAPS（如 `PROJECTS`），改 sentence case。

### 3.6 页面影响（9 页 + 弹层全覆盖）

1. 运行总览（状态条 / Agent 依赖图 / Agent 协作 / 质量门禁 / 成本 / 流式输出 / 最近事件）
2. 计划审查 3. 任务图 4. Agents 5. 质量门禁 6. 产物 7. 事件日志 8. 供应商 9. 设置
+ 新建运行模态、任务详情抽屉、结束报告、运行切换下拉、Toast、底部命令输入框。

---

## 4. 分期实施计划

| 阶段 | 内容 | 产出 / 验证 |
|---|---|---|
| **P0 令牌重写**（约半天） | §2 映射表落进 tokens.css / tokens-light.css；boot-theme 与 main.js 默认主题改 light | 全站立即换肤；`npm run check` 过 |
| **P1 骨架扁平化**（约半天） | shell / sidebar / topbar + index.html 去 aurora；侧边栏/顶栏白底细边框 | 肉眼走查 9 页导航 |
| **P2 全局排版清扫**（约 1 天） | base.css 字重/eyebrow/滚动条；全站 68 处 600→500、19 处 uppercase、15 处 blur、1 处硬 rgba 清零（grep 验证为 0） | `grep` 四项指标归零 |
| **P3 组件对齐**（约 1–1.5 天） | §3.3 的 13 个组件文件逐个按 WorkBuddy 规范重写关键规则 | 详情抽屉 / 模态 / composer 走查 |
| **P4 页面与交互统一**（约 1 天） | §3.4 的 15 个 feature 文件；hover=边框加深+浅灰底、焦点环 2px 紫、动效只保留 transform/opacity 且 ≤200ms；文案 sentence case | e2e 28 项全绿 |
| **P5 回归与基线重建**（约半天） | `npm run check && npm run e2e && npm run selftest`；`npm run visual:update` 重建 16 张本机视觉基线 | 三套测试 + 新基线 |

总计约 4–5 个工作日。P0 完成后即可运行查看整体方向，中途可随时叫停回滚
（git 分支：`refactor/workbuddy-style`）。

---

## 5. 交互模式统一（WorkBuddy 特征落地）

1. **hover 语言**：玻璃高亮 → 「边框 0.15→0.30 透明度加深 + 底色白→gray-50」，无位移无缩放。
2. **焦点环**：`:focus-visible` 2px 实线 purple-600、offset 2px（现有机制保留，仅换色）。
3. **选中/激活**：导航 active = purple-50 底 + 900 字；不再用发光。
4. **浮层层级**：mask（纯色 32%）+ 0.5px 边框白卡表达空间，而不是投影+模糊。
5. **命令输入框**：对齐 WorkBuddy 对话框形态 —— 居中限宽白卡、左下附件/权限 chips、
   右下模型选择 + 实心紫发送键；`Ctrl/⌘+Enter` 发送语义保留。
6. **动效收敛**：只留 transform / opacity，时长 ≤200ms（`--dur-2` 收敛为 200ms），
   尊重 `prefers-reduced-motion`（现有支持保留）。
7. **数字表达**：指标数字统一 500 字重、`tabular-nums`（现有，保留）。

---

## 6. 不改动清单（安全边界）

- `main.js`（除默认主题 1 行）、`backend-api.js`、`preload.js`
- `src/lib/`、`src/contracts/`、`src/app/`（boot / router / shell / realtime / data / mock）
- `src/features/**` 的全部 JS 业务逻辑（渲染 DOM 结构不动，仅极少数 label 文案大小写）
- 后端 `agentcrew/` Python 包、`main.py`、output/ 数据、测试脚本 `e2e.js / selftest.js`
  （若 e2e 断言中存在依赖旧视觉的选择器/截图，在 P5 一并更新）

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 视觉回归基线全失效（预期内） | P5 `npm run visual:update` 重建，之后改样式仍受保护 |
| 300 行单文件护栏（tokens 重写后行数变化） | 语义色分片到 tokens-light.css / 必要时新增 tokens-status.css（仍走 index.css @import） |
| 深色主题对比度回归 | dark 值全部按 ramp 200/800 级取，肉眼走查 12-暗色主题截图页 |
| e2e / selftest 选择器失效 | 只动 class 不动 id；P4 跑全量测试兜底 |
| 回滚 | 全程在 `refactor/workbuddy-style` 分支；token 文件独立提交，可按阶段 revert |
