# agentcrew 模块重构说明

> 范围：`agentcrew/models`、`agentcrew/agents`、`agentcrew/crew`、`agentcrew/api`、`agentcrew/artifacts`、`agentcrew/tools`
> 原则：**只调整代码组织与依赖方向，不改对外接口、不改运行时行为**

---

## 一、结论

| 指标 | 改造前 | 改造后 |
| --- | --- | --- |
| `models/factory.py` | 2040 行 | 1794 行（-246） |
| `agents/base_agent.py` | 1184 行 | 1001 行（-183） |
| `crew/crew.py` | 1242 行 | 1178 行 |
| `api/server.py` | 1053 行 | 995 行 |
| 新增可独立测试的策略/工具模块 | 0 | 8 个，共 1320 行 |
| `models` 层是否依赖编排层 | 是（`runtime.ledger`、`task.task`） | 否 |
| 私有 `LedgerLike` 协议副本 | 2 份 | 0 份（统一到 `contracts.EventSink`） |
| 架构护栏测试 | 无 | `tests/test_architecture.py`（26 个用例） |

全量测试 1910 项：**1908 通过 / 1 跳过 / 1 失败**；唯一失败项 `test_board_view.py::test_live_is_refreshed_by_events_not_by_a_timer`
在改造前的 HEAD 副本上**同样失败**（rich 15 改了 `Live.refresh` 的节流行为，环境问题），与本次重构无关。

---

## 二、改造前的问题

### 1. `factory.py` 是「上帝模块」

单个文件 2040 行、63 个公开成员，同时承担六件互不相干的事：

- 别名解析与 Provider 构造（含 API Key 从环境读取、mock 全局降级）
- 路由决策（有序故障转移 / 权重 / rotate / usage 四种策略 + 自适应权重半衰期）
- 健康记录与冷却表（`_health`、`_cooldown_until`、`Retry-After` 截断）
- 配额预扣与预算软阈值
- 调用记账、响应缓存、会话亲和性、并发信号量
- 结构化输出的三级降级阶梯

后果：任何一处策略改动都要通读全文；策略无法脱离事件循环单测（必须先造出一个持有 provider 缓存的工厂）；文件已接近 IDE / 静态检查的可用上限。

### 2. 跨层反向依赖

```
models/factory.py      → runtime.ledger（编排层）、task.task（任务领域层）
models/complexity.py   → task.task
artifacts/store.py     → task.task
artifacts/store.py     → 自建私有 Protocol LedgerLike
tools/registry.py      → 自建私有 Protocol LedgerLike（与上一份重复）
```

基础设施层（Provider 适配、产物落盘）反向依赖上层实现，`Task` 加字段就会牵连模型层；两份 `LedgerLike` 还各自漂移。

### 3. 同段逻辑重复

- `call()` / `stream_call()` / `complete_structured()` 三处各抄一遍「配额预扣 → 信号量 → 脱敏 → 调用 → 记账 → 失败释放」。
- `base_agent.py` 里有 13 个模块级私有函数（`_render_pack`、`_section`、`_render_deliverables`、`_is_truncated`…）负责提示词渲染，与 Agent 执行流程混在一个类文件里。
- `api/server.py` 手写 HTTP 请求行/头解析、JSON 响应报文、SSE 帧拼装，协议细节与业务路由交织。

### 4. 私有别名跨模块流通

`_format_tokens`、`_render_pack` 这类带下划线的名字被 `crew.py` 之外的调用方和测试直接引用，语义上是「公开 API」却用了私有命名，改名只能全仓搜索。

### 5. 没有架构护栏

依赖方向只靠记忆和 code review 维持，删掉一个 `from agentcrew.runtime...` 没人拦得住。

---

## 三、重构目标与范围

**做**：

1. 把 `factory.py` 的**无状态策略**抽成独立模块，状态仍留在工厂（状态要活到整次 run，并被 API / 看板并发读）。
2. 打破跨层反向依赖：新增零依赖的契约层，用 `Protocol` 表达「形状」。
3. 消除重复：调用管线收敛为一条「候选计划 → 单次尝试」；渲染、报告、传输各自成模块。
4. 用测试把依赖方向钉死。

**不做**（刻意的边界）：

- 不改事件名、事件字段、日志与报告措辞（`llm_call` / `llm_routed` / `provider_health` / §14.3 中文报告文本）。
- 不改 CLI 参数、HTTP 路由、SSE 帧格式、产物目录布局。
- 不动 `roles → agents` 的单向导入（`roles/builtin.py` 汇总 `agents/*.py` 的 `ROLE`，是既有设计，且 `tests/test_agents_contract.py` 已锁定）。
- 不追逐行数：`prompting.py`（266 行）比原先内联的渲染代码更长，因为补了类型标注与契约说明。

---

## 四、新的分层与依赖规则

