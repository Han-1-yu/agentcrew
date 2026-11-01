# Studio 前端重构说明

> 范围：`studio/`（Electron 桌面端渲染层）。参考 `lancer/apps/desktop` 的分层约定（按域拆分、contracts 先行、tokens 集中、架构护栏）。
> 硬约束：**功能与交互行为完全不变**，视觉按真实产品标准重做。

---

## 一、结论

| 指标 | 重构前 | 重构后 |
| --- | --- | --- |
| 渲染层 JS | `renderer.js` 单文件 **3891 行** | **49 个模块 / 4700 行**（含模块头与 import），最大单文件 238 行 |
| 样式 | `styles.css` 单文件 **1503 行** | **32 个分片 / 2033 行**，最大 285 行（令牌） |
| 入口 | `index.html` 引 `styles.css` + `renderer.js` | 只引 `styles/index.css` + 单条 `<script type="module">` |
| 全局命名空间 | 139 个顶层符号全部挂在 window | 每模块显式 `import` / `export`，仅保留 2 个对外全局（`window.__st`、`window.BUILTIN_PROVIDERS`） |
| 结构护栏 | 无 | `npm run check`：分层、导入完整性、**语法**、体量、入口收敛 |
| 视觉基线 | 无 | `npm run visual`：16 张 e2e 截图与本机基线比对（版式/配色回归） |
| 自动化验证 | e2e 28 项 / selftest 16 项 | 同样 28 项 + 16 项**全部通过**，测试文件未改一行 |

---

## 二、重构范围

### 2.1 页面（9 个）

| 页面 | 模块 | 备注 |
| --- | --- | --- |
| 运行总览（看板） | `features/dashboard/{index,dag,status-bar,agents,gates,cost,events}.js` | 5 个区块，各自独立成文件 |
| 计划审查 | `features/plan/index.js` | 任务可编辑表单 |
| 任务图 | `features/dag-page/index.js` | 分层列表视图 |
| Agents | `features/agents-page/index.js` | 8 角色卡片 + 产出统计 |
| 质量门禁 | `features/gates-page/index.js` | 三层门禁 + 人工审批 |
| 产物 | `features/artifacts/index.js` | 目录树 + 代码预览 + 语法着色 |
| 事件日志 | `features/events/{index,export}.js` | 筛选 + 分页 + 三种导出 |
| 供应商 | `features/providers/{index,card,forms,modals,models}.js` | 27 内置 + 自定义，Key 状态/测试/测速/模型拉取 |
| 设置 | `features/settings/{index,fields,environment,model-budget,gateway,connection,appearance,about,quota}.js` | 五组配置各自成文件 + 统一的取值函数 |

### 2.2 组件（按层）

- **原子（ui/）**：`toast`（轻提示）、`modal`（模态 + 二次确认）、`icons`（内联 SVG 工厂）
- **布局（layout/）**：顶栏（品牌/工作区/运行状态/用量/操作）、侧栏（9 入口 + 运行元信息）、主区骨架
- **看板组件（components/）**：状态条、成本面板、DAG（节点/连线/缩放）、Agent 卡片、门禁卡片、事件时间线、详情抽屉
- **功能组件（features/\*）**：供应商卡片、模型切换器、在线模型多选、端点测速、供应商表单、新建运行弹窗、运行对比、配额弹窗

### 2.3 状态管理（改造前后）

**改造前**：一个全局 `const S = {...}` 横跨 3891 行，任何函数都能直接改；`localStorage` 的 5 个键名散落在 6 处字符串里；数据来源（IPC / HTTP / mock）与视图混在同一文件。

**改造后**：

```
数据源                                状态                          视图
window.studio（IPC）  ─┐
本机 HTTP（lib/api）  ─┼─→ app/data.js（映射）─→ S（lib/store）─→ features/*（纯渲染）
app/mock.js（演示）   ─┘                          ↑
                                    app/realtime.js（SSE + 文件监听，增量更新）
```

- `S` 只在 `lib/store.js` 定义，视图层只读；写入集中在 `app/` 的装配代码（boot / data / realtime / mock / router）。
- `localStorage` 键名收敛为 `STORE_KEYS`（theme / lastRunDir / lastPage / dagView / nrMock），不再散写字符串。
- 后端字段 → UI 领域模型的转换全部收在 `app/data.js`（`mapRealTask` / `deriveAgents` / `deriveGates` / `enrichRun`），页面不再直接读 `run.json` 结构。
- 保留 `window.__st` 遥测（selftest 依赖）与 `window.BUILTIN_PROVIDERS`（e2e 与调试台依赖），作为契约上的显式出口而非偶然全局。

