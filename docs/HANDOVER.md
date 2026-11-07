# 交接文档

> 给下一个接手的人。**先读这一页**，需要细节再跳 [README](../README.md)（怎么用）与
> [PROJECT-STATE](../PROJECT-STATE.md)（每轮做了什么、逐条设计取舍）。
>
> 本文只写三件事：**现在是什么状态**、**代码从哪读起**、**哪些地方会咬人**。

---

## 0. 当前状态（先看这段，别跳过）

| 项 | 值 |
|---|---|
| 已提交 | R3–R18；R18 是最新提交（具体 hash 以 `git log -1` 为准） |
| **未提交** | **无，工作区干净**（R18 已于 2026-11-07 提交） |
| 测试 | R26 后全量 **2573 passed / 1 skipped / 0 failed**（2574 collected；分 5 批实测） |
| 静态检查 | `ruff check .` 全绿 · `mypy agentcrew/` 114 文件零错误（本机 3.12 首次跑通 CI 等价检查链） |
| 离线样例 | `todo` 9/9 · `linear` 5/5 · `hierarchy` 7/7，全部 `completed` |
| 一句话定位 | 8 角色多 Agent 编排器：规划 → 执行 → 三层门禁 → 交付；可完全离线（mock）跑通 |

**接手第一件事**：跑一次三套样例确认环境（§5.1）。**第一档、第二档功能项与
全部小项均已闭环**（R17–R23：心跳收敛 / resume 锁、CI 检查链、限流窗口通道、
主管建议通道、单步断点调试、API 契约守卫、resume 白名单 + MCP 管道关闭、
测试覆盖矩阵、工具管理页与试跑、多用户 RBAC、可视化编排），提交已推送到远端（CI 四版本矩阵全绿）；
下一轮只剩 §7.4 的整块工程（多用户 RBAC / 拖拽画布 / 向量检索），
需要先定范围；工具管理页与试跑已由 R24 闭环。

---

## 1. 五分钟跑起来

```bash
pip install -r requirements.txt

# 不配任何密钥，用 mock 通道跑一遍完整流程
python main.py run --sample todo --no-live --auto
```

预期输出 `任务 9 个 · 成功 9 · 失败 0`，产物落在 `output/<run_id>/artifacts/`。

三套样例覆盖三种协作拓扑，都应当在 1–3 秒内跑完：

| 样例 | 拓扑 | 任务数 |
|---|---|---|
| `todo` | 串行为主 | 9 |
| `linear` | 纯串行 | 5 |
| `hierarchy` | 层级（经理派活） | 7 |

**接真实模型**：`AGENTCREW_API_KEY`（或 `OPENAI_API_KEY` / `DASHSCOPE_API_KEY` …）
+ `AGENTCREW_DEFAULT_MODEL=openai:gpt-4o-mini`。`python main.py doctor` 会告诉你缺什么。

**四种对外形态**：

```bash
python main.py run "目标"          # CLI
python main.py serve               # HTTP API + Web 面板（默认 127.0.0.1:8765）
pip install -e ./sdk               # 官方 Python SDK（零依赖，给别的程序调）
# MCP：作为客户端连别人的服务器（mcp_servers 配置）
#      ——注意目前**不是** MCP server，不能反过来被别的 agent 调用
```

---

## 2. 目录地图

```
agentcrew/
  contracts.py utils/     零项目内依赖（谁都可以依赖它们）
  roles/                  角色定义（8 个内置）+ 目录查询
  models/                 Provider 调用、别名解析、预算、缓存
  config.py               Settings 单一来源（YAML / env / API 三通道）
  task/                   任务模型 + 10 态状态机 + 规划器
  artifacts/              产物契约、校验、store（MANIFEST + sha256）
  context/                上下文装配与预算压缩
  agents/                 执行体（BaseAgent + 8 个角色实现）
  tools/                  6 个内置工具 + 注册表 + schema 强校验
  mcp/                    MCP 客户端 + 工具适配器（R13）
  gate/                   三层门禁：static / llm / 主管审核（+ 自动重派）
  knowledge/              经验库（BM25 检索）+ 实体记忆
  runtime/                ledger / checkpoint / webhook / approvals / 看板
  crew/                   编排层：Crew + Scheduler + 报告
  api/                    FastAPI + SSE + 内置 Web 面板
  cli/                    CLI 子命令实现
```

