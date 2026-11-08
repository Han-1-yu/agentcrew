# AgentCrew 开源对标调研与完善方案

> 调研日期：2026-11-08。方法：GitHub 官方仓库目录/源码/文档/许可证一手核验 + 本地 `E:\agentcrew` 源码通读。
> 本文是对《AgentCrew项目完善计划-整理版.md》的增量修订，不替代它，两者配合阅读。

## 0. 证据等级与声明

- **已核验（GitHub 一手）**：直接读取官方仓库目录结构与关键文件得到的事实，附仓库路径。
- **已核验（本地源码）**：直接读取 `E:\agentcrew` 源码得到的事实，附文件路径与行号。
- **归纳/建议**：跨项目横向归纳或针对 AgentCrew 的方案建议，非外部事实。
- 未在本文标注星数、下载量等易漂移数字；所有外部项目状态以 2026-11-08 仓库快照为准。

---

## 1. 执行摘要

1. **原计划主线正确，保留**：`P0 安全止血 → P1 可信基线 → P2 核心闭环 → P3 按需演进` 的分层完全成立，不与任何成熟项目的实践冲突。
2. **本次调研修正了原计划的 3 处严重度误判**（详见 §3）：
   - CLI Provider 的 `create_subprocess_shell` 是**配置项安全设计问题**，不是"远程 RCE"（模型无法注入 shell 命令）；
   - `run_python` 已有 `safe_relpath` + 符号链接逐段检查 + `-I` 隔离子进程 + 超时杀进程，不是"裸子进程"，缺的是**系统级/网络级沙箱**；
   - 看板 XSS 的 `innerHTML` 拼接确实存在，但攻击面是"有 API Key 的运营者/协作方"，不是完全匿名可利用。
3. **最值得借鉴的 6 项**（按 ROI，均来自核验）：
   - LangGraph 的 **super-step checkpoint + pending writes**（`libs/checkpoint`、`checkpoint-sqlite` 独立模块）；
   - OpenAI Agents SDK 的 **guardrail/tool_guardrails/handoffs/memory/tracing 模块化边界**；
   - Pydantic AI 的 **`pydantic_evals` + `pydantic_graph` + slim 分层 + THREAT_MODEL.md**；
   - Temporal 的 **event history / activity / 幂等 / 确定性重放语义**（只借语义，不引入 Server）；
   - Inspect AI 的 **Task/Solver/Scorer 评测抽象**（MIT）；
   - OTel GenAI 的 **`gen_ai.*` 语义约定**（独立仓库，`docs/gen-ai` 分层）。
4. **明确不引入**：AutoGen（官方已归档）、Semantic Kernel planners（已弃用）、n8n（Sustainable Use License，非 OSI）、Dify 多租户全家桶（Apache 2.0 修改版，附加多租户与 LOGO 限制）、任何分布式 runtime / K8s sandbox。
5. **新增 2 个原计划遗漏的改进点**（见 §5）：统一 run overrides 的"单点校验"缺失、配置错误与运行失败的退出码区分需要在 Settings 层集中实现。

---

## 2. AgentCrew 当前架构基线（本地源码核验）

### 2.1 模块划分（实际，非推测）

```
agentcrew/
├── agents/      8 个角色子类 + base_agent + registry + team + peer + prompting
├── task/        state_machine + task + task_board + graph（DAG）
├── crew/        crew + scheduler + report（确定性调度，无 LLM 调度）
├── runtime/     checkpoint + ledger + board_view + webhook + lifecycle
├── gate/        gatekeeper + reviewer + reassign + cli_prompter + static_checks
├── artifacts/   contract + parser + spec + store + summarizer
├── models/      factory + config + providers/{base,mock,openai_compat,anthropic,cli}
│                + pricing/structured/cache/discovery/routing/redact/quota/…
├── api/         server + http + auth + rbac + dashboard
├── mcp/         client + transports + tool + adapter + mcp_adapter
├── knowledge/   retrieval（自研 BM25 + CJK 二元组，无向量）
├── tools/       tool + registry + builtins + schema + network + mcp_adapter
└── cli/         命令子模块
```

### 2.2 关键设计资产（应保留）

