# AgentCrew Studio 前端功能补全方案（对标成熟产品，2026-11-21）

> 状态：**方案待评审**（本轮只读调查产出，未改任何代码）
> 范围：`studio/` 全部 13 页 + 全局组件；后端仅列依赖清单，不在本方案内实施
> 方法：逐模块实读当前 revision 源码锚定现状 → 对标成熟产品功能集 → 输出补全项（优先级 / 依赖 / 落点 / 验证）
> 视觉语言：沿用 R67 iOS/HIG 令牌与原子组件（`docs/STUDIO-IOS-REDESIGN-2026-11-19.md`），本方案**只补功能、不改设计语言**；布尔开关统一 iOS Switch，弹窗/空态复用 `ui/modal.js`、`ui/states.js`

**与既有文档的关系**

| 文档 | 关系 |
|---|---|
| `docs/AGENT-CHAT-UX-2026-11-20.md` | 对话页增强（工具栏/搜索/筛选/工具卡片/产物预览/消息操作/未读）已落地；其 §5「暂未做」四项在本方案 §5 重新裁决 |
| `docs/WorkBuddy功能对齐-差距与实施.md` | 对齐基线（执行模式三态/定时任务/执行体档案已补齐）；本方案在其覆盖映射之上继续补「会话管理、追问、通知、流程库」等空白 |
| `docs/GAP-ANALYSIS-2026-11-10.md`、`docs/IMPROVEMENT-PLAN-2026-11-09.md` | 历史差距/改进清单；本方案不重复其后端项，仅引用前端相关结论 |
| `docs/PROFILES-2026-11-20.md`、`docs/RUN-GUIDANCE-SPEC-2026-11-14.md` | profiles 与 guidance 的现行契约，本方案对应模块以其为准 |

---

## 1. 结论先行

**核心决策：conversation = run（1:1），不引入独立会话实体。**
后端 ledger / checkpoint / guidance / rewind / approvals 全部以 run 为作用域（路由表 `agentcrew/api/server.py:191-237`）。引入「一会话含多 run」需要全新后端存储与迁移，收益不抵成本。在此模型下：

- **新建对话** = 空态对话页 + 输入框直发创建 run（REST `POST /runs` 优先，CLI 兜底）；
- **对话列表** = 侧栏 Recents 升级为全量会话列表（搜索 / 重命名 / 置顶 / 归档 / 时间分组）；
- **运行中追问** = 输入框直发 `POST /runs/{id}/guidance`（后端已有），对话流渲染为用户气泡；
- **结束后追问** = 「基于此对话再问」新 run（继承 project + 上下文摘要，零后端改动）；真·同 run 多轮列为 P2 后端新增；
- **分支 / 重做** = 现有 rewind（dry-run → resume）挂到对话气泡上。

**三批落地**

| 批次 | 内容 | 依赖 |
|---|---|---|
| 批次 1（P0） | 对话闭环：新建对话 / 列表 / 直发与追问 / 停止 / 附件 / 审批自动 resume / 气泡级重做 | 纯前端 + 现有端点 |
| 批次 2（P1） | 管理与观测：flows 流程库页、通知中心、Ctrl+K、产物版本 diff、schedule 编辑、快捷键自定义等 | 现有端点 / 本地存储 / 小 CLI 增量 |
| 批次 3（P2） | 需新后端端点：pause / followup / plan PATCH / 元数据端点等 | 后端契约改动 |

**投入产出比最高的三项**：① flows 流程库页（后端 CRUD + validate 全现成、前端为零）；② 审批后自动 resume（现在裁决完还要用户手动 resume）；③ 通知中心（现在通知按钮是 stub，后台跑完没人知道）。

**硬约束**（沿用 `studio/README.md:155-160`）：分层 contracts→lib→ui→app→features 单向；单 JS ≤320 行、单 CSS ≤300 行；模型输出零 `innerHTML` 拼接；零新 npm 依赖；新增页面同步 `e2e.js` / `selftest.js` pages 数组与视觉基线；纯逻辑抽 `lib/` 走 `node:test`。

---

## 2. 证据基线

### 2.1 前端现状盘点（本轮实读）

`studio/src` 全部 16 个功能域 + `main.js` IPC 面（46 个 handler）+ `preload.js` + `backend-api.js`。关键事实：

