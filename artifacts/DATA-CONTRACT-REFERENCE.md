# AgentCrew 数据契约参考（基于真实运行产物逆向）

> 来源：`output/20261006-183534-fa97/`（Todo List Web 应用 demo run，mock 模式，9 任务全成功）
> 原始数据副本：`artifacts/reference-run/`（run.json / tasks.json / ledger.jsonl / decisions.jsonl）
> 本文档供契约 Agent 校验 Schema、前端 Agent 构造 Mock 数据使用。

## 1. run.json（运行快照）

```json
{
  "run_id": "20261006-183534-fa97",
  "schema_version": 1,
  "project_goal": "string",
  "status": "completed",
  "created_at": "2026-10-06T18:35:34.060583",
  "updated_at": "2026-10-06T18:35:34.886545",
  "config_snapshot": {
    "default_model": "deepseek:deepseek-chat",
    "max_concurrency": 4,
    "context_budget_tokens": 24000,
    "gate": "none",
    "output_dir": "E:\\agentcrew\\output",
    "log_level": "INFO",
    "mock": true,
    "mock_latency_ms": 0,
    "plan_max_attempts": 2,
    "role_bindings": {},
    "roles_dir": null,
    "provider_overrides": {},
    "model_tiers": {},
    "model_failover": {},
    "mcp_servers": {},
    "review_score_threshold": null,
    "max_cost_usd": null,
    "max_llm_calls": null,
    "health_check_enabled": true,
    "health_check_interval_secs": 30
  },
  "stats": {
    "total_tasks": 9,
    "succeeded": 9,
    "failed": 0,
    "blocked": 0,
    "skipped": 0,
    "llm_calls": 21,
    "prompt_tokens": 101175,
    "completion_tokens": 17825,
    "estimated_cost_usd": 0.04692475,
    "started_at": "2026-10-06T18:35:34.060583",
    "finished_at": "2026-10-06T18:35:34.886543"
  }
}
```

### RunStatus 枚举（实际观测值）
- `preparing` — 初始化中
- `planning` — PM 拆解任务中
- `awaiting_approval` — 等待计划审查
- `running` — 执行中
- `paused` — 已暂停
- `failed` — 运行失败
- `completed` — 已完成
- `cancelled` — 已取消

### RunPhase 枚举
- `planning` — 规划阶段
- `execution` — 执行阶段
- `delivery` — 交付阶段

---

## 2. tasks.json（任务图快照）

顶层结构：`{ "tasks": [ ... ] }`

### 单个 Task 完整字段

```json
{
  "id": "T-001",
  "title": "产品需求与验收标准",
  "description": "string",
  "brief": "string (一句话摘要)",
  "role": "product_manager",
  "priority": 1,
  "timeout_seconds": 300,
  "max_attempts": 3,
  "max_reworks": 2,
  "attempts": 1,
  "rework_count": 0,
  "status": "succeeded",
  "dependencies": [],
  "parent_id": null,
  "dynamic": false,
  "acceptance_criteria": ["string", "..."],
  "deliverables": [
    {
      "path": "docs/PRD.md",
      "kind": "markdown",
      "description": "string",
      "required": true,
      "min_bytes": 600,
      "max_bytes": 524288,
      "must_contain": ["目标用户", "功能清单"]
    }
  ],
  "result": {
    "task_id": "T-001",
    "role": "product_manager",
    "summary": "string",
    "notes": "string",
    "artifacts": [
      {
        "path": "docs/PRD.md",
        "kind": "markdown",
        "size_bytes": 2730,
        "sha256": "6d46aeaf...",
        "produced_by_task": "T-001",
        "produced_by_role": "product_manager",
        "written_at": "2026-10-06T18:35:34.116697"
      }
    ],
    "started_at": "2026-10-06T18:35:34.106611",
    "finished_at": "2026-10-06T18:35:34.126086",
    "duration_ms": 19
  },
  "reviews": [
    {
      "task_id": "T-001",
      "round": 1,
      "layer": "static",
      "verdict": "approve",
      "score": null,
      "issues": [],
      "fix_hints": [],
      "reviewer": "static:contract",
      "created_at": "2026-10-06T18:35:34.139261"
    }
  ],
  "status_history": [
    { "from_status": "pending", "to_status": "ready", "at": "..." },
    { "from_status": "ready", "to_status": "running", "at": "..." }
  ],
  "created_at": "2026-10-06T18:35:34.088261",
  "updated_at": "2026-10-06T18:35:34.153048"
}
```

### TaskStatus 枚举（10 态状态机，来自 state_machine.py）
- `pending` — 已创建，未就绪
- `ready` — 依赖满足，可派发
- `running` — Agent 执行中
- `review` — 执行完成，门禁审查中
- `rework` — 门禁驳回，返工中
- `blocked` — 依赖失败，被阻塞
- `succeeded` — 成功完成
- `failed` — 失败（耗尽 attempts）
- `skipped` — 跳过
- `cancelled` — 已取消

### 8 个角色（role 字段值）
`project_manager`, `product_manager`, `architect`, `backend_dev`, `frontend_dev`, `qa_engineer`, `devops_engineer`, `technical_writer`

