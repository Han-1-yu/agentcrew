# AgentCrew Studio · 表现层重构记录（Aurora Glass）

> 目标：把现有界面重构为参考视觉稿的「深蓝黑玻璃拟态」语言——
> 布局结构、配色、组件样式、交互细节四件事对齐，**业务逻辑与数据链路一行不改**。
> 原则：改的是「变量值 + 样式分片 + 少量 DOM 装配」，不动 API、store、状态机。

---

## 0. 一句话结论

界面从「暖色纸感 Console Ink（铜色强调）」换成「深蓝黑 Aurora Glass（靛蓝强调 + 毛玻璃）」。
换肤的主要杠杆是 `styles/tokens.css`：**组件全部只引用变量**，所以配色是一次全站生效的替换，
而不是 37 个样式分片逐个改颜色。结构性的变化集中在壳层（侧边栏 / 顶栏 / 命令框）与区块卡片。

---

## 1. 逐区域改动清单

### 1.1 应用壳（背景 / 整体骨架）

| 项 | 目标样式 | 改动范围 |
| --- | --- | --- |
| 背景 | 深蓝黑渐变 `#0A1224 → #070B17 → #04060F`，叠三团大模糊蓝紫光晕（`filter: blur(72px)`），不用任何图案 | `styles/layout/shell.css`；`.aurora` 三个色团在 `index.html` 里，纯装饰 `aria-hidden` |
| 布局 | 由「顶栏压在两列之上」改为**两列两行**：左侧通高侧边栏 + 右侧主列（指标条 / 内容 / 命令框） | `index.html` 结构：`#sidebar` 与 `.main-col` 成兄弟节点，`#topbar` 移进 `.main-col` |
| 主列 | `display:flex; flex-direction:column`，内容区独立滚动 | `styles/layout/shell.css` |
| 窗口底色 | 与页面同色，启动不闪白 | `main.js` → `backgroundColor: '#070B16'` |

### 1.2 侧边栏（导航栏）

| 项 | 目标样式 | 改动范围 |
| --- | --- | --- |
| 宽度 | 22%（232–304px 夹紧），通高 | `styles/layout/sidebar.css` |
| 材质 | 半透明玻璃：`--glass-bg-quiet` + `backdrop-filter: blur(26px) saturate(150%)` + 右侧 1px 白描边 | 同上 |
| 顶部 | 品牌字 + 小方块 logo + 下拉箭头，右侧一颗搜索图标（点开运行切换下拉） | `index.html` `.sb-head`；`boot.js` 绑定 `#btn-search` |
| 新建入口 | `+ 新建运行` 作为第一条，比普通导航亮一档 | `index.html` `#btn-new-run`（id 保留，e2e 依赖） |
| 导航项 | **去掉图标**，纯文字行，31px 高、9px 圆角 | `index.html` 8 个 `.nav-item`；样式 `.nav-row` 基类 |
| 选中态 | 深色圆角胶囊：`--glass-bg-strong` + 1px 白描边 + 顶部高光；去掉原来的「左侧铜色标尺」 | `styles/layout/sidebar.css` `.nav-item.active` |
| Projects 分组 | 小写字母间距标签 + 文件夹图标 + 项目名（= 当前运行，点击开运行切换）+ 缩进任务列表（最多 5 条，超出折叠成「还有 N 个任务…」）+ `Show more` | `index.html` `.sb-group`；数据由 `shell.js renderSidebarTasks()` 从 `S.dash.tasks` 渲染；`Show more` → 计划审查页 |
| Recents 分组 | 最近 5 条运行，小圆点 + 单行文字，点击切换运行 | `runs/index.js renderSidebarRecents()`，与运行下拉共用同一份 `listRuns` 结果，不新增数据通道 |
| 底部 | 运行元信息（Provider / 模型 / 预算条 / 连接状态，4 行压缩）+ `设置` 入口 | `index.html` `.sb-foot`；`shell.js` 所有写入改为空值安全 |
| 运行下拉 | 从顶栏左下移到侧边栏内，贴着项目名展开，圆角 14 + 玻璃描边 | `styles/layout/sidebar.css` `.run-dropdown`（`rd-*` 规则一并从 topbar 迁移过来） |

### 1.3 顶栏（指标条）

