# AgentCrew Studio iOS / HIG 视觉与交互重设计说明（R67）

> 日期：2026-11-19 · 范围：`studio/`（Electron 原生 ESM + CSS 分片，零框架）
> 依据：用户提供的《Apple HIG 前端工程规格》（会话附件）+ Apple Human Interface Guidelines
> 约束：**功能与接口调用零变更**；Python 后端（含 `api/dashboard.py` 控制台）一行未改。

---

## 1. 目标与边界

对 Studio 全部 13 个页面做 iOS 化重设计：页面结构、信息层级、组件布局、交互反馈、
响应式适配，以及配色 / 字体 / 间距 / 圆角 / 阴影 / 动效的基础规范统一。

**不变项（合同）**

- 81 个 JS 模块的公共接口、IPC 调用、事件流、DOM key 复用与每帧重绘机制全部保留；
  流式消息 / 事件行 / DAG 不加逐帧动画（性能红线）。
- `ui/modal.js` 焦点圈定、Esc 关闭、遮罩点击行为不变。
- e2e 文案断言依赖的文本符号（`✓` `✗` `✕` `⚡` `` 等）保留为文本，不转图标。
- 令牌表外不写死颜色；两主题令牌键集合严格一致。
- Python 侧 `tests/test_dashboard_ui.py` / `test_dashboard_ops.py` 守卫的后端控制台
  HTML 不在本轮范围（规格明确「不改后端代码」），定向 16 passed。

**与规格文档的偏差（已裁决并披露）**

1. 规格中的 `.shell/.sidebar/.toolbar/.badge` 类名与 `bg/surface/danger` 令牌键描述的是
   Python 后端控制台；Studio 实际类名不同，iOS 规范落在 Studio 真实类与令牌上。
2. 语义色文字对比度：iOS 系统色（`#34C759/#FF9500/#FF3B30`）直接做小字不满足 ≥4.5:1，
   故系统色用于底 / 点 / 进度条，文字用同色系深阶变体（`--success-text` 等）。
3. `xs=12px` 保留给密集 mono 元信息（时间戳 / 字节数 / ID），组标题显式 `fs-sm=13px`。
4. 多选语义保留复选框（供应商勾选列表、定时任务星期 chips）；**布尔开关**统一 iOS Switch。

## 2. 基础规范（切片 A，`a1481a9`）

| 维度 | 规范 |
|---|---|
| 浅色底 | 页面 `#F2F2F7`（systemGroupedBackground）、卡片 `#FFFFFF`、分隔 `rgba(60,60,67,0.12)` |
| 深色底 | 页面 `#000000`、卡片 `#1C1C1E`、分隔 `rgba(84,84,88,0.65)`、主文字 `#FFFFFF` |
| 强调 | 系统蓝 `#007AFF`（dark `#0A84FF`）；成功 / 警告 / 危险用 iOS 系统色 + 深阶文字变体 |
| 辅助灰 | light sub .78 / ter .72，dark sub .65 / ter .55（对比度达标） |
| 字阶 | display 28 / h1 22 / h2 17 / body 15 / sm 13 / xs 12；body 行高 1.45；标题 700 |
| 字体 | `-apple-system, BlinkMacSystemFont` 优先，Segoe UI Variable / 微软雅黑兜底，不打包字体；mono `ui-monospace, "SF Mono", "Cascadia Code", Consolas` |
| 圆角 | 连续圆角体系：卡片 / 模态 16（`--r-md`）、控件 10–12、胶囊 full |
| 阴影 | 三级：shadow-1 发丝级、shadow-2 浮层、shadow-3 模态；浅色减轻避免脏灰 |
| 动效 | iOS 缓动族（`--ease-out` / `--ease-spring`）；页面淡入 ≤200ms；模态弹簧滑入；按压缩放反馈 |
| 新增键 | `--on-role`（角色色块上文字：light `#FFFFFF` / dark `#1C1C1E`）、`--seg-thumb`、`color-scheme`、`--mask-strong` 双主题同步 |
| 窗口 | `main.js` 启动底色 `#070B16` → `#F2F2F7`（唯一 JS 改动，Electron 窗口配置非 CSS） |

