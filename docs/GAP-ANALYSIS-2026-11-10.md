# AgentCrew 全栈查漏补缺报告

> **处理状态（2026-11-12）**：本文登记的 69 条已全部有结论——67 条已修复，D-14 / D-16
> 两项部分完成（功能风险已消除，剩余部分有方案与守规）。**逐条处置明细见
> [`docs/REMEDIATION-STATUS.md`](REMEDIATION-STATUS.md)**，本文保留原文作为审计痕迹，
> 不再单独维护状态。

- 审查日期：2026-11-10
- 审查对象：`E:/agentcrew`（Python 编排引擎 + 零依赖 HTTP/SSE 服务 + Electron Studio + 官方 SDK）
- 审查方式：四路并行静态审查（后端引擎 / API 与安全 / 前端 Studio / 工程化与运维），对 10 处最高风险结论做了逐行复核
- 基线：项目已具备完善的文档体系（`THREAT_MODEL.md` 46 条威胁 + 15 条已登记缺口、`TEST-REPORT.md` 覆盖矩阵、`adr/` 7 条决策、`OPERATIONS.md`），测试规模约 2500 用例 / 146 个测试文件。**本报告的定位是补充，不是重复**——已登记缺口不复述，只记录尚未被登记或已被登记但文档与实现漂移的部分。

---

## 0. 总体判断

四路合计产出 **85 条定位到文件与行号的发现**，归并重复后约 **78 项独立问题**：P0（立即修）8 项、P1（1–2 周）约 28 项、P2（中期）约 42 项。

一句话结论：**核心调度引擎（TaskBoard / DAG / 三层门禁 / Checkpoint）质量较高，风险并不集中在算法，而是集中在三处「后加的面」**：

1. **HTTP 控制面与 Studio 的信任边界**：新增的 `POST /runs` options 通道、Studio 主进程代理、MCP 传输，把原本「仅运维本地配置」的假设打破了，形成了 SSRF、凭据外发、跨 run 事件伪造等新攻击面。
2. **崩溃一致性**：多份文件（run.json / tasks.json / MANIFEST / ledger）各自原子但彼此不原子，加上若干「先改内存后落盘」的计数更新，在硬杀 / Ctrl-C 窗口下会产生自相矛盾的快照。
3. **文档—实现漂移**：`README.md`「已知限制」整节、`studio/README.md` 多处、`openapi.yaml`、`error-codes.md`、`TEST-REPORT.md` 的数字与默认值均已过期。这类问题不直接导致崩溃，但会**直接误导部署决策**（例如按「认证默认关闭」写启动脚本，服务会退 3 拒绝启动）。

> 阅读约定：每条按「现象 → 触发条件 → 潜在影响 → 最小修复 → 验证方式」组织。行号为 2026-11-10 审查时的定位，改动后请重新定位。

---

## 1. P0 · 立即修（可被直接利用 / 资金与数据一致性）

### P0-1 Webhook SSRF：API 用户可控目标 + `urlopen` 跟随重定向 + 携带 HMAC 密钥

- **证据**：`agentcrew/api/run_options.py:117-123` 只校验 scheme 与 `@`；`agentcrew/config.py:290-291` 明确注释「不做内网 IP 判断：这是运维显式配置的目标」；`agentcrew/runtime/webhook.py:563-566` `urlopen(request, timeout=...)` 默认跟随 30x；装配链 `agentcrew/api/server.py:839` → `agentcrew/crew/crew.py:1567-1572`。
- **触发**：持 `runs:write` 的普通 key 发
  ```json
  POST /runs  {"options": {"webhook_url": "http://169.254.169.254/latest/...", "webhook_events": ["*"]}}
  ```
  或给公网 URL 使其 302 到内网。
- **影响**：服务端以自身网络身份向云元数据、内网管理接口发 POST，且请求头带 `X-AgentCrew-Signature`（运维密钥 HMAC）。`THREAT_MODEL` 的 T-11/G-6 只覆盖 `web_fetch` 工具；`config.py` 的「运维显式配置」前提被 `POST /runs` 的 options 通道打破，属于**新攻击面**。
- **修复**：
  1. 出站 URL 只允许来自服务端配置/密钥库，**从 `validate_run_options` 白名单中移除 `webhook_url`**；确需保留则在双处（校验 + `_build_webhook`）拒绝回环/私网/链路本地/保留 IP 与非 80/443 端口，并在 DNS 解析后二次校验（防 rebinding）。
  2. `urlopen` 改用自定义 opener，移除 `HTTPRedirectHandler`（或对每跳重新校验）。
  3. 对同一 URL 的重定向链做跳数上限。
- **验证**：新增 `tests/test_webhook_ssrf.py`，参数化 `127.0.0.1` / `169.254.169.254` / `10.x` / IPv6 `::1` / `http://public@127.0.0.1` / 302→内网，断言拒绝且不产生出站连接。

### P0-2 `POST /runs/{id}/events` 是唯一缺 run 归属校验的 run 级端点

- **证据**：`agentcrew/api/server.py:725-739`，`data.setdefault("run_id", run_id)` 后直接 `self.on_event(event, data)`；对比同文件 `:522 / 598 / 623 / 964 / 1015 / 1086 / 1136 / 1156 / 1565 / 1608 / 1646 / 1665 / 1716 / 1976` 全部先调 `_deny_if_run_hidden`（定义 `:451-467`）。
- **触发**：任一 `runs:write` key 对别人的 `run_id` 发事件；或 body 自带 `"run_id": "<另一 run>"`（`setdefault` 不覆盖已有值）。
- **影响**：`on_event` 改写 `self.runs[run_id].status/goal/tasks` 并经 `EventBus.publish` 推给该 run 的所有 SSE 订阅者 → 跨 run 状态伪造与控制面注入，T-03/T-13 的隔离在此端点失效。
- **修复**：分支开头加 `if await self._deny_if_run_hidden(run_id, writer): return`；把 `setdefault` 改为强制覆盖 `data["run_id"] = run_id`；`_event` 白名单化（见 P0-3）。
- **验证**：契约守卫新增一条「遍历 `API_ROUTES` 中全部含 `{id}` 的写操作，断言均调用归属校验」，防止后续新增端点再漏。

### P0-3 SSE 事件名注入，可伪造任意帧

- **证据**：`agentcrew/api/http.py:190-198` `return f"event: {event}\ndata: {data}\n\n"`；入口 `agentcrew/api/server.py:735` `event = data.pop("_event", "custom")` 是裸字符串（`data` 走 `json.dumps` 会转义换行，`event` 不会）。
- **触发**：`{"_event": "x\ndata: {\"status\":\"completed\"}\n\nevent: y"}`。
- **影响**：闭合当前帧、插入任意 `event:`/`data:` 行，向所有订阅者伪造「审批已通过 / run 已完成」；与 P0-2 组合可跨 run 投递。
- **修复**：`sse_frame` 内对 `event` 用 `^[A-Za-z0-9._-]{1,64}$` 白名单，非法字符替换或拒绝；对字符串型 `data` 的 `\r`/`\n` 做逐行 `data:` 拆分（SSE 规范要求每个数据行单独前缀）；SDK `inject_event` 侧同步校验。
- **验证**：断言 `sse_frame` 输出中 `\n` 只出现在每个 `data:` 行末与帧尾；构造注入串后按 SSE 解析，事件名只能是 `y` 之外的白名单值。

