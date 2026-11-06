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

## 预设样例（串行 / 层级 / 混合并发）

仓库内置三个可直接运行的 Crew 样例，覆盖三种编排形状。全部**离线可跑**：
`--sample` 会自动开启 `--mock` 并挂上该样例的夹具，不需要任何 API Key。

| 样例 | 形状 | 任务数 | 图的最大并行宽度 |
| --- | --- | ---: | ---: |
| `todo` | 混合并发：多条支线并行后汇入收尾任务 | 8 + 1 | 4 |
| `linear` | 简单串行：严格一条链，每步等上一步 | 4 + 1 | 1 |
| `hierarchy` | 层级管理：两条实现路径并行，在用例层汇合 | 6 + 1 | 2 |

```bash
python main.py samples                          # 列出样例（--json 可脚本解析）
python main.py run --sample linear --no-live     # 简单串行
python main.py run --sample hierarchy --no-live  # 层级管理
python main.py run --sample todo --no-live       # 混合并发（等价于 --demo）
```

每个样例是一套独立夹具，互不干扰：

```
examples/samples/<name>/
├── plan_todo.json              # PM 拆解的计划（任务 / 角色 / 交付物契约 / 依赖）
├── reviews.json                # G2 评审回放：按 task_id 给逐条判定
└── responses/<task_id>/attempt-1.md   # G1 执行回放：围栏里的产物正文
```

加一个样例只需在 `examples/samples/__init__.py` 的 `SAMPLES` 里登记一行、放好
上述三样东西，`samples` / `--sample` / 测试会自动带上它。
`tests/test_samples.py` 对三个样例各跑一次真实 run，断言**编排形状**（串行每层
只有一个任务、层级必须有分叉与汇合）、依赖关系与跑出来的完全一致、以及产物
在盘上且 sha256 对得上 —— 形状退化会直接红。

---

## 命令速查

常用子命令（`python main.py --help` 查看完整帮助；直接运行 `python main.py` 不带
子命令时，打印帮助、已注册角色数与最近 3 次 run 概览）：

| 子命令 | 用途 | 常用参数 |
|---|---|---|
| `python main.py run` | 执行一次完整 run（规划 → 执行 → 交付） | `--goal TEXT` 与 `--demo` 二选一；`--mock`；`--auto`；`--gate both\|plan\|delivery\|none`；`--max-concurrency N`；`--default-model ALIAS`；`--output-dir DIR`；`--context-budget N`；`--no-live` |
| `python main.py resume <run_id>` | 续跑一个中断或未完成的 run | `--force`（配置快照不一致时仍继续）；以及 `run` 的全部配置参数 |
| `python main.py status <run_id>` | 打印 run 记录、统计、任务看板与待审批条数 | `--output-dir DIR` |
| `python main.py approvals <run_id>` | R4：列出该 run 的持久化人工审批单 | `--output-dir DIR` |
| `python main.py approve <run_id> <approval_id>` | R4：在另一个进程里写裁决（之后 `resume` 继续） | `--decision ACTION`；`--task-ids ID...`（rework）；`--by NAME`；`--output-dir DIR` |
| `python main.py roles` | 列出 8 个内置角色的职责 / 边界 / 产出 | 无 |
| `python main.py clean` | 保留最近 N 个 run，删除更旧的（默认 N=5） | `--keep N`；`--dry-run`（只列不删）；`--output-dir DIR` |
| `python main.py cancel <run_id>` | 给离线 run 打 cancelled 标记（下次 resume 直接收尾） | `--output-dir DIR` |
| `python main.py serve` | 启动 HTTP API + Web UI 面板（V2） | `--host HOST`（默认 127.0.0.1）；`--port PORT`（默认 8765） |
| `python main.py doctor` | 诊断配置 / 环境 / 依赖 / Provider 连通性，输出健康报告 | `--config PATH`（指定 agentcrew.yaml）；`--output-dir DIR`；`--json`（机器可读） |
| `python main.py diff <a> <b>` | 对比两个 run 的配置 / 任务 / 产物 / 事件统计差异 | `--output-dir DIR`；`--json`（机器可读） |

行为说明：

- `--auto` 等价于 `--gate none`；`--auto` 与 `--gate` 同时给出时 `--auto` 胜出。
- `resume` 会先比对 run 创建时的配置快照与当前配置：不一致（例如换了默认模型、
  或用非 mock 参数续跑一个 mock run）会打印差异并以退出码 3 拒绝，确认无误后加
  `--force`。`Ctrl-C` 中断的 run 会落盘为 `INTERRUPTED` 并提示 `resume <run_id>`；
  `RUNNING` 的任务在 resume 时回到 `READY` 重新执行（产物可能只写了一半），
  `REWORK` 的任务保持原状、由调度器直接重新派发，`REVIEW` 的任务保持原状只重跑门禁；
  `attempts` / `rework_count` 一律不重置。
- R4：`--approval-mode deferred` 的 run 会在人工卡点落成 `awaiting_approval` 并正常
  退出（退出码 0）；用 `approvals` / `approve` 裁决后 `resume` 回到**原卡点**继续。
  这与 `INTERRUPTED` 的 resume（直接重入执行、跳过规划与计划卡点）是两条不同路径。
  审批挂起的 run 受 `clean --protect-running` 与 `delete_run` 保护。
- `clean` 的删除范围被限制在 `--output-dir` 之下，`--dry-run` 与真删共用同一套
  边界校验。
- `doctor` 打印一份健康报告：Python 版本、核心依赖可导入性、已配置 provider 的
  API Key、`default_model` 别名可解析、`output_dir` 可写，以及对每个已配置别名的
  Provider 连通性探测（复用 P2-11 的 healthy / degraded / unavailable 三级结论）。
  `--json` 输出机器可读的 JSON。退出码：全部通过或仅有警告 → 0；任何一项失败 → 3
  （警告不阻断，因为「能用但不理想」不该挡住跑 run）。
- `diff <run_a> <run_b>` 对比两个 run：配置快照关键字段、任务数量与状态分布、
  产物（按路径与字节数判 added / removed / modified）、ledger 事件计数与 LLM
  用量、最终状态与汇总统计。只读，不改动任何 run。`--json` 输出结构化差异。
  退出码：两个 run 都存在 → 0（差异大小不影响退出码，diff 是信息性命令）；任一
  run 不存在 → 1。

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
| `/runs` | GET | 列出全部 run（盘上记录 + 运行中实时状态，按创建时间倒序） |
| `/runs` | POST | 创建并后台启动 run（body: `{"goal": "...", "options": {...}}`，202 返回 run_id） |
| `/runs/{id}/cancel` | POST | 软取消运行中的 run（202；在跑任务收尾后以 cancelled 收尾，退出码 130） |
| `/runs/{id}/resume` | POST | 从 checkpoint 恢复 run 并后台执行（202） |
| `/runs/{id}/status` | GET | 运行状态与任务看板 |
| `/runs/{id}/ledger` | GET | Ledger 事件分页查询（`?event=&limit=&offset=`，P2-9） |
| `/runs/{id}/stats` | GET | 事件分布 / LLM 用量 / 任务成功率与平均时长 / 工具调用（P2-9） |
| `/runs/{id}/model` | GET | 当前模型绑定（仅运行中的 run） |
| `/runs/{id}/model` | PUT | 切换模型（body `{"alias": ..., "role": ...}`） |
| `/runs/{id}/events` | GET | SSE 事件流（实时推送） |
| `/runs/{id}/events` | POST | 注入事件（调试用） |

P1-5 起 `POST /runs` 经 `crew_factory` 装配 Crew、挂上 `on_event` 后用
`asyncio.create_task` 后台执行；`run_started` 事件一到就把 `run_id` 回给 HTTP
响应并登记进内存注册表，任务结束自动摘除。取消为软取消：不再派发新任务，
等在跑任务收尾后以 `cancelled` 封版。

#### 可选：API Key 认证与速率限制（E-3）

默认**关闭**：不传 `auth` / `rate_limiter` 时，所有端点匿名可访问，行为与改造前
完全一致。开启方式（`serve` 子命令按配置自动装配）：

```bash
# 方式一：环境变量（推荐，key 不落盘）
export AGENTCREW_API_AUTH_ENABLED=true
export AGENTCREW_API_KEYS="key-alpha,key-beta"
export AGENTCREW_API_RATE_LIMIT_ENABLED=true
export AGENTCREW_API_RATE_LIMIT_MAX=100     # 每 IP 每分钟 100 次
python main.py serve

# 方式二：--api-key 快速开单个 key
python main.py serve --api-key dev-key-123
```