- 对话页是**单 run 只读回放**：`features/conversation/index.js:133`、`lib/messages.js:9`（ledger → user/system/agent 三类消息）。
- 输入框**不产生数据**，只预填新建运行弹窗：`features/composer/index.js:50-61`。
- 运行列表 = 顶栏下拉（≤50 条 + 项目筛选 + 删除 + 对比，`features/runs/index.js:34,59-96`）+ 侧栏 Recents 5 条（`runs/index.js:111`）。
- 暂停仅演示模式假动作：`features/dashboard/status-bar.js:37-40`；真实模式只有 cancel / resume。
- 计划审查真实模式只读：`features/plan/index.js:139-142`。
- 审批裁决后**不自动 resume**，只 toast 提示手动：`features/gates-page/approvals.js:43`。
- 通知按钮 stub：`app/boot.js:57`（恒「暂无新通知」）；账户菜单 = 演示状态切换器：`app/shell.js:220`。
- `deriveAgents` 的 model/cost 硬编码 `'—'/0`：`app/data.js:47-48`。
- 实时通道纪律：任一时刻单通道（SSE 或文件 watch），`app/realtime.js` 头注。

### 2.2 后端能力地图（补全项的依赖判定依据）

| 能力 | 端点 / CLI（证据） | 前端现状 | 补全用途 |
|---|---|---|---|
| 创建 run（含多模态） | `POST /runs`（`server.py:1162-1275`，images 校验 `:1193`）；CLI `--image` ≤4 张 ≤2.6MB（`cli/parser.py:392`、`cli/common.py:64-78`） | 仅 CLI 通道、无附件 | 新建对话直发、附件 |
| run 选项白名单 | `api/run_options.py:30-66`（flow/flow_name、tools_enabled、docs_rag_enabled、supervisor_review_enabled、peer_dialogue_enabled、approval_mode、gate_config…） | 仅暴露 model/预算/调用数/mock/模式 | 新建弹窗「能力」组 |
| 运行中指引 | `GET/POST /runs/{id}/guidance`、`DELETE .../{gid}`、`POST .../compact`（`server.py:205-209`）；ledger 审计事件 `guidance_injected/expired`（`crew/assembly.py:109,130`）、`guidance_withdrawn/compacted`（`server.py:1023,1065`） | 门禁页管理面完整 | 对话追问气泡 |
| 人工审批 | `GET/POST /runs/{id}/approvals`（`server.py:202-203`） | 门禁页裁决，无自动 resume | 审批闭环 + 对话内联审批 |
| 取消 / 恢复 / 回滚 | `POST .../cancel|resume|rewind`（rewind 支持 dry_run，`server.py:212-214`） | 状态条 + rewind 页/弹窗 | 停止键、气泡级重做 |
| 运行中切模型 / 切执行体 | `PUT .../model`、`POST .../agents`、`POST .../tasks/{tid}/agent`（`server.py:215-221`） | 已接 | — |
| 事件 SSE / ledger / stats / artifacts | `GET .../events`（SSE，`backend-api.js:168-173`）、`/ledger`、`/stats`、`/artifacts` | 已接 | 通知中心、跨 run 搜索索引 |
| **流程库** | `GET/POST/DELETE /flows`、`POST /flows/validate`（`server.py:230-234`） | **零入口** | 新增 flows 页 |
| **API Key 管理** | `GET/POST/DELETE /keys`（`server.py:227-229`） | 设置页单 Key | Key 管理弹窗（P1） |
| **工具目录 / 试跑** | `GET /tools`、`POST /tools/{name}/test`（`server.py:225-226`） | profiles 编辑器仅用 CLI 目录 | 工具试跑面板（P1） |
| 入站 Webhook | `POST /webhooks/trigger`（HMAC，`server.py:236`） | 零入口 | P2 管理/文档面 |
| 定时任务 | CLI `schedule add/list/remove/tick`（`cli/schedule.py`；`Schedule.enabled` 字段 `:51`） | 列表/新建/删除/tick | 编辑、启停（需 CLI 增子命令）、触发历史 |
| 执行体档案 | CLI `agents --json/--save/--delete/--yaml/--usage/--check` | R69 工作台完整 | 模板扩充、绑定直写（待确认） |
| rewind 产物快照 | `history/rewind-NNN/`（`features/rewind/page.js:109`） | 仅提示文案 | 产物版本 diff |
| **缺失** | pause、plan PATCH、followup、run 元数据、`GET /runs` 搜索/分页、guidance POST 落 ledger、decision.comment、provider 配置写 | — | 见 §6 |

### 2.3 对标产品与来源