### P0-4 `X-Forwarded-For` 无条件信任 → 限流完全可绕过

- **证据**：`agentcrew/api/http.py:102-113`，先于 `peername` 返回 XFF 首地址，无可信代理判定；消费点 `server.py:383-387`；`tests/test_api_auth.py:232-247` 把该行为钉成期望。
- **触发**：服务默认直连 `--host 127.0.0.1`（无反代），攻击者每请求换一个 XFF。
- **影响**：即使按运维手册打开 `AGENTCREW_API_RATE_LIMIT_ENABLED`，滑动窗口按伪造 IP 分桶恒不触发；`security_events.client_ip` 审计失真。
- **修复**：默认只用 `peername`；新增 `AGENTCREW_TRUSTED_PROXIES` 配置，仅当 peer 在列表内才取 XFF 最右侧可信跳。同步修改 `test_api_auth.py` 的期望（先补 fixture 声明可信代理）。
- **验证**：无配置时 200 次带随机 XFF 的请求应触发 429；配置可信代理后按真实客户端分桶。

### P0-5 未预料异常把 `str(exc)` 回显给客户端

- **证据**：`agentcrew/api/server.py:347-355` `{"error": f"服务端内部错误：{type(exc).__name__}: {exc}"}`，同类散见 `:810 / 1203 / 1221 / 1693 / 1742 / 1950 / 1966`。
- **影响**：异常消息常含绝对路径、配置片段、内部状态；T-33 只覆盖日志/ledger/spool 脱敏，**HTTP 错误响应这条通道未登记**；SDK `_message_from` 会把文案原样抛给调用方。
- **修复**：对外统一 `{"error": "服务端内部错误"}` + 一个 `request_id`；详情经 `secret_values()` 脱敏后只写服务端日志。集中在一个 `_send_internal_error()` 里，避免逐点改漏。
- **验证**：构造畸形 `tasks.json` / 非法 `expires_at` / 模型切换失败，断言响应体不含路径、不含 `Traceback`、不含任何 key 值。

### P0-6 全局成本硬上限是「事后」检查，并发下集体越线

- **证据**：`exhaustion_error` 全仓只在 `agentcrew/models/factory.py:1978-1985`（`_account`，调用已完成并记账之后）出现一次；`call()` `:894`、`stream_call()` `:956`、`complete_structured()` `:1090` 入口只有 `_guard_run_cost()`（`:1578`，执行体级 ceiling），不检查 `Settings.max_cost_usd` / `max_llm_calls`。`budget.py:121-126` 用严格 `>`。
- **触发**：配置 `max_cost_usd` / `max_llm_calls` 且 `max_concurrency > 1`。
- **影响**：触发上限的那次调用的钱已经花掉；N 个在飞请求各自完成记账后才各自抛错，硬上限退化为事后报警；`max_llm_calls=1` 会允许第 2 次调用完成后才抛。
- **修复**：在 `call` / `stream_call` / `complete_structured` 入口复用 `exhaustion_error` 做事前检查；把 `_estimate_request_tokens` 的预估成本纳入 `_quota_tracker.reserve` 预扣并在返回/异常时 `release`；`_account` 保留事后兜底；`exhaustion_error` 改 `>=`。
- **验证**：`max_llm_calls=1` + `max_concurrency=8` 并发 8 次，断言只有 1 次真正发出 provider 请求；`max_cost_usd` 用注入的计费表做边界测试。

### P0-7 README「认证默认关闭 / 64KB 上限 / 仅 stdio」与实现相反，会直接误导部署

- **证据**：`README.md:376-389` 称「默认关闭」、`README.md:2489-2490` 称「默认关闭、64KB 硬编码」、`README.md:2491-2495` 称「MCP 仅 stdio、Web UI 无产物预览」；实际 `agentcrew/config.py:741` 为 `api_auth_enabled: bool = True`，`main.py:1873-1888` 无密钥时退 3 拒绝启动、显式关认证还禁止绑非回环；`OPERATIONS.md:19-21 / 128-145 / 200` 为 fail-closed + 4 MiB；`OPERATIONS.md:348` 与 `THREAT_MODEL.md:252` 已有 streamable HTTP；`studio/src/features/` 已有 `artifacts/`、`detail/`。同一 README 的 `:135` 又写「认证 fail-closed」，自相矛盾。
- **影响**：按 README 写启动脚本 → 服务启动失败（可用性）；更危险的是运维据「默认关闭」跳过密钥配置与上线检查表，把安全假设建立在错误的文档上。
- **修复**：把 `README.md:376-399` 改写为「默认开启、无密钥拒绝启动、显式关闭仅限回环」；把 `:2464-2495` 整节标注为「V1/V2 历史快照，已被 R18–R30 取代」并给出当前值，或直接删除后统一指向 `OPERATIONS.md` / `THREAT_MODEL.md`。
- **验证**：加一条文档守卫测试，扫描 README 中的 `默认关闭` / `64KB` / `仅支持 stdio` 等失效表述并断言不存在。

### P0-8 Studio 主进程把 Bearer Key 发给渲染端指定的任意 `baseUrl`

- **证据**：`studio/backend-api.js:58` `const b = String(options.baseUrl || base())...`、`:66-68` 只校验 scheme、`:72-78` `headers: Object.assign({}, authHeaders(), ...)` + `redirect: 'follow'`；`studio/preload.js:56` 把 `apiRequest` 暴露给整个渲染进程；调用方 `studio/src/features/settings/connection.js:97-99` 传用户输入的地址。（`backend-api.js:60-61` 的注释自称「避免主进程被当成任意 URL 的代理」，与实际实现相反。）
- **触发**：在「设置 → 连接」填入攻击者地址点「测试连接」；或渲染端被注入后调用 `api:request`。
- **影响**：`safeStorage` 解密后的 API Key 以 `Authorization: Bearer` 发往任意主机，重定向进一步放大 → 凭据外泄 + SSRF。
- **修复**：`api:request` **忽略** `options.baseUrl`，只用 `getSettings().apiBaseUrl`；「测试连接」拆成独立 IPC handler 且**不注入** `authHeaders()`；`options.headers` 不允许覆盖 `Authorization`；`net.fetch` 改 `redirect: 'error'` 或对每跳重新校验。
- **验证**：`studio` 单测桩住 `net.fetch`，断言自定义 `baseUrl` 请求中不存在 `Authorization` 头。

---

## 2. P1 · 近期修（崩溃一致性 / 可靠性 / 异常分流）

### 2.1 崩溃与状态一致性（最高优先）

