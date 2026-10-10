# V4-FE 前端完善升级 — 只读审查问题清单

审查时间：2026-10-10
审查范围：F0-F3（styles.css, renderer.js F2/F3, main.js F1, index.html F0）
审查模式：只读（未修改任何代码；工作区 renderer.js/styles.css 在审查期间已被另一 agent 提交为 99d5acd F4-F6，本报告基于提交时点的 HEAD=99d5acd 与 F0-F3 相关提交 9c774bb / b818b19 / 4bc70a8 / 6fbdcfa）

> 说明：审查期间发现工作区 renderer.js/styles.css 的未提交变更已被另一 agent 提交为
> `99d5acd feat(studio): F4+F5+F6`。本报告的 F2/F3 判定以该提交所落地的代码为准，
> 并区分了「真正属于 F0-F3」与「F1 token 化遗留的深色可读性缺口」。

## 问题汇总
| 编号 | 严重程度 | 文件 | 行号 | 问题描述 | 建议修复方向 |
|------|----------|------|------|----------|-------------|
| FE-001 | 高 | studio/renderer.js | 441 | DAG 节点主标题文字在深色模式下不可见（近黑字压近黑底） | SVG 文字 fill 改走主题感知色（CSS 变量或按 data-theme 切换） |
| FE-002 | 中 | studio/renderer.js | 13-23, 44-51, 57-76, 2445-2449 | 状态/角色/事件/运行状态色全部硬编码浅色 hex，不随主题切换；styles.css 里定义的全套 --status-* token 因此成为死变量 | 色值改为 var(--…) 或按 data-theme 双套取值，让 --status-* token 真正被引用 |
| FE-003 | 中 | studio/renderer.js | 1587, 1959, 2028 | 内联 input 边框硬编码 `rgba(60,60,67,.18)`，深色模式下边框几乎不可见 | 改用 var(--border-input) |
| FE-004 | 低 | studio/renderer.js / main.js | boot 段; main.js:46,753 | 启动先闪浅色再切深色（默认 theme='dark' 但 :root 为浅色，applyTheme 在异步 getSettings 后才执行） | 尽早在 HTML 内联按 settings 预置 data-theme，或首帧前同步应用 |
| FE-005 | 低 | studio/main.js | 71 | saveSettings 每次都 pushThemeChanged()，非主题设置（recentGoals、保存设置页、设默认模型）也多发一次 theme:changed | 仅当 next 含 theme 时才 push，或由渲染端忽略重复 |
| FE-006 | 低 | studio/index.html | 96 | 产物导航图标 SVG 路径重复且笔画超出 viewBox | 清理重复 path，修正火箭图标坐标 |
| FE-007 | 低 | studio/renderer.js / styles.css | renderer.js:81-82；styles.css | 死代码：C 对象多数字段未用；--indigo/--ink-25/--warn-bg-chip 无引用 | 删除未用常量/变量 |
| FE-008 | 低 | studio/renderer.js | cycleTheme | cycleTheme 乐观改 S.settings.theme 后 fire-and-forget 持久化，失败时内存态与 settings.json 不一致 | setSettings 失败时回滚 S.settings.theme 并重应用 |

## 详细说明

### FE-001: DAG 节点主标题在深色模式下不可见
- 文件: studio/renderer.js
- 行号: 441（节点标题 fill），关联 428-431（节点框填充）
- 严重程度: 高
- 问题描述:
  DAG 节点框填充来自 `STATUS_META[status].fill`，对运行中/就绪/门禁评审/返工/阻塞/已通过/失败
  等状态是半透明淡色（如 running: `rgba(0,122,255,0.10)`），叠在 `.dag-canvas-wrap` 的
  `var(--bg-canvas)` 上；而深色模式 `--bg-canvas = #0d1117`，节点框实际呈近深色。
  节点主标题却内联写死 `fill:'#1C1C1E'`（近黑）。近黑文字（#1C1C1E）落在近深底色（≈#0c1c2e）
  上，对比度约 1:1，**标题完全不可读**。而 `main.js:46` 默认 theme='dark'，即新用户首屏默认
  就是深色，任务图（核心视图）的节点标题肉眼看不见。这直接违背 F1 验收「暗色下看板/DAG…
  无可读性破绽（对比度抽查状态徽章与次要文字）」。e2e 第16项只断言 body 背景色变化，未抽查
  DAG 文字对比度，故漏网。
  - 附带：436/438 行 task_id 与耗时 `fill:'#8E8E93'`、449 行角色名 `fill:'#6E6E73'` 在深色
    节点框上对比度也偏低（次要文字偏暗）。
  - pending/unknown 节点 fill 为 `#FFFFFF`、skipped/cancelled 为 `#F2F2F7`，在深色界面里
    反而成了突兀的亮白卡片，与其他深色节点不一致。
