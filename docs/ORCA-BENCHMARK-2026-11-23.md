# Orca 对标调研与完善方案（R70）

> 调研日期：2026-11-23。方法：`stablyai/orca` 官方仓库 README 一手核验 + 本地
> `E:\agentcrew` 源码通读（基线 `416ff5e`，R69/Studio §5 之后）。
> 口径沿用 `完善/AgentCrew开源对标调研与完善方案.md`：外部事实附来源，本地事实附
> 文件路径，建议与事实分开标注。星数/提交数等易漂移数字只作快照记录。

---

## 1. 执行摘要

1. **Orca 是什么**：开源（MIT）的 Agent IDE / 编排台——把用户**自己的订阅**
   （Claude Code、Codex、Grok、Cursor 等任意 CLI agent）接成一支可并行干活的
   bot 舰队，配桌面端、移动端与远程运行时。仓库快照：约 133.5k star / 8.5k fork /
   24,140 commits，Electron + TypeScript monorepo（`src/` `mobile/` `native/`
   `cloud/` `skills/` `examples/plugins/`）。
2. **与 AgentCrew 的关系**：两者同属「多 coding-agent 编排」赛道，但定位互补——
   Orca 是**人机协作的 IDE**（人随时介入、终端/聊天双模、移动端随身），AgentCrew
   是**确定性交付引擎**（DAG 调度无 LLM、契约门禁、可断点续跑）。AgentCrew 在
   治理/审计/可复现上更强，Orca 在**执行隔离、人类反馈回路、触发入口、执行体
   人格化**四件事上更成熟。
3. **本轮落地两片（均默认关闭、离线可测、不碰四条红线）**：
   - **O1 任务级工作区隔离**（`agentic_task_isolation`）：借 Orca「Parallel
     Worktrees」的隔离思想，补 R60 以来「同一 run 内所有 agentic 任务共享一个
     cwd」的并发隐患；
   - **O2 SOUL.md / MEMORY.md 执行体人格与记忆**：借 Orca「Bot Memory in
     Markdown」，给 R69 的执行体档案补**纯 Markdown、平台外可编辑**的人格与
     长期记忆 companion 文件。
4. **明确不借**：插件市场、Design Mode、Computer Use、移动端模拟器、语音输入
   （与「虚拟软件团队交付」定位不符或属重型 IDE 能力）；不引入 Orca 任何代码
   依赖（MIT 可借思想，不搬实现）。

---

## 2. Orca 已核验事实（README 一手，2026-11-23 快照）

| # | 能力 | README 原文要点 |
|---|---|---|
| F1 | Orca Bots | 在 Slack / Linear / GitHub 里让 bot 修 bug，回来是一个**测过的 PR**；跑在用户自己的 Claude Code / Codex 上；可打开 Orca 审查或介入 |
| F2 | Chat or Terminal | 任意 agent 可在聊天 UI 或裸 CLI 里跑，**会话在两种形态间迁移不丢上下文** |
| F3 | Orchestrate Agents | 把大任务交给一个协调 agent，它拆活、在**各自工作区**里起 worker agent、把结果汇回来 |
| F4 | Run Anywhere | 配对远程 Orca Server、任意 SSH 主机、或给 worktree 配独立云 VM；**笔记本休眠 agent 照跑** |
| F5 | Mobile App | 手机配对：agent 需要你时推送、聊天里回复、离席也能推进任意机器上的工作 |
| F6 | Review AI Output | 审 diff、**在确切变更行上留批注**、把聚焦反馈发回给 agent |
| F7 | Git Integration | 从 diff 起草 commit message 与 PR；把**失败的 checks / PR 评审意见**发回 agent 修 |
| F8 | Parallel Worktrees | 把任务快速拆进**隔离环境**，多 agent 并肩干活互不干扰 |
| F9 | Design Mode | 内置浏览器里点任意元素，复制其 HTML/样式/选择器进 agent 聊天 |
| F10 | GitHub & Linear Native | 一处看 GitHub/Linear 任务，就绪即为每个任务开工作区 |
| F11 | Orca CLI | GUI 能做的 CLI 都能做：建 worktree、起 worker、驱动浏览器/桌面、排自动化 |
| F12 | Plugins | 社区主题/编辑器/语言/面板，一键安装 |
| F13 | Split Anything | agent / 终端 / 浏览器 / diff 任意分屏 |
| F14 | Bot Schedules & Triggers | 每工作日 9 点跑、**PR 打开时跑、check 失败时跑**、webhook 触发 |
| F15 | Bot Memory in Markdown | bot 的灵魂与记忆是**可读可编辑的纯 Markdown**（`SOUL.md`、`MEMORY.md`） |
| F16 | Drag Files to Agents | 从资源管理器拖文件、从桌面拖图片，直接进 agent 提示词 |
| F17 | Jump Anywhere | 一个快捷键跳到跨仓库跨主机的任意工作区/标签 |
| F18 | Rich Previews | 工作区内直接打开图片 / PDF / Markdown |
| F19 | Mobile Emulator | agent 旁边跑 iOS 模拟器 / Android 模拟器，让它自己点、输、验 |
| F20 | Dictation | 快捷键说话，转写进提示词或终端 |
| F21 | Usage & Accounts | 看 agent 订阅额度用了多少、何时重置；不登出切换账户 |
| F22 | Automations | 按仓库排定时提示词：夜间依赖升级、每周 triage、flaky 测试清扫 |
| F23 | Computer Use | 没有 CLI/API 时，agent 看并驱动桌面应用与外部浏览器 |
| F24 | Supported Agents | 「任何 CLI agent——能在终端里跑就能在 Orca 里跑」，列名 30+（Claude Code / Codex / Grok / Cursor / Copilot / OpenCode / Qwen Code / Kimi / Goose / Cline …） |

