# Magpie ↔ AgentCrew 逐模块对比分析

> 基于对 Magpie 源码（Go，AI 网关）的深度阅读，与 AgentCrew（Python，多智能体平台）现状的逐模块对比。
> 阅读范围：fallback.go(2452行)、routing.go(925行)、gateway.go(5247行)、concurrency.go、affinity.go、loop_guard.go、rpm.go、models.go(67KB)、presets.go(44KB)、provider.go(52KB)、account.go(53KB)、budget/、catalog/、redact/、format.go、prompt.go。

---

## 一、网关与路由层

### 1.1 故障转移（fallback.go + routing.go）

**Magpie 核心机制：**
- **holdWriter 缓冲写器**：在第一个真实内容（first content）流出前，所有上游响应先写入内存 buffer（上限 1MB / 15s）。一旦已 pass（流式已开始吐字 / 非流式已整体写出），即使中途断裂也不再换家。这是"agent 永远只看到一个完整答案"的关键。
- **失败分类驱动的差异化冷却矩阵**（`restAfterMarked`）：

| 失败类型 | 判定条件 | 冷却时长 |
|---|---|---|
| 欠费 (credit) | 402 / insufficient_quota 词 | **固定 30 分钟** |
| 限流 (rate) | 429 且限流词（非套餐词） | 基数 1 分钟，指数退避 1→2→4→…→30 分钟封顶；30 分钟不复发才清零计数；厂商 Retry-After 更大则采用，封顶 1 小时 |
| 套餐耗尽 (quota) | 429/400 且 usedUpWords | 15 分钟起步，取错误体 resets_at / 响应头 / 账号窗口重置时间，**封顶 8 天** |
| 其他 (other) | 5xx / 404 / 未知 | 连续失败计数 n++，1→2→4→8→10 分钟封顶 |
| 404 模型不存在 | 该 key 无此模型词 | **只冷却模型级键**（其他模型照常用） |
| 代理不通 / 请求形状错 | proxy / shape / auth | **零冷却**（换家就行，冷却只损失容量） |

- **冷却排序**：构建候选列表后，`restLast()` 把仍在冷却的候选排到队尾；列表只剩一个时即使冷却也照试。
- **SSE 事件分类器**：流式响应中的 error 事件（如 `response.failed`）被转成等价 HTTP 状态码，走同一套冷却逻辑。

**AgentCrew 现状：**
- 已有 `model_failover`（settings 中 per-alias 备用别名列表），支持有序故障转移和权重路由。
- 已有健康监控（P2-11），failover 时优先跳过 unavailable 的别名。
- **关键缺陷**：`_PROVIDER_UNAVAILABLE_TYPES` 只捕获 `TransientError / TimeoutError / ConnectionError`。而 OpenAICompatProvider 的 `_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}`——**402（余额不足）不在其中**，会立即失败而不触发故障转移。这正是用户痛点：DeepSeek API 余额不足（402）时整个运行失败。
- 无冷却时间概念：失败的别名不会被标记冷却，下次调用仍会先试它。
- 流式调用（`stream_call`）**明确不做故障转移**：注释说"流已吐出的块无法回滚"。

**差距评估：**
- 实施复杂度：中。holdWriter 模式在 Python async 里可用 `asyncio.Queue` + 缓冲列表实现。
- 预期收益：**极高**。直接解决 402 导致整个 run 失败的痛点。
- **结论：必须借鉴，P0 优先级。**

### 1.2 路由策略（routing.go）

**Magpie 核心机制：**
- 六种路由模式：smart（默认，按额度窗口分三档）、ordered（保持原序）、rotate（轮询）、usage（最少使用，带 tokenUse 半衰期衰减）、pace（节奏策略，"匀速花完配额"）、weighted（加权随机）。
- `tokenUse` 半衰期：`now(t) = n * 2^(-(t-at)/1h)`——每过一小时历史用量减半，让 least-used 路由自动"遗忘"历史。
- smart 模式的经济学排序：额度<90% 的 fine 档内，按"下一个窗口重置时间最早者优先"（即将过期的额度先花掉）。

**AgentCrew 现状：**
- 已有权重路由（P3-10）：failover 列表可配置 `{alias, weight}` 格式，按权重随机选择，失败后从剩余权重重选。
- 已有健康感知调整：unavailable 权重置 0，degraded 权重减半。
- 无 tokenUse 衰减、无 smart/pace 模式。

**差距评估：**
- AgentCrew 是单租户多智能体平台，不是多 key 聚合网关，smart/pace 路由的配额感知价值有限。
- **结论：暂不借鉴。现有权重路由 + 健康感知已足够。**

