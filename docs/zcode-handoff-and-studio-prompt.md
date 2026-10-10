# AgentCrew 项目工作报告与桌面应用开发提示词

> 本文档面向 ZCode：前半部分是后端平台的完成状态报告，后半部分是给 ZCode 的桌面应用开发提示词。后端已完全可用，
>
> **不改一行后端代码**
>
> ，只做 GUI 封装。



***

## 第一部分：工作报告（ZCode 必读）

### 项目概述

AgentCrew 是一个多智能体协作平台：PM 调度 8 个角色 Agent（产品经理、架构师、后端、前端、部署、文档、测试、终审），从一句话目标自动完成需求拆解 → 代码生成 → 门禁审查 → 交付物产出的全流程。

### 完成状态



| 模块                  | 状态            | 说明                                                   |
| ------------------- | ------------- | ---------------------------------------------------- |
| V1 核心平台             | ✅ 完成          | 1227 测试全绿，真实模型端到端 8/8                                |
| V2-1 LLM 摘要压缩       | ✅ 完成          | 超预算上游摘要自动 LLM 压缩，未配置时零开销                             |
| V2-2 复杂度选模型         | ✅ 完成          | 按任务复杂度自动选低 / 中 / 高三档模型                               |
| V2-3 外部 CLI 引擎      | ✅ 完成          | 支持任意外部命令作为 LLM 提供者                                   |
| V2-4 树状任务拆解         | ✅ 完成          | Task 支持 parent\_id，TaskGraph 支持树查询                   |
| V2-5 HTTP API + SSE | ✅ 完成          | `python main.py serve` 启动，实时事件推送                     |
| V2-6 MCP 协议互操作      | ✅ 完成          | stdio JSON-RPC 客户端，已通过真实服务器验证                        |
| V2-7 Web UI 面板      | ✅ 完成          | Kanban 看板 + 实时事件流（浏览器端）                              |
| CI                  | ✅ 全绿          | Python 3.10/3.11/3.12/3.13 矩阵 + ruff + mypy + pytest |
| 测试                  | ✅ 1287 passed | 含 API 测试；ruff 全清；mypy 源码 + 测试 0 error                |

### 技术栈



* **语言**：Python 3.10+（本机 3.14，CI 覆盖 3.10–3.13）

* **核心依赖**：openai、anthropic、pydantic、rich、PyYAML、python-dotenv

* **零额外依赖**：HTTP 服务器、MCP 客户端、Web UI 全部手写，无框架

* **入口**：`main.py`（CLI），`agentcrew/`（包）

### 仓库结构



```
E:\agentcrew\
├── main.py                    # CLI 入口（run/resume/cancel/status/roles/clean/serve）
├── agentcrew/                 # 核心包
│   ├── agents/                # 8 角色 Agent 实现
│   ├── api/                   # HTTP API + Web UI（V2-5/7）
│   ├── artifacts/             # 产物存储与校验
│   ├── config.py              # 配置（pydantic Settings）
│   ├── context/               # 上下文总线 + LLM 摘要（V2-1）
│   ├── crew/                  # Crew 调度器 + Scheduler
│   ├── gate/                  # 三级门禁（静态/LLM/人工）
│   ├── mcp/                   # MCP 客户端（V2-6）
│   ├── models/                # 模型工厂 + Provider + 复杂度（V2-2/3）
│   ├── roles/                 # 角色定义与目录
│   ├── runtime/               # Ledger + Checkpoint + BoardView
│   ├── task/                  # Task + TaskGraph（V2-4）
│   └── utils/
├── tests/                     # 1287 个测试
├── examples/                  # 演示夹具 + Todo 目标
├── docs/                      # 设计文档 + 本提示词
├── output/                    # 运行产物（gitignore）
├── .github/workflows/ci.yml   # CI 配置
└── README.md
```

### CLI 接口（ZCode 调用后端的唯一方式）



```
# 启动新运行
python main.py run --goal "<目标描述>" --mock --auto --no-live --output-dir <目录>

# 恢复中断的运行
python main.py resume <run_id> --mock --auto --no-live

# 取消运行
python main.py cancel <run_id>

# 查看运行状态
python main.py status <run_id>

# 列出可用角色
python main.py roles

# 启动 HTTP API + Web UI
python main.py serve --port 8765

# 清理旧运行
python main.py clean
```

**关键参数**：



* `--mock`：离线模式，无需 API Key（开发 / 演示默认用）

* `--auto`：跳过人工闸门（等价 `--gate none`）

