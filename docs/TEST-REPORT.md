# AgentCrew 功能测试报告（覆盖矩阵）

**报告时间**：2026-11-07 · **对应提交**：`792503d`（R22 后）
**口径**：本机 Python 3.12.10 + pytest 9.1.1，`--assert=plain`，分 5 批全量回归；
CI（GitHub Actions）四版本矩阵 3.10 / 3.11 / 3.12 / 3.13 同源用例全绿。

---

## 1. 结论摘要

| 指标 | 数值 |
|---|---|
| 用例总数 | **2501 collected / 2500 passed / 1 skipped / 0 failed** |
| 测试文件 | 120 个（`tests/`） |
| 源码规模 | `agentcrew/` 115 个模块（`mypy` 零错误范围） |
| 端到端场景 | 3 套离线样例（todo 9 任务 / linear 5 / hierarchy 7）100% 通过 |
| 质量门 | `ruff check .` 全绿 · `mypy agentcrew/` 零错误 · CI 四版本全绿 |
| 真实模型通道 | `doctor` 6/6（密钥已恢复，仅冒烟，不进 CI） |

**一句话**：引擎与管线的每个子系统都有独立用例簇 + 跨子系统端到端，
门禁三层、审批挂起恢复、委派治理、Webhook、MCP、SDK、CLI、HTTP API 全覆盖；
缺口不在用例广度，而在**行覆盖率没有度量**（见 §5）。

---

## 2. 模块覆盖矩阵（按子系统聚合）

计数口径：`pytest --collect-only` 每文件用例数，按该文件引用的主要子系统归组
（一个文件只计入一组；跨子系统端到端用例按被测主线归组）。

| 子系统 | 用例 | 文件 | 主要覆盖点 |
|---|---:|---:|---|
| 模型层 `models/` | 521 | 29 | 工厂与别名解析 · OpenAI/Anthropic 兼容 · 流式 · 故障转移与冷却 · 加权路由 · 配额（RPM/并发/预算）· 亲和性 · 定价 · 缓存 · 循环检测 · 复杂度校准 · 结构化输出 · provider 预设/健康/发现 |
| 产物与契约 `artifacts/` | 426 | 13 | 按 kind 解析校验（json/yaml/toml/python，病态输入不抛异常）· 产物仓库与冲突检测 · 契约校验 · 占位符检测 · 摘要器 · 静态检查 · 流式写盘 |
| 任务与调度 `task/` | 391 | 17 | 状态机跃迁表 · 面板派发/额度 · 规划器与 T-FINAL · checkpoint（含增量）· 条件分支图 · 调度器并发/死锁 · 停滞检测 · 审查去重 · gate 配置 |
| 运行时设施 `runtime/` | 222 | 13 | ledger 追加/坏行容忍/脱敏 · 审批单三态与跨进程消费 · 心跳/僵尸收敛/resume 锁与白名单 · Webhook（签名/重试/落盘重放）· 断点调试 · 模型热切换 |
| Agent 层 `agents/` | 134 | 6 | BaseAgent ReAct 循环 · 契约与架构守卫 · 多轮 · 自我纠正 · 工具生产接线 · 跨角色咨询 |
| HTTP API `api/` | 154 | 7 | 21 条路由逐条探测（契约守卫）· 认证 · 限流 · SSE · 看板 · 删除守卫 · 服务重启后读盘 |
| 门禁 `gate/` | 108 | 5 | G1 静态扩展 · G2 多评审并行/去重 · 主管审核（R7/R9）· 自动重派（R10）· 人工卡点（interactive/deferred）· 增量编辑 |
| 工具函数 `utils/` | 83 | 4 | 路径安全（符号链接/越界）· 日志脱敏 · 重试退避 · token 估算 |
| 角色 `roles/` | 78 | 4 | 目录与 reviewer 解析 · 动态子任务四道闸 · 委派被拒反馈 |
| 配置 `config/` | 81 | 5 | 五级合并顺序 · env/YAML/export/import 四通道 · 限流窗口 · 样例元数据 |
| 工具 `tools/` | 77 | 4 | 内置工具（run_python/web_fetch/write_file/…）· 权限 · 注册表 |
| MCP `mcp/` | 36 | 3 | stdio/SSE/HTTP 传输 · 客户端握手 · 生产接线与资源关闭 |
| SDK `sdk/` | 34 | 1 | 17 个方法 + `request()` 逃生口 · 异常族 · 真 serve 端到端 |
| 知识与经验 `knowledge/` | 32 | 2 | 实体记忆 · BM25 + 中文二元组检索 |
| CLI / 跨切面 | 40 | 3 | 子命令与退出码 · doctor · diff · README 链接一致性 |
| 编排 `crew/` | 79 | 3 | 两阶段编排 · 交付打回 · 主管 suggestions · 硬杀恢复 |
| 上下文包 `context/` | 5 | 1 |
| **合计** | **2501** | **120** | |

> 注：`test_crew.py`（51 例）与 `test_flow_runtime.py`（23 例）被归入
> 「编排」，虽然它们的导入分析会偏向 `agents` / `gate`——用例主线是被编排器
> 驱动的跨子系统行为。

---

## 3. 场景覆盖矩阵（按业务动作）