| 编号 | 位置 | 现象与影响 | 最小修复 |
|---|---|---|---|
| A-4 | `crew/crew.py:2159-2162`、`:2154` | 封版时 `run.json` / `tasks.json` / `MANIFEST.json` 三次写入各自原子但彼此不原子；`_finalized=True` 在写盘**之前**置位，中途 `OSError` 不回滚且兜底跳过 → resume 读到自相矛盾快照 | `_finalized` 移到三次写全部成功之后；三份先写同一临时目录再整体切换，或至少 `save_record` 放最后并捕获异常回滚状态字段 |
| A-6 | `crew/crew.py:625-633`、`runtime/lifecycle.py:68-88` | 心跳文件缺失/损坏时 `read_heartbeat` 返回 `None`，`RUNNING` 守卫因此 **fail-open**；`ResumeLock` 只在 `resume()` 占用，`run()`（`:655-676`）不占锁 → 两进程同写同一 run | `RUNNING` 且 `heartbeat is None` 时拒绝；`run()` 也占 `ResumeLock` |
| A-7 | `runtime/lifecycle.py:151/265`、`runtime/checkpoint.py:526` | 三处用 naive 本地时间做正向差值，时钟前跳 >15min 会把**活 run** 判为孤儿并改写/删除；回拨则死锁无法收敛 | 心跳与 `updated_at` 统一 UTC，负差值 fail-closed；或改用 `time.monotonic()` 基线的独立活性文件 |
| A-11/A-12 | `artifacts/store.py:253-266`、`:171-196` | `discard_task` 先删文件后写清单，写清单失败留下幽灵记录；`commit` 落盘文件后 MANIFEST 要等状态跃迁才写，硬杀产生「盘上有文件、清单无记录」→ 跨任务路径冲突检测失明、G1 看不到、返工清不掉 | 清单先行再删文件；`commit` 原子写成功后立即 `write_manifest()` |
| A-18/A-19 | `gate/gatekeeper.py:488-490`、`:240-245` | `bump_rework` / `record_review` 走 `_replace` 不触发 `on_change`，`transition` 已先落盘旧值；G2 长评审插在 record 与跃迁之间 → 硬杀后返工额度白送、评审轮次重算、收敛历史丢失 | 计数更新移到 `transition` 之前（与 `take_ready` 的 `bump_attempt` 对齐）；G1 裁决落可重放事件或让 `record_review` 触发持久化 |
| A-20 | `crew/crew.py:906-910`、`crew/scheduler.py:336-337` | `CancelledError` 分支直接 `_finalize` 同步写盘，但 `Scheduler.run()` 的 `finally` 不取消 workers → 封版写盘与在跑 worker 的 `commit`/`emit` 交错 | 取消分支先 `scheduler.cancel(hard=True)` 并 `await` 排空 workers，再 `_finalize` |

**回归验证（建议先补这一组）**：新增 `tests/test_resume_consistency.py`，用「两进程 + 删除心跳文件 + 在 `_finalize` 中途注入 `OSError`」三个场景，可同时暴露 A-4 / A-6 / A-7 / A-20。

### 2.2 门禁与执行链

| 编号 | 位置 | 现象与影响 | 最小修复 |
|---|---|---|---|
| A-5 | `gate/static_checks.py:107` | `len(content.encode("utf-8"))` 在 `parse_check` 之前且无 `try`，产物含孤立代理码点（截断的多字节输出）时抛 `UnicodeEncodeError` → `_handle_error` 在 REVIEW 阶段直接判 `FAILED`，无 fix_hints、无返工，一次格式异常烧掉整个任务 | `encode("utf-8", errors="replace")`，或捕获 `UnicodeError` 按 0 字节产出 issue |
| A-15 | `artifacts/contract.py:165-166` | `_check_python` 的兜底消息误写「YAML 解析失败」（复制粘贴残留）→ 返工提示让模型去改 YAML，正确 Python 产物被反复驳回直到烧光 `max_reworks` | 改为「Python 解析失败」 |
| A-8 | `crew/scheduler.py:362-368`、`:952-955` | `Gatekeeper.evaluate` 无任何超时包装，而停滞检测只看主循环跃迁；单次 G2 评审超过 `stall_timeout_seconds`（默认 300s）会被误判停滞、硬取消在跑 worker、退出码 4 | 给 `evaluate` 单独包 `asyncio.wait_for`，并让 Reviewer / ModelFactory 在每次调用与重试开始时回调 `_touch_progress()` |
| A-9 | `models/providers/openai_compat.py:196-197`、`anthropic.py:63-65` | client 未传 `timeout`（SDK 默认 600s），`_ATTEMPTS=4` 且 `APITimeoutError` 判为可重试 → 单次调用最坏 4×600s；门禁/评审路径无任务级兜底 | 构造 client 显式传 `httpx.Timeout`；新增 `llm_request_timeout_seconds` 接通 `Settings → factory → provider` |
| A-14 | `task/planner.py:79/120`、`task/task.py:113`、`crew/scheduler.py:449-451` | `timeout_seconds` 来自 PM 的结构化输出且无下界校验，`0`/负数让 `wait_for` 立即超时、每次执行烧一次 attempt | 字段加 `Field(300, ge=1, le=3600)`，`validate_plan` 兜底 |
| A-17 | `agents/base_agent.py:1540-1558` | `_commit` 的异常元组不含 `OSError`；多产物写入中途失败留下部分写入，且裸异常冒泡被收敛为执行失败 | 把 `OSError` 加入捕获元组并统一 `_fail`；`_commit` 开头先 `discard_task` 清理 |
| A-16 | `utils/files.py:195-201` | `tree()` 的 `try` 只包住 `iterdir()`，循环体内 `lstat()` / `is_dir()` 未保护；并发下另一任务删文件时会抛 `OSError` 白烧一次 attempt | 整个循环体纳入 `try/except OSError: continue` |
| A-21 | `crew/scheduler.py:380-383` | `except Exception` 把 `ModelFactoryError`（缺 Key / 非法别名）也吞进 3 次重试，本应「退出码 3 快速失败」变成任务 FAILED + 退出码 1 | `_worker` 对 `ModelFactoryError` 白名单重抛，或 `_start` 前置 `models.health_check()` |
| A-23 | `runtime/ledger.py:172-173` + `models/factory.py:1961-1985` | observer 异常（SSE 断连 / Webhook 序列化失败）在 `_account` 中位于 `consume` 之后、`exhaustion_error` 之前 → 事件已落盘但预算硬上限检查被整段跳过 | observer 调用包 `try/except Exception` + warning，与读取侧的宽容策略一致 |
| A-22 | `config.py:612`、`context/context_bus.py:250-253` | `max_cost_usd` 库层无 `ge`/`isfinite` 校验（NaN 使 `>` 恒 False，上限静默失效）；`room=0` 时仍发 `max_tokens=0` 的摘要请求，把无意义文本写进 `_summary_cache` | 给 `max_cost_usd` / `max_llm_calls` 加 `field_validator`；`room <= 0` 直接缓存空串跳过调用 |

### 2.3 API / MCP / Webhook 健壮性