## 3. 切片明细

| 切片 | 提交 | 关键改动 |
|---|---|---|
| A 令牌层 | `a1481a9` | 上表全部；含第一轮整体 iOS 化基础（毛玻璃顶栏 / 命令框 / toast / 下拉、分段控件、响应式断点 + `pointer:coarse` 触控放大） |
| B 通用组件 | `27c8fb1` | 页头大标题 28/700 + tint 操作按钮；看板分组间距 24 / 边距 16；侧栏毛玻璃 `blur(20px) saturate(180%)`；设置布尔项 iOS Switch（51×31、绿轨白滑块、弹簧过渡）；模态 Sheet grabber + 16 圆角 + 加粗取消；加载菊花改三态灰；`rs-goal` 升 h1 |
| C 对话页 | `20dda95` | iMessage 化：用户蓝实底右对齐气泡（18/18/6/18）、Agent 白气泡（6/18/18/18）+ 发丝边 + shadow-1；角色色头像 + 13px 灰角色名（去内联 color）；披露行灰底无边框、chevron 右置、按压 opacity；「回到最新」底部居中毛玻璃胶囊；Markdown 代码块灰底发丝边、标题 600；**文件类 emoji 全部换线性 SVG**（`lib/artifacts.js` 新增共享 `artifactIcon(kind,size)`，24 viewBox / 1.6 圆头 / currentColor，产物页 / 结果区 / 对话 / 详情抽屉共用） |
| D DAG 与余页 | `b25fd31` | 画布分组灰底 + 16 圆角内嵌；Agent 节点角色色描边 1.5px（纯 CSS 消费节点内联 `--role-color`）+ running/failed/blocked 状态色覆盖；头像角色色 + `--on-role` 文字；进度条 4px 全圆角（CSS 几何覆盖，JS 不动）；节点标题 600；1100px 断点看板边距收敛 16 |
| D 补 | `c7f2151` | Switch 抽为 `components/switch.css`（45 分片），覆盖新建运行 / 定时任务模态布尔项；**Agents 页定高网格裁切修复**：`height:calc(100vh-100px)+overflow:hidden` 使 auto 行被压到 min/max-content 之间裁掉统计第二行 → 改自然高度、主区统一滚动 |

其余页（事件 / 门禁 / 回滚 / 设置 / 供应商 / 档案 / 定时）经令牌层自动继承，逐页截图核对。

## 4. 验证结果（全部本机实跑）

| 护栏 | 结果 |
|---|---|
| `node tools/check-architecture.js` | 通过：81 JS 模块 / 45 CSS 分片，分层 / 导入 / 体量 / 入口合规 |
| `node --test tests/*.test.mjs` | 38/38 |
| `npm run e2e` | 32/32，20 张截图（13 页浅色 + 暗色代表面 + 模态 / 下拉 / 详情 / 结果区） |
| `npm run selftest` | 16/16（真实 LLM 运行） |
| `npm run visual` | 基线重建后复检 20/20 一致（全局差 ≤1、变化像素 ≤0.5%、分块差 ≤20） |
| Python 守卫定向 | `test_dashboard_ui.py` + `test_dashboard_ops.py` 16 passed |
| Python 全量 | `scripts/test.py --batch 2 --retries 4 --retry-failed 3 --keep-pycache`：**3576 passed / 0 failed / 6 skipped / 0 error**（193 文件 / 97 批次 / 513.5s，与 R66 基线同数） |

**视觉基线流程**：A–D 每片护栏绿后截图 → 20 张逐张人工核对（明暗）→ `visual:update`
重建 → `visual` 复检一致。基线目录 `studio/visual-baseline/` 为 gitignore 本地产物。

## 5. 披露与未验证项