- **认证**：请求须带 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`；缺失或
  无效返回 `401 Unauthorized`（带 `WWW-Authenticate` 头）。
- **限流**：按客户端 IP 的滑动窗口计数（取 `X-Forwarded-For` 首地址，否则取
  socket peer）；超限返回 `429 Too Many Requests`（带 `Retry-After` 头）。
- **诚实留痕**：认证失败记 `api_auth_failed`、限流记 `api_rate_limited` 事件
  （含 `client_ip` / `path`，限流事件含 `remaining`）。Key 值只参与布尔比对，
  **绝不**写进日志或审计记录。

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

> **R3 更新**：以上示例描述的是**库层**能力。R3 之前 `ToolRegistry` 从未在真实
> run 中被构造，因此 `tool_permissions` 是死配置；现在可用
> `AGENTCREW_TOOLS_ENABLED=true` 让 `Crew` 自动装配 6 个内置工具并下发角色权限，
> 也可用 `Crew(..., tool_registry=registry)` 显式注入。详见「R3 能力完善」。

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

### P1-5. 创建 run 的 REST API（create / cancel / resume / list）

`ApiServer` 不再只能查状态/推 SSE，可直接创建并后台跑 run：

- `POST /runs`（body `{"goal": ..., "options": {...}}`）→ 202，返回 `run_id`；
  经 `crew_factory` 装配 Crew、挂 `on_event` 后 `asyncio.create_task` 后台执行，
  `run_started` 事件一到就把 `run_id` 回给 HTTP 响应
- `POST /runs/{id}/cancel` → 202 软取消：不再派发新任务，在跑任务收尾后以
  `cancelled` 封版（退出码 130，复用既有 `EXIT_CODES`）
- `POST /runs/{id}/resume` → 202 从 checkpoint 恢复并后台执行
- `GET /runs` → 盘上 `run.json` 列表 + 内存中运行中状态叠加，按创建时间倒序

新参数（`settings` / `crew_factory`）均有默认值，老调用方行为不变。

### P1-6. 运行时切换大模型选项

跑起来后可切默认模型或角色绑定，不必重启 run：

- `ModelFactory.set_default_alias()` / `set_role_binding()`：用 `model_copy` 换
  frozen Settings、清空 provider 缓存（旧实例绑定了旧配置）、记 `llm_model_switched`
  ledger 事件；`current_bindings()` 返回当前绑定
- `Crew.switch_model(alias, role=None)`：委托工厂切换、把新 `config_snapshot`
  回写 `run.json`、记 `model_switched` 事件
- HTTP：`GET /runs/{id}/model` 读绑定；`PUT /runs/{id}/model`
  （body `{"alias": ..., "role": ...}`）切换，仅对运行中 run 开放（非运行中 409）
- CLI 计划/交付卡点菜单新增 `[m] 切换模型`（注入 `crew` 才显示，向后兼容）
- Dashboard 顶部新增模型选择器，选别名+可选角色即调 `PUT` 接口

### P2-3. 评审意见去重与收敛检测

G2 评审意见此前不做跨评审人 / 跨轮次的语义去重，且「评审员每轮都能再挑出
毛病」会让返工永不收敛。本项做两件事（均向后兼容，未配置时不改变既有行为）：

- **意见去重**：`Reviewer.deduplicate_issues()` 在 `review()` 返回前对 issues 做
  两级合并——先规范化（小写 / 去标点 / 压缩空白）后精确比较，再按 token 集合
  Jaccard 相似度 > 0.8 合并近重复意见。去重时记 `review_dedup` ledger 事件
  （`original_count` / `deduped_count` / `removed_issues`）。
- **收敛检测放行**：`Gatekeeper(convergence_threshold=N=2)` 跟踪每个任务历次
  LLM 驳回的规范化 issue 集合指纹；连续 N 轮意见完全不变即判收敛——说明返工
  再多次也只会撞同一堵墙。收敛后若 verdict 仍是 `reject`，改为放行
  （SUCCEEDED），记 `review_converged` 事件（`task_id` / `rounds` / `issues`），
  并补一条 `gate_result`：verdict 标 `approve`、issues 保留、reviewer 追加
  `:converged`。仅对 LLM 层生效（G1 结构性损坏不参与），且不消耗返工额度。
- **向后兼容**：默认 `None`（关闭）——生产路径不传 gate_config 时，驳回循环仍走
  旧的 FAILED 结局；传 `N`（或 `gate_config.convergence_threshold=N`）启用，`0`
  也关闭。

### P2-4. G1 扩展代码可执行性校验

G1 原本只做契约校验（缺产物 / 未声明 / 占位 / must_contain）。本项在 G1 既有检查
之后追加一层**代码可执行性校验**，分 error / warning 两级：

- **新增检查**（`static_checks.py`）：`check_python_syntax()` 用 `ast.parse` 查
  Python 语法；`check_imports()` 遍历 AST 找危险模块导入（`os` / `sys` /
  `subprocess` / `socket` / `ctypes` 等）；`check_file_nonempty()` 查空文件；
  `run_extended_checks(artifacts, kind)` 按 kind 分流——只对 `kind="python"` 跑
  语法 + 危险导入，其余 kind 只做非空检查。
- **分级**：语法错误、空文件进 `errors`（**阻断**，并入 G1 issues 使 G1 不通过）；
  危险模块导入进 `warnings`（**不阻断**，只留痕——G1 不在宿主上跑代码，用得对不
  对交 G2 / 人工）。
- **留痕**：Gatekeeper 在 G1 后记 `g1_extended_check` 事件（`errors` /
  `warnings`）。全程不抛异常，畸形产物以中文消息回灌返工方。
- **向后兼容**：默认启用，但只对 python 产物做语法检查，不影响非代码产物。

### P2-5. 门禁配置化

门禁规则此前硬编码在 `Gatekeeper` 里。本项把它们收进 `Settings.gate_config`，
从 YAML `gate_config:` 段读取，缺省回落硬编码默认值：

- **配置键**（`gate_config: dict`）：`g1_enabled` / `g2_enabled` / `g3_enabled`
  （三层开关）、`g2_reviewers`（并行评审人数，取目录前 N 个评审人）、
  `convergence_threshold`（P2-3 收敛阈值）、`review_score_threshold`（P0-1）、
  `max_review_attempts`。
- **生效方式**：`Gatekeeper(..., gate_config={...})` 读取各键；`g1_enabled=False`
  时跳过 G1 静态校验（记一条 `static:disabled` 放行记录），`g2_enabled=False` 时
  跳过 LLM 评审，`g2_reviewers` 写到 `Reviewer.max_reviewers` 限制评审人数，
  `convergence_threshold` / `review_score_threshold` 覆盖构造参数。
- **落盘**：`gate_config` 加入 `_MAPPING_FIELDS`（分层按 key 合并），并进
  `config_snapshot`。
- **向后兼容**：`gate_config=None`（或空 dict）时行为与原来完全一致。

### P2-1. 运行时动态子任务

规划阶段任务固定，执行阶段 Agent 可以**在执行中**请求拆出独立子任务，子任务
立即入板并由调度器在下一轮派发，无需等规划重来。

- **触发方式**：Agent 在回复里用一对 `<subtask>` 围栏发出一个 JSON 对象
  `{"title": ..., "description": ..., "role": ..., "deliverables": [...]}`；
  `BaseAgent.execute()` 检测到后经回调交给调度器构造新任务。
- **入板即就绪**：新任务直接以 `READY` 入板（`TaskBoard.add_task()`），无依赖、
  独立调度；父任务继续执行自己的交付物，不等待子任务。
- **字段**：`Task.dynamic=True` 标记运行时创建（vs 规划阶段静态），`parent_id`
  指向请求它的父任务；`TaskBoard.children_of(parent_id)` 查询某任务的全部子任务。
- **诚实留痕**：每次创建记 ledger `subtask_created`（含 parent_id / child_id /
  title）；坏 JSON 的子任务请求记 `subtask_malformed` 后跳过，不拖垮本任务。
- **向后兼容**：Agent 未注入 `subtask_creator` 时完全不检测、系统提示词也不提
  子任务能力，行为与之前一致。

### P2-2. 超时停滞检测

调度主循环原本裸 `await queue.get()`：worker 僵在某个永不返回的 await 上时，
run 会永远挂住。本项补上**运行时停滞检测**——有任务在跑却长时间没有任何状态
跃迁，就判停滞、硬取消在跑任务、以退出码 4 优雅收尾。

- **判据**：`Scheduler` 记 `_last_progress_at`（monotonic 时钟），每次派发 /
  完成 / 状态跃迁都刷新；主循环把阻塞等待改成 `asyncio.wait_for` 分段等，每
  秒醒一次检查。**仍有在跑 worker** 且距上次进展超过 `stall_timeout_seconds`
  （默认 300 秒）→ 判停滞。
- **与结构性死锁互补**：既有的「无就绪、无在跑」死锁检测（退出码 4）仍在；
  本项补的是「有 worker 在跑但卡死」这一类——两者都映射到 `stalled` 退出码 4。
- **留痕**：停滞时记 ledger `run_stalled`（含 `stalled_for_seconds` 与当时
  `active_tasks`），Crew 再补一条 run 级 `run_stalled` 并落
  `diagnostics/stall-report.md`。
- **向后兼容**：`stall_timeout_seconds` 默认 300；正常长任务只要在阈值内有过
  任意一次完成/跃迁就不会误触。

### P2-8. Agent 自我修正循环

Agent 执行失败（输出格式 / 契约校验不通过）时，不再直接抛错让调度器重试——
而是在**同一次 `execute()` 内**把错误回灌给模型，让它自己改对。

- **流程**：parse 或 validate 失败 → 把错误信息 + 上一条 assistant 回复追加进
  messages → 重调一次 LLM → 再 parse/validate，循环直到成功或用尽次数。
- **额度**：`max_self_corrections`（默认 2）次自我修正；仍失败才抛
  `AgentExecutionError`，由调度器按 attempts 重试。
- **留痕与记账**：每次修正记 ledger `agent_self_correction`（含 task_id / role /
  attempt / error）；重调走 `models.call()`，token / 成本照记。
- **边界**：仅作用于 parse/validate 失败；Provider 异常（网络 / 鉴权）原样上抛，
  不自我修正。
- **向后兼容**：`max_self_corrections=0` 时首次失败即抛错，与原行为完全一致。

### P2-9. Ledger 查询与分析 API

Ledger 此前只被终端看板 / 状态命令消费，现在 REST 也能直接读：

- `GET /runs/{id}/ledger?event=&limit=&offset=` — 分页查询 `output/<run_id>/ledger.jsonl`
  中的事件，返回 `{"events": [...], "total": <过滤后总数>}`；`event` 按事件名过滤，
  `limit`（默认 100）/ `offset` 分页
- `GET /runs/{id}/stats` — 统计分析：`event_counts` 各事件类型计数；`llm` 从
  `llm_call` 事件聚合调用数 / prompt / completion / 总 token / 估算成本（USD）；
  `tasks` 为 `task_finished / task_started` 成功率（无 `task_started` 时为 `null`，
  不拿 0/0 冒充 100%）与 `task_finished.duration_ms` 的平均时长；`tools` 为
  `tool_call` 事件的总数 / 成功 / 失败
- 读取侧完全复用 `Ledger.events()` 的宽容规则：文件不存在 / 坏行 / 空行都不炸；
  run 目录不存在返回 404，run 存在但从未 emit 返回 200 空列表——查询路径只读不写，
  绝不改动审计日志
- 实现：`agentcrew/runtime/ledger.py::LedgerReader`（只读查询器，run_id 路径越界
  一律拒绝），`agentcrew/api/server.py::_get_ledger` / `_get_stats`

### P2-10. Web UI 操作化

Dashboard 从纯查看升级为可操作（同一服务器提供页面与 API，天然同域）：

- **运行列表**：页面加载即 `GET /runs` 拉取全部 run 并每 5 秒自动刷新，卡片展示
  目标、状态徽章与操作按钮
- **取消 / 删除**：运行中的 run 卡片显示「取消」（`POST /runs/{id}/cancel`，确认后
  执行）；非运行状态显示「删除」（`DELETE /runs/{id}?confirm=1`，前端两次确认、
  API 强制 `?confirm=1`，running 状态服务端 409 拒绝）；操作后自动刷新列表
- **模型切换**：顶部模型选择器（P1-6）在连接任意 run 后即可用，调
  `PUT /runs/{id}/model`
- **事件日志标签页**：调 `GET /runs/{id}/ledger` 展示事件列表，支持按事件类型
  过滤（如 `llm_call`），连接 run 时自动加载
- **统计标签页**：调 `GET /runs/{id}/stats` 展示 LLM 调用数 / token / 估算成本 /
  任务完成率与平均时长 / 工具调用成败 / 事件分布
- 错误处理：所有 API 调用失败时弹出错误提示，不影响看板其余部分；看板、实时
  SSE 事件流等原有查看功能保持不变

### P2-6. 计划质量评分

PM 拆解通过 §7.2 二元校验后，再由 `score_plan()` 做一次**多维度质量评分**，
低分只警告、不阻断执行（校验已放行，重拆额度也已用尽，再拦只会把能用的计划
退成 `PlanFailedError`）。

- `PlanScore` 四个维度（都归一到 0–1）：`coverage`（goal 关键词在任务正文的
  覆盖率，权重 0.3）、`specificity`（平均描述长度 + 平均验收标准数，权重 0.2）、
  `dependency_quality`（有环 = 0；无环且有依赖边 = 1；散点计划 = 0.5，权重 0.2）、
  `deliverable_clarity`（有交付物任务比例 + 必需产物比例 + 路径规范比例，权重 0.3）
- 只评**真实任务**：平台追加的 `T-FINAL` 永远有交付物与依赖，算进去会把任何
  计划的清晰度抬高
- 每次规划成功后落 ledger 事件 `plan_scored`（含 total 与四维分值）；
  `total < MIN_PLAN_SCORE`（默认 0.5）时再落一条 `plan_low_quality` 警告
- 评分是观测信号、不抛异常：非法拆解早在 `validate_plan` 就被拦下

### P2-7. 增量计划编辑

计划卡点的 `[e] 编辑` 不再只能全量重写 JSON：进入编辑子菜单后可对**单个任务**做
增量增 / 删 / 改，每次操作后立即重跑 §7.2 校验，失败则打印错误并丢弃本次操作。

- 子菜单：`[a] 添加任务`（提示 id / 标题 / 描述 / 角色 / 验收标准 / 交付物路径，
  插到 `T-FINAL` 之前）/ `[d] 删除任务`（按 id 删除并自动清理其他任务对它的依赖）
  / `[m] 修改任务`（按 id 改 title / description / role / acceptance_criteria /
  deliverables）/ `[e] 全量编辑器`（原有行为，不变）/ `[b] 完成提交` / `[q] 取消`
- planner 层四个纯函数均返回**新列表**、不修改输入：`add_task_to_plan()` /
  `remove_task_from_plan()` / `modify_task_in_plan()` / `validate_incremental_edit()`
  （后者不抛异常，把结构错误收成列表返回）
- 每次成功的增量操作都经 `PlanDecision.incremental_edits` 带回 Crew，逐条落
  ledger 事件 `plan_incremental_edit`（含 action / task_id / changes）；
  全量编辑器记一条 `action=full_editor`
- **向后兼容**：未走子菜单（老的全量编辑器路径）行为完全不变；`incremental_edits`
  有默认空列表，旧的 `PlanDecision` 构造方式不受影响

### P2-11. Provider 主动健康监控

后台定期探测所有已缓存 Provider 的健康状态，failover 时优先跳过不健康的别名，
不浪费调用在已知不可用的 Provider 上。

- **健康状态三级**：`healthy`（正常，延迟 < 2s）/ `degraded`（正常但慢，>= 2s）/
  `unavailable`（异常或超时）
- **探测方式**：对每个已缓存别名发一条 "ping" 消息（max_tokens=1），5 秒超时；
  MockProvider 直接标记 healthy（不真实调用）
- **后台循环**：每隔 `health_check_interval_secs`（默认 30s）自动探测所有已缓存别名；
  `start_health_monitoring()` / `stop_health_monitoring()` 控制启停
- **failover 感知**：`_failover_aliases()` 自动将 `unavailable` 的别名排到列表末尾
  （优先尝试健康的）；`degraded` 的别名保持原位；全部不可用时保持原序（最后一搏）
- **诚实留痕**：每次探测记 `provider_health` ledger 事件（含 alias / status /
  latency_ms / error / consecutive_failures）；failover 事件追加 `from_status` /
  `to_status`
- `get_health(alias)` 查询某别名的最新健康状态

```bash
# .env
AGENTCREW_HEALTH_CHECK_ENABLED=true
AGENTCREW_HEALTH_CHECK_INTERVAL=30
```

默认启用（`true`），间隔 30 秒。设 `AGENTCREW_HEALTH_CHECK_ENABLED=false` 关闭，
行为与改动前完全一致。

### P3-10. 权重优先级路由

`model_failover` 支持权重配置：按权重随机选择 Provider（而非固定顺序），失败时
按剩余权重重选，并结合健康状态自动调整权重。

- **两种格式**（向后兼容）：
  - 旧格式（纯字符串列表）：按顺序故障转移（P0-2 行为不变）
  - 新格式（权重列表）：按权重随机选择
- **权重调整**（结合 P2-11 健康状态）：`unavailable` 的别名权重置 0（不被选中）；
  `degraded` 的别名权重减半；所有权重为 0 时回退到第一个别名
- **失败重选**：某个别名失败后从可用池中按剩余权重重选，全部失败时抛
  `ProviderUnavailableError`
- **诚实留痕**：每次路由选择记 `llm_routed` ledger 事件（含 selected_alias /
  reason / candidates / weights）；reason 为 `weighted` / `health_adjusted` /
  `fallback_first`

```yaml
# agentcrew.yaml
model_failover:
  openai:gpt-4o:
    - alias: deepseek:v3
      weight: 0.6
    - alias: anthropic:claude
      weight: 0.4