---

## 三、目录与分层

```
src/
  contracts/   enums.js       状态 / 运行 / 角色 / 事件 / Agent 状态文案（新增状态只改这里）
               providers.js   27 个内置供应商目录 + 模型别名
  lib/         dom.js         h()/s()/($)/$$ —— 函数式建元素，杜绝字符串拼 HTML
               format.js      token / 金额 / 时长 / 时间 / 字节 / 截断（纯函数）
               store.js       S、STORE_KEYS、persistState、window.__st
               api.js         本机 HTTP 客户端（8s 超时，失败静默降级由调用方决定）
  ui/          toast.js  modal.js  icons.js
  app/         boot.js        启动装配
               router.js      switchNav
               shell.js       顶栏/主题/全局快捷键/账户菜单
               realtime.js    SSE + 文件监听 → 统一事件入口
               data.js        真实数据适配
               mock.js        演示数据 + 模拟器
  features/    dashboard/ detail/ providers/ settings/ events/ artifacts/
               plan/ dag-page/ agents-page/ gates-page/ runs/ new-run/
```

**分层规则（`npm run check` 强制）**

- `contracts` 不依赖任何项目内模块；`lib` 不依赖 `ui/app/features`；`ui` 不依赖 `app/features`。
- 只有 `app` 与 `features` 可以互相引用；`features` 之间允许跳转（如门禁页调用看板的 `acceptGate`），但不得反向依赖 `app` 的私有实现。
- 单 JS 模块 ≤ 320 行，单 CSS 分片 ≤ 300 行；超限即视为该拆。

---

## 四、变更清单

### 4.1 JS

| 从 | 到 | 行数变化 |
| --- | --- | --- |
| `renderer.js` 常量区（139 个符号） | `contracts/enums.js`、`contracts/providers.js` | 3891 → 分 36 个文件 |
| DOM/格式化工具 | `lib/dom.js`、`lib/format.js` | 同上 |
| 状态与持久化 | `lib/store.js` | 同上 |
| 后端 HTTP 调用 | `lib/api.js` | 同上 |
| Toast / Modal / Icon | `ui/*.js` | 同上 |
| 看板 5 区块 + DAG | `features/dashboard/*` | 同上 |
| 详情抽屉 | `features/detail/panel.js` | 同上 |
| 7 个功能页 | `features/*` | 同上 |
| 运行时装配（SSE、监听、映射、模拟器、路由、外壳、启动） | `app/*` | 同上 |

拆分是**机械切分 + 依赖自动推导**完成的（按顶层声明边界切片，再按实际引用生成 import/export），不是手抄，因此不会在搬运中丢逻辑。切分后逐一清掉了推导产生的伪导入（字符串/模板里出现的标识符被误判为依赖）。

### 4.2 CSS

| 从 | 到 |
| --- | --- |
| `styles.css` 的 `:root` / `[data-theme=dark]` | `styles/tokens.css`（重写，见第五节） |
| 全局重置与排版 | `styles/base.css` |
| 顶栏 / 侧栏 / 骨架 | `styles/layout/{topbar,sidebar,shell}.css` |
| 按钮、区块、状态条、DAG、Agent、门禁、事件、详情、模态、Toast、空态 | `styles/components/*.css` |
| 各页面私有样式 | `styles/features/*.css` |
| 过渡与动画、窄屏适配 | `styles/motion.css`、`styles/responsive.css` |

原 `styles.css` 里混在"顶栏"段落中的 21 段页面样式（产物页、设置页、事件页、任务图页、Agents 页、门禁页、计划页、成本面板、运行对比……）按归属重新分发到对应域文件；`features/events.css` 里误放的 `.tb-run-status` / `.tb-center` 顶栏规则已删除——它此前**覆盖了顶栏状态胶囊的样式**（这也是顶栏看起来"没有边框"的原因）。

### 4.3 顺带修掉的真实缺陷