| 场景 | 用例来源 | 关键断言 |
|---|---|---|
| **规划 → 执行 → 交付全链路** | `test_crew` `test_end_to_end_mock` `test_samples` `test_flow_runtime` | 三套样例任务数/状态全对；计划校验错误回灌重拆；交付打回重建面板 |
| **三层门禁** | `test_gatekeeper` `test_reviewer` `test_g1_extended` `test_review_dedup` | G1 静态拦截 → G2 多评审聚合 → 主管验收；各层恰好一条 `gate_result` |
| **人工审批（挂起-跨进程恢复）** | `test_approval_interfaces` `test_flow_runtime` | plan/delivery 卡点建单 → 另一进程裁决 → resume 消费；未裁决重复挂起不重复建单 |
| **单步断点调试（R20）** | `test_debug_step` | 派发前挂起（attempts 未扣）· step/run/abort 三裁决 · 断点随快照跨进程存活 |
| **run 生命周期（R17/R22）** | `test_run_lifecycle` `test_run_lifecycle_hardening` `test_resume_whitelist` | 心跳 30s · 僵尸收敛（24h 回退）· resume 锁互斥/接管 · completed 拒绝 / 活 run 拒绝 / FAILED 放行 |
| **委派与协作（R6–R10）** | `test_delegation` `test_dynamic_subtasks` `test_peer_dialogue` `test_supervisor_review` `test_reassign` | 四道闸 · 咨询带上下文 · 主管建议不阻塞 · 驳回换人 |
| **条件下发与返工** | `test_graph` `test_task_board` `test_review_dedup` | 结果标签分支 · 互斥分支汇合 · 返工额度有界 · 产物 discard |
| **产物与契约校验** | `test_contract` `test_artifact_parser` `test_artifact_store` `test_static_checks` | 12 kind 显式分类 · 病态输入（深嵌套/超长整数/孤立代理项）只报错不抛 · 冲突检测 |
| **HTTP API 与契约守卫（R21）** | `test_api_server` `test_api_auth` `test_api_contract` `test_ledger_api` `test_delete_run` | 21 路由逐条探测 · 401/429/409/503 语义 · 行级越界归 400 |
| **SDK（R16）** | `test_sdk` | 17 方法 · 401/404/409/429 子类 · 真 serve + 真 run 端到端 |
| **CLI** | `test_cli` `test_cli_prompter` `test_doctor` `test_diff` `test_readme` | 退出码映射 · 非 TTY 降级 · 编辑会话 · 文档链接一致性 |
| **Webhook（R12/R14/R15）** | `test_webhook` `test_webhook_spool` | HMAC 签名 · 只推失败 · 落盘补投 · at-least-once 幂等键 |
| **MCP（R13/R22）** | `test_mcp_client` `test_mcp_transports` `test_mcp_wiring` | 三种传输 · 握手 · 工具前缀登记 · 管道显式关闭 |
| **模型层与成本（V5–V7）** | `test_model_factory` `test_routing_v7` `test_quota_v7` `test_pricing` `test_loop_guard` 等 29 文件 | 别名解析 · 故障转移冷却 · 配额预扣/归还 · 预算软阈值 · 循环检测 |
| **安全与脱敏（V7）** | `test_redact_v7` `test_logger_redaction` `test_files` `test_tool_permissions` | 密钥不落日志/快照 · 路径越界与符号链接 · 工具白名单 |
| **确定性 mock 基线** | `test_mock_fixtures` `test_mock_provider` `test_mock_no_bypass` | 夹具键控 · 不绕过门禁 · 错误路径可复现 |
| **架构守卫** | `test_architecture` `test_agents_contract` | 分层依赖方向 · Agent 契约 · SDK 不 import 主包 |

---

## 4. 质量门

| 门 | 命令 | 结果 |
|---|---|---|
| Lint | `ruff check .` | All checks passed |
| 类型 | `mypy agentcrew/` | 115 文件零错误（CI 范围；根 `main.py` 不在 mypy 范围） |
| 单测 | `pytest -q --assert=plain`（本机分 5 批） | 2500 passed / 1 skipped |
| CI 矩阵 | GitHub Actions `ci.yml` | 3.10 / 3.11 / 3.12 / 3.13 四 job 全绿（run `792503d`） |
| 离线样例 | `main.py run --sample {todo,linear,hierarchy} --no-live --auto` | 9/9 · 5/5 · 7/7 全部 completed |

**分 5 批的原因**：本机 pytest 断言重写在 Windows 上偶发段错误（EXIT 139），
与项目代码无关（详见 HANDOVER §6）；`--assert=plain` + 分批是完全覆盖的等价口径。

---

## 5. 已知边界（如实声明）

1. **没有行覆盖率数字**：项目不依赖 `coverage`/`pytest-cov`，本报告是
   「用例-模块-场景」矩阵，不是行覆盖百分比。若要补，接线成本约 0.5 人日
   （CI 上加 `--cov` 并设阈值），但 4 个 Python 版本的覆盖率合并需要额外配置。
2. **mock 与真实模型的行为差**：mock 夹具保证确定性，真实模型只做冒烟
   （`doctor` + 手工 6 次端到端）；提示词质量/模型漂移不在自动化范围。
3. **性能与容量**：`bench/` 有吞吐基线，但没有长期压测；`serve` 单进程多
   Crew 容量未做上限验证。
4. **平台差异**：CI 覆盖 Linux 四版本；Windows 仅本机 3.12 验证（分批口径），
   未进 CI 矩阵。
