# AgentCrew

AgentCrew 是一个多 Agent 协作开发平台：给它一个项目目标，由 8 个岗位角色组成的
虚拟团队自动完成需求分析、架构设计、前后端实现、测试、部署配置与文档，最终在
`output/<run_id>/artifacts/` 下交付一套带产物清单的完整项目。它既是可运行的演示，
也是一份「把 LLM 关进确定性引擎」的工程实现：调度、状态流转、依赖解析、契约校验
全部由可测试的代码完成，模型只负责它真正擅长的事。

---

## 项目介绍

AgentCrew 把一次交付拆成**两个阶段**：

- **规划阶段**：项目经理（`project_manager`）用一次受治理的 LLM 调用把目标拆解为
  任务 DAG。每个任务声明执行角色、依赖、验收标准与产物契约（路径、类型、最小字节数、
  必需符号等）。引擎先把保留的收尾任务 `T-FINAL` 追加到拆解结果上（由
  `project_manager` 承担，独占 `DELIVERY.md`），再对整个任务列表做结构性校验：
  任务 id 唯一、依赖无环且存在、角色已注册、产物路径不被两个任务同时声明、每个任务
  都有验收标准，等等；校验失败会把具体错误回灌给 PM 重新拆解（上限
  `plan_max_attempts`，默认 2 次）。规划阶段可以设置人工卡点（`--gate plan`），
  在开始烧 token 之前让人确认或编辑拆解结果。
- **执行阶段**：确定性调度器按 DAG 依赖派发任务、限流并发、组装上下文、执行状态机，
  并对每个任务的产物依次跑三层门禁（G1 静态校验 → G2 LLM 评审 → G3 人工卡点）。
  **调度决策全程没有 LLM 参与** —— LLM 只在两处被调用：任务执行（各角色 Agent）与
  G2 评审。这使平台可测试、可复现、可断点续跑。

```
┌──────────────────────────────────────────────────────────────┐
│ main.py  —  CLI（run / resume / status / roles / clean / cancel）│
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ Crew — 确定性执行引擎（不包含任何 LLM 调度逻辑）                 │
│                                                              │
│  ┌────────────┐   ┌────────────┐   ┌──────────────┐          │
│  │ TaskBoard  │──▶│ Scheduler  │──▶│  Gatekeeper  │          │
│  │ DAG+状态机 │◀──│ 就绪队列   │   │  G1/G2/G3    │          │
│  │ 唯一事实源 │   │ 信号量限流 │   │              │          │
│  └─────┬──────┘   └─────┬──────┘   └──────┬───────┘          │
│        │                │                 │                  │
│  ┌─────▼──────┐   ┌─────▼──────┐   ┌──────▼───────┐          │
│  │ Checkpoint │   │ ContextBus │   │ ArtifactStore│          │
│  │ JSON 原子写│   │ 上下文包   │   │ 契约+安全落盘│          │
│  └────────────┘   └────────────┘   └──────────────┘          │
│  ┌────────────────────────────────────────────┐              │
│  │ Ledger — 日志 / token / 成本 审计            │              │
│  └────────────────────────────────────────────┘              │
└───────────────────────────┬──────────────────────────────────┘
                            │  dispatch(Task) → AgentResult
┌───────────────────────────▼──────────────────────────────────┐
│ Agent 层（唯一持有 LLM 的地方）                                │
│  BaseAgent（模板方法：组装提示词 → 调用 → 解析 → 校验 → 提交）   │
│    └─ 8 个角色子类，各自实现钩子 + 声明 RoleSpec                │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ Model 层：ModelFactory → BaseProvider                         │
│   OpenAICompatProvider / AnthropicProvider / MockProvider      │
│   + StructuredOutput 降级链 + 能力位 + 计费表                  │
└──────────────────────────────────────────────────────────────┘
```

几条贯穿全局的设计原则：

1. **单一事实源**：任务状态只存在于 `TaskBoard`；任何状态变更必须经
   `TaskBoard.transition()` 走状态机校验，非法跃迁抛 `IllegalTransitionError`。
2. **角色是数据，不是代码**：8 个角色的差异集中在提示词与输出契约，全部由
   `RoleSpec` 数据对象承载；新增角色 = 新增一份 `RoleSpec`，引擎代码零改动。
3. **契约先行**：「完成」由机器可判定的检查定义（产物契约 + 门禁），不由 Agent 自称。
4. **一切可中断**：任意时刻 `Ctrl-C`，checkpoint 已落盘；`resume` 从最近一致状态继续。
5. **诚实**：mock 产物标注为非模型生成，评审失败、停滞、降级全部留痕（见「已知限制」）。

---

## 角色说明

8 个内置角色，每份 `RoleSpec` 位于 `agentcrew/agents/<key>.py`：

| key | 岗位 | 核心职责 | 主要交付物 | 默认评审人 |
|---|---|---|---|---|
| `project_manager` | 项目经理 | 目标拆解、计划制定、终验 | 结构化 `plan`（任务 DAG，不落盘为普通文件）；`DELIVERY.md`（收尾任务 `T-FINAL`） | architect + product_manager（双评审，都 approve 才算过） |
| `product_manager` | 产品经理 | 需求分析、验收标准 | `docs/PRD.md`、`docs/acceptance.md` | project_manager |
| `architect` | 架构师 | 技术选型、模块划分、接口定义 | `docs/architecture.md`、`docs/api-spec.yaml` | project_manager |
| `backend_dev` | 后端开发工程师 | 业务逻辑、数据模型、API 实现 | `backend/app/` 下的代码（附依赖与运行说明） | architect |
| `frontend_dev` | 前端开发工程师 | 页面、交互、样式 | `frontend/` 下的 HTML / CSS / JS | architect |
| `qa_engineer` | 测试工程师 | 测试用例、执行测试、Bug 报告 | `tests/` 下的测试与 `docs/test-report.md` | backend_dev |
| `devops_engineer` | 运维工程师 | 部署方案、Docker、CI/CD | `Dockerfile`、`docker-compose.yml`、`docs/deploy.md` | architect |
| `technical_writer` | 文档工程师 | README、使用文档、接口文档 | `README.md`、`docs/usage.md` | product_manager |

