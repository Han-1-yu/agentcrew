# 角色绑定多 Agent：设计方案

> 版本：v1 草案 · 2026-11-01
> 定位：把"一个角色 = 一个执行体"放宽为"角色 = 职责 + 权限，执行体可绑定 1..N 个 agent"。
> 约束：不破坏现有四级模型解析、契约验收、两层并发与禁用 fail-open 四条红线。

---

## 0. 一句话概括

**角色（Role）决定"该做什么、能碰什么"，Agent（Profile）决定"用谁做、怎么做"。**
一个角色可以绑定多个 agent，按 `duty` 分工，产物按"路径唯一所有者"归并，结果按 `aggregation` 汇总。

三个概念必须分清，否则后面全是歧义：

| 概念 | 含义 | 现有对应物 | 谁定义 |
| --- | --- | --- | --- |
| **Role** | 职责、边界、验收口径、权限上限 | `RoleSpec` | 角色目录（内置 8 + `roles.d`） |
| **Agent Profile** | 执行体：模型别名池、工具子集、职责范围、预算 | 目前只有"role 绑一个 model 别名" | 新增 `AgentProfile` |
| **Model** | 具体后端与型号 | `provider:model` 别名 | provider 预设 / 自定义 |

---

## 1. 现状与缺口

| 能力 | 现状 | 位置 | 缺口 |
| --- | --- | --- | --- |
| 角色数据化 | ✅ 纯数据 `RoleSpec`，新增角色不改引擎 | `agentcrew/roles/spec.py` | — |
| 角色→模型 | ✅ 四级优先：`RoleSpec.model` > `role_bindings[role]` > `model_tiers` > `default_model` | `models/factory.py:alias_for_task` | 只能 1:1，无法表达"主笔用便宜模型、复核用强模型" |
| Agent 实例 | ⚠️ 按角色缓存**单实例** | `agents/registry.py:get` | 同角色第二个 agent 无处安放 |
| 工具权限 | ✅ 角色级白名单 + 执行期闸门 | `tools/registry.py:enforce` | 粒度只到角色，无法按 agent 收窄 |
| 路由策略 | ✅ 池内 weighted/rotate/usage/order + 冷却 + 亲和 | `models/{routing,health,affinity}.py` | 策略按"别名池"生效，不区分同一角色内的职责 |
| 产物冲突 | ✅ 跨任务路径冲突检测 | `artifacts/store.py` | 没有"同角色多 agent 的路径认领" |
| 结果聚合 | ❌ | — | 多 agent 产出无合并/择优/投票机制 |
| 配置面 | ✅ YAML / env / CLI 三级合并 | `config.py` | 无 UI 绑定矩阵，无运行前预览 |

**结论**：不需要重写引擎，需要扩三个点——`AgentProfile` 数据结构、`AgentFactory` 的缓存键、调度器的 fan-out/aggregation。

---

## 2. 概念模型与数据结构

### 2.1 新增 `AgentProfile`

```python
class AgentProfile(BaseModel):
    """一个可被角色调用的执行体。不含职责（职责在 Role），只含"怎么干"。"""
    model_config = ConfigDict(extra="forbid")

    key: str                      # "backend_fast"
    title: str                    # "后端-快速档"
    model: str                    # 别名或别名池："deepseek:deepseek-chat"
    fallback: list[str] = []      # 复用现有 model_failover 语义
    tools: list[str] | None = None      # None = 继承角色；列出则取交集（只能收窄）
    path_scope: list[str] = []    # 允许写的前缀，如 ["backend/", "docs/api.md"]
    cost_ceiling_usd: float | None = None   # 该 agent 在单任务上的花费上限
    max_calls_per_task: int | None = None   # LLM 调用次数上限
    temperature: float | None = None        # None = 继承角色
    max_output_tokens: int | None = None
```

### 2.2 新增 `RoleAgentBinding`