| 编号 | 位置 | 现象与影响 | 最小修复 |
|---|---|---|---|
| B-7 | `api/run_options.py:62-65/141-145`、`runtime/webhook.py:499/516/566` | `webhook_max_retries` 只校验 `>=0`、`timeout` 只 `>0`，可传 1000000 次 / 3600s；每个 run 一个 daemon 线程，`close()` 只 `join(5s)` 后放手 | `validate_run_options` 加上界（`retries<=5`、`timeout<=30`），`WebhookConfig` 构造处 clamp |
| B-8/A-13 | `api/http.py:93-99`、`runtime/ledger.py:306-309` | `clamp_int` 只有下界，`limit=999999999` 合法；`query_events` 把整份 JSONL `list()` 全量物化后再切片，`count_events` 再遍历一遍 → 单次请求内存 ≈ 整个审计文件 | `clamp_int` 增 `maximum`（`limit<=1000`）；`query_events` 改 `itertools.islice` 流式跳过 offset，只物化 limit 条 |
| B-9 | `api/server.py:258-266` | `self.runs` 注册表无上界，唯一清理是 DELETE；body 显式给 `"run_id": ""` + `task_transition` 时 `self.runs[""]` 抛 `KeyError` → 500（且回显异常） | 只登记通过 `_validated_run_id` 且盘上确实存在的 run；设 LRU 上限；`on_event` 对空 `run_id` 直接 return |
| B-10/B-11/B-12 | `mcp/transports.py:95-111`、`:290-302`、`:435-459`、`:407-415` | stdio `readline()` 无行长上限且只捕获 `JSONDecodeError`（非法 UTF-8 让 `_read_loop` 静默死亡，此后每个 RPC 等 30s 超时）；`_iter_lines` 的 `buffer` 在无换行时无限增长，绕过 8 MiB `MAX_RESPONSE_BYTES`；旧 `SSETransport.connect` 不判 4xx/5xx 就视为成功 | `readline` 改带上限循环读；捕获 `UnicodeError`；把字节累计检查下放到 `_iter_lines`；`connect` 解析 status，`>=400` 抛 `MCPTransportError` |
| B-13 | `mcp/transports.py:425-433`、`:418-421` | SSE `endpoint` 事件给绝对 URL 就原样采用，后续带 `Authorization: Bearer <GH_TOKEN>` 的 JSON-RPC 被 POST 到攻击者地址 | `_resolve_endpoint` 校验与 `self._url` 同 scheme/host/port，不同源一律拒绝 |
| B-17 | `api/rbac.py:262-267`、`api/server.py:1400-1401` | `expires_at` 任意字符串直接落库；带时区的 ISO 串与 naive `now` 比较抛 `TypeError`（只 `except ValueError`）→ **全站所有请求 500** | `except (ValueError, TypeError)`；`KeyStore.add` 用 `fromisoformat` 校验并归一化为 naive UTC |
| B-18 | `runtime/approvals.py:169-198`、`:251-258` | `decide` 的「读 pending → 追加」无锁，两个并发裁决都被接受且归并时后写覆盖先写，与 T-05 的断言只在串行下成立 | 用文件锁 / `O_EXCL` 标记把检查+追加变原子；`all()` 忽略已 decided/consumed 单的后续 decide 行 |
| B-6 | `api/auth.py:72`、`api/rbac.py:324`、`main.py:1872` | legacy / 单密钥路径用 `presented in self._keys`，非恒定时间比较；`KeyStore.verify` 的 `compare_digest` + scrypt 不覆盖 legacy 分支 | legacy 分支改 `any(secrets.compare_digest(...))`，或统一写入 KeyStore |
| B-16 | `api/http.py:63-90` | 请求头解析无单行长度 / 数量 / 总字节 / 时间上限；`_BODY_READ_TIMEOUT` 只保护 body，header 阶段可 drip 攻击 | 单行超 8 KiB 即 400；限制 header 数量与总字节；整个 `read_request` 包 `asyncio.wait_for(..., 5)` |
| B-20 | `sdk/agentcrew_sdk/client.py:376-377`、`api/server.py:1996` | SDK SSE `settimeout(30)` 与服务端心跳间隔同为 30s，空闲期心跳到达前即超时断开；`timeout or self.timeout` 让显式 `0` 失效 | SDK 默认 timeout 提到 60s 或心跳缩到 10–15s；改 `timeout if timeout is not None else ...` |
| B-21 | `api/rbac.py:349-380` | `GET /runs/{id}/agents` 未登记 `_ROUTE_SCOPES`，`required_scope` 返回 `None` → `Principal.allows(None)` 恒 True | 补 `SCOPE_RUNS_READ`；加「`API_ROUTES ⊆ _ROUTE_SCOPES`」双向守卫测试 |
| B-22 | `runtime/approvals.py:97-118` | `_validate_decision` 末尾 `return dict(decision)` 透传任意额外字段并落盘回显 | 白名单只保留 `action` / `task_ids` |

### 2.4 契约与错误码一致性

- **B-15**：`api/http.py:40-46` 的 `REASONS` 未登记 201，而 `server.py:1331/1414` 用 201 → 响应为 `HTTP/1.1 201 OK`；`artifacts/contracts/error-codes.md:12-15` 又声称「代码中不存在 201」。补 `REASONS[201]` 并同步契约文档。
- **B-19/D-11**：`output_dir` 未配置时 `server.py` 有的端点返 500（`:968/1568/1611/1724`）、有的返 503（`:601/1234/1243/1312/1339/1473/1482`），`error-codes.md:25-27` 的定义自相矛盾。统一为 503。
- **D-11**：`error-codes.md` 与 `openapi.yaml` 都缺 401/413/429，而 `OPERATIONS.md:625-627` 把它们当一等公民；`error-codes.md:4` 自称汇总全栈，端点清单却只覆盖约 12 个（openapi 有 24 路径 / 32 操作）。以 openapi 为唯一事实源。

---

## 3. P2 · 中期修（前端体验 / 性能 / 工程化）

### 3.1 Studio 功能遗漏与「假功能」（用户问的「遗漏功能点」主要集中在这里）

**声明/渲染了但后端没有或不可达**：

| 位置 | 现象 |
|---|---|
| `studio/src/features/detail/panel.js:80-98` | 「重试/跳过/人工处理」按钮只在 `!S.realMode` 分支渲染，而按钮内部又判 `if (S.realMode ...)` → `main.js:889-912` 完整实现的 `task:action` 三种操作在真实模式下**完全不可达**（死代码） |
| `studio/src/app/boot.js:82-86` | 空格「暂停/恢复」只有 `e.preventDefault()` 和注释，无实现 |
| `studio/src/features/dashboard/gates.js:38/41`、`gates-page/index.js:54` | 「查看详情」只 toast；「重试门禁」只改本地对象，不调任何 IPC，真实模式下 500ms 后被快照覆盖 |
| `studio/src/features/detail/panel.js:98/126-127`、`dashboard/status-bar.js:79-80` | 「从此恢复」「查看事件」「查看产物」「重新运行」「从检查点继续」均只 toast / pushEvent |
| `studio/index.html:141-151`、`boot.js:57-58` | 通知按钮固定「暂无新通知」；用户菜单只有演示状态按钮，无账户体系 |
| `studio/index.html:28` + `features/runs/index.js:19-105` | 「搜索运行」只切换下拉，下拉里没有运行搜索框（只有任务搜索），与 `studio/README.md:23` 不符 |
| `studio/index.html:71` + `boot.js:47-48` | 「显示更多」是 `switchNav('tasks')`，不是展开列表 |