### 1.3 请求主流程（gateway.go）

**Magpie 核心机制：**
- `serve()` 是超长的"解析→构建候选链→逐个尝试"主循环。
- 候选循环的失败后 if-chain 有 13 个决策点，每条决定 continue/重试本家/break。
- 关键设计：**全部失败时，如果之前有更早的错误（非配额耗尽），报那个更早的错**——避免 agent 端见 quota 错误直接罢工。

**AgentCrew 现状：**
- `ModelFactory.call()` 的 failover 循环简洁：遍历 aliases，捕获 `_PROVIDER_UNAVAILABLE_TYPES`，记 `llm_failover` 事件，continue。
- 全部失败时抛 `ProviderUnavailableError`（包装最后一次错误）。

**差距评估：**
- AgentCrew 的循环结构已足够清晰，不需要照搬 Magpie 的 13 段 if-chain。
- **结论：不借鉴架构，只借鉴失败分类逻辑。**

---

## 二、网关支撑层

### 2.1 并发控制（concurrency.go）

**Magpie 核心机制：**
- per-key/per-account 的有界信号量 + FIFO 等待队列（`lanes` 结构）。
- 排队有 QueueLimit（队列长度上限）和 QueueWait（等待时长上限），双超时即拒绝并触发 fallback。
- `laneMate()` 让位扫描：本候选满时，向后扫描同 provider/同 group 的后续候选，找一个 lane 有空 + rpm 有空的提前插入。

**AgentCrew 现状：**
- 全局 `asyncio.Semaphore(settings.max_concurrency)`，默认容量 4。
- 无 per-provider 并发限制，无排队机制（信号量满了直接 await）。

**差距评估：**
- 实施复杂度：低。Python `asyncio.Semaphore` 已覆盖 80% 场景。
- 预期收益：中。防止某个 provider 的 429 影响全局。
- **结论：P0.5 优先级，添加 per-provider 并发信号量。**

### 2.2 亲和性（affinity.go）

**Magpie 核心机制：**
- 同一会话的请求钉在上次应答的 key/account 上，复用 vendor 侧 prompt cache。
- `cacheWorth=1024 tokens`：cache read 超过此值才值得跨 turn 粘住。
- `cacheCold=5min`：vendor prompt cache 存活期。
- `leastHeld()`：新对话首请求时，选当前被最少对话持有的候选，避免雪崩。

**AgentCrew 现状：**
- 无亲和性概念。每次调用按 failover 列表顺序/权重选择。

**差距评估：**
- AgentCrew 是单租户，通常一个 provider 只有一个 key，亲和性价值有限。
- 如果未来支持多 key 轮询，亲和性才有意义。
- **结论：暂不借鉴。**

### 2.3 循环检测（loop_guard.go）

**Magpie 核心机制：**
- 流式读取模型回复，切成"unit"（行 + 句），在滑动窗口内统计不同 unit 的去重数。
- `loopWindow=256 units`（约 600 tokens），`loopDistinct=24`：窗口内 distinct unit ≤ 24 即判定陷入循环。
- 环形缓冲 + FNV-64a 哈希计数，O(1) 滑动窗口 distinct 统计。
- reasoning channel 与 text channel 分别独立检测。
- 检测到循环立即截断回复并返回结构化错误。
- 额外的 run-on 检测：针对"无断点超长字符串"循环（如 "mcpmcp..."），周期前缀检测 + 词频统计。

**AgentCrew 现状：**
- 无循环检测。推理模型（deepseek-r1、qwen-qwq）陷入死循环时会一直烧 token 直到 max_tokens。

**差距评估：**
- 实施复杂度：中。环形缓冲 + 哈希计数在 Python 里用 `collections.deque` + dict 即可。
- 预期收益：**高**。多智能体平台最痛的就是推理模型死循环白烧 token。
- **结论：P1.5 优先级，必须借鉴。**

### 2.4 RPM 速率限制（rpm.go）

**Magpie 核心机制：**
- 滑动时间窗（不是令牌桶）：每个 who 维护 `[]time.Time`，记录每次请求计划发出时刻。
- 预约制：`reserve()` 返回未来可发时刻，`wait()` 阻塞到该时刻。
- `giveBack()`：被取消的请求把预约的 slot 吐回去。
- 与 concurrency 两级配合：先拿并发槽，再拿 RPM slot。

**AgentCrew 现状：**
- 无 RPM 限制。只有全局并发信号量。