| 产品 | 借鉴面 | 来源 |
|---|---|---|
| ChatGPT | 会话 ••• 菜单（分享/重命名/归档/删除）、Ctrl+K 搜索（标题+正文、含归档）、临时聊天、消息编辑/重试/分支、代码块操作 | [归档与删除](https://help.openai.com/zh-hans-cn/articles/8809935-deleting-and-archiving-chats-in-chatgpt)、[搜索](https://help.openai.com/zh-hans-cn/articles/10056348-finding-your-chats-projects-and-files-in-chatgpt)、[临时聊天](https://help.openai.com/zh-hans-cn/articles/8914046-temporary-chat-in-chatgpt)、[编辑/重试/分支](http://www.shturl.cc/4d0cf4e1ca4afe3cf9a933f8d45c57ec)、[代码块](http://www.shturl.cc/7bd39f76df04e310cda0faf3b764dc04) |
| Claude | Projects = 独立工作区 + 知识库 + 项目指令；新版项目「告诉你什么完成了、什么在等你」；Artifacts 独立窗口/迭代/分享 | [Projects](https://support.claude.com/en/articles/9517075-what-are-projects)、[Artifacts](https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them) |
| LobeChat / LobeHub | 话题面板（筛选/批量整理/归档/收藏/智能重命名/复制副本/导出分享）、分支对话（延续/独立）、Agent Groups、Schedule、资源库 | [话题](https://lobehub.com/zh/docs/usage/agent/topic)、[简介](https://lobehub.com/zh/docs/usage/start)、[分支对话](https://gitee.com/libearys/lobe-chat) |
| Cherry Studio | 助手库 300+ 预设、助手编辑五标签、知识库多来源+引用溯源、全局搜索/话题管理、设置五块+快捷键自定义+定时任务 | [对话-助手](https://cherryai.com.cn/docs/zh-cn/cherrystudio/preview/chat/)、[设置](https://cherryai.com.cn/docs/zh-cn/pre-basic/settings/)、[知识库](https://docs.cherry-studio-ai.com/knowledge-base/knowledge-base)、[官网](https://cherry-ai.com) |
| Dify | 可视化画布节点类型、单节点/整流程调试、失败路径（重试/默认值/错误分支）、版本发布/回滚/DSL 导出、人工审核（审批/修改/评论/超时）、日志/反馈/标注 | [Workflow Studio](https://dify.ai/zh/workflows)、[平台](https://dify.ai/zh) |
| LangGraph Studio | 图实时点亮、threads/assistants 管理、interrupt 挂起 UI（看 payload→输入→resume）、time travel（checkpoint 历史→编辑状态→fork 分支对比） | [Overview](https://docs.langchain.com/langgraph-platform/langgraph-studio)、[time travel/interrupt](https://theplanettools.ai/tools/langgraph)、[LangGraph Cloud](https://blog.langchain.com/langgraph-cloud/) |
| LibreChat | Agents/MCP/Artifacts/Subagents/RAG/fork/share 的功能组织方式 | [Features](https://www.librechat.ai/docs/features) |

---

## 3. 会话模型决策与失败恢复

### 3.1 候选取舍

| 候选 | 机制 | 裁决 |
|---|---|---|
| **A. conversation = run 别名层（采用）** | 会话列表/重命名/置顶/归档是 run 的前端元数据层；追问=guidance（运行中）或继承上下文的新 run（终态）；分支=rewind | 零后端存储改动；guidance/rewind/approvals 语义天然对齐；元数据本地存 `settings.json`，P2 再上端点同步 |
| B. 独立会话实体（一会话多 run） | 新建 conversation 存储，run 挂会话下，跨 run 上下文拼接 | **拒绝**：后端无会话存储；guidance/rewind/checkpoint 均 run 级，跨 run「同会话」语义需要引擎侧上下文继承契约；迁移与恢复成本高。保留为未来演进 |

### 3.2 采用合同与反例检查

| 流程 | writer / reader / oracle | 失败窗口与恢复 |
|---|---|---|
| 新建对话直发 | REST `POST /runs` 返回 run_id（`server.py:1274`）；CLI 通道靠 `proc:run-dir` + `waitForStartedRun` | **双通道互斥**：按 `apiBaseUrl && apiKeyConfigured` 选一条；REST 401/503 显式提示降级，**绝不静默双发**；CLI 秒退由 `proc:error/proc:exit` 报错（`boot.js:122-131`） |
| 运行中追问 | `POST guidance` 202；reader = `GET guidance` 列表 + ledger `guidance_injected` | POST 超时但服务端可能已写入 → 按钮置 pending、不自动重试，提示「已发送，请在列表确认」；以列表 pending→consumed 为恢复 oracle（4s 回显机制已有，`guidance/index.js:37`）。at-most-once 语义由后端保证，前端不重发 |
| 审批后自动 resume | `POST approvals` → `POST resume` | resume 409（他进程在跑）→ 显示「已裁决，待恢复」+ 手动重试按钮（复用 `rewind/page.js:62-69` 模式）；oracle = run status（SSE/`GET status`） |
| 本地元数据（别名/置顶/归档） | main.js `settings:set` 写 `runMeta` | 写失败回滚内存态并重渲染（沿用 `cycleTheme` 乐观更新+回滚，`shell.js:205-218`）；`deleteRun` 时 prune 对应键，避免幽灵元数据 |
| 停止 | REST `POST cancel` / CLI `cancelOffline` | run 级语义，无「只停一条消息」；UI 文案明示「停止整个运行」 |

---

## 4. 逐模块补全清单

优先级：**P0** 对话核心闭环；**P1** 管理/观测补全；**P2** 需新后端或低频。
依赖：〔现成〕后端/CLI 已有；〔本地〕settings.json/localStorage；〔新后端〕见 §6。

### A. 对话页 + 输入框（差距最大，核心）

现状：单 run 只读回放；输入框只预填新建弹窗；无新建对话/追问/停止/附件（§2.1）。
对标：ChatGPT 消息编辑/重试/分支；LobeChat 话题管理。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| A1 | **新建对话**：侧栏「+ 新对话」→ 清空当前 run 进空态对话页，焦点落输入框；空态给目标模板 chips | P0〔本地〕 | `app/shell.js` 侧栏、`app/router.js:60` 空态分支、`conversation/index.js:139` |
| A2 | **输入框语义分流**：空态/终态 → 直发创建 run（跳过弹窗，高级选项记忆上次）；running → 直发 guidance；running 时发送键变「停止」（cancel 带确认） | P0〔现成〕 | `composer/index.js:submit` 三分支 |
| A3 | **guidance 气泡**：`messages.js` 增 `guidance_injected/expired/withdrawn` 分支；发送成功乐观插入用户气泡，以 guidance 列表对账；气泡带「已被 T-xxx 吸收」chip | P0〔现成〕 | `lib/messages.js`、`lib/conversation.js` 新纯函数 |
| A4 | **消息级操作**：agent 气泡「从此重做」（复用 `openRewindDialog(taskId)`）；用户目标气泡「编辑后重发」= 预填新 run；复制已有 | P0〔现成〕 | `conversation/view.js` |
| A5 | **图片附件**：输入框「+」与新建弹窗选图（≤4 张、单张 ≤2.6MB，与 `cli/common.py:64-78` 同口径）→ CLI `--image` 或 REST `images`；用户气泡缩略图（`readRunImage` 已有） | P0〔现成〕 | `composer`、`new-run`、`main.js run:start` 传参 |
| A6 | 对话导出 .md 文件（`buildTranscript` 已有，加保存对话框）；目标模板库（recentGoals + 内置） | P1〔本地〕 | `conversation/toolbar.js` |
| A7 | 代码块全屏/语言标签/HTML 预览切换（iframe sandbox 需安全评审） | P2 | `conversation/items.js` |

### B. 会话/运行列表（runs + 侧栏）

现状：下拉 ≤50 条 + 项目筛选 + 删除 + 对比；Recents 5 条。
对标：ChatGPT ••• 菜单与 Ctrl+K；LobeChat 话题面板批量整理/收藏/智能重命名。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| B1 | 侧栏「对话」全量列表：时间分组（今天/昨天/近 7 天/更早）与项目分组切换；搜索 goal/run_id/别名；状态筛选 chips；去 50 条截断改本地分页 | P0〔本地〕 | `runs/index.js` + `shell.js` |
| B2 | 条目 ••• 菜单：重命名（本地别名）/置顶/归档（隐藏但可搜）/删除/对比/打开目录（后三者已有） | P0〔本地〕 | `runs/index.js` |
| B3 | `runMeta` 存储契约：`settings.json` 增 `runMeta:{[run_id]:{alias,pinned,archived}}`；deleteRun prune；写失败回滚 | P0〔本地〕 | `main.js` settings、`lib/store.js` |
| B4 | Ctrl+K 命令面板：搜 run/任务/产物/事件（当前 run）+ 页面跳转；跨 run 全文搜索需主进程增量扫 ledger 建索引 | P1〔本地+现成〕 | 新 `features/command-palette` |
| B5 | 批量归档/删除；智能重命名（默认模型一次调用生成标题，带成本提示） | P2 | `runs/index.js` |

### C. 新建运行弹窗（new-run）

现状：目标/两级模型/三模式/Mock/高级（预算/调用数/项目/慢速）/最近目标。
对标：ChatGPT 临时聊天；Cherry Studio 助手预设。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| C1 | **REST 创建通道**：已连 API 时 `POST /runs`（options 映射白名单键），返回 run_id 直接切换，消除目录扫描竞态；`waitForStartedRun` 仅留 CLI 通道；401/503 显式降级 | P0〔现成〕 | `new-run/run-launch.js`、`main.js` |
| C2 | 附件区（同 A5） | P0〔现成〕 | `new-run/index.js` |
| C3 | 「能力」折叠组：`tools_enabled / docs_rag_enabled / supervisor_review_enabled / peer_dialogue_enabled / flow_name`（后端全已有、前端零暴露） | P1〔现成〕 | `new-run/index.js` 高级区 |
| C4 | 临时运行：不写 recentGoals、runMeta 标记、列表默认隐藏 | P1〔本地〕 | `new-run` + `runs` |
| C5 | 目标+选项存为本地模板，一键复用 | P2〔本地〕 | `new-run` |

### D. 运行总览 dashboard

现状：状态条（取消/恢复/重跑/切模型/导出）+ 成本 + 门禁 + 事件 + 流式正文；暂停仅演示。
对标：LangGraph Studio 实时点亮与 interrupt；LangSmith 成本监控。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| D1 | 真实暂停（`POST /runs/{id}/pause` 缺失）；前端先给演示暂停按钮加「演示」角标，消除假 affordance | P1〔新后端〕 | `status-bar.js:37-40` |
| D2 | 预算 90% toast + 通知中心条目（顶栏高亮已有 `shell.js:60`） | P1〔现成〕 | `shell.js` + 通知中心 |
| D3 | 成本面板按模型/provider 聚合（`llm_call` 事件含 model） | P1〔现成〕 | `dashboard/cost.js` |
| D4 | 阶段耗时时间线（status_history 已有） | P2 | `dashboard/status-bar.js` |

### E. 计划审查 plan

现状：演示可编辑/批准；真实只读。
对标：Dify 人工审核（审批/修改/评论/超时）；LangGraph Studio 编辑状态后 resume。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| E1 | 审批闭环：plan 卡点审批卡加「查看计划」跳转；批准后自动 resume（同 I1） | P0〔现成〕 | `plan/index.js` + `approvals.js` |
| E2 | 真实模式计划编辑（需 `PATCH /runs/{id}/plan`）；过渡替代 = rewind instruction 重写任务要求（已有），UI 明示「通过重做改写」 | P1〔新后端〕 | `plan/index.js` |
| E3 | 计划 diff：两次 run 任务级对比（`compare.js` 已有指标对比，扩展任务表） | P1〔现成〕 | `runs/compare.js` |

### F. 任务图 dag + 协作流 flow

现状：Agent/任务双视图 + 缩放平移 + 三列泳道，数据齐全。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| F1 | 节点「正在执行」实时高亮与状态动画（SSE `task_transition` 已有；遵守 R67 性能红线：不加逐帧动画） | P1〔现成〕 | `dashboard/dag-*.js` |
| F2 | 节点抽屉补输入/输出产物摘要 | P1〔现成〕 | `detail/panel.js` |
| F3 | awaiting_approval 节点挂「待人」徽标，点击跳审批 | P2〔现成〕 | `dag-page` |

### F+. 流程库页（新增页：后端全现成、前端为零）

后端 `GET/POST/DELETE /flows` + `POST /flows/validate`（`server.py:230-234`）+ run options `flow/flow_name`（`run_options.py:57`）完全没有前端入口。对标 Dify 版本发布/回滚/DSL 导出。

- **P1〔现成〕** 新页 `features/flows-page`：流程列表 / 查看（复用 DAG 画布渲染 flow spec）/ 校验 / 从当前 run 另存为流程 / 新建运行时选 flow_name。
- 路由 + 侧栏 + `e2e.js`/`selftest.js` pages 同步 + 视觉基线重建。

### G. Agents 页

现状：8 角色统计 + 执行体热切。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| G1 | `deriveAgents` 补齐 model/cost（现硬编码 `'—'/0`，从 llm_call 事件聚合） | P1〔现成〕 | `app/data.js:47-48` |
| G2 | 角色→执行体绑定速览（链到 profiles 矩阵） | P1〔现成〕 | `agents-page/index.js` |
| G3 | 跨 run 同角色对比 | P2 | `agents-page` |

### H. 执行体档案 profiles

现状：R69 工作台完整（CRUD/导入/导出/诊断/用量/绑定矩阵只读）。
对标：Cherry Studio 助手库与五标签编辑。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| H1 | 模板库扩充（现 3 个 → 写作/复核/代码执行/数据分析分类预设） | P1〔本地〕 | `profiles/templates.js` |
| H2 | 绑定矩阵直写：现仅 YAML 复制；若 `agents --save` 支持 bindings 则直写（**待确认**，§8），否则保留复制 | P1〔待确认〕 | `settings/agents.js`、`profiles/matrix.js` |
| H3 | .yaml 文件选择导入（现文本粘贴）、档案分享导出 | P2 | `profiles/import-dialog.js` |

### I. 质量门禁 gates（审批 + 指引）

现状：三层门禁卡 + 审批 decide + guidance 全功能。
对标：LangGraph Studio interrupt 展示 payload、人输入后 resume。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| I1 | **批准后自动 resume**（现仅 toast 提示手动）；resume 409 显示状态+重试按钮，不假装成功 | P0〔现成〕 | `gates-page/approvals.js:43` |
| I2 | **审批卡上下文**：plan 卡点→计划摘要+任务数+「查看计划」；delivery 卡点→T-FINAL 产物清单+「查看产物」（数据都在 S.dash，让人看着内容裁决） | P0〔现成〕 | `approvals.js` |
| I3 | guidance 入口复制到对话输入框（A2），门禁页保留管理面（撤回/过期/压缩） | P1〔现成〕 | `composer` + `guidance` |
| I4 | 审批意见字段（decision 当前仅 action，comment 待确认） | P2〔新后端〕 | `approvals.js` |

### J. 产物 artifacts + 结果面板

现状：MANIFEST 树 + 预览 + 高亮 + ZIP + 图片放大 + 输入图片展示，完成度高。
对标：Claude Artifacts 独立窗口/迭代/分享。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| J1 | **版本对比**：rewind 快照 `history/rewind-NNN/` → 版本选择器 + 行级 diff（零依赖） | P1〔现成〕 | `artifacts/index.js` |
| J2 | Markdown 渲染视图切换（`lib/markdown.js` 已有，预览现仅高亮） | P1〔现成〕 | `artifacts/index.js` |
| J3 | 单文件另存为（main.js 增 `artifact:saveAs` IPC） | P1〔本地〕 | `main.js` + `artifacts` |
| J4 | HTML sandbox 预览、自包含单 HTML 分享导出 | P2 | `artifacts` / `result` |

### K. 回滚重跑 rewind

现状：独立页 + 任务详情弹窗，dry-run→确认→自动 resume 已闭环。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| K1 | 对话气泡「从此重做」入口（A4） | P0〔现成〕 | `conversation/view.js` |
| K2 | 影响集可视化：受影响任务列表 + 快照路径，链到 DAG 高亮 | P1〔现成〕 | `rewind/page.js` |
| K3 | 回滚历史浏览器（history/rewind-NNN 列表 + 只读查看） | P2〔现成〕 | `rewind` |

### L. 事件日志 events

现状：类型筛选 + 搜索 + 分页 + 展开 + JSONL/报告导出 + 执行体审计，完成度高。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| L1 | 时间范围筛选；事件→「跳到对话/任务」定位 | P1〔现成〕 | `events/index.js` |
| L2 | 实时滚动锁开关（follow toggle） | P1〔本地〕 | `events/index.js` |
| L3 | 类型分布/时间密度小图 | P2 | `events` |

### M. 供应商 providers

现状：卡片/测试/测速/在线模型/设默认/预设 YAML，完成度高。
对标：Cherry Studio 50+ 服务商统一管理。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| M1 | 自定义供应商直存：现仅 YAML 复制；配置写端点缺失 → 〔新后端〕或保留复制（§8） | P1〔新后端〕 | `providers/forms.js` |
| M2 | provider 用量/成本聚合（ledger llm_call）；Key 最近测试时间戳 | P2〔现成〕 | `providers/card.js` |

### N. 定时任务 schedules

现状：once/weekly 列表/添加/删除/手动 tick；无编辑/启停/触发历史。
对标：Cherry Studio Cron 定时；LobeHub Schedule。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| N1 | 编辑（预填表单+删旧项，前端闭环）；启停开关（`Schedule.enabled` 已有但 CLI 仅 add/remove/tick → 增 `schedule enable/disable` 子命令；**不要** main.js 直写 schedules.json，会与 tick 并发写冲突） | P1〔小 CLI 增量〕 | `schedules/index.js` + `cli/schedule.py` |
| N2 | 触发历史：`run_triggered`/goal 匹配 → 链到 run | P1〔现成〕 | `schedules/index.js` |
| N3 | daily/月度/cron（`_compute_recurring` 现仅 weekly） | P2〔新后端〕 | `cli/schedule.py` |

### O. 设置 settings

现状：七组卡片 + 配额弹窗 + 连接测试 + 主题三态。
对标：Cherry Studio 设置五块 + 快捷键全量自定义。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| O1 | 快捷键自定义（`boot.js:67-87` 硬编码 → settings.shortcuts 映射） | P1〔本地〕 | `boot.js` + `settings` |
| O2 | 通知偏好（完成/审批/预算开关，联动通知中心） | P1〔本地〕 | `settings` |
| O3 | 数据管理：输出目录占用、归档 run 一键清理、设置导出/导入 | P1〔本地〕 | `settings` + `main.js` |
| O4 | i18n、更新检查 | P2 | `settings/about.js` |

### P. 全局壳（顶栏/侧栏/通知/账户）

现状：通知按钮 stub；账户菜单 = 演示状态切换器。
对标：Claude 新版项目「告诉你什么完成了、什么在等你」。

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| P1 | **通知中心**：聚合 SSE/ledger 的 `approval_requested / budget_exhausted / run_finished / gate reject` → 列表 + 未读角标 + 点击跳转 + Electron 系统通知；去重键 run_id+event+ts | P1〔现成〕 | 新 `features/notifications` + `main.js` Notification |
| P2 | 侧栏折叠 + 对话列表区（B1） | P1〔本地〕 | `shell.js` |
| P3 | 演示状态切换器移入「开发者」子菜单，与真实账户语义分离 | P1〔本地〕 | `shell.js:220` |
| P4 | 多 run 并行视图：现行单实时通道纪律是硬约束，多路复用需单独设计 | P2（另立项） | `app/realtime.js` |

### Q. API Key 管理 + 工具试跑（后端现成、前端薄弱）

| # | 补全项 | 优先级/依赖 | 落点 |
|---|---|---|---|
| Q1 | Key 管理弹窗：`GET/POST/DELETE /keys` 列表/新建（明文只回一次）/吊销 + scope 展示；设置页单 Key 保留为快捷入口 | P1〔现成〕 | 新 `features/keys` 或 `settings/connection.js` 扩展 |
| Q2 | 工具目录 + 单工具试跑面板：`GET /tools` + `POST /tools/{name}/test`（沙箱隔离），展示 code_exec 本地/docker 口径与 web_search 启用态 | P1〔现成〕 | 新 `features/tools-page` 或 profiles 子 tab |

---

## 5. 对 AGENT-CHAT-UX §5「暂未做」的重新裁决

| 原暂缓项 | 原理由 | 本方案裁决 |
|---|---|---|
| 内联审批（通过/驳回/重跑） | 「S.dash 没有 approvals 数据；需主进程读 approvals.json + 裁决 IPC」 | **可做（P1）**：REST `GET/POST /runs/{id}/approvals` 已存在且 `approvals.js` 已在用；对话页系统消息「等待人工审批」处内联审批卡，直接 `apiFetch`，未连 API 时降级为跳转门禁页。无需新 IPC |
| 重生成 / 分支对话 | 「run 是一次性执行而非可编辑会话」 | **部分关闭（P0）**：分支/重做 = rewind dry-run→resume（A4/K1），语义是「从该任务重做下游」，与 ChatGPT 分支同构；「同目标非破坏性再跑」= 重新运行（已有）+  run 对比（已有）。1/N 版本切换不做（run 不可变是审计资产） |
| 行内来源引用 | 「ledger 没有 web 检索来源字段」 | **维持暂缓（P2）**：先在工具卡片展示 `web_search` 入参/结果摘要（数据已有），来源 chip 待事件补齐 |
| 分享链接 | 「涉及发布与权限」 | **轻量替代（P1/P2）**：.md 导出（A6）→ 自包含单 HTML 导出（J4）；托管分享链接不做 |

---

## 6. 需新后端端点清单（前端无法独自闭环）

| 端点/改动 | 服务项 | 优先级 | 备注 |
|---|---|---|---|
| `POST /runs/{id}/pause`（resume 语义兼容） | D1 | P1 | 现仅 cancel；演示暂停是假动作 |
| `PATCH /runs/{id}/plan` | E2 | P1 | 真实模式计划编辑；过渡用 rewind instruction |
| `schedule enable/disable` CLI 子命令 | N1 | P1 | 小增量；避免前端直写 schedules.json 的并发写冲突 |
| `POST /runs/{id}/followup` | A2 终态追问完全体 | P2 | 终态同 run 追问、继承上下文；过渡 = 新 run 继承 project+摘要 |
| `GET /runs` 搜索/分页参数 | B1/B4 | P2 | 前端本地过滤短期覆盖 |
| run 元数据（rename/archive/pin）端点 | B2/B3 跨设备同步 | P2 | P0 用本地 settings.json |
| guidance POST 落 ledger 事件 + SSE | A3 去 4s 轮询 | P2 | 现 POST 不写 ledger（仅 withdraw/compact 写，`server.py:984-994`） |
| approvals `decision.comment` | I4 | P2 | 现 decision 仅 action |
| provider 配置写端点 | M1 | P2 | 现行边界：Studio 不重写 agentcrew.yaml |

落地纪律：每项同步 `API_ROUTES`（`server.py:191`）+ `artifacts/contracts/openapi.yaml`，`tests/test_api_contract.py` 双向比对已有；前端 `verify:api` 假后端断言同步。

---

## 7. 实施批次与验证

### 批次 1（P0，对话闭环）

A1-A5、B1-B3、C1-C2、E1、I1-I2、K1。

- 验证：`npm run check`（分层/体量护栏）；`npm run test:unit` 新增纯函数用例（guidance 气泡聚合、runMeta 合并、时间分组、输入框分流判定）；`npm run e2e` + `selftest`；`npm run verify:api` 增三条假后端断言：guidance 直发对账、审批后自动 resume（含 409 分支）、REST 创建不双发；`npm run visual:update` 重建基线后 `npm run visual`。
- 提交节奏：每模块一提交 + README 功能节回填；批次末更新 `PROJECT-STATE.md`。

### 批次 2（P1）

flows 页（F+）、通知中心（P1）、Ctrl+K（B4）、产物版本 diff（J1-J3）、schedule 编辑/启停（N1-N2）、快捷键自定义（O1）、审批上下文与内联审批（I2/§5）、deriveAgents 补全（G1）、导出增强（A6）、Key 管理（Q1）、工具试跑（Q2）、D2-D3、E3、F1-F2、H1-H2、L1-L2、M1（若保留复制则降级）、O2-O3、P2-P3。

- e2e pages +2（flows、tools 若独立页）；视觉基线重建；schedule 子命令带 `tests/test_schedule.py` 增量用例。

### 批次 3（P2 + 后端新增）

pause / followup / plan PATCH / 元数据端点 / guidance SSE / comment / provider 写 / cron 扩展 / 多 run 视图评估。

- 顺序：后端契约测试先行 → 前端 `verify:api` → e2e；每项独立提交可回退。

### 回滚纪律

- 前端每批次独立提交，回滚 = revert 提交 + 视觉基线回退；本地元数据（runMeta/shortcuts/模板）为纯增量键，旧版本忽略未知键，无迁移。
- 后端新端点均为增量路由，回滚不影响旧客户端（旧前端不调用新路由）；pause/followup 若引入新 run 状态，须同步 `RUN_META`/`enums.js` 与 API 状态映射（参照 PLANNED 的贯穿清单，`WorkBuddy功能对齐-差距与实施.md` §3）。

---

## 8. 开放决定（实现前需拍板或核实）

1. **conversation = run 1:1** 为采用路线；独立会话实体保留为未来演进（§3.1）。
2. `agents --save` 是否支持角色绑定直写（H2）——本轮未核实；实现时先读 `agentcrew/profiles/service.py` 与 CLI `agents` 参数面确认，不支持则保留 YAML 复制。
3. 自定义供应商直存 vs 保留 YAML 复制（M1）——现行设计边界是 Studio 不重写 `agentcrew.yaml`（`studio/README.md:164`）；直存需突破边界或走新端点，默认保留复制。
4. 终态追问过渡形态（新 run 继承 project + 上下文摘要）是否可接受为 P0 体验；完全体等 followup 端点。
5. 多 run 并行视图（P4）突破单实时通道纪律，需单独设计评审，不在本方案承诺。

---

## 9. 参考

- OpenAI Help：删除与归档聊天 <https://help.openai.com/zh-hans-cn/articles/8809935-deleting-and-archiving-chats-in-chatgpt>
- OpenAI Help：查找聊天、项目和文件（Ctrl+K） <https://help.openai.com/zh-hans-cn/articles/10056348-finding-your-chats-projects-and-files-in-chatgpt>
- OpenAI Help：临时聊天 <https://help.openai.com/zh-hans-cn/articles/8914046-temporary-chat-in-chatgpt>
- OpenAI Help：编辑消息 / 重试 / 在新聊天中分支 <http://www.shturl.cc/4d0cf4e1ca4afe3cf9a933f8d45c57ec>
- OpenAI Help：写作块与代码块操作 <http://www.shturl.cc/7bd39f76df04e310cda0faf3b764dc04>
- Anthropic Help：Projects <https://support.claude.com/en/articles/9517075-what-are-projects>
- Anthropic Help：Artifacts <https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them>
- LobeHub Docs：话题 <https://lobehub.com/zh/docs/usage/agent/topic>
- LobeHub Docs：简介（Agent Groups / Schedule / 资源库） <https://lobehub.com/zh/docs/usage/start>
- LobeChat README（分支对话） <https://gitee.com/libearys/lobe-chat>
- Cherry Studio Docs：对话-助手 <https://cherryai.com.cn/docs/zh-cn/cherrystudio/preview/chat/>
- Cherry Studio Docs：软件设置 <https://cherryai.com.cn/docs/zh-cn/pre-basic/settings/>
- Cherry Studio Docs：知识库 <https://docs.cherry-studio-ai.com/knowledge-base/knowledge-base>
- Cherry Studio 官网 <https://cherry-ai.com>
- Dify Workflow Studio <https://dify.ai/zh/workflows>
- Dify 平台 <https://dify.ai/zh>
- LangGraph Studio Overview <https://docs.langchain.com/langgraph-platform/langgraph-studio>
- LangGraph time travel / interrupt 评述 <https://theplanettools.ai/tools/langgraph>
- LangGraph Cloud 发布博客 <https://blog.langchain.com/langgraph-cloud/>
- LibreChat Features <https://www.librechat.ai/docs/features>
- 仓库内：`docs/AGENT-CHAT-UX-2026-11-20.md`、`docs/WorkBuddy功能对齐-差距与实施.md`、`docs/STUDIO-IOS-REDESIGN-2026-11-19.md`、`docs/PROFILES-2026-11-20.md`、`docs/RUN-GUIDANCE-SPEC-2026-11-14.md`