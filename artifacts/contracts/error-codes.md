# AgentCrew 错误码契约

> 本文档汇总 AgentCrew 全栈四层错误标识：HTTP 状态码、CLI 退出码、业务异常类、IPC 错误约定。
> 所有定义均逆向自真实代码，未凭空设计。

---

## 1. HTTP 状态码（`agentcrew/api/server.py`）

### 1.1 成功状态码

| 状态码 | 含义 | 触发端点 |
|--------|------|----------|
| 200 OK | 请求成功，返回 JSON 或 HTML | GET /runs, GET /runs/{id}/status, GET /runs/{id}/ledger, GET /runs/{id}/stats, GET /runs/{id}/model, GET /runs/{id}/agents, GET /runs/{id}/approvals, GET /runs/{id}/artifacts, GET /keys, GET /tools, GET /flows, GET /providers, GET /metrics（text/plain）, DELETE /runs/{id}, DELETE /keys/{id}, DELETE /flows/{name}, GET /, GET /health |
| 201 Created | 资源已创建 | POST /keys（新建密钥）、POST /flows（保存流程） |
| 202 Accepted | 操作已接受，后台异步执行 | POST /runs, POST /runs/{id}/cancel, POST /runs/{id}/resume, PUT /runs/{id}/model, POST /runs/{id}/events, POST /runs/{id}/approvals, POST /runs/{id}/agents |

> DELETE 返回 200（body 含 `freed_bytes`），不使用 204 No Content。

### 1.2 客户端错误（4xx）

| 状态码 | 触发条件 |
|--------|----------|
| **400 Bad Request** | 请求体非 JSON / 非对象；`goal` 为空字符串；`options` 非对象或含未知键；DELETE 未带 `?confirm=1`；PUT /model 时 `alias` 为空或 `role` 非字符串；ledger/stats 路径参数越界或非法 run_id（ValueError）；模型切换参数非法；`_event` 事件名不合白名单 |
| **401 Unauthorized** | 认证启用（默认）且请求缺少或携带无效 API Key；响应带 `WWW-Authenticate: Bearer` 头。`/health` 同样受保护 |
| **403 Forbidden** | 身份有效但作用域不足（`required_scope` 校验失败）；响应体点名所需作用域 |
| **404 Not Found** | run_id 不存在（status/ledger/stats/delete/resume/model 端点）；非 admin 访问他人 run（刻意用 404 而非 403，避免枚举）；cancel 时 run 不在运行中；未匹配路径兜底 |
| **405 Method Not Allowed** | 使用了未实现的 HTTP 方法（手写 server 仅支持 GET / POST / PUT / DELETE） |
| **409 Conflict** | cancel：驱动对象无 cancel 方法；resume：run 已在运行中，或另一进程持有 ResumeLock；审批单已裁决/已过期；GET /model：run 未装配 ModelFactory；PUT /model：run 不在运行中；DELETE：run 正在运行中拒绝删除 |
| **413 Payload Too Large** | 请求体超过 4 MiB（`http.MAX_BODY_BYTES`） |
| **429 Too Many Requests** | 速率限制超限；响应带 `Retry-After` 头。默认只按 socket peer 计数，XFF 仅在配置可信代理时解析 |

### 1.3 服务端错误（5xx）

| 状态码 | 触发条件 |
|--------|----------|
| **500 Internal Server Error** | 未预料的内部异常（响应体只含固定文案与异常类型名，详情经脱敏后只进服务端日志）；DELETE 时 OSError；create/resume 启动时 crew 在首个事件前抛出异常 |
| **503 Service Unavailable** | 未配置 `output_dir`（各 run 级端点统一）；未配置 crew_factory / settings |
| **504 Gateway Timeout** | create：30 秒内未收到 `run_started` 事件 |

### 1.4 错误响应体格式

**实际形状**（代码统一）：
```json
{ "error": "<中文错误消息>" }
```

> 注意：代码中 **未实现** `{code, message, details}` 三段式错误结构。所有错误响应均为单字段 `error`，值为中文描述字符串。前端应直接读取 `error` 字段展示。

---