**差距评估：**
- 实施复杂度：低。Python 用 `collections.deque` + `asyncio.sleep` 即可实现滑动窗口。
- 预期收益：中。防止多 agent 并发打同一个上游 key 触发 429。
- **结论：P0.5 优先级，与 per-provider 并发一起实现。**

---

## 三、Provider 与模型层

### 3.1 模型元数据（models.go + catalog/）

**Magpie 核心机制：**
- 三层叠加：(1) vendor API `/models` 实时拉取；(2) models.dev 开源目录运行时同步；(3) 用户手动覆盖。
- `catalog.Model` 完整字段：ID, Name, Price{Input,Output,CacheRead,CacheWrite,CacheWrite1h,Tiers}, Context, MaxContext, Output, Efforts, Images, APIs, Reasoning, Released 等 25+ 字段。
- 价格完全来自 models.dev API（`https://models.dev/api.json`），运行时拉取缓存到本地，**不硬编码**。
- 跨 provider 投票聚合：同一模型被多个 relay 服务时，取多数派的 context/output 值。
- id 归一化链：bareID → 去 Bedrock 地理前缀 → 去 `(high)` 后缀 → `4.6`↔`4-6` → relay 前缀重命名。
- 阶梯定价（Tiers）：按输入 token 量分段计价（如 OpenAI >272K 时涨价）。

**AgentCrew 现状：**
- `ProviderPreset` 已有：implementation, base_url, api_key_env, capabilities{max_context_tokens, max_output_tokens}, suggested_models, category, family, search_aliases, endpoint_candidates, api_key_url。
- **无价格字段**：input_price / output_price 不存在于 ProviderPreset。
- `pricing.py` 有一个小型 `MODEL_PRICING` dict（仅 9 个模型：gpt-4o-mini, gpt-4o, gpt-4.1, claude-sonnet-5, claude-opus, claude-haiku, deepseek-chat, deepseek-reasoner），用最长前缀匹配。
- `ProviderCapabilities` 有 `max_context_tokens=128000` 和 `max_output_tokens=8192`，但是**所有 provider 共用同一默认值**，不是 per-model 的。

**差距评估：**
- 实施复杂度：低。在 ProviderPreset 添加 4 个可选字段，为已知模型填充数据。
- 预期收益：中。Studio 展示价格/窗口信息，成本估算更准确。
- **结论：P1 优先级。不引入 models.dev 运行时同步（增加外部依赖），改为硬编码已知价格 + 留 None 标注未知。**

### 3.2 Provider 预设（presets.go）

**Magpie 核心机制：**
- 50+ 个内置预设，每个是 `PresetDef`，包含各协议 base URL、models.dev catalog 引用、Region 列表。
- 预设本身不内嵌价格数据——价格全部来自 models.dev。
- 支持 `Only` 前缀过滤（计划型套餐）、`NoList` 模式（无 /models API 的 vendor）、`Hosts`（托管其他厂商模型）。

**AgentCrew 现状：**
- 27 个内置预设（ProviderPreset），结构已比较完善。
- 无 Region 多端点、无 Only 过滤、无 NoList 标记。

**差距评估：**
- AgentCrew 的预设结构已满足当前需求。
- **结论：不借鉴。只添加价格/窗口元数据字段。**

### 3.3 Provider 核心（provider.go）

**Magpie 核心机制：**
- `Provider` 结构体有 25+ 字段，核心包括：Keys（多 key 列表）、Routing（路由策略）、MaxConcurrency、MaxRPM、QueueLimit、QueueWait、PriceRate、Contexts（用户自定义窗口覆盖）、Fallback、Affinity。
- 多 key 路由：一个 provider 可以配多个 key，支持 6 种路由策略。
- PriceRate 价格倍率：relay 可以按官方价的 0.8x 或 1.5x 计费。

**AgentCrew 现状：**
- `ProviderPreset` 无 MaxConcurrency / MaxRPM / Fallback 字段。
- 无多 key 路由（一个 provider 一个 api_key_env）。
- 无 PriceRate。

**差距评估：**
- 多 key 路由对 AgentCrew（单租户）价值有限。
- per-provider MaxConcurrency / MaxRPM 有价值。
- per-preset Fallback（fallback_models）有价值：让 provider 预设自带推荐的 fallback 模型，用户无需手动配置 model_failover。
- **结论：P0 添加 fallback_models，P0.5 添加 max_concurrency/max_rpm。**

### 3.4 账号管理（account.go）