**必修建议**：要么接上对应 IPC/REST（`task:action` 已有后端，只需放开按钮；`resumeRun` / `gate:action` 可复用 REST），要么明确置灰并标注「演示 / 未实现」。**当前给出成功 toast 但不生效，比没有按钮更有害**——用户会误以为操作已执行。

### 3.2 Studio 确定的前端缺陷

| 编号 | 位置 | 现象与影响 | 最小修复 |
|---|---|---|---|
| C-1 | `studio/main.js:250/259/262/267` | `run.json` 缺 `created_at` 时 `createdAt || mtime` 回落成**数字**，`.localeCompare` 抛 `TypeError`（已用 node 复现）→ `runs:list` 整体 reject，被 `boot.js:136` 的 `catch {}` 吞掉后退回演示数据，用户看到「数据全丢」 | `String(...)` 或统一数值比较；无 `created_at` 时用 mtime 补 ISO 串 |
| C-4 | `studio/src/ui/modal.js:7` + `features/providers/modals.js:153` | `closeModal` 挂 200ms 定时隐藏，紧接着 `showModal` 打开新弹窗 → 200ms 后新弹窗被旧定时器隐藏 | `showModal` 开头 `clearTimeout(hideTimer)` |
| C-5 | `studio/src/features/settings/quota.js:30/44-50/84` | 「删除」只 `rowEl.remove()`，`rows` 数组仍持有 input 引用，保存时被删别名又写回 | 删除时同步 `rows.splice` 或保存时只遍历存活 DOM 行 |
| C-6 | `studio/src/features/new-run/index.js:125-150` | `before` 快照在 `startRun` **之后**取；快速启动/ mock 下新目录已进入 `before`，轮询 60s 恒找不到新 run | 快照提前到 `startRun` 之前，或直接用 `proc:run-dir`（`main.js:441`）推送的 `runDir` |
| C-7 | `studio/src/features/providers/modals.js:184-189` | 测速结果按数组下标映射回可变的 `urls`，请求在途期间增删端点会错位 → 「最快」判定与「采用」的 YAML 覆盖用错端点 | 固定 `snapshot=[...urls]` 回填，测速期间禁用增删 |
| C-8 | `studio/src/features/flow/index.js:265-272`、`app/router.js:36` | 切页时 `ph.innerHTML=''` 不调 `_flowDispose`，`ResizeObserver` 钉住游离 DOM | router 清空前遍历并调用 `_flowDispose` |
| C-10 | `studio/src/features/settings/agents.js:221-224/235`、`gates-page/approvals.js:110`、`app/boot.js:93/104/144` | `role.agents` 可能 undefined 导致 `TypeError`，被 `.catch(()=>{})` 全静默吞掉，界面永久停在「正在读取…」 | `(role.agents \|\| [])` 兜底；`.catch(e => body.appendChild(errorState(e)))` |
| C-13 | `studio/src/features/dashboard/status-bar.js:28`、`app/mock/simulator.js:93` | `parseTs()` 返回 null 后直接 `.getTime()`；非法时间串让 `renderDashboard()` 整体中断、看板空白 | `parseTs(x)?.getTime() ?? Date.now()` |
| C-14 | `studio/src/features/plan/index.js:43-45/113` | 删除任务后用 `tasks.length+1` 生成 ID → 删 `T-002` 后再添加得到重复的 `T-003`，DAG 分层与详情跳转错乱 | `Math.max(...ids)+1` 或 `crypto.randomUUID()` |
| C-15 | `studio/src/features/events/export.js:114-120` | 「导出报告」`a.click()` 后立即 `revokeObjectURL`（同文件另两个导出延迟 1000ms）→ 下载失败或 0 字节 | 统一 1000ms 延迟 |
| C-2/C-17 | `studio/main.js:973-992/997-1034/1037-1056` | `provider:test` / `fetchModels` / `testEndpoints` 对渲染端给的 `base_url` / `urls` 无 scheme、无私网、无数量上限校验，且 `fetchModels` 带 `process.env` 里的 Key → 主进程 SSRF + 内网端口探测 + 无上限并发 | 抽 `assertHttpUrl()` 统一校验；`urls<=20` 并限制并发 |
| C-21 | `studio/main.js:944-970` | `provider:saveKey` 把 API Key **明文写入项目根 `.env`** 并注入 `process.env`，与 `studio/README.md:90-91/152`、`backend-api.js:5-7` 的「密钥零接触 / 只加密存主进程」声明冲突 | 文档明确区分两套存储，或统一收进 `safeStorage` 由主进程临时注入；`value` 增加引号/`#`/换行转义校验 |
| C-22 | `studio/main.js:1064-1113`、`:88-95`、`preload.js:39-42/56/65-69` | 无 `setWindowOpenHandler` / `will-navigate`（`target="_blank"` 会在 Electron 新窗口渲染外站）、未显式 `sandbox`、`on(channel)` 任意频道、`saveSettings` 纯 `Object.assign` 无字段白名单（渲染端可改 `repoRoot` / `outputDir` 进而改变 `.env` 落点与 CLI `--output-dir`） | 注册窗口打开守卫；`sandbox: true`；preload 频道白名单；`saveSettings` 只接受 `defaultSettings()` 的键；`openPath` 校验在 `outputDir` 之下 |
| C-16 | `studio/src/lib/markdown.js:16-21/74-77` + `conversation/index.js:69/72/140` | `escapeHtml` 不转义 `"`/`'`，`href="${url}"` 可被 `"` 提前闭合注入属性（已复现）；CSP `script-src` 回退 `'self'` 挡住了内联事件，**不构成可执行 XSS**，但链接可被改写 | `escapeHtml` 补 `"`/`'`；改用 DOM `setAttribute`；补 `object-src`/`base-uri`/`form-action`，收紧 `connect-src` |
| C-18 | `studio/backend-api.js:94-111/120-190`、`app/realtime.js:25-50/100-132/145-172` | SSE 无重连、忽略 `id:`/`retry:`；后端未发 `connected` 帧时 `sseLive` 恒 false → 既不补文件监听又让 `data.js:170` 认为已连接；跨通道 3 秒去重窗口与本地收包时间戳导致事件翻倍/乱序 | `apiStreamStart` 返回 ok 即置连接成功；`onSseClosed` 无条件补 watch + 指数退避重连；透传 `Last-Event-ID`；消息按 `ts` 排序 |
| C-19 | `studio/src/app/realtime.js:214-223/237-258`、`app/data.js:117/125-126` | `data.js` 有 `loadSeq` 保护「换 run」竞态，但同 run 重刷与 SSE 防抖定时器之间只比对目录 → 旧 `loadRun` 响应可覆盖新快照，状态短暂回退 | 复用全局 `snapshotSeq`；`watch:snapshot` 载荷带 mtime，旧于当前则丢弃 |