```python
class Duty(str, Enum):
    PRIMARY = "primary"     # 主笔：产出契约内的产物
    SECTION = "section"     # 分工：只负责一部分产物/章节
    REVIEW = "review"       # 复核：只出意见，不落盘
    JUDGE = "judge"         # 裁判：在 best_of 中选优，不参赛

class RoleAgentBinding(BaseModel):
    role: str
    agents: list[BoundAgent]            # 1..N
    aggregation: Aggregation | None     # 多 agent 时必填，单 agent 忽略

class BoundAgent(BaseModel):
    ref: str                            # AgentProfile.key
    duty: Duty = Duty.PRIMARY
    weight: float = 1.0                 # 同 duty 多实例时的分流权重
    on_failure: Literal["fail", "degrade"] = "fail"

class Aggregation(BaseModel):
    mode: Literal["primary_with_review", "split_by_path", "best_of", "merge"]
    quorum: int = 1                     # 至少几个 PRIMARY 成功才算任务成功
    judge: str | None = None            # best_of 时指定 JUDGE agent 的 ref
```

### 2.3 `RunPlan`（解析产物，运行期唯一事实源）

配置是"声明"，`RunPlan` 是"本次 run 落地后的解析结果"。它必须在**规划阶段**算好并可审计：

```python
class ResolvedAgent(BaseModel):
    role: str
    profile: str
    alias: str          # 已解析到具体 provider:model（含降级结果）
    tools: list[str]    # 角色 ∩ agent 之后的结果
    duty: Duty
    path_scope: list[str]

class RunPlan(BaseModel):
    run_id: str
    agents: list[ResolvedAgent]
    delegations: list[Delegation]   # role → agent 的调度关系，写入 run.json
```

落盘位置：`output/<run_id>/run.json` 的 `config_snapshot.run_plan`，并在规划期写一条 `run_plan_resolved` 事件。这样"这次为什么用了这个模型"永远可回溯。

---

## 3. 配置方式

四种入口，合并优先级与现有一致：**CLI > 环境变量 > `agentcrew.yaml` > `roles.d/*.yaml` > 内置默认**。

### 3.1 `agentcrew.yaml`（主入口）

```yaml
agents:
  profiles:
    backend_fast:
      title: 后端-快速档
      model: deepseek:deepseek-chat
      fallback: [openai:gpt-4o-mini]
      tools: [read_file, grep, run_python]
      path_scope: ["backend/"]
      cost_ceiling_usd: 0.08
    backend_solid:
      title: 后端-强推理
      model: anthropic:claude-sonnet-4
      tools: [read_file, grep, run_python]
      path_scope: ["backend/"]
    doc_polish:
      title: 文档润色
      model: deepseek:deepseek-chat
      tools: [read_file]
      path_scope: ["docs/", "README.md"]

roles:
  backend_dev:
    agents:
      - ref: backend_solid
        duty: primary
      - ref: backend_fast
        duty: review
    aggregation:
      mode: primary_with_review
      quorum: 1
  technical_writer:
    agents:
      - ref: doc_polish
        duty: primary
```

### 3.2 `roles.d/` 内联（自定义角色自带 agent）

自定义角色把绑定写进同一个文件，避免"角色定义在一处、绑定在另一处"：

```yaml
# roles.d/data_analyst.yaml
key: data_analyst
title: 数据分析师
# …其余 RoleSpec 字段…
agents:
  - ref: backend_solid
    duty: primary
aggregation:
  mode: split_by_path
  quorum: 1
```

### 3.3 环境变量

```bash
AGENTCREW_AGENT_PROFILES='{"backend_fast":{"model":"deepseek:deepseek-chat"}}'   # JSON
AGENTCREW_ROLE_AGENTS_BACKEND_DEV='backend_solid:primary,backend_fast:review'    # 紧凑写法
```

### 3.4 管理界面（Studio）

