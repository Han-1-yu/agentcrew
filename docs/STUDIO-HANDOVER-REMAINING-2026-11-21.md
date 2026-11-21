# Studio 功能补全 · 交接文档（剩余工作清单，2026-11-21）

> 状态：**交接快照**——接手人从本文读起，不需要重读方案全文
> 上游方案：`docs/STUDIO-FEATURE-COMPLETION-2026-11-21.md`（对标分析与逐模块清单）
> 已完成部分：见 §2（含提交链）；本文只写**剩余工作**的优先级 / 依赖 / 落点 / 验收
> 视觉语言：沿用 R67 iOS/HIG 令牌；硬约束见 §5（单 JS ≤320 行 / 单 CSS ≤300 行 / 零新 npm 依赖 / 零 innerHTML 拼接模型输出）

---

## 1. 一分钟现状

- 会话模型已定为 **conversation = run（1:1）**（方案 §3.1 决策 A）：会话列表/重命名/置顶/归档是 run 的前端元数据层（settings.json `runMeta`），运行中追问走 guidance、分支/重做走 rewind。**不要再提独立会话实体**。
- 批次 1（P0 对话闭环）**全部完成**；批次 2（P1）完成七项（flows 流程库页、通知中心、Ctrl+K、deriveAgents、导出 .md、Key 管理、**Q2 工具试跑页**）；批次 3（P2 + 新后端端点）**未动**。
- 当前基线：`check` **136 模块 / 55 分片**、`test:unit` **161/161**、`verify:api` **21/21**、`selftest` 16/16（**15 页**）、`e2e` **49/49**（15 页导航 + 工具试跑页头 + 任务详情产物摘要 + 产物操作条 + 快捷键录制 + 输出目录统计 + 事件范围筛选 + 定时任务表单/触发历史）、`visual` 24 张重建后一致。全部验证命令见 §4。
- 提交链：P0 `192eacb→…→35406cf`；P1 批次一 `129fbc8→86d9681→f611382→7ee52f6→d128fbd→74e9eea→1821796`；Q2 见 `PROJECT-STATE.md` §3.109 的提交记录。
- 原「约 60 个无关在途改动」已于 2026-11-21 按轮次入库（R68 / R69 后端与前端 / 供应商分层 / 对话页 / 文档共 4 笔），工作区干净；接手时若要改文档仍按「只 add 自己的文件」执行。

---

## 2. 已落地能力与关键落点（接手前必读的五个文件）

| 能力 | 关键文件 | 设计要点 |
| --- | --- | --- |
| 输入框三态分流 | `src/lib/composer-intent.js`、`src/features/composer/submit.js` | create/guidance/stop 判定与后端 `_GUIDANCE_RUNNABLE`（running/awaiting_approval）同口径；**REST 创建超时绝不自动降级 CLI**（防双发），仅 401/403/400/503/NO_API 降级 |
| 启动编排（REST/CLI 双通道） | `src/features/new-run/launch.js`、`rest-launch.js` | composer 与新建弹窗共用；ask/demo 恒走 CLI；`flowName` → REST `options.flow_name` / CLI `--flow` |
| runMeta 契约 | `src/lib/run-meta.js`、`src/features/runs/{sidebar,meta-store,item-menu}.js` | settings.json `runMeta:{[run_id]:{alias,pinned,archived}}`；乐观写盘失败回滚；deleteRun 后裁剪；归档默认隐藏、可搜不可见 |
| guidance 对账 | `src/features/conversation/assist.js`、`src/lib/messages.js` | 乐观插入 + 4s 轮询 GET 列表；`local-` 项 12s 宽限后丢弃；气泡 chip 由 ledger `guidance_injected/expired/withdrawn` 回写 |
| 通知中心事件源 | `src/features/notifications/index.js`、`src/app/realtime.js appendEvents` | 只收**实时增量**（loadRealRun 的历史回放不触发）；通知偏好（系统开关 / 静默时段 / 级别 / 类目静音）存 settings.json `notifyPrefs`，判定在 `lib/notify-prefs.js` |