**性能**：`studio/main.js:189-195/492/699` 的 `spawnSync`（最长 15s）+ 每分钟 `setInterval` 定时 tick 会**冻结整个应用所有窗口**；`boot.js:160` 1Hz 全量 chrome、`realtime.js` 每次状态事件触发 `renderDashboard()` 全量重建 SVG DAG；`dag-canvas.js:55-57/79` 拖拽每个 mousemove 同步写 localStorage。建议：`spawnSync` → 异步 `spawn`；按区块脏检查局部重绘；localStorage 写入做 rAF 节流；watch 快照改只发 diff。

**文档漂移**（`studio/README.md` 与实现大面积不符，按 README 验收会误判）：声称 9 页实际 12 页、供应商「第 9 页」实为第 10 项；数字键 1-9 说明与 `boot.js:78` / `about.js:16` 两套口径；「零 innerHTML 拼接」与 `conversation/index.js:69`、`artifacts/index.js:127` 矛盾；「传 `--model`」与 `main.js:775-779` 明确注释「会被 argparse 拒绝」矛盾；「运行结束自动弹统计」「LLM 调用次数与门禁比例图」「P1-P9 徽章」「最多 500 条事件」「四组卡片」均无对应实现（实际 7 组、50 条）。建议以代码为准一次性重写该 README，并加一条 e2e 断言页面数与 nav 项数一致。

### 3.3 工程化与运维

| 编号 | 依据 | 影响 | 最小修复 |
|---|---|---|---|
| D-1 | 全仓无 `add_signal_handler` / `SIGTERM`；`main.py:513/805/1940/2667` 只捕获 `KeyboardInterrupt`，`api/server.py:241` 有 `stop()` 但无信号入口；`OPERATIONS.md:231-232/552/715` 却要求 `systemctl stop` | systemd / `docker stop` 发 SIGTERM → 在途 run 不收尾、ledger 未 flush、Webhook spool 未补投 | `serve` 入口 `loop.add_signal_handler(SIGTERM, ...)` → `await server.stop()`；补停机测试 |
| D-2 | `artifacts/contracts/openapi.yaml` 全文无 `securitySchemes`/`security`，无 401/413/429；`:518` 仍写「所有事件原样广播给所有订阅者」，与 T-13 分桶实现相反 | 按契约生成的客户端不带 key 全 401；安全评审误判跨 run 隔离 | 补 Bearer / X-API-Key scheme + 全局 security + 401/413/429；修正 L518；加「每个 operation 必须声明 security」守卫 |
| D-3 | `pyproject.toml:6-7` 声称「与 requirements.txt 同一份清单，CI 会各跑一遍，不一致立刻红」，但 `ci.yml:36-42` **无比对步骤**；`requirements.txt:9-11` 把 pytest 混进运行时；运行时依赖只有 `>=` 无上界，pip 路径无 constraints / 哈希，仅 uv 路径有 `uv.lock`；`THREAT_MODEL.md:241/214` 写「无 lock 文件」与事实矛盾 | `pip install` 拉到 openai 2.x / pydantic 3.x 可在无代码改动下使生产启动失败；测试框架进生产镜像 | requirements 只留运行时依赖；生成 `constraints.txt` + 哈希；CI 增清单一致性校验；修正 G-4/T-38 为「pip 路径无锁」 |
| D-4 | `.env.example` 仅 11 个 `AGENTCREW_*`，而 `config.py:42-103` 声明约 60 个；`OPERATIONS.md:305-321` 的「生产必配」`API_AUTH_ENABLED`/`API_KEYS`/`RATE_LIMIT_*`/`LOG_JSON`/`MAX_CONCURRENT_RUNS` 等**全部缺席**；`MAX_CONCURRENT_RUNS` 只在 `api/server.py:233`、`LOG_JSON` 只在 `utils/logger.py:154` 直读环境变量，不在 `_ENV_MAP`，与「三通道」承诺矛盾 | 照样例配置后认证/限流/日志/并发上限仍靠默认；`config export` 快照不含这些值，回滚比对失真 | 两变量纳入 `Settings`；`.env.example` 补齐生产必配（脱敏占位） |
| D-6 | `OPERATIONS.md:551-571` 只有升级 5 步、全文无 `rollback` 命令；`:384-387` 恢复顺序纯文字无命令；`:556-557/603/209-229` 全为 Linux/POSIX，而项目主排障在 Windows（`scripts/launch-studio.bat`、`ci-check.ps1`） | 升级失败无法按手册回退；Windows 部署方无法执行备份/升级 | §6 增回滚段（版本回退 + 快照回灌 + output 恢复具体命令）；补 PowerShell 等价命令与可复制脚本 |
| D-7 | `OPERATIONS.md:502-514` 表头即「**建议的**告警规则」，仓库无 exporter / 告警配置 / metrics；`:87-88` 日志只有重定向无 logrotate；`:389-401` 只有手动 `clean`；systemd 示例无探针，而 `/health` 启用认证后标准 HTTP 探针会 401 | P1 故障靠人工发现；日志与 output_dir 无限增长；编排器认为健康但探活 401 | 提供 logrotate 配置、`clean` systemd timer、带 key 的 `ExecStartPre` 探针、告警规则样例，并在文档标注「未接线」 |
| D-8 | `ci.yml:17-87` 只有 Python job，无 studio/npm；`studio/package.json:8-17` 定义 7 个检查脚本 CI 一个不跑；`studio/.gitignore:2` 忽略 `package-lock.json`（文件实际存在，且 `resolved` 指向 npmmirror）；`scripts/launch-studio.bat:34` 用 `npm install` | 前端回归无门禁；clone 后依赖树漂移；非中国网络拉不到 npmmirror | 移除 lockfile 忽略；改 `npm ci`；CI 增 `npm ci && npm run check && npm run test:unit` |
| D-9 | `tests/test_api_contract.py:168` 唯一断言是 `assert not _is_sentinel(...)`（只要不是 404 就过）；`:42-50` 只读 `(method, path)`，openapi 正文从未被消费；fixture `:121-129` 是**无 auth / 无限流**的 `ApiServer` | 契约正文可静默失真；认证/限流回归不被守卫拦住 | 断言具体状态码与响应形状；带 key / 不带 key / 超限矩阵；文件守卫校验 security 与 401/429/413 |
| D-10 | `TEST-REPORT.md:13-14` 称「2549 用例 / 122 文件」实际 146 个；`:38/70` 称「21 条路由」实际 32 条；`:99-101` 称「不依赖 pytest-cov、补接线约 0.5 人日」，但 `pyproject.toml:21/45-58` 与 `scripts/test.py:299-345/379-382` **已完整实现** `--coverage`；`ci.yml:53/85` 均未启用，`--cov-fail-under` 默认 0 | 覆盖率能力已存在却无门槛，报告反称需补接线，误导质量判断 | CI 加 `--coverage --cov-fail-under=<基线>`；更新报告数字与表述 |
| D-12 | `scripts/ci-check.ps1:1-2/20-22/39` 自称「一键复现 CI」但用 pip + 裸 `pytest -q`，而 `ci.yml:59-63` 明确注释「不用裸 pytest -q」（随机崩溃）、`:64-87` 走 uv + `uv lock --check` + `scripts/test.py`；`OPERATIONS.md:589-591` 沿用错误描述 | 本地按手册跑会撞 `OPERATIONS.md:693-694` 的 access violation，且不校验 lock 同步 | 脚本改调 `python scripts/test.py` 并加 `uv lock --check`；更正手册 |
| D-13 | 根目录无任何 `agentcrew.yaml` 样例；`config.py:306-317` 读取该文件，而 `OPERATIONS.md:410-428` 要求运维手写 `tool_guards` YAML、`:344` guardrails、`:159-161` RBAC 段；无 `production` profile 或 `serve --strict-production` | 新部署无结构参考；漏配限流/日志 JSON 时服务照常启动且不告警 | 新增 `agentcrew.yaml.example`（各段注释 + 安全默认值）；`serve --strict-production` 检查限流/日志/非回环 host/key 权限，缺一退 3 |
| D-14 | `error-codes.md:178` 与 `openapi.yaml:13-15` 均承认时间戳为 **naive 本地时间**；`OPERATIONS.md:358` 的 run_id 基于本地时钟，`:404-406` 心跳 15min/24h 判据依赖本机时钟 | 跨时区、夏令时、多实例汇总时排序/幂等/心跳过期/事件关联错位（与 A-7 的时钟前跳叠加） | 落盘统一 `datetime.now(timezone.utc).isoformat()`（读取兼容旧格式）；手册增加「主机统一 UTC」 |
| D-15 | `OPERATIONS.md:47` 自认「没有 Dockerfile / Makefile / entrypoint / --daemon」；`ci.yml` 无镜像构建、`pip-audit`、SBOM、部署 job | 部署全靠手抄 systemd 片段；依赖 CVE 无发布前检查点 | 至少增 Dockerfile + `pip-audit`/SBOM CI job |
| D-16 | 负向用例盲区：全 `tests/` 无 SIGTERM/signal 用例；`test_api_contract.py:33-37/149-151` 32 条探测全用空 body + 不存在资源；G-14 承认「多实例共享 output_dir 无运行时检测」却无测试；`TEST-REPORT.md:104-105` 自认单进程多 Crew 容量未验证 | 中断恢复与多实例边界靠手工 | 补 SIGTERM 停机、并发 `POST /runs` 触顶 429、双进程写同一 ledger 检测/拒绝用例 |
| A-2 | `main.py:877` → `:551-597` → `runtime/approvals.py:131-138` | `_cmd_status` 自称「打印」/「只读入口」，但 `expire_overdue()` 会追加 `approvals.jsonl` 并 emit ledger（两次写盘 + flush）；巡检脚本并发调用会对同一 JSONL 并发追加 | 给 `_expire_overdue_approvals` 加 `dry_run=True`，`status` 只统计打印；真正的 expire 只留 `serve` 启动与 `approve` 流程 |
| A-3 | `runtime/checkpoint.py:52/341-364`、`crew/crew.py:1418-1421` | `save_task_delta` / `apply_deltas()` / `load_with_deltas()` 只被测试与 `cli/diff.py` 调用，生产代码零调用；`load_board()` 每次读一个**永不产生**的 `tasks.delta.jsonl` → 宣称的「增量 checkpoint + 每 50 行 compaction」不存在，长 run 仍全量重写 | 在 `_on_change` 按策略调用 `save_task_delta`，或删除机制并修正 docstring 与 `load_board()` |
| A-10 | `runtime/checkpoint.py:413-431` | 「坏行跳过」只捕获 `JSONDecodeError`；合法 JSON 但结构不完整的行（崩溃截断/旧版本）抛 `KeyError`/`TypeError` 穿透 `load_board()` → 整个 resume 失败 | `op` 分支整段包 `except (KeyError, TypeError, AttributeError): continue` |