## 2. CLI 退出码（`agentcrew/crew/crew.py:82-89`）

| 码 | 键名 | 触发条件 |
|----|------|----------|
| **0** | `ok` | run/resume 终态为 COMPLETED；status / roles / clean / cancel / delete / serve 命令成功；无参数裸调用 |
| **1** | `failed` | run 结束但非 COMPLETED 且非 CANCELLED / INTERRUPTED（存在任务失败）；预算耗尽后优雅收尾 |
| **2** | `plan` | `PlanFailedError` —— 规划连续重拆未通过校验。注意：argparse 自身参数错误也会退 2（来源不同） |
| **3** | `config` | `--goal` / `--demo` 互斥或缺失；非法数值参数；Settings / catalog / 模型工厂错误；resume 配置不一致且未加 `--force`；run 目录不可读；delete 时 run 处于 RUNNING；非法 run_id 路径穿越 |
| **4** | `stalled` | `crew.stalled` 为真（调度无进展），判定优先于终局状态 |
| **130** | `cancelled` | `RunCancelled`（G3 卡点用户选 q / save_exit）；Ctrl-C 中断落盘 INTERRUPTED；入口二次 KeyboardInterrupt |

> 退出码常量定义于 `agentcrew/crew/crew.py`，`main.py` 的 `_HANDLERS` 分发后由 crew 返回的 `exit_code` 决定进程退出码。

---

## 3. 业务异常类

### 3.1 运行时与检查点

| 异常类 | 定义位置 | 用途 |
|--------|----------|------|
| `SchemaMismatchError` | `runtime/checkpoint.py` | run.json 的 `schema_version` 与当前代码不匹配 |

### 3.2 任务与状态机

| 异常类 | 定义位置 | 用途 |
|--------|----------|------|
| `IllegalTransitionError` | `task/state_machine.py` | 非法 TaskStatus 状态跃迁（违反 10 态跃迁表） |
| `PlanValidationError` | `task/planner.py` | 单次规划校验失败（可重试） |
| `PlanFailedError` | `crew/crew.py` | 规划重拆次数用尽（触发 CLI 退出码 2） |
| `CyclicGraphError` | `task/graph.py` | 任务依赖图存在环 |

### 3.3 运行控制

| 异常类 | 定义位置 | 用途 |
|--------|----------|------|
| `RunCancelled` | `crew/crew.py` | G3 门禁卡点用户取消 / save_exit（触发 CLI 退出码 130） |

### 3.4 模型与 Provider

| 异常类 | 定义位置 | 用途 |
|--------|----------|------|
| `ModelFactoryError` | `models/factory.py` | Provider 构造失败 / 别名非法（触发 CLI 退出码 3） |
| `ContextBudgetError` | `models/factory.py` | 上下文预算超出模型窗口 |
| `ProviderUnavailableError` | `models/factory.py` | 故障转移（failover）全部 Provider 耗尽 |
| `BudgetExhaustedError` | `models/factory.py` | 成本 / 调用次数硬上限（P0-P3 预算层级） |
| `UnknownProviderError` | `models/config.py` | 别名引用的 provider 未知 |
| `StructuredOutputError` | `models/structured.py` | 结构化输出修复重试失败 |

### 3.5 Agent 执行

| 异常类 | 定义位置 | 用途 |
|--------|----------|------|
| `AgentExecutionError` | `agents/base_agent.py` | Agent 执行 / 解析 / 契约 / 落盘失败 |

### 3.6 产物管理

| 异常类 | 定义位置 | 用途 |
|--------|----------|------|
| `ArtifactConflictError` | `artifacts/store.py` | 跨任务声明同一路径产物冲突 |
| `ArtifactTooLargeError` | `artifacts/store.py` | 产物超过 `max_bytes` 限制 |
| `UndeclaredArtifactError` | `artifacts/store.py` | Agent 产出了任务契约外的文件 |
| `ArtifactParseError` | `artifacts/parser.py` | 产物围栏块（fence block）解析失败 |

### 3.7 角色与工具

