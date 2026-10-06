# Ledger 事件类型文档（event-types.md）

> 权威来源：`agentcrew/runtime/ledger.py:Ledger.emit` 与代码中所有 `ledger.emit(...)` 调用点。
> 信封固定为 `{ts, run_id, event, data}`（见 schemas/event.json）。本文逐事件列出 `data` 字段。
>
> **单位约定**：金额 = USD decimal（number，浮点）；耗时 = 毫秒（integer）；token = 整数（integer）。
> 时间戳在外层 `ts`，naive 本地时间，格式 `YYYY-MM-DD HH:MM:SS.ffffff`（空格分隔）。
>
> **区分 ledger 事件 vs 看板事件**：`Scheduler._notify(...)` 发出的 `task_dispatched` / `task_completed` / `task_regated` / `scheduler_cancelled` / `deadlock` **只走 on_event 回调（SSE/终端看板），不写 ledger.jsonl**，本文不收录。

---

## 1. Run 生命周期

### `run_started`
- **触发时机**：`Crew._start()` 装配完资源后立刻发出（resume 也会再发一条）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `goal` | string | 是 | 项目目标原文。 |

### `run_finished`
- **触发时机**：`Crew._finalize()` 封版时发出（任何终态都发）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `goal` | string | 是 | 项目目标原文。 |
  | `status` | string | 是 | 终态 RunStatus 值：`completed` / `failed` / `cancelled` / `interrupted`。 |
  | `exit_code` | integer | 是 | 退出码（见 state-enums.md §5.6）。 |
  | `duration_ms` | integer | 是 | run 总耗时，毫秒。 |

---

## 2. 规划阶段