- **确定性调度**：README 明确"调度决策全程没有 LLM 参与"，LLM 只在任务执行 + G2 评审两处调用。这与 LangGraph/CrewAI 的图/角色调度不同，是 AgentCrew 可测试性的来源。
- **单一事实源**：任务状态只在 `TaskBoard`，状态变更必须走 `TaskBoard.transition()` 状态机。
- **角色是数据**：8 角色差异集中在 `RoleSpec` 数据对象，新增角色零引擎改动。
- **契约先行**：完成由机器可判定的产物契约 + G1/G2/G3 门禁定义，不由 Agent 自称。
- **原子 checkpoint**：`checkpoint.py` 用 `atomic_write_text`（临时文件 + `os.replace`），保证"任何时刻只有旧的完整或新的完整内容"。
- **写入严、读取宽的 ledger**：`ledger.py` 逐行追加 + flush + fsync，读取端容忍崩溃截断尾行。

### 2.3 需要保留但待修正的现状

| 现状 | 核验结果 | 说明 |
|---|---|---|
| RBAC 路由授权 | 授权端 `server.py:522` 传**未 unquote 的原始 path** 给 `required_scope`（`rbac.py:386-391` 用正则匹配）；分发端 `server.py:784` 用 `unquote(s)` 解码后的 segment 路由 | **授权用未解码、分发用已解码，路径不一致，属真实高危**，需修 |
| SSE 事件总线 | `server.py:200-226` `EventBus.publish` 广播到所有订阅者，`_handle_sse` 订阅后不按 run_id 过滤 | **跨 run 广播属实** |
| CLI Provider | `cli.py:50-58` `command.format(...)` + `create_subprocess_shell` | 见 §3.1，降级为配置加固 |
| run_python 沙箱 | `builtins.py:188-255` 用 `create_subprocess_exec(sys.executable, "-I", "-c", code)` + `safe_relpath` + 逐段链接检查 | 无网络/系统级沙箱，但非"裸" |
| 真实流式 | `base.py:100-123` 默认退化；`mock.py:145` 唯一覆写 | `factory.py:886` 已有 `stream_call` 记账链路 |
| config_snapshot 脱敏 | `config.py:1094-1111` 已脱敏 provider_overrides/custom_providers/mcp_servers/webhook_secret | **api_keys 本身不进 snapshot**（走环境变量），原计划"api_keys 未脱敏"表述需修正 |

---

## 3. 原计划三处严重度修正（重要）

### 3.1 CLI Provider 注入：从 P0 RCE 降为 P1 配置加固

**核验**：`cli.py:49-58` 中 `command = self.binding.command.format(model=..., prompt=..., ...)`，`command` 模板来自 `agentcrew.yaml` 的 `provider_overrides.cli.command`（配置文件，非模型输出）；`prompt` 通过 `proc.communicate(input=prompt.encode())` 作为 **stdin** 传入，而非拼接进 shell 字符串。

**结论**：模型无法通过 `default_model` 或对话内容注入 shell 命令——`default_model` 只是模型别名，不进 shell。真正风险是**配置文件被篡改**（等于拿到本机配置写权限）。因此：
- 严重度：P0 → **P1（配置加固）**；
- 做法：改用 `create_subprocess_exec` + argv 模板（去掉 shell 解释层），`command` 只接受预置别名而非自由 shell 字符串。

### 3.2 run_python：从"裸子进程"修正为"缺系统级沙箱"

**核验**：`builtins.py:188-255` 已实现——`-I` 隔离模式、`cwd=workdir`、裁剪环境变量（只传 PATH/SYSTEMROOT/TEMP/TMP/PYTHONIOENCODING）、默认 10s 超时、超时杀进程；读/写工具走 `safe_relpath` + 逐段符号链接检查（`_crosses_link`），拒绝 `../`、绝对路径、盘符、Windows 保留名。

**结论**：这是**文件系统隔离 + 进程超时隔离**，不是"裸子进程"。缺的是：
- 网络隔离（`run_python` 可出网）；
- 资源上限（CPU/内存无硬限制）；
- 系统调用隔离（非 gVisor/容器级）。

**严重度**：P0 → **P1**。做法改为：默认不注册 `run_python`，启用时明确文档边界 + 可选 Docker/gVisor 后端（P3 才做），P0 只需"默认关闭 + 边界写清"。

### 3.3 看板 XSS：确认真实，但攻击面需澄清

**核验**：`dashboard.py:350-360` 的 `card.innerHTML = ... ${r.goal} ...` 直接用模板字符串拼 HTML，`goal` 来自 `POST /runs` 的请求体，未经转义。

