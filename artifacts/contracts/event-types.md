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

### `run_paused`（R4）
- **触发时机**：`DeferredPrompter` 在 G3 卡点抛 `ApprovalPending`，`Crew._suspend()` 把 run 落成 `awaiting_approval` 后正常退出。**不是终局**，不发 `run_finished`、不封版。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `goal` | string | 是 | 项目目标原文。 |
  | `run_id` | string | 是 | run 目录名。 |
  | `approval_id` | string | 是 | 本次挂起对应的审批单 id。 |
  | `checkpoint` | string | 是 | `plan` / `delivery`。 |
  | `status` | string | 是 | 恒为 `awaiting_approval`。 |
  | `exit_code` | integer | 是 | 挂起是正常中间态，复用 `ok=0`。 |

### `approval_requested`（R4）
- **触发时机**：与 `run_paused` 同一次 `_suspend()`，单独发一条便于 SSE/看板订阅待办。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `approval_id` | string | 是 | 审批单 id。 |
  | `checkpoint` | string | 是 | `plan` / `delivery`。 |
  | `context` | object | 是 | 给审批人看的上下文（任务 id、目标摘要、统计等），不含密钥。 |

### `peer_dialogue_enabled`（R5）
- **触发时机**：`Crew._start()` 在 `peer_dialogue_enabled` 打开时构造并下发 `PeerRouter` 后发一次（默认关闭时不发）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `run_id` | string | 是 | run 目录名。 |
  | `max_messages` | integer | 是 | 本次 run 的真实咨询次数上限。 |

> R4 的 `approval_decided` **只走 `on_event`（SSE / 看板）**，由 `ApiServer._decide_approval` 在 HTTP 写入裁决后推送，不写 `ledger.jsonl`；裁决本身的权威记录是 `approvals.jsonl`。`run_paused` 与两条审批事件同名也会推给 `on_event`，供 `RunState.status` 更新为 `awaiting_approval`。

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
- **触发时机**：每一层门禁裁决完成时——G1 static 由 Gatekeeper 发；G2 llm 由 Reviewer 发（带 `suggestions`）；G2 被 score 阈值旁路放行时 Gatekeeper 再补发一条 `verdict=approve`、`reviewer="<role>:score-bypass"` 的事件；R7 主管审核（`layer=supervisor`）同样由 Reviewer 发。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | |
  | `round` | integer | 是 | 评审轮次（从 1 起）。 |
  | `layer` | string | 是 | `"static"` / `"llm"` / `"human"` / `"supervisor"`（R7：派活的人验收）。 |
  | `verdict` | string | 是 | `"approve"` / `"reject"`。 |
  | `score` | number \| null | 是 | 0..1；static 层恒为 null。 |
  | `issues` | string[] | 是 | 驳回理由。 |
  | `suggestions` | string[] | 否 | 仅 layer=llm：不阻塞的额外意见。 |
  | `reviewer` | string | 是 | `"static:contract"` / 角色 key / `"<role>:score-bypass"` / `"human:delivery"`。 |

### `supervisor_review_skipped`（R7）
- **触发时机**：`supervisor_review_enabled` 打开但没有真的审——三种原因：自己派给自己、主管角色不在目录里、离线 mock 通道无夹具。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `reason` | string | 是 | `self_delegated` / `unknown_supervisor` / `mock_channel`。 |
  | `task_id` | string | 否 | 任务级跳过时有；`mock_channel` 是 run 级，没有任务。 |
  | `supervisor` | string | 否 | 派活者的角色 key（`mock_channel` 时缺省）。 |
  | `run_id` | string | 否 | 仅 `mock_channel`（run 装配期决定不启用）。 |

> 主管审核**成功**时不会有额外事件：它就是一条 `gate_result(layer="supervisor")`，与 G2 同构，便于按层统一统计。

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

### `agent_message`（P3-4 / R5）
- **触发时机**：Agent 的回复里出现 `<message to="…">` 围栏，且注入了路由（R5 起为 `peer_dialogue_enabled`）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | 提问方任务。 |
  | `role` | string | 是 | 提问方角色。 |
  | `from_role` | string | 是 | 同 `role`（保留字段，便于统一消费）。 |
  | `to_role` | string | 是 | 被问角色（或 `human`）。 |
  | `content` | string | 是 | 问题正文，截断到 300 字。 |

