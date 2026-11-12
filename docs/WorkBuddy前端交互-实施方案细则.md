# WorkBuddy 前端交互对齐 — 实施方案细则

> 定位：本文是「调研 WorkBuddy 前端交互 → 修改 AgentCrew Studio」的**落地细则**，把已实施的五个环节展开为可执行、可验收的工程规格。与 `WorkBuddy功能对齐-差距与实施.md`（差距映射）和 `前端重构方案-WorkBuddy风格.md`（视觉语言）配合阅读。
> 状态：**已实施**（后端全量 2887 passed / 0 failed；前端 check 69 JS·42 CSS、e2e 32/32、selftest 16/16）。§7 列出仍未闭合的遗漏点。

---

## 0. 定位与范围

对齐目标：WorkBuddy 的「四区域布局」交互 —— 顶栏 / 侧边栏任务列表（分组·搜索·筛选）/ 对话区（消息流）/ 右侧结果区（概览·产物）。

本次落地三个增量 + 一个后端联动 + 一处细节打磨：

| 环节 | 类型 | 落点 |
|---|---|---|
| 1. Ledger observer | 后端联动 | 明细事件实时推 SSE |
| 2. 对话式执行流 | 前端新页 | 侧边栏「对话」页 |
| 3. 右侧结果区 | 前端新面板 | 顶栏「结果」按钮 |
| 4. 侧边栏任务列表 | 前端增强 | 搜索 / 筛选 / 分组 |
| 5. 交互细节 | 前端打磨 | 动效 / 折叠 / 焦点 |

红线（不破坏）：后端确定性引擎不变、配置合并顺序不变、测试只增不减、前端「密钥零接触」与「纯文本节点渲染防 XSS」不变。

---

## 1. 环节一：Ledger observer（明细事件实时化）

### 1.1 目标

让 `task_started / task_finished / task_notes / tool_call / gate_result / artifact_written / llm_call / plan_created` 等**明细事件**在 `ledger.emit` 落盘的同时推到 `on_event`（SSE），使 SSE 模式下前端能实时重建执行过程，摆脱「收到状态事件后 500ms 快照轮询」的延迟。

### 1.2 执行步骤

1. `agentcrew/runtime/ledger.py`：`Ledger.__init__` 增加可选参数 `observer: Callable[[str, dict], None] | None = None`，存入 `self._observer`。
2. `Ledger.emit`：在 `written = LedgerEvent.model_validate_json(line)` 之后、`return written` 之前，若 `self._observer is not None` 则 `self._observer(event, written.data)`。
   - **关键**：传 `written.data`（从写入行重建、已过 `default` 兜底脱敏），而非 `record.data`（`set/bytes` 仍可能是原值）。密钥零泄露。
3. `agentcrew/crew/crew.py`：
   - 模块级常量 `_LEDGER_SSE_EXCLUDED`（见下）。
   - 新增方法 `_make_ledger_observer(run_id)`，返回闭包：`event` 在排除表则跳过；否则 `payload = dict(data); payload.setdefault("run_id", run_id); self._on_event(event, payload)`。
   - `Crew._start` 构造 Ledger 时传入 `observer=self._make_ledger_observer(record.run_id)`。

排除表（避免重复/高频）：

```python
_LEDGER_SSE_EXCLUDED = {
    "run_started", "run_paused", "approval_requested", "run_finished",  # 已走 _dispatch_event
    "run_stalled",                                                       # scheduler 已 _notify
    "agent_stream_chunk",                                                # 逐块高频，正文走 agent_stream_delta
}
```

### 1.3 输入输出

- **输入**：`Ledger.emit(event, **data)` 的每次调用。
- **输出**：一条 `on_event(event, {**脱敏data, run_id})` 回调；SSE 端表现为一帧命名事件（`event: task_started` 等），`data.run_id` 供 `EventBus` 按 run 分桶。
- **不做**：不触发 Webhook（刻意只走 `_on_event`，明细量级会打爆接收端）。

### 1.4 边界条件

- **向后兼容**：`observer=None` 时 `emit` 行为与改造前逐字一致。
- **同步异常传播**：observer 抛异常会从 `emit` 向上抛（文件已落盘，审计不受损）。当前 `_on_event`（`ApiServer.on_event`→`bus.publish`→`put_nowait`）与 `_BoardHook.__call__` 均不抛，故安全；但**库层自定义 on_event 若抛异常，会中断 emit 调用方**——这是待文档化的契约。
- **库层直连 Ledger**：不传 observer 的 Ledger（测试/库层）无实时推送，属预期。
- **队列保护**：`EventBus` 队列 `maxsize=1000` + `put_nowait` 满即丢，慢客户端不阻塞 run。