评审关系由 `RoleCatalog.reviewer_for(role_key)` 解析：先取角色自身声明的
`RoleSpec.reviewer_for`，再追加声明 `["*"]` 的通用评审人，过滤自评审与未注册 key；
没有任何评审人时回落到 `project_manager`（它也不在目录中则跳过 G2，由 G1 决定）。
`python main.py roles` 可列出这 8 个内置角色的 key、职责、边界与产出契约。

---

## 快速开始

要求 Python 3.10+（开发与验证环境为 3.12.10 / Windows 11；与设计规格 §1 的目标运行时一致）。三步：

```bash
# 1. 安装依赖（运行与测试都在这份清单里）
pip install -r requirements.txt

# 2. 配置模型 Key（Windows cmd / PowerShell 用 copy .env.example .env）
cp .env.example .env
#    编辑 .env，至少填一个 provider 的 Key，例如：
#    OPENAI_API_KEY=sk-...

# 3. 用内置演示目标跑一次完整交付
python main.py run --demo
```

说明：

- `--demo` 等价于使用 `examples/todo_web_project.py` 中内置的 Todo List Web 应用
  目标；也可以换成自己的目标：`python main.py run --goal "你的项目目标"`。
  `--goal` 与 `--demo` 二选一，都不给或都给会以退出码 3 报错。
- 默认模型是 `openai:gpt-4o-mini`，可在 `.env` 中用 `AGENTCREW_DEFAULT_MODEL`
  改成任意 `<provider>:<model>` 别名（见「模型绑定」）。
- 当 stdin 是终端时默认 `gate=both`：规划完成后与封版前各有一个人工确认点；
  当 stdin 不是终端（管道 / CI / 重定向）时，含人工卡点的模式会**显式降级为
  `gate=none`** 并在 stderr 说明，不会偷偷把人工闸门换成自动放行。想全程无人
  值守就加 `--auto`。
- 想看实时看板直接运行；想关掉看板（CI 友好）加 `--no-live`。

---

## 离线演示

没有 API Key、不想联网时，用 mock 模式跑完整流程：

```bash
python main.py run --demo --mock --auto
```

> **重要声明（mock 的诚实性）**：mock 模式下的产物来自
> `examples/fixtures/` 中**手写的预置回复**
> （`plan_todo.json`、`responses/<task_id>/attempt-<n>.md`、`reviews.json`），
> **不是模型生成的**。mock 的用途是**离线演示编排流程**与**支撑 hermetic 测试**
> —— 它证明的是「管线能跑通」，不是「模型能写出这样的东西」。把 mock 产物当成
> 模型能力证据会得出完全错误的结论；真实生成能力必须用 `--goal` + 真实 API Key
> 验证。mock 回放走与真实模型完全相同的数据通路（围栏解析、契约校验、安全落盘、
> 三层门禁一条不跳），因此它能真正验证管线，也仍然不能证明模型质量。

mock 演示的几个设计细节：

- 夹具里有一个**故意不合格**的尝试（`T-005` 的 `attempt-1.md` 是占位骨架：
  只有 `import` 与注释，缺少 `must_contain` 要求的 `def test_` 符号），用来演示
  G1 驳回 → 返工 → `attempt-2.md` 通过的完整闭环，所以一次 mock 运行中出现
  1 次返工是预期行为。
- `--mock` 是全局开关：覆盖所有角色绑定，任何 provider 别名都解析为 MockProvider，
  不需要任何 Key。
- 未匹配到夹具时返回「结构合法但内容为占位」的响应，并在 ledger 记一条
  `mock_fallback` 事件，绝不静默。
- `AGENTCREW_MOCK_LATENCY_MS` 可以给每次 mock 调用加固定延迟，方便观察实时看板。

**产物可运行性（人工验收步骤）**：mock 夹具里的 Todo 项目是手写的、真实可运行的
（FastAPI + 内存存储 + 原生前端），但平台不执行生成的代码（见「已知限制 1」）。
要确认它可运行，需要人工执行：

```bash
cd output/<run_id>/artifacts/backend
pip install -r requirements.txt
uvicorn app.main:app --reload
# 浏览器打开 http://127.0.0.1:8000/  → 增删改查 Todo 应正常工作
```

---

## 命令速查

六个子命令（`python main.py --help` 查看完整帮助；直接运行 `python main.py` 不带
子命令时，打印帮助、已注册角色数与最近 3 次 run 概览）：

| 子命令 | 用途 | 常用参数 |
|---|---|---|
| `python main.py run` | 执行一次完整 run（规划 → 执行 → 交付） | `--goal TEXT` 与 `--demo` 二选一；`--mock`；`--auto`；`--gate both\|plan\|delivery\|none`；`--max-concurrency N`；`--default-model ALIAS`；`--output-dir DIR`；`--context-budget N`；`--no-live` |
| `python main.py resume <run_id>` | 续跑一个中断或未完成的 run | `--force`（配置快照不一致时仍继续）；以及 `run` 的全部配置参数 |
| `python main.py status <run_id>` | 打印 run 记录、统计与任务看板 | `--output-dir DIR` |
| `python main.py roles` | 列出 8 个内置角色的职责 / 边界 / 产出 | 无 |
| `python main.py clean` | 保留最近 N 个 run，删除更旧的（默认 N=5） | `--keep N`；`--dry-run`（只列不删）；`--output-dir DIR` |
| `python main.py cancel <run_id>` | 给离线 run 打 cancelled 标记（下次 resume 直接收尾） | `--output-dir DIR` |
| `python main.py serve` | 启动 HTTP API + Web UI 面板（V2） | `--host HOST`（默认 127.0.0.1）；`--port PORT`（默认 8765） |

行为说明：