| 项 | 目标样式 | 改动范围 |
| --- | --- | --- |
| 形态 | 不再是「品牌 + 主按钮 + 工具条」，而是**一行精简指标**：`● 状态 · N Agents · N 任务 · tok / 费用 / 预算条 / 百分比 · 连接状态` | `index.html` `.tb-metrics`；`styles/layout/topbar.css` |
| 高度 | 54px，透明底（不再有 `border-bottom` 与实色背景） | 同上 |
| 数字 | 全部 `tabular-nums`，与标签灰度区分（数字亮、标签暗） | `styles/layout/topbar.css` |
| 右侧 | 主题 / 刷新 / 通知 / 账户 四个圆形图标按钮 | 保留 id，样式换 `.btn-icon` 正圆 |
| 拖拽 | 整条 `-webkit-app-region: drag`，控件 `no-drag` | 同上 |

### 1.4 命令输入框（新增组件）

| 项 | 目标样式 | 改动范围 |
| --- | --- | --- |
| 位置 | 主列底部居中悬浮，宽 `min(760px, 100% - 96px)` | 新增 `styles/components/composer.css` |
| 项目条 | 输入框上方的小胶囊：状态点 + 项目名 + `· 自动执行` + 箭头 | `index.html` `.composer-bar` |
| 输入卡 | 26px 大圆角 + 双层玻璃 + `--shadow-3`；`focus-within` 提亮描边 | `composer.css` `.composer-box` |
| 左下 | 圆形 `+`（打开完整新建运行表单）+ `Full access` 标签 | 同上 |
| 右下 | 模型选择（当前运行模型）+ 白色圆形发送键 | 同上；发送键颜色走 `--cmp-send-bg/fg`，浅色主题自动变成实心靛蓝 |
| 行为 | 输入目标 → 发送 / `⌘/Ctrl+Enter` → 打开新建运行弹窗并**预填目标**；空输入只聚焦并给一条提示 | 新增 `src/features/composer/index.js`；`new-run/index.js` 增加 `opts.goal` 预填 |
| 显隐 | 只在「运行总览」出现，其他页面隐藏（避免盖住表单） | `src/app/router.js` |
| Full access | 与新建运行弹窗的「自动执行」共用 `agentcrew:nrAuto` 键，点击即切换 | `composer/index.js` + `new-run/index.js` |

### 1.5 区块卡片（Blocks）

| 项 | 目标样式 | 改动范围 |
| --- | --- | --- |
| 卡面 | 半透明玻璃 `--glass-bg` + 1px 白描边 + 顶部 1px 高光 + 18px 圆角 + `--shadow-2` | `styles/components/blocks.css` `.dash-block` |
| 卡头 | 发丝分隔线，标题字重 600 | 同上 |
| 运行抬头 | 由「独立卡片 + 左侧彩色标尺 + 扫描光带」改为**无卡的页面抬头**：目标大字 + 阶段胶囊 + 状态 chip + 六列指标 + 动作按钮 | `styles/components/status-bar.css` |

### 1.6 按钮

| 项 | 目标样式 | 改动范围 |
| --- | --- | --- |
| 常规 | 玻璃面（半透明底 + 白描边 + 顶部高光），9px 圆角 | `styles/components/buttons.css` |
| 主操作 | 全页唯一实心：靛蓝渐变 + 柔和外发光 | 同上 `.btn-primary` |
| 小操作 | 26px 高，6px 圆角 | 同上 |
| 图标按钮 | 正圆 30px，hover 才出现玻璃面 | 同上 `.btn-icon` |

### 1.7 依赖图（视觉重点）

| 项 | 目标样式 | 改动范围 |
| --- | --- | --- |
| 画布 | 去掉点阵网格与四角标记，只留一层极淡径向光；高度 `clamp(320px, 40vh, 480px)` 跟着窗口走 | `styles/components/dag.css` |
| 节点卡 | 中性毛玻璃：`--dag-node-fill`（白 5.5%）+ 1px 白描边，**不再按角色染色**；顶部保留中性白 sheen | 同上 + `dag-agent-view.js`（sheen 改白色） |
| 头像 | 中性玻璃圆（`--avatar-fill`），汉字墨色文字 | 同上 |
| 角色色 | 8 个角色不再有彩虹色：`--purple/--pink/--sky/--orange/--indigo` 全部收进冷蓝灰一档 | `styles/tokens.css`；`ROLE_META` 不动（仍引用同一批变量） |
| 状态 | 整块彩色胶囊 → **一颗 2.8px 小圆点 + 极淡文字** | `dag-agent-view.js`（`.agent-state` → `.agent-state-dot`）；`dag.css` 对应规则 |
| 连线 | 统一冷白 `--dag-edge`，线宽仍编码依赖任务数；只有 live 态用亮色 + 流动虚线 | `dag.css`；`dag-agent-view.js` 里 `--edge-color` 改指 `--dag-edge` |
| 箭头 | 统一冷白，不再按角色上色 | `dag-canvas.js arrowDefs()` |
| 层级带 | 去掉虚线框（`stroke-dasharray`），改成一块无描边的极淡底衬 | `dag.css .dag-tier` |
| 交互 | 悬停抬升 / 上下游聚焦压暗 / 点节点开详情 全部保留 | 未改逻辑 |