| 异常类 | 定义位置 | 用途 |
|--------|----------|------|
| `DuplicateRoleError` | `roles/catalog.py` | 角色名重复注册 |
| `UnknownRoleError` | `roles/catalog.py` | 引用了未注册的角色 |
| `MCPError` | `mcp/client.py` | MCP 子服务器通信 / 执行错误 |

### 3.8 通用工具

| 异常类 | 定义位置 | 用途 |
|--------|----------|------|
| `UnsafePathError` | `utils/files.py` | 路径穿越 / 非法路径（run_id 校验） |
| `TransientError` | `utils/retry.py` | Provider 可重试错误（触发重试逻辑） |
| `PermanentError` | `utils/retry.py` | Provider 不可重试错误（直接失败） |

---

## 4. IPC 错误约定（`studio/preload.js` / `studio/main.js`）

### 4.1 异步方法错误

所有 `window.studio` 异步方法（返回 Promise）在失败时 **reject**，reject 值为 `Error` 对象，`message` 字段包含中文或英文错误描述。前端应使用 `try/catch` 或 `.catch()` 处理。

### 4.2 同步方法错误

同步方法（如 `getSettings` 在配置缺失时）直接 `throw`，前端应使用 `try/catch` 包裹。

### 4.3 子进程推送通道错误

主进程通过以下 6 个推送通道向渲染进程发消息，错误场景通过 `proc:error` 通道传递：

| 通道 | 方向 | 用途 |
|------|------|------|
| `proc:log` | main → renderer | 子进程 stdout/stderr 行 |
| `proc:exit` | main → renderer | 子进程退出（含 code / signal） |
| `proc:run-dir` | main → renderer | 子进程 run 目录路径 |
| `proc:error` | main → renderer | 子进程启动失败 / 运行时错误 |
| `watch:ledger` | main → renderer | 被监视 run 的 ledger 新事件 |
| `watch:snapshot` | main → renderer | 被监视 run 的状态快照更新 |

### 4.4 未暴露的内部通道

`main.js:592` 注册了 `app:relaunch` 通道，但 preload.js **未暴露**给渲染进程，前端不可调用。

---

## 5. 跨层错误映射参考

| 业务场景 | 业务异常 | HTTP 状态 | CLI 退出码 |
|----------|----------|-----------|------------|
| 规划失败 | `PlanFailedError` | — | 2 |
| 配置错误 | `ModelFactoryError` / 参数非法 | 400 / 503 | 3 |
| 预算耗尽 | `BudgetExhaustedError` | — | 1 |
| 用户取消 | `RunCancelled` | — | 130 |
| 调度停滞 | —（crew.stalled） | — | 4 |
| run 不存在 | — | 404 | 3（delete 时） |
| run 运行中冲突 | — | 409 | 3（delete 时） |
| Provider 不可用 | `ProviderUnavailableError` | 500 / 503 | 1 |
| 非法状态跃迁 | `IllegalTransitionError` | 500 | 1 |

---

## 6. 数据格式约定（与错误处理相关）

| 维度 | 约定 | 实际代码偏差 |
|------|------|-------------|
| 时间 | ISO 8601 UTC | **D-14 起统一 aware UTC**（带时区标记：`run.json` 经 pydantic 序列化为 `Z`，ledger/心跳用 `isoformat` 为 `+00:00`）：run.json / tasks.json / 评审 / 审批 / ledger `ts` / 心跳 / 锁。存量 run 仍是 naive 本地时间（无后缀），读取端按本机时区补齐。run.json 用 `T` 分隔，ledger 的 `ts` 用空格分隔 |
| 金额 | USD decimal | `cost_usd` 为 float，6 位小数 |
| 耗时 | 毫秒 | 代码中 `duration_ms` 为整数毫秒 |
| Token | 整数 | `prompt_tokens` / `completion_tokens` / `total_tokens` 均为整数 |

> 前端解析时间字段要兼容三种形态：带偏移的 ISO（`2026-11-12T08:00:00+00:00`）、
> naive ISO（`2026-11-12T16:00:00`，按本地时区理解）、ledger 的空格分隔。
> **不要用字符串比较排序时间**——混存时字典序与实际时刻不一致（见 `studio/src/app/realtime.js`
> 的 `parseTs` 用法）。