### 1.5 验收标准

- `tests/test_ledger.py` 4 条：observer 收到脱敏 data / 调用时已落盘 / 非可序列化值脱敏 / 缺省 None 行为不变。
- `tests/test_stream_delta_2026_11_08.py::test_detail_events_reach_on_event_via_ledger_observer`：mock run 中 `task_started/task_finished/artifact_written/plan_created` 均达 on_event、每帧带 `run_id`、`run_started` 恰出现 1 次。

---

## 2. 环节二：对话式执行流

### 2.1 目标

把 run 的 ledger 事件重建成 WorkBuddy 式消息流：用户目标 → PM 规划 → 各 Agent 任务气泡（内聚工具调用/评审/产物/流式正文）。

### 2.2 执行步骤

1. 新增 `studio/src/features/conversation/index.js`，核心 `buildMessages()`：
   - 遍历 `S.dash.events`（已按到达序）：`run_started`→user 消息；`plan_created/plan_scored/plan_rejected/run_finished/run_paused/approval_requested/budget_exhausted/error`→system 消息；其余带 `task_id` 的→按 `task_id` 聚合为 agent 消息（`Map<taskId, msg>`）。
   - agent 消息 items：`task_notes`→note、`tool_call`→tool、`gate_result`→gate、`artifact_written`→artifact、`task_finished`→finish（finish 不单独渲染，状态在头部）。
   - 消息按首次出现 `order` 排序。
2. 渲染 `renderConversation(container)`：`page-head` + `.conv-wrap`（`.conv-list` + 「回到最新」浮动按钮）。
3. `appendConversationDelta(data)`：`agent_stream_delta` 累积到 `S.convStreams[taskId]`（限 80000 字符），对话页可见时重渲染。
4. 接线：`index.html` 侧边栏加「对话」导航（首位）；`router.js` 加 `conversation` 分支并让 composer 在对话页可见；`boot.js` 数字键 `pages` 首项改 `conversation`、`studio:refresh` 加对话分支；`realtime.js` `agent_stream_delta` 同时进流式面板与对话流、事件到达时 `refreshConversation`。
5. 样式 `styles/features/conversation.css` + `index.css` @import。

### 2.3 输入输出

- **输入**：`S.dash.events`（信封 `{ts, run_id, event, data}`）+ `S.dash.tasks`（取标题/状态）+ `S.convStreams`（流式正文）。
- **输出**：消息流 DOM——user 右对齐紫气泡、system 居中胶囊、agent 左对齐气泡（`角色头像 + 角色名 + task_id·标题 + 状态chip`），内部 tool/gate 折叠卡片、artifact 产物卡片、note 说明、流式正文 `pre`。
- **安全**：所有模型输出走文本节点 / `pre`，零 `innerHTML` 拼接。

### 2.4 边界条件

- **事件上限**：`S.dash.events` 截断为最近 500 条，超长 run 的最早事件不回溯（见 §7-3）。
- **流式正文瞬态**：`agent_stream_delta` 只走 SSE、不落 ledger，刷新后正文丢失（见 §7-1）。
- **task_notes 超长**：>400 字符折叠（「展开全文」）。
- **markdown 不解析**：`task_notes` 若含 markdown 表格，管道符以纯文本裸露（零依赖安全权衡，见 §7-2）。
- **自动滚动**：运行中仅当贴近底部才跟随，避免打断回看。
- **mock 演示模式**：fixtures 无 tool/gate/artifact 明细，对话流只显示目标/规划/任务起止（属数据缺失，非缺陷）。

### 2.5 验收标准

- `e2e.js` 测试 2d：`.conv-list` 且 `.conv-user` 且 `.conv-agent` 存在。
- `e2e.js` 测试 14：数字键 1→对话、5→Agents、8→产物。
- `npm run check`：单 JS 模块 ≤320 行、分层/导入/入口合规。

---

## 3. 环节三：右侧结果区（概览 + 产物）

### 3.1 目标

顶栏「结果」按钮打开右侧面板，双 tab：概览（运行统计/任务状态分布/门禁）+ 产物（文件清单/内联预览）。

### 3.2 执行步骤

