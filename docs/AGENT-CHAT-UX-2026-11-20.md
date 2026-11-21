# Agent 对话界面调研与 Studio 对话页增强（2026-11-20）

## 1. 调研范围

- **通用助手**：ChatGPT、Claude、Gemini、Perplexity
- **执行型 Agent**：Manus、Devin、Cursor / Claude Code（工具调用与审批形态）
- **国内产品**：Kimi、DeepSeek、豆包（长文档、深度思考、检索）
- **设计模式资料**：Agentic UI（agentsurface.dev）、8 Agent UI Patterns、16 AI agent UI patterns

结论先行：2026 年的 Agent 对话区已经不再是「气泡 + 输入框」，而是**监督工作台**——
用户要在同一条时间线上看清「它做了什么、花了多久、产出了什么、哪里需要我拍板」。
纯聊天产品（ChatGPT/Claude）重在**输出物的沉淀与再加工**；执行型 Agent 重在**过程可见与
副作用可控**。AgentCrew 的对话页属于后者，但当前只做到了「把事件流画成气泡」。

## 2. 横向对比

| 产品 | 对话区值得借鉴的点 | AgentCrew 可复用性 |
|---|---|---|
| ChatGPT | 回复下方的复制 / 朗读 / 重试；「思考」折叠；写作块内联编辑；非破坏性重生成（1/N 版本） | 复制、消息操作可复用；重生成需要「同目标再跑一次」的语义，暂不做 |
| Claude | 短回答留在流内，交付物升级为 Artifact 侧栏；选区变成引用 chip | 产物抽屉预览（本轮已做）；选区引用留待后续 |
| Gemini | 长回答分节 + 表格 + 行内来源 chip；按节导出 | 分节/表格渲染已有；来源数据暂缺 |
| Perplexity | 每条回答是一份小报告：Answer / Links / Images 分页，可导出 | 检索与导出（本轮做了对话内搜索 + Markdown 复制） |
| Manus / Devin | 计划与步骤列表常驻；步骤可展开看工具调用；长任务可离开再回来 | 工具卡片与耗时（本轮）；后台任务面板已有独立页面 |
| Cursor / Claude Code | 工具调用卡片：命令、入参、输出、耗时、状态；危险操作先预览再确认 | 工具摘要行 + 状态/耗时（本轮）；审批预览需后端审批数据 |
| Kimi / DeepSeek / 豆包 | 深度思考开关、联网来源、长文档上下文；对话内搜索与历史管理 | 搜索与筛选（本轮）；思考/联网是模型侧能力 |

## 3. 归纳出的十个模式

1. **可读的工具卡片**：工具名 + 关键入参 + 成功/失败 + 耗时，详情可折叠。不要把原始 JSON 直接摊给用户。
2. **计划与实时进度**：先给步骤清单，再逐步打勾；比一个转圈更能建立信任。
3. **副作用前预览/审批**：发送、删除、发布前展示确切参数，两个按钮明确结果。
4. **产物升级为独立面板**：长文档、代码、图表不留在气泡里滚动，而是进侧栏/抽屉。
5. **非破坏性重生成**：保留旧版本，用 1/N 切换，而不是覆盖。
6. **消息级操作**：复制、重试、引用、定位到任务/产物。
7. **会话内检索**：关键词 + 类型筛选 + 命中高亮 + 无命中空态。
8. **长任务可恢复**：未读计数、回到最新、后台任务状态、离开再回来能接上。
9. **流式不抖动**：预留空间、增量渲染、不要每个 token 重排整页。
10. **来源与引用**：读过什么就标什么；没有来源的「查过了」会损害信任。

## 4. 本轮在 Studio 的落地

| 能力 | 实现 | 位置 |
|---|---|---|
| 对话工具栏 | 运行状态 + 任务/消息/产物计数 + 搜索 + 复制整段对话 | `features/conversation/toolbar.js` |
| 会话内搜索 | 180ms 防抖、`/` 聚焦、Esc 清除、命中数、`<mark>` 高亮、无命中空态 | 同上 + `view.js` |
| 类型筛选 | 全部 / 目标 / Agent / 系统 / 工具 / 产物 / 门禁，chips 带计数 | `lib/conversation.js` |
| 消息操作 | 复制整条 / 打开任务 / 复制提示；悬停或键盘聚焦出现 | `view.js` |
| 工具卡片 | 入参摘要（command/path/query/url…）+ 状态点 + 耗时 + 错误行 | `items.js` |
| 产物卡片 | 按扩展名选图标；点击在右侧抽屉预览 Markdown/代码/图片；可复制路径、跳关联任务 | `preview.js` |
| Agent 页脚 | 耗时 · 产物数 · 门禁结论 · 返工次数 | `view.js` |
| 系统消息 | 计划 → 查看计划；结束 → 查看结果；审批 → 查看计划与审批 | `view.js` |
| 未读与回底 | 离开底部时累计「N 条新消息」，回底清零 | `index.js` |
| 代码块复制 | Markdown 渲染后给 `<pre>` 挂复制键（不影响原有安全策略） | `items.js` |
| 正文兜底 | 无 `task_notes` 时用 `task.result.summary` 作为消息正文 | `lib/messages.js` |
| 空态与可达性 | 无运行 → 新建运行；无事件 → 查看任务图；`role=log`、aria 标签、触屏常显操作 | `index.js` |

顺带修掉一个真实缺陷：筛选导致消息顺序变化时，`reconcileMessages` 按 key 判断「旧节点是否还在用」，
会把已被新节点替换的旧节点留下，出现**重复消息**（搜索时一条变两条）。改为按节点身份清理。

## 5. 暂未做与原因

| 能力 | 为什么不在本轮 |
|---|---|
| 内联审批（通过/驳回/重跑） | `S.dash` 目前没有 approvals 数据；需要主进程读 `approvals.json` + 裁决 IPC，属于后端契约改动，单独立项更稳 |
| 重生成 / 分支对话 | run 是一次性执行而非可编辑会话；需要「基于同一目标再跑」与分支语义，不能只做 UI |
| 行内来源引用 | ledger 没有 web 检索来源字段，可以先在工具卡片里展示 `web_search` 入参与结果，待事件补齐 |
| 分享链接 | 涉及发布与权限，暂用「复制 Markdown」作为轻量导出 |

## 6. 验证

- `npm run check`：88 模块 / 46 样式分片通过
- `npm run test:unit`：**50/50**（新增 10 例对话筛选/导出 + 2 例正文兜底）
- electron `e2e`：**35/35**（新增：工具栏/搜索/筛选/产物预览、复制 Markdown）
- `npm run visual`：22 张截图与基线一致

## 7. 参考

- AI UX Playground：<https://aiuxplayground.com/teardowns/compare/output-artifacts-refinement>
- Agentic UI 模式：<https://agentsurface.dev/docs/agentic-ui>
- 8 Agent UI Patterns：<https://dev.to/khiem_nguyen_cb052493dab2/8-agent-ui-patterns-52d>
- 16 AI agent UI design patterns：<https://www.setproduct.com/blog/ai-agent-ui-design-patterns>
- Agent UX is not chatbot UX：<https://dev.to/victor_desg/agent-ux-is-not-chatbot-ux-and-most-teams-in-2026-ship-them-as-if-they-were-23bi>
- ChatGPT / Claude / Gemini 功能对比：<https://drainpipe.io/?p=4993/>
- Kimi / DeepSeek 能力与定价：<https://toolquestor.com/vs/deepseek-vs-kimi>
