# AgentCrew 改善方案（2026-11-09）

> 配套文档：`docs/AUDIT-2026-11-09.md`（能力审查与差距矩阵）。
> 本文只回答一个问题：**接下来改什么、按什么顺序改、怎么判断改对了。**
>
> 全部条目都已在代码里定位到具体落点，不做"应该加强可观测性"这类无法验收的建议。

---

## 0. 现状锚点

目标能力（不同 agent 用不同模型、协同完成同一任务）**已经成立**，`split_by_path` /
`primary_with_review` / `best_of` / `merge` 四种协同都跑得通。当前的问题不是"缺功能"，
而是**同一种能力存在两套质量不同的实现**，以及**多执行体一旦规模上来就管不住**。

改善的主线因此不是加功能，而是：**统一实现、补齐治理、让配置可被验证**。

### 0.1 执行状态（2026-11-10）

| 项 | 状态 | 关键证据 |
| --- | --- | --- |
| P0 复核接治理通道 | ✅ 已交付 | 裁决结构与 G2 共用 `gate/verdict.py`；复核改走 `complete_structured` |
| P1.3 门禁评审归因 | ✅ 已交付 | `unknown` 从 7 次降到 1 次，出现 `gate:architect` 等桶 |
| P1.1 执行体级熔断 | ✅ 已交付 | 默认关；连续失败达阈值后换 primary，落 `agent_circuit_open` |
| P1.2 成本感知路由 | ✅ 已交付 | 默认关；剩余额度 < `cost_aware_headroom` 时本次调用改便宜档 |
| P2.1 绑定 A/B 评测 | ✅ 已交付 | `eval --variant NAME=FILE`，首个变体作基线，输出 Δpass^k/Δcalls/Δcost/Δ修订 |
| P2.2 共享工作区 | ✅ 已交付（最小形态） | `Aggregation.intra_deps` + 拓扑执行 + `UpstreamDigest` 注入，见 §10.2 |

**实施中额外发现并修掉的两个缺陷**（都不在原方案里，是动手时才暴露的）：

1. **修订过程会在报告里丢失**：`review_rounds>0` 时第二轮复核会覆盖第一轮的意见，
   最终 notes 只剩"未发现问题"，"这一轮要求改什么"无从追溯。已改为跨轮累加并标注轮次。
2. **复核的 mock 回放没接上夹具**：`TeamAgent` 直接调 `models.call` 时不设 `purpose`，
   mock 拿不到 `reviews.json`，复核在离线跑里一直是"自由发挥"。改走结构化通道时
   必须显式 `set_purpose`，这条路径才真正被夹具覆盖。

---

## 1. 三条改善原则

1. **先统一，再新增。** 同一个语义在两处实现、质量还不一致，是最贵的债——加新功能只会
   把分叉复制一份。
2. **默认关闭，可回滚。** 所有行为变更走配置开关，默认值与改动前逐字一致（沿用本项目
   既有惯例：`peer_dialogue_enabled`、`supervisor_review`、`review_rounds` 都是这么做的）。
3. **每条改动都要能算账。** 验收标准必须是可观测的数字（账平率、收敛轮数、驳回有效率），
   不是"感觉更好了"。

---

## 2. P0 — 把角色内复核接上治理通道（最高优先级）

### 2.1 问题

同一个项目里有两条评审路径，成熟度差一个代际：

| | G2 门禁评审（`gate/reviewer.py`） | 角色内复核（`agents/team.py::_review_once`） |
| --- | --- | --- |
| 调用入口 | `ModelFactory.complete_structured()`，受信号量 + 记账治理 | 裸 `models.call()` |
| 输出 | `ReviewVerdict`：逐条 `CriterionJudgement`（`satisfied` + `evidence`） | 自由文本 |
| 裁决 | `derive_review()` 派生：**有标准不满足才驳回** | 子串匹配 `"未发现问题"` |
| 返工输入 | `fix_hints` 来自 `evidence`，指向"缺什么、改成什么" | 整段复核文本塞进 `fix_hints` |
| 格式错误 | 重试 → 放行，且**不消耗返工额度** | 无此概念 |