- 建议修复方向:
  SVG 文字/节点框不写死 hex：(a) 标题 fill 用 `var(--text)`、task_id/角色名用
  `var(--text-ter)`/`var(--text-sub)`（SVG fill 支持 CSS 变量）；(b) 节点 fill/stroke 改由
  CSS 类 `.node-box` 按 `[data-theme=dark]` 覆盖；(c) 给 e2e 暗色截图加一条 DAG 文字非空/
  对比度断言。

### FE-002: 状态/角色/事件色硬编码浅色，未随主题切换
- 文件: studio/renderer.js
- 行号: 13-23（STATUS_META）、44-51（ROLE_META）、57-76（EVENT_META）、2445-2449（RUN_STATUS_COLOR）
- 严重程度: 中
- 问题描述:
  这些调色板全部写死浅色 hex（#007AFF/#34C759/#FF3B30/#5AC8FA/#AF52DE…），并通过
  内联 style / SVG 属性直接落到 DOM，不受 `[data-theme]` 切换影响。后果：
  - 运行下拉状态圆点（2485-2489）、事件时间线色条（759 行 `--ev-color`）、成本条按 Agent
    分布（738-739）、Agent 头像圆（446）在深色模式下仍用浅色值。
  - F1 在 styles.css 里精心定义了深色版状态色（--status-running:#0A84FF、
    --status-succeeded:#30D158、--purple:#BF5AF2 等），但 renderer.js 从不引用它们——
    导致 styles.css 里整套 `--status-*`（10 组文字+填充）与 --purple/--pink/--sky 等
    实际是**死变量**（经脚本核对：CSS 与 renderer.js 中均无 var(--status-*) 引用）。
  - 多数点/条仍可辨认（饱和色在深底上并非完全不可读），但与深色 token 的调亮版不一致，
    属体验缺陷而非功能瘫痪，故定中。
- 建议修复方向:
  让 STATUS_META/ROLE_META/EVENT_META 的 color 引用 `var(--status-*)`/`var(--purple)` 等
  token，或在 JS 内按 `document.documentElement.dataset.theme` 取双套色值；使已定义的
  `--status-*` token 真正被消费，消除死变量。

### FE-003: 设置页/弹窗内联 input 边框深色下消失
- 文件: studio/renderer.js
- 行号: 1587、1959、2028
- 问题描述: 三处 `<select>/<input>` 内联 `border:1px solid rgba(60,60,67,.18)`（中性深灰）。
  深色模式背景为 #161b22 级，0.18 不透明度的深灰描边几乎不可见，控件像无边框浮在面板上。
- 严重程度: 中
- 建议修复方向: 内联边框改为 `var(--border-input)`，与 .ap-field（styles.css:973）保持一致。

### FE-004: 启动首屏浅色闪烁
- 文件: studio/renderer.js（boot 中 applyTheme 时机）、studio/main.js:46/753
- 问题描述: HTML 加载时 `<html>` 尚无 data-theme，CSS 回退 `:root`（浅色）。而 applyTheme()
  在 `await window.studio.getSettings()` 之后才执行。默认 theme='dark'，故每次启动先渲染
  一帧浅色 body（#F2F2F7）再跳深色；虽 BrowserWindow backgroundColor 预填 #0d1117 减轻了
  白屏感，但 body 区仍可见一次闪白。
- 严重程度: 低
- 建议修复方向: index.html `<head>` 内加一段同步内联脚本，读 localStorage 缓存的主题或
  默认 dark，立即设置 `document.documentElement.dataset.theme`，避免首帧闪色。

### FE-005: saveSettings 每次都推送 theme:changed
- 文件: studio/main.js:66-72, 85-89
- 问题描述: 任何 setSettings（含 run:start 记录 recentGoals:587、设置页保存、供应商页「设为默认
  模型」）都会触发 `pushThemeChanged()`，向渲染进程多发一次 theme:changed → applyTheme。
  applyTheme 本身很轻，无功能问题，仅冗余 IPC 与一次 DOM 写入。
- 严重程度: 低
- 建议修复方向: 仅当 `next` 含 theme 键时才 pushThemeChanged；或在渲染端做相同 mode 去重。