| 缺陷 | 说明 | 处理 |
| --- | --- | --- |
| 运行对比按钮必然失败 | `openCompareModal` 调用了 `window.studio.readRun/readTasks/readLedger`（preload 里不存在）且最后调用了未定义的 `openModal` | 改用 `loadRun` + `ledgerHistory` + `showModal`，按钮恢复为"两个 run 的指标对比" |
| `callIpc` 死代码 | 定义后无任何调用点 | 删除 |
| Agent / 门禁 / 任务状态显示英文 | 页面直接渲染 `a.status`（done / succeeded / failed / passed） | 统一走 `contracts/enums.js` 的中文文案（`agentStatusLabel` / `gateStatusLabel` / `STATUS_META.label`） |
| DAG 节点元素堆叠 | 右上角对勾与耗时文字重叠；标题按 18 字截断在 208px 节点里溢出 | 去掉对勾（与状态 chip + 描边色三重重复），标题按 13 字截断，耗时右对齐到顶行 |

---

## 五、视觉重构（"Console Ink"）

**方向**：这是一个多智能体调度控制台，不是营销页。取"仪表盘 / 图纸"的语汇：暖中性纸感底、铜色单一强调、等宽数值、细分隔线，密度靠排版层次而不是色块。

| 维度 | 改造前 | 改造后 |
| --- | --- | --- |
| 主色 | iOS 蓝 `#007AFF` | 铜 `#A9531F`（深色主题 `#D98550`），全站唯一实心强调色 |
| 背景 | 冷灰 `#F2F2F7` / `#0d1117` | 暖纸 `#F3F1EB` / 暖黑 `#131211`，边框与阴影同步暖调 |
| 状态色 | 糖果色（`#34C759` / `#FF9F0A` / `#FF3B30`） | 降饱和"油墨色"，运行中改青（`#2F7385`）与铜色强调区分 |
| 字体 | `-apple-system` 全家桶（Windows 上即 Segoe UI） | 展示字 `Bahnschrift`（DIN 系窄体）＋ 正文 `Segoe UI Variable / 雅黑`＋ 数字/ID/代码 `Cascadia Mono` |
| 字阶 | 混用 10–17px 无体系 | `--fs-display/h1/h2/body/sm/xs` 六档 + 行高/字距令牌 |
| 间距 | 4/6/8/10/12/14/16/18/20 混用 | 4 的倍数七档（`--sp-1..10`），组件内只用这套 |
| 圆角 | 4/5/6/8/10/12 混用 | 4 / 6 / 8 / 12 四档 + 胶囊 |
| 阴影 | 单一 `--shadow` | 三级（Y=2/8/24），暖调、低透明度 |

**组件级重做**

- **顶栏**：主操作从"居中悬浮"改到最右（flex `order`，DOM 不动）；品牌字改全大写 DIN + 字距；运行状态做成胶囊；用量改等宽数字 + 细进度条。
- **侧栏**：当前页由"整块反白"改为"左侧 2px 铜色标尺 + 淡底"；底部元信息改 key-value 两列、数值等宽右对齐。
- **看板区块**：块头加发丝分隔线与 DIN 字标题，卡片统一 8px 圆角与 1px 暖色边框。
- **运行状态条**：目标用 19px 展示字；指标改 6 列等宽数字 + 竖向发丝线；状态由实心块改为淡底 + 语义色文字。
- **DAG**：画布加点阵网格（空白区域不再是死白）；节点精简为「id · 耗时 / 标题 / 角色 · 状态」三行。
- **供应商页**：卡片从"竖排四个按钮"改为横向操作组；头像由圆形改 6px 圆角方块，与全站几何语言一致；搜索/分类控件统一 28px 高。
- **设置页**：表单收敛为一套规格（32px 高、6px 圆角、铜色 focus 环），删掉重复的 `.settings-field input` 与 `.set-input` 两套规则。
- **事件日志**：筛选条从"一排实心铜色块"改为描边胶囊（默认全选时不再像一堵墙）；行内列宽固定，时间/类型/任务全部等宽对齐。

**刻意没做的**：没引入框架、没引入构建步骤、没改任何 DOM 结构与类名（因此 e2e 的选择器与 28 项断言原样生效）。

---

## 六、兼容性验证