仓库结构佐证：`skills/` `skill-guides/` `skill-stubs/`（技能体系）、
`examples/plugins/`（插件样例）、`mobile/`（移动端）、`cloud/`（远程运行时）、
`native/`（原生终端，topic 含 ghostty）、`.mcp.json`（MCP 接入）、
`orca.yaml`（配置即文件）。许可证 MIT。

---

## 3. AgentCrew 对应现状（本地源码核验）

| Orca 能力 | AgentCrew 现状 | 证据 |
|---|---|---|
| F3/F8 隔离工作区 | R60 起 agentic CLI 的 cwd = **run 级**工作区，run 入口一次性注入；**同 run 并发 agentic 任务共享同一 cwd** | `crew/crew.py:957` `set_workspace(run_dir/"workspace")`；`utils/workspace.py`（run 级 contextvar）；`models/providers/cli_agentic.py:145-152` |
| F6 行级反馈 | 产物版本 diff 只读（J1 rewind 快照）；G3 审批仅 action；guidance 是 run 级文本；返工通道只吃门禁 issues | `gate/gatekeeper.py`（`bump_rework` 唯一调用点）；交接表批次 3「approvals `decision.comment`」未动 |
| F7/F14 Git/触发 | `run_tests` + G1 执行校验（R51）；入站 webhook 触发是**通用** HMAC/API Key（C8），不识别 GitHub 事件；定时仅 weekly/once | `api/server.py`（`POST /webhooks/trigger`）；`cli/schedule.py`（`_compute_recurring` 仅 weekly，交接表 N3） |
| F15 Markdown 人格/记忆 | 执行体档案是 YAML（`agents.d/<key>.yaml`，R69），字段为绑定/治理项，**无人格/记忆文本**；知识在内部经验库与 docs RAG | `profiles/store.py:1-17`（docstring 已引 Claude Code `.claude/agents/*.md` 为先例）；`roles/binding.py:33-78`（`AgentProfile` 无 soul/memory） |
| F2 聊天↔终端 | 对话页 = ledger 监督视图（conversation=run 1:1）；CLI 执行体 headless stdin→stdout，无终端附着 | `cli_agentic.py:159-169`；Studio 对话页（§3.102/§3.106） |
| F4/F5 远程/移动 | REST+SSE+RBAC、云主机部署验收 7/10、Python/Node SDK；**无移动伴侣** | `api/server.py`；`docs/DEPLOYMENT-VM-2026-11-13.md`；`sdk/` |
| F16 拖文件 | 图片附件 ≤4 张 ≤2.6MB（composer 与弹窗共用）；**无任意文件拖入工作区** | Studio `features/attachments/`（§3.106） |
| F17 跳转 | Ctrl/Cmd+K 命令面板（页面/运行/任务三类） | §3.107 |
| F18 预览 | Markdown 渲染 / 代码高亮 / 图片 data URI；**无 PDF** | §3.112（J1-J3） |
| F21 用量 | token/成本 ledger + 预算硬上限 + 80%/90% 软阈值；**无订阅窗口额度**（CLI 执行体按次计费口径） | `models/budget.py`；`runtime/ledger.py` |
| F22 自动化 | 定时任务新建/编辑/启停/触发历史（N1-N2） | §3.115 |
| F24 任意 CLI | `cli` / `cli_agentic` 两类 provider + 29 预设（含 claude-code/codex）；命令模板 argv 化不经 shell | `models/providers/cli.py`、`cli_agentic.py`；R60（§3.80） |