```
                    ┌──────────────────────────────┐
   入口层           │  api/    cli/    crew/       │
                    └──────────────┬───────────────┘
                                   │
   编排层           ┌──────────────┴───────────────┐
                    │  runtime/   gate/   task/    │
                    └──────────────┬───────────────┘
                                   │
   领域层           ┌──────────────┴───────────────┐
                    │  agents/   roles/  context/  │
                    └──────────────┬───────────────┘
                                   │
   基础设施层       ┌──────────────┴───────────────┐
                    │  models/  artifacts/  tools/ │
                    └──────────────┬───────────────┘
                                   │
   契约层           ┌──────────────┴───────────────┐
                    │  contracts.py   utils/       │
                    └──────────────────────────────┘
```

规则（由 `tests/test_architecture.py` 强制）：

- `contracts.py` 只依赖标准库，**不得** import 项目内任何模块。
- `models` / `artifacts` / `tools` **不得** import `runtime` / `task` / `crew` / `gate` / `api`。
- 只有入口层（`crew`、`api`、`cli`）可以 import `agents`。
- `roles/spec.py`、`roles/catalog.py` 不得 import `agents`。

依赖图现状（跨包边）：

```
models    -> config, contracts, utils          # 不再有 runtime / task
artifacts -> contracts, utils                  # 不再有 task
tools     -> contracts, mcp, utils
agents    -> artifacts, context, models, roles, runtime, task, tools, utils
crew      -> agents, artifacts, config, context, gate, knowledge, models, roles, runtime, task, utils
api       -> crew, models, runtime
```

---

## 五、变更清单

### 新增模块

| 文件 | 行数 | 职责 | 从哪来 |
| --- | --- | --- | --- |
| `agentcrew/contracts.py` | 79 | `EventSink` / `TaskLike` 两个结构化契约 | 吸收 `artifacts/store.py`、`tools/registry.py` 的两份私有 `LedgerLike`，并新增 `TaskLike` |
| `agentcrew/models/errors.py` | 77 | 模型层异常层次（`ModelFactoryError`、`ProviderUnavailableError`、`BudgetExceededError`…） | `factory.py` |
| `agentcrew/models/health.py` | 186 | `HealthRecord` / `HealthStatus`、冷却截止时间与判定、健康优先/冷却后置重排 | `factory.py` |
| `agentcrew/models/routing.py` | 291 | 四种路由策略（order / weighted / rotate / usage）、自适应权重半衰期、failover 候选序列 | `factory.py` |
| `agentcrew/models/budget.py` | 111 | 预算软阈值、输入价估算、上下文窗口预算校验 | `factory.py` |
| `agentcrew/agents/prompting.py` | 266 | 提示词渲染全套纯函数（上下文包、任务块、交付物、上游摘要、决策记录、摘录、截断提示） | `base_agent.py` 的 13 个模块级私有函数 |
| `agentcrew/crew/report.py` | 126 | §14.3 运行报告渲染（任务统计/返工/用量/通过率/耗时/产物）与格式化助手 | `crew.py` |
| `agentcrew/api/http.py` | 184 | 纯 stdlib HTTP 传输原语：请求解析、JSON/HTML 响应、SSE 头与帧、客户端 IP | `api/server.py` |

### 修改文件

| 文件 | 变更 |
| --- | --- |
| `models/factory.py` | 策略全部外移，改为**装配者 + 状态持有者**；三个调用入口共用 `_CandidatePlan` 与 `_attempt` / `_structured_attempt`；`ledger` 参数类型改为 `EventSink`，任务参数改为 `TaskLike` |
| `models/complexity.py` | `Task` 注解 → `TaskLike`，不再 import 任务领域层 |
| `artifacts/store.py` | 删除私有 `LedgerLike`，改用 `contracts.EventSink`；`task: Task` → `task: TaskLike`；保留 `LedgerLike = EventSink` 兼容别名 |
| `tools/registry.py` | 同上，删除第二份 `LedgerLike` |
| `agents/base_agent.py` | 渲染函数迁到 `prompting.py`，本模块保留同名下划线别名再导出 |
| `crew/crew.py` | `delivery_summary` 委托 `crew/report.py`；格式化函数保留别名 |
| `api/server.py` | 连接处理、`_send_json`、`_client_ip`、`_clamp_int`、SSE 帧全部委托 `api/http.py`，签名不变 |
| `tests/test_architecture.py` | 新增 26 个用例的架构护栏 |

### 兼容层约定

所有被搬走的私有函数，原模块都保留**同名再导出**：

```python
# agents/base_agent.py
_render_pack = render_pack
_section = section
...
# crew/crew.py
_format_tokens = report.format_tokens
_rework_line = report.rework_line
...
# artifacts/store.py
LedgerLike = EventSink
```

这样 `tests/` 与外部调用点一行不用改，重构的 diff 不会渗进测试文件——这也是判断「行为是否真的没变」的前提：测试是被验证的对象，不能跟着一起改。

---

## 六、关键设计决策

**1. 契约层用 `Protocol`，不用抽象基类。**
`EventSink.emit` 是结构化契约：`Ledger`、测试里的假记录器、`NullLedger` 都自动满足，无需注册或继承。用 ABC 会迫使 `runtime.ledger` 去继承一个基础设施层的类，依赖方向又反了。