### GateLayer 枚举
- `static` — G1 静态校验（文件存在/大小/语法/must_contain）
- `llm` — G2 LLM 评审（按验收标准逐条判定）
- `delivery` — G3 交付确认（人工）

### GateVerdict 枚举
- `approve` — 通过
- `reject` — 驳回（触发返工）

---

## 3. ledger.jsonl（追加式事件账本）

每行一个 JSON 对象，格式统一：

```json
{
  "ts": "2026-10-06 18:35:34.076329",
  "run_id": "20261006-183534-fa97",
  "event": "run_started",
  "data": { ... }
}
```

> 注意：`ts` 格式是 `"YYYY-MM-DD HH:MM:SS.ffffff"`（空格分隔，非 ISO T 分隔），无时区后缀（本地时间）。

### 事件类型清单（按出现顺序）

| event | data 字段 | 说明 |
|---|---|---|
| `run_started` | `goal` | Run 开始 |
| `llm_call` | `role`, `model`, `prompt_tokens`, `completion_tokens`, `estimated_cost_usd`, `latency_ms`, `purpose` | 一次 LLM 调用（purpose: plan/execute/review/summarize） |
| `plan_created` | `task_count`, `roles[]`, `planner_attempts` | PM 拆解完成 |
| `plan_scored` | `total`, `coverage`, `specificity`, `dependency_quality`, `deliverable_clarity` | 计划质量评分（P2-6） |
| `task_started` | `task_id`, `role`, `attempt` | 任务开始执行 |
| `task_notes` | `task_id`, `role`, `attempt`, `notes` | Agent 执行笔记 |
| `artifact_written` | `task_id`, `path`, `size_bytes`, `sha256` | 产物文件写入 |
| `task_finished` | `task_id`, `role`, `attempt`, `duration_ms` | 任务执行完成（进入 review） |
| `task_transition` | `task_id`, `from`, `to`, `reason` | 任务状态跃迁 |
| `gate_result` | `task_id`, `round`, `layer`, `verdict`, `score`, `issues[]`, `reviewer` | 门禁审查结果 |
| `run_finished` | `goal`, `status`, `exit_code`, `duration_ms` | Run 结束 |

### 其他可能出现的事件（来自代码，本 run 未触发）
- `llm_failover` — Provider 故障转移（P0-2）
- `budget_exhausted` — 预算耗尽（P0-3）
- `budget_warning` — 预算预警
- `task_timeout` — 任务超时（P1-4）
- `tool_call` — Agent 调用工具（P1-1 ReAct）
- `llm_model_switched` — 运行时切换模型（P1-6）
- `provider_health` — Provider 健康状态（P2-11）
- `llm_routed` — 权重路由选择（P3-10）
- `checkpoint_saved` — 检查点保存
- `human_approved` — 人工批准
- `run_cancelled` — Run 被取消

---

## 4. 时间/金额/Token 约定

| 字段类型 | 格式 | 示例 |
|---|---|---|
| run.json 时间 | ISO 8601（T 分隔，微秒） | `2026-10-06T18:35:34.060583` |
| ledger 时间 | 空格分隔，微秒，本地时间 | `2026-10-06 18:35:34.076329` |
| 耗时 | 毫秒（整数） | `duration_ms: 19` |
| 金额 | USD decimal（浮点） | `estimated_cost_usd: 0.04692475` |
| Token | 整数 | `prompt_tokens: 101175` |
| 延迟 | 毫秒（整数） | `latency_ms: 0` |

---

## 5. 产物目录结构

```
output/<run_id>/
├── run.json              # 运行快照
├── tasks.json            # 任务图快照
├── ledger.jsonl          # 事件账本（追加式）
├── context/
│   └── decisions.jsonl   # 决策记录（DECISION: 行提取）
└── artifacts/            # 所有产物文件
    ├── DELIVERY.md       # T-FINAL 交付摘要
    ├── MANIFEST.json     # 产物清单
    ├── README.md
    ├── Dockerfile
    ├── docker-compose.yml
    ├── backend/          # 后端代码
    ├── frontend/         # 前端代码
    ├── docs/             # 文档（PRD/架构/API spec/验收/部署/测试报告）
    └── tests/            # 测试代码
```

---

## 6. 关键注意事项（前端必看）

1. **result 字段可能为 null** — 任务未执行完成时 `result` 不存在或为 null，前端必须处理。
2. **reviews 数组可能为空** — 未经过门禁的任务没有 reviews。
3. **score 可能为 null** — static 层门禁没有 score，只有 llm 层有。
4. **issues 可能为空数组** — approve 时 issues=[]，reject 时 issues 含具体问题。
5. **deliverables 是预期产物，result.artifacts 是实际产物** — 两者要区分显示。
6. **T-FINAL 是特殊收尾任务** — role=project_manager，依赖所有其他任务，priority=9。
7. **ledger 的 ts 无时区** — 前端显示时需注意，建议按本地时间解析。
8. **config_snapshot 字段很多** — 大部分为 null/空对象，前端只展示关键字段（default_model, max_concurrency, mock, max_cost_usd）。