**Magpie 核心机制：**
- `Account` 代表用户登录的 agent 订阅（Claude Code、Codex CLI 等），直接读取 agent 自己存在磁盘上的凭证。
- `Allowance` 多窗口配额：5小时窗口 + 周窗口，路由时取最紧张的。
- Pace 路由策略：选"如果匀速使用，重置时刚好花完"的账号。

**AgentCrew 现状：**
- 无账号概念。只有 API Key 模式。
- 无订阅配额窗口。

**差距评估：**
- AgentCrew 是 API Key 模式的多智能体平台，不做 agent 订阅账号聚合。
- **结论：不借鉴。**

---

## 四、预算与配额

### 4.1 预算控制（budget/）

**Magpie 核心机制：**
- 按 gateway key 维度的滚动日历窗口限额（day/week/month），多 key 独立。
- **预留（reservation）+ 事后结算（settle）两段式**：请求进入前按 body 大小 + 历史平均 output 预估占用，请求后用 vendor 真实 usage 结算。
- `usage.jsonl` 持久化，重启不丢；内存 sums 只是缓存。
- 超预算行为：拒绝新请求并返回 RetryAfter，**不自动降级到更便宜模型**。
- 窗口是本地时区的自然日/周/月，到点自动重置。

**AgentCrew 现状：**
- `max_cost_usd` / `max_llm_calls` 硬上限：每次记账后检查累计值，超限抛 `BudgetExhaustedError`，Crew 优雅收尾。
- 全局/单次 run 的一个数字，无窗口概念，无预留机制，重启即忘。
- 无自动降级到更便宜模型的逻辑。

**差距评估：**
- Magpie 的预算是多租户配额系统（每人每天/每月不能烧超过 X），AgentCrew 是单任务保险丝（这个任务别花太多钱）。
- 预留机制值得借鉴：并发请求的超额近似量化成"约一次调用"。
- 但 AgentCrew 的 run 级预算场景更简单，不需要日历窗口。
- **结论：P2 优先级。在现有 max_cost_usd 基础上，添加"超预算时自动切换到更便宜 fallback 模型"的逻辑。不引入完整的预留/窗口系统。**

---

## 五、其他有价值模块

### 5.1 数据脱敏（redact/）

**Magpie 核心机制：**
- 双向确定性占位符替换：发往 vendor 的请求里敏感值换成 `{{KIND_xxxxxxxx}}`，vendor 返回流里再原样换回来。
- 确定性占位符：HMAC-SHA256(key, kind+"\x00"+value) 取前 8 字节 base32，同一值永远同一占位符（保证 prompt cache 不失效、thinking signature 校验通过）。
- 20+ 敏感信息类别：API Key（10+ 种前缀）、JWT、密码、邮箱、中国身份证（加权模 11 校验）、中国手机号、银行卡（Luhn 校验）、SSN、护照、IBAN（mod-97 校验）、生日、MAC、IP（排除私有/版本号）、云存储 bucket 等。
- 性能优化：marker 预筛（文本里没有 `"sk-"` 直接跳过 API key 正则）、fold 窗口化、bitset bloom filter 加速已知值二次匹配。
- 流式还原：SSE 流里占位符可能被拆到两个 event，Writer 缓存末尾不完整的 placeholder 头。

**AgentCrew 现状：**
- 无数据脱敏。用户的 API key、文件路径、个人信息会直接出现在发往第三方模型的请求里。

**差距评估：**
- 实施复杂度：中高。正则规则多，但核心的双向占位符机制不复杂。
- 预期收益：中高。隐私保护，特别是发往第三方 API 时。
- **结论：P2.5 优先级，作为 opt-in 功能默认关闭。先实现核心类别（API Key、邮箱、手机号、身份证、IP），不追求全覆盖。**

### 5.2 结构化输出跨协议（format.go）

**Magpie 核心机制：**
- 统一 `Format` 结构，按目标协议重新编码。
- 上游不支持结构化输出时，降级为把 schema 写进 system prompt，并记住这个模型以后都走 prompt 路线。

**AgentCrew 现状：**
- `models/structured.py` 已有三级降级阶梯：json_schema → json_object → 围栏提取。
- 每级失败后自动降级，有 `llm_degraded` 事件留痕。

**差距评估：**
- AgentCrew 的结构化输出降级已比较完善。
- **结论：不借鉴。**

### 5.3 Prompt 组成分析（prompt.go）

**Magpie 核心机制：**
- 按 6 类拆解估算 token：system / tools / memory / files / results / chat。
- vendor 返回真实 usage 后用 `calibrate` 按比例缩放，差值补给最大项。