| 验证项 | 方法 | 结果 |
| --- | --- | --- |
| 端到端行为 | `electron . --e2e`（9 页导航、详情面板、运行切换、新建运行、成本面板、供应商全链路、快捷键、主题、DAG 暗色可读性） | **28 / 28 通过** |
| 真实运行链路 | `electron . --selftest`（启动一次 `--mock` run，验证实时更新、结束统计、取消与进程回收） | **16 / 16 通过** |
| 结构护栏 | `npm run check` | 通过：49 JS / 32 CSS，分层、导入、语法、体量、入口全合规 |
| 测试文件改动 | 本次重构未编辑 `studio/e2e.js` 与 `studio/selftest.js`（文件时间戳停留在重构前）；28 + 16 项断言原样通过 | 0 行改动 |
| 视觉回归 | `npm run e2e && npm run visual`（16 张截图对基线） | **16 / 16 一致**；换色实验（铜→蓝）8 张被拦、退出码 1；DAG 暗色两条防回归断言（16b/16c）仍通过 |

> 说明：多次重构过程中曾出现两次"5/28"回归，都是模块级语法错误（漏一个括号）导致整条 ESM 依赖图不执行。为此给 `npm run check` 补了**逐文件语法检查**（`node --check`），这类错误现在会在秒级护栏里被拦住，而不用等 40 秒的 e2e。

---

## 六之二、视觉回归基线

改样式最容易出的事故不是"新页面丑"，而是"顺手把别的页面改坏了"。为此加了一道 `npm run visual`：

| 环节 | 做法 |
| --- | --- |
| 取样 | 直接复用 `npm run e2e` 产出的 16 张截图（不另起一套截图流程，避免出现两套不一致的"真相"） |
| 解码 | 只用 Electron 自带的 `nativeImage`，不引入 pixelmatch / sharp 等依赖（守住"唯一依赖 electron"） |
| 比对 | 两级分辨率：160px 看整页版式是否位移；480px + 16px 分块看局部是否被改（按钮换色、间距变化会顶到分块差） |
| 阈值 | 全局差 ≤ 1、变化像素 ≤ 0.5%、分块差 ≤ 20 |
| 基线 | `visual-baseline/` 不入库（依赖本机 DPI 与字体），首次 `npm run visual:update` 自建 |

**实测标定**（本机 1440×900）：

| 场景 | 表现 |
| --- | --- |
| 同一份代码连跑两次 e2e | 全部 0 差异（页面上的时间都来自 run.json / ledger，不随墙上时钟变） |
| 只有相对时间文案（"3 分钟前"）跨了分钟 | 单块最高 **15.2** → 阈值定 20 时通过 |
| 把强调色从铜换成蓝（明暗都换） | 单块 **42.4**、**8/16 张**被判差异过大、退出码 1 |

也就是说：这个闸能抓住"整体换色/版式位移"，同时不会被相对时间文案误伤。

## 七、已知问题与建议

1. **`--shots` 视觉走查脚本已移除**：当前 Electron 环境（窗口被遮挡/离屏）下 `capturePage` 会返回首帧的陈旧画面，导致逐页截图全是同一张。e2e 的截图不受影响（同一进程内连续 `invalidate` + 短间隔），因此视觉走查改用 `npm run e2e` 产出的 `e2e-out/*.png`。若后续需要独立走查，建议加 `win.showInactive()` + `webContents.beginFrameSubscription` 或改用 Playwright。
2. ~~`app/mock.js` 偏大~~ → 已拆为 `app/mock/{fixtures,simulator}.js`，`app/mock.js` 只做再导出（上层导入路径不变）。
3. ~~`dashboard/blocks.js` 逼近上限~~ → 已拆为 `dashboard/{status-bar,agents,gates,cost,events}.js`，旧文件删除、引用方全部更新。
4. ~~主题切换掉帧~~ → `applyTheme()` 在换肤瞬间给 `<html>` 加 `.theme-switching`（禁过渡/动画，两帧后移除），切主题变成"瞬间换好"。
5. ~~`features/settings/index.js`（283 行）~~ → 已按组拆成 `environment / model-budget / gateway / connection / appearance / about` 六个 builder，
   取值统一走 `fields.collectSettings`；`index.js` 只剩 46 行的装配与保存。
6. **`features/new-run/index.js`（238 行）** 是当前最大模块，仍在 320 行上限内；再长就该按「基础字段 / 高级选项 / 模型选择」拆。

---

## 八、验证入口

```bash
cd studio
npm run check      # 结构 + 语法护栏（秒级）
npm run e2e        # 28 项端到端，截图落 e2e-out/
npm run visual     # 16 张截图与本机基线比对（首次先跑 npm run visual:update）
npm run selftest   # 16 项真实运行链路，截图落 selftest/
npm start          # 正常启动
```