- `--auto` 等价于 `--gate none`；`--auto` 与 `--gate` 同时给出时 `--auto` 胜出。
- `resume` 会先比对 run 创建时的配置快照与当前配置：不一致（例如换了默认模型、
  或用非 mock 参数续跑一个 mock run）会打印差异并以退出码 3 拒绝，确认无误后加
  `--force`。`Ctrl-C` 中断的 run 会落盘为 `INTERRUPTED` 并提示 `resume <run_id>`；
  `RUNNING` 的任务在 resume 时回到 `READY` 重新执行（产物可能只写了一半），
  `REWORK` 的任务保持原状、由调度器直接重新派发，`REVIEW` 的任务保持原状只重跑门禁；
  `attempts` / `rework_count` 一律不重置。
- `clean` 的删除范围被限制在 `--output-dir` 之下，`--dry-run` 与真删共用同一套
  边界校验。

退出码（`agentcrew/crew/crew.py::EXIT_CODES`，对应 spec §7.4）：

| 退出码 | 含义 |
|---|---|
| `0` | 正常结束（全部任务 `SUCCEEDED`） |
| `1` | 存在 `FAILED` / `BLOCKED` / `SKIPPED` 任务 |
| `2` | 拆解连续未通过校验（`plan_max_attempts` 用尽） |
| `3` | 配置 / 参数错误（缺 Key、参数非法、resume 配置不一致未加 `--force` 等） |
| `4` | 停滞（死锁：没有可运行任务但仍有任务未终结） |
| `130` | 用户中断（`Ctrl-C` → `INTERRUPTED`）或取消（`cancel` → `CANCELLED`） |

---

## V2 增强功能

V2 在 V1 基础上新增七项增强，全部向后兼容（未配置时行为与 V1 完全一致）。

### 1. LLM 摘要式上下文压缩

当上游任务的自述摘要超出上下文预算时，自动调用 LLM 将其压缩为简短摘要，
避免长上下文截断关键信息。未配置摘要模型时走纯截断（零额外开销）。

- 实现：`agentcrew/context/summarizer.py`
- 接入点：Scheduler 在 `build()` 前调用 `prefetch_summaries(task)`
- 配置：无需额外配置，默认使用 `default_model` 做摘要

### 2. 按复杂度自动选模型

根据任务的认知复杂度（角色权重 + 验收标准数 + 依赖数 + 交付物数 + 描述长度）
自动分级为 LOW / MEDIUM / HIGH，不同级别可绑定不同模型。

```bash
# .env 中按级指定模型
AGENTCREW_TIER_LOW=openai:gpt-4o-mini
AGENTCREW_TIER_MEDIUM=openai:gpt-4o
AGENTCREW_TIER_HIGH=openai:gpt-4o
```

优先级：`role_bindings[role]` > `model_tiers[复杂度]` > `default_model`。

### 3. 外部 CLI 引擎接入

通过命令行工具接入任意 LLM 引擎（ollama CLI、llama.cpp、本地脚本等），
无需 HTTP API。prompt 通过 stdin 传入，stdout 作为补全文本。

在 `agentcrew.yaml` 的 `models.providers` 中配置：

```yaml
models:
  providers:
    cli:
      command: "ollama run {model}"
```

然后用 `cli:my-model` 作为模型别名即可。

### 4. 树状任务拆解

Task 新增 `parent_id` 字段，支持父子层级的任务树。父任务自动成为子任务的
依赖（父子边是 DAG 边的子集），因此现有拓扑排序和环检测天然保证父先于子执行。

- 查询：`TaskGraph.children(id)` / `ancestors(id)` / `subtree(id)` / `roots()` / `leaves()` / `depth(id)`

### 5. HTTP API + SSE 实时事件流

零依赖 async HTTP 服务器，提供 REST 端点和 SSE 实时事件推送：

| 端点 | 方法 | 用途 |
|---|---|---|
| `/health` | GET | 健康检查 |
| `/runs/{id}/status` | GET | 运行状态与任务看板 |
| `/runs/{id}/events` | GET | SSE 事件流（实时推送） |
| `/runs/{id}/events` | POST | 注入事件（调试用） |

### 6. MCP 协议互操作

零依赖 stdio MCP 客户端，支持连接外部 MCP 服务器发现和调用工具：

```python
from agentcrew.mcp import MCPClient

async with MCPClient(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]) as client:
    tools = await client.list_tools()
    result = await client.call_tool("read_file", {"path": "/tmp/x.txt"})
    print(result.text)
```

实现 JSON-RPC 2.0 + initialize 握手 + tools/list + tools/call。

### 7. Web UI 面板

`python main.py serve` 启动后，浏览器打开 `http://127.0.0.1:8765/` 即可访问
Kanban 任务看板（10 状态列）+ 实时事件流日志。输入 Run ID 后点击连接，
通过 SSE 自动刷新任务状态。

## V3 增强功能

V3 在 V2 基础上新增三十二项增强，全部向后兼容（未配置时行为与 V2 完全一致）。
按优先级分为 P0（立即做）、P1（近期做）、P2（中期做）、P3（长期做）四阶段。

### P0-1. G2 评审 score 阈值放行

G2 LLM 评审的 `score` 字段（0–1 诊断性质量分）此前只落 ledger、不参与裁决。
启用阈值后，当评审 verdict="reject" 但 score >= threshold 时，Gatekeeper 改为
放行（SUCCEEDED），避免"有小瑕疵但整体质量高"的产物被无意义返工。

- 放行时记补充 `gate_result` 事件：verdict 标 "approve"、issues 原样保留（审计可见被绕过的驳回理由）、reviewer 追加 `:score-bypass`
- 仅对 LLM 层生效（G1 静态校验 score 恒为 None，不会被误旁路）
- 旁路不消耗返工额度

```bash
# .env 中设置阈值
AGENTCREW_REVIEW_SCORE_THRESHOLD=0.8
```

默认 `None` = 不启用，保持现有行为。

### P0-2. Provider 故障转移

当主别名的 Provider 抛出不可用错误（503/429/连接超时/读取超时，内部重试耗尽后），
ModelFactory 按 `model_failover` 配置的备用别名列表依次尝试，每次切换记
`llm_failover` 事件（含 from_alias / to_alias / error）。