**2. 策略无状态，状态归工厂。**
`routing.select()` 接收 `health`、`usage`、`rotate_counters`、`rng`，返回 `(alias, reason)`；它不持有任何东西。因此路由策略可以在**没有事件循环、没有 Provider** 的情况下单测——这是抽模块最大的收益，比行数减少重要得多。反过来，`_health`、`_cooldown_until`、`_usage_scores`、信号量仍然留在工厂，因为它们要活过整次 run 并被并发读。

**3. 能力差异由「计划形态」表达，不用复制分支。**
`_CandidatePlan` 有两种形态：有序（P0-2 故障转移）与权重（P3-10 / V7-P1）。两者对外都是 `start()` / `after_failure()`，差别只在内部如何取下一个。响应缓存与会话亲和性只挂在有序形态上——权重路由的语义是分摊流量，缓存命中会让某个别名永久吃掉全部请求。这类「哪些入口有哪些能力」的判断现在集中在一处，而不是散在四个入口的 `if` 里。

**4. HTTP 传输层保持 stdlib。**
`api/http.py` 只用 `asyncio` + `json`，不引入 aiohttp/starlette。项目的卖点之一是零重依赖，为了「代码好看」引入框架属于用架构换依赖，不划算。代价是 184 行手写报文，但这部分现在有独立测试，且与业务路由彻底解耦。

---

## 七、兼容性怎么证明的

重构最容易出错的地方是「看起来一样、边界上差一点」。因此每一项外移都做了对跑（新旧实现喂同一组输入，比对输出）：

| 验证项 | 方法 | 结果 |
| --- | --- | --- |
| 冷却时长 | 对 401/403/429/5xx/超时/未知 六类失败，比对新旧 `cooldown_minutes` | 一致 |
| 路由选择 | order / weighted / rotate / usage 四策略 × 多组健康与用量状态，含半衰期衰减 | 一致 |
| failover 候选顺序 | 7 组用例覆盖显式映射、隐式 fallback、健康重排、冷却后置、重复项去重 | 一致 |
| HTTP 报文 | 同请求比对新旧解析结果；JSON/HTML/SSE 响应比对**字节** | 一致 |
| 对外符号 | 遍历 `__all__` 与历史被引用符号，共 33 个 | 全部可导入，缺失 0 |
| 静态检查 | `ruff --select F,E9,B` | 新增 0 条（余 5 条均为既有问题） |
| 全量测试 | 重构前后各跑一遍 | 失败集合完全相同（各 1 项，同一用例）；该用例随后已定位为 `TERM=dumb` 并修复，现全量 **1911 passed / 0 failed** |
| 端到端 | 真起 `serve`，用裸 socket 打各分支 | `/health` 200、`/runs` 200、`/` 200（19581 字节 HTML）、未知路径 404、`POST /runs` 202、SSE 响应头与 `connected` 帧逐字正确 |

---

## 八、对相关模块的影响

- **`api/`**：路由、鉴权、限流、SSE 事件格式零变化；`_send_json` 等私有方法签名保留，故 40 余处调用点未动。看板页面改为按需 import，`dashboard.py` 的大字符串不再被每个 API 请求加载。
- **`studio/`（Electron 前端）**：不涉及。前端只吃 HTTP/SSE，这两层的对外形状未变。
- **`cli/`**：不涉及。CLI 通过 `crew` / `config` 交互，`crew.run()` 签名未变。
- **`gate/`**：不涉及。`gate` 依赖 `agents` 与 `context`，二者对外导出未变。
- **`task/`**：`Task` 类本身未改，但现在被 `models`、`artifacts` 以 `TaskLike` 协议消费——**今后给 `Task` 加字段，不会再牵连模型层与产物层**；反之，若删除 `TaskLike` 里声明的字段（`id`/`role`/`description`/`acceptance_criteria`/`dependencies`/`deliverables`），会在这三处立刻暴露。
- **测试**：除新增的 `tests/test_architecture.py` 外**未改动任何测试文件**。这既是兼容性的证据，也是约束——若有人为了让测试变绿去改断言，说明行为已经变了。

---

## 九、遗留事项

1. ~~`test_board_view.py` 的既有失败~~ → **已定位并修复**。根因不是 rich 版本本身，而是 `TERM=dumb`：rich 15 的
   `Live.refresh()` 在 dumb 终端下既不重绘也不报错（`is_terminal` 为真，走不到任何分支）。这同时是一个**生产缺陷**——
   CI / 精简 shell 上看板会停在首帧、事件全丢。修法两处：
   - `BoardView.start()`：把 `is_dumb_terminal` 与"非 TTY"同等对待，降级为逐行日志；
   - 测试：Live 刷新用例显式 `monkeypatch.setenv("TERM", "xterm-256color")`，并新增 dumb 终端降级的回归用例。
2. **`factory.py` 仍有 1794 行**：还能继续拆的是「Provider 构造 + Key 解析」（约 200 行）与「响应缓存/亲和性」（已在 `affinity.py`，仍被工厂直接持有）。再拆一次的收益不如这次明显，建议等到下次改这块需求时顺手做。
3. **`models/quota.py` 有两处未使用变量**（`now`）、`gate/reviewer.py` 有 `zip()` 未指定 `strict`，均为既有问题，未纳入本次范围。