**分层是强制的**，`tests/test_architecture.py` 会扫 import 并失败。规则见该文件里的
`FORBIDDEN` 表：例如 `task` 不许依赖 `crew`，`gate` 不许依赖 `agents`。加新包时先想清楚
放哪层，别就地 import。

---

## 3. 三条主线（读代码从这进）

### 3.1 一次 run 的生命周期

```
Crew.run(goal)
  └─ _start()          ← 装配：ledger / store / 工具注册表 / MCP / models / agents
      ├─ 规划          PM 拆任务 → 计划评分 → 甘特依赖
      ├─ Scheduler     按依赖派发，并发受 max_concurrency 约束
      │    └─ Agent.execute() → 产物落盘 → Gatekeeper.evaluate()
      │         └─ G1 static → G2 llm →（R7 起）supervisor
      │              通过 → SUCCEEDED；驳回 → REWORK（带 fix_hints）
      └─ 交付          人类卡点（可选）→ 报告
  └─ _finalize()       ← 封版：写状态、锁 MANIFEST、发 run_finished、关账
```

关键在 `_finalize` + `run()/resume()` 的 `finally`：**收尾动作必须幂等**，因为正常路径
走 `_finalize`、异常路径只走 `finally`。

### 3.2 门禁与返工

`Gatekeeper.evaluate()` 是唯一裁决点，返回 `GateDecision{verdict, next_status, fix_hints}`。
驳回路径在 `_reject()`：

```
还有返工额度？
  ├─ 是 → （R10）主管驳回且配了 role_fallback → 换人 → REWORK
  └─ 否 → FAILED
```

额度 = `min(任务 max_reworks, gate_config.max_review_attempts)`。

### 3.3 委派与监督

| 环节 | 入口 |
|---|---|
| 模型请求建子任务 | `BaseAgent._maybe_create_subtasks`（解析 `<subtask>` 围栏） |
| 四道闸 | `Scheduler._create_subtask`（角色存在 / 权限 / 层数 / 总数） |
| 拒绝原因回灌 | `BaseAgent._retry_after_subtask_rejection`（R8，只回灌一轮） |
| 主管验收 | `gate/supervisor.py`（R7/R9） |
| 换人 | `gate/reassign.py`（R10） |

---

## 4. 配置全景

`Settings`（`agentcrew/config.py`）是唯一来源，三条写入通道：YAML 文件 / `AGENTCREW_*`
环境变量 / `POST /runs` 的 `options`。

**大多数能力默认关闭**，这是刻意的——升级不该静默改变成本结构。开了才会生效的：

| 开关 | 默认 | 开了会发生什么 |
|---|---|---|
| `tools_enabled` | `false` | Agent 能调工具（6 内置 + MCP），每任务多有限轮 ReAct |
| `entity_memory_enabled` | `false` | PM 上下文注入跨 run 实体 |
| `supervisor_review_enabled` | `false` | 主管额外验收一次（多一次 LLM 调用） |
| `supervisor_scope=all` | `delegated` | 规划期任务也要主管验收（每任务多一次调用） |
| `webhook_url` / `webhooks` | 空 | 关键节点推给外部 |
| `mcp_servers` | 空 | 连 MCP 服务器并登记其工具 |
| `llm_cache_enabled` | `false` | 同 prompt 的响应缓存 |
| `delegation_mode` | `any` | `manager_only` 时只有经理能派活 |

完整配置项见 `python main.py config export`。

---

## 5. 排障手册（开发者视角）

> 这一节回答「代码/流程为什么不对」。部署、进程托管、日志采集、备份扩容、升级、
> 上线前检查是运维视角，见 [OPERATIONS.md](OPERATIONS.md)；本文 §0 与 §7 只给状态与清单。
> 测试覆盖情况（2501 用例的模块/场景矩阵）见 [TEST-REPORT.md](TEST-REPORT.md)。

### 5.1 单条命令速查

