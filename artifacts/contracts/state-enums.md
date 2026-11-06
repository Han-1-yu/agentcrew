# 状态枚举文档（state-enums.md）

> 本文档从代码逆向得到，**不是设计稿**。每个枚举的权威来源在括号内标出。
> 原则：`state_machine.py` 是 TaskStatus 跃迁表的唯一权威；`checkpoint.py:RunStatus` 是 run 级状态的唯一权威。
> 代码中**不存在** `AgentStatus` / `GateStatus` / `RunPhase` 枚举——下文逐一说明。

---

## 1. RunStatus（run 级状态）

**权威来源**：`agentcrew/runtime/checkpoint.py:RunStatus`（`str, Enum`）。
**持久化位置**：`run.json` 的 `status` 字段。

| 值 | 含义 | 触发条件 |
|---|---|---|
| `running` | run 正在执行（规划中或执行中）。 | `CheckpointStore.create()` 落盘初始状态；`_start()` 进入执行段时重置为 running。 |
| `interrupted` | Ctrl-C / 计划卡点 `save_exit` 落盘后的**可恢复断点**（不是 cancelled）。 | `asyncio.CancelledError` 被捕获时 `_finalize(..., INTERRUPTED)`；G3 计划卡点选 `save_exit`。 |
| `awaiting_approval` | R4 **非终态**：人工审批挂起，等待外部决策。 | `DeferredPrompter` 在 G3 卡点抛 `ApprovalPending` → `Crew._suspend()` 写状态、发 `run_paused` / `approval_requested`，**不封版、不发 `run_finished`**。 |
| `completed` | 全部任务 SUCCEEDED 且交付卡点接受。 | `_final_status()` 在无 failed/blocked/skipped/cancelled 时返回 COMPLETED。退出码 0。 |
| `failed` | 任一任务 failed/blocked/skipped，或规划失败（PlanFailedError）/ 预算耗尽 / 配置错误（ModelFactoryError）。 | `_final_status()`；异常路径见 `_start()` 的 except 分支。退出码 1（规划失败为 2、配置错误为 3、停滞为 4）。 |
| `cancelled` | 用户主动取消（G3 `abort` / 软取消 / `cancel` 子命令打标）。 | `_finalize(..., CANCELLED)`。退出码 130。 |

**RunStatus 没有显式跃迁表**——终局仍由 `_final_status()` 在收尾时一次性判定。R4 之后多了一条**非终局**路径：`running → awaiting_approval → running`，由 `_suspend()` 与 `resume()` 驱动；`awaiting_approval` 在删除/清理守卫里与 `running` 同等保护（不会被 `clean(protect_running=True)` 或 `delete_run` 抹掉）。

**RunPhase 不存在**：代码里没有独立的"阶段"枚举。规划/执行/交付三阶段是 `Crew._start()` 内的代码路径（`_plan_phase` / `_execute` / `human_delivery_gate`），不枚举。但 R4 把「挂起在哪个卡点」持久化成了 `approvals.jsonl` 里审批单的 `checkpoint ∈ {plan, delivery}`，`Crew.resume_from` 据此决定重入哪个卡点 —— 这是可恢复的路标，不是阶段枚举。

**resume 语义分叉**：`interrupted` / `running` 按 §13.3 直接重入执行（跳过规划与计划卡点）；`awaiting_approval` 必须回到原卡点，让 `DeferredPrompter` 消费已裁决的审批单。审批单缺失时 `resume_from` 直接报错，绝不悄悄绕过人工审批。

---

## 2. TaskStatus（任务状态，10 态）

**权威来源**：`agentcrew/task/task.py:TaskStatus` + `agentcrew/task/state_machine.py:ALLOWED_TRANSITIONS`。
**持久化位置**：`tasks.json` 每个 task 的 `status` 字段；历史跃迁在 `status_history[]`。