```

旧格式（纯字符串列表）行为完全不变；权重格式通过 YAML 配置，环境变量
`AGENTCREW_FAILOVER_*` 仍只产生旧格式。

### P3-1. 复杂度动态校准

静态复杂度评估（V2 第 2 项）是纯启发式的，不知道「这类任务实际跑下来是不是被
低估/高估」。P3-1 在静态评估之上加一层**运行时校准**：按任务特征（角色 +
描述长度档 + 交付物数量）分桶累计实际成功率与平均尝试次数，据此把模型分级
上调 / 下调一级，使模型选择更贴合真实难度。

- **校准规则**（有历史样本才生效）：
  - 同类任务成功率 < 50% **或** 平均尝试次数 > 1.5 → **上调**一级
    （静态低估了难度，换更强模型）；
  - 同类任务成功率 > 90% **且** 平均尝试次数恰为 1 → **下调**一级
    （静态高估了难度，换更便宜模型）；
  - 其余情况或无历史 → 原样使用静态评估结果。级别在 LOW/MEDIUM/HIGH 两端夹断。
- **数据来源**：`Scheduler` 在任务走到终局（SUCCEEDED / FAILED）时把实际结局
  喂给全局 `ComplexityCalibrator`；待重试 / 待返工的中间态不记录，避免一次
  重试污染同类任务的统计。
- **诚实留痕**：校准改变了级别时记 `complexity_calibrated` ledger 事件（含
  task_id / original_level / calibrated_level / reason=upgraded|downgraded）。
- **向后兼容**：全局校准器未注入时 `alias_for_task` 行为与改动前完全一致；
  `ComplexityCalibrator.stats()` 可导出各桶统计用于调试。

### P3-2. LLM 响应缓存

对相同的 LLM 调用（相同 alias + messages + 采样参数）缓存响应，避免重复调用
浪费 token。只有 **temperature == 0**（确定性输出）的调用才缓存；temperature > 0
的调用结果非确定，一律不缓存。

- **缓存键**：alias / messages / temperature / max_tokens 的 SHA-256 摘要；
  `LLMCache` 内存缓存，容量上限默认 1000 条，超限时按 LRU 淘汰最旧条目。
- **命中路径**：直接返回缓存结果（标记 `from_cache=True`），不消耗 token /
  成本、不触发预算检查；记 `llm_cache_hit` 事件（含 alias、键摘要），并落一条
  usage 为 0、`cached=True` 的 `llm_call` 便于审计。
- **向后兼容**：`ModelFactory(cache=None)`（默认）时行为与改动前完全一致；
  开关 `AGENTCREW_LLM_CACHE_ENABLED` 默认关闭，避免意外改变既有行为。

```bash
# .env（默认 false）
AGENTCREW_LLM_CACHE_ENABLED=true
```

### P3-3. LLM 流式输出

长输出时不再等整段生成完才看到进度：Agent 可切到流式路径，按增量块（SSE/chunk）
边生成边收集，并实时留痕。

- **接口分层**：`BaseProvider.stream_complete()` 逐块 yield `StreamChunk(delta,
  finish_reason)`；默认实现退化为一次性 `complete()` 后吐一个完整块，未覆写的
  Provider 仍可工作（只是没有实时性）。`MockProvider` 覆写为按固定长度分块，
  拼接所有块必然等于 `complete()` 的文本。
- **工厂入口**：`ModelFactory.stream_call()` 与 `call()` 同形，只是走流式路径；
  信号量同样只包住流消费。**流式不做故障转移**（已吐出的块无法回滚），Provider
  不可用直接上抛。流完整消费后把全部 `delta` 拼成全文，按同口径估算 token 并
  复用 `_account` 落一条 `llm_call`（token/成本/预算检查与非流式一致）。
- **Agent 开关**：构造 `BaseAgent(..., stream=True)` 即启用；流式路径逐块收集，
  每块记一条 `agent_stream_chunk` ledger 事件（含 `task_id` / `role` /
  `delta_length`），收集齐全文后仍走原有的 parse / validate / commit 流程。
- **向后兼容**：`stream=False`（默认）时完全走 `models.call`，行为与改造前
  逐字节一致，也不产生任何 `agent_stream_chunk` 事件。

```python
from agentcrew.agents.generic import GenericAgent

agent = GenericAgent(
    spec, models=models, store=store, ledger=ledger,
    project_goal="目标", stream=True,   # 流式；省略则为默认的非流式
)
```

### P3-5. 成功经验沉淀

run 以 `COMPLETED` 收尾后，引擎自动把每个成功任务的「任务描述 → 解决方案 / 关键
决策 / 产物」映射沉淀进**全局**经验库 `output/knowledge/experiences.jsonl`（JSONL，
每行一条 `Experience`）。它不在任何单个 run 目录之下，因此 run 被 `clean` 删掉后
经验依然保留，供后续 run 复用。

- **只收成功任务**：`SUCCEEDED` 的任务才进库；失败 / 取消 / 跳过的任务不沉淀
  （它们的「解决方案」不成立）。`solution_summary` 取 Agent 自述 summary（截断到
  200 字），`artifacts` 取该任务实际落盘的产物路径，`key_decisions` 取 ContextBus
  里与该任务相关的决策记录。
- **诚实留痕**：每沉淀一条经验，ledger 记一条 `experience_captured` 事件
  （含 `task_id` / `experience_id` / `role`）。
- **开关**：构造 `Crew(settings, capture_experiences=False)` 可关闭（默认开启），
  关闭时不写经验库、也不发任何事件。

```python
from agentcrew.knowledge.experience import ExperienceStore

store = ExperienceStore(settings.output_dir)
for exp in store.all():
    print(exp.task_description, "→", exp.solution_summary)
```

### P3-7. 跨 run 知识复用

新 run 进入规划阶段时，引擎从全局经验库检索与项目目标相关的历史成功经验
（朴素关键词匹配），渲染成「历史经验」参考块追加到 PM 的拆解 user 消息末尾。
PM 因此在拆第一版计划时就知道「上次类似任务是怎么做成的」，而不是每次从零开始。

- **注入点**：仅规划阶段（PM 拆解）。历史经验是**参考材料**而非新约束——PM 仍须
  遵守全部拆解规则与输出契约。
- **诚实留痕**：检索到至少一条经验时，ledger 记一条 `knowledge_reused` 事件
  （含 `goal` 与 `matched_experiences` 条数）。
- **向后兼容**：`reuse_knowledge=False`（默认开启）或经验库为空 / 零命中时不注入
  任何内容，PM 提示词与改造前逐字一致。

```python
# 关掉复用（行为与引入本功能前完全一致）
Crew(settings, reuse_knowledge=False)
```

### P3-8. 增量 Checkpoint

此前每次任务状态变化都全量重写 `tasks.json`——大 run（几十上百任务）时一次写盘
要序列化整板，既慢又磨损磁盘。P3-8 增加增量保存：任务变化只把**这一个任务**的
最新状态追加到 `tasks.delta.jsonl`（一行 JSONL），远小于整板。

- **增量格式**：每行 `{"op": "update", "task_id": "T-1", "changes": {…任务完整 dict…}}`；
  也兼容 `{"op": "add", "task": {…}}`。`changes` 存任务完整快照，apply 即整体替换，
  不做字段级 diff。
- **自动合并（compaction）**：delta 累计到 `DELTA_COMPACTION_THRESHOLD`（默认 50）
  行时自动合并回 `tasks.json` 并清空 delta 文件；`apply_deltas()` 也可手动触发。
- **resume 自动应用**：`load_board()` 检测到 delta 文件存在时，先叠加增量再建面板，
  无需调用方区分；`load_with_deltas()` 额外返回 `{task_id: Task}` 供程序化读取。
- **向后兼容**：原有全量 `save_tasks()` / `load_board()` 行为不变；没有 delta 文件
  时一切与改造前逐字一致。增量是可选优化，调度器不接线时默认仍走全量保存。

```python
store.save_task_delta(task)   # 只追加一行增量
store.apply_deltas()          # 手动合并回 tasks.json
```

---

### P3-6. 产物正文智能摘要

此前任务摘要只取 `notes` 前 400 字符，下游上下文包看到的要么是模型随口的说明、
要么只是一串文件名。`BaseAgent.summarize()` 在产出了文件时，改为对**产物正文**
做结构性摘要（`agentcrew/artifacts/summarizer.py`，纯函数、不调用 LLM）：

- `markdown`：提取标题层级（`#`/`##`/`###`）与首段；
- `python`：提取 `def`/`class` 定义行与模块 docstring；
- `json`/`yaml`/`toml`：解析后提取顶层键与值类型；
- `html`/`css`/`js`：提取 `<title>`/`<h1>`/id、顶层选择器、函数名；
- 其他类型：前 N 行 + 总行数 / 字符数统计。