* `--no-live`：**必须传**，关闭终端看板（否则会与 GUI 刷屏冲突）

* `--output-dir`：指定输出根目录

* `--demo`：使用内置 Todo Web 目标（与 `--goal` 二选一）

**退出码**：



| 码   | 含义           |
| --- | ------------ |
| 0   | 成功           |
| 1   | 有任务失败        |
| 2   | 计划拆解失败       |
| 3   | 配置错误         |
| 4   | 运行停滞         |
| 130 | 用户取消（Ctrl-C） |

### 数据格式（ZCode 读取的唯一来源）

每次运行在 `<output-dir>/<run_id>/` 下产生：



```
<run_id>/
├── run.json        # 运行概览（周期性重写）
├── tasks.json      # 所有任务详情（周期性重写）
├── ledger.jsonl    # 实时事件流（追加写，**实时进度唯一来源**）
├── artifacts/      # 交付物 + MANIFEST.json
├── context/        # 上下文快照（调试用）
├── diagnostics/    # 诊断信息
└── review/         # 评审记录
```

#### run.json 字段



```
{
  "run_id": "20261006-124602-9dfc",
  "schema_version": 1,
  "project_goal": "...",
  "status": "running|completed|failed|cancelled|interrupted",
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "config_snapshot": { ... },
  "stats": {
    "total_tasks": 8,
    "succeeded": 6,
    "failed": 1,
    "blocked": 1,
    "skipped": 0,
    "llm_calls": 14,
    "prompt_tokens": 12345,
    "completion_tokens": 6789,
    "estimated_cost_usd": 0.05,
    "started_at": "ISO8601|null",
    "finished_at": "ISO8601|null"
  }
}
```

#### tasks.json 字段



```
{
  "tasks": [
    {
      "id": "T-BE",
      "title": "后端开发",
      "description": "...",
      "role": "backend_dev",
      "acceptance_criteria": ["..."],
      "dependencies": ["T-ARCH"],
      "priority": 5,
      "brief": "...",
      "status": "running",
      "attempts": 2,
      "rework_count": 1,
      "max_attempts": 3,
      "max_reworks": 2,
      "deliverables": [{"path": "main.py", "kind": "python", "required": true}],
      "result": {"summary": "...", "artifacts": [...], "duration_ms": 12345},
      "reviews": [{"round": 1, "layer": "static", "verdict": "reject", "issues": [...]}],
      "status_history": [{"from_status": "pending", "to_status": "ready", "at": "..."}]
    }
  ]
}
```

**10 种任务状态**（配色须区分）：

`pending` `ready` `running` `review` `rework` `succeeded` `failed` `blocked` `cancelled` `skipped`

#### ledger.jsonl 事件

每行一个 JSON 对象：`{"ts": "ISO8601", "run_id": "...", "event": "...", "data": {...}}`

**13 种事件**：



| event              | data 字段                                                                                     |
| ------------------ | ------------------------------------------------------------------------------------------- |
| `run_started`      | goal                                                                                        |
| `plan_created`     | task\_count, roles, planner\_attempts                                                       |
| `plan_rejected`    | attempt, errors\[]                                                                          |
| `task_started`     | task\_id, role, attempt                                                                     |
| `task_finished`    | task\_id, role, attempt, duration\_ms                                                       |
| `task_transition`  | task\_id, from, to, reason                                                                  |
| `task_notes`       | task\_id, notes                                                                             |
| `gate_result`      | task\_id, round, layer, verdict, score, issues\[], reviewer                                 |
| `artifact_written` | task\_id, path, size\_bytes, sha256                                                         |
| `llm_call`         | role, model, prompt\_tokens, completion\_tokens, estimated\_cost\_usd, latency\_ms, purpose |
| `llm_degraded`     | provider, from\_level, to\_level, reason                                                    |
| `error`            | where, exc\_type, message, task\_id                                                         |
| `run_finished`     | status, exit\_code, duration\_ms                                                            |

**读取方式**：追加写，轮询读新增字节（记录文件偏移量，不要整文件重读）。

#### artifacts/ 目录



* 真实交付物文件（代码、文档等）

* `MANIFEST.json`：`{"entries": [{"path": "...", "kind": "python", "size_bytes": 123, "produced_by_role": "backend_dev"}]}`

### 现成测试数据



* `output/realrun7/`：一次成功的真实模型运行（Todo Web，8/8 成功）

* `output/realrun8/`：泛化验证运行（短链接服务，9/9 成功）

* `output/v2verify/`：V2 协同验证运行（3 成功 1 失败 4 阻塞）