1. 新增 `studio/src/features/result/index.js`：`openResultPanel(tab)` → `openDetailPanel(buildResultPanel())`（复用 `#detail-panel` 容器与遮罩/Esc）。
2. `buildResultPanel`：`dp-head`（标题 + `tabBar()`）+ `dp-body`，按 `S.resultTab` 选 `renderOverview()` 或 `mountArtifactsTab()`。
3. 概览：`run.stats` 8 格（任务/成功/失败/LLM调用/Token/成本/已运行/预算）+ 任务状态分布条（`STATUS_ORDER` 计数）+ 门禁汇总（`S.dash.gates`）。
4. 产物：**同步挂载 + 异步填充**（`mountArtifactsTab` 同步建 `.result-art`，`renderArtifactsInto` 异步 `listArtifacts` 后填文件列表与预览）；预览复用 `readArtifact` + `highlightCode`。
5. 接线：`index.html` 顶栏 `btn-result`；`boot.js` 绑定 `openResultPanel('overview')`；`styles/features/result.css` + `index.css` @import。

### 3.3 输入输出

- **输入**：`S.dash.run.stats`、`S.dash.tasks`、`S.dash.gates`、`listArtifacts(dir)`、`readArtifact(dir, path)`。
- **输出**：400px 右侧面板，概览网格/分布条/门禁 + 产物文件树/预览。
- **曾修 bug**：产物 tab 原为 `async` 函数，`h()` 把 Promise 当子节点 `appendChild` 失败致空白 → 改为「同步 mount + 异步 fill」。

### 3.4 边界条件

- **空 run / 空目录**：分别给「未选择运行」「该运行暂无产物」空态。
- **产物预览截断**：单文件 >100000 字符截断。
- **时长口径**：概览「已运行」用 `runDuration`（结束用 `finished_at - started_at`，运行中用 `now - started_at`）。
- **无导出**：结果区产物 tab 未放「导出 ZIP」按钮（独立产物页有）——见 §7-5。

### 3.5 验收标准

- `e2e.js` 测试 18：`btn-result` 打开 → `.result-tabs`/`.result-grid`（概览）→ 切产物 → `.result-art` 存在 → Esc 关闭。

---

## 4. 环节四：侧边栏任务列表（搜索 / 筛选 / 分组）

### 4.1 目标

当前运行的任务列表贴近 WorkBuddy 任务管理：搜索 + 状态筛选 + 按角色分组。

### 4.2 执行步骤

1. `index.html`：`sb-project-group` 内、`sb-tasklist` 前插入 `sb-task-search` 输入框 + `sb-task-filters` chips 容器。
2. `shell.js` 重写 `renderSidebarTasks()`：
   - `TASK_FILTERS` 五组：全部 / 进行中（running·review·rework）/ 待处理（pending·ready·blocked）/ 完成（succeeded·skipped）/ 失败（failed·cancelled）。
   - 过滤：`S.taskSearch`（小写匹配 id/title）+ `S.taskStatusFilter`（默认 all）。
   - 渲染：chips 幂等（`dataset.key` 变化才重绘）；任务按角色分组（`roleMeta` 色点 + 角色名·数量），前 12 个 + 「还有 N 个」。
   - 签名防抖：`filterKey|search|runDir|前12状态|总数`，不变跳过（避免每秒丢 hover）。
3. `sidebar.css` 补 `.sb-task-tools/.sb-task-search/.sb-task-chip/.sb-task-group/.sb-task-item/.sb-task-dot`。

### 4.3 输入输出

- **输入**：`S.dash.tasks`、`S.taskSearch`、`S.taskStatusFilter`。
- **输出**：搜索框 + 5 chips + 角色分组任务列表（状态色点 + 标题，点击 `openTaskDetail`）。

### 4.4 边界条件

- **只显示前 12**：「还有 N 个」为纯文本，点击跳计划审查页。
- **搜索仅匹配 id/title**（不匹配描述）——见 §7-6。
- **筛选为 5 组粗粒度**，非 10 态精确筛选——见 §7-7。
- **任务列表为空**：显示「暂无任务」/「无匹配任务」。

### 4.5 验收标准

- `e2e.js` 测试 19：搜索框存在 + chips ≥5 + 分组存在 + 输入「T0」过滤后列表仍可渲染。

---

## 5. 环节五：交互细节打磨

### 5.1 目标

对话流与结果区的动效/焦点/折叠与 WorkBuddy 扁平语言一致。

### 5.2 已落地