放在**设置页新增「角色与 Agent」分区**，不新增第 10 页——9 页导航是既有 e2e 断言的一部分，改导航的成本与收益不成比例。

界面形态：一张**绑定矩阵**。

```
              backend_fast   backend_solid   doc_polish
project_manager     ○              ○              ○
backend_dev         ●(review)      ●(primary)     ○
technical_writer    ○              ○              ●(primary)
```

- 行 = 角色，列 = agent profile，单元格点击在 `primary / section / review / judge / 不绑定` 之间循环；
- 右侧抽屉编辑 profile（模型、工具、路径域、预算），带「试跑一次」按钮（mock，不花钱）；
- 顶部实时校验：`未绑定的角色`、`duty 冲突`、`judge 与参赛者重复`、`路径域重叠`，不通过时禁用保存并指出行号；
- 保存前弹「影响预览」：列出会被影响的 run 配置项与 diff，确认后写 `agentcrew.yaml`（Studio 不直接改 Python 配置）。

### 3.5 启动期校验（fail fast，不做运行期兜底）

| 校验 | 失败行为 |
| --- | --- |
| `ref` 指向不存在的 profile | `ValueError`，退出码 3（配置错误） |
| 同一角色多个 `primary` 且 `aggregation` 为空 | `ValueError` |
| `best_of` 未指定 `judge`，或 judge 同时参赛 | `ValueError` |
| 同一角色内两个 agent 的 `path_scope` 完全相同且都声明 primary/section | `ValueError`（必然冲突，早失败好过晚返工） |
| `weight <= 0` / `quorum > primary 数量` | `ValueError` |
| 旧配置（只有 `role_bindings`） | 自动降级为单 agent，`duty=primary`，不报错 |

### 3.6 向后兼容

`role_bindings` / `RoleSpec.model` 继续有效，解析顺序变成：

```
RoleSpec.agents（新）> role_agents[role]（新）
  > RoleSpec.model（旧）> role_bindings[role]（旧）> model_tiers > default_model
```

旧配置等价于"单 agent、primary、无聚合"，行为逐字节不变。

---

## 4. 权限与路由

### 4.1 权限四个维度

| 维度 | 作用点 | 规则 |
| --- | --- | --- |
| **工具** | `ToolRegistry.enforce` | agent.tools ⊆ role.allowed_tools；写 `*` 也不能扩权，只能继承角色全集 |
| **路径** | `ArtifactStore.commit` | 实际写入路径必须落在 `agent.path_scope` 前缀内，否则抛 `ArtifactScopeError`（等同契约外产物，走 G1 驳回而非静默丢弃） |
| **预算** | `ModelFactory._account` | 单 agent 单任务 `cost_ceiling_usd` / `max_calls_per_task` 超限 → 该 agent 冻结，同 duty 其余实例继续 |
| **审批** | `Gatekeeper` | 只有 `reviewer_for` 命中的角色可出 `approve/reject`；agent 的 duty=review 只产意见，不直接改裁决 |

**一条硬规则**：agent 只能**收窄**角色权限，永远不能扩权。校验在启动期做（`agent.tools - role.allowed_tools` 非空即报错），不做运行期容错。

### 4.2 路由流水线

```
resolve(role, task)
  │
  ├─ 1. 取该 role 的 bindings（已按 duty 分组）
  ├─ 2. 过滤：工具/路径/预算/配额/健康冷却 → eligible[]
  ├─ 3. 按 duty 选实例：
  │      primary/section → 池内策略（weighted|rotate|usage|order）
  │      review          → 全部 review agent（可并行，全过才算过）
  │      judge           → 指定 ref，不参与路由
  ├─ 4. 别名解析：profile.model → fallback 池 → 冷却过滤 → 策略选择
  ├─ 5. 亲和性（affinity_mode != off 时固定同一 alias，5 分钟冷缓存失效）
  └─ 6. 产出 ResolvedAgent，写 run_plan_resolved（规划期一次）
```

