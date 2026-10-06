# AgentCrew Studio 开发提示词

为已有的 AgentCrew 平台做一个 **Electron 桌面 GUI**。后端**已完全可用，不改它一行** —— 你只调用它的 CLI、读它写的状态文件、把进度可视化。

## 技术栈
Electron + 原生 HTML/CSS/JS。**不要** React/Vue/打包器/原生模块。唯一依赖 `electron`。（Node v24 / npm 11 已验证可用。）

## 调后端
```bash
cd <仓库根>   # 含 main.py
python main.py run --goal "<目标>" --mock --auto --no-live --output-dir <目录>
python main.py resume <run_id> --mock --auto --no-live
python main.py cancel <run_id>
```
- **必须传 `--no-live`**（后端自带终端看板，会与 GUI 刷屏）。
- `--mock` 离线免 Key，开发与演示默认用它；`--auto` 跳过人工闸门。
- 退出码：`0` 成功 / `1` 有任务失败 / `2` 拆解失败 / `3` 配置错误 / `4` 停滞 / `130` 取消。

## 数据（**严格按此，均从真实运行抽取**）

目录：`<output_dir>/<run_id>/` = `run.json` + `tasks.json` + `ledger.jsonl` + `artifacts/`

**`run.json`** — 概览
```
run_id, project_goal, status(running|completed|failed|cancelled|interrupted),
stats{ total_tasks, succeeded, failed, blocked, skipped, llm_calls,
       prompt_tokens, completion_tokens, estimated_cost_usd, started_at, finished_at }
```
> 运行中**周期性重写**，不是每事件更新。实时进度只认 `ledger.jsonl`。

**`tasks.json`** — `{"tasks":[…]}`，每项
```
id, title, description, role, acceptance_criteria[], dependencies[], priority,
brief, status, attempts, rework_count, max_attempts, max_reworks,
deliverables[{path, kind, required, min_bytes, must_contain[], max_bytes}],
result{ summary, artifacts[{path, size_bytes, sha256, produced_by_role}], duration_ms },
reviews[{ round, layer(static|llm|human), verdict(approve|reject), score|null,
          issues[], fix_hints[], reviewer, created_at }],
status_history[{ from_status, to_status, reason, at }]
```

**10 个任务状态**（配色须区分）：
`pending` `ready` `running` `review` `rework` `succeeded` `failed` `blocked` `cancelled` `skipped`

**`ledger.jsonl`** — **实时进度的唯一来源**。每行 `{ts, run_id, event, data}`，**追加写，轮询读新增字节**（记录偏移，别整文件重读）。13 种事件：

| event | data |
|---|---|
| `run_started` | goal |
| `plan_created` | task_count, roles, planner_attempts |
| `plan_rejected` | attempt, errors[] |
| `task_started` / `task_finished` | task_id, role, attempt, (duration_ms) |
| `task_transition` | task_id, from, to, reason |
| `task_notes` | task_id, notes |
| `gate_result` | task_id, round, layer, verdict, score, issues[], reviewer |
| `artifact_written` | task_id, path, size_bytes, sha256 |
| `llm_call` | role, model, prompt_tokens, completion_tokens, estimated_cost_usd, latency_ms, purpose |
| `llm_degraded` | provider, from_level, to_level, reason |
| `error` | where, exc_type, message, task_id |
| `run_finished` | status, exit_code, duration_ms |

**`artifacts/`** = 真实交付物 + `MANIFEST.json`（entries 含 path/kind/size_bytes/produced_by_role）。

## 要做的
运行列表 · 启动新运行 · **实时任务看板**（状态配色+进度+累计成本） · 事件流（可过滤） · 任务详情（含逐轮评审） · 产物浏览与预览 · 结束报告（含失败任务的 issues） · 取消与 resume · 后端异常时给可读提示不白屏。

## 不做
不改后端 · 不自己实现状态机（状态一律读盘） · 不做登录/云同步/自动更新/打包。

## 验收（我逐条实测）
1. `npm install && npm start` 无报错
2. GUI 启动 `--mock` 运行，**看板运行中就持续更新**（不是结束后才刷）
3. 结束统计与 `run.json` 的 `stats` **完全一致**
4. 10 种状态配色全覆盖
5. 任务详情能看到评审记录与状态历史
6. 能预览文本产物
7. 列表能同时显示多个 run
8. 后端报错时有可读提示，不崩
9. 关窗时若有运行中进程，正确提示或终止，不留僵尸

## 交付
代码放 `studio/`（自带 `package.json`）+ `studio/README.md`。

## 现成数据
**`output/realrun7/` 是一次已成功跑完的真实运行**（含完整 run.json / tasks.json / ledger.jsonl / artifacts）—— 直接拿它调 UI，不必每次重跑。另可参考 `README.md` 与 `examples/fixtures/`。