```bash
# 全量回归：必须用 3.12.10 解释器 + --assert=plain，分 5 批
# （本机 pytest 断言重写在大量用例下会偶发 access violation / 段错误）
PY="C:/Users/33793/AppData/Local/Microsoft/WindowsApps/python.exe"
PYTHONPATH= "$PY" -m pytest tests/test_[a-b]*.py -q --assert=plain   # 448 passed, 1 skipped
PYTHONPATH= "$PY" -m pytest tests/test_[c-e]*.py -q --assert=plain   # 547
PYTHONPATH= "$PY" -m pytest tests/test_[f-l]*.py -q --assert=plain   # 300
PYTHONPATH= "$PY" -m pytest tests/test_[m-r]*.py -q --assert=plain   # 744
PYTHONPATH= "$PY" -m pytest tests/test_[s-z]*.py -q --assert=plain   # 405
# 累计 2444 passed / 1 skipped / 0 failed（2445 collected）

# R18 起 CI 等价静态检查（钉版本，与 .github/workflows/ci.yml 一致）
pip install "ruff==0.17.3" "mypy==2.4.1"
ruff check .
mypy agentcrew/

python main.py run --sample todo --no-live --auto       # 三套离线样例，各 1–3 秒
python main.py doctor               # 配置 / 依赖 / Provider 连通性
python main.py status <run_id>      # 任务表与状态
python main.py diff <run_a> <run_b> # 配置/任务/产物/事件差异
```

### 5.2 出问题去哪儿看

| 想知道的 | 看这个 |
|---|---|
| 到底发生了什么（逐事件） | `output/<run_id>/ledger.jsonl` |
| 任务为什么失败 | `tasks.json` 里该任务的状态 + ledger 的 `error` / `gate_result`（带 issues）；`python main.py status <run_id>` 是汇总视图 |
| 门禁驳回了什么 | ledger 里的 `gate_result`（带 issues / score / layer） |
| 评审为什么反复不过 | `gate_result` 的 `issues` 逐轮对比，或 `python main.py diff` |
| 配置到底生效了什么 | `output/<run_id>/run.json` 的 `config_snapshot`（密钥已打码） |
| 卡住了 / 停滞 | `output/<run_id>/diagnostics/stall-report.md` |
| 预算花在哪 | ledger 的 `llm_call`（含 tokens 与成本），或 `status` 的统计 |

### 5.3 典型症状 → 原因

| 症状 | 大概率原因 |
|---|---|
| 所有任务都是同一个通用角色在做 | 角色 key 拼错：R6 起会走四道闸拒绝并留 `subtask_rejected_by_policy`，**不再静默兜底**。查 ledger |
| 任务反复返工到 FAILED | `max_reworks` 太小，或 G2 评审标准过严。看 `gate_result.issues` 是否每轮都在变（在变=有进展，没变=提示词问题） |
| `awaiting_approval` 一直挂起 | 审批单在等裁决：`python main.py approvals <run_id>` 列单，`approve` 写裁决（可在另一个进程） |
| resume 报“状态非法” | checkpoint 与 ledger 不一致。`run.json` 的 status 是权威，`python main.py doctor` 会提示 |
| Webhook 一条都没收到 | 检查 `webhook_enabled` 事件里的 `targets`；R14 修过一个“只配 `webhooks` 时不生效”的 bug，见 §6.5 |
| 通知重复到达 / 一直没补投 | R15 是 at-least-once：多进程同时补投会重复，接收方按 `X-AgentCrew-Delivery` 幂等。没补投就看 `<output_dir>/_webhook_spool/` 有没有残留文件、ledger 里有没有 `replay_target_missing` / `replay_discarded` |
| MCP 工具没出现在提示词里 | `tools_enabled` 必须为 true；看 `mcp_connected` / `mcp_failed` |

---

## 6. 设计上的坑（读代码前必看）

### 6.1 状态机不允许 `REWORK → READY`

10 态契约是硬约束。想让返工换个执行者，只能**改 `task.role` 后仍走 `REWORK → RUNNING`**
（R10 就是这么做的）。不要为此加跃迁。