### FE-006: 产物导航图标 SVG 路径冗余且越界
- 文件: studio/index.html:96
- 问题描述: 产物(artifacts)按钮的 SVG 第二条 path 重复了 `M3 8l9 5 9-5M12 13v8`；且首条
  `M21 8l9-5…` 相对位移后 x 到达 30，超出 viewBox `0 0 24 24`，火箭右上笔画被裁剪。
  不影响功能，仅图标显示不完整。
- 严重程度: 低
- 建议修复方向: 合并重复 path，把越界坐标收进 viewBox。

### FE-007: 死代码 / 未使用 token
- 文件: studio/renderer.js:81-82（C 对象）；studio/styles.css（--indigo、--ink-25、--warn-bg-chip 及全套 --status-*）
- 问题描述:
  - `const C={blue,success,warn,error,text,sub,ter}` 仅 `C.ter` 在 515 行被用一次，其余字段
    （尤其 text:'#1C1C1E'）是死值。
  - styles.css 中 `--indigo`、`--ink-25`、`--warn-bg-chip` 经核对无任何 var() 引用。
  - 全套 `--status-*` 死变量见 FE-002（本可通过 JS 引用激活）。
- 严重程度: 低
- 建议修复方向: 清理未用 C 字段；FE-002 修复后 --status-* 自然激活；其余未用 token 删除或补引用。

### FE-008: cycleTheme 持久化失败时状态不一致
- 文件: studio/renderer.js（cycleTheme）
- 问题描述: 点击主题按钮先 `S.settings.theme=next` 并立即 applyTheme()，再
  `window.studio.setSettings({theme:next})`（外层 try/catch 吞错）。若持久化失败，UI 已切到
  next 但 settings.json 仍是旧值，重启后回退，造成「看着切了、重启复原」的困惑。setSettings
  很少失败，故定低。
- 严重程度: 低
- 建议修复方向: setSettings 失败时 toast 提示并把 S.settings.theme 回滚到旧值重应用。

## 正面确认（做得好的地方）
- **styles.css token 化非常彻底**：按验收标准 `grep -nE '#[0-9A-Fa-f]{3,8}|rgba\(' styles.css`
  的全部 149 处色值命中都落在顶部 token 定义区（11-227 行），规则区零硬编码色值；light/dark
  两套变量 95/88 个，差异仅为 7 个「两主题一致的固定深色组件」（日志/Toast），属设计意图。
- **主题机制闭环正确**：main.js 的 `resolveEffectiveTheme` 正确区分 light/dark/system，
  `nativeTheme.on('updated')` 仅在 system 模式推送；渲染端 applyTheme 同时接主进程推送与
  matchMedia 兜底；persistence 走 settings.json，`settings:get` 回传 effectiveTheme。
  e2e 第16项覆盖了按钮存在、dataset.theme 与 body 背景色变化，并补暗色截图。
- **F0 结构修复完整**：DAG 导航按钮已补齐 SVG 图标 + `<span>任务图</span>` + 闭合标签；
  fmtBytes/ROLE_ORDER 各仅一份，无重复定义；PLACEHOLDERS 占位文案彻底删除；switchNav 9 个
  页面全部指向真实渲染函数，兜底分支仅对未知 key 生效。
- **F2 模型链路正确**：BUILTIN_PROVIDERS 对齐 9 个内置 provider；新建弹窗 provider→model
  下拉 + 自定义手输 + `provider:model` 别名组装正确；mock 勾选会置灰模型区；main.js run:start
  按「弹窗选择 > 设置 defaultModel」优先级传 `--model`，且 mock 时不传；--max-cost /
  --max-llm-calls 回落逻辑正确。
- **F3 供应商页与主题兼容良好**：prov-* 全套样式走 CSS 变量（prov-name/text-sub/success/error
  等），深浅模式均不违和；数据源三级降级（api→常量+keyStatus→demo）、测试按钮 1.5s 防抖、
  主进程 net.fetch 绕 CORS、key 只回布尔不泄露值，均符合红线。
- **无 prefers-color-scheme 媒体查询与 data-theme 冲突**；CSP connect-src 仅放行本地回环，
  未放宽；密钥仅显示环境变量名与已配置布尔，未见把 env 值写入 DOM/settings 的路径。

## 结论
F0 结构修复、F1 token 体系与切换机制、F2 模型链路、F3 供应商页主干均已正确落地；
最主要缺口是 **F1 的深色可读性只完成了 CSS 层，renderer.js 内联 SVG/调色板色值未主题化**，
其中 DAG 节点标题在默认深色主题下不可读（FE-001，高）需优先修复，其余为中等一致性与低风险项。