- 暗色逐页截图覆盖：计划审查、供应商、结果区概览 / 产物四个代表面 + 主题切换断言
  （body 底色 light `rgb(242,242,247)` ↔ dark `rgb(0,0,0)`、DAG 节点标题 / 框填充断言）。
  其余页暗色由令牌层结构保证——非令牌分片 grep 仅注释含 hex，无写死颜色。
- 窄屏（≤1100/768）与 `pointer:coarse` 触控规则为 CSS 断点实现，未实机 / 真触控验证。
- `reduced-motion` 沿用项目既有路径，本轮动效均挂既有缓动令牌，未新增独立开关。
- 过程中两个已修复陷阱：emoji→`h()` 替换括号配平（架构检查拦截）；
  `ELECTRON_RUN_AS_NODE` 空串仍触发 Node 模式崩溃（须整变量移除，勿设空串）。

## 6. 回滚

提交链 `a1481a9 → 27c8fb1 → 20dda95 → b25fd31 → c7f2151 → e80cefc`，每片独立可 revert；
功能 / 接口 / 测试合同未动，回滚任一视觉片不影响行为护栏。

## 7. 设计 bug 排查补片（`e80cefc`，2026-11-20）

重设计交付后一轮专项排查，发现并修复 6 项缺陷（纯 CSS，JS/接口零变更）：

| # | 缺陷 | 根因 | 修复 |
|---|---|---|---|
| 1 | 对话/计划/任务图/门禁四页状态徽章无语义色（纯黑文本），与看板/Agents 彩色芯片断档 | `.status-chip/.dag-task-status/.gate-full-status` 只有基础样式，`status-*` 修饰类从未接线；令牌 `--status-*` 双主题齐备却被闲置 | `blocks.css` 共享徽章基础 + 10 个状态语义规则（浅色用无障碍文字阶 `#248A3D/#D70015/#C93400`） |
| 2 | 设置页 Switch 行「标题左 Switch 右」不生效 | **特异性覆盖**：`.settings-field label`(0,1,1) 的 `display:block` 压过 `.set-check`(0,1,0) 的 flex，order/auto-margin 失效；经 CDP 读 computed style 实证 | 泛化规则改 `.settings-field label:not(.set-check)` |
| 3 | 门禁/任务图/事件全屏页定高 + 嵌套滚动条，与主区滚动不一致 | 旧布局 `height:calc(100vh-…)` 遗留 | 自然高度、主区统一滚动（同 Agents 页修复） |
| 4 | 门禁卡片圆角 10、名称字重 500，与 16/600 规范断档 | 旧值遗留 | `--r-md` + 600；设置组标题同步 600 |
| 5 | 星期 chips 内嵌原生方框、供应商复选框原生观感 | 多选控件未 iOS 化 | 星期改可选胶囊（隐藏原生框、选中 tint+加粗、`:has(input:focus-visible)` 焦点环）；供应商 `accent-color` 18px |
| 6 | 蓝气泡时间戳白字 0.72 透明度，对比度仅 ~2.8:1 | 降透明叠加系统蓝 | 去透明度改纯白（~4.0:1，Apple 系统蓝白字惯例，作为已披露例外） |

**验证**：check 81/45、unit 38/38、e2e 32/32、selftest 16/16；改动页截图逐张人工核对
（门禁红徽章、对话/计划绿徽章、模态与设置 HIG 行、事件主区滚动）后基线重建、复检 20/20。
排查方法沉淀：静态审计（类名-规则配对 grep）+ 截图核对 + **CDP computed-style 探针**
（`--remote-debugging-port` + Node WebSocket 读 `getComputedStyle` 与已加载规则），
后者是定位特异性覆盖的关键——文件里规则正确不代表运行时生效。

**未验证披露**：任务图页徽章与星期胶囊无 e2e 截图覆盖，共享规则与已核对页同源；
供应商勾选模态未截图，`accent-color` 走系统渲染。