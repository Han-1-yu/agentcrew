# 时间戳 UTC 迁移方案（D-14）

> 状态：**已执行（2026-11-12）**。三步全部落地，全量 3110 passed / 0 failed。
> 本文保留为**迁移记录与回滚依据**，第 1 节的「待迁移」列现在全部是 UTC。

## 0. 执行结果（回填）

| 项 | 结果 |
|---|---|
| 落盘点 | 10 处 → **0 处**（`tests/test_time_migration_guard.py` 的 `BASELINE` 已设为 0） |
| 时长计算 | 9 处 `(end - start)` / `datetime.now() - start` 全改 `clock.seconds_between()` |
| 工具位置 | 从 `runtime/clock.py` **移到 `utils/clock.py`**——`models` 层禁止依赖 `runtime`，而 provider latency 与任务时长都要算差值 |
| 生产 bug | 迁移暴露出 `Approvals.is_expired` 用 naive now 比 aware `expires_at`（会 TypeError）→ 已改双口径归一 |
| 前端 | `realtime.js` 的快照单调性原来按字符串比较 ISO 串，混存会判反 → 改 `parseTs` 时间戳比较 |
| 未迁移（有意） | `run_id` 目录名仍用本地时间（给人看）；`rbac` 密钥文件时间保持 naive（B-17 已归一化过期比较） |

## 1. 现状

| 位置 | 格式 | 说明 |
|---|---|---|
| 心跳 `heartbeat.json` | **aware UTC**（`+00:00`） | A-7 已迁移 |
| 产物清单 `written_at` | **aware UTC** | D-14 已迁移 |
| `resume.lock` 的 `created_at` | **aware UTC** | A-7 已迁移 |
| `run.json` 的 `created_at` / `updated_at` | **aware UTC** | D-14 已迁移 |
| `RuntimeStats` 的 `started_at` / `finished_at` | **aware UTC** | D-14 已迁移 |
| `TaskResult` 的 `started_at` / `finished_at` | **aware UTC** | D-14 已迁移 |
| `ReviewRecord.created_at` | **aware UTC** | D-14 已迁移 |
| `ApprovalRequest` 的 `created_at` / `decided_at` / `expires_at` | **aware UTC** | D-14 已迁移 |
| `ledger.jsonl` 的事件 `ts` | **aware UTC** | D-14 已迁移 |

读取侧已经兼容两种格式：`agentcrew/runtime/clock.py` 的 `parse_stamp()` 把
naive 串按本机时区补齐，`elapsed_since()` 统一做差值并处理时钟倒挂。**因此
现在混存不会产生错误行为**——A-7 修的是「判定被时钟变化打乱」，与格式无关。

## 2. 为什么不在同一个批次里做完

迁移必须**对称**：只改写入不改比较，`datetime.now() - record.updated_at` 立刻
`TypeError`（aware 与 naive 不能相减）。受影响的比较点：

- `crew/report.py:126` — `end = stats.finished_at or record.updated_at`，随后与 `started_at` 相减
- `runtime/board_view.py:474` — 同上，用于看板耗时列
- `crew/crew.py:2199` — `int((datetime.now() - started).total_seconds() * 1000)`
- `api/server.py:1613` — 直接 `isoformat()` 返回给前端（格式变化影响看板解析）
- 大量测试用 `now=datetime.now() - timedelta(...)` 注入 naive 时间

一次改完的回归面覆盖 6 个模型、10 处赋值、5 处比较、数十个测试。风险高于收益，
所以拆成三步。

## 3. 迁移步骤

### 第 1 步：把比较点全部换成 `elapsed_since`（不改变任何落盘格式）

- `report.py` / `board_view.py` / `crew.py:2199` 的差值计算改用
  `clock.elapsed_since(stamp, now=...)`，`None` 时退化为 0 并记 warning。
- 这一步**不改变行为**，只是为第 2 步铺路。跑一遍全量确认零差异。

### 第 2 步：写入侧统一 `utc_now()`

按下列顺序改，每类改完跑一次相关测试：

1. `runtime/checkpoint.py::create`：`created_at=utc_now()`、`updated_at=utc_now()`
   （`run_id` 仍用本地时间生成，目录名保持用户熟悉的本地时钟语义）
2. `crew/crew.py` 的 4 处 `record.updated_at = datetime.now()` → `utc_now()`
3. `gate/*` 的 3 处 `ReviewRecord(created_at=...)` → `utc_now()`
4. `runtime/approvals.py` 的 `decided_at` → `utc_now()`
5. `runtime/ledger.py` 的事件 `ts` → `utc_now()`
6. `TaskResult` / `RuntimeStats` 的时间字段 → `utc_now()`

### 第 3 步：清理与收紧

- 删除 `tests/test_time_migration_guard.py` 里的 `BASELINE`（改为断言「0 处」）。
- `openapi.yaml` 的时间说明从「naive 本地时间」改为「UTC，带 `+00:00`」。
- `error-codes.md:178` 同步。
- `OPERATIONS.md` 增加「主机统一 UTC / 走 NTP」的部署要求。

## 4. 验证清单

- [x] `python scripts/test.py` 全绿（3110 passed / 0 failed）
- [x] 存量 run（naive 时间）与新 run 混存时 `status` / `resume` 正常——`test_clock_liveness` 的兼容用例覆盖
- [x] 新跑一个 run，`run.json` 里 `updated_at` 为 `...Z`（pydantic 序列化）
- [x] 看板「运行时长」不出现负数或 8 小时偏差（改走 `seconds_between`）
- [x] 时钟回拨时 `reconcile` 仍报 `clock_skew` 且不改状态（`test_resume_consistency` 覆盖）
- [x] `tests/test_time_migration_guard.py` 基线已降为 0

## 5. 回滚

三步各自独立可回滚：第 1 步只改比较逻辑，回滚就是把 `elapsed_since` 换回减法；
第 2 步落盘格式变化，回滚后新数据会带 `+00:00`——`parse_stamp` 能读，所以
**不需要回滚数据**，只要把写入侧改回 `datetime.now()` 即可。