---

## 4. 已登记缺口对照（本次不重复，但需修正其表述）

`docs/THREAT_MODEL.md` §6（L236-252）的 15 条：G-1 提示词注入无法根除；G-2 配置即 RCE；G-3 无限流默认值/无按 key 配额；G-4 无 lock 文件/SBOM/依赖哈希（**表述需改为「pip 路径无锁」**）；G-5 output_dir OS 权限由部署方决定；G-6 web_fetch DNS rebinding TOCTOU；G-7 跨 run 知识库有意共享；G-8 guardrails 是可绕过启发式；G-9 run_python 无隔离；G-10 OTel 内容采集外发 prompt；G-11 审计无签名/WORM；G-12 无 per-run 磁盘配额与自动清理；G-13 无 TLS、无内置用户体系；G-14 多实例共享 output_dir 无运行时检测；G-15 MCP 传输不含 TLS、未实现 GET 主动通道与会话 DELETE。

`README.md:2464-2495` 的 8 条 V1/V2 已知限制：不执行生成代码、token 估算启发式、千帆 v2 限制、单进程、mock 产物非模型生成、HTTP API 为内部控制面（**默认值已过期**）、MCP「仅 stdio」（**已过期**）、Web UI「无闸门/无预览/无详情」（**已过期**）。

`docs/TEST-REPORT.md` §5 的四条边界：无行覆盖率数字（**表述已失真，见 D-10**）、mock 与真实模型行为差、性能与容量无长期压测、平台差异（称 Windows 未进 CI，**实际已有 Windows job**）。

> 结论：这 27 条「已登记」内容里有 **6 处已经与代码不符**。修文档不是文档洁癖，而是本次审查里成本最低、收益最高的 P0 项之一。

---

## 5. 落地路线图与验收

### 批次 1（P0，建议 1–2 天，上线阻断）

1. 出站 URL 收口（P0-1）+ Webhook 参数上界（B-7）
2. `events` 端点归属校验 + `_event` 白名单 + SSE 帧转义（P0-2 / P0-3）
3. 错误响应统一脱敏（P0-5）+ XFF 可信代理（P0-4）
4. 预算事前检查（P0-6）
5. Studio `baseUrl` 收口（P0-8）+ 供应商 IPC URL 校验（C-2/C-17）
6. README 已知限制与认证章节改写（P0-7）

**验收**：
```bash
python scripts/test.py --coverage --cov-fail-under=<当前基线>
python main.py doctor --json
python main.py serve --api-key <test-key>   # 另起终端做下列探测
```
- 断言 `POST /runs` 带内网 webhook_url 被拒（400），且无出站连接
- 断言跨 run `POST /runs/{other}/events` 返回 403/404，注入型 `_event` 不产生额外 SSE 帧
- 断言 500 响应体不含路径/`Traceback`/key 值
- 断言 `max_llm_calls=1` + 并发 8 只发出 1 次 provider 请求
- 断言 200 次无 XFF / 随机 XFF 请求触发 429