### 1.8 表单与浮层

| 项 | 目标样式 | 改动范围 |
| --- | --- | --- |
| 模态框 | 从不透明化：`--bg-float` + 18px 圆角 + 玻璃描边 + `--shadow-3`；遮罩加 4px 背景模糊 | `styles/components/modal.css` |
| 输入框 / 下拉 | 沿用原结构，颜色与圆角随令牌自动更新（`--border-input`、`--r-sm` 都换了值） | 无需逐条改 |
| 详情面板 | 背景随令牌变深，仍是不透明浮层 | `styles/components/detail.css`（仅继承令牌变化） |

---

## 2. 令牌层改动（全站生效的杠杆）

`styles/tokens.css` 重写 + 新增 `styles/tokens-light.css`（浅色覆盖层，拆分是为了守住
「单个样式分片 ≤ 300 行」的架构护栏）。

| 令牌组 | 变化 |
| --- | --- |
| 背景四层 | 暖纸→深蓝黑；`--bg-panel` 从实色变成 4.2% 白（卡片因此天然半透明） |
| 玻璃组（新增） | `--glass-bg` / `--glass-bg-strong` / `--glass-bg-quiet` / `--glass-border` / `--glass-highlight` / `--blur-panel|card|float` / `--glass-saturate` |
| 极光组（新增） | `--aurora-1|2|3` + `--aurora-base-from|mid|to` |
| 强调色 | 铜 `#A9531F` → 靛蓝 `#6E8CF7`（浅色 `#3D5FD9`） |
| 文字 | 暖黑墨 → 冷白三级（`#EDF2FF` / 64% / 38%） |
| 状态色 | 重调色相与透明度，运行中是亮蓝、通过是绿、失败是软红；仍是页面上唯一允许的饱和色 |
| 装饰色 | 5 个角色色全部降为冷蓝灰，节点不再彩虹 |
| 圆角 | 4/6/8/12 → 6/9/14/18/22/26 六档（大圆角是玻璃拟态的关键） |
| 阴影 | 暖调灰 → 冷蓝黑，且更柔更散 |
| 字体 | 标题字体由 Bahnschrift（窄体 DIN 感）换成 SF Pro / PingFang 系；等宽数字保留 |
| 命令框（新增） | `--cmp-send-bg/fg/shadow` |

同时修正一个隐患：浅色主题选择器从 `[data-theme="light"]` 收紧为 `:root[data-theme="light"]`——
设置页的主题分段按钮也带 `data-theme` 属性，全局选择器会把它整块刷成浅色。

---

## 3. 交互细节

- 导航：hover 只加 5% 白底，选中项是胶囊；每秒刷新不再重建 DOM（`setText` 值不变不写、侧栏任务列表带签名去重），hover 与选中态不会被闪掉。
- 命令框：`focus-within` 提亮描边；`⌘/Ctrl+Enter` 启动；输入自动长高（上限 148px）；空输入不弹错误，只给一条 info。
- 依赖图：悬停抬升 2px、只对上游/下游提亮；运行中节点描边呼吸（2.6s），其余静止。
- 主题切换：沿用原有 `theme-switching` 临时禁过渡，避免几百个元素同时跑动画。
- 降级：`prefers-reduced-motion` 全站生效（原有规则保留）。

---

## 4. 明确没有动的东西