`gate/reviewer.py` 的模块注释里已经写清了为什么不能要开放式裁决：
"过去直接向模型要 approve/reject……一个认真的评审员总能在两轮内挑够毛病，
于是任何非平凡任务都注定 FAILED（实测 run6：问题数 7→12→8 不收敛）"。

**角色内复核现在正踩在这个坑上**：我用子串匹配 `"未发现问题"` 来判断"没意见"，
模型换一种说法（"未发现明显问题""基本符合要求"）就会被判为"有问题"，进而触发
`review_rounds` 重跑主笔。轻则浪费一次调用，重则主笔被无意义的意见改坏。

### 2.2 做法

| 步骤 | 落点 | 内容 |
| --- | --- | --- |
| 1 | `gate/reviewer.py` | 把 `ReviewVerdict` / `CriterionJudgement` / `derive_review` / `_is_malformed` 抽到可复用位置（`gate/verdict.py`），G2 行为不变（纯搬迁 + re-export） |
| 2 | `agents/team.py::_review_once` | 改为 `complete_structured(..., ReviewVerdict, purpose="review")`，用 `derive_review(verdict, task)` 得到 `(approved, issues, hints)` |
| 3 | `agents/team.py::_collect_reviews` | `actionable` 改为取 `approved=False` 的 `hints`；删掉 `_NO_ISSUE` 子串判断 |
| 4 | `roles/binding.py::Aggregation` | `review_rounds` 语义收紧为"**仅当有验收标准被判不满足时**才回灌"；`suggestions` 只进 `notes`，不触发修订 |

### 2.3 为什么这条排第一

- **消除一个已经存在的错误源**：字符串匹配是当前唯一会"误判复核结果"的地方。
- **复用而非新造**：`derive_review` 已经把"评审员不能凭喜好驳回"这件事论证过一遍，
  没有理由让角色内复核再走一遍弯路。
- **让 `review_rounds` 真正可用**：现在开 `review_rounds=1` 有被自由文本带偏的风险，
  接上结构化裁决后，"改一轮就好"才是可预期的。

### 2.4 验收

1. 构造三条用例：全部满足 / 一条不满足 / 都满足但带 suggestions。
   只有第二条触发 `agent_revise`。
2. 复核输出改为"基本符合要求，未发现明显问题"这类**同义表述**时，不再触发修订。
3. G2 的全部既有用例**一行不改**通过（证明搬迁无行为变化）。
4. `review_rounds=0` 与改造前逐字一致。

**风险**：`complete_structured` 有降级链，若某 provider 不支持结构化输出会退回文本解析。
需要确认 mock 通道的 `reviews.json` 夹具能产出 `ReviewVerdict` 形状；否则 mock run 会
走"格式错误 → 放行"分支，看起来像复核失效。**这是本条最需要先验证的点。**

---

## 3. P1 — 多执行体的治理短板

### 3.1 执行体级熔断

**现状**：`models/health.py` 只做 **provider** 健康（402/429/5xx 冷却矩阵）。
某个 agent profile 连续失败（例如模型不支持长上下文、工具权限配错）不会自停，
只会一次次消耗返工额度，直到整条 DAG 卡住。

**做法**：在 `AgentFactory` 层加一个 per-run 的失败计数（不是全局、不落盘、不跨 run）：

- 计数来源：`task_finished` 的失败 + `agent_degraded` 事件（已有，无需新埋点）；
- 阈值：连续 N 次（默认 3）失败 → 该 profile 在本 run 内标记 `degraded`；
- 降级动作（按 `Aggregation.mode` 分）：`primary_with_review` 退回其它 primary；
  `split_by_path` 把该 section 的产物标记为缺失并**让任务失败**（不能假装写完）；
- 开关：`agent_circuit_breaker_enabled`，默认 **false**。

**不建议做**：跨 run 的全局熔断。那需要持久化的健康状态与恢复探测，收益远小于复杂度，
且会引入"上次崩了这次直接不试"的隐蔽故障。