**结论**：AgentCrew 的治理内核（确定性调度、契约门禁、审计、断点续跑）是 Orca
没有的长板，不动。短板集中在「执行体怎么干活、人怎么反馈、活从哪来、执行体
是谁」四个面，与 Orca 的 F8/F6/F14/F15 一一对应。

---

## 4. 差距矩阵与取舍（按 ROI 排序）

| 编号 | 借鉴项 | 对应 | 优先级 | 本轮 | 理由 / 边界 |
|---|---|---|---|---|---|
| O1 | **任务级工作区隔离** | F8/F3 | P0 | ✅ | 并发 agentic 任务共享 cwd 是真隐患（互相踩草稿/依赖安装）；隔离 + 上游产物播种即可，git worktree 作后续可选后端 |
| O2 | **SOUL.md / MEMORY.md** | F15 | P0 | ✅ | 与 R69 档案天然互补；纯文件、平台外可编辑正是 orca 的精髓；空文件=零变化 |
| O3 | GitHub 事件触发适配器 | F14/F7/F10 | P1 | ⏭ | 在 C8 入站 webhook 上识别 `X-GitHub-Event`（issues/PR/check_suite）→ 目标模板；需真实 webhook 联调，下一轮 |
| O4 | 行级评审批注 → 定向返工 | F6 | P1 | ⏭ | 审批 `decision.comment`（交接表 I4）+ 产物预览行锚点批注注入返工上下文；动审批状态机与前端，单独一轮 |
| O5 | git worktree 后端 + diff 交付 | F8/F7 | P2 | ⏭ | O1 的可选升级：配 `workspace_repo` 时按任务 `git worktree add`，T-FINAL 产 diff/commit message 草稿；依赖 git 环境与清理策略 |
| O6 | 终端双模（Chat↔Terminal） | F2 | P3 | ⏭ | Studio 需 xterm 附着与 PTY 会话管理，重型；且与「确定性引擎」定位有张力，先评审 |
| O7 | 移动伴侣 / 远程配对 | F4/F5 | P3 | ⏭ | 现有 REST+SSE 已可支撑只读移动 Web 伴侣；推送裁决需安全评审，不做原生 App |
| O8 | 订阅额度窗口 | F21 | P3 | ⏭ | CLI 执行体的 5h 窗口额度解析属厂商私有口径，易漂移；先做「按执行体聚合用量」（交接表 M2） |
| O9 | 任意文件拖入 / PDF 预览 | F16/F18 | P2 | ⏭ | 纯前端小片，随下一轮 Studio 批次顺手做 |
| — | 插件市场 / Design Mode / Computer Use / 模拟器 / 语音 | F9/F12/F19/F20/F23 | 不做 | ✗ | 与「虚拟软件团队交付」定位不符，或属重型 IDE/桌面能力；MCP 工具链已覆盖扩展性 |

**不引入**：Orca 代码依赖、Electron 终端栈（ghostty/native）、云 VM 编排、
插件市场。借思想不借实现，与既有对标文档「不引入重型依赖」原则一致。

---

## 5. 本轮实施设计（O1 / O2）

### 5.1 O1 任务级工作区隔离（`agentic_task_isolation`，默认 `false`）

**问题**：`crew.py:957` 在 run 入口注入 `run_dir/workspace`，contextvar 按协程
隔离但**所有任务协程继承同一个值**。两个并发 claude-code 任务（如后端+前端）
在同一 cwd 里各自 `npm install` / 写草稿，互相覆盖；失败后也分不清是谁写的。

**设计**（最小改动，复用 R60 既有机制）：

| 层 | 文件 | 改动 |
|---|---|---|
| 配置 | `config.py` | `agentic_task_isolation: bool = False`；env `AGENTCREW_AGENTIC_TASK_ISOLATION`、YAML 标量键、快照四通道登记 |
| 入口 | `api/run_options.py` | 白名单 + 布尔校验（非法 400） |
| 装配 | `agents/registry.py` | 构造 Agent 时下发 `task_isolation=settings.agentic_task_isolation` |
| 执行 | `agents/base_agent.py` | `execute()` 在 trace 之外再套一层工作区上下文：开关开且 `models.is_agentic_cli(alias)` 为真时，`set_workspace(run_ws/"tasks"/<task_id>)`，`finally` 复位；进入前把 `pack.upstream` 声明的上游产物从 `ArtifactStore` **播种**进隔离目录（`read_bytes` + `safe_relpath` 二次防逃逸，缺件跳过并计数）；落 `task_workspace_isolated` 事件（路径/播种数/跳过数） |
| 读回 | 无需改 | `_agentic_workspace_files` / `_workspace_binary_files` 读同一个 contextvar，自动从隔离目录按契约读回 |