- 故障转移不消耗任务的 attempts（那是执行失败，不是模型不可用）
- 失败别名已消耗的 token 仍记账（真实 LLM 调用）
- 全部备用都失败时抛 `ProviderUnavailableError`（未配置 failover 时原样抛出原始错误，向后兼容）
- `call()` 和 `complete_structured()` 均支持

```yaml
# agentcrew.yaml
model_failover:
  openai:gpt-4o-mini:
    - anthropic:claude-haiku
    - deepseek:deepseek-chat
```

```bash
# .env（别名中 : 用 __ 表示，值逗号分隔）
AGENTCREW_FAILOVER_OPENAI__GPT-4O-MINI=anthropic:claude-haiku,deepseek:deepseek-chat
```

默认空字典 = 不启用，保持现有行为。

### P0-3. 成本预算硬上限

ModelFactory 累计 `total_cost_usd` 和 `llm_calls`，可配置硬上限防止失控 run 无限烧钱。
超限后 `_account()` 抛 `BudgetExhaustedError`，Crew 捕获后优雅收尾（等在跑任务完成，
不再派发新任务），记 `budget_exhausted` 事件，以 FAILED 收尾、退出码 1。

- 触发超限的那次调用仍被记账（ledger 事件先于异常发出，可追溯）
- 严格大于才抛：上限本身包含在内（`max_llm_calls=1` 允许 1 次成功调用）

```bash
# CLI 参数
python main.py run --goal "..." --max-cost 1.0 --max-llm-calls 50

# .env
AGENTCREW_MAX_COST_USD=1.0
AGENTCREW_MAX_LLM_CALLS=50
```

默认 `None` = 不限制，保持现有行为。

### P0-4. 可删除会话（删除单个 run）

`clean` 只能批量删旧 run，`cancel` 只打标记。新增按 run_id 删除单个会话的能力：

```bash
# CLI：打印概要后询问确认（TTY），--force 跳过
python main.py delete <run_id> [--output-dir DIR] [--force]

# API：必须带 ?confirm=1
DELETE /runs/{run_id}?confirm=1
```

- ``running`` 状态拒绝删除（退出码 3 / HTTP 409），提示先 `cancel`
- 删除前打印 run 概要（目标/状态/时间/产物大小），TTY 下询问 `[y/N]`
- 返回释放的字节数；Web UI 顶部有删除按钮（running 状态置灰，带确认对话框）
- 与 `clean` 共享同一套边界校验（链接样式 / output_dir 之外一律拒绝）

### P1-1. MCP 工具集成到 Agent 执行链路（ReAct 循环）

`BaseAgent.execute()` 从「单次 LLM 调用 → 落盘」升级为可选的 **ReAct 循环**：
模型在产出最终产物前，可以先调用工具（读文件、列目录、grep、跑 Python 片段、
外部 MCP 工具）获取信息，再写交付物。**默认关闭**——不传 `tool_registry` 时行为
与之前完全一致。

- **统一工具接口**：`agentcrew/tools/` 提供 `Tool` 基类、`ToolRegistry`、四个
  内置沙箱工具（`read_file` / `list_files` / `grep` / `run_python`），以及
  `MCPToolAdapter`——把 `tools/list` 发现的外部 MCP 工具包成同一接口登记进注册表。
- **ReAct 协议**：模型在回复里用 `<tool_call>` 围栏发出
  `{"name": ..., "args": {...}}`；工具结果以 `<tool_result name="...">` 回灌，
  可连续多轮，最多 `max_tool_calls`（默认 10）轮；触顶仍想调工具时降级为把最后
  回复当产物，记一条 `tool_loop_exhausted` 警告，不判失败。
- **能力边界**：`RoleSpec.allowed_tools`（默认 `["*"]`）按角色白名单过滤可见工具，
  未授权工具既不进提示词、被强行调用也会以错误回灌。
- **安全**：所有文件操作限制在项目根内（`safe_relpath` 拒绝 `../`、绝对路径、
  盘符）；`run_python` 用独立子进程（`-I` 隔离、cwd 受限、默认 10s 超时、裁剪
  环境变量），生成代码不在宿主进程执行。
- **诚实留痕**：每次工具调用都记 ledger `tool_call` 事件（工具名、入参摘要、
  success、耗时）。
- **配置**：`agentcrew.yaml` 可声明 `mcp_servers`（服务器名 → `command`/`args`），
  配置快照落盘前对含 key/token/secret 的字段脱敏为 `***`。

```python
# 给某个 Agent 接上工具（Crew 层接线，单测直接构造即可）
from agentcrew.tools import ToolRegistry, ReadFileTool, RunPythonTool
registry = ToolRegistry()
registry.register(ReadFileTool(workdir=project_root))
registry.register(RunPythonTool(workdir=project_root))
agent = GenericAgent(spec, models=..., store=..., ledger=...,
                     project_goal=..., tool_registry=registry)
```

### P1-2. 并行 G2 评审

多评审人（如 PM 任务的 architect + product_manager 双评审）从串行改为
`asyncio.gather` 并行，评审延迟从「N 人 × 单次延迟」降为「max(单次延迟)」。

- 单评审人保持原串行路径（行为不变，无 gather 开销）
- 聚合逻辑不变：全 approve 才 approve，issues 并集去重，score 取最小值
- mock 的 contextvar 隔离由 `set_purpose` 在每个协程内独立设置，已安全
- 评审调用受全局信号量限流，并行不会突破并发上限

### P1-3. 优先级调度

`TaskBoard.take_ready()` 按 `(priority, id)` 升序派发就绪任务：priority 数字小者优先
（1 = 最高，9 = 最低），同优先级按 id 字典序。`Task.priority`（1-9）字段此前存在但
未被消费，现已接入调度器。

- T-FINAL 的 `priority=9` 自然最后执行（与现有依赖保证一致，双重保险）
- 无依赖的高优先级任务先于低优先级任务派发，即使后者 id 更小
- REWORK 任务与 READY 任务共用同一优先级排序通道