**验收**：mock 一个"永远抛异常"的 profile，连跑 3 个任务后第 4 个不再调用它，
且报告里能看出"因熔断跳过"。

### 3.2 成本感知路由

**现状**：`models/routing.py` 按健康 / 用量 / 权重在**别名池**内选，不读"这个 profile
还剩多少额度"。现在的省钱手段是 `budget.py` 的**事后降级**——花到 90% 才换便宜模型，
属于"撞墙才刹车"。

**做法**：给路由加一档"预算加权"：

- 输入：`agent_budget` 当前 profile 的 `spent_usd / run_ceiling_usd`；
- 规则：剩余额度低于阈值（默认 30%）时，把候选池里**单价更高**的别名权重压到 0；
- 边界：**只在同一 profile 内生效**，不跨 profile 抢额度（那是 `run_cost_ceiling` 的事）；
- 开关：`cost_aware_routing`，默认 false。

**验收**：给定 `run_cost_ceiling_usd` 与两个单价不同的别名，跑到 70% 后
`llm_call.model` 不再出现贵的那个，且账本里能看到一次 `routing_budget_shift` 事件。

### 3.3 门禁评审的归因

**现状**：G2 评审走 `complete_structured(purpose="review", role=reviewer_key)`，
但**没有 profile**，因此在 `llm.by_profile` 里落进 `unknown`。实测一次 5 任务的 run，
`unknown` 共 7 次调用，构成是：**6 次门禁评审**（`project_manager` / `architect` /
`product_manager` 各 2 次）+ 1 次规划调用。也就是说这个桶里 6/7 是**可以归因却丢失了**的。

**做法**：给 `ReviewRecord` / `complete_structured` 调用补一个伪 profile
（如 `gate:<reviewer_key>`），或允许 `TraceContext(profile=...)` 由 `Reviewer` 显式设置。
**优先选后者**——不新增概念，只补一层上下文。

**验收**：`unknown` 只剩规划期调用；`by_profile` 各项之和仍等于 `llm.calls`。

---

## 4. P2 — 让"换绑定"变成可验证的决定

### 4.1 绑定配置的 A/B 评测

**现状**：`evals/` 能跑固定用例并给 pass^k，但**不能回答"我这套绑定比上一套好还是坏"**。
调 `review_rounds`、换便宜模型、改 duty 分配，全靠感觉。

**做法**：扩 `evals` 为二维矩阵：

```
配置维度：agentcrew.yaml 的若干变体（只换 roles/agents 段）
用例维度：已有 eval cases
输出：每个 (配置 × 用例) 的通过率 / 成本 / 复核轮数 / 返工次数
```

落点：`evals/runner.py` 加 `--config-set`（多个 yaml），结果落 `artifacts/evals/<ts>/matrix.json`。

**验收**：能用一条命令回答"把 backend_dev 的复核换成便宜档，通过率掉几个点、成本降多少"。

### 4.2 跨执行体共享工作区

**现状**：同角色多 agent 之间只通过①上游产物、②`consult` 跨角色咨询 交互。
`best_of` / `merge` 已经用影子任务解决了"各写各的"，但**没有共享的中间态**
（例如两个 section 需要对齐同一个接口签名）。

**做法**：不新建模块，扩 `context/context_bus.py`：

- 增加一个 per-task 的 `scratch` 区（只在本任务生命周期内可见，落 `drafts/<task_id>/_shared/`）；
- `split_by_path` 的成员在开工前可读到同任务其它成员已写入的 scratch 文件；
- 默认空实现，不改变现有 `ContextPack` 的组装顺序（那六条 resolutions 是钉死的，不能动）。

**验收**：两个 section 分工写 `backend/` 与 `tests/`，tests 侧能读到 backend 侧写入的
`_shared/api.md`，且 `ContextPack.truncated` 语义不变。

---

## 5. P3 — 按需，不预做