### `plan_created`
- **触发时机**：PM 拆解通过 `validate_plan` 后发出。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_count` | integer | 是 | 任务总数（含引擎追加的 T-FINAL）。 |
  | `roles` | string[] | 是 | 本计划用到的角色 key（升序去重）。 |
  | `planner_attempts` | integer | 是 | 本次是第几次规划尝试（从 1 起）。 |

### `plan_rejected`
- **触发时机**：拆解未通过 `validate_plan`，错误回灌 PM 重拆时。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `attempt` | integer | 是 | 第几次尝试（1-based）。 |
  | `errors` | string[] | 是 | 本次校验失败的全部错误。 |

### `plan_scored`
- **触发时机**：校验通过后 `score_plan` 打质量分（P2-6）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `total` | number | 是 | 总分（0..1，round 4 位）。 |
  | `coverage` | number | 是 | 目标覆盖维度分。 |
  | `specificity` | number | 是 | 验收标准具体性分。 |
  | `dependency_quality` | number | 是 | 依赖结构分。 |
  | `deliverable_clarity` | number | 是 | 交付物清晰度分。 |

### `plan_low_quality`
- **触发时机**：`total < MIN_PLAN_SCORE`（只警告不阻断）。
- **data**：同 `plan_scored`，额外带 `min_score`（number，触发阈值）。

### `plan_incremental_edit`
- **触发时机**：G3 计划卡点选 `edit` 且走增量编辑子菜单时，**每条 add/remove/modify 各发一条**（P2-7）。
- **data**：字段由 `decision.incremental_edits` 的 spread 决定，典型为 `{action, task_id?, title?, ...}`（全量编辑器路径不发此事件）。

### `human_decision`
- **触发时机**：G3 计划卡点或交付卡点做出选择后（仅当 `gate` 模式开启对应卡点）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `checkpoint` | string | 是 | `"plan"` 或 `"delivery"`。 |
  | `choice` | string | 是 | 计划卡点：`approve`/`edit`/`save_exit`/`cancel`；交付卡点：`accept`/`rework`/`abort`。 |
  | `task_ids` | string[] | 是 | 相关任务 id（打回场景为被打回的任务）。 |

---

## 3. 任务执行

### `task_started`
- **触发时机**：`BaseAgent.execute()` 开头（每次尝试，含返工重跑）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | |
  | `role` | string | 是 | 执行角色 key。 |
  | `attempt` | integer | 是 | 第几次执行（= task.attempts）。 |

### `task_finished`
- **触发时机**：产物成功落盘后（失败路径不发，失败走 `error` 事件）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | |
  | `role` | string | 是 | |
  | `attempt` | integer | 是 | |
  | `duration_ms` | integer | 是 | 执行耗时，毫秒。 |

### `task_transition`
- **触发时机**：`Scheduler._transition()` 发起的每次状态机跃迁（即所有经 `TaskBoard.transition()` 的跃迁）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | |
  | `from` | string | 是 | 源 TaskStatus 值。 |
  | `to` | string | 是 | 目标 TaskStatus 值。 |
  | `reason` | string | 是 | 跃迁原因（见 state-enums.md §2.1/2.2）。 |

### `task_notes`
- **触发时机**：Agent 围栏块之外有说明文字时（非空才发）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | |
  | `role` | string | 是 | |
  | `attempt` | integer | 是 | |
  | `notes` | string | 是 | 说明原文。 |

---

## 4. 门禁

### `gate_result`
- **触发时机**：每一层门禁裁决完成时——G1 static 由 Gatekeeper 发；G2 llm 由 Reviewer 发（带 `suggestions`）；G2 被 score 阈值旁路放行时 Gatekeeper 再补发一条 `verdict=approve`、`reviewer="<role>:score-bypass"` 的事件。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | |
  | `round` | integer | 是 | 评审轮次（从 1 起）。 |
  | `layer` | string | 是 | `"static"` / `"llm"` / `"human"`。 |
  | `verdict` | string | 是 | `"approve"` / `"reject"`。 |
  | `score` | number \| null | 是 | 0..1；static 层恒为 null。 |
  | `issues` | string[] | 是 | 驳回理由。 |
  | `suggestions` | string[] | 否 | 仅 layer=llm：不阻塞的额外意见。 |
  | `reviewer` | string | 是 | `"static:contract"` / 角色 key / `"<role>:score-bypass"` / `"human:delivery"`。 |

---

## 5. 产物

### `artifact_written`
- **触发时机**：`ArtifactStore.commit()` 落盘一个产物文件后。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | 产出任务。 |
  | `path` | string | 是 | 相对 artifacts/ 的 POSIX 路径。 |
  | `size_bytes` | integer | 是 | 文件字节数。 |
  | `sha256` | string | 是 | 内容 SHA-256 十六进制。 |

### `manifest_repaired`
- **触发时机**：resume 时 `load_manifest()` 发现 MANIFEST.json 里有指向不存在文件的幽灵记录并丢弃。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `path` | string | 是 | MANIFEST.json 路径。 |
  | `dropped` | string[] | 是 | 被丢弃的条目相对路径列表。 |

---

## 6. LLM 调用

### `llm_call`
- **触发时机**：每次成功的 LLM 调用（`ModelFactory._account()`），含 PM 拆解、Agent 执行、G2 评审、结构化输出阶梯的每次内部调用。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `role` | string | 是 | 调用方角色 key。 |
  | `model` | string | 是 | 实际模型名（如 `deepseek-chat`）。 |
  | `prompt_tokens` | integer | 是 | 输入 token。 |
  | `completion_tokens` | integer | 是 | 输出 token。 |
  | `estimated_cost_usd` | number | 是 | 本次调用估算成本，USD。 |
  | `latency_ms` | number | 是 | 调用耗时，毫秒（mock 模式常为 0）。 |
  | `purpose` | string | 是 | `"plan"` / `"execute"` / `"review"` 等。 |

### `llm_retry`
- **触发时机**：Provider 内部即将重试（429/503/网络抖动），Retry-After 被截断到 300s 时尤其重要。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `attempt` | integer | 是 | 第几次重试。 |
  | `delay_seconds` | number | 是 | 实际等待秒数。 |
  | `error` | string | 是 | 原始错误（`Type: message`）。 |
  | `retry_after_clamped_from` | number | 否 | 若 Retry-After 被截断，记录服务端原始要求秒数。 |

### `llm_failover`
- **触发时机**：主别名 Provider 不可用，切到下一个备用别名时（P0-2）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `from_alias` | string | 是 | 失败别名。 |
  | `to_alias` | string | 是 | 切到的备用别名。 |
  | `error` | string | 是 | 触发故障转移的错误。 |
  | `from_status` | string | 否 | 健康监控启用时：失败别名健康状态。 |
  | `to_status` | string | 否 | 健康监控启用时：目标别名健康状态。 |

### `llm_degraded`
- **触发时机**：结构化输出阶梯因能力降级/修复失败下降级别时（spec §12.3）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `provider` | string | 是 | Provider 名。 |
  | `from_level` | string | 是 | 起始能力级别。 |
  | `to_level` | string | 是 | 下降后的级别。 |
  | `reason` | string | 是 | 降级原因。 |

### `llm_routed`
- **触发时机**：P3-10 权重路由选中一个别名时。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `selected_alias` | string | 是 | 选中的别名。 |
  | `reason` | string | 是 | `"weighted"` / `"health_adjusted"` / `"fallback_first"`。 |
  | `candidates` | string[] | 是 | 候选别名池。 |
  | `weights` | object | 是 | alias → 实际权重（健康调整后）。 |

### `llm_model_switched`
- **触发时机**：运行时切换模型绑定（`ModelFactory.set_default_alias` / `set_role_binding`）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `from_alias` | string \| null | 是 | 旧别名（首次设置时可能为 null）。 |
  | `to_alias` | string | 是 | 新别名。 |
  | `scope` | string | 是 | `"default"` 或 `"role:<role>"`。 |

### `model_switched`
- **触发时机**：`Crew.switch_model()` API 调用后（与 `llm_model_switched` 互补，Crew 层视角）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `from_alias` | string \| null | 是 | |
  | `to_alias` | string | 是 | |
  | `role` | string \| null | 是 | null = 切默认模型；否则切角色绑定。 |

### `provider_health`
- **触发时机**：P2-11 主动健康探测每次完成（后台循环）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `alias` | string | 是 | 被探测别名。 |
  | `status` | string | 是 | `"healthy"` / `"degraded"` / `"unavailable"`。 |
  | `latency_ms` | number \| null | 是 | 探测延迟。 |
  | `error` | string \| null | 是 | 失败时的错误。 |
  | `consecutive_failures` | integer | 是 | 连续失败次数。 |

### `mock_fallback`
- **触发时机**：MockProvider 找不到匹配 fixture 时（spec §16.1，绝不静默）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `message` | string | 是 | 替换/降级说明。 |

---

## 7. 工具调用（ReAct, P1-1）

### `tool_call`
- **触发时机**：Agent 在 ReAct 循环里执行一次工具调用（成功/失败都发）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | |
  | `role` | string | 是 | |
  | `tool_name` | string | 是 | 工具名。 |
  | `args` | object | 是 | 入参摘要（`_args_summary` 截断后的）。 |
  | `success` | boolean | 是 | 是否成功执行。 |
  | `duration_ms` | integer | 是 | 工具执行耗时，毫秒。 |

### `tool_loop_exhausted`
- **触发时机**：ReAct 循环撞 `max_tool_calls` 上限仍在发 tool_call（降级把最后回复当产物，不判失败）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | |
  | `role` | string | 是 | |
  | `max_tool_calls` | integer | 是 | 配置的最大轮数。 |

---

## 8. 动态子任务（P2-1）

### `subtask_created`
- **触发时机**：Agent 在回复中输出合法 `<subtask>` 围栏，经 `Scheduler._create_subtask` 入板后。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `parent_id` | string | 是 | 父任务 id。 |
  | `child_id` | string | 是 | 新子任务 id（`T-DYN-xxx`）。 |
  | `title` | string | 是 | 子任务标题。 |
  | `role` | string | 是 | 子任务角色。 |

### `subtask_malformed`
- **触发时机**：`<subtask>` 围栏 JSON 解析失败（记一条留痕后跳过，不拖垮主任务）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | 输出坏围栏的任务。 |
  | `role` | string | 是 | |
  | `raw` | string | 是 | 原始坏 JSON 文本。 |

---

## 9. 预算与错误

### `budget_exhausted`
- **触发时机**：`ModelFactory._account()` 检查累计成本或调用次数超硬上限（P0-3），Crew 捕获后优雅收尾前。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `kind` | string | 是 | `"cost_usd"` 或 `"llm_calls"`。 |
  | `current` | number | 是 | 触发时的累计值。 |
  | `limit` | number | 是 | 配置的硬上限。 |
  | `total_cost_usd` | number | 是 | 本次 run 累计成本。 |
  | `llm_calls` | integer | 是 | 本次 run 累计调用数。 |

### `error`
- **触发时机**：任务执行或门禁评估抛异常（收敛成错误结局回流时）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `where` | string | 是 | `"agent.execute"` 或 `"gate.evaluate"`。 |
  | `task_id` | string | 是 | |
  | `exc_type` | string | 是 | 异常类名（从错误串 `": "` 前取）。 |
  | `message` | string | 是 | 完整错误串。 |

---

## 事件触发矩阵（真实 demo run `20261006-183534-fa97` 实测）

| 事件 | 次数 |
|---|---|
| artifact_written | 25 |
| llm_call | 21 |
| gate_result | 19 |
| task_started | 10 |
| task_notes | 10 |
| task_finished | 10 |
| task_transition | 10 |
| run_started | 1 |
| plan_created | 1 |
| plan_scored | 1 |
| run_finished | 1 |

其余事件（plan_rejected / llm_failover / llm_retry / tool_call / subtask_* / budget_exhausted / error / human_decision / manifest_repaired / provider_health / mock_fallback 等）在 `--mock --auto` 正常路径下不触发，但代码路径已定义，见上文。