**结论**：XSS 真实存在。但看板是运营/协作用面（需先能创建 run），`goal` 注入者是**持 API Key 的调用方**，攻击对象是**其他看板的运营者**——是"低权限用户跨站"而非"匿名反射"。仍应修（`textContent` + 转义 + CSP），但不应作为"匿名 RCE 级"排最前。

---

## 4. 开源对标（按方向，含核验证据）

### 4.1 编排与检查点

| 项目 | 已核验架构 | 对 AgentCrew 借鉴 |
|---|---|---|
| **LangGraph** | `libs/checkpoint`（独立包）+ `libs/checkpoint-sqlite` + `checkpoint-postgres` + `checkpoint-conformance`；checkpoint 按 super-step 落盘 | 借：super-step 快照 + pending writes 抽象；用 `checkpoint-conformance` 思路做"checkpoint 一致性测试套件"。**不借**：用图引擎替换现有 TaskBoard |
| **CrewAI** | 单仓 Python，`lib/` 分层，MIT 许可证；Flows/role/process 概念 | 借：工具级配额、显式事件；**不借**：把角色数量当能力、自由 GroupChat 当主流程 |
| **Microsoft Agent Framework** | 多语言（dotnet/go/python），有 `declarative-agents`、`skills`、`docs`，MIT | 借：可序列化 HITL、A2A/MCP 互操作；AutoGen 官方迁移目标 |
| **AutoGen** | README 明确 `[Archived]`、不再维护、不建议生产，迁移到 MAF | **不引入依赖**，只借 SelectorGroupChat/AgentTool 交互模式 |
| **OpenAI Agents SDK** | `guardrail.py`、`tool_guardrails.py`、`handoffs/`、`memory/`、`tracing/`、`sandbox/` 模块齐全 | 借：guardrail 输入/输出/tool 三态、handoffs 语义、memory scope |
| **Pydantic AI** | `pydantic_evals/`、`pydantic_graph/`、`pydantic_ai_slim/` 分层 + `THREAT_MODEL.md` | 借：typed RunContext、evals、威胁建模文档 |

### 4.2 持久化执行与恢复

| 项目 | 已核验架构 | 借鉴 |
|---|---|---|
| **Temporal SDK Python** | 单仓 SDK，`temporalio/` 实现，`uv.lock` 锁定，MIT | 借：workflow/activity 语义、event history、幂等键、确定性重放**思想**。**不引入 Server** |
| **Inspect AI**（评测侧也有，见 4.3）| — | — |

**核心结论**：AgentCrew 已有 `checkpoint.py` 的原子落盘 + `tasks.delta.jsonl` 增量 + `DELTA_COMPACTION_THRESHOLD=50`，已经走在"轻量 durable"正确方向上。缺的是**pending 状态**（正在等审批/正在调工具时崩溃无法恢复），这正是 LangGraph super-step 要补的，无需引入 Temporal。

### 4.3 可观测与评测

| 项目 | 已核验架构 | 借鉴 |
|---|---|---|
| **OTel GenAI** | 独立仓库 `open-telemetry/semantic-conventions-genai`，`docs/gen-ai` + `model/` YAML 定义，`reference/` 有 Python 合规矩阵 | 借：`gen_ai.*` span/metrics 语义；**不自定义一套** |
| **Inspect AI** | `src/` + `design/`，Task/Solver/Scorer 抽象，MIT，`.release-please` 自动化发布 | 借：评测抽象 + 离线/真实分层 |
| **Pydantic AI evals** | `pydantic_evals/` 独立目录 | 借：pass^k 多次运行成功率 |
| **Langfuse/Phoenix/OpenLIT/Opik** | 未逐一深核（本次只做方向性结论） | 建议：先 OTel OTLP 输出，Langfuse 作为可选后端，不早期自建重型 |

### 4.4 MCP / 记忆 / 沙箱

| 项目 | 已核验 | 借鉴 |
|---|---|---|
| **MCP specification** | 仓库为 `modelcontextprotocol/modelcontextprotocol`，`docs/specification/` 含 2024-11-05 → 2026-07-28 多版本 | AgentCrew `mcp/client.py` 现用 2025-03-26 协议；建议升级到 2026-07-28（Streamable HTTP），补 server 端 + OAuth |
| **Mem0 / Letta** | 方向性结论（未深核源码） | 借：user/agent/session scope；**不借**：向量库/图记忆先行 |
| **E2B / Daytona / gVisor** | 方向性 | 借：沙箱后端抽象；**不借**：云依赖、K8s |

### 4.5 软件团队与产品化