| 项 | 触发条件 | 备注 |
| --- | --- | --- |
| MCP resources / prompts | 有真实的 MCP 服务器提供非 tool 能力 | 当前只接 tools，够用 |
| `web_search` | 任务真的需要外部检索 | 需外部 API 与密钥，属新依赖 |
| 分布式执行 | 单机跑不动，或需要多机并行 | `Checkpoint` + `resume` 锁已留了口子；触发前不做 |
| 事件溯源内核替换 | 需要审计级重放或时间旅行调试 | 当前 `TaskBoard` + JSON checkpoint 足够 |

---

## 6. 明确不做

| 想法 | 为什么不做 |
| --- | --- |
| 重写引擎 | 36K 行 + 39K 行测试 + 2,899 用例构成的行为契约，重写收益为负（详见审查报告 §4） |
| 提高 `review_rounds` 上限 | 超过 3 轮说明**验收标准本身没写清**，该改契约而不是加轮数 |
| 给复核加"必须挑出问题"的激励 | G2 已有实测教训（run6 问题数 7→12→8 不收敛），会烧光返工额度 |
| 跨 run 全局熔断 | 见 §3.1，复杂度远超收益 |
| 引入向量库做经验检索 | R11 的中文二元组 + BM25 已够用；R27 已加向量通道，再加一层是重复建设 |

---

## 7. 怎么判断改善有效

不要用"测试数量"衡量，用这四个数（都已在 ledger / run.json 里）：

| 指标 | 现状基线 | 目标 | 数据来源 |
| --- | --- | --- | --- |
| **账平率**：`sum(by_profile) == llm.calls` | 100%（本轮已修） | 保持 100% | `ledger.jsonl` |
| **复核有效驳回率**：触发 `agent_revise` 且最终通过 / 总 `agent_revise` | 未知（新增事件） | > 60% | ledger |
| **返工收敛轮数**：任务平均 `rework_count` | 未知 | 不上升 | `tasks.json` |
| **绑定变更的可解释性**：能给出 A/B 数据的变更占比 | 0% | 每季度 > 50% | `evals/matrix.json` |

第一条是**守门指标**——它一旦掉下来，说明又出现了"两本账"，应立刻回滚。

---

## 8. 建议顺序与依赖

```
P0 复核接治理通道 ──┬─→ P1 执行体熔断（依赖结构化裁决判断"失败原因"）
                    └─→ P1 门禁评审归因（小改动，可并行）
P1 成本感知路由 ─────→ P2 绑定 A/B 评测（有了省钱的旋钮，才需要验证它值不值）
P2 共享工作区（独立，可随时插队）
```

先做 P0 的理由不是它最"重要"，而是**它是唯一一个"现在就有错误风险"的项**——
其余都是能力缺口，P0 是正确性问题。

---

## 9. 已交付实现（P0 + P1）

### 9.1 文件级改动

| 文件 | 改动 |
| --- | --- |
| `gate/verdict.py`（新） | 裁决契约与派生规则：`CriterionJudgement` / `ReviewVerdict` / `derive_review` / `is_malformed`。G2 与角色内复核**共用同一份** |
| `gate/reviewer.py` | 改为从 `verdict` 导入（行为不变，仅搬迁）；评审调用挂 `TraceContext(profile="gate:<角色>")` |
| `agents/team.py` | 复核改走 `complete_structured`；新增 `_ReviewOutcome` / `_call_structured` / `_as()`；新增 `_Circuit` 执行体熔断与 `_run_member`；修订意见跨轮累加 |
| `config.py` | 新增 4 个开关：`agent_circuit_breaker_enabled`（false）、`agent_circuit_breaker_threshold`（3）、`cost_aware_routing`（false）、`cost_aware_headroom`（0.3） |
| `models/budget.py` | 新增纯函数 `under_headroom(spent, ceiling, headroom)` |
| `models/factory.py` | `_maybe_apply_downgrade` 增加执行体级额度分支（无状态）；新增 `routing_budget_shift` 事件；`reset_stats` 复位上报去重集合 |

### 9.2 新增事件