其他：审批自动 resume（`gates-page/approvals.js`，409 → 重试按钮）；附件（`features/attachments/`，主进程 `images:pick/readFiles` 校验 ≤4 张 ≤2.6MB）；流程库（`features/flows-page/`）；命令面板（`features/command-palette/`，Ctrl/Cmd+K 在 `boot.js`）。

---

## 3. 剩余工作

### 3.1 批次 2 剩余（P1，前端为主）

按建议实施顺序排列；「依赖」列说明是否需要先动别处。

| # | 项 | 内容 | 依赖 / 落点 | 验收 |
| --- | --- | --- | --- | --- |
| 1 | ~~**Q2 工具试跑面板**~~ ✅ 2026-11-21 | `GET /tools` 清单 + `POST /tools/{name}/test` 沙箱试跑；口径 chips 解释「清单为什么缺 run_python / web_search」；结果区分 200+`ok=false` 与 409/400/404 | 独立页 `features/tools-page/{index,dialog}.js` + `lib/tool-lab.js`；pages 数组与视觉基线已同步（`PROJECT-STATE.md` §3.109） | 未连 API 给诚实空态；试跑失败原样展示后端错误 ✅ |
| 2 | ~~**J1-J3 产物增强**~~ ✅ 2026-11-21 | J1 版本对比：`history/rewind-NNN/` 快照 → 版本选择器 + 行级 diff（零依赖 LCS）；J2 Markdown 渲染切换（`lib/markdown.js` 已有）；J3 单文件另存为（main.js 增 `artifact:saveAs` IPC + 保存对话框） | `lib/artifact-versions.js` + `features/artifacts/versions.js` + `artifact:snapshots` / `artifact:saveAs` IPC（`PROJECT-STATE.md` §3.112） | 选择器与 diff 在真实回滚数据上未自动验证（本机无 `history/`，见 §3.112 边界）；另存只验到按钮与 IPC |
| 3 | ~~**N1-N2 定时任务编辑/启停/历史**~~ ✅ 2026-11-21 | 编辑=预填表单+删旧项（前端闭环）；启停走 CLI 新增的 `schedule enable/disable`（启用重算 `next_run`，一次性 at 已过明确拒绝）；触发历史=`run --trigger-source schedule:<id>` → ledger `run_triggered` → 扫最近 30 个 run 关联 | `agentcrew/cli/schedule.py`（`set_enabled`）+ `tests/test_schedule.py`；前端拆 `features/schedules/{form,card}.js` + `lib/schedule-draft.js`（`PROJECT-STATE.md` §3.115） | CLI 子命令有 `--json` ✅；前端启停走 IPC 调 CLI，**不直写 schedules.json** ✅ |
| 4 | ~~**O1 快捷键自定义**~~ ✅ 2026-11-21 | `boot.js` 硬编码 → settings.shortcuts 映射（palette / newRun / refresh） | `lib/shortcuts.js` + `features/settings/shortcuts.js`；settings 白名单已加 `shortcuts`（`PROJECT-STATE.md` §3.113） | 改键后立即生效、重启保持 ✅（e2e 13a/13a2 录制 Alt+P 后真能打开命令面板） |
| 5 | ~~**O2 通知偏好**~~ ✅ 2026-11-21 | 系统通知开关 + 静默时段 + 最低级别 + **按类目静音**（审批 / 预算 / 运行结果） | `lib/notify-prefs.js`；localStorage 旧开关已并入 settings.notifyPrefs | 静音的类目不再进面板与系统通知 ✅（历史条目保留） |
| 6 | ~~**O3 数据管理**~~ ✅ 2026-11-21 | 输出目录占用统计、归档 run 一键清理（走回收站）、设置导出/导入 | `data:usage` / `data:trashArchive` / `settings:export` / `settings:import` IPC + `lib/data-usage.js` | 统计实测 349 run / 519 MB ✅；清理只收已归档且非运行中，走 `shell.trashItem`（清理动作本身未自动化） |
| 7 | **D2 预算 90% toast** | `budget_warning` 事件已有 → toast + 通知中心条目（事件源钩子已就绪，`INTERESTING` 已含 budget_warning） | 无依赖，半小时工作量 | 跑 mock 预算场景可见一次（去重生效，不重复弹） |
| 8 | **D3 成本按模型聚合** | `dashboard/cost.js` 增按 model/provider 分组（llm_call 含 model，G1 已示范聚合写法） | 无依赖 | 与 run.json stats 的总成本守恒 |
| 9 | ~~**E3 计划 diff**~~ ✅ 2026-11-21 | `runs/compare.js` 指标对比 → 下方新增任务级表格（新增/移除/变更，比标题·角色·状态·优先级·依赖） | `lib/task-diff.js` 纯函数 + 八例单测；`passed`/`succeeded` 归一（`PROJECT-STATE.md` §3.110） | 对比弹窗可见任务差异行 ✅（自动化点击未做，见 §3.110 边界） |
| 10 | ~~**F1-F2 任务图**~~ ✅ 2026-11-21 | F1 running 节点实时高亮（SSE `task_transition` 已接；**不加逐帧动画**，只切 class）；F2 节点抽屉补输入/输出产物摘要 | `lib/task-live.js` + `features/dashboard/dag-live.js`、`lib/task-artifacts.js` 纯函数 + 十一例单测；事件去重键拆到 `app/realtime-dedup.js`（`PROJECT-STATE.md` §3.111） | 节点 class/chip 就地切换 ✅；抽屉显示输入/输出条目 ✅（真实 SSE 高亮无自动化断言，见 §3.111 边界） |
| 11 | **H1 profiles 模板扩充** | `features/profiles/templates.js` 3 个 → 写作/复核/代码执行/数据分析分类预设 | 纯本地数据 | 模板可导入为档案 |
| 12 | **H2 绑定矩阵直写（待核实）** | 若 `agents --save` 支持 bindings 则直写，否则保留 YAML 复制 | **先读** `agentcrew/profiles/service.py` 与 CLI `agents` 参数面（方案 §8.2 未核实项） | 核实结论写回本表与 PROJECT-STATE |
| 13 | ~~**L1-L2 事件页**~~ ✅ 2026-11-21 | L1 时间范围筛选（以最新事件为基准）+ 事件→「跳到任务 / 对话」；L2 follow toggle 已有 | `lib/event-view.js`（`PROJECT-STATE.md` §3.114） | 筛选组合与倒序 ✅；跳转入口存在 ✅（真实点击未做，见 §3.114 边界） |
| 14 | **P2-P3 壳层** | P2 侧栏折叠（对话列表区可收起）；P3 `shell.js openUserMenu` 的演示状态切换器移入「开发者」子菜单，与真实账户语义分离 | 无依赖 | 演示菜单不再与账户混放 |
| 15 | **§5 内联审批（P1）** | 对话页系统消息「等待人工审批」处内联审批卡：REST `GET/POST /runs/{id}/approvals` 直接 apiFetch（无需新 IPC）；未连 API 降级为跳门禁页 | 数据都在；复用 approvals.js 的 decide+auto-resume 逻辑（建议抽公共函数避免两处漂移） | 裁决后卡片显示 resume 结果；未连 API 可跳转 |
| 16 | **M1 自定义供应商** | 默认**保留 YAML 复制**（设计边界：Studio 不重写 agentcrew.yaml，方案 §8.3）；若要直存需新后端端点 → 归入批次 3 | 维持现状即可关闭此项 | — |