输出形如 `[python 摘要] src/app.py: 定义：类App、函数run；说明：...`。产物摘要
在前、`notes` 在后合并（整体上限 1500 字符）；**无产物时完全回落到旧的 notes
截断逻辑**，向后兼容。批量摘要由 `summarize_records(records, store)` 拼接。

### P3-9. MCP SSE/HTTP 传输 + 工具权限管控

MCP 客户端原本只支持 stdio 子进程传输。`agentcrew/mcp/transports.py` 把 JSON-RPC
消息收发抽象成 `MCPTransport` 接口（`connect`/`send`/`receive`/`close`），内置三种
实现，零第三方依赖（HTTP 基于 `asyncio.open_connection` 手写最小 HTTP/1.1，仅
`http://`）：

- `StdioTransport`：从客户端抽出的子进程 stdin/stdout（旧行为不变）；
- `SSETransport`：GET 长连接读服务器推送（`endpoint` 事件给出 POST 地址，
  `data:` 事件即 JSON-RPC 消息），POST 发请求（简化：单次连接、不自动重连）；
- `HTTPTransport`：纯 HTTP 请求-响应，每次 send 即一次 POST，响应体进接收队列。

`MCPClient` 构造接受 `transport=` 注入（旧 `MCPClient([cmd])` API 不变），并新增
`MCPClient.from_config(server_config)`：按 `transport` 字段（`stdio`/`sse`/`http`，
默认 stdio）选通道——stdio 需 `command`，sse/http 需 `url`（可选 `headers`）。传输层
IO / 协议帧错误统一抛 `MCPTransportError`。

同时在 `ToolRegistry` 增加**角色级工具权限白名单**：

- `set_role_permissions(role, [工具名...])` 设置白名单；`["*"]` 或空列表 = 全部允许；
- `is_allowed(role, tool)` / `filter_by_role(role, tools)` 判断与过滤；
- `enforce(role, tool)` 是执行期闸门：被拒时记 `tool_permission_denied` ledger 事件
  （含 `role`、`tool_name`）并返回 `False`，**被拒工具绝不真正执行**。

未配置任何角色权限时所有角色可用全部工具（向后兼容）。配置经
`Settings.tool_permissions: {角色: [工具名...]}`（YAML `tool_permissions:` 段）
加载；`mcp_servers` 各项可带 `transport` 字段。

### P3-4. 多轮 Agent 对话

Agent 在执行过程中可以向其他角色（或 "human"）发问、拿到回复后再继续，而不再
只能靠上下文包里的静态摘要。

- **对话围栏**：模型在回复里写 `<message to="角色名">问题</message>`。Agent
  检测到后调用注入的 `message_router(目标角色, 问题) -> 回复`，把回复以
  `<message_reply from="角色名">…</message_reply>` 作为一条 user 消息回灌，再问
  一次 LLM，直到模型不再发消息围栏。
- **留痕**：每发一条消息记 `agent_message`（含 `task_id` / `from_role` /
  `to_role` / 问题摘要）；对话轮数触顶（默认 5，`max_message_rounds` 可调）时
  记 `message_loop_exhausted` 并降级为把最后一次回复当最终产物。
- **路由**：`Crew._route_message(from_role, to_role, content)` 给出简化路由——
  `to="human"` 返回无人值守占位回复，其他角色从目录取一句职责说明。默认**不**
  注入 Agent，需要对话能力时再把它作为 `message_router` 传进构造。
- **向后兼容**：`message_router=None`（默认）时不检测 message 围栏、系统提示词
  也不告知对话能力，行为与原来完全一致。

```python
from agentcrew.agents.generic import GenericAgent

def my_router(to_role: str, question: str) -> str:
    return f"关于{to_role}：已确认，请继续。"

agent = GenericAgent(
    spec, models=models, store=store, ledger=ledger,
    project_goal="目标", message_router=my_router,  # 省略则不对话
)
```

## V4 增强功能：供应商与模型管理

参考 cc-switch 的供应商管理设计，补齐多供应商接入、模型发现、连通性测速与
Studio 供应商管理页，并修复两条 Studio 链路 bug。

### 1. 自定义 Provider 注册表

`agentcrew.yaml` 新增 `models.custom_providers` 段，用户可定义全新 provider
（如中转网关、自建 API），与 15 个内置预设并存。密钥只存环境变量名
（`api_key_env`），绝不落盘。自定义名与内置重名报错（覆盖内置走
`models.providers`）。`parse_alias` 与 `ModelFactory` 查合并视图。

CLI：`python main.py providers [name]` 列出合并目录。

### 2. 预设库扩充

内置预设从 9 个扩充到 15 个，新增 `openrouter`、`siliconflow`、`moonshot`、
`zhipu`（GLM）、`groq`、`together`，每个含能力位注释与 `suggested_models`
建议模型列表。

### 3. 模型发现服务

新增 `agentcrew/models/discovery.py`：`async fetch_models(binding)` 按需
拉取 provider 模型列表。OpenAI 兼容 `GET /v1/models`，URL 候选自动回退
（`/v1/models` → `/models` → 剥离子路径），支持 `models_url` 覆写，10s 超时，
错误分类（network/auth/not_found/parse/timeout），结果排序去重。anthropic
单独适配。CLI：`python main.py models <provider>`。

### 4. 测速与健康整合

`HealthRecord` 含延迟（ms）与时间戳。按需测速发最小补全请求（`max_tokens=1`），
一次只测一个 provider。API `POST /providers/{name}/test` 返回
`{ok, latency_ms, error?}`。

### 5. API 供应商目录端点

- `GET /providers`：合并目录（内置/自定义标记、base_url、api_key_env 名、
  `key_configured`、能力位摘要、健康状态、suggested_models）。响应**绝不含密钥值**。
- `GET /providers/{name}/models`：实时拉取模型列表（60s 进程内缓存，错误透传）。

### 6. 配置导入导出与版本化

- `python main.py config export`：导出合并视图 YAML（脱敏）。
- `python main.py config import <file>`：校验后合并写入，写前备份
  `agentcrew.yaml.bak`，含疑似密钥字段的导入被拒绝。
- `agentcrew.yaml` 顶层 `config_version`（当前 1），更高版本报清晰错误。

### 7. Studio 供应商管理页（第 9 页）

Studio 新增「供应商」页（侧边栏第 9 项，快捷键 9）：卡片列表展示所有 provider
（名称、base_url、内置/自定义徽标、api_key_env、已配置/未配置、健康状态点、
最近延迟），支持一键测速、拉取模型、「从预设添加」弹窗（生成 YAML 片段供复制，
Studio 不直接改 agentcrew.yaml）。

### 8. Studio 链路 bug 修复

- `COMMON_MODELS` 从裸模型名改为 `<provider>:<model>` 结构化别名，设置页
  选出的默认模型可被引擎正确解析。
- `main.js` 新建运行时传 `--model`（弹窗显式选择优先，否则回落设置页
  `defaultModel`）和 `--max-cost`（弹窗优先，否则回落设置页 `maxCostUsd`）。
- 新建运行弹窗新增 provider 下拉 + 模型下拉（后端不可用时可手输不阻塞）。

## V5 增强功能：供应商模块深化

参考 cc-switch 的供应商模块设计（provider 数据模型、端点候选回退、流式连通检测、ProviderCard 信息展示），在 V4 基础上深化扩充：预设库 15→27、元数据增强、端点候选回退矩阵、流式检测、Studio 编辑与搜索过滤、CLI 过滤增强。

### 1. 预设库扩充（15→27）

内置预设从 15 个扩充到 27 个，新增 12 个：

| 分类 | Provider | base_url | 环境变量 |
|---|---|---|---|
| 聚合 | fireworks | `https://api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` |
| 聚合 | deepinfra | `https://api.deepinfra.com/v1/openai` | `DEEPINFRA_API_KEY` |
| 聚合 | replicate | `https://openai.replicate.com/v1` | `REPLICATE_API_TOKEN` |
| 聚合 | huggingface | `https://router.huggingface.co/v1` | `HF_API_TOKEN` |
| 国内 | baichuan | `https://api.baichuan-ai.com/v1` | `BAICHUAN_API_KEY` |
| 国内 | minimax | `https://api.minimax.chat/v1` | `MINIMAX_API_KEY` |
| 国内 | stepfun | `https://api.stepfun.com/v1` | `STEPFUN_API_KEY` |
| 国内 | lingyi（零一万物） | `https://api.lingyiwanwu.com/v1` | `YI_API_KEY` |
| 国内 | xinghuo（讯飞星火） | `https://spark-api-open.xf-yun.com/v1` | `XINGHUO_API_KEY` |
| 国际 | gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` |
| 国际 | mistral | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` |
| 国际 | cohere | `https://api.cohere.com/v2` | `CO_API_KEY` |

按分类分布：国际 5、国内 12、本地 1（ollama）、专用 2（mock/cli）、聚合 7。

### 2. ProviderPreset 元数据增强

`ProviderPreset` 新增 5 个可选字段（全部有默认值，向后兼容）：

- `category`：`aggregator` / `national` / `international` / `local` / `specialized`
- `family`：预设家族（如 `openai_compat`、`anthropic`）
- `search_aliases`：搜索别名（如 deepseek 的 `["ds", "深度求索"]`）
- `endpoint_candidates`：多端点候选列表，用于模型发现和测速时自动回退
- `api_key_url`：获取 API Key 的官方页面 URL（Studio 显示"获取 Key"链接）

`ModelBinding` 同步携带这些字段，`GET /providers` 响应包含全部新字段。

### 3. 端点候选回退矩阵

`discovery.py` 的 `candidate_urls()` 扩展为"端点候选 × 路径变体"矩阵：当 provider 预设了 `endpoint_candidates` 时，对每个端点依次派生 `/models` 和 `/v1/models` 路径变体，按顺序尝试，第一个成功即返回。无 `endpoint_candidates` 时保持 V4 行为（单端点 + 路径变体回退）。

### 4. 流式检测

新增 `agentcrew/models/stream_check.py`：`async check_streaming(binding, *, api_key, timeout_secs)` 发一个 `stream=true` 的最小补全请求（`max_tokens=1`），验证 SSE 流能正常返回（检测第一个 `data:` 帧的到达时间）。比普通测速更严格——有些端点普通补全能通但流式会挂。