### 6.2 `Ledger.emit` 没有锁

每次 `open/append/fsync`，多线程并发写会交错。**任何后台线程都不要直接写 ledger**——
R12 的 Webhook 走的是「结果进队列 → 主线程 drain → 落账」。

### 6.3 工具注册表是「后登记覆盖先登记」

MCP 工具名由第三方定义，所以 R13 强制加 `server__` 前缀。新增任何工具都要考虑撞名。

### 6.4 事件的 data 键名不能与宿主函数参数撞

R12 踩过：结果字典用 `"event"` 作键，展开进 `ledger.emit(event, **data)` 直接
`TypeError`，**把整个 run 打挂**（实测 `exit_code=1`）。改名为 `webhook_event`。

### 6.5 单测覆盖了组件，不等于覆盖了入口

R14 最值钱的一课：`WebhookConfig` 的多接收端逻辑全绿，但 `Crew._build_webhook` 只看
`webhook_url`，只配 `webhooks` 时整条链路静默失效。**端到端实跑一次才暴露**。

新增功能时，至少跑一次真 run，别只看单测。

### 6.6 收尾顺序有讲究

`_finalize` 里先 `_close_webhook()`（排空队列）再 `ledger.close()`。反过来的话，投递结果的
回调会往已关闭的 ledger 里写。

同理，MCP 连接在 `run/resume` 的 `finally` 里关——异常路径不走 `_finalize`，不在这里关就
留下孤儿子进程。

### 6.7 本机（Windows + Git Bash）的已知环境问题

| 现象 | 处理 |
|---|---|
| 分 5 批跑测试偶发 `access violation` / `SystemError: bad format string` | 本机 pytest 在大量用例下的内存问题，**不是回归**。重跑或逐文件跑 |
| `SandboxError: center 规则快照不可用` | 环境问题，命令实际会执行（输出标注 bypassed），重试即可 |
| `api/server.py` 写入偶发被独占锁 | 用「写临时文件 → os.replace」原子替换 |
| 从宿主机继承 `os.environ` 起子进程时 `import ssl` 崩 / 127.0.0.1 请求极慢 | 宿主机注入的 `PYTHONPATH` shim 与 `HTTP_PROXY`。子进程用**白名单环境**（剔除 `PYTHON*` / `*PROXY*`），主进程设 `NO_PROXY=127.0.0.1,localhost`；R16 的 serve 端到端用例实测从 3 分 51 秒降到 3.19 秒 |

### 6.8 spool 里的记录不认「旧配置」

R15 的补投按 `sha256(url)[:16]` 匹配**当前**配置里的接收端，文件里不存 URL 与密钥。
改了接收端地址，旧记录会被丢弃并记 `replay_target_missing`——这是刻意的，不要「顺手」
改成按名字匹配：那样会把上个进程的事件推到一个全新的地址上。同理，往 spool 写
payload 前必须走 `secret_values()` 递归脱敏，别绕过 Ledger 已有的防护。

### 6.9 心跳与「进程还活着吗」的三个坑（R17）

1. **后台心跳只写自己的 `heartbeat.json`**：`Ledger.emit` 无锁（§6.2），从心跳线程
   写 ledger 会与主线程交错；写 `run.json` 则会和调度器的状态落盘竞争。
2. **别用 `os.kill(pid, 0)` 判进程存活**：Windows 上它走 `TerminateProcess`，
   「检查」可能直接把目标进程杀掉。存活一律看心跳时间戳，pid 只留作人工排查。
3. **没有心跳文件的老 run 必须回退 24h 旧判据**：升级后第一次启动若按新阈值扫，
   会把一批历史 `running` 全改成 `interrupted`。`awaiting_approval` 也不参与收敛——
   它的进程本就该退出，不是僵尸。

---

## 7. 剩余工作

### 7.1 已收口（2026-11-07）