### 3.2 批次 3（P2 + 需新后端端点，契约先行）

落地纪律（方案 §6）：每项先动 `agentcrew/api/server.py` 的 `API_ROUTES` + `artifacts/contracts/openapi.yaml`（`tests/test_api_contract.py` 双向比对会拦），再写前端 `verify:api` 假后端断言，最后前端 UI + e2e；每项独立提交可回退。

| 端点/改动 | 服务项 | 备注 |
| --- | --- | --- |
| `POST /runs/{id}/pause` | D1 真实暂停 | resume 语义兼容；若引入新 run 状态须同步 `contracts/enums.js` RUN_META 与状态映射（参照 PLANNED 的贯穿清单，`docs/WorkBuddy功能对齐-差距与实施.md` §3） |
| `PATCH /runs/{id}/plan` | E2 真实模式计划编辑 | 过渡形态（rewind instruction 重写）已在 UI 明示 |
| `POST /runs/{id}/followup` | 终态同 run 追问完全体 | 现行为 = 「基于此对话再问」新 run（继承 project），已可接受为过渡 |
| `GET /runs` 搜索/分页参数 | B1/B4 大库优化 | 前端本地过滤短期够用 |
| run 元数据端点（rename/archive/pin） | runMeta 跨设备同步 | 现存本地 settings.json，纯增量键 |
| guidance POST 落 ledger + SSE | A3 去 4s 轮询 | 现 POST 不写 ledger（仅 withdraw/compact 写） |
| approvals `decision.comment` | I4 审批意见字段 | 现 decision 仅 action |
| provider 配置写端点 | M1 直存 | 突破「Studio 不重写 agentcrew.yaml」边界，需明确拍板 |
| schedule daily/月度/cron | N3 | `_compute_recurring` 现仅 weekly |