| 项目 | 已核验 | 借鉴 |
|---|---|---|
| **MetaGPT / OpenHands / SWE-agent / gpt-engineer / aider** | 方向性（未逐一深核） | 借：SOP、共享消息池、执行隔离；AgentCrew 已有 SOP + 角色，无需照搬 |
| **Dify** | `langgenius/dify`，`api/` + `web/` + `sdks/` + `packages/` monorepo；**Apache 2.0 修改版**，附加两条：未经书面授权不得运行多租户环境、不得移除/修改前端 LOGO 与版权信息 | 借：RAG 管线、插件机制；**不借**：多租户全家桶、附加限制条款 |
| **n8n** | `LICENSE.md` 明确 Sustainable Use License（**非 OSI**，禁止商用分发） | **不引入**，仅参考 workflow 可视化交互 |
| **Langflow / Flowise** | 方向性 | 借：可视化编排交互；不借：作为核心依赖 |

---

## 5. 对现有计划的修订：保留 / 调整 / 新增 / 不做

### 5.1 保留（原计划正确，不改）

- P0 整体顺序与"先安全止血再扩能力"的主线；
- P1 环境重建（标准 CPython 3.12）、编码修复、覆盖率门槛、可安装化；
- P2 原生 function calling → Checkpoint v2 → HITL → OTel → evals → guardrails 的顺序；
- P3 插件体系、复杂 Flow、durable 后端、MCP server 的"按需"定位；
- "不重写、不引入分布式 runtime、不早期做向量/图记忆、不以测试数量当质量"的原则。

### 5.2 调整（原计划有，但修正内容或严重度）

| 原项 | 调整为 | 依据 |
|---|---|---|
| SEC-P0-4 CLI 注入（P0） | **P1 配置加固**：`create_subprocess_exec` + argv 模板 + 命令别名白名单 | §3.1 |
| SEC-P0-2 run_python 裸沙箱（P0） | **P1**：默认不注册 + 边界文档化；P3 才做 Docker/gVisor 后端 | §3.2 |
| SEC-P1-2 看板 XSS（P1） | 保留 P1，但澄清攻击面（低权限→运营者，非匿名反射） | §3.3 |
| SEC-P1-1 config_snapshot 泄露 api_keys | 修正表述：`api_keys` 走环境变量不进 snapshot；已脱敏 provider_overrides/webhook_secret；**新增**补 `mcp_servers` 的 headers 中可能含 Bearer 的字段脱敏 | `config.py:1094-1111` |
| BAS-1 环境重建 | 保留，**补充**：用 `uv lock`（对齐 LangGraph/Pydantic AI/Temporal 均已用 uv 锁定的实践） | 多仓库核验 |
| P2-2 Checkpoint v2 | 保留，**明确**为"super-step + pending writes/requests"，并补 `checkpoint-conformance` 式一致性测试 | LangGraph `libs/checkpoint-conformance` |

### 5.3 新增（原计划遗漏，本次补充）

| 编号 | 新增项 | 依据 |
|---|---|---|
| P1-11 | **统一 run overrides 单点校验**：CLI/Web/SDK/Webhook 四端共用的覆盖项，收敛到一处白名单 + 类型校验（现 `_validate_run_options` 只覆盖 HTTP 端） | `server.py:51-186` 仅 HTTP 校验；SDK/Webhook 端未统一 |
| P1-12 | **配置错误退出码集中化**：非法配置在 `Settings` 加载期统一报错（退出码 3），删除散落的运行时兜底（如 `max_concurrency=0` 当前被兜成 run FAILED） | 原 D6；对齐 Pydantic AI 用 `Field(ge=)` 在加载期拦截 |
| P2-11 | **MCP 协议升级**：`mcp/client.py` 从 2025-03-26 升到 2026-07-28（Streamable HTTP），补 `outputSchema`/`structuredContent` | MCP 仓库多版本核验 |
| P3-9 | **THREAT_MODEL.md**：对齐 Pydantic AI，写威胁模型文档，把安全假设与攻击面显式化 | Pydantic AI 根目录 `THREAT_MODEL.md` |

### 5.4 不做（明确排除）

- 引入 AutoGen / Semantic Kernel 依赖；
- 用 LangGraph/FastAPI 重写；
- 引入 Temporal Server / DBOS / Restate / Hatchet 等外部 durable 服务（先本地 SQLite/JSONL 达成 pending 恢复）；
- 引入 n8n（Sustainable Use License）、Dify（Apache 2.0 修改版，含多租户与 LOGO 附加限制）作为依赖或组件；
- 早期自建 Langfuse 全套 / 向量库 / 图记忆 / K8s sandbox / 多租户。