### P1-4. 任务级超时

`Task.timeout_seconds`（默认 300 秒）控制单次 Agent 执行的最大时长。调度器的
`_execute_one()` 外包 `asyncio.wait_for()`，LLM 调用僵死不会永久占槽位。

- 超时转为 `AgentExecutionError`，走正常 `attempts` 重试通道（不单独计数）
- PM 可在 `TaskDraft` 中按任务指定 `timeout_seconds`（默认 300）
- 与 P2-2 停滞检测互补：任务超时是单次执行，停滞检测是调度器级别的兜底
- 正常快速完成的任务不受影响（默认 300s 远大于常规执行时间）

---

## 自定义角色 / 团队开发指南

「角色是数据，不是代码」不是口号：一个新角色的全部差异（提示词、输出契约、
温度、模型绑定、评审关系）都写在 `RoleSpec` 里，Agent 行为默认由 `GenericAgent`
（`BaseAgent` 的通用子类）承载，**引擎代码零改动**。只有需要特殊输出解析逻辑的
角色才值得写 `.py` 子类。

`RoleSpec` 的字段（`agentcrew/roles/spec.py`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `key` | str | 角色唯一标识，任务通过它引用角色（如 `wizard`） |
| `title` | str | 岗位名称 |
| `goal` | str | 核心职责（对应 CrewAI 的 goal） |
| `backstory` | str | 背景设定 |
| `responsibilities` | list[str] | 职责清单 |
| `boundaries` | list[str] | 能力边界：明确不做什么 |
| `output_contract` | str | 输出规范的自然语言描述 |
| `system_prompt` | str | 系统提示词 |
| `temperature` | float（默认 0.3） | 采样温度 |
| `max_output_tokens` | int（默认 8192） | 单次输出上限 |
| `model` | str（默认 `"default"`） | 模型别名；保持 `"default"` 时按三级优先级解析（见「模型绑定」） |
| `reviewer_for` | list[str]（默认空） | 默认评审本角色的角色 key；`["*"]` 表示声明者是通用评审人 |

### 方式一：`roles.d/` 目录（YAML，推荐给团队）

1. 在项目根目录的 `agentcrew.yaml` 里指定目录：

   ```yaml
   roles_dir: roles.d
   ```

2. 把角色 YAML 放进该目录（启动时自动加载所有 `*.yaml` / `*.yml`，按文件名排序；
   字段非法或 key 与已有角色重复都会直接报错，绝不静默跳过）：

   ```yaml
   # roles.d/wizard.yaml
   key: wizard
   title: 向导
   goal: 把项目目标翻译成一份向导说明。
   backstory: 你擅长把复杂目标压缩成三步。
   responsibilities:
     - 产出 docs/wizard.md
   boundaries:
     - 不写代码
   output_contract: 产出 docs/wizard.md（Markdown）。
   system_prompt: 你是向导，只输出 docs/wizard.md 的内容。
   temperature: 0.2
   reviewer_for:
     - project_manager
   ```

之后 `python main.py run --goal "..."` 会把这个角色注册进 `RoleCatalog`，PM 的
「可用角色」菜单直接来自这份目录，拆解时即可为 `wizard` 安排任务；计划卡点的
`[e] 编辑后执行` 也用同一份目录校验，自定义角色不会被误判为未注册。引擎与 CLI
代码均无需改动。

### 方式二：代码注册 `crew.register_role(RoleSpec(...))`

把 AgentCrew 当库使用时，可以在构造 `Crew` 之后、`run()` 之前直接注册
（完整可复制示例，`mock=True` 下离线即可验证）：

```python
import asyncio

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.roles.spec import RoleSpec

WIZARD = RoleSpec(
    key="wizard",
    title="向导",
    goal="把项目目标翻译成一份向导说明。",
    backstory="你擅长把复杂目标压缩成三步。",
    responsibilities=["产出 docs/wizard.md"],
    boundaries=["不写代码"],
    output_contract="产出 docs/wizard.md（Markdown）。",
    system_prompt="你是向导，只输出 docs/wizard.md 的内容。",
    temperature=0.2,
    reviewer_for=["project_manager"],
)


async def main() -> None:
    settings = Settings.load(mock=True, gate="none")  # 离线，无需 API Key
    crew = Crew(settings)
    crew.register_role(WIZARD)  # 引擎代码零改动
    record = await crew.run("开发一个 Todo List Web 应用。")
    print(record.status.value)


asyncio.run(main())
```

需要特殊解析逻辑时，再给角色配一个 `BaseAgent` 子类并把类一并传入：
`crew.register_role(WIZARD, WizardAgent)`；未登记专用类的 key 一律回落到
`GenericAgent`，行为完全由 `RoleSpec` 驱动。

注意：

- 注册必须发生在 run 开始之前（`AgentFactory` 在 run 开始时首次取用角色）。
- 任务引用未注册的角色 key 时，引擎会回落到带通用提示词的 `GenericAgent`，
  让任务仍有机会跑完，而不是把整次 run 打崩；但计划校验（任务角色必须已注册）
  会先拦住这种拆解。
- `python main.py roles` 列出的是 8 个内置角色；自定义角色通过目录 / 注册进入
  `Crew.catalog`，不改变这份内置清单。

---

## 模型绑定

模型别名格式为 `<provider>:<model>`，**只按第一个冒号切分**，因此模型名可以
自带冒号，例如 `anthropic:claude-sonnet-5`、`ollama:qwen2.5:14b`。

角色 → 模型的三级优先级（高者胜，`agentcrew/crew/crew.py::_alias_for`）：

1. `RoleSpec.model`（角色级；保持默认值 `"default"` 时不参与优先级）；
2. `agentcrew.yaml` 的 `models.role_bindings.<role_key>`，或用环境变量
   `AGENTCREW_MODEL_<ROLE_KEY>`（大写、下划线，如 `AGENTCREW_MODEL_QA_ENGINEER`）；
3. `Settings.default_model`（默认 `openai:gpt-4o-mini`，可用
   `AGENTCREW_DEFAULT_MODEL` 或 `--default-model` 覆盖）。