### `agent_consult`（R5）
- **触发时机**：被咨询的角色真的完成了一次模型调用并给出答复。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `role` | string | 是 | 被咨询角色。 |
  | `from_role` | string | 是 | 提问方角色。 |
  | `question` | string | 是 | 问题正文，截断到 300 字。 |
  | `reply_length` | integer | 是 | 答复字符数（不落全文，避免把 ledger 撑爆）。 |

### `agent_consult_failed`（R5）
- **触发时机**：被咨询方的模型调用抛异常（网络/鉴权等）。**不抛给提问方**，返回可继续的文案。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `role` | string | 是 | 被咨询角色。 |
  | `from_role` | string | 是 | 提问方角色。 |
  | `error` | string | 是 | `类型: 消息`，截断到 300 字。 |

### `agent_message_deduped`（R5）
- **触发时机**：同一 `(提问方, 目标, 问题摘要)` 第二次发问，直接复用上次答复、不花调用。
- **data**：`from_role` / `to_role`（均为 string）。

### `agent_message_exhausted`（R5）
- **触发时机**：`max_peer_messages` 配额用尽后仍有新的发问。
- **data**：`from_role` / `to_role` / `max_messages`（integer）。

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

### `subtask_rejected`（R8）
- **触发时机**：`subtask_creator` 返回 `None`（没建成）。与 `subtask_rejected_by_policy` 的区别：这条是 Agent 侧的通用留痕，那条是 Scheduler 四道闸的具体原因。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | 发起请求的任务。 |
  | `role` | string | 是 | 发起请求的角色。 |
  | `title` | string | 是 | 请求的子任务标题。 |
  | `reason` | string | 是 | 拒绝原因；回调没提供时为空串（裸闭包用法）。 |

### `subtask_rejection_fed_back`（R8）
- **触发时机**：拒绝原因被回灌给模型、准备重问一次（仅在 `max_subtask_corrections > 0` 且回调提供了原因时）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | 发起请求的任务。 |
  | `role` | string | 是 | 发起请求的角色。 |
  | `reasons` | string[] | 是 | 本轮被拒的全部原因。 |

### `subtask_malformed`
- **触发时机**：`<subtask>` 围栏 JSON 解析失败（记一条留痕后跳过，不拖垮主任务）。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `task_id` | string | 是 | 输出坏围栏的任务。 |
  | `role` | string | 是 | |
  | `raw` | string | 是 | 原始坏 JSON 文本。 |

### `subtask_rejected_by_policy`（R6）
- **触发时机**：`Scheduler._delegation_rejection` 的四道闸任一不放行（角色不存在 / 非经理越权 / 层数超限 / 总数超限）。**与 `subtask_rejected` 的区别**：后者是 Agent 侧「回调返回 None」的通用留痕，这条带具体原因。
- **data**：
  | 字段 | 类型 | 必填 | 含义 |
  |---|---|---|---|
  | `parent_id` | string | 是 | 发起委派的任务 id。 |
  | `role` | string | 是 | 请求派给的角色。 |
  | `title` | string | 是 | 请求的子任务标题。 |
  | `reason` | string | 是 | 人类可读的拒绝原因（含可用角色清单 / 上限值）。 |

### `subtask_role_unknown`（R6）
- **触发时机**：`<subtask>` 的 `role` 不在角色目录里，且不是显式的 `generic`。
- **data**：`parent_id`（string）、`role`（string）、`available`（array of string，当前可用角色 key）。

### `subtask_unauthorized`（R6）
- **触发时机**：`delegation_mode=manager_only` 且父任务角色不是 `project_manager` / `product_manager`。
- **data**：`parent_id`、`role`（父任务角色）、`requested_role`（请求派给谁）。

### `subtask_depth_exceeded`（R6）
- **触发时机**：新任务的委派层数超过 `max_delegation_depth`（根任务算第 0 层）。
- **data**：`parent_id`、`depth`（新任务将会是第几层）、`max_depth`。

### `subtask_quota_exceeded`（R6）
- **触发时机**：本次 run 的动态子任务总数已达 `max_dynamic_tasks`。
- **data**：`parent_id`、`used`、`max_tasks`。

### `consult_experience_captured`（R6）
- **触发时机**：`peer_consult_capture_enabled` 打开，且一次真实咨询给出了非占位答复，已写入 `output/knowledge/experiences.jsonl`。
- **data**：`from_role`、`to_role`、`experience_id`。

### `consult_capture_failed`（R6）
- **触发时机**：沉淀回调抛异常（磁盘满 / 权限等）。**不影响本次咨询**，返回值照常交付。
- **data**：`from_role`、`to_role`、`error`（`类型: 消息`，截断 300 字）。

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