* 可直接用这些目录调试 UI，不必每次重跑后端

### 已知限制（设计边界）



1. 不执行生成的代码（只生成，不运行）

2. token 估算为启发式（非精确计数）

3. 文心模型仅支持千帆 v2 端点

4. 单进程（无分布式）

5. mock 产物非模型生成（固定夹具）

6. HTTP API 无认证（不建议暴露公网）

7. MCP 仅 stdio 传输（无 SSE/HTTP）

8. Web UI 只读看板（不支持人工闸门交互）



***

## 第二部分：ZCode 桌面应用开发提示词

### 任务

为 AgentCrew 平台开发一个 **Electron 桌面 GUI**（代号 AgentCrew Studio）。后端已完全可用，**不改后端一行代码**—— 你只调用 CLI、读状态文件、把进度可视化。

### 技术栈（严格遵守）



* **Electron** + 原生 HTML/CSS/JS

* **禁止**：React、Vue、Angular、任何打包器（webpack/vite）、任何原生模块

* **唯一依赖**：`electron`（npm 安装）

* Node v24 /npm 11 已验证可用

* 代码放 `studio/` 目录，自带 `package.json`

### 核心功能（按优先级）

#### P0：必须有



1. **运行列表**：扫描 `output-dir` 下所有 run，显示 run\_id、目标、状态、时间、统计

2. **启动新运行**：输入目标 → 调用 `python main.py run --goal "..." --mock --auto --no-live` → 实时显示进度

3. **实时任务看板**：10 种状态配色，显示每个任务的状态、角色、进度、累计成本

* **运行中就持续更新**（轮询 ledger.jsonl 新增字节），不是结束后才刷新

1. **事件流**：实时显示 ledger 事件，可按事件类型过滤

2. **结束报告**：运行结束后显示统计（成功 / 失败 / 阻塞数、LLM 调用、token、成本、耗时）

3. **任务详情**：点击任务查看描述、验收标准、交付物、逐轮评审记录、状态历史

4. **产物浏览**：列出 artifacts/ 下的文件，可预览文本类产物（.py/.md/.json/.yaml/.html/.txt/.css/.js）

#### P1：应该有



1. **取消运行**：运行中可取消（调用 `python main.py cancel <run_id>`）

2. **恢复运行**：对 interrupted 状态的 run 可 resume

3. **多 run 同时显示**：列表页能同时看到多个 run 的状态

4. **后端异常处理**：CLI 调用失败、文件不存在、JSON 解析失败时给可读提示，不白屏不崩溃

5. **关窗保护**：有运行中进程时，关窗前提示用户确认或终止进程，不留僵尸进程

#### P2：锦上添花



1. **成本统计图表**：简单的柱状图 / 饼图展示各角色 LLM 调用和成本

2. **产物导出**：将 artifacts/ 打包为 zip 下载

3. **主题切换**：明 / 暗主题

4. **设置页**：配置 Python 路径、output-dir、默认参数（mock/auto）

### 实现要点

#### 进程管理



```
// 启动运行
const { spawn } = require('child_process');
const proc = spawn('python', ['main.py', 'run', '--goal', goal, '--mock', '--auto', '--no-live', '--output-dir', outputDir], {
  cwd: 'E:\\agentcrew',  // 仓库根目录
  stdio: ['ignore', 'pipe', 'pipe']
});
```



* 用 `spawn` 而非 `exec`（长时间运行，需要流式输出）

* 进程退出时读取退出码，映射为运行状态

* 保存 `proc` 引用，取消时调用 `proc.kill()`

* **Windows 注意**：`python` 可能是 `python.exe` 或 `python3`，先探测可用命令

#### 实时进度读取



```
const fs = require('fs');
let ledgerOffset = 0;

// 每 500ms 轮询 ledger.jsonl 新增内容
setInterval(() => {
  const stat = fs.statSync(ledgerPath);
  if (stat.size > ledgerOffset) {
    const content = fs.readFileSync(ledgerPath, 'utf8', { start: ledgerOffset });
    ledgerOffset = stat.size;
    const lines = content.trim().split('\n');
    for (const line of lines) {
      const event = JSON.parse(line);
      // 处理事件，更新 UI
    }
  }
}, 500);
```



* ledger.jsonl 是**追加写**，记录偏移量只读新增部分

* run.json 和 tasks.json 是**周期性重写**，每次完整读取

* 文件可能正在写入，读取时处理 JSON 解析异常

#### 状态配色（参考）