- 所有 `window.studio.*` IPC 调用、`S`（store）结构、快照/账本轮询、mock 模拟器。
- 新建运行的完整链路：命令框只做「预填 + 打开弹窗」，启动仍是 `startRun`。
- 路由与页面：9 个页面入口一一对应，`switchNav` 的 key 未变。
- 依赖图的数据与几何：分层算法、边权、回环判定、缩放平移、点击开详情全部原样。
- 测试契约：`#btn-new-run`、`#tb-workspace`、`#run-dropdown`、`#conn-indicator`、`.nav-item[data-nav]`、
  `#nr-*` 等选择器全部保留（e2e 28 项断言全绿）。

---

## 5. 验证

```bash
cd studio
node tools/check-architecture.js     # 60 模块 / 37 样式分片，分层·导入·体量·入口全过
npx electron . --e2e                 # 28/28 通过，16 张截图 → e2e-out/
npx electron . --selftest            # 运行时自检
```

视觉基线 `visual-baseline/` 是旧皮肤的快照，本次属于**预期内的全站视觉变更**，
需要时执行 `npm run visual:update` 重新基线（不要在未确认变更的前提下直接更新）。

---

## 6. 已知取舍

1. 参考稿是「一张图 + 一个输入框」的极度留白构图；真实看板有 5 个区块，
   因此**版式语言对齐、构图密度不等于参考稿**——依赖图通过抬高卡片层级、压缩抬头、
   限制画布高度，成为首屏主体。
2. 参考稿限定「节点状态只用绿点」；真实 run 有 10 种状态、状态文字对排查很有用，
   所以保留文字，只把「彩色胶囊」降级为「小圆点 + 极淡文字」。
3. 窗口控件用系统原生标题栏，没有自绘假控件（自绘会让 macOS/Windows 行为不一致，
   且 `titleBarOverlay` 的半透明在 Windows 上不保证生效）。

---

## 7. 第二轮：放宽密度（对应"界面过于拥挤"的反馈）

问题诊断：头部四行（指标条 / 抬头 / 六列读数 / 五个按钮）堆在 200px 内；
依赖图画布是固定高度，图形按比例缩在中间，四周全是空白；命令框压住下一张卡片。

### 7.1 呼吸感（间距 / 留白）

| 位置 | 改前 | 改后 |
| --- | --- | --- |
| 看板外边距（上/左右/下） | 16 / 24 / 208 | 24 / 32 / 192 |
| 区块之间的间距 | 12px | 20px |
| 卡片内边距 | 16 / 20 | 20 / 24 |
| 卡片圆角 | 18px | 22px |
| 卡头与内容的距离 | 头下 8 + 头后 12 | 头下 12 + 头后 16 |
| 抬头读数行 | 上 12 / 上内 12 | 上 20 / 上内 16 |
| 读数列内边距 | 16px | 20px |
| 抬头按钮行 | 上 12 / 间隔 8 | 上 20 / 间隔 10 |
| 顶栏（高/左内距/右内距） | 54 / 26 / 16 | 56 / 32 / 20 |
| 顶栏指标间距 | 10px | 13px |
| 图卡头部控件间距 | 10px | 12px（缩放组内 4 → 6） |

### 7.2 元素尺寸

- **读数数值 15 → 16px，并强制不换行**。原来 `deepseek:deepseek-chat` 折成两行，
  整行被撑高、看起来挤成一团；现在单行 + 省略号。
- **命令输入框整体收紧**：宽 760 → 720，内边距 15/15/12 → 12/14/10，输入区 42 → 36px，
  圆角 26 → 22px，加号 27 → 26px、发送键 33 → 32px。收起后高度约 148px（原 180+），
  不再压住下一张卡片。

### 7.3 排列方式

- **依赖图画布不再固定高度**：由 JS 按图形 `总宽/总高` 写入 `aspect-ratio`，
  画布贴合图形比例（原来固定 480px，图按比例缩在正中，左右各留一大片空白）；
  仅在矮窗口由 `max-height: clamp(340px, 40vh, 480px)` 兜底。
  实测节点宽度从 ~100px 提升到 ~150px，图占满卡片宽度。
- **去掉"卡中卡"**：画布自身的描边、底色、内阴影全部移除，图形直接坐在玻璃卡片上。
- **新增底部渐隐带**（`.main-col::after`，172px，`--scrim`）：内容滚到底部时柔和淡出，
  命令框是"浮在留白上"而不是"压住内容上"；切换到其他页面时渐隐带一起关闭。

### 7.4 未改

数据、路由、交互逻辑与上一轮完全一致；e2e 28/28、架构检查、视觉基线均已复核。
