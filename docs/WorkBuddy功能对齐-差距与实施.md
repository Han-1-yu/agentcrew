# AgentCrew ↔ WorkBuddy 功能对齐：差距分析与实施

> 状态：已实施（执行模式 + 定时任务 + 执行体档案，2026-11-09）
> 目标：以 WorkBuddy 为参照，明确 AgentCrew 现有功能覆盖与交互表现的差距，补齐/调整相关功能及前后端逻辑，且不破坏现有功能。

---

## 1. WorkBuddy 功能模型 → AgentCrew 覆盖映射

WorkBuddy 的核心能力（来自其运行时能力面）与 AgentCrew 现状逐项对照：

| WorkBuddy 能力 | AgentCrew 现状 | 覆盖 |
|---|---|---|
| **工作模式（Agent / Plan / Ask）** | 仅有 `--auto`（全自动）与 `--gate plan`（人工卡点）；无「只读规划」态；前端只有一个二元「自动执行」开关，非三态 | **部分** |
| **任务编排 + 实时看板** | run → 任务 DAG → 三层门禁 → 交付，10 种任务状态实时看板 | 已对齐 |
| **流式输出 / 思考过程展示** | R40 `agent_stream_delta` 实时流式面板 | 已对齐 |
| **角色 / 执行体（专家）** | 8 内置角色 + 执行体档案 + 角色绑定（`agents`/`switch` 命令） | 已对齐 |
| **技能 / 工具** | 原生工具注册表 + 只读工具模式 + MCP 工具挂载（`tools`/`mcp`） | 已对齐 |
| **记忆 / 经验沉淀** | `knowledge/experience` 跨 run 经验库 + `recall` 命令 + 报告归档角色 | 已对齐 |
| **知识库（资料库）** | `knowledge`（嵌入 + 检索）+ `artifacts` 产物浏览 | 已对齐 |
| **连接器（外部服务）** | 9 内置 Provider + 自定义 Provider + 端点测试（供应商页） | 已对齐 |
| **质量门禁 / 人工审批** | 三层门禁 + `approve`/`approvals` + 计划/交付卡点（deferred 审批单） | 已对齐 |
| **成本预算控制** | `--max-cost` / `--max-llm-calls` + 预算耗尽优雅收尾 | 已对齐 |
| **后端 API / 认证 / RBAC** | `serve` + 认证 fail-closed + RBAC + API Key（`api/`） | 已对齐 |
| **可视化** | Agent 依赖图 + 协作流图 + 成本/门禁图表 | 已对齐 |
| **定时 / 周期任务（Automations）** | 无（无调度器、无持久化日程） | **已补齐** |
| **云端托管（数据库/存储/认证/LLM）** | 本地 CLI + 文件落盘；无托管后端 | **不适用/缺失** |
| **Skills / Experts 市场** | 无 marketplace；角色/执行体为内置扩展 | **已补齐（浏览+导出）** |

## 2. 关键差距与取舍

### 2.1 工作模式（Agent / Plan / Ask）—— 已补齐 ✅

这是 WorkBuddy 最核心的「交互表现」差异：用户在命令框 / 新建弹窗里切换执行模式，决定「要不要动手、先看不看计划」。

- **Agent（自动执行）** = `--auto`（gate none，全自动跑完）。原有。
- **Plan（计划审查）** = `--gate plan --approval-mode deferred`：计划落盘后挂起审批（`awaiting_approval`），在「计划审查」页批准后才执行。原有能力，本次把前端「自动执行」二元开关升级为三态，并显式映射到该组合。
- **Ask（只读规划）** = **新增** `--plan-only`：只生成计划并落盘（`RunStatus.PLANNED`），不派发执行；可 `resume` 续跑成完整 run。对应 WorkBuddy「Talk only / hands off」。

### 2.2 定时 / 周期任务 —— 已补齐 ✅

WorkBuddy 的 Automations（一次性 / 周期性调度）在 AgentCrew 里原本是空白，本次补齐：