R14 的第 3–5 批回归已补跑（5 批累计 2358 passed / 1 skipped / 0 failed），改动已提交；
随后按第一档清单完成 R15（Webhook 持久化重放队列）：新增 18 例、全量
2376 passed / 1 skipped / 0 failed、三套离线样例 9/9 5/5 7/7，含两次真实 CLI run 的
端到端验证。随后完成 R16（官方 Python SDK）：新增 34 例、全量
2410 passed / 1 skipped / 0 failed，含一次真 `serve` + 真 mock run 的端到端。
随后完成 R17（run 生命周期加固）：新增 24 例、全量
2434 passed / 1 skipped / 0 failed，含一次真 CLI 启动的 reconcile 端到端。
随后完成 R18（CI 就绪 + 限流窗口配置通道）：全仓 ruff / mypy 首次双绿、
新增 10 例、全量 2444 passed / 1 skipped / 0 failed。
PROJECT-STATE §3.19–§3.23 与本文 §0 的状态口径已对齐，工作区干净。

### 7.2 第一档（投入小、直接影响可用性）

| 项 | 预估 | 理由 |
|---|---|---|
| ~~运维部署/排障手册~~ | — | ✅ **已闭环**：`docs/OPERATIONS.md`（端口/认证/反代、配置与密钥、数据与备份、监控、升级、排障、上线清单） |
| ~~官方 SDK + 示例~~ | — | ✅ **R16 已闭环**：`sdk/README.md` + `sdk/agentcrew_sdk/` + 两个示例（见 PROJECT-STATE §3.21） |
| ~~Webhook 持久化重放队列~~ | — | ✅ **R15 已闭环**（见 PROJECT-STATE §3.20）：入队前落盘、启动补投、24h / 5 次淘汰；仍不保证「恰好一次」 |

**第一档已全部闭环（2026-11-07）**：三项均已完成并提交。第二档的稳定性两项
（过期 run 收敛、resume 锁）由 R17 闭环，两个小项（限流窗口通道、CI 静态检查
清理）由 R18 闭环。下一轮从 §7.3 剩下的功能项开始。

### 7.3 第二档（价值明确、改动面中等）

| 项 | 预估 | 说明 |
|---|---|---|
| ~~单步/断点调试~~ | — | ✅ **R20 已闭环**（见 PROJECT-STATE §3.25）：`debug_step` 每任务派发前挂起，`approve --decision step/run/abort` + `resume` 逐个放行 / 解除断点 / 取消 |
| ~~过期 run 自动 reconcile~~ | — | ✅ **R17 已闭环**（见 PROJECT-STATE §3.22）：30s 心跳 + CLI `run`/`serve` 启动时把孤儿 `running` 标为 `interrupted`，记 `run_reconciled` |
| ~~resume 锁文件~~ | — | ✅ **R17 已闭环**：`O_EXCL` 原子锁 + owner token + 陈旧接管；CLI 给可读提示、API 回 409 |
| ~~主管 `suggestions` 通道~~ | — | ✅ **R19 已闭环**：`Reviewer._finish()` 落主管层裁决时非空建议额外发 `supervisor_suggestions` 事件（不阻塞、不进返工提示），`status` 命令汇总最近 5 条（见 PROJECT-STATE §3.24） |
| ~~`api_rate_limit_window` 配置通道~~ | — | ✅ **R18 已闭环**：env / YAML / `config export` / `config import` 四通道 + `ge=1` 校验（见 PROJECT-STATE §3.23）；OPERATIONS §2.2 已同步 |

### 7.4 第三档（整块工程，需要先定范围）

| 项 | 预估 | 备注 |
|---|---|---|
| ~~多用户与 RBAC~~ | — | ✅ **R25 已闭环**：多密钥 + 角色/作用域 + run 归属 + 审计（见 PROJECT-STATE §3.30） |
| ~~拖拽式编排画布~~ | — | ✅ **R26 已闭环**：看板「编排」画布（拖拽/连线/属性面板/自动布局）+ YAML 双向 + 图校验 + 流程库（见 PROJECT-STATE §3.31） |
| ~~工具管理页 + 单工具试跑~~ | — | ✅ **R24 已闭环**：`tools`/`tool` CLI + `GET /tools`、`POST /tools/{name}/test` + 看板「工具」页；沙箱隔离、`tools_enabled` 未开时拒绝试跑（见 PROJECT-STATE §3.29） |
| 向量检索 | — | 缺 embedding 通道。分词/打分已独立成 `knowledge/retrieval.py`，替换点单点 |