- `HealthRecord` 新增 `stream_ok: bool | None` 字段
- `ModelFactory.health_check_stream(alias)` 按需探测流式能力
- API `POST /providers/{name}/test?mode=stream` 返回 `{ok, stream_ok, latency_ms, error}`
- 默认 `mode=completion` 保持 V4 行为不变

### 5. Studio 供应商页增强

- **编辑 provider**：卡片新增「编辑」按钮，打开编辑弹窗（复用添加弹窗表单结构），修改后生成 YAML 片段供复制。内置 provider 生成 `models.providers.<name>:` 覆盖片段，自定义生成 `models.custom_providers.<name>:`。Studio 仍不直接写 agentcrew.yaml。
- **卡片信息增强**：category 徽标（聚合/国内/国际/本地/专用，不同配色）、「🔗 获取Key」链接（打开官方页面）、多端点候选数量标签、流式状态（流式 ✓/✗）。
- **搜索与过滤**：顶部搜索框（按 name/label/search_aliases 实时过滤，200ms 防抖）+ 6 个 category 筛选按钮，AND 组合，结果计数。
- **BUILTIN_PROVIDERS 同步**：从 14 个扩充到 27 个，与引擎 `config.py` 对齐，含全部新元数据字段。

### 6. CLI 增强

- `python main.py providers`：文本输出增加 category 标签、endpoint_candidates 数量、api_key_url；`--json` 输出完整元数据。
- `python main.py providers --category <cat>`：按分类过滤。
- `python main.py providers --search <keyword>`：按 name/search_aliases 搜索（支持中文别名）。
- `--category` 与 `--search` 可 AND 组合。
- `python main.py models <provider> --verbose`：显示端点候选尝试过程；JSON 输出含 `endpoints_tried` 和 `successful_endpoint`。
- `python main.py doctor`：provider 检测增加流式状态行。

### 红线遵守

- 密钥绝不落盘：仅存 `api_key_env` 环境变量名；`config_snapshot()` 脱敏；`GET /providers` 响应不含密钥值
- 向后兼容：现有 15 个预设 key/语义不变；现有 API 响应只增字段；custom_providers 格式不变；无 endpoint_candidates 时 discovery 行为逐字节一致
- 确定性引擎不变：发现/测速/流式检测均为按需调用，无常驻网络轮询
- Studio 零框架：不新增 npm 依赖，沿用 vanilla JS + h() DOM 构建

---

## R3 能力完善：工具链生产化与实体记忆

参考 GitHub 上同类多 Agent 框架的抽象后，对「能力层」做了一轮定点补齐：
CrewAI 的 `BaseTool._validate_kwargs()`（执行前按 `args_schema` 强校验入参）、
CrewAI 的 entity memory 与 MetaGPT `BrainMemory.knowledge`（跨 run 记住项目里的
命名事实）。这一轮**不动确定性引擎**、不引入任何第三方依赖，新增开关一律默认
关闭，强校验也只对原本就会失败的非法入参生效。

### 1. 工具链真正接进生产 run

P1-1 实现的 `ToolRegistry` / ReAct 循环此前**只在单测里被实例化**：`Crew` 从未构造
注册表，`tool_permissions` 因此是死配置，上面 P1-1 描述的 ReAct 能力在真实 `run`
中根本不会启用。R3 补上唯一装配点 `Crew._build_tool_registry()`：

- `AGENTCREW_TOOLS_ENABLED=true`（或 YAML `tools_enabled: true`，或 `POST /runs`
  的 `options.tools_enabled`）开启；**默认 `false`** —— 关闭时 `AgentFactory` 收不到
  注册表，Agent 走「单次 LLM 调用 → 落盘」的原路径，行为逐字一致。
- 库调用方也可以绕过配置：`Crew(settings, tool_registry=my_registry)`。
- 开启时为本次 run 登记 6 个内置工具：`read_file` / `list_files` / `grep` /
  `run_python` / `write_file` / `web_fetch`；`tool_permissions` 同步下发为执行期
  白名单；装配时记一条 `tool_registry_ready` 事件（含工具清单与授权角色）。
- 读工具以 `<run_dir>` 为根（能看上游 `artifacts/` 与 `context/`），写工具只以
  `<run_dir>/workspace` 为根 —— 中间草稿与交付产物物理隔离，不进 `MANIFEST.json`、
  不过 G1 契约校验。
- `mcp_servers` 仍需异步连接与生命周期管理，本轮保持库层显式装配，不做半接线。

### 2. 工具入参 JSON Schema 强校验

`input_schema` 过去只渲染进系统提示词，模型写错参数只能靠工具自己兜底。现在
`tools/schema.py` 在**执行前**做确定性校验（零第三方依赖，实现 `type` / `required`
/ `enum` / `const` / 数值边界 / 长度 / `pattern` / `items` / `additionalProperties`
子集）：

- 校验失败 → 工具**绝不执行**，错误文本连同 `schema_hint()` 给出的「正确形状」
  一起回灌，让模型在下一轮 ReAct 里自己写对；
- 仍记 `tool_call`（`success=false`），并额外记 `tool_schema_invalid`（含错误列表）；
- 空 schema、非对象 schema、无 `properties` 的自由形状一律放行 —— 自定义工具与
  外部 MCP 的 `inputSchema` 向后兼容；不做隐式类型转换，`"10"` 不会被悄悄变成 `10`。

### 3. 两个新内置工具

| 工具 | 能力 | 边界 |
|---|---|---|
| `write_file` | 往 run 私有工作区写文本草稿，供后续工具读回 | `safe_relpath` + 逐段链接检查 + 64 KiB 上限 + 原子写；不碰产物目录 |
| `web_fetch` | 只读抓取公网页面文本 | 仅 http/https、仅 80/443；拒绝私网 / 回环 / 链路本地 / 保留 IP 与本地主机名；请求前解析 DNS 防 rebinding；不跟随重定向；只收文本类型，响应上限 256 KiB，默认 10s 超时 |

`web_search` 依赖外部搜索 API 与密钥，与「密钥绝不落盘」和离线可测的口径冲突，
本轮不引入。`WebFetchTool` 支持注入同步 `fetcher`，因此相关测试完全离线。

### 4. 实体记忆

`agentcrew/knowledge/entity.py` 在经验库之外补一层「这个项目里出现的名字」：
从成功经验的任务描述、方案摘要、关键决策与产物路径里，用纯规则抽取
`file` / `technology` / `role` / `concept` / `acronym` 五类实体，追加到全局
`output/knowledge/entities.jsonl`（不在任何 run 目录下，`clean` 后仍在）。
读取时按名字归并（`mentions` 累加、`sources` 去重、上下文取最近一次），检索按
「名字命中 +3、上下文每命中一个 token +1」排序并设降噪阈值。

- `AGENTCREW_ENTITY_MEMORY_ENABLED=true`（或 YAML / `POST /runs` options）开启，
  **默认 `false`**；也可 `Crew(settings, entity_memory_enabled=True)`。
- 开启后：经验沉淀时同步写实体库并记 `entity_memory_updated`；规划阶段把相关实体
  渲染成参考块拼进 PM 的 `context_hint` 并记 `entity_memory_reused`。
- 与 `reuse_knowledge` 相互独立：只开经验复用时，PM 收到的上下文与 R3 之前逐字一致。
- 实体来自同一批成功经验，因此 `capture_experiences=False` 时不会产生新实体。

### 配置速查

```yaml
# agentcrew.yaml（两个开关都是顶层标量，默认均为 false）
tools_enabled: true
entity_memory_enabled: true
tool_permissions:
  backend_dev: [read_file, grep, write_file, run_python]
  qa_engineer: [read_file, list_files, grep]
```

```bash
AGENTCREW_TOOLS_ENABLED=true python main.py run --sample linear --no-live --auto
```

### 红线遵守

- 确定性引擎不变：两个开关默认关闭；关闭时不发新事件、不改提示词、不建目录。
- 配置合并顺序不变：新字段走既有 `_YAML_SCALAR_KEYS` / `_ENV_TO_FIELD` 通道；
  `config export` 只在开启态输出这两个键，默认导出文档不变。
- 密钥绝不落盘：两个开关是布尔值，不含密钥；`web_fetch` 不接收凭据。
- 测试只增不减：新增 4 个测试文件覆盖纯函数、工具边界、装配链路与端到端样例。

---

## R4 工作流运行时：条件分支 + 可恢复的人工审批

R3 补的是能力插件；R4 改的是**编排内核本身**。此前 AgentCrew 只能跑「PM 一次拆完的
静态 AND 依赖 DAG」，人工闸门是一次同步 `input()`（非 TTY 直接降级为全自动，HTTP API
更是硬编码 `gate="none"`）。对照需求原文列为调研参考的 LangGraph 状态图、MAF durable
checkpoint、AutoGen `human_input_mode`，以及 Studio 前端早已预留却从未被后端写入的
`awaiting_approval` 状态，本轮把两件事做成一等能力。

### 1. 按上游结果走不同分支（Outcome Routing）

- Agent 在回复里用 `<outcome>标签</outcome>` 声明结果标签，落进 `TaskResult.outcome`。
  标签是受约束的小写 slug，不是表达式 —— **调度决策依旧全程无 LLM、无 eval**。
- PM 拆解时可给任务声明 `when_outcomes: [...]`，只有上游标签命中才执行。
- 未命中分支以 `condition-not-met` 进入 `SKIPPED` 并沿分支传播
  `conditional-skip-propagated`。这两种 reason 是**预期跳过**，不会被
  `Crew._final_status` 判成 FAILED；上游真失败导致的 `upstream-skipped` 仍判失败。
- 依赖多条互斥分支的汇合任务（如 `T-FINAL`）只要有一条分支 `SUCCEEDED` 就继续，
  条件性 SKIPPED 的依赖视为已满足。
- 条件语义集中在新模块 `task/conditions.py`；`TaskGraph` 继续只回答「谁依赖谁」。

### 2. 可跨进程恢复的人工审批（Durable HITL）

新增 `gate/deferred.py::DeferredPrompter`，实现**同一个** `Prompter` Protocol：

1. 卡点创建 `pending` 审批单 → run 以 `awaiting_approval` **正常退出**（不是失败、
   不是取消，不发 `run_finished`），面板与产物照常落盘；
2. 人在**另一个进程**（CLI / HTTP API / Studio）写裁决；
3. `resume` 回到**原卡点**：`decided` → 消费裁决并继续；仍 `pending` → 复用同一张单
   再次挂起。审批单缺失时直接报错，绝不悄悄绕过人工审批。

审批单是 `<run_dir>/approvals.jsonl` 里的追加事件（`create` / `decide` / `consume`），
读取按 id 归并成 `pending → decided → consumed` 三态；`consume` 保证同一裁决不会被
重复执行。`awaiting_approval` 在 `clean` / `delete_run` 守卫里与 `running` 同等保护。

### 3. 完整用法（CLI 跨进程）

```bash
# 进程 A：跑到计划卡点挂起
python main.py run --sample linear --no-live --gate plan --approval-mode deferred

# 另一个终端：看审批单、批准
python main.py approvals <run_id>
python main.py approve <run_id> <approval_id> --decision approve --by 张三

# 进程 B：从原卡点继续（样例续跑必须挂回同一套夹具）
python main.py resume <run_id> --sample linear --force --gate plan --approval-mode deferred
```

