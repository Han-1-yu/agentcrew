# 差距报告处置状态（GAP-ANALYSIS-2026-11-10）

> 最后更新：2026-11-12（全部条目关闭） · 对应 `docs/GAP-ANALYSIS-2026-11-10.md` 的 69 条登记。
> 本表是**交接用**的处置记录：哪条改了、改在哪、怎么验证、还剩什么。

## 汇总

| 类别 | 条目 | 已修复 | 部分 | 未做 |
|---|---|---|---|---|
| §1 P0 上线阻断 | 8（与 A/B 重叠，不重复计） | 8 | — | — |
| A · 崩溃一致性 / 门禁执行链 | 22 | 22 | — | — |
| B · API / MCP / Webhook / SDK | 15 | 15 | — | — |
| C · Studio 前端 | 17 | 17 | — | — |
| D · 契约与工程化 | 15 | 15 | — | — |
| **合计** | **69** | **69** | **0** | **0** |

全量回归：**3115 passed / 0 failed / 2 skipped**（`python scripts/test.py`）。
Studio：架构检查通过（73 模块 / 42 样式分片）、`test:unit` 18 passed。

## §1 P0（8 项，全部关闭）

| 编号 | 内容 | 落地 |
|---|---|---|
| P0-1 | Webhook SSRF（可控目标 + 跟随重定向 + 带 HMAC） | 仅 http/https、禁重定向、禁内网、超时上限 |
| P0-2 | `POST /runs/{id}/events` 缺 run 归属校验 | 补归属 + 事件名白名单 |
| P0-3 | SSE 事件名注入可伪造任意帧 | `valid_event_name` 严格校验 + 换行剥离 |
| P0-4 | `X-Forwarded-For` 无条件信任 → 限流可绕过 | 仅可信代理网段取值，否则用 peer |
| P0-5 | 未预料异常把 `str(exc)` 回显给客户端 | 统一 500 + 服务端日志留痕 |
| P0-6 | 全局成本硬上限是「事后」检查 | 记账即校验，并发下不再集体越线 |
| P0-7 | README 与实现相反（认证/上限/传输） | README 全量校正 |
| P0-8 | Studio 主进程把 Bearer Key 发给渲染端指定 `baseUrl` | 只认主进程落盘配置、`redirect: 'error'`、独立 `api:probe` 不带凭据 |

## A · 崩溃一致性与门禁执行链（22 项）

| 编号 | 要点 | 状态 | 批次 |
|---|---|---|---|
| A-1 | Webhook/抓取类出站 SSRF | ✅ | 1 |
| A-2 | 审批事件注入 | ✅ | 1 |
| A-3 | checkpoint 半写 | ✅ | 1 |
| A-4 | 封版与写盘顺序 | ✅ | 2 |
| A-5 | 孤立代理码点导致 `UnicodeEncodeError` 烧任务 | ✅ `errors="replace"` | 2 |
| A-6 | 心跳缺失时 fail-closed（防双写） | ✅ | 2 |
| A-7 | 墙钟差值：前跳误杀活 run、回拨死锁 | ✅ `runtime/clock.py` + 三处判定 fail-closed | 6 |
| A-8 | 门禁超时无上限 + 心跳 | ✅ | 3 |
| A-9 | LLM 调用无任务级超时兜底 | ✅ | 2 |
| A-10 | checkpoint 原子写与损坏恢复 | ✅ | 1 |
| A-11 / A-12 | MANIFEST 事务化（产物与清单一致） | ✅ | 2 |
| A-13 | 分页 `limit` 无上界 + 整份 JSONL 物化 | ✅ 与 B-8 同修 | 2 |
| A-14 | planner 校验缺口 | ✅ | 2 |
| A-15 | `_check_python` 兜底消息误写「YAML 解析失败」 | ✅ | 2 |
| A-16 | `tree()` 循环体 stat 未保护，并发删文件白烧 attempt | ✅ | 7 |
| A-17 | `_commit` 异常元组缺 `OSError` | ✅ | 2 |
| A-18 | 计数更新早于 `transition` 落盘 | ✅ | 2 |
| A-19 | `record_review` 不持久化（G2 长评审窗口） | ✅ `on_persist` 钩子 | 6 |
| A-20 | `CancelledError` 同步封版与 worker 交错 | ✅ | 2 |
| A-21 | `ModelFactoryError` 被吞进重试，应快速失败 | ✅ | 2 |
| A-22 | `max_cost_usd` 无 NaN/inf 校验 | ✅ `_validate_max_cost_usd` | 2 |
| A-23 | observer 异常跳过预算硬上限 | ✅ 判定提前 + emit 包 try | 8 |