`--mock` 是全局开关，会覆盖以上所有绑定，把一切别名解析为 MockProvider。
`ModelFactory` 按别名缓存 provider 实例，避免重复建连。

8 个内置 provider（`agentcrew/models/config.py::PROVIDER_PRESETS`，可被
`agentcrew.yaml` 的 `models.providers` 覆盖）：

| 别名 | 实现 | 默认 base_url | `json_schema` | `json_object` | API Key 环境变量 |
|---|---|---|---|---|---|
| `openai` | OpenAI 兼容 | `https://api.openai.com/v1` | ✅ | ✅ | `OPENAI_API_KEY` |
| `anthropic` | Anthropic 原生 Messages API | `https://api.anthropic.com` | ❌ | ❌ | `ANTHROPIC_API_KEY` |
| `deepseek` | OpenAI 兼容 | `https://api.deepseek.com/v1` | ✅ | ✅ | `DEEPSEEK_API_KEY` |
| `doubao` | OpenAI 兼容 | `https://ark.cn-beijing.volces.com/api/v3` | ❌ | ✅ | `ARK_API_KEY`（豆包 / 火山方舟） |
| `qwen` | OpenAI 兼容 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | ❌ | ✅ | `DASHSCOPE_API_KEY`（通义千问） |
| `ernie` | OpenAI 兼容 | `https://qianfan.baidubce.com/v2` | ❌ | ❌ | `QIANFAN_API_KEY`（文心一言，千帆 v2） |
| `ollama` | OpenAI 兼容 | `http://localhost:11434/v1` | ❌ | ✅ | 不需要真实 Key（端点用 `models.providers.ollama.base_url` 覆盖，见「配置项」） |
| `mock` | MockProvider | — | ✅ | ✅ | 不需要（离线确定性回放） |

**`json_schema` 支持情况为什么重要**：需要结构化输出的调用（PM 拆解计划、G2 评审）
走三级降级链 + 修复循环：

1. 支持 `json_schema` 时，由服务端按 JSON Schema 严格约束输出（最稳、正常一次调用）；
2. 只支持 `json_object` 时，不传 schema 的 JSON 模式是「无形状」的，引擎把 schema
   内嵌进提示词补足；
3. 两者都不支持时（`anthropic`、`ernie`），纯提示词约束 + 从回复中提取 `json` 围栏块
   （兜底提取首个平衡的 `{...}`），解析 / 校验失败进入修复循环，把 Pydantic 校验错误
   原文回灌重试，最多 2 次（即最多 3 次模型调用）。

因此同一个结构化任务，在不支持 `json_schema` 的 provider 上可能花更多调用次数、
稳定性也略低（功能不受影响）。每次降级都会在 ledger 记一条 `llm_degraded` 事件
（工厂默认落账，调用方无需接线；显式传入自定义回调则替换该默认行为），便于事后
诊断「哪家 provider 不稳定」。能力位描述的是实际在跑的 provider，`--mock`
下就是 mock 预设的能力位。

---

## 配置项

配置来源与优先级（低 → 高，`agentcrew/config.py::Settings.load`）：

1. 代码内默认值；
2. `agentcrew.yaml`（可选；默认读工作目录下的这一文件，可用 `AGENTCREW_CONFIG`
   环境变量指定其他路径）；
3. 环境变量（`python-dotenv` 会把 `.env` 载入这一层；同名时真实环境变量优先于
   `.env` 中的值）；
4. CLI 参数。

### `.env`（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 空 | `openai` 的 Key |
| `ANTHROPIC_API_KEY` | 空 | `anthropic` 的 Key |
| `DEEPSEEK_API_KEY` | 空 | `deepseek` 的 Key |
| `ARK_API_KEY` | 空 | `doubao`（豆包 / 火山方舟）的 Key |
| `DASHSCOPE_API_KEY` | 空 | `qwen`（通义千问）的 Key |
| `QIANFAN_API_KEY` | 空 | `ernie`（文心一言，千帆 v2 API Key） |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | **不决定端点**：只作为 ollama 客户端所需的非空 api_key 取值（留空则用占位串 `ollama`）；端点请用 `agentcrew.yaml` 的 `models.providers.ollama.base_url` 覆盖 |
| `AGENTCREW_DEFAULT_MODEL` | `openai:gpt-4o-mini` | 默认模型别名 |
| `AGENTCREW_MODEL_<ROLE>` | 空 | 按角色覆盖模型，8 个：`PROJECT_MANAGER` / `PRODUCT_MANAGER` / `ARCHITECT` / `BACKEND_DEV` / `FRONTEND_DEV` / `QA_ENGINEER` / `DEVOPS_ENGINEER` / `TECHNICAL_WRITER` |
| `AGENTCREW_MAX_CONCURRENCY` | `4` | 全局并发上限（含 LLM 调用） |
| `AGENTCREW_OUTPUT_DIR` | `output` | run 输出根目录 |
| `AGENTCREW_GATE` | stdin 为终端时 `both`，否则 `none` | 人工闸门：`both` / `plan` / `delivery` / `none` |
| `AGENTCREW_CONTEXT_BUDGET` | `24000` | 上下文预算（tokens，启发式估算） |
| `AGENTCREW_LOG_LEVEL` | `INFO` | 日志级别 |
| `AGENTCREW_MOCK` | `false` | 全局 mock 开关 |
| `AGENTCREW_MOCK_LATENCY_MS` | `0` | mock 调用固定延迟（毫秒） |
| `AGENTCREW_CONFIG` | `agentcrew.yaml` | YAML 配置文件路径 |

> 注意：`OLLAMA_BASE_URL` **不参与端点解析** —— provider 的 `base_url` 只来自
> 内置预设或 `agentcrew.yaml` 的 `models.providers.<provider>.base_url`。把远程
> ollama 地址写进这个环境变量不会生效，请求仍会打到
> `http://localhost:11434/v1`；要换端点请用 YAML 的
> `models.providers.ollama.base_url`。