### 7.5 不建议做

多级主管链、MCP resources/prompts、`<subtask>` 结构化纠错建议。收益模糊、复杂度不低，
属于“为了完整而完整”。

---

## 8. 改动历史（R3–R14）

每一轮的完整设计取舍在 PROJECT-STATE §3.8–§3.19，这里只给索引。

| 轮 | 主题 | 提交 |
|---|---|---|
| R3 | 工具链生产化（装配 / schema 强校验 / 两个新工具 / 实体记忆） | 并入 `540cd6e` |
| R4 | 流程运行时（结果分支 / 审批单三态 / 挂起与跨进程 resume） | 并入 `540cd6e` |
| R5 | 跨角色协作（`<message to>` 接成真实 consult 调用） | 并入 `540cd6e` |
| R6 | 委派治理（四道闸 / `delegated_by` 留痕） | 并入 `540cd6e` |
| R7 | 主管审核（谁派的活谁验收） | 并入 `540cd6e` |
| R8 | 拒绝原因回灌模型 | 并入 `540cd6e` |
| R9 | 主管审核覆盖规划期任务 | `303b27f` |
| R10 | 主管驳回后换人做 | `96beb6b` |
| R11 | 经验检索换 BM25 + 中文二元组 | `9e0652c` |
| R12 | 出站 Webhook | `24194a4` |
| R13 | MCP 接进生产工具链 | `c327df2` |
| R14 | Webhook 多接收端 + 只推失败 | 已提交（2026-11-07，`git log -1` 看 hash） |
| R15 | Webhook 持久化重放队列 | 已提交（2026-11-07） |
| 文档 | 运维部署/排障手册 `docs/OPERATIONS.md`；勘误 serve 端口（8000 → 8765）与 `clean` 保护默认值注释 | 已提交（2026-11-07） |
| R16 | 官方 Python SDK + 示例（`sdk/`） | 已提交（2026-11-07） |
| R17 | run 生命周期加固：心跳 / 僵尸收敛 / resume 锁 | 已提交（2026-11-07） |
| R18 | CI 就绪（ruff/mypy 清零）+ 限流窗口配置通道 | 已提交（2026-11-07） |
| R19 | 主管 `suggestions` 通道（`supervisor_suggestions` 事件 + `status` 汇总） | 已提交（2026-11-07） |
| R20 | 单步/断点调试（`debug_step` + `step` 审批卡点） | 已提交（2026-11-07） |
| R21 | API 契约守卫（`API_ROUTES` ↔ openapi 双向锁死 + 真服务逐条探测） | 已提交（2026-11-07） |
| R22 | resume 状态白名单 + MCP stdio 管道显式关闭 | 已提交（2026-11-07） |
| R23 | 功能测试报告：2501 用例的模块/场景覆盖矩阵（`docs/TEST-REPORT.md`） | 已提交（2026-11-07） |
| R24 | 工具管理页与单工具试跑（CLI/API/看板三入口，沙箱隔离） | 已提交（2026-11-07） |
| R25 | 多用户与 RBAC（密钥库 / 角色×作用域 / run 归属 / 审计身份） | 已提交（2026-11-07） |
| R26 | 拖拽式可视化编排（SVG 画布 + YAML 双向 + 图校验 + 流程库） | 已提交（2026-11-07） |

R3–R8 之所以合并成一个提交：`config.py` / `crew.py` / `main.py` / `api/server.py` 四个文件
被多轮同时改，`git add` 是文件粒度，按轮切分会把不同轮的改动硬塞进一个提交。

---

## 9. 接手建议

1. 先跑三套样例，确认环境正常（1 分钟）。
2. 读 `crew/crew.py` 的 `_start`（§3.1 那张图），这是全系统的装配中心。
3. 读 `gate/gatekeeper.py` 的 `evaluate` + `_reject`，理解“什么算通过”。
4. 从 ledger 里挑一个真实 run，按事件顺序走一遍——比读代码快。
5. 动手前先看 `tests/test_architecture.py` 的分层表，别把新代码放错层。