`--decision` 取值：计划卡点 `approve` / `cancel` / `save_exit`；交付卡点
`accept` / `abort` / `rework`（`rework` 需 `--task-ids`）。

### 4. HTTP API 与 Studio

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/runs/{id}/approvals` | 列出审批单（服务重启后仍可读） |
| POST | `/runs/{id}/approvals` | body `{"request_id": "...", "decision": {"action": "approve"}, "decided_by": "web"}` |

`POST /runs` 的 `options.approval_mode="deferred"` 会打开 API 通道的人工卡点（此前
恒被覆写为 `gate="none"`）。Studio 第 5 页「质量门禁」顶部新增真实审批单区块，
`run_paused` / `approval_requested` / `approval_decided` 进入 SSE 与事件日志。

### 5. 兼容性与红线

- 默认 `approval_mode=interactive`：仍走 `CliPrompter`，非 TTY 仍降级为 `none`；
  `interrupted` 的 resume 语义（跳过规划与计划卡点）逐字不变。
- 默认无 `<outcome>` / 无 `when_outcomes`：`TaskBoard.refresh()` 的四步结果、
  `Crew._final_status` 判定与 R4 之前完全一致；TaskStatus 仍是 10 态、状态机跃迁表
  一行未改。
- `EXIT_CODES` 字典不变（挂起复用 `ok=0`）；`schema_version` 仍为 1，新增字段均为
  可选；密钥绝不落盘；调度决策全程无 LLM。

---

## R5 真实协作：Agent 间咨询走真调用

R4 让工作流能分支、能挂起等人；R5 修的是另一半——**Agent 之间其实没有真的协作**。
`<message to="…">` 围栏、路由回调、ledger 事件全都写好了，但 `Crew._route_message`
从头到尾只回一句「来自某角色的说明：<goal>」，而且这个方法**从未被注入任何 Agent**。
一个自称 multi-agent 的平台，队友之间交换的是角色职责描述而不是答案。

### 1. 一行围栏换来一次真回答

- 新增 `BaseAgent.consult()`：被点名咨询时发**一次**模型调用，系统提示词换成
  「咨询模式」（保留角色身份、去掉产物协议与长度预算），回复压到 1k token 以内。
- 它**不**解析产物围栏、**不**落盘、**不**改任务状态、**不**进看板——咨询是「问一句
  拿一句」，不是一次任务执行；否则队友的答复会被误当成交付物提交。
- 路由回调同时支持同步函数与协程（`inspect.isawaitable`），既有同步夹具一行不用改。

### 2. 三条边界（都返回文案，不抛错）

| 边界 | 行为 | 原因 |
|---|---|---|
| 问自己 / 未知角色 | 直接给占位文案，**不花调用** | 拼错角色名是最常见的浪费 |
| 配额 `max_peer_messages`（默认 8） | 超出后回「配额已用尽，按已掌握信息继续」 | 互相求证会指数级膨胀预算 |
| 同一 `(提问方, 目标, 问题)` | 第二次直接复用答案 | 模型拿到答复后常原样再问一遍 |

递归天然只有一层：`consult` 走单次调用、不解析 `<message>`，不会 A 问 B、B 又问 A。

### 3. 装配

```bash
AGENTCREW_PEER_DIALOGUE_ENABLED=true AGENTCREW_MAX_PEER_MESSAGES=4 \
  python main.py run --demo
```

或 YAML 顶层 `peer_dialogue_enabled: true` / `max_peer_messages: 4`，或 `POST /runs`
的 `options` 同名键（服务端白名单已登记，非布尔/负数当场 400）。

`Crew` 在开关打开时构造一个 `PeerRouter` 交给 `AgentFactory`；工厂在 `get()` 里按
角色 `bind(from_role)` 后下发——**路由是共享对象，身份必须按角色绑定**，否则所有发问
都会被记到同一个 `from_role` 上。`AgentFactory.message_router` 的 setter 会同步刷新
已缓存实例（与 `subtask_creator` 同一个坑：缓存实例拿着旧值，能力等于没开）。

### 4. 兼容性与红线

- 默认关闭：不注入路由 → 系统提示词里连这段能力都不出现，`<message>` 围栏不检测，
  行为与 R5 之前逐字一致；三个离线样例照旧 9/9、5/5、7/7。
- 离线 mock 通道没有 consult 夹具（`MockProvider` 只认 plan/review/execute），
  `consult` 检测到 mock 直接返回占位说明——演示 run 不会因为「模型答不出咨询」而失败。
- Provider 异常不抛给提问方：记 `agent_consult_failed` 后返回可继续的文案；路由失败
  不该打断在跑的任务。咨询消耗的 token 计入**提问方**的任务预算。
- 调度决策仍全程无 LLM：咨询只影响某个 Agent 拿到的信息，不影响谁先跑、谁可跑。

---

## R6 协作深化：委派治理 + 咨询带上下文 + 咨询沉淀

R5 让队友之间真能对话，但三处遗留都指向同一个问题：**「谁能做什么、知道多少」没有
约束**。这一轮按 §7 的 ROI 顺序把它们补上。

### 1. 动态委派治理（四道闸）

此前 `Scheduler._create_subtask` 直接 `role=str(spec.get("role") or "generic")` ——
模型写什么就是什么。拼错角色名会静默落到通用兜底：任务跑完、产物看着也在，质量却
塌一档，还不报错。委派链也没有任何上限。

| 闸 | 规则 | 不通过时 |
|---|---|---|
| 角色存在 | 必须在角色目录里，或显式写 `generic` | `subtask_role_unknown` + 可用角色清单 |
| 权限 | `delegation_mode=manager_only` 时只有 `project_manager` / `product_manager` 能派 | `subtask_unauthorized` |
| 层数 | 新任务深度 ≤ `max_delegation_depth`（默认 2） | `subtask_depth_exceeded` |
| 总数 | 动态子任务 ≤ `max_dynamic_tasks`（默认 20） | `subtask_quota_exceeded` |

拒绝一律**返回可继续的文案 + 留一条带原因的 `subtask_rejected_by_policy`**，run 不停。
判断「谁在派」看的是**父任务的角色**，不是 Agent 自述。新增 `Task.delegated_by` 留痕
派发者——父任务可能被返工丢弃，角色不会。

```yaml
delegation_mode: manager_only   # 默认 any（= P2-1 以来行为）
max_delegation_depth: 2
max_dynamic_tasks: 20
```

### 2. 咨询带上提问方的任务背景

`consult` 此前只发一句孤立问题，队友只能反问「你们在做什么、要交什么文件」。
现在发问前把提问方的**任务标题 / 目标 / 交付契约**放进 contextvar，由 `PeerRouter`
透传，被问方第一次就能问到点上。

用 contextvar 而不是改回调签名：`message_router` 的 `(目标角色, 问题)` 二参形式
一行没动，既有同步夹具完全不受影响。背景只带三样、目标截断到 300 字——咨询是补充
信息，把上游全部产物摘要塞过去等于把一次咨询变成一次全量执行。

### 3. 咨询结果沉淀进经验库

```bash
AGENTCREW_PEER_CONSULT_CAPTURE_ENABLED=true AGENTCREW_PEER_DIALOGUE_ENABLED=true \
  python main.py run --demo
```

开启后每次**真实**咨询写成一条 `Experience`（`task_description` = 「谁问谁问了什么」，
`solution_summary` = 答复）。下次同类问题走经验库关键词检索能直接命中，省掉一次咨询。

三条边界：占位答复（离线降级、「对方未能作答」）**不落库**——那不是知识；去重命中
不算新咨询，不重复沉淀；沉淀失败只记 `consult_capture_failed`，**不影响**这次咨询的
返回值。默认关闭：咨询是高频低价值事件，默认全收会把经验库淹没。

**分层说明**：`agents` 层被 `test_architecture.py` 禁止 import `knowledge`，所以
写经验库的动作由 Crew 以 `on_consult` 回调注入，`PeerRouter` 只管调用。

### 4. 兼容性

- 四项新配置默认值 = R6 之前的行为：`delegation_mode=any`、深度 2 / 总数 20 只在
  **超限时**才生效、`peer_consult_capture_enabled=false`。
- `Scheduler` 新增的 `catalog` / `settings` 都是可选参数；不传（库层直接构造、
  R6 之前写的测试）时四道闸全部跳过。
- 契约 `schemas/task.json` 新增可选 `delegated_by`；`schema_version` 仍为 1。

---

## R7 主管审核：谁派的活谁验收

R6 的四道闸只回答了「谁能派」，没回答「派出去的活谁验收」。`manager_only` 模式下
经理把活派给下属后，下属的产出仍然只由目录里静态配置的 G2 评审人把关——**经理本人
对结果没有任何发言权**，这不是 hierarchical，只是加了个权限开关。

这一层补那条回路：动态委派任务的产出，在 G1/G2 都放行之后，**先由派活的人过一遍**。

### 1. 它是怎么接进去的

`Gatekeeper.evaluate` 的裁决顺序变成：

```
G1 静态校验 → G2 LLM 评审 → [主管审核] → 状态跃迁
```

主管审核复用同一个 `Reviewer`（只是换了评审人来源与层名），所以返工额度、`fix_hints`
回灌、`ContextBus` 决策记录、ledger 审计全部沿用既有通道，没有第二条返工路径。

```bash
AGENTCREW_SUPERVISOR_REVIEW_ENABLED=true python main.py run --demo
```

### 2. 三条刻意的边界

| 边界 | 行为 | 原因 |
|---|---|---|
| 只对委派任务生效 | `Task.delegated_by` 为空 → 返回 `None`，一次调用都不发 | 规划阶段的任务没有主管，行为与 R7 之前逐字一致 |
| 自己派给自己 | 跳过，记 `supervisor_review_skipped(self_delegated)` | 换个身份再看一遍只会白烧调用和返工额度 |
| 主管不在目录里 | 跳过，记 `unknown_supervisor` | 父任务角色可能是 `generic` 之类，没有可用的评审提示词 |

**主管驳回不受 score / 收敛旁路保护**。那两个旁路是对「机器评审意见」的容错（分数
够高 / 意见收敛就放行），派活的人说不行就是不行——插入点因此在旁路**之后**。

### 3. 审计

主管的裁决是 `ReviewRecord(layer="supervisor")`，与 `static` / `llm` / `human` 区分开：
按层统计驳回数时不会和 G2 混在一起。Studio 门禁页在**出现过 supervisor 记录时**
追加一张「主管复核」卡片——默认（开关关闭）仍然是三层，既有布局与断言不受影响。

离线 mock 通道没有 supervisor 夹具，开启后会被明确跳过并记
`supervisor_review_skipped(mock_channel)`：宁可说「没审」，不要装作审过。

### 4. 兼容性

- 默认 `supervisor_review_enabled=false`：`supervisor=None`，`evaluate` 的代码路径
  与 R7 之前逐字一致。
- `Reviewer.review` 新增的 `reviewer_keys` / `layer` 都是可选参数，不传时评审人仍由
  `catalog.reviewer_for(task.role)` 决定、层名仍是 `llm`。
- `ReviewRecord.layer` 枚举扩为 `static / llm / human / supervisor`（纯新增值）。
- `schema_version` 仍为 1；`TaskStatus` 仍 10 态；`ALLOWED_TRANSITIONS` 未改。

---

## R8 被拒之后：把原因回灌给模型

R6 的四道闸会把拒绝原因写进 ledger，R7 让派活的人能验收——但**模型自己仍然看不见
原因**。它只拿到「没建成」，于是下一轮往往换个同样错的名字继续试，或者干脆把这件活
丢掉。四道闸做了正确的拒绝，却没人告诉被拒的一方错在哪。

这一轮把那条反馈接上。

### 1. 一次额外的纠正机会

```
模型回复 → 解析 <subtask> → 建子任务
                              ├─ 成功 → 继续
                              └─ 被拒 → 原因回灌 + 重问一次 → 用新回复继续