### 4.3 可复现性

`weighted` 的随机必须**可复现**，否则 mock 回放与真实 run 对不上：

```python
seed = sha1(f"{run_id}|{task_id}|{attempt}|{role}|{profile}")[:8]
rng = random.Random(seed)      # 不用全局 random
```

同一 run 重跑、resume 重派、mock 回放，选中的 agent 必须一致。

### 4.4 与既有机制的关系

- **冷却/故障转移**：仍按 provider 别名维度，不按 agent 维度——一个 alias 挂了，所有引用它的 agent 一起避开；
- **配额**：按别名计，不按 agent 计（厂商额度是别名维度的）；
- **亲和**：按 `(run_id, role)` 保持，避免同一角色在两个 agent 间来回跳导致 prompt 缓存全失效。

---

## 5. 单角色多 Agent：职责、协作、汇总

### 5.1 职责划分

| duty | 能写产物 | 能出裁决 | 典型用途 |
| --- | --- | --- | --- |
| `primary` | ✅ 仅限自己的 `path_scope` | ❌ | 主笔，产出契约产物 |
| `section` | ✅ 仅限自己认领的路径 | ❌ | 分工（如"只写 tests/"） |
| `review` | ❌（写也只在 review/ 目录） | ✅（意见，进 fix_hints） | 交叉复核 |
| `judge` | ❌ | ✅（择优，进聚合结果） | best_of 裁判 |

### 5.2 三种协作模式

**A. `primary_with_review`（主笔 + 复核）**——默认推荐，成本最低

```
primary 产出 → review agent 读产物 → 逐条对照 acceptance_criteria 出意见
   ├─ 无阻断意见 → 进 G1/G2
   └─ 有阻断意见 → 意见合并进 fix_hints，primary 返工（复用现有 rework 通道）
```

**B. `split_by_path`（按路径分工）**——适合产物边界清晰的任务

```
计划期：每个 section agent 认领若干 deliverable 路径（路径唯一所有者）
执行期：并行产出，互不可写对方路径
汇总：merge_by_path（天然无冲突）
```

**C. `best_of`（N 选 1）**——适合"写好为止"的开放性产物

```
N 个 primary 各产一份草稿（同一路径 → 写入影子目录 drafts/<agent>/）
judge agent 按 acceptance_criteria 打分选优 → 当选者写入正式路径
落选草稿保留在 drafts/ 供审计，不进 MANIFEST
```

### 5.3 产物归属与冲突

沿用并扩展 `ArtifactStore` 的冲突检测：

- 计划期生成 **claim 表**：`path -> agent_key`，必须一对一；
- 同一路径被两个 agent 声明 → **计划期报错**（不是运行期冲突检测），因为这是配置错误；
- `best_of` 走影子目录，不参与 claim；
- 返工时只清该 agent 的路径，不清同角色其他 agent 的产物（对应现有 `discard_task` 的粒度问题）。

### 5.4 汇总方式

| 模式 | 汇总算法 | 冲突处理 |
| --- | --- | --- |
| primary_with_review | 产物取 primary；意见 concat + 去重（复用 `deduplicate_issues`） | 不可能冲突（单写者） |
| split_by_path | 各写各的，按 claim 合并 | 计划期已排除 |
| merge | 同路径多份 → 按 `merge_strategy`（`concat_sections` / `prefer_weight`） | 未配置策略则报错 |
| best_of | judge 选 1，其余进 drafts/ | 单写者 |

**汇总必须是确定性的**：同样的输入与种子，汇总结果一致；否则无法回归。

### 5.5 失败与降级