---

## 6. 优先级实施矩阵

| 阶段 | 任务 | 关键依赖 | 风险 | 验收标准 |
|---|---|---|---|---|
| **P0** | RBAC 路径统一（`required_scope` 与路由分发用同一份 `unquote` 后规范路径，拒绝 `%2f/%5c/%00`/`.`/`..`） | 无 | 改动路由匹配需回归全部 `_ROUTE_SCOPES` | 遍历编码变体断言 401/403；原 `/%6beys/` 复现失效 |
| **P0** | SSE 事件按 `run_id` 分桶 | EventBus 增加 run 维度 | 慢客户端丢弃语义需保留 | 两个并发 run 事件互不串台 |
| **P0** | 认证默认开启 + 非 loopback 强制 | 无 | 向后兼容 CLI | `serve --host 0.0.0.0` 无密钥拒绝启动 |
| **P1** | CLI `create_subprocess_exec` + argv 模板 | 无 | 现有 command 模板格式迁移 | 注入字符串只作字面参数 |
| **P1** | run_python 默认不注册 + 边界文档 | 无 | 需更新 OPERATIONS.md | 默认配置不能执行 Python |
| **P1** | 统一 run overrides 单点校验（新增 P1-11） | `_validate_run_options` 抽为共享 | 四端行为需一致 | 四端传非法键均被拒 |
| **P1** | 环境重建 + uv lock + windows CI | 标准 CPython 3.12 | 锁文件首次生成 | 干净环境一条命令复现 |
| **P1** | 编码修复（非 TTY 强制 UTF-8）+ `--fail-on-recovered-crash` | 无 | 现有测试可能依赖 GBK 行为 | 标准 `pytest -q` 0 failed |
| **P1** | 覆盖率基线 + 门槛 | pytest-cov | 基线数字可能偏低 | CI 出报告，核心模块设门槛 |
| **P2** | 原生 function calling（tools/tool_choice + tool_call_id + 并行） | Provider 接口扩展 | 文本围栏回退路径需保留 | 原生与回退路径同任务同结果 |
| **P2** | Checkpoint v2（super-step + pending） | ledger/board 状态模型 | 与现有 delta 机制合并 | 从"等审批/等工具"状态可恢复 |
| **P2** | HITL 闭环（request→pending→resume(checkpoint, response)） | Checkpoint v2 | 幂等、部分审批 | 进程重启后仍可审批恢复 |
| **P2** | OTel GenAI（gen_ai.* + OTLP） | 无重型依赖 | 默认不采集 prompt/completion | 一次 run 一棵 trace |
| **P2** | evals（离线回归 + 真实 smoke + LLM judge） | Inspect AI/pydantic-evals 思路 | 真实模型成本 | 离线套件进 CI，夜间限额 smoke |
| **P2** | 工具级 guardrails（输入/输出/tool 三态，副作用前拦截） | 原生 function calling | 误拦截 | 注入用例被拦截留痕 |
| **P2** | 真实流式（Provider SSE → Settings → UI） | `stream_complete` 真实实现 | 跨 chunk 脱敏 | `curl -N` 看到增量 |
| **P3** | MCP 协议升级 + server 端 | MCP 2026-07-28 规范 | client 兼容 | 与官方 server 互通 |
| **P3** | THREAT_MODEL.md + ADR | 无 | 文档维护成本 | 威胁模型随安全改动更新 |

---

## 7. 里程碑与降级策略

- **M0（可安全继续开发）**：P0 全部完成。**降级**：若时间紧，P0 只做 RBAC 路径 + SSE 隔离 + 认证，其余 P0 并入 P1。
- **M1（结果可信）**：P1 全部完成，含新增 P1-11/P1-12。
- **M2（核心闭环）**：原生工具调用、Checkpoint v2、HITL、OTel、evals、guardrails 可串联。
- **M3（按需产品化）**：仅确有需求才做插件/复杂 Flow/durable 后端/MCP server/记忆。

**统一降级原则**：任何一步做不到"确定性 + 可测试 + 不引重型依赖"，就退回到"本地 SQLite/JSONL + 明确文档边界"，不强行上外部服务。

---

## 8. 事实核查清单