```

回灌的内容是**具体原因**，不是「失败了」：

```
你请求的子任务没有被接受，原因如下：
- 团队里没有 'ghost_dev' 这个角色（可用：project_manager、product_manager、…）

请据此调整后重新输出本任务的完整最终回复：换一个真实存在的角色、改为自己完成，
或减少拆分。仍然必须包含本任务自己的 file= 交付物围栏。
```

只纠正**一轮**。给一次改的机会足够——模型要么换对角色名，要么接受「自己完成」；
继续追问只会把预算花在同一件事上。本任务自己的交付物才是主体，子任务拆不成不该把
任务本身拖垮。

### 2. 原因是怎么传过去的

`subtask_creator` 的签名始终是 `(spec) -> str | None`，没有改。Crew 传给 Agent 的
不再是裸闭包，而是一个 `SubtaskDispatcher` 对象，它在拒绝后暴露 `last_rejection`：

```python
child_id = creator(enriched)
if child_id is None:
    reason = getattr(creator, "last_rejection", None)   # 裸 lambda → None
```

**没有这个属性就不回灌**。这一条是刻意留的：既有测试与库层直连用法传的都是
`lambda spec: None`，它们不该因为一个没声明的属性而凭空多出一次模型调用。于是
默认行为逐字不变，只有 Crew 装配的路径会真正回灌。

### 3. 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `max_subtask_corrections` | `1` | 回灌次数上限；`0` = 关闭（回到 R8 之前） |

也支持 `AGENTCREW_MAX_SUBTASK_CORRECTIONS`、`POST /runs` 的 `options`（负数当场 400）。

### 4. 兼容性

- 裸闭包（无 `last_rejection`）→ 不回灌，调用次数与 R8 之前一致。
- 子任务**建成**时不回灌、不记拒绝（`test_accepted_subtask_is_silent`）。
- 自我修正轮（parse/validate 失败后的重试）里再出现的子任务请求照建但不追问——
  那层已有自己的轮次上限，再套一层只会成倍放大成本。
- mock 样例不含 `<subtask>` 围栏，因此不会触发额外调用；三套样例照旧 9/9、5/5、7/7。

---

## R9 跨任务层级：经理也验收规划期的活

R7 的主管审核只覆盖**动态委派**任务——`delegated_by` 是谁就由谁验收。但 PM 在规划
阶段拆出来的那些任务没有 `delegated_by`，它们的主管理应也是 PM，R7 却完全没管。
这是 hierarchical 缺的最后一环。

### 1. 一个开关加一个角色

```yaml
supervisor_review_enabled: true
supervisor_scope: all            # delegated（默认）| all
supervisor_role: project_manager # scope=all 时规划期任务的主管
```

`scope=all` 时，没有 `delegated_by` 的任务回落到 `supervisor_role` 验收；有
`delegated_by` 的仍然以派活的人为准（派活优先于配置）。

### 2. 为什么默认不开

规划期任务每个都多一次真实评审调用——CNY 意义上的涨价。升级不该静默改变成本结构，
所以默认仍是 `delegated`，要更严格的层级监督就显式打开。

### 3. 不重复烧钱的判断

角色的 G2 评审人是静态配置的（`catalog.reviewer_for`）。比如 `architect` 的任务本来
就由 `project_manager` 审——`scope=all` 再让 PM 以「主管」身份审一遍纯属浪费。所以
主管已经在 G2 评审人里时直接跳过，记 `supervisor_review_skipped(already_in_g2)`。

`backend_dev` 的 G2 评审人是 `architect`，不含 PM，所以 PM 额外验收它是真正的新增
监督，正常执行。

### 4. 跳过的五种原因都可追溯

| reason | 含义 |
|---|---|
| `self_delegated` | 自己派给自己 |
| `unknown_supervisor` | 主管角色不在目录里 |
| `already_in_g2` | 主管本来就在 G2 评审人里（去重） |
| `no_supervisor_configured` | `scope=all` 但 `supervisor_role` 配了空串 |
| `mock_channel` | 离线 mock 无夹具（run 级） |

### 5. 兼容性

默认 `scope=delegated` + `role=project_manager`：规划期任务完全不受影响，R7 的 14 个
用例原样通过。两个新键都可经 YAML / env / `config export-import` / `POST /runs`
`options` 设置（枚举与空串当场 400）。

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

## 角色绑定多 Agent

一个角色可以绑定**多个执行体（agent profile）**：角色仍决定"做什么、能碰什么"，
agent 决定"用谁做、怎么写"。单角色单 agent 时行为与旧版完全一致。

### 配置

```yaml
agents:
  profiles:
    backend_fast:                 # key 即映射键，不用重复写
      model: deepseek:deepseek-chat
      fallback: [openai:gpt-4o-mini]
      tools: [read_file, grep, run_python]   # 只能收窄角色权限，不能扩权
      path_scope: ["backend/"]               # 只能往这些前缀下写
      cost_ceiling_usd: 0.08
    backend_solid:
      model: anthropic:claude-sonnet-5
      path_scope: ["backend/"]

roles:
  backend_dev:
    agents:
      - { ref: backend_solid, duty: primary }
      - { ref: backend_fast,  duty: review  }
    aggregation: { mode: primary_with_review, quorum: 1 }
```

### duty 与汇总模式

| duty | 能写产物 | 能出裁决 | 说明 |
|---|---|---|---|
| `primary` | ✅ 本人 path_scope 内 | ❌ | 主笔，产出契约产物 |
| `section` | ✅ 本人认领的路径 | ❌ | 分工，配合 `split_by_path` |
| `review` | ❌ | ✅（意见） | 只出复核意见，进任务 notes |
| `judge` | ❌ | ✅（择优） | `best_of` 时选一份 |

| 汇总模式 | 行为 |
|---|---|
| `primary_with_review` | 主笔产出 → 复核意见写进 `notes`；复核失败记 `agent_degraded`，不静默放行 |
| `split_by_path` | 按 `path_scope` 切开契约并行执行，各自落盘，产物按人归属 |
| `best_of` | 各写一份到 `drafts/<profile>/`，judge 择优后转正，草稿留档 |
| `merge` | 各写一份草稿，按路径拼接后统一落盘 |

### 权限与可观测性

- **路径**：越界写入抛 `ArtifactScopeError`，走 G1 驳回，不落盘；
- **工具**：`agent.tools` 与角色白名单取交集，启动期校验，越界直接报错（退出码 3）；
- **归属**：`MANIFEST.json` 的 `entries[].produced_by_agent` 记录产出者；
- **事件**：`run_plan_resolved` / `agent_team_start` / `agent_participated` /
  `agent_review` / `agent_aggregated` / `agent_degraded`；
- **可复现**：池内选择用 `sha1(run_id|task_id|attempt|role)` 做种子，resume 与
  mock 回放选中同一个 agent。

### 预算与切换

- **执行体预算**：`cost_ceiling_usd` / `max_calls_per_task` 在**每次调用前**检查，
  超限抛 `AgentBudgetExceeded`，任务判失败（不会再花钱）；`max_cost_usd` 仍是
  run 级硬上限，两者互不替代。
- **整场累计成本**：`run_cost_ceiling_usd` 是同一执行体在**整次 run** 里的累计
  上限（跨任务、跨返工、跨 resume——重启后按 `stats.agent_costs` 灌回）。
  与任务级上限互补：一个管"这次别超支"，一个管"这一场别超支"。超限报
  `已达本次 run 累计成本上限`，由调度器判任务失败。
- **备用模型**：`AgentProfile.fallback` 优先于全局 `model_failover`——某个 agent
  的备用别名只服务它自己，不会影响同别名的其他角色。
- **执行体并发**：`max_concurrency` 限制单个 agent 同时在飞的 LLM 调用数。两级
  槽位固定「先 agent、后 provider」，所有调用点同一取锁顺序，不会循环等待；不配
  则完全沿用 provider / 全局并发，单 agent 路径零改动。
- **执行体速率**：`max_rpm` 是 60 秒滑动窗口（与 provider 级 `max_rpm` 同时生效，
  两者都要满足）。并发管"同时几个"，RPM 管"一分钟几个"——廉价档常被上游按 RPM
  计费，只用并发挡不住。
- **按执行体统计**：run.json 的 `stats.agent_costs` 按 profile 拆分调用次数/成本/
  token，运行报告多一行「执行体 backend_fast 0.02/3次」。
- **运行中切换**：三种入口，效果一致——`Crew.switch_agent(role, profile)`（进程内，
  下一次派发生效，在途任务不打断）、`POST /runs/{id}/agents`（serve 模式下 HTTP
  热切）、`python main.py switch <run_id> <role> <agent>`（写盘，下次 resume 生效）。
  结果统一落 `<run_dir>/binding_overrides.json`；切换目标可以是"已定义但当前没绑"
  的执行体。
- **回滚**：`Crew.reset_agent(role)` / `POST /runs/{id}/agents` 带 `reset: true`
  撤销角色级覆盖，回到配置文件；幂等（没切过返回 `changed: false`），覆盖清空后
  `binding_overrides.json` 会被删除。
- **任务级覆盖**：`Crew.set_task_agent(task_id, profile, role=...)` /
  `POST /runs/{id}/tasks/{task_id}/agent`（`{"agent": "..."}` 或 `{"reset": true}`）。
  只影响这一个任务（含其每次 attempt 与返工），别的任务照旧走角色绑定；优先级
  **任务级 > 角色级 > 配置**。落 `task_overrides.json`，resume 自动恢复；目标
  profile 若在执行前消失，丢覆盖并记 `task_agent_override_dropped`，不炸 run。
  CLI：`python main.py switch <run_id> <role> <agent> --task T-003`。

### 查看

```bash
python main.py roles          # 已绑定的角色会多一行「执行体 ...（汇总：...）」
python main.py agents         # 执行体档案 + 绑定链
python main.py agents --json  # 供 Studio 绑定矩阵读取
```

Studio 两处入口：

- 设置 →「角色与 Agent」矩阵：配置期用，改完点「生成配置片段」复制回 `agentcrew.yaml`；
- Agents 页每张卡底部的「执行体 / 切换执行体 / 恢复默认」：运行期用，选完点确认即
  `POST /runs/{id}/agents`。只在 run 仍在跑时生效；未连后端或 run 已结束时
  就地写明原因（不弹窗、不假装成功）。
- 事件日志页顶部「执行体变更」卡：把 `agent_switched` / `agent_binding_reset`
  按发生顺序摊开（时间 · 角色 · from → to · 原因 · duty），回滚行标绿。
  这类事件混在几千条日志里等于没记，所以单独置顶。
- 任务详情抽屉「负责 Agent」下方：`[执行体 ▾] [确认] [撤销覆盖]`，只改当前任务。

示例配置见 `examples/agentcrew.multi-agent.example.yaml`。

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

15 个内置 provider（`agentcrew/models/config.py::PROVIDER_PRESETS`，可被
`agentcrew.yaml` 的 `models.providers` 覆盖；另可通过 `models.custom_providers`
新增自定义 provider，见「自定义 Provider」）：

| 别名 | 实现 | 默认 base_url | `json_schema` | `json_object` | API Key 环境变量 |
|---|---|---|---|---|---|
| `openai` | OpenAI 兼容 | `https://api.openai.com/v1` | ✅ | ✅ | `OPENAI_API_KEY` |
| `anthropic` | Anthropic 原生 Messages API | `https://api.anthropic.com` | ❌ | ❌ | `ANTHROPIC_API_KEY` |
| `deepseek` | OpenAI 兼容 | `https://api.deepseek.com/v1` | ❌ | ✅ | `DEEPSEEK_API_KEY` |
| `doubao` | OpenAI 兼容 | `https://ark.cn-beijing.volces.com/api/v3` | ❌ | ✅ | `ARK_API_KEY`（豆包 / 火山方舟） |
| `qwen` | OpenAI 兼容 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | ❌ | ✅ | `DASHSCOPE_API_KEY`（通义千问） |
| `ernie` | OpenAI 兼容 | `https://qianfan.baidubce.com/v2` | ❌ | ❌ | `QIANFAN_API_KEY`（文心一言，千帆 v2） |
| `ollama` | OpenAI 兼容 | `http://localhost:11434/v1` | ❌ | ✅ | 不需要真实 Key |
| `mock` | MockProvider | — | ✅ | ✅ | 不需要（离线确定性回放） |
| `cli` | 外部 CLI 引擎 | — | ❌ | ❌ | 不需要（command 经 provider_overrides 指定） |
| `openrouter` | OpenAI 兼容 | `https://openrouter.ai/api/v1` | ❌ | ✅ | `OPENROUTER_API_KEY` |
| `siliconflow` | OpenAI 兼容 | `https://api.siliconflow.cn/v1` | ❌ | ✅ | `SILICONFLOW_API_KEY` |
| `moonshot` | OpenAI 兼容 | `https://api.moonshot.cn/v1` | ❌ | ✅ | `MOONSHOT_API_KEY` |
| `zhipu` | OpenAI 兼容 | `https://open.bigmodel.cn/api/paas/v4` | ❌ | ✅ | `ZHIPU_API_KEY`（智谱 GLM） |
| `groq` | OpenAI 兼容 | `https://api.groq.com/openai/v1` | ❌ | ✅ | `GROQ_API_KEY` |
| `together` | OpenAI 兼容 | `https://api.together.xyz/v1` | ❌ | ✅ | `TOGETHER_API_KEY` |

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