### `agentcrew.yaml`

YAML 只识别以下键；`output_dir` / `mock` / `log_level` 等请用环境变量或 CLI
设置，未识别的键会被忽略：

| 键 | 说明 |
|---|---|
| `max_concurrency` | 全局并发上限 |
| `context_budget_tokens` | 上下文预算（tokens） |
| `gate` | 人工闸门模式 |
| `roles_dir` | 自定义角色目录（见「自定义角色」） |
| `models.default` | 默认模型别名（等价 `AGENTCREW_DEFAULT_MODEL`） |
| `models.role_bindings.<role_key>` | 按角色覆盖模型 |
| `models.providers.<provider>` | 覆盖内置 provider 预设：`base_url` / `capabilities`（逐位合并） / `implementation` |

`models.providers.<provider>` 下的键会被**校验**：未知 provider、未知字段，以及
名字像密钥的字段（`api_key` / `token` / `secret` …）一律在读取配置时报错并退出码 3
—— 这些字段过去被静默忽略（引擎只读上面那三个），却会明文写进 `run.json` 的
`config_snapshot`。**API Key 只从环境变量读取**（见「模型绑定」一节），不要写进
`agentcrew.yaml`。

```yaml
max_concurrency: 4
context_budget_tokens: 24000
gate: both
roles_dir: roles.d

models:
  default: openai:gpt-4o-mini
  role_bindings:
    architect: anthropic:claude-sonnet-5
    qa_engineer: deepseek:deepseek-chat
  providers:                    # 覆盖内置能力位（逐位合并，未写的保持预设值）
    doubao:
      capabilities: { supports_json_schema: false, supports_json_object: true }
```

引擎级参数（如 `plan_max_attempts`，规划重拆上限，默认 2）当前只在代码层设置：
`Settings.load(plan_max_attempts=3)`。CLI 覆盖只写「命令行真的给了」的键，不会把
`--mock` 的 `False` 缺省反过来盖掉 `AGENTCREW_MOCK=true` 之类的环境变量。

---

## 产物说明

每次 `run` 在 `output/<run_id>/` 下建立独立命名空间，`run_id` 格式为
`YYYYMMDD-HHMMSS-<4位随机十六进制>`（本地时间）：

```
output/<run_id>/
├── run.json                 # RunRecord：目标、状态、时间、配置快照、统计
├── tasks.json               # TaskBoard 全量快照（每个任务的契约、评审记录、状态历史）
├── ledger.jsonl             # 追加式事件日志：llm_call / gate_result / human_decision …
├── context/
│   └── decisions.jsonl      # 上下文决策记录（返工意见等，供下游 Agent 引用）
├── review/                  # 预留目录（V1 的评审记录随 tasks.json 落盘）
├── diagnostics/
│   └── stall-report.md      # 仅停滞（死锁）时生成
└── artifacts/               # 真实交付物根目录（按契约路径分布）
    ├── README.md、docs/、backend/、frontend/、tests/、Dockerfile …
    ├── DELIVERY.md          # T-FINAL（project_manager）汇总的交付总结
    └── MANIFEST.json        # 产物清单
```

- `run.json`：`run_id`、`schema_version`、项目目标、`status`（`running` /
  `interrupted` / `completed` / `failed` / `cancelled`）、创建/更新时间、run 创建时
  捕获的配置快照（resume 用它判断结果可比性）、任务与 token/成本统计。
- `tasks.json`：任务 DAG 的唯一事实源快照；每次状态跃迁后落盘，配合原子写
  （临时文件 + `os.replace`）保证中断后可恢复。
- `ledger.jsonl`：逐行 JSON 事件，token 与成本审计的来源；`status <run_id>`
  输出的「llm_call N 次」即来自这里。
- `DELIVERY.md`：由引擎追加的收尾任务 `T-FINAL` 产出，依赖全部叶子任务，
  汇总完成清单与验收结论（契约要求含「完成」字样且不小于 100 字节）。
- `MANIFEST.json`：产物清单，随每次状态跃迁刷新、封版时最终写定；
  字段为 `run_id`、`generated_at` 与 `entries` 列表；每条 entry 含 `path`、
  `kind`、`size_bytes`、`sha256`、`produced_by_task`、`produced_by_role`、
  `written_at`。resume 时引擎从它重建产物记录，跨任务的路径冲突检测与返工前的
  清理都依赖这份清单。

---

## 运行测试

```bash
python -m pytest -q
```

- 测试套件**不需要网络、不需要 API Key**：模型调用要么由 mock provider 回放
  `examples/fixtures/` 中的夹具，要么被测试内的假 Provider 拦截；端到端测试在
  `tmp_path` 下跑完整流程（零网络）。
- 覆盖范围包括：状态机与非法跃迁、产物契约与解析、安全落盘、DAG 与调度、
  三层门禁、模型接入与结构化输出降级、配置合并、checkpoint 与 resume、
  CLI 行为、mock 夹具与 mock 无旁路，以及本文档的交付验收。
- 本机 `pytest` 裸命令不在 PATH 上，请统一用 `python -m pytest`；只跑 README
  验收可以指定 `python -m pytest tests/test_readme.py -v`。

---

## 已知限制

V1 的功能边界如下，请在使用前明确知悉。这些限制不是「以后再说」的注脚，
而是会影响你对产物的信任方式的硬约束：

1. **不执行生成的代码**。G1 只做静态校验（产物存在、大小、`.py` 可 `ast.parse()`、
   `.json` / `.yaml` / `.toml` 可解析、`must_contain` 符号齐全、HTML/CSS/JS 非纯
   占位符），**不运行产物测试、不启动服务**。因此产物「可运行」是由人工按
   「离线演示」一节的 uvicorn 步骤验收确认的，不是平台自动保证的。这既是安全边界
   （不在宿主上跑生成的代码），也是 V1 的复杂度边界。
2. **Token 估算为启发式**。上下文预算判定使用字符启发式估算 token 数，与真实
   tokenizer 存在偏差；成本统计以 API 返回的 usage 为准，未知模型按 0 计但照样
   记录 token 数。预算是软约束方向上的工程近似，不是精确计量。