| 情形 | 行为 | 可见性 |
| --- | --- | --- |
| primary 失败，quorum 未达成 | 任务 FAILED（与现在一致） | 报告点名失败的 agent |
| 某个 primary 失败但 quorum 达成 | 任务继续，产物缺该 agent 的部分 → 由 G1 的 `must_contain` 决定是否驳回 | `agent_failed` + `partial_result` 事件 |
| review agent 不可用 | **不静默放行**：记 `agent_degraded`，任务标记 `review_degraded=true`，G1 照跑，运行报告显式列出"本次 N 个任务缺少复核" | 报告 + ledger |
| judge 不可用（best_of） | 不回退到"随便选一个"：任务 FAILED，退出码 1 | 报告点名 |
| 单 agent 超预算 | 冻结该 agent，同 duty 其他实例继续；全部冻结则任务 FAILED | `agent_budget_exceeded` |

**这条对应项目的"禁止 fail-open"原则**：降级可以，但必须写在报告里，不能悄悄变成"看起来审过了"。

---

## 6. 交互流程（用户行为 / 系统行为）

### 6.1 配置期

| 步骤 | 用户 | 系统 |
| --- | --- | --- |
| 1 | 打开 设置 → 角色与 Agent | 读 `agentcrew.yaml` + `roles.d`，渲染绑定矩阵 |
| 2 | 点单元格切换 duty | 实时校验，冲突行标红并禁用保存 |
| 3 | 点「试跑」 | 用 mock 跑一个最小任务，回放该 agent 的提示词与产物（不花钱、不落正式 run） |
| 4 | 保存 | 写 `agentcrew.yaml`（保留注释与顺序），记 `config_changed` 审计 |

### 6.2 运行前预览

新建运行弹窗增加「本次编排」折叠区：

```
backend_dev   → backend_solid (primary) + backend_fast (review)
technical_writer → doc_polish (primary)
预计并发 3 · 预计成本 $0.12 · 2 个角色使用降级模型
```

系统在规划阶段算完 `RunPlan` 并写入 `run.json`，预览与实际执行必须同源（同一函数），不允许"预览一套、跑另一套"。

### 6.3 运行中切换

| 步骤 | 用户 | 系统 |
| --- | --- | --- |
| 1 | 任务详情 →「切换 Agent」 | 列出该角色 eligible 的 agent，标注当前选中与不可用原因（冷却/预算/权限） |
| 2 | 选中并确认 | 不改在途任务；写入 `pending_binding[role]` |
| 3 | 下一次派发 | 用新绑定解析，记 `agent_switched`（含 from/to/原因/操作者） |
| 4 | 已在跑的任务 | 继续用旧 agent 跑完，不打断——中断会浪费已花的 token |

### 6.4 故障切换

```
alias 失败 → 冷却矩阵 → 同 profile 的 fallback 别名 → 仍失败 → 同 duty 其他 agent
  → 全部不可用 → 任务按 attempts 重试 → 耗尽则 FAILED
```

每个跳转都记 `llm_failover` / `agent_failover`，事件流里可点开看链路。

### 6.5 审计事件（新增）

| 事件 | 关键字段 |
| --- | --- |
| `run_plan_resolved` | plan 全量（角色/agent/别名/duty/路径域） |
| `agent_selected` | role, profile, alias, strategy, seed |
| `agent_switched` | role, from, to, operator, reason |
| `agent_failover` | role, from, to, cause |
| `agent_degraded` | role, profile, duty, reason |
| `agent_aggregated` | role, mode, participants, winner |

---

## 7. 典型场景与预期输出

| # | 场景 | 系统行为 | 预期输出 | 失败处理 |
| --- | --- | --- | --- | --- |
| S1 | 单角色单 agent（回归） | 走旧路径，`duty=primary` | 与当前行为逐字节一致；`run_plan_resolved` 只有一条 | 同现状 |
| S2 | 后端：主笔强模型 + 复核快模型 | primary 产出 → review 出意见 | 产物同现；`review/` 下多一份意见；无阻断则 G1/G2 照常 | 复核不可用 → `agent_degraded`，报告标注 |
| S3 | 文档：两个 section 分别写 README / docs/ | 并行执行，路径互斥 | 两份产物均落盘；MANIFEST 的 `produced_by_task` 相同但 `produced_by_agent` 不同 | 一方失败 → quorum 判定 |
| S4 | 架构：best_of 3 选 1 | 3 份草稿进 `drafts/`，judge 择优 | 正式路径只有 1 份；`drafts/` 保留 3 份；`agent_aggregated` 记录 winner 与理由 | judge 不可用 → FAILED |
| S5 | 运行中把 `backend_fast` 换成 `backend_solid` | 写 pending，下一次派发生效 | 在途任务不中断；后续任务的 `llm_call` 记录新别名 | 目标不可用 → 拒绝切换并提示原因 |
| S6 | DeepSeek 402 余额不足 | 冷却 30min → 切 fallback 别名 → 仍失败切同 duty 另一 agent | 任务继续成功；事件流显示两次 failover | 全部不可用 → attempts 重试 → FAILED |
| S7 | agent 配了角色没有的工具 | 启动期校验失败 | 拒绝启动，退出码 3，指出 role/agent/tool | — |
| S8 | 两个 agent 声明同一路径 | 计划期报错 | 退出码 2（计划校验未通过），指出冲突路径与两个 agent | — |
| S9 | 角色路径域越界写文件 | `commit` 抛 `ArtifactScopeError` | G1 驳回，返回"路径不在允许范围内"；不落盘 | 返工仍越界 → 额度耗尽 FAILED |
| S10 | 单 agent 成本超 `cost_ceiling_usd` | 冻结该 agent，其他实例继续 | 报告显示"backend_fast 已达预算上限"；总成本不超全局 `max_cost_usd` | 全部冻结 → FAILED |
| S11 | 全部 agent 都用同一别名池且额度耗尽 | 配额检查在别名维度拦截 | 任务失败，但**不重试**（重试必然同样失败），直接进 FAILED | 退出码 1 |

---

## 8. 代码改动清单

| 文件 | 改动 | 规模 |
| --- | --- | --- |
| `agentcrew/roles/spec.py` | `RoleSpec` 增加 `agents: list[BoundAgent] = []`、`aggregation: Aggregation | None` | 小 |
| `agentcrew/agents/profile.py` | **新增** `AgentProfile` + 校验 | 中 |
| `agentcrew/agents/registry.py` | `get(role_key, profile_key=None)`，缓存键改 `(role, profile)`；`get` 返回已注入 profile 的 agent | 中 |
| `agentcrew/models/factory.py` | `alias_for_task` 增加 profile 入参；`resolve_plan()` 新函数产出 `RunPlan` | 中 |
| `agentcrew/crew/scheduler.py` | 派发前取 plan；fan-out（primary/section 并行、review 汇总）；聚合入口 | 大 |
| `agentcrew/artifacts/store.py` | `commit` 增加 `path_scope` 校验；claim 表 | 中 |
| `agentcrew/tools/registry.py` | `enforce(role, tool, profile=None)`，交集判断 | 小 |
| `agentcrew/config.py` | 解析 `agents.profiles` / `roles.<role>.agents`，env 映射，合并顺序 | 中 |
| `agentcrew/cli/roles.py` | `roles` 输出增加 agent 绑定与 duty | 小 |
| `studio/src/features/settings/agents.js` | **新增** 绑定矩阵分区 | 大 |
| `studio/src/app/data.js` | `deriveAgents` 从"8 角色"改为"role × profile" | 中 |
| 测试 | `test_agent_profiles.py`、`test_binding_plan.py`、`test_multi_agent_aggregation.py`、`test_path_scope.py` | 4 个文件 |

**改动顺序**（每步都可独立合入、每步测试全绿）：

1. 数据结构 + 配置解析 + 校验（无行为变化）
2. `RunPlan` 解析 + `run_plan_resolved` 事件（单 agent 时与原行为等价）
3. `AgentFactory` 缓存键改造（回归测试必须全绿）
4. `path_scope` 与 claim 表（先只支持 primary）
5. fan-out + `primary_with_review`
6. `split_by_path` / `best_of`
7. Studio 绑定矩阵
8. 预览与运行中切换

---

## 9. 兼容性与约束

1. **旧配置零改动可用**：`role_bindings` 自动映射为单 agent，产物、事件、报告格式不变。
2. **mock 夹具要扩**：现在按 `(purpose, task_id, attempt)` 回放，多 agent 后需加 `profile` 维度，且**不能静默回退**——缺夹具要记 `mock_fallback`（沿用现有约定）。
3. **并发**：fan-out 的每个 agent 都占一个任务槽位内的子并发；**不得在槽位内再开槽位**（两层并发不嵌套的红线继续成立）。
4. **确定性**：选择用 hash seed，不用全局随机；聚合算法必须可复现。
5. **成本**：`best_of` 的成本是 N 倍，必须在预览里显式提示，并受 `max_cost_usd` 硬上限约束。
6. **不引入 fail-open**：任何降级都要落事件、进报告。
7. **不动 9 页导航**：绑定矩阵放进设置页，避免破坏 e2e 的页面断言。

---

## 10. 验收标准

| # | 标准 | 判定方式 |
| --- | --- | --- |
| A1 | 旧配置（仅 `role_bindings`）行为不变 | 全量 1946+ 用例全绿；`realrun` 复跑产物 diff 为空 |
| A2 | 单 agent 新配置与原路径等价 | 同一任务跑两次，`ledger` 的 `llm_call` 别名与产物 sha256 一致 |
| A3 | 多 agent 分工产物齐全 | S3 场景：claim 表内每个路径都有产物，无越界写入 |
| A4 | 聚合确定性 | S4 场景重复 3 次，winner 与最终产物一致 |
| A5 | 权限不可扩 | S7 场景启动失败，退出码 3 |
| A6 | 路径越界被拦 | S9 场景 G1 驳回，`artifacts/` 无该文件 |
| A7 | 降级可见 | S2 故障注入：报告中出现 `review_degraded`，且不是"通过" |
| A8 | 成本可控 | S4 场景实际花费 ≤ 预览预估的 1.3 倍 |
| A9 | 并发不嵌套 | `test_scheduler_deadlock.py` 结构性守卫仍绿 |
| A10 | UI 闭环 | 绑定矩阵保存后，新建运行预览与 `run.json` 的 `run_plan` 一致 |

---

## 11. 风险与未决

| 风险 | 影响 | 处置 |
| --- | --- | --- |
| `best_of` 成本翻倍 | 预算失控 | 预览强提示 + 默认关闭 + 受硬上限 |
| 多 agent 让 G2 评审变复杂（评谁的产物？） | 裁决歧义 | 评审对象始终是"任务产物"，不区分 agent；agent 只是生产者 |
| claim 表与现有跨任务冲突检测重复 | 两套规则打架 | claim 表在**计划期**，跨任务检测在**落盘期**，前者是配置校验、后者是最后防线，保留两层 |
| Studio 绑定矩阵与 YAML 双写冲突 | 用户手改 YAML 后被 UI 覆盖 | 保存前读盘比对，有外部改动则要求确认（与 `config export` 的备份策略一致） |
| 自定义角色 + 未注册 profile | 启动失败 | 校验期明确报错并给出 `roles` 命令提示 |

---

## 附：为什么不做"每角色一个 Python 子类"

现有 `AGENT_CLASSES` 允许为角色注册专用类，但那是**行为**差异（如 PM 的拆解逻辑），不是**执行体**差异。多 agent 若靠"多写类"实现，会退化成 8×N 个类，且模型/工具/预算这些纯配置被迫写进代码。所以本方案坚持：**角色是数据，agent 是配置，类是最后手段**。