- **后端** `agentcrew/cli/schedule.py` + `main.py schedule`（add/list/remove/tick）：日程持久化到 `output/_schedules/schedules.json`；`tick` 幂等、到期用 `subprocess.Popen` 起 `run` 子进程、**不引入常驻 daemon**（谁负责「到点叫 tick」由部署方决定）。一次性日程触发后置 `enabled=False`（留档），周期日程推进到下一个命中点。
- **前端**：新增「定时任务」导航页（列表 / 新建一次性与周期日程 / 删除）+ `schedule:*` IPC + 主进程每分钟 `tick`。

### 2.3 云端托管 —— 不适用

WorkBuddy 是云端产品，天然带托管后端；AgentCrew 是本地 CLI + 桌面 GUI + 可选自托管 `serve`。二者部署模型不同，强行对齐无意义。

---

## 3. 本次实施改动范围

### 后端（agentcrew/ + main.py）

| 文件 | 改动 |
|---|---|
| `agentcrew/runtime/checkpoint.py` | `RunStatus` 新增 `PLANNED = "planned"`（只读规划态，非保护态，可 resume） |
| `agentcrew/config.py` | `Settings` 新增 `plan_only: bool = False`（一次性行为标志，不参与 resume 快照比对） |
| `agentcrew/crew/crew.py` | `_start` 在规划完成后、执行前判断 `plan_only` → 收口 `PLANNED`；`_exit_code` 增加 `PLANNED → ok` |
| `agentcrew/api/server.py` | `run_finished` 状态映射增加 `planned`（否则 API 通道会把 planned 误报为 failed） |
| `main.py` | `run`/`resume` 新增 `--plan-only` 参数；`_settings_from` 透传 `plan_only`；`_drive` 对 planned 打印「计划已生成，可 resume 续跑」；`_run_exit_code` 增加 `PLANNED → ok` |

### 前端（studio/）

| 文件 | 改动 |
|---|---|
| `src/lib/mode.js` | **新增**：执行模式共享模块（agent/plan/ask 三态 + 持久化键 `agentcrew:nrMode`） |
| `src/contracts/enums.js` | `RUN_META` 增加 `idle`/`planned`/`interrupted`（顺带修复 interrupted 缺失导致的顶栏崩溃隐患） |
| `src/features/composer/index.js` | 命令框模式标签改为三态循环（自动执行 / 计划审查 / 只读规划），`cmp-access` 文案随之变化 |
| `src/features/new-run/index.js` | 新建弹窗「自动执行」复选框改为三态分段控件 + 模式说明；`startRun` 传 `mode` |
| `src/features/dashboard/status-bar.js` | 恢复按钮对 `planned`/`interrupted` 状态也生效（planned → 一键「恢复」续跑执行） |
| `studio/main.js` | `run:start` 按 mode 映射 CLI 参数：agent→`--auto`、plan→`--gate plan --approval-mode deferred`、ask→`--plan-only` |
| `styles/features/new-run.css` | 新增 `.nr-mode` 分段控件样式 |
| `e2e.js` | 断言 6 从「自动执行复选框」改为「执行模式三态」 |

### 测试

| 文件 | 改动 |
|---|---|
| `tests/test_crew.py` | 新增 `test_plan_only_generates_plan_without_execution`（planned → resume → completed） |
| `tests/test_cli.py` | 新增 `test_planned_run_maps_to_0` |
| `tests/test_schedule.py` | **新增**：add/next_run（工作日/跳过周末）/tick 触发与幂等/remove，7 项 |

### 定时任务（Automations）改动清单

| 层 | 文件 | 改动 |
|---|---|---|
| 后端 | `agentcrew/cli/schedule.py` | **新增**：日程模型 + 读写 + next_run 计算 + tick 触发（Popen 不阻塞） |
| 后端 | `main.py` | 新增 `schedule` 子命令（add/list/remove/tick）+ `_cmd_schedule` + `_HANDLERS` 注册 |
| 前端 | `src/features/schedules/index.js` | **新增**：定时任务页（列表 / 新建一次性与周期 / 删除 / 立即检查） |
| 前端 | `styles/features/schedules.css` | **新增**：日程卡片与新建表单样式 |
| 前端 | `studio/main.js` | 新增 `schedule:list/add/remove/tick` IPC + `startScheduleTicker`（每 60s tick） |
| 前端 | `studio/preload.js` | 暴露 `scheduleList/Add/Remove/Tick` |
| 前端 | `index.html` / `router.js` / `styles/index.css` | 新增「定时任务」导航项与路由、样式入口 |
| 测试 | `tests/test_schedule.py` | 7 项单测 |