**AgentCrew 现状：**
- `estimate_tokens()` 简单按字符数估算（ASCII 4 字节/token，CJK 1 字符/token）。
- 无分项拆解。

**差距评估：**
- 分项 token 估算对上下文窗口管理/compact 决策有价值。
- **结论：暂不借鉴。当前简单估算已够用。**

---

## 六、最终实施清单（按 ROI 排序）

### P0：核心故障转移增强（解决用户痛点）

| # | 功能 | 借鉴来源 | 实施复杂度 | 预期收益 |
|---|---|---|---|---|
| 1 | **失败分类 + 冷却时间**：402=30min, 429=retry-after, 5xx=1min指数退避, 404=仅模型级 | fallback.go restAfterMarked | 中 | **极高**（解决 402 导致 run 失败） |
| 2 | **per-preset fallback_models**：ProviderPreset 添加 fallback_models 字段，默认从 suggested_models 取 | provider.go Fallback | 低 | 高（无需手动配置即可故障转移） |
| 3 | **流式故障转移**：缓冲第一个 chunk 前的响应，启用 holdWriter 模式 | fallback.go holdWriter | 中 | 高（流式调用也能故障转移） |

### P0.5：并发与速率限制

| # | 功能 | 借鉴来源 | 实施复杂度 | 预期收益 |
|---|---|---|---|---|
| 4 | **per-provider 并发信号量**：ProviderPreset 添加 max_concurrency，ModelFactory 按 provider 分桶 | concurrency.go lanes | 低 | 中（防止单 provider 429 影响全局） |
| 5 | **per-provider RPM 限制**：ProviderPreset 添加 max_rpm，滑动窗口预约制 | rpm.go | 低 | 中（防止触发 provider 429） |

### P1：模型元数据增强

| # | 功能 | 借鉴来源 | 实施复杂度 | 预期收益 |
|---|---|---|---|---|
| 6 | **价格/窗口元数据**：ProviderPreset 添加 input_price/output_price/context_window/max_output_tokens，为 27 个预设填充已知数据 | catalog.Model Price | 低 | 中（Studio 展示、成本估算更准） |
| 7 | **GET /providers 包含元数据**：API 响应添加价格/窗口字段 | — | 低 | 低（前端展示用） |

### P1.5：循环检测

| # | 功能 | 借鉴来源 | 实施复杂度 | 预期收益 |
|---|---|---|---|---|
| 8 | **流式循环检测**：滑动窗口 256 units / loopDistinct 24，FNV 哈希 + 环形缓冲，reasoning/text 双通道 | loop_guard.go | 中 | **高**（防止推理模型死循环烧 token） |

### P2：预算控制增强

| # | 功能 | 借鉴来源 | 实施复杂度 | 预期收益 |
|---|---|---|---|---|
| 9 | **超预算自动降级**：累计成本接近上限时，自动切换到更便宜的 fallback 模型 | budget.go（简化版） | 中 | 中（run 不中断，只是降配） |

### P2.5：数据脱敏（opt-in）

| # | 功能 | 借鉴来源 | 实施复杂度 | 预期收益 |
|---|---|---|---|---|
| 10 | **核心类别脱敏**：API Key、邮箱、手机号、身份证、IP，双向占位符替换 | redact.go（简化版） | 中高 | 中高（隐私保护） |

### 不实施（ROI 低或不适用）

- 路由策略 smart/pace/usage（AgentCrew 单租户，配额感知价值有限）
- 会话亲和性（单 key 场景无意义）
- 多 key 路由（单租户不需要）
- 账号订阅配额窗口（AgentCrew 是 API Key 模式）
- models.dev 运行时同步（增加外部依赖，硬编码已知价格即可）
- 结构化输出跨协议降级（已有三级阶梯）
- Prompt 分项 token 估算（当前简单估算够用）
- library 包（跨 agent CLI 配置分发，不适用）
- quota_wait CLI（独立工具，不适用）

---

## 七、实施约束

1. **向后兼容**：所有新字段必须有默认值（None / 空列表），现有 agentcrew.yaml 和 run.json 照常加载。
2. **测试只增不减**：每个新功能必须有回归测试，pytest 总数不得下降（基线约 1694 passed）。
3. **密钥安全**：不得在任何响应/日志/文件中泄露 API key 值。
4. **不破坏现有流程**：故障转移是 opt-in 或有合理默认，不改变现有单模型调用的行为。
5. **只修改 agentcrew/ 下的 Python 代码和 studio/ 下相关前端代码，不碰 Magpie 项目（只读参考）。**
6. **使用 .venv-ci\Scripts\python.exe 运行测试。**