**语义与边界**：
- 默认关：不建 `tasks/` 目录、不发事件、提示词与 cwd 逐字不变（黄金用例钉住）。
- 只影响 **agentic CLI** 任务；普通 provider / mock 通道 `is_agentic_cli` 恒
  False，离线回放不受影响。ReAct 工具（write_file 等）仍以 run 级工作区为根
  （`crew/assembly.py:540`），不受本开关影响。
- 同一任务的多次 attempt / 返工复用同一隔离目录（task.id 稳定），自己的上一版
  文件天然可见；上游每次 execute 重新播种（覆盖），跟上上游返工后的新版。
- 隔离目录是 run 工作区的子目录，`clean` / 归档按 run 整体回收，无新增清理面。
- **不做** git worktree（O5）：本轮是「目录隔离 + 产物播种」，git 语义留待
  `workspace_repo` 配置出现时再叠加。

**验收**：新增 `tests/test_task_isolation.py`——默认关布局不变 / 开且 agentic
时按任务建目录 / 上游产物播种且缺件跳过 / 读回走隔离目录 / 并发两任务互不见
草稿 / 非 agentic 不隔离 / 配置四通道 / options 非法值 400。

### 5.2 O2 SOUL.md / MEMORY.md（companion 文件，空=零变化）

**问题**：执行体档案只有绑定/治理字段，「这个执行体是谁、记得什么」无处安放；
经验库是平台内部资产，人不能在平台外直接读写一个执行体的长期记忆。

**设计**（orca F15 的直译，文件即事实源）：

| 层 | 文件 | 改动 |
|---|---|---|
| 存储 | `profiles/store.py` | `companion_path(profile_yaml, kind)`（`<key>.SOUL.md` / `<key>.MEMORY.md`，与档案同目录）；`read_companion(...)`：缺失/空→`""`，超长按字符截断并标注，读失败→`""`（不阻断 run） |
| 装配 | `agents/registry.py` | 解析出 profile 后，按 `settings.profiles_dir` 读两份 companion，注入 `agent.soul_text` / `agent.memory_text`（仅文件化档案有；YAML 内联 profile 无 companion） |
| 提示词 | `agents/base_agent.py` | `build_messages`：`soul_text` 非空→在角色 system_prompt 后插「## 执行体人格（SOUL.md）」；`memory_text` 非空→追加「## 执行体记忆（MEMORY.md）」；两者皆空时 system 拼接逐字不变 |
| 文档 | `.env.example` / `agentcrew.example.yaml` / 本文 | 说明文件位置、生效时机（下一次 run / 工厂刷新）、字符上限 |

**语义与边界**：
- **Markdown 是唯一事实源**：YAML 保存/备份/回收站（R69）不触碰 companion；
  人在编辑器里改完，下一次 run 生效——与 orca「plain files you can read and
  edit」一致。Studio 编辑入口留下一轮（O2-FE）。
- 上限：SOUL / MEMORY 各 6000 字符（超出截断 + 「（已截断）」标注），防上下文
  预算膨胀；截断在读取层做，提示词层不再二次截断。
- 注入位置：人格紧跟角色职责（身份优先），记忆在 system 末尾（长期 notes，
  不覆盖交付物契约的近因优先级——契约仍在 user 消息末尾）。
- 分层护栏：`agents → profiles` 不在 `test_architecture.py` 禁止集内，合规。
- 默认空：无 companion 文件时提示词与 R69 逐字一致（黄金用例钉住）。

**验收**：新增 `tests/test_profile_documents.py`——读取四态（缺失/空/超长/
坏路径）/ 装配注入 / 无档案不注入 / build_messages 空态逐字不变 / 非空两段
出现且顺序正确 / YAML 保存不写 companion。

---

## 6. 红线合规

| 红线 | 本轮遵守方式 |
|---|---|
| 密钥绝不落盘 | O1/O2 均不接触密钥；companion 是用户自写文本，进提示词前不经过任何密钥通道 |
| 确定性引擎不变 | 调度/状态机/门禁零改动；O1 只改执行协程的 cwd 上下文，O2 只改提示词文本 |
| 配置合并顺序不变 | 新字段走既有四通道（代码默认→.env→YAML→env→CLI/options），不新增合并路径 |
| 测试只增不减 | 两片各带新测试文件；既有黄金用例（默认关逐字不变）必须仍绿 |

---

## 7. 来源

- https://github.com/stablyai/orca （README，2026-11-23 抓取；仓库快照
  133.5k star / 8.5k fork / 24,140 commits / MIT）
- 本地：`E:\agentcrew` @ `416ff5e`，文件路径见 §3/§5 各行
- 既有口径：`完善/AgentCrew开源对标调研与完善方案.md`、
  `docs/STUDIO-HANDOVER-REMAINING-2026-11-21.md`（批次 3 / I4 / N3 / M2）