| 事件 | 字段 | 用途 |
| --- | --- | --- |
| `agent_review`（扩充） | `approved` / `issues` | 复核结论可审计（此前只有 `chars`） |
| `agent_circuit_open` | `profile` / `threshold` / `reason` | 某执行体被判不可用 |
| `routing_budget_shift` | `profile` / `from_model` / `to_alias` / `spent_usd` / `ceiling_usd` | 提前降档留痕 |

### 9.3 开关与回滚

四项行为变更**全部默认关闭**，关闭时与改动前逐字一致：

```yaml
# 执行体级熔断
agent_circuit_breaker_enabled: false
agent_circuit_breaker_threshold: 3

# 成本感知路由
cost_aware_routing: false
cost_aware_headroom: 0.3

# 角色内复核的修订回路（R44 之前已有）
roles:
  backend_dev:
    aggregation:
      mode: primary_with_review
      review_rounds: 1        # 0 = 只出意见
```

回滚只需把对应开关置回默认；没有数据迁移、没有状态文件格式变化。

### 9.4 验收结果

```
test_agent_binding.py            36 passed（+7：结构化裁决×3、熔断×3、替身适配）
test_cost_aware_routing.py        9 passed（新文件：阈值/开关/优先级/无额度）
test_reviewer.py + mock + e2e    58 passed（G2 一行未改）
test_architecture.py             26 passed（agents→gate 方向合法）
test_crew.py                     52 passed
test_api_contract + server       64 passed
test_model_factory.py            52 passed
test_config + config_io          38 passed
test_base_agent + hardening      98 passed（与绑定/路由合并跑）
```

端到端（`linear` + 双执行体 + `review_rounds=1`，mock 通道）：

```
任务 5/5 成功 · LLM 13 次
执行体  be_primary 1次 · be_reviewer 1次
by_profile: be_primary 1 | be_reviewer 1 | default 4 | gate:* 6 | unknown 1
            合计 13 = llm.calls   ← 账平
```

`unknown` 从 7 降到 1（只剩规划期调用）；`gate:architect` / `gate:product_manager` /
`gate:project_manager` 各自成桶——门禁评审的花费终于能按评审角色查。

---

## 10. P2.1 已交付 / P2.2 暂缓的理由

### 10.1 绑定配置 A/B 评测（已交付）

**用法**（变体文件与 `agentcrew.yaml` 同形，只写要覆盖的那一段）：

```bash
python main.py eval examples/evals/revision.jsonl \
  --variant base=variants/base.yaml \
  --variant rounds1=variants/rounds1.yaml \
  --matrix-out artifacts/evals/matrix.json
```

**输出**（实测，`revision` 数据集）：

```
配置矩阵 · 2 条 case × 2 个变体 · 每条跑 1 次
  base      pass^k=100.00%  calls=24  cost=$0.0161  修订=0
  rounds1   pass^k=100.00%  calls=30  cost=$0.0208  修订=2
            Δpass^k +0.00 · Δcalls +6 · Δcost $+0.0047 · Δ修订 +2
```

这就是矩阵要回答的问题：**开启复核修订，在这批 case 上多花 6 次调用 / +29% 成本，
通过率不变**。原因写在数据集注释里——mock 回放中主笔前后返回同一份产物，
"改得更好"是夹具声明的、不是真的发生了。结论应当是"这批 case 不值得开修订"，
而不是"修订没用"；要区分二者，需要真实模型通道。

**为让差异显形补的数据**（否则所有样例的复核都判「通过」，修订永不触发）：

| 件 | 内容 |
| --- | --- |
| `examples/samples/revision/` | 复制 `linear`，仅把 `reviews.json` 加一条 `"T-003@1"`（第 1 轮判首条标准不满足），第 2 轮起回到全满足 |
| `examples/evals/revision.jsonl` | 2 条 case：跑到底 + 复核确实发生。**刻意不断言"变好了"**——回放里没真变好，钉它等于自欺 |
| mock 回放 | `reviews.json` 支持 `"<task_id>@<轮次>"` 键，让"第 1 轮驳回、修订后通过"可表达；无此键时行为与之前逐字一致 |
| 门禁评审 | G2 用 `attempt=0` 取夹具（门禁不分轮次），避免误读角色内复核的 `@1` 裁决把好产物驳回 |

**落点**：`evals/runner.py` 新增 `load_variant_file` / `apply_overrides` /
`VariantResult` / `MatrixReport` / `run_matrix`；`CaseOutput` 与 `CaseResult` 增加
`cost_usd` / `llm_calls` / `revise_rounds`；CLI `eval --variant`。
变体归一化复用 `config.load_yaml_config`，与主配置**同一套规则**（含 profile key 回填）。

**三处刻意的防御**（都是被真实事故逼出来的）：

- 未知字段当场拒绝——`model_copy(update=...)` 会静默加属性，本仓历史上踩过"死字段"；
- **无法识别的顶层段当场拒绝**（`strict=True`）：第一版变体文件我按扁平字段名写了
  `agent_profiles` / `role_agents`，归一化直接丢弃 → 变体等于没生效 → 矩阵给出
  "两套配置完全相同"的假结论。现在这种拼写错误在起跑前就炸；
- 覆盖后走 `model_validate` 全量重建，嵌套约束（`review_rounds ≤ 3`）才真的生效。

### 10.2 共享工作区（以"分工依赖"的最小形态交付）

原方案写的是：

> `split_by_path` 的成员在开工前可读到同任务其它成员已写入的 scratch 文件。

问题在于 **`split_by_path` 是并行的**（`asyncio.gather`），并行成员之间没有"先后"，
"其它成员已写入"这个前提不成立。两条出路：

| 方案 | 代价 | 判断 |
| --- | --- | --- |
| A. section 一律串行 | 放弃并行收益 | 与"分工写"的初衷相反 |
| B. 任务内依赖声明 + 拓扑执行 | 要给 `Aggregation` 加边、按序派发 | **采用**：只有真需要先后时才串，其余保持并行 |

**已实现（R49）**：`Aggregation.intra_deps`（profile → 它要先看谁的产物）。

- 默认空 ⇒ 行为与之前**逐字一致**（仍然并行 `gather`）；
- 非空 ⇒ 该角色改走拓扑序，依赖方执行时上下文多一条 `UpstreamDigest`
  （对方摘要 + 产物路径；文件本就在盘上，可直接 `read_file`）；
- **依赖项失败不阻塞下游**，由 `quorum` 判定整体成败——否则一个 section 失败会让
  整条链静默停摆，连"哪几份产物缺失"都报不出来；
- 配置期校验：只能指向本角色的 section、不能自依赖、不能成环、只允许 `split_by_path`
  （成环的后果不是慢，而是永远跑不起来）；
- 成环兜底：运行期若仍遇到环（例如手工构造 `TeamAgent` 绕过校验），退回原序继续跑，
  **绝不死循环**。

**为什么停在"依赖声明"而不是"共享 scratch 目录"**：真正的需求是"谁先谁后"，
共享目录只是它的实现手段之一。把顺序表达出来之后，前序产物本来就在 `ArtifactStore`
里，不需要再造一层 scratch 目录——少一个状态源，少一类不一致。

---

## 11. 真实通道验证（2026-11-10）

mock 里的"改好了"是夹具声明的。本轮用 DeepSeek 真跑两次，回答"修订到底有没有用"。

### 11.1 跑批数据

| | 自动跑 A（7 任务） | 自动跑 B（7 任务） |
| --- | --- | --- |
| LLM 调用 | 37 | 32 |
| 估算成本 | $0.16 | $0.11 |
| 执行体 | be_primary 8 次 / $0.06 · be_reviewer 5 次 / $0.01 | be_primary 6 次 / $0.04 · be_reviewer 6 次 / $0.01 |
| 角色内复核 | 5 次（4 驳 1 过） | 6 次（4 驳 2 过） |
| 修订 | 3 次 | 3 次 |
| 结果 | 7/7 成功 | 7/7 成功 |

**修订确实改写了产物**：`artifact_written` 的 `sha256` 显示，同一路径在修订前后哈希全变
（T-03 五个文件、T-04 三个文件），不是"重跑一遍输出一样"。

### 11.2 三个只有真跑才看得见的问题

**① 复核材料被大文件挤掉，导致"指控不存在的东西"**

复核原文（真跑 B，T-04）：

> 未满足验收标准：README 片段给出安装与调用示例命令 —— 产物节选只包含 `src/logstat/cli.py`，未提供 README 片段……

而 ledger 里 `README.md` 有 **4 条 `artifact_written`**——文件一直在。原因在
`_artifact_excerpt` 的旧实现：预算**先到先得**，前面的 cli.py / renderer.py 吃完 1800
token 后，README 连文件名都没进材料。更糟的是，这类意见**靠修订永远修不掉**：
文件没缺，缺的是"给复核看"。

> 修复：预算按文件数**均分**，每份保底 180 token，超出的截断并标注。
> 约 30 行产品代码配一条回归测试（三个文件、首个超大，断言三个路径都出现）。

**② 角色内复核只留条数、不留原文**

`agent_review` 事件原先只有 `approved` / `issues`（条数）。真跑时想复盘"到底哪条不满足"
只能看到"issues=2"。已补 `detail`（截断 400 字）。

**③ 复核比门禁严格，但只有门禁能改任务状态**

同一次真跑里，T-03 的角色内复核判"2 条不满足"，任务最终仍是 `succeeded`——
因为 G2 门禁独立裁决且放行了。两个评审人对同一份产物结论不一致。

这不是 bug，是需要明确的产品决定：**角色内复核不参与任务状态**（当前设计）意味着它只能
触发"改一轮"，不能拦交付。若希望它能拦，需要一个"两票制"策略（门禁 + 复核都通过才算过），
那是 `Aggregation` 的新模式，不应顺手塞进现有实现。

### 11.3 对照验证（已补做）

修复前后用**同一目标、同一配置**各跑一次（PM 拆解有随机性，任务数 7 vs 8，因此比的是
**意见的性质**而不是精确计数）：

| | 修复前（跑 B） | 修复后（跑 C） |
| --- | --- | --- |
| 任务 / 调用 / 成本 | 7 / 32 / $0.11 | 8 / 28 / $0.10 |
| 角色内复核 | 6 次（4 驳 2 过） | 3 次（1 驳 2 过） |
| 修订 | 3 次 | 1 次 |
| **"指控文件不存在"型意见** | **4 条** | **0 条** |

修复前那 4 条（全部是误判，文件都在 `artifact_written` 里）：

- T-04 ×3：「未提供 README 片段」（README 写入 4 次）；
- T-03 ×1：「产物节选未包含任何单元测试文件」（`tests/test_parser.py`、
  `tests/test_stats.py` 均已写入）。

修复后的驳回换了一种说法（跑 C，T-04）：

> 未满足验收标准：CLI 行为与 docs/PRD.md 中的参数清单和输出定义一致 ——
> **产物节选被截断**，无法核对 docs/PRD.md 第 5 节参数清单……

差别在于：前者说"文件不存在"（**无法通过修订修复**，只能白跑），后者承认"材料被截断"
（可补材料、可判断）。且该任务一轮修订后即通过。

### 11.4 附带修掉的规划期误判

对照跑的第一次尝试直接被拒（`exit 2`）：验收标准里出现「与**设计文档**样例一致」
「按**设计文档**策略处理」，而任务交付物只有 `.py`。旧的文档一致性检查只看关键词，
于是判定"要求交付文档却没声明文档类交付物 → 该任务不可能通过"。

问题是这些句子在**引用另一份产物**，不是要求本任务写文档。修复：关键词前后加引用标记
判定（「与/按/依据/参照/符合 + 至多 4 个连接字」，或「一致/约定/对齐/定义/样例」），
同一个词出现多次时**只要有一次不是引用**仍判为要求文档（「输出文档需与设计文档一致」
不会被漏掉）。运行 58 个 planner 用例全绿。