| 值 | 含义 |
|---|---|
| `pending` | 依赖未满足，尚未进入调度。 |
| `ready` | 依赖已满足，等待派发（含重试等待、返工等待——`take_ready()` 同时派发 ready 与 rework）。 |
| `running` | 已派发给 Agent，执行中（占任务槽位）。 |
| `review` | Agent 已提交产物，正在过门禁（G1 static → G2 llm）。 |
| `rework` | 被门禁驳回，等待带 fix_hints 再次执行。 |
| `succeeded` | **终态**：通过全部门禁。 |
| `failed` | **终态**：重试/返工额度用尽，或不可恢复错误。 |
| `blocked` | 上游永久失败（FAILED/CANCELLED）导致本任务无法开始；非终态（留人工恢复边）。 |
| `cancelled` | **终态**：用户取消。 |
| `skipped` | **终态**：上游被 SKIPPED 导致本任务跳过。 |

### 2.1 允许跃迁表（与 `ALLOWED_TRANSITIONS` 完全一致）

| 源状态 | 允许跃迁到 | 触发条件 |
|---|---|---|
| `pending` | `ready` | 全部依赖 SUCCEEDED（reason=`dependencies-met`）。 |
| `pending` | `blocked` | 任一传递依赖 FAILED/CANCELLED（reason=`upstream-failed`）。 |
| `pending` | `skipped` | 任一**非条件性**依赖 SKIPPED（reason=`upstream-skipped`）；R4 条件分支未命中时 reason=`condition-not-met`，沿分支传播时 reason=`conditional-skip-propagated`——这两种是**预期跳过**，`Crew._final_status` 不把它们判成失败。 |
| `pending` | `cancelled` | 用户取消。 |
| `ready` | `running` | `take_ready()` 取得槽位派发，先 `attempts+=1`（reason=`dispatched`）。 |
| `ready` | `blocked` | 派发前发现依赖已失败。 |
| `ready` | `cancelled` | 用户取消。 |
| `running` | `review` | Agent 正常返回且产物解析成功（reason=`executed`）。 |
| `running` | `ready` | 执行异常且 `attempts < max_attempts`（reason=`retry: <错误串>`）。 |
| `running` | `failed` | 执行异常且 `attempts >= max_attempts`（reason=`attempts-exhausted`）。 |
| `running` | `cancelled` | 硬取消（reason=`hard-cancel`）。 |
| `review` | `succeeded` | G1+G2 全 approve（reason=`gate-succeeded`）。 |
| `review` | `rework` | 任一门禁 reject 且 `rework_count < max_reworks`（reason=`gate-rework`；同时 `bump_rework`）。 |
| `review` | `failed` | 门禁 reject 且 `rework_count >= max_reworks`（reason=`gate-failed`）。 |
| `review` | `cancelled` | 用户取消。 |
| `rework` | `running` | 再次派发（reason=`rework-dispatched`），**不扣 attempts**。 |
| `rework` | `cancelled` | 用户取消。 |
| `blocked` | `ready` | 依赖事后全部变为 SUCCEEDED（reason=`dependencies-recovered`，人工修复上游后）。 |
| `succeeded` | （无出边） | 终态。 |
| `failed` | （无出边） | 终态；人工重试走 `TaskBoard.reset_failed()`，留 reason=`manual-retry` 的显式旁路记录。 |
| `cancelled` | （无出边） | 终态。 |
| `skipped` | （无出边） | 终态。 |

### 2.2 状态机之外的"显式旁路"（必须在 status_history 留痕）

| 旁路 | from → to | reason | 位置 |
|---|---|---|---|
| resume 修复 | `running` → `ready` | `resume-repair` | `checkpoint.repair_statuses()` |
| 人工重试 | `failed` → `ready` | `manual-retry` | `TaskBoard.reset_failed()` |
| 交付卡点打回成功任务 | `succeeded` → `ready` | `delivery-rework` | `Crew._apply_rework()` |
| 动态子任务入板 | `pending` → `ready` | `dynamic-created` | `TaskBoard.add_task()`（合成一条 PENDING→READY） |
| R4 条件分支未命中 | `pending` → `skipped` | `condition-not-met` / `conditional-skip-propagated` | `TaskBoard.refresh()`；汇合节点把条件性 SKIPPED 的依赖视为已满足 |

### 2.2b R4 条件分支（不改变十态状态机）

条件性是 **reason 层的语义**，没有新增 TaskStatus 值：Agent 用 `<outcome>标签</outcome>` 把结果标签写进 `TaskResult.outcome`；下游 `Task.when_outcomes` 声明命中的标签。未命中分支以 `condition-not-met` 进入 `skipped` 并沿分支传播 `conditional-skip-propagated`；依赖多条互斥分支的汇合任务只要有一条 `succeeded` 就继续。默认无标签/无条件时 `refresh()` 的四步结果与 R4 之前逐字一致。

### 2.3 resume 修复规则（`repair_statuses`）

- `running` → `ready`：进程死亡时产物可能只写了一半，必须重执行。
- `rework` 保持不动：`take_ready()` 本就派发它。
- `review` 保持：产物已完整落盘，由 Scheduler 的 `_regate_ids` 补派发到门禁（不占任务槽位）。
- `attempts` / `rework_count` **一律不重置**。

---

## 3. AgentStatus（不存在）

代码中**没有** `AgentStatus` 枚举。Agent 是无状态执行器，其"状态"完全由所执行任务的 `TaskStatus` 承载。外部可观测的 Agent 生命周期事件落在 ledger：`task_started` →（`tool_call`* / `llm_call`*）→ `task_finished`，随后由 `task_transition` 推进任务状态。看板/API 若需要"Agent 状态"，应由 `Task.status` + 是否有活跃 worker 派生（见 schemas/agent.json 的 `status` 字段：`queued`/`executing`/`reviewing`/`done`）。

---

## 4. GateStatus（不存在）

代码中**没有** `GateStatus` 枚举。门禁裁决由两层表达：
- 单次评审裁决：`ReviewRecord.verdict ∈ {approve, reject}`（见 schemas/gate.json）。
- 任务离开 REVIEW 后的去向：`Task.status ∈ {succeeded, rework, failed}`（由 Gatekeeper 跃迁）。
- 三层门禁：`ReviewRecord.layer ∈ {static (G1), llm (G2), human (G3), supervisor (R7 主管审核)}`。

G3 人工闸门的两个卡点选择：`PlanDecision.action ∈ {approve, edit, save_exit, cancel}`；`DeliveryDecision.action ∈ {accept, rework, abort}`。这些决策对象本身仍是一次性的，**但 R4 新增了持久化审批单**：`<run_dir>/approvals.jsonl` 中的 `ApprovalRequest` 有 `pending → decided → consumed` 三态，是「人在环」的可恢复实体；`CliPrompter`（interactive 模式）与 `DeferredPrompter`（deferred 模式）各自满足同一个 `Prompter` Protocol，前者不产生审批单。

---

## 5. 其他枚举（附带，非任务级状态）

### 5.1 ReviewRecord.layer / verdict
- `layer`: `static` / `llm` / `human` / `supervisor`（R7：动态委派任务由派活的人验收，审计上与常规 G2 分开统计）
- `verdict`: `approve` / `reject`

### 5.2 ArtifactKind（`agentcrew/artifacts/spec.py`）
`python` / `json` / `yaml` / `toml` / `markdown` / `text` / `html` / `css` / `js` / `dockerfile` / `sql` / `shell`

### 5.3 GateMode（`agentcrew/config.py`）
`both` / `plan` / `delivery` / `none`（控制 G3 两个卡点的开关）。

### 5.4 ComplexityLevel（`agentcrew/models/complexity.py`，V2 模型路由）
`low` / `medium` / `high`

### 5.5 HealthStatus（`agentcrew/models/factory.py:HealthRecord`，P2-11）
`healthy` / `degraded`（≥2s）/ `unavailable`

### 5.6 退出码（`agentcrew/crew/crew.py:EXIT_CODES`）
| key | code |
|---|---|
| ok | 0 |
| failed | 1 |
| plan | 2 |
| config | 3 |
| stalled | 4 |
| cancelled | 130 |