纯前端 P2（无后端依赖）：A7 代码块全屏/HTML 预览（iframe sandbox 需安全评审）、B5 批量归档/删除 + 智能重命名、C4 临时运行、C5 目标+选项存为模板、D4 阶段耗时时间线、F3 awaiting_approval 节点徽标、G3 跨 run 同角色对比、H3 .yaml 文件导入、J4 HTML sandbox 预览/单 HTML 分享导出、K3 回滚历史浏览器、L3 事件分布小图、M2 provider 用量聚合、O4 i18n/更新检查。

**P4 多 run 并行视图**：突破「单实时通道纪律」（`app/realtime.js` 头注是硬约束），需单独设计评审，不在承诺范围——接手人不要顺手做。

### 3.3 开放决定复核表（方案 §8 现状）

1. conversation = run 1:1 —— **已采用并实现**，关闭。
2. H2 `agents --save` 绑定直写 —— **仍未核实**（实施前先读 service.py）。
3. M1 自定义供应商直存 —— 默认保留复制，未突破。
4. 终态追问过渡形态 —— 已按「新 run 继承 project」实现，可接受；完全体等 followup 端点。
5. P4 多 run 视图 —— 需单独评审。

---

## 4. 验证命令（本机 Windows / Git Bash 实测口径）

```bash
cd E:/agentcrew/studio
npm run check          # 秒级，无需 GUI：分层/导入完整性/体量/入口
npm run test:unit      # node --test tests/*.test.mjs（当前 155 用例）
npm run selftest       # 真实 UI 驱动 --mock 运行 + 15 页导航（16 项）
npm run e2e            # 15 页 + 命令面板/通知中心/快捷键/数据统计等（47 项）
npm run verify:api     # 假后端 21 断言（含工具试跑与 SSE 帧）

# Git Bash 下 Electron 子进程需清掉宿主注入变量；npm script 之外的直调写法：
env -u ELECTRON_RUN_AS_NODE -u NODE_OPTIONS ./node_modules/electron/dist/electron.exe tools/verify-api-bridge.js
env -u ELECTRON_RUN_AS_NODE -u NODE_OPTIONS ./node_modules/electron/dist/electron.exe tools/visual-check.js           # 视觉复检
env -u ELECTRON_RUN_AS_NODE -u NODE_OPTIONS ./node_modules/electron/dist/electron.exe tools/visual-check.js --update  # 版式/配色改了才重建
```

后端全量回归（改了 Python 才需要，如 N1 的 schedule 子命令）：

```bash
cd E:/agentcrew && python scripts/test.py    # 崩溃隔离跑器；基线 3612 passed / 0 failed
```