| # | 事实 | 来源 | 核验状态 |
|---|---|---|---|
| 1 | AutoGen 官方归档 | `microsoft/autogen` README `[Archived]` | ✅ 已核验 |
| 2 | AutoGen 迁移目标 MAF | 同上 README | ✅ 已核验 |
| 3 | LangGraph checkpoint 独立包 + sqlite/postgres | `langchain-ai/langgraph/libs/` | ✅ 已核验 |
| 4 | OpenAI Agents SDK guardrail/tool_guardrails/handoffs/memory/tracing 模块 | `openai/openai-agents-python/src/agents/` | ✅ 已核验 |
| 5 | Pydantic AI pydantic_evals/pydantic_graph/slim + THREAT_MODEL.md | `pydantic/pydantic-ai/` | ✅ 已核验 |
| 6 | Temporal SDK Python 单仓 + uv.lock + MIT | `temporalio/sdk-python/` | ✅ 已核验 |
| 7 | Inspect AI MIT + release-please | `UKGovernmentBEIS/inspect_ai/` | ✅ 已核验 |
| 8 | OTel GenAI 独立仓库 + docs/gen-ai | `open-telemetry/semantic-conventions-genai/` | ✅ 已核验 |
| 9 | MCP 多版本 spec（至 2026-07-28） | `modelcontextprotocol/modelcontextprotocol/docs/specification/` | ✅ 已核验 |
| 10 | n8n Sustainable Use License（非 OSI） | `n8n-io/n8n/LICENSE.md` | ✅ 已核验 |
| 11 | Dify 为 Apache 2.0 修改版（附加多租户禁止 + LOGO/版权不可移除） | `langgenius/dify/LICENSE` | ✅ 已核验（非标准 OSI Apache，含附加限制） |
| 12 | CrewAI MIT | `crewAIInc/crewAI/LICENSE` | ✅ 已核验 |
| 13 | AgentCrew CLI 用 create_subprocess_shell + format | `E:\agentcrew\agentcrew\models\providers\cli.py:49-58` | ✅ 本地核验 |
| 14 | AgentCrew 真实 Provider 未覆写 stream_complete | `base.py:100-123`、`mock.py:145` | ✅ 本地核验 |
| 15 | AgentCrew SSE EventBus 全局广播 | `server.py:200-226` | ✅ 本地核验 |
| 16 | AgentCrew RBAC 授权用未解码 path、分发用已解码 path | 授权 `server.py:522` + `rbac.py:386-391`；分发 `server.py:784` | ✅ 本地核验（两处路径处理不一致） |
| 17 | AgentCrew run_python 有 safe_relpath + -I + 超时 | `builtins.py:188-255` | ✅ 本地核验 |
| 18 | AgentCrew config_snapshot 已脱敏多字段 | `config.py:1094-1111` | ✅ 本地核验 |

---

## 9. 来源索引

- https://github.com/microsoft/autogen
- https://github.com/microsoft/agent-framework
- https://github.com/langchain-ai/langgraph
- https://github.com/crewAIInc/crewAI
- https://github.com/openai/openai-agents-python
- https://github.com/pydantic/pydantic-ai
- https://github.com/temporalio/sdk-python
- https://github.com/UKGovernmentBEIS/inspect_ai
- https://github.com/open-telemetry/semantic-conventions-genai
- https://github.com/modelcontextprotocol/modelcontextprotocol
- https://github.com/n8n-io/n8n
- https://github.com/langgenius/dify

---

## 10. 未决问题与需复验项

1. **部署边界**：当前服务是否已暴露到非回环地址/非可信用户？若是，P0 立即升级为发布阻断项。（待用户确认）
2. **产品定位**：默认保持"虚拟软件团队交付"垂直定位，通用框架能力后置。（待确认）
3. **真实模型评测预算**：夜间 smoke 的 Provider、额度、频率、脱敏要求。（待确认）
4. **Dify 许可证具体条款**：已读原文，确认为 Apache 2.0 修改版，附加两条——(a) 未经 Dify 书面授权不得用其源码运营多租户环境；(b) 使用其前端时不得移除/修改 LOGO 与版权信息（仅用后端不涉前端则不受此条约束）。作为"不引入"的充分理由，不构成引入前提。
5. **Langfuse/Phoenix/OpenLIT/Opik**：本次仅方向性结论，若决定引入具体某个，需再单独深核其部署与许可证。
6. **需 AgentCrew 源码进一步复验**：`_validate_run_options` 之外 SDK/Webhook 端的 options 处理现状（新增 P1-11 的前置核查）。