### 自定义 Provider

除 15 个内置预设外，可在 `agentcrew.yaml` 的 `models.custom_providers` 段新增
任意 provider（如自建网关、第三方中转站），与内置预设并存：

```yaml
models:
  custom_providers:
    myrelay:
      implementation: openai_compat
      base_url: http://localhost:9000/v1
      api_key_env: MYRELAY_API_KEY
      capabilities:
        supports_json_object: true
      suggested_models:
        - gpt-4o-mini
        - deepseek-chat
```

- **密钥只存环境变量名**：`api_key_env` 是环境变量的**名字**（如 `MYRELAY_API_KEY`），
  真实 Key 写进 `.env` 或系统环境变量，绝不落盘。
- 自定义名与内置重名时报错（想覆盖内置走 `models.providers` 段）。
- 环境变量 `AGENTCREW_CUSTOM_PROVIDERS` 支持紧凑 JSON 形式。
- `python main.py providers` 列出合并目录（内置/自定义标记、base_url、能力位）。

### 模型发现与测速

- `python main.py models <provider>`：实时拉取该 provider 的模型列表
  （OpenAI 兼容 `GET /v1/models`，URL 候选自动回退，10s 超时，错误分类清晰）。
- `POST /providers/{name}/test`：按需测速（发 `max_tokens=1` 的最小请求），
  返回 `{ok, latency_ms, error?}`。
- `GET /providers`：返回合并目录（含健康状态、延迟、`key_configured` 布尔值，
  **响应不含密钥值**）。
- `GET /providers/{name}/models`：实时拉取模型列表（60s 缓存）。

### 配置导入导出

- `python main.py config export`：导出合并视图的 YAML（密钥字段天然脱敏）。
- `python main.py config import <file>`：校验后合并写入 `agentcrew.yaml`，
  写前自动备份为 `agentcrew.yaml.bak`。
- `agentcrew.yaml` 顶层支持 `config_version`（当前 1），更高版本报清晰错误。

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
| `AGENTCREW_API_AUTH_ENABLED` | `false` | REST API 的 API Key 认证总开关（E-3） |
| `AGENTCREW_API_KEYS` | 空 | 有效 API Key 白名单（逗号分隔；只参与认证比对，不落盘） |
| `AGENTCREW_API_RATE_LIMIT_ENABLED` | `false` | REST API 的 IP 速率限制总开关（E-3） |
| `AGENTCREW_API_RATE_LIMIT_MAX` | `100` | 滑动窗口内每 IP 最大请求数 |
| `AGENTCREW_TOOLS_ENABLED` | `false` | R3：把 6 个内置工具接进真实 run 的 ReAct 循环（见「R3 能力完善」） |
| `AGENTCREW_ENTITY_MEMORY_ENABLED` | `false` | R3：跨 run 实体记忆的沉淀与规划注入 |
| `AGENTCREW_APPROVAL_MODE` | `interactive` | R4：人工卡点实现方式 `interactive`（TTY 问答）/ `deferred`（审批单挂起，跨进程恢复） |
| `AGENTCREW_PEER_DIALOGUE_ENABLED` | `false` | R5：Agent 间真实咨询（`<message to="…">` 走真模型调用） |
| `AGENTCREW_MAX_PEER_MESSAGES` | `8` | R5：一次 run 内真实跨角色咨询的次数上限（`0` = 全禁） |
| `AGENTCREW_PEER_CONSULT_CAPTURE_ENABLED` | `false` | R6：把真实咨询的问答沉淀进经验库 |
| `AGENTCREW_DELEGATION_MODE` | `any` | R6：`any`（谁都能派活）/ `manager_only`（只有经理能派） |
| `AGENTCREW_MAX_DELEGATION_DEPTH` | `2` | R6：动态委派的层数上限（根任务算第 0 层） |
| `AGENTCREW_MAX_DYNAMIC_TASKS` | `20` | R6：一次 run 内动态子任务总数上限 |
| `AGENTCREW_SUPERVISOR_REVIEW_ENABLED` | `false` | R7：动态委派任务的产出先由派活的人验收 |
| `AGENTCREW_MAX_SUBTASK_CORRECTIONS` | `1` | R8：子任务被拒时把原因回灌给模型的次数上限（`0` = 关闭） |
| `AGENTCREW_SUPERVISOR_SCOPE` | `delegated` | R9：主管审核范围 `delegated`（只审委派任务）/ `all`（也审规划期任务） |
| `AGENTCREW_SUPERVISOR_ROLE` | `project_manager` | R9：`scope=all` 时规划期任务的主管角色 |

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
| `tools_enabled` | R3：工具链生产化开关，默认 `false`（等价 `AGENTCREW_TOOLS_ENABLED`） |
| `entity_memory_enabled` | R3：实体记忆开关，默认 `false`（等价 `AGENTCREW_ENTITY_MEMORY_ENABLED`） |
| `approval_mode` | R4：人工审批模式，默认 `interactive`（等价 `AGENTCREW_APPROVAL_MODE`） |
| `peer_dialogue_enabled` | R5：Agent 间真实咨询开关，默认 `false`（等价 `AGENTCREW_PEER_DIALOGUE_ENABLED`） |
| `max_peer_messages` | R5：跨角色咨询次数上限，默认 `8`（等价 `AGENTCREW_MAX_PEER_MESSAGES`） |
| `peer_consult_capture_enabled` | R6：咨询沉淀开关，默认 `false` |
| `delegation_mode` | R6：动态委派权限模式，默认 `any` |
| `max_delegation_depth` | R6：委派层数上限，默认 `2` |
| `max_dynamic_tasks` | R6：动态子任务总数上限，默认 `20` |
| `supervisor_review_enabled` | R7：主管审核开关，默认 `false` |
| `max_subtask_corrections` | R8：委派拒绝原因回灌次数上限，默认 `1` |
| `supervisor_scope` | R9：主管审核范围，默认 `delegated` |
| `supervisor_role` | R9：规划期任务的主管角色，默认 `project_manager` |
| `tool_permissions` | R3 生效：角色 → 工具白名单，开启 `tools_enabled` 后由 Crew 下发 |
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

## 性能与稳定性基准

两个脚本把「并发表现」与「长期稳定性」变成可复跑、可复核的材料，产物落在
`bench/`（`report.md` / `stability.md` 可读，`results.json` / `stability.json` 可机读）。

```bash
python scripts/bench.py                       # 并发 1/2/4/8，各 3 次
python scripts/bench.py --levels 1,4,8 --real # 额外做一次真实 provider 探测
python scripts/soak.py                        # 连续 20 次 run + 2 类故障注入
```

**并发基准**测的是**引擎调度**，不是模型速度：离线模式下给每次 LLM 调用注入固定
延迟（默认 200ms，真实调用常在 1–3 秒，已是保守取值），然后从 ledger 的
`task_started` / `task_finished` 时间戳把并发形态还原出来。关键指标是：

- **峰值并发** —— 任一时刻同时运行的任务数，直接证伪"调度器在串行派发"；
- **图上的理论上限** —— Σ单任务耗时 / 关键路径耗时，即这张图**再并发也超不过**的加速比；
- **达效率** —— 实测加速比 / 理论上限。判据用达效率而不是"快了几倍"：
  `[1,1,2,4]` 形状的图上限只有 1.65×，拿它去对"并发 4 应该快 4 倍"必然不达标。

**稳定性测试**分两段：连续 20 次 run 看成功率、内存与句柄是否随次数增长、墙钟是否
劣化；再注入两类故障 —— 模型调用瞬时失败（期望任务重试后整轮仍完成）与产物缺件被
G1 连续驳回（期望失败被隔离在该任务与其下游，其余任务照常完成、run 正常收尾）。

> 离线夹具是确定性回放，因此这两份报告只代表**引擎与本地 IO** 的表现；
> 真实模型侧的延迟与可用性见 `bench/report.md` 的第四节探测。

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

6. **HTTP API 为内部控制面设计**。手写 HTTP/1.1 解析，仅支持 GET/POST/PUT/DELETE；
   E-3 起提供**可选**的 API Key 认证与 IP 速率限制（默认关闭，见上「API Key 认证与
   速率限制」），但仍无请求体大小上限（64KB 硬编码），开启认证前不建议直接暴露公网。
7. **MCP 客户端仅支持 stdio 传输**。未实现 SSE/HTTP 传输、resources/prompts 能力、
   progress 通知；连接真实 MCP 服务器前建议先用 `list_tools()` 验证兼容性。
8. **Web UI 无人工闸门交互**。P2-10 起 Dashboard 已支持取消 / 删除 / 切换模型 /
   查看 ledger 与统计，但 G3 人工卡点（plan / delivery 确认）仍需 CLI；无产物预览、
   无任务详情面板。

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