```
.status-pending   { background: #6b7280; }  /* 灰 */
.status-ready     { background: #3b82f6; }  /* 蓝 */
.status-running   { background: #f59e0b; }  /* 黄 */
.status-review    { background: #8b5cf6; }  /* 紫 */
.status-rework    { background: #f97316; }  /* 橙 */
.status-succeeded { background: #22c55e; }  /* 绿 */
.status-failed    { background: #ef4444; }  /* 红 */
.status-blocked   { background: #64748b; }  /* 灰蓝 */
.status-cancelled { background: #9ca3af; }  /* 浅灰 */
.status-skipped   { background: #d1d5db; }  /* 更浅灰 */
```

#### 窗口布局建议



```
┌─────────────────────────────────────────────────┐
│  AgentCrew Studio    [运行列表] [新运行] [设置]  │
├──────────┬──────────────────────────────────────┤
│ 运行列表  │  任务看板（Kanban，按状态分列）        │
│          │  ┌─────┬─────┬─────┬─────┬─────┐    │
│ ● run1   │  │pending│ready│run │review│rework│  │
│ ○ run2   │  │  T1  │ T2  │ T3  │     │     │  │
│ ○ run3   │  ├─────┴─────┴─────┴─────┴─────┤    │
│          │  │succeeded│failed│blocked│cancel│skip│ │
│          │  │  T4 T5  │ T6  │ T7  │     │    │  │
│          │  └──────────────────────────────┘    │
│          │  实时事件流（可过滤）                  │
│          │  [12:01] task_started T-BE backend    │
│          │  [12:02] gate_result T-BE reject      │
│          │  [12:03] llm_call backend $0.002      │
└──────────┴──────────────────────────────────────┘
```

### 验收标准（逐条实测）



1. `cd studio && npm install && npm start` 无报错，GUI 正常启动

2. 点击 "新运行"，输入目标，启动 `--mock` 运行，**看板在运行中持续更新**（不是结束后才刷）

3. 运行结束后，统计数字与 `run.json` 的 `stats` **完全一致**

4. 10 种任务状态配色全覆盖，视觉上可区分

5. 点击任意任务，能看到描述、验收标准、交付物、评审记录、状态历史

6. 能预览文本类产物（.py/.md/.json 等），内容正确

7. 运行列表能同时显示多个 run（至少 realrun7、realrun8、v2verify）

8. 后端 CLI 报错或文件缺失时，显示可读错误信息，不白屏不崩溃

9. 运行中关闭窗口，弹出确认对话框；确认后进程被终止，不留僵尸

10. 取消按钮能正确终止运行中的 CLI 进程

### 交付物



```
studio/
├── package.json          # 依赖只有 electron，start 脚本
├── main.js               # Electron 主进程
├── preload.js            # 预加载脚本（如需要）
├── index.html            # 主窗口 HTML
├── renderer.js           # 渲染进程逻辑（原生 JS）
├── styles.css            # 样式
└── README.md             # 使用说明
```

### 现成数据（直接用来调 UI）



* `E:\agentcrew\output\realrun7\` — 成功运行（Todo Web，8/8）

* `E:\agentcrew\output\realrun8\` — 成功运行（短链接服务，9/9）

* `E:\agentcrew\output\v2verify\` — 部分失败运行（3 成功 1 失败 4 阻塞）

* 用这些目录验证列表、看板、详情、产物预览，不必每次重跑后端

### 注意事项



* **Python 路径**：用户机器上可能是 `python`、`python3`、或 venv 里的 `python.exe`。启动时探测，设置页允许手动配置

* **Windows 路径**：output-dir 用反斜杠，CLI 参数里的路径要正确转义

* **编码**：run.json/tasks.json 是 UTF-8，ledger.jsonl 也是 UTF-8。Windows 上 `fs.readFileSync` 指定 `'utf8'`

* **并发**：Electron 主进程管理子进程，渲染进程通过 IPC 通信。不要在渲染进程直接 spawn

* **性能**：ledger.jsonl 可能很大（几千行），只渲染最新的 N 条（如 200 条），支持滚动加载

* **不要改后端**：所有功能通过 CLI + 文件读取实现，不修改 `agentcrew/` 下任何代码



***

## 附录：快速验证命令



```
# 启动一次 mock 运行（约 3 秒完成）
cd E:\agentcrew
python main.py run --demo --mock --auto --no-live --output-dir output\studio-test

# 查看产生的文件
dir output\studio-test\<run_id>\

# 启动 HTTP API（可选，浏览器端也能看板）
python main.py serve --port 8765
# 然后打开 http://localhost:8765
```