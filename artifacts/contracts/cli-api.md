# AgentCrew — CLI 接口契约

逆向自 `main.py`（argparse 定义 + 分派）与 `agentcrew/crew/crew.py`（EXIT_CODES）、`agentcrew/config.py`（环境变量映射）。
本文件**只读分析**，未修改任何源码。

- 入口：`python main.py <subcommand> [options]`
- 顶层 description：`AgentCrew：8 角色多 Agent 协作平台（规划 → 执行 → 三层门禁 → 交付）`（main.py:148）
- 无任何子命令时：打印 help + 已注册角色数 + 最近 3 次 run 概览，**退出码 0**（main.py:231–233, 243–269）。

> 注意：`build_parser()` 的 metavar 字符串（main.py:151）写作 `{run,resume,status,roles,clean,cancel,serve}`，**漏写了 `delete`**；但 `delete` 实际通过 `add_parser('delete', ...)`（main.py:185）注册并在 `_HANDLERS`（main.py:637–646）登记。**真实子命令共 8 个**。

---

## 一、子命令清单（8 个）

| 子命令 | 处理函数 | 作用 |
|---|---|---|
| `run` | `_cmd_run` (main.py:275) | 执行一次完整 run（规划→执行→交付） |
| `resume` | `_cmd_resume` (main.py:314) | 续跑中断/未完成的 run |
| `status` | `_cmd_status` (main.py:442) | 打印某 run 的记录 + 任务表 |
| `roles` | `_cmd_roles` (main.py:485) | 列出 8 个内置角色 |
| `clean` | `_cmd_clean` (main.py:507) | 保留最近 N 个 run，删除更旧的 |
| `cancel` | `_cmd_cancel` (main.py:533) | 给离线 run 打 cancelled 标记 |
| `delete` | `_cmd_delete` (main.py:555) | 删除单个 run（不可恢复；running 拒绝） |
| `serve` | `_cmd_serve` (main.py:608) | 启动 HTTP API + Web UI 面板 |

---

## 二、各子命令参数

### `run` — 执行一次完整 run（main.py:153–158, 200–220）

| 参数 | 类型 | 必填 | 默认 | help / 含义 |
|---|---|---|---|---|
| `--goal TEXT` | string | 与 `--demo` 二选一 | 无 | 项目目标 |
| `--demo` | flag (store_true) | 与 `--goal` 二选一 | false | 用 `examples/todo_web_project.py` 的内置 Todo Web 目标 |
| `--mock` | flag | 否 | false | 强制 mock provider（覆盖所有角色绑定，无需 API Key） |
| `--auto` | flag | 否 | false | 等价 `--gate none`（全自动，跳过人工闸门） |
| `--gate {both,plan,delivery,none}` | choice | 否 | 配置层默认 | 人工闸门模式；含人工卡点但无 TTY 时降级为 none |
| `--max-concurrency N` | int (≥1) | 否 | 配置默认 4 | 全局并发上限（含 LLM 调用） |
| `--default-model ALIAS` | string | 否 | `openai:gpt-4o-mini` | 默认模型别名，形如 `provider:model` |
| `--output-dir DIR` | path | 否 | 配置默认 `output` | run 输出根目录 |
| `--context-budget N` | int (≥1) | 否 | 24000 | 上下文预算（tokens） |
| `--max-cost USD` | float (>0) | 否 | 无 | 累计 LLM 成本硬上限（USD），超限优雅收尾（P0-3） |
| `--max-llm-calls N` | int (>0) | 否 | 无 | 累计 LLM 调用次数硬上限，超限优雅收尾（P0-3） |
| `--no-live` | flag | 否 | false | 关闭实时看板，只保留结束报告（CI 友好） |

- 互斥规则：`--goal` 与 `--demo` **同时给 / 都不给** → 报错 + 退出码 3（main.py:277–284）。
- 非法数值：`--max-concurrency <1` 或 `--context-budget <1` → 退出码 3（main.py:723–731）。
- 示例：
  ```
  python main.py run --goal "做一个待办网页" --mock --auto --no-live
  python main.py run --demo --mock --max-cost 1.0 --max-llm-calls 50
  ```

### `resume` — 续跑（main.py:160–164）

| 参数 | 类型 | 必填 | 默认 | 含义 |
|---|---|---|---|---|
| `run_id`（位置参数） | string | 是 | — | run 目录名（见 status/clean 输出） |
| `--force` | flag | 否 | false | 配置快照与当前配置不一致时仍继续 |
| `--mock` / `--auto` / `--gate` / `--max-concurrency` / `--default-model` / `--output-dir` / `--context-budget` / `--max-cost` / `--max-llm-calls` / `--no-live` | 同上 | 否 | 同上 | 与 `run` 共用 `_add_run_flags` |

- 配置可比性比对键：`default_model / role_bindings / provider_overrides / context_budget_tokens / mock`（main.py:74–80）。不一致且未 `--force` → 退出码 3（main.py:325–331）。
- 示例：`python main.py resume 20261006-abc123 --force --mock --auto --no-live`

### `status` — 查看 run（main.py:166–168）

| 参数 | 类型 | 必填 | 默认 | 含义 |
|---|---|---|---|---|
| `run_id`（位置） | string | 是 | — | run 目录名 |
| `--output-dir DIR` | path | 否 | 配置默认 | run 输出根目录 |

- 打印 run 记录、任务看板、ledger 事件数；无法读取 run → 退出码 3。
- 示例：`python main.py status 20261006-abc123`

### `roles` — 列角色（main.py:170）

- **无参数**。列出 8 个内置角色的 key/职责/边界/产出；成功退出码 0。
- 示例：`python main.py roles`

### `clean` — 清理旧 run（main.py:172–178）

| 参数 | 类型 | 必填 | 默认 | help |
|---|---|---|---|---|
| `--keep N` | int (≥0) | 否 | 5 | 保留的 run 个数 |
| `--dry-run` | flag | 否 | false | 只列出将删除的 run，不真删 |
| `--output-dir DIR` | path | 否 | 配置默认 | run 输出根目录 |

- `--keep <0` → 退出码 3（main.py:513–514）。
- 示例：`python main.py clean --keep 5 --dry-run`

### `cancel` — 离线取消标记（main.py:180–183）

| 参数 | 类型 | 必填 | 默认 | 含义 |
|---|---|---|---|---|
| `run_id`（位置） | string | 是 | — | run 目录名 |
| `--output-dir DIR` | path | 否 | 配置默认 | run 输出根目录 |

- run 已处于 `COMPLETED/FAILED/CANCELLED` → 提示无需取消，退出码 0（main.py:542–544）。
- 示例：`python main.py cancel 20261006-abc123`

### `delete` — 删除单个 run（main.py:185–190）

| 参数 | 类型 | 必填 | 默认 | help |
|---|---|---|---|---|
| `run_id`（位置） | string | 是 | — | 要删除的 run 目录名 |
| `--output-dir DIR` | path | 否 | 配置默认 | run 输出根目录 |
| `--force` | flag | 否 | false | 跳过确认提示（非交互/脚本用） |

- `RUNNING` 状态拒绝删除 → 退出码 3，提示先 `cancel`（main.py:570–574）。
- TTY 下默认询问 `[y/N]`；非 y/yes 则不删，退出码 0（main.py:591–598）。
- 示例：`python main.py delete 20261006-abc123 --force --output-dir E:/agentcrew/output`

### `serve` — HTTP API 服务（main.py:192–195）

| 参数 | 类型 | 必填 | 默认 | 含义 |
|---|---|---|---|---|
| `--host HOST` | string | 否 | `127.0.0.1` | 监听地址 |
| `--port PORT` | int | 否 | `8765` | 监听端口 |

- 启动 `ApiServer`，常驻直到 Ctrl-C；结束退出码 0。
- 暴露路由（main.py:620–623）：`GET /health`、`GET /runs/{id}/status`、`GET /runs/{id}/events`（SSE）、`POST /runs/{id}/events`。
- 示例：`python main.py serve --host 127.0.0.1 --port 8765`

---

## 三、环境变量（`AGENTCREW_*`）

配置合并优先级（低→高）：代码默认 → `.env`（python-dotenv）→ `agentcrew.yaml` → `AGENTCREW_*` 环境变量 → CLI 覆盖（config.py:4；main.py:652–677）。空值视为未设置。

### 直接映射（`agentcrew/config.py:38–52`）

| 环境变量 | Settings 字段 | 说明 |
|---|---|---|
| `AGENTCREW_DEFAULT_MODEL` | `default_model` | 默认模型别名（`provider:model`） |
| `AGENTCREW_MAX_CONCURRENCY` | `max_concurrency` | 全局并发上限（默认 4） |
| `AGENTCREW_CONTEXT_BUDGET` | `context_budget_tokens` | 上下文预算 tokens（默认 24000） |
| `AGENTCREW_GATE` | `gate` | `both/plan/delivery/none` |
| `AGENTCREW_OUTPUT_DIR` | `output_dir` | run 输出根目录（默认 `output`） |
| `AGENTCREW_LOG_LEVEL` | `log_level` | 日志级别（默认 INFO） |
| `AGENTCREW_MOCK` | `mock` | 全局 mock 开关 |
| `AGENTCREW_MOCK_LATENCY_MS` | `mock_latency_ms` | mock 调用固定延迟毫秒 |
| `AGENTCREW_REVIEW_SCORE_THRESHOLD` | `review_score_threshold` | G2 评审 score 阈值放行 |
| `AGENTCREW_MAX_COST_USD` | `max_cost_usd` | 累计成本硬上限（USD） |
| `AGENTCREW_MAX_LLM_CALLS` | `max_llm_calls` | 累计 LLM 调用次数硬上限 |
| `AGENTCREW_HEALTH_CHECK_ENABLED` | `health_check_enabled` | Provider 健康检查开关（默认 true） |
| `AGENTCREW_HEALTH_CHECK_INTERVAL` | `health_check_interval_secs` | 健康检查间隔秒（默认 30） |

### 前缀型（config.py:55, 67, 72）

| 环境变量 | 含义 |
|---|---|
| `AGENTCREW_MODEL_<ROLE>` | 按角色覆盖模型。`<ROLE>` 大写连下划线，8 个：`PROJECT_MANAGER / PRODUCT_MANAGER / ARCHITECT / BACKEND_DEV / FRONTEND_DEV / QA_ENGINEER / DEVOPS_ENGINEER / TECHNICAL_WRITER` |
| `AGENTCREW_TIER_<LEVEL>` | 按复杂度选模型：`LOW / MEDIUM / HIGH` → `model_tiers[<level>]` |
| `AGENTCREW_FAILOVER_<ALIAS>` | Provider 故障转移备用别名列表（逗号分隔）；别名中的 `:` 在变量名里写成 `__`，如 `AGENTCREW_FAILOVER_OPENAI__GPT-4O-MINI` |
| `AGENTCREW_CONFIG` | 指定 YAML 配置文件路径（默认读 `./agentcrew.yaml`，config.py:198） |
| `AGENTCREW_EDITOR` | 计划卡点 `[e]` 编辑用的编辑器（优先级高于 `$EDITOR`，cli_prompter.py:550） |

> Studio 子进程还会透传 `AGENTCREW_MOCK_LATENCY_MS`（renderer.js:1314，慢演示用 `'1200'`）。

---

## 四、退出码（`agentcrew/crew/crew.py:82–89`）

```python
EXIT_CODES = {
    "ok": 0, "failed": 1, "plan": 2,
    "config": 3, "stalled": 4, "cancelled": 130,
}
```

| 码 | 键 | 含义 | 触发条件（代码出处） |
|---|---|---|---|
| `0` | `ok` | 成功 | run/resume 正常结束且 `record.status is COMPLETED`（main.py:411–412）；status/roles/clean/cancel/delete/serve 成功；无子女命令的裸调用（main.py:233） |
| `1` | `failed` | 有任务失败 | run 结束但状态非 COMPLETED 且非 CANCELLED/INTERRUPTED（main.py:415）；预算耗尽 `BudgetExhaustedError` 收尾落 FAILED（crew.py:461–465） |
| `2` | `plan` | 计划拆解失败 | `PlanFailedError`：规划连续未通过校验（main.py:375–380） |
| `3` | `config` | 配置错误 | `_fail()` 的所有路径：`--goal/--demo` 互斥或缺失、非法数值、Settings 加载/校验失败、`build_catalog` 失败、`ModelFactoryError`（含 ContextBudgetError）、resume 配置快照不一致且未 `--force`、run 目录不可读、`delete` 时 run 处于 RUNNING、`clean --keep<0`、非法 run_id 路径穿越（main.py:277–291, 325–331, 381–384, 774–777 等） |
| `4` | `stalled` | 运行停滞 | `crew.stalled` 为真（调度卡死/无进展），判定优先于终局状态（main.py:409–410；crew.py:840–841） |
| `130` | `cancelled` | 用户取消 | `RunCancelled`（计划卡点选 q/save_exit）、Ctrl-C 中断（checkpoint 落盘 INTERRUPTED）、main 入口二次 KeyboardInterrupt（main.py:385–394, 237–240, 827–829） |

> Studio 主进程把这些码映射为可读中文（main.js:15–22）：0 成功 / 1 有任务失败 / 2 计划拆解失败 / 3 配置错误 / 4 运行停滞 / 130 用户取消。
> 注意：argparse 自身错误（未知参数等）仍按标准退出码 `2` 结束——这与本项目自定义的 `plan=2` 同值但来源不同，排查时需区分。