3. **文心一言仅支持千帆 v2 的 API Key 模式**。旧版 `access_token` 鉴权不在支持范围；
   并且千帆 v2 不支持 `json_schema` / `json_object`，结构化输出走提示词约束 +
   围栏提取 + 修复循环（见「模型绑定」）。
4. **单进程**。调度器是单进程 asyncio，不支持多机分布式执行；并发上限由
   `max_concurrency` 控制。
5. **mock 产物不是模型生成的**。`--mock` 的产物来自 `examples/fixtures/` 中手写的
   预置回复，用于离线演示编排流程与支撑 hermetic 测试，**不代表模型能力**；
   真实生成能力需要 `--goal` + 真实 API Key 验证。

V2 新增能力的已知边界：

6. **HTTP API 为内部控制面设计**。手写 HTTP/1.1 解析，仅支持 GET/POST，无认证、
   无速率限制、无请求体大小上限（64KB 硬编码），不建议直接暴露公网。
7. **MCP 客户端仅支持 stdio 传输**。未实现 SSE/HTTP 传输、resources/prompts 能力、
   progress 通知；连接真实 MCP 服务器前建议先用 `list_tools()` 验证兼容性。
8. **Web UI 为只读看板**。不支持人工闸门交互（G3 仍需 CLI）、无产物预览、
   无任务详情面板；适合监控，不适合操作。

---

## 状态机与门禁

### 任务状态机（10 态）

任务状态只存在于 `TaskBoard`，跃迁表硬编码在 `agentcrew/task/state_machine.py`，
非法跃迁抛 `IllegalTransitionError`：

| 起始态 | 可跃迁到 | 触发条件 |
|---|---|---|
| `PENDING` | `READY` / `BLOCKED` / `SKIPPED` / `CANCELLED` | 依赖全部成功 / 任一依赖失败或取消 / 任一依赖被跳过 / 用户取消 |
| `READY` | `RUNNING` / `BLOCKED` / `CANCELLED` | 取得并发槽位并派发 / 派发前发现依赖已失败 / 用户取消 |
| `RUNNING` | `REVIEW` / `READY` / `FAILED` / `CANCELLED` | 正常返回且产物解析成功 / 执行异常且 `attempts < max_attempts`（重试）/ 异常且额度用尽 / 用户硬取消 |
| `REVIEW` | `SUCCEEDED` / `REWORK` / `FAILED` / `CANCELLED` | G1+G2+G3 全 approve / 任一门禁 reject 且 `rework_count < max_reworks` / 驳回且返工额度用尽 / 用户取消 |
| `REWORK` | `RUNNING` / `CANCELLED` | 再次派发（携带 `fix_hints`）/ 用户取消 |
| `BLOCKED` | `READY` | 每轮调度重算：全部依赖变为 `SUCCEEDED` |
| `SUCCEEDED` / `FAILED` / `CANCELLED` / `SKIPPED` | — | 终态（无出边） |

额度语义：`attempts` 是执行尝试次数（每次 `READY → RUNNING` 时 +1，派发即扣除，
所以「中断 → resume」也会消耗一次，杜绝无限循环）；`rework_count` 是返工次数
（每次 `REVIEW → REWORK` 时 +1）。默认 `max_attempts=3`、`max_reworks=2`，
两者独立计数，上限明确。`FAILED` 是终态，不是状态机边；人工重试走
`TaskBoard.reset_failed()` 的显式接口（交付卡点打回 `FAILED` 任务即用这条路径，
`SUCCEEDED` 任务被打回则记 `reason="delivery-rework"` 后置回 `READY`）。

### 三层门禁

`Gatekeeper.evaluate` 是任务离开 `REVIEW` 的唯一入口，三层顺序执行、任一层
reject 即短路：

- **G1 — 静态校验**（确定性，零 token）：契约中每个必需产物都已产出、未声明产物
  未出现、文件字节数达标、`.py` 通过 `ast.parse()`、`.json` / `.yaml` / `.toml`
  可解析、HTML/CSS/JS 非空且不含纯占位内容、`must_contain` 字面量齐全。G1 驳回
  直接短路 G2 —— 缺文件、语法错误不是判断题，一次评审调用都不发。
- **G2 — LLM 评审**（结构化输出，走降级链）：评审人由
  `RoleCatalog.reviewer_for(task.role)` 决定（`project_manager` 的任务由
  architect + product_manager 双评审，取都 approve）；评审输入为任务描述 +
  验收标准 + 产物内容（按 8000 tokens 的评审节选预算截断）+ 返工历史。`verdict=reject`
  时 `issues` 必须非空，否则视为评审格式错误重试（不计任务返工额度，最多 2 次；
  仍失败则按 approve 放行，并在该项评审的 `issues` 里写明格式错误，可追溯 ——
  宁可放过，也不因评审器故障卡死全流程）。评审的 token 消耗单独记入 ledger。
- **G3 — 人工闸门**（CLI）：由 `--gate` 控制两个卡点。`plan` 卡点在 PM 拆解通过
  校验、开始烧 token 以前展示任务 DAG，可选 `[a] 批准执行 / [e] 编辑后执行 /
  [s] 保存退出 / [q] 取消`；`delivery` 卡点在全部任务 `SUCCEEDED`、封版以前展示
  产物清单与统计，可选 `[a] 接受交付 / [r] 打回指定任务返工 / [q] 放弃`。
  `--gate none`（等价 `--auto`）跳过两层；当 stdin 不是终端（管道 / CI / 重定向）
  时，含人工卡点的模式会**显式降级为 `none`** 并在 stderr 说明，绝不「声称有人工
  卡点、实际全自动」。人工裁决作为 `ReviewRecord(layer="human")` 完整留痕。

每条门禁裁决都会写入任务历史（`board.record_review`），并落一条 `gate_result`
审计事件（含层、裁决、分数、问题列表），`status` 命令的「G1 拦截 / G2 驳回 /
人工打回」统计即来自这些事件。