## B · API / MCP / Webhook / SDK（15 项）

| 编号 | 要点 | 状态 | 批次 |
|---|---|---|---|
| B-6 | legacy 单密钥非恒定时间比较 | ✅ `secrets.compare_digest`（不短路、bytes 比较） | 7 |
| B-7 | webhook 重试/超时无上限 | ✅ ≤5 次 / ≤30s（validator 保留中文消息） | 7 |
| B-8 | `clamp_int` 无上界 | ✅ 加 `maximum` | 2 |
| B-9 | `runs` 注册表无上界 + `run_id=""` KeyError→500 | ✅ `MAX_TRACKED_RUNS=500` + 空 id 早返回 | 7 |
| B-10 / B-11 / B-12 | MCP 传输上限、同源校验、管道关闭 | ✅ | 3 |
| B-13 | MCP 传输层边界 | ✅ | 3 |
| B-16 | 请求头无长度/数量上限（drip 攻击） | ✅ | 3 |
| B-17 | `expires_at` 带时区时与 naive 比较 → 全站 500 | ✅ 落库前归一为 naive UTC | 2 |
| B-18 | `decide` 读-改-写无锁，并发裁决互相覆盖 | ✅ `threading.Lock` + 落盘复核 | 8 |
| B-19 | 错误码契约与实现不一致 | ✅ 统一 Body/形状，`docs`/`error-codes.md` 同步 | 3 |
| B-20 | SDK `timeout or self.timeout` 吞掉显式 0；SSE 超时=心跳 | ✅ `_request_timeout()` + `SSE_MIN_TIMEOUT_SECONDS=75` | 8 |
| B-21 | `GET /runs/{id}/agents` 未登记 scope → 恒放行 | ✅ 补 `SCOPE_RUNS_READ` | 7 |
| B-22 | `_validate_decision` 透传任意字段并落盘 | ✅ 只回 `{action, task_ids}` | 7 |
| B-1 ~ B-5、B-14、B-15 | 分别对应 §1 P0 与其它 API 加固 | ✅ 随 §1 与批次 2 关闭 | 1 / 2 |

## C · Studio（17 项）

| 编号 | 要点 | 状态 | 批次 |
|---|---|---|---|
| C-1 | 主进程窗口/生命周期缺陷 | ✅ | 3 |
| C-2 / C-17 | `provider:test`/`fetchModels` 无 scheme、私网、数量上限（主进程 SSRF） | ✅ | 3 |
| C-4 | 弹窗隐藏定时器不可取消 | ✅ | 5 |
| C-5 | 配额页状态错乱 | ✅ | 3 |
| C-6 | 新建 run 表单校验 | ✅ | 3 |
| C-7 | 测速期间下标错位，「采用」写错端点 | ✅ 快照 + 禁用增删 | 5 |
| C-8 | 路由切换未释放 flow，ResizeObserver 钉住游离 DOM | ✅ `_flowDispose` | 5 |
| C-10 | 三处静默吞错（agents/approvals/boot） | ✅ 可见错误态 | 5 |
| C-13 | 状态栏与模拟器状态漂移 | ✅ | 3 |
| C-14 | 计划页编辑丢改动 | ✅ | 3 |
| C-15 | 事件导出转义 | ✅ | 3 |
| C-16 | `escapeHtml` 漏引号 → 属性注入；CSP 不完整 | ✅ | 5 |
| C-18 | SSE 断线后彻底静止（无重连、无补 watch） | ✅ 连接成功即置活 + 指数退避 + 退避单测 | 5 |
| C-19 | 刷新代次缺失，旧响应覆盖新快照 | ✅ `currentLoadSeq` + `updated_at` 单调性 | 5 |
| C-20 | `spawnSync` 阻塞主进程最长 15s | ✅ 全链路 async（`runCli`） | 6 |
| C-21 | `provider:saveKey` 明文写 `.env`，与文档声明矛盾 | ✅ 按「本机开发工具」定位：文档如实声明 + `.env` 收 `0600` | 8 |
| C-22 | 主进程安全边界（settings 白名单、openPath、sandbox、外链） | ✅ | 5 |