### 批次 2（P1，1–2 周）

1. 先补 `tests/test_resume_consistency.py`（两进程 + 缺心跳 + 写盘注入 OSError），再按 A-4/A-6/A-7/A-20 修一致性
2. 产物与 MANIFEST 事务化（A-11/A-12）、计数落盘顺序（A-18/A-19）
3. 门禁/Provider 超时链（A-5/A-8/A-9/A-14/A-15/A-17/A-21/A-22/A-23）
4. MCP 传输上限与端点同源（B-10~B-13）、ledger 流式分页（B-8）、审批并发锁（B-18）、`expires_at`（B-17）
5. 契约与错误码统一（B-15/B-19/D-11）

**验收**：所有 resume 场景能从任意中断点恢复且 `rework_count` / `attempts` 不回退；「产物文件存在 ↔ MANIFEST 有记录」在硬杀后恒成立；单次 600s 挂起 provider 能被 `llm_request_timeout_seconds` 收住且不被误判停滞。

### 批次 3（P2，中期）

1. Studio：先清「假功能」（要么接后端要么置灰），再修 C-1/C-4/C-5/C-6/C-13/C-14 这批确定性崩溃
2. SSE 重连与去重（C-18）、刷新代次（C-19）、窗口守卫与设置白名单（C-22）
3. 性能：`spawnSync` 异步化、局部重绘、拖拽节流
4. 工程化：SIGTERM（D-1）、openapi security（D-2）、依赖清单与 CI（D-3/D-8/D-9/D-10/D-12）、样例与严格生产模式（D-4/D-13）、UTC（D-14）、Docker/审计（D-15）、负向用例（D-16）
5. 增量 checkpoint 死代码（A-3）、只读 status 写盘（A-2）、delta 坏行（A-10）

**验收**：`npm ci && npm run check && npm run test:unit && npm run e2e` 全绿；`studio/README.md` 的页面数、快捷键表与 `index.html` 一致；`systemctl stop` 后最后一次 ledger 完整落盘。

---

## 6. 风险提示、dev/prod 差异与常见坑

**风险提示**
- 本报告基于静态审查，**未执行任何修改**；行号为审查时定位，重构后需重新定位。标注为「潜在风险」的条目依赖特定触发窗口，建议先写复现用例再动手，避免为不存在的问题引入回归。
- P0-1 / P0-2 / P0-8 属于「信任边界假设被新通道打破」的同一类问题，修复时务必**统一收口**，不要只堵一个入口：出站 URL、渲染端可控参数、重定向链三处都要覆盖。
- 修 A-4（多文件原子性）会改变 checkpoint 写入时序，务必与 A-6/A-20 一起改、一起测；单独改 `_finalized` 置位而不排空 workers，竞态仍然存在。
- 修 D-14（时间戳改 UTC）必须与读取侧兼容旧格式，否则**已有 5590 个 output 文件**在 resume 时会被判为「未来时间」而误收敛。

**dev 环境**
- 允许 `--mock`、`--auto`、认证关闭但**仅绑回环**；用 `AGENTCREW_MOCK_LATENCY_MS` 观察看板。
- 建议把「文档守卫测试」放进 dev 的快速测试集：README 失效表述、`API_ROUTES ⊆ _ROUTE_SCOPES`、openapi operation 数与服务端路由数相等。

**prod 环境**
- 必配：`AGENTCREW_API_AUTH_ENABLED=true` + 密钥库（不要只依赖 legacy `API_KEYS`）、限流、日志 JSON、非回环绑定时强制 TLS 反代、`CODE_EXEC_ENABLED=false`、`UNTRUSTED_INPUTS=true`、可信代理列表、Webhook 目标白名单。
- 补 `serve --strict-production` 启动校验，缺项退 3（见 D-13）；探针必须带 key 或走独立的无认证 `/health`（当前启用认证后标准探针会 401，见 D-7）。
- 出站 Webhook 与 MCP 服务器建议放到独立网络策略/egress 白名单里，不要依赖应用层单点校验。

**常见坑**
1. Windows 下 `os.replace` 遇到文件被占用会抛 `PermissionError`，是 A-4/A-11 最常见的真实触发源；批处理务必在 Windows 与 Linux 各跑一遍。
2. `X-Forwarded-For` 改默认值会让已有反代部署的限流分桶变化，需同步给运维手册与 `test_api_auth.py` 的期望值。
3. `expires_at` 归一化为 naive UTC 后，历史带 `+08:00` 的记录要能被 `except (ValueError, TypeError)` 兜住，不能让老数据把全站认证打挂。
4. 把 `webhook_url` 从 `POST /runs` options 移除属于**接口行为变更**，需同步 `openapi.yaml`、SDK 与 Studio 的「新建运行」表单，避免客户端静默丢参数。
5. Studio 的 `safeStorage` 在不同 OS / 用户账户下解密失败会返回空 key，测试「测试连接」时不要把它当成凭据已配置的证据。

---

## 附录：问题索引（按文件）

| 文件 | 条目 |
|---|---|
| `agentcrew/api/http.py` | P0-3、P0-4、B-8、B-15、B-16 |
| `agentcrew/api/server.py` | P0-2、P0-5、B-9、B-17、B-19、B-20、B-21、D-4 |
| `agentcrew/api/run_options.py` / `config.py` | P0-1、B-7、A-22、D-4 |
| `agentcrew/api/auth.py` / `rbac.py` | B-6、B-17、B-21、P0-7 |
| `agentcrew/runtime/webhook.py` | P0-1、B-7 |
| `agentcrew/runtime/approvals.py` / `ledger.py` / `lifecycle.py` / `checkpoint.py` | A-2、A-3、A-7、A-10、A-23、B-18、B-22、D-14 |
| `agentcrew/crew/crew.py` / `scheduler.py` | A-4、A-6、A-8、A-9、A-14、A-20、A-21、P0-6 |
| `agentcrew/gate/` / `artifacts/` / `utils/files.py` | A-5、A-11、A-12、A-15、A-16、A-17、A-18、A-19 |
| `agentcrew/models/factory.py` / `providers/` | P0-6、A-9、A-23 |
| `agentcrew/mcp/transports.py` / `client.py` | B-10、B-11、B-12、B-13 |
| `sdk/agentcrew_sdk/client.py` | B-20、P0-5 |
| `studio/main.js` / `backend-api.js` / `preload.js` | P0-8、C-1、C-2、C-6、C-17、C-20、C-21、C-22 |
| `studio/src/**` | C-3~C-19（详见 §3.1–3.2） |
| `README.md` / `studio/README.md` / `docs/*.md` / `artifacts/contracts/*` | P0-7、D-2、D-6、D-7、D-10、D-11、D-12、D-13 |
| `pyproject.toml` / `requirements.txt` / `ci.yml` / `scripts/*` | D-3、D-8、D-9、D-10、D-12、D-15 |