**验收基线（截至 2026-11-21 N1-N2）：136 模块 / 55 分片、unit 161/161、verify:api 21/21、selftest 16/16（15 页）、e2e 49/49、visual 24/24。** 任何一项做完都要不低于此基线（新增能力要带新增用例）。

---

## 5. 硬约束与已知的坑（全部实测踩过）

1. **体量护栏**：`tools/check-architecture.js` 按 `content.split('\n')` 计行——文件以换行结尾时**有效上限是 319 行**，不是 320。超限时先砍注释，再拆模块（本轮拆过 `conversation/assist.js`、`new-run/flow-options.js` 两个先例）。
2. **分层**：contracts/lib 不得依赖 ui/app/features；ui 不得依赖 app/features；features 可互相引用。lib 纯函数不许碰 window（node:test 直测）。
3. **新页面**：`index.html` 加 nav → `router.js` 分支 → `e2e.js`/`selftest.js` 两个 pages 数组同步（selftest 步骤 2 的「N 页导航」文案要一起改）→ `visual:update` 重建基线。漏 pages 数组会让 selftest 步骤 2 直接 FAIL。
4. **settings 新键**：必须先在 `main.js defaultSettings()` 加默认值（C-22 白名单），渲染端 `setSettings` 才放行（runMeta 是先例）。写失败要回滚内存态（cycleTheme 模式）。
5. **preload 白名单**：新增主进程推送频道要同步 `ALLOWED_EVENTS`（静默失败无报错）；新 invoke 通道同理。
6. **双通道纪律**：任一时刻只允许 SSE 或文件 watch 一条实时通道（`app/realtime.js` 头注）；resetRealtime 同时关两者，别绕过。
7. **REST/CLI 降级**：`isTransportError`（TRANSPORT/超时）= 结果未知，**只能报错不能降级**（防双发）；有 HTTP 状态码才是服务端明确答复。
8. **诚实空态**：任何依赖后端 API 的页面（flows/rewind/approvals/keys）在未连 API 时给 `emptyState` 引导去「设置 → 连接」，不伪造数据。e2e/selftest 跑在无后端环境，会验证这些空态。
9. **视觉基线**：凡是肉眼可见的 UI 变化，跑完 `visual:update` 再 `visual` 复检；只加逻辑不动版式时基线应保持一致（0.00%）。
10. **提交纪律**：工作区有更早会话的无关在途改动，提交只 `git add` 自己的文件；回滚 = revert 提交 + `visual:update` 回退基线。
11. **Windows 换行**：仓库无 .gitattributes 强制，git 会提示 LF→CRLF 警告，属正常；不要为了消除警告改全局配置。

---

## 6. 建议的接手顺序

1. 先跑一遍 §4 全套验证，确认起点是绿的（任何红先修环境再看代码）。
2. 顺手项（半小时级）：D2 预算 toast → D3 成本聚合 → H1 模板扩充 → L2 follow toggle。
3. 中型项：~~Q2 工具试跑页~~（✅ 2026-11-21）→ ~~E3 计划 diff~~（✅ 2026-11-21，§3.110）→ ~~F1-F2 任务图实时高亮与抽屉产物摘要~~（✅ 2026-11-21，§3.111）→ ~~J1-J3 产物增强~~（✅ 2026-11-21，§3.112）→ ~~O1-O3 设置页三件套~~（✅ 2026-11-21，§3.113）→ ~~L1-L2 事件页~~（✅ 2026-11-21，§3.114）→ ~~N1-N2 定时任务~~（✅ 2026-11-21，§3.115）→ 下一批：H1-H2 / P2-P3 / §5 内联审批。
4. 需拍板/核实的：H2（读 service.py 后回填结论）、§5 内联审批（建议先抽 decide+resume 公共函数）。
5. ~~N1 是唯一动 Python 的 P1 项，单独一笔提交~~（✅ 2026-11-21，`5ac16c6`；批次 2 内已无待动 Python 的 P1 项）。
6. 批次 3 一律契约先行（API_ROUTES + openapi.yaml + test_api_contract.py），每项独立提交。