- **回到最新**：`.conv-to-latest` 浮动按钮，滚动离开底部时显示，点击回底。
- **进入动效**：`.conv-msg` / `.conv-item-body` 用 `transform/opacity ≤200ms`。
- **长文本折叠**：`task_notes` >400 字符折叠。
- **焦点环**：沿用 `base.css` 全局 `:focus-visible`（2px 紫色），`sb-task-search` / `.result-tab` 等新增控件均受控。
- **reduced-motion**：沿用 `base.css` 全局降级。

### 5.3 验收标准

- `npm run visual:update` 重建视觉基线后 `npm run visual` 通过（**当前未重建**，见 §7-8）。

---

## 6. 全量验证汇总

| 层 | 命令 | 结果 |
|---|---|---|
| 后端 | `python scripts/test.py` | 2887 passed / 0 failed / 1 skipped |
| 前端结构 | `npm run check` | 69 JS / 42 CSS 全合规 |
| 前端端到端 | `npm run e2e` | 32/32 |
| 前端自测 | `npm run selftest` | 16/16 |
| 视觉基线 | `npm run visual` | **未重建**（新增页样式未纳入基线） |

---

## 7. 遗漏与待补充（闭环跟踪）

> 13 项中 **12 项已闭环**，仅剩 1 项保留为设计取舍。每条标 ✅ 的项为后续迭代补上。

1. ✅ **流式正文不回溯** → 已闭环：后端 `stream_sink` 把正文追加落盘 `output/<run>/streams/<task_id>.md`（追加写/无 fsync/单文件 400KB 截断/task_id 消毒/失败降级），主进程 `streams:load` IPC + 前端 `loadRealRun` 加载填充 `S.convStreams`，刷新后对话流仍可见完整回复正文。测试 `test_crew_stream_sink_persists_without_on_event` + `test_crew_stream_sink_sanitizes_task_id`。
2. ✅ **markdown 不渲染** → 已闭环：新增 `lib/markdown.js`（零依赖，先转义再替换白名单标记，覆盖标题/粗体/斜体/行内代码/代码块/列表/表格/链接/分隔线，链接只放行 http(s)/相对/锚点）；对话流 `task_notes` 与流式正文改 markdown 渲染。单测 `tests/markdown.test.mjs`。
3. ✅ **事件 500 条上限** → 已闭环：`S.dash.events` 截断上限 500→2000、`ledgerHistory` 300→2000（内存 ~1MB 可接受）。
4. ✅ ~~概览「已运行」时长 bug~~ → 已修：`runDuration()` 用 `finished_at - started_at`。
5. ✅ **结果区无导出按钮** → 已闭环：产物 tab 加「导出 ZIP」（复用 `exportArtifacts`）。
6. ✅ **任务搜索仅匹配 id/title** → 已闭环：匹配扩展到 `description` / `brief`。
7. **状态筛选粗粒度**：保留 5 组（全部/进行中/待处理/完成/失败），不细化为 10 态——侧边栏窄、10 态 chips 放不下，5 组已覆盖常用视角。若需精确筛选可走「计划审查」页。
8. ✅ ~~视觉基线未重建~~ → 已重建并 `npm run visual` 20 张一致。
9. ✅ **单元测试缺口** → 已闭环：`buildMessages` 抽为纯函数 `lib/messages.js`，用 **node 内置 `node:test`**（零新依赖）补 `tests/messages.test.mjs`（6 例）+ `tests/markdown.test.mjs`（8 例）；`npm run test:unit`。
10. ✅ **无障碍 a11y** → 已闭环：折叠项 head 改 `<button>` + `aria-expanded`（可 Tab/Enter/Space 操作）、消息流容器 `aria-live="polite"`。
11. ✅ **流式正文刷新性能** → 已闭环：`appendConversationDelta` 加 100ms 防抖（`scheduleConvRender`），高频逐 token 不再逐帧全量重建。
12. ✅ ~~文档未同步~~ → 已同步：README/PROJECT-STATE/studio README/event-types.md。
13. ✅ ~~后端 observer 契约未文档化~~ → 已写入 `ledger.py` docstring + `THREAT_MODEL.md` T-47。

---

## 8. 收尾状态

- 剩余唯一保留项：§7-7（状态筛选保持 5 组粗粒度，属设计取舍）。
- 本轮回归：后端 `scripts/test.py` 2887 passed（1 例 `test_config_export_import_roundtrip` 为本机已知间歇性崩溃，单独重跑 10/10 通过，非回归）；前端 `npm run check` 71 JS / 42 CSS、`test:unit` 14/14、`e2e` 32/32、`selftest` 16/16、`visual` 20 张一致。