## D · 契约与工程化（15 项）

| 编号 | 要点 | 状态 | 批次 |
|---|---|---|---|
| D-1 | 无 SIGTERM 优雅停机 | ✅ 清锁、收尾、退出码 | 3 |
| D-2 | openapi 无 `securitySchemes` / 401/413/429 | ✅ | 3 |
| D-3 | `pyproject` 与 `requirements.txt` 漂移 | ✅ `uv.lock` + CI 校验 | 2 |
| D-4 | `.env.example` 仅 11 键 vs 实际 49+ | ✅ 重写（去掉行内注释——dotenv 会连注释读成值） | 4 |
| D-6 | 无回滚流程 | ✅ `OPERATIONS.md` §6.1（代码/依赖/配置/密钥 + 三条边界） | 4 |
| D-7 | 无指标端点 / 告警无法接线 | ✅ `GET /metrics`（Prometheus 文本，不扫盘）+ 抓取示例 | 6 |
| D-8 | CI 无前端 job | ✅ `studio` job（npm ci + check + test:unit） | 4 |
| D-9 | 契约测试只断言「不是 404」 | ✅ 39 条新断言（逐路由 401 穷举、429、413、形状） | 8 |
| D-10 | 覆盖率无门槛 | ✅ `fail_under=50` + CI `--cov` | 4 |
| D-12 | `ci-check.ps1` 与 CI 不一致 | ✅ 补 pytest-cov 与 studio 步骤 | 4 |
| D-13 | 无配置样例 | ✅ `agentcrew.example.yaml`（零未知键校验） | 4 |
| D-14 | 时间戳 naive 本地时间 | ✅ 全量迁 aware UTC（含前端单调性比较与 `is_expired` 口径） | 6 / 8 |
| D-15 | 无 Dockerfile / compose | ✅ 三件套（非 root、健康检查、挂卷） | 4 |
| D-16 | 中断恢复与多实例边界靠手工 | ✅ 12 + 5 例覆盖（心跳/时钟/锁/状态矩阵/写盘原子性） | 8 |

### D-14（已完成）

- 落盘时间全部改 aware UTC：`run.json`、`RunStats`、`TaskResult`、`ReviewRecord`、
  `ApprovalRequest`（created/decided/expires）、ledger `ts`、产物 `written_at`、
  心跳与 resume 锁。`run_id` 目录名仍用本地时间（给人看）。
- 9 处时长计算改 `clock.seconds_between()`；`clock` 从 `runtime` 移到 **`utils`**
  （`models` 层禁止依赖 `runtime`，而 provider latency 也要算差值）。
- 迁移暴露并修掉两个真实缺陷：`Approvals.is_expired` 用 naive now 比 aware `expires_at`；
  Studio `realtime.js` 按字符串比较 ISO 串（混存时字典序与时刻不一致）。
- 守规 `tests/test_time_migration_guard.py` 的 `BASELINE` 已降为 **0**。
- 方案与回滚见 `docs/TIME-MIGRATION-PLAN.md`。

### D-16（已完成）

- `tests/test_resume_consistency.py`（12 例）：心跳被删的两种走向、时钟回拨时
  `reconcile` 报 `clock_skew`、锁的双持有/释放/外来锁、resume 状态矩阵、心跳清理。
- `tests/test_finalize_atomicity.py`（5 例）：注入点是 **`os.replace`**（标准库调用，
  不是内部方法名，重构不假失败）——硬失败（ENOSPC）保持旧内容完整且清理 tmp、
  瞬时钟（PermissionError）有界重试后成功、重试用尽原样抛、失败后可重试恢复、
  tasks 新 / record 旧的中间态仍可 resume（A-4/A-20 的顺序契约）。

## 需要真机/CI 才能验证的两项

| 项 | 现状 | 怎么验 |
|---|---|---|
| 覆盖率门槛 50% | 本机 Win 受插件抖动 + 沙箱拦截测不准，门槛只在 CI 生效 | 看首次 CI 的 `--cov-fail-under`；不足则补测试或下调 |
| Docker 构建 | 本机无 docker，仅做了 Dockerfile/compose 静态校验 | 在 CI 或目标机 `docker compose build`，重点验非 root（uid 10001）对 `output/` 的写权限 |