### 执行体档案（Skills / Experts 管理页）改动清单

| 层 | 文件 | 改动 |
|---|---|---|
| 后端 | `main.py` | `agents` 命令新增 `--yaml`：导出可复用 `agentcrew.yaml` 片段（agents.profiles + roles 绑定） |
| 前端 | `src/features/profiles/index.js` | **新增**：执行体档案页（档案卡片 + 角色→执行体绑定矩阵 + 导出 YAML） |
| 前端 | `styles/features/profiles.css` | **新增**：档案卡片与绑定矩阵样式 |
| 前端 | `studio/main.js` | 新增 `agents:export` IPC；修复 `schedule` 系列 IPC 的 JSON 解析（容错缩进多行 + add/remove 不再误解析 JSON） |
| 前端 | `studio/preload.js` | 暴露 `agentsExport` |
| 前端 | `index.html` / `router.js` / `styles/index.css` | 新增「执行体档案」导航项与路由、样式入口 |

---

## 4. 未实施项的可落地方案（供后续按需推进）

### 4.1 Skills / Experts 管理页 —— 已补齐 ✅（本轮）

- 新增「执行体档案」页（第 11 页）：浏览执行体档案（模型/工具白名单/写入范围/成本与 RPM 上限）、角色→执行体绑定矩阵（duty/weight/汇总策略）、「导出 YAML 片段」一键复制（可粘贴进 `agentcrew.yaml` 复用）。后端 `agents --yaml` 新增导出；前端 `agents:export` IPC + `profiles` 页。
- 导入暂以「复制片段 → 手动粘贴进 agentcrew.yaml」落地，不做程序化写配置（写坏 config 风险高于收益，且需要重启才能被 `Settings.load()` 读到）。

### 4.2 云端托管 —— 不适用（部署模型不同）

---

## 5. 影响说明与回归验证

### 影响

- **新能力**：三种执行模式（Agent/Plan/Ask）前后端一致；只读规划态 `planned` 完整贯穿 CLI → run.json → 前端看板 → API SSE。定时任务（一次性/周期）可注册、到期自动触发 run，主进程每分钟幂等 `tick`。
- **向后兼容**：旧 `startRun({ auto })` 调用仍可用（`p.mode` 缺省时按 `p.auto` 回退）；`run:resume` 行为不变；`EXIT_CODES` 字典未改（test_crew 对它有精确相等断言）。
- **不破坏现有功能**：`--plan-only` 与 `schedule` 是纯增量；`RunStatus.PLANNED` 只在新增路径产生；API 状态映射只多一个分支。

### 验证结果

| 检查 | 结果 |
|---|---|
| 后端 CLI 冒烟：`run --plan-only` → 状态 `planned`、9 任务落盘、退出码 0 | ✅ |
| 后端 CLI 冒烟：`resume` planned run → 执行 → `completed` | ✅ |
| 后端 CLI 冒烟：`run --gate plan --approval-mode deferred` → `awaiting_approval` | ✅ |
| 后端 CLI 冒烟：`schedule add/list/remove/tick` 全链路（到期触发 + 一次性停用） | ✅ |
| `pytest`：test_schedule（7）+ test_crew/test_cli 新用例 + api 组 | ✅ 通过（api 组首跑命中本机已知 pytest 随机崩溃，重跑通过） |
| `npm run check`（架构护栏） | ✅ 66 JS / 39 CSS 分片 |
| `npm run e2e` | ✅ 28/28（含「执行模式三态」与「10 页导航」断言） |
| `npm run selftest` | ✅ 16/16（10a 首跑抖动为已知时序问题，重跑全绿） |
| `npm run visual:update` | ✅ 基线重建 16 张 |
