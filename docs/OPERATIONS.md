# 运维部署 / 排障手册

> 面向**把 agentcrew 部署到机器或常驻服务上**的人。开发者视角的排障（读代码、看
> ledger 定位逻辑问题）见 [HANDOVER.md](HANDOVER.md) §5；完整用法见
> [README.md](../README.md)；每轮能力与设计取舍见
> [PROJECT-STATE.md](../PROJECT-STATE.md)；HTTP 接口契约见
> [openapi.yaml](../artifacts/contracts/openapi.yaml)。
>
> 本文只写运维要负责的事：**怎么起、怎么配、数据在哪、怎么监控、怎么升级、出事怎么查**。
> 文中的命令、端口与变量名均在代码中核对过（2026-11-07），示例里的 systemd / Nginx
> 片段是**部署建议**，仓库不自带这些文件。

---

## 0. 先记住这六条

1. **服务自身是明文 HTTP，没有 TLS、没有内置守护进程、没有用户体系。** 公网暴露必须
   前面放反向代理做 TLS 终止，并显式开启 API Key 认证。见 §2.2、§3.3。
2. **认证默认关闭**（`api_auth_enabled=false`）。不改就是匿名可访问，这是最常见的上线事故。
3. **默认监听 `127.0.0.1:8765`**，不是 8000。要对外就得显式 `--host` + 反代。
4. **数据全在本地磁盘**，没有数据库：`output_dir` 下的 run 目录、跨 run 知识库、
   Webhook 待投队列都是文件。备份与扩容都围绕这个目录做。见 §4。
5. **单进程 asyncio，内存里有一张运行中 run 表**，多个 `serve` 实例共享同一个
   `output_dir` 会互相看不见对方、且 `Ledger.emit` 无锁——不要这样扩容。见 §4.4。
6. **密钥绝不落盘**：`.env` 不进版本库、`config export` 脱敏、run 的
   `config_snapshot` 打码、日志与 spool 都走脱敏词表。见 §3.3。

---

## 1. 部署前置

| 项 | 要求 | 依据 |
|---|---|---|
| Python | **3.10+**；CI 矩阵 3.10 / 3.11 / 3.12 / 3.13，日常验证环境 3.12.10 | `pyproject.toml`、`.github/workflows/ci.yml` |
| 依赖 | `pip install -r requirements.txt` | 清单里含 pytest / pytest-asyncio，安装后即可跑测试 |
| Web 框架 | **没有**。HTTP API 是 `asyncio.start_server` 手写实现，不依赖 fastapi / uvicorn | `agentcrew/api/server.py` |
| Node / Electron | 仅 Studio 桌面端需要，服务器部署不用 | `studio/package.json` |
| 磁盘 | 取决于 run 数与产物大小；`output_dir` 必须可写且留足余量 | `doctor` 的 `output_dir` 检查 |
| 网络 | 出站到所选 Provider 的 HTTPS；出站 Webhook、MCP 子进程按需放行 | `doctor` 的 `provider:*` 检查 |

仓库**没有** Dockerfile、Makefile、entrypoint 脚本，也没有内置 `--daemon` / `--workers`。
`serve` 是一个前台进程，进程管理交给 systemd / supervisor / nssm / 容器编排。

```bash
# 最小安装（在仓库根目录）
python -m venv .venv
# Linux/macOS: .venv/bin/activate    Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 连通性自检：不配任何 Key 也能跑，会如实报告缺什么
python main.py doctor --json
```

`doctor --json` 返回 `{"ok": bool, "checks": [...]}`；检查项有 `python` /
`dependencies` / `api_keys` / `config` / `output_dir` / `provider:<alias>` /
`streaming:<alias>`。**它会真的探测 Provider 连通性**，别放在高频（如每 5 秒）探活里。
退出码：全通过或仅警告 → 0，任一失败 → 3。

---

## 2. 五种运行形态

| 形态 | 命令 | 适用 |
|---|---|---|
| CLI 一次性 run | `python main.py run --goal "…" --auto --no-live` | cron、CI、批处理 |
| 常驻 HTTP API + Web 面板 | `python main.py serve` | 生产服务主形态 |
| Studio 桌面端 | `scripts/launch-studio.bat`（或 `cd studio && npm start`） | 本机 GUI，不上服务器 |
| 作为库调用 | `from agentcrew.crew.crew import Crew` | 二次集成 |
| MCP **客户端** | 配置 `mcp_servers` | 连别人的 MCP 服务器取工具 |

> 方向别搞反：`agentcrew/mcp` 是客户端，**本项目不是 MCP server**，不能被别的 Agent
> 反向调用。要被外部系统驱动，用 HTTP API 或 CLI。
>
> R16 的官方 SDK（`sdk/`）不是第六种运行形态，它只是 `serve` 的客户端：服务仍按
> 上表五种之一部署，SDK 装在调用方那一侧。

### 2.1 CLI 一次性（cron / CI）

```bash
# 无人值守：--auto 关人工卡点，--no-live 关实时看板（CI 友好）
python main.py run --goal "交付目标" --auto --no-live \
  > /var/log/agentcrew/run.log 2>&1

# 完全离线演练（不联网、不需要 Key）
python main.py run --sample todo --no-live --auto
```

生产跑真模型时不要带 `--mock` / `AGENTCREW_MOCK=true`——产物会变成 mock 内容且
`resume` 时配置快照不一致。退出码用于告警判定：

| 码 | 含义 | 运维动作 |
|---|---|---|
| `0` | 全部任务 SUCCEEDED | 正常 |
| `1` | 有 FAILED / BLOCKED / SKIPPED | 看 `status <run_id>` 与 ledger 的 `error` / `gate_result` |
| `2` | 计划拆解连续未通过校验 | 多半是提示词 / 模型问题，看 `doctor` 的 Provider 项 |
| `3` | 配置或参数错误（缺 Key、参数非法、resume 快照不一致） | 查配置与 `doctor`，**不要**无脑加 `--force` |
| `4` | 停滞（没有可运行任务但仍有未终结任务） | 看 `diagnostics/stall-report.md` |
| `130` | Ctrl-C 中断 / `cancel` 取消 | 用 `resume <run_id>` 续跑 |

日志走 **stderr**（root logger 的 StreamHandler），所以 `2>&1` 的写法能收到全部输出。
`AGENTCREW_LOG_LEVEL=INFO`（可调），`AGENTCREW_LOG_JSON=1` 切结构化 JSON，便于日志系统检索；
两者都经脱敏词表处理，不会把 Key 打进日志。

### 2.2 常驻 HTTP API（生产主形态）

```bash
# 本机调试（默认 127.0.0.1:8765，匿名可访问）
python main.py serve

# 生产：显式开认证 + 限流，绑内网地址，由反代对外
AGENTCREW_API_AUTH_ENABLED=true \
AGENTCREW_API_KEYS="替换成随机长串A,替换成随机长串B" \
AGENTCREW_API_RATE_LIMIT_ENABLED=true \
AGENTCREW_API_RATE_LIMIT_MAX=100 \
AGENTCREW_API_RATE_LIMIT_WINDOW=60 \
AGENTCREW_LOG_JSON=1 \
python main.py serve --host 127.0.0.1 --port 8765
```

也可以用 `--api-key KEY` 临时加一个 key（与 `AGENTCREW_API_KEYS` 合并，给出即开启认证）。
请求带 `Authorization: Bearer <key>` 或 `X-API-Key: <key>`。

**认证是 fail-closed 的**：`api_auth_enabled=true`（或传了 `--api-key`）却没有任何有效 key，
`serve` 会拒绝启动并打印提示，不会退化成匿名服务。限流三参数**都能配**（R18 补齐）：
`AGENTCREW_API_RATE_LIMIT_ENABLED` / `_MAX` / `_WINDOW` 环境变量、`agentcrew.yaml`
同名顶层标量，以及 `config export/import` 往返。`api_rate_limit_window` 默认 60 秒、
下限 1 秒——非法值在配置加载期就报错；`serve` 启动期仍保留同一条兜底校验，防止
`model_copy` 绕过 pydantic 的路径。

认证中间件挂在**所有**路由之前，`GET /health` 也不例外——启用认证后探活必须带 key：

```bash
curl -H "Authorization: Bearer <key>" http://127.0.0.1:8765/health
# -> {"status": "ok"}
```

### 2.2.1 多用户与 RBAC（R25）

一个共享 key 在多人环境里等于没有权限边界（谁都能删别人的 run）。RBAC 用密钥
文件替代共享 key，**单密钥部署不需要改任何配置**（legacy `api_keys` 等价 admin）。

```bash
# 建密钥：明文只打印这一次，之后服务端只有 scrypt 哈希
python main.py keys add --name alice --role operator --runs R-1,R-2
python main.py keys list
python main.py keys revoke alice        # 立即生效（mtime 失效重读，无需重启）

# 也可从看板「密钥」页操作（admin 身份）；HTTP 等价物：
#   GET /keys · POST /keys · DELETE /keys/{id}
```

| 角色 | 作用域 | 典型用途 |
|---|---|---|
| `viewer` | `runs:read` `tools:read` | 看板只读、看日志 |
| `operator` | + `runs:write` `approvals:write` `tools:test` | 日常跑单、批审批、试工具 |
| `admin` | 全部（含 `runs:admin` `config:read` `keys:*`） | 删 run、改执行体绑定、管密钥 |
| `custom` | 显式 `--scope` 列表 | 特例（如只给 approvals:write） |

三条硬规则：

1. **归属隔离**：API 建的 run 在 run 目录写 `owner.json`；非 admin 只能看
   `--runs` 白名单里或自己创建的 run，别人的 run 一律回 **404**（不泄露存在性）。
   CLI 建的 run 视作 `local`，只有 admin 可见。
2. **审计身份**：审批裁决的 `decided_by` 取密钥名，body 里写什么都覆盖不掉。
3. **越权回 403**（身份有效、作用域不足），与 401（未认证）分开——排障时先看
   状态码就知道是「key 不对」还是「权限不够」。

密钥库在 `<output>/_auth/keys.json`（0600）。它**不在 `clean` 的清理范围**
（clean 只扫 run 目录），但备份 `output/` 时要把 `_auth` 一起带上。

常用端点（完整契约见 `artifacts/contracts/openapi.yaml`）：

| 端点 | 用途 |
|---|---|
| `GET /health` | 存活探活（**启用认证时也要带 key**） |
| `GET /` | 内置 Web 面板 |
| `POST /runs` | 创建并后台启动 run（body: `{"goal": ..., "options": {...}}`） |
| `GET /runs` · `GET /runs/{id}/status` · `GET /runs/{id}/stats` | 列表 / 状态 / 事件与用量统计 |
| `GET /runs/{id}/ledger?event=&limit=&offset=` | 审计事件分页 |
| `GET /runs/{id}/events` | SSE 实时事件流 |
| `POST /runs/{id}/cancel` · `/resume` · `DELETE /runs/{id}?confirm=1` | 生命周期管理 |
| `PUT /runs/{id}/model` · `POST /runs/{id}/agents` | 运行中换模型 / 换执行体 |
| `GET /providers` · `/providers/{name}/models` | Provider 目录与健康状态 |

请求体上限 **4 MiB**（`http.MAX_BODY_BYTES`），超限回 413。`POST /runs` 的 `options`
是白名单，不接受 `webhook_secret`、`api_keys` 这类运维级配置。

**不想手拼请求**：用官方 SDK（`pip install -e ./sdk`，见 `sdk/README.md`）。
`AgentCrewClient` 封装了上表全部端点、认证、按状态码分型的异常、SSE 事件流与
ledger 自动翻页，适合写巡检脚本或把 AgentCrew 接进别的系统。

**进程托管示例**（Linux systemd，按实际路径调整）：

```ini
[Unit]
Description=AgentCrew API
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/agentcrew
Environment=AGENTCREW_CONFIG=/etc/agentcrew/agentcrew.yaml
Environment=AGENTCREW_OUTPUT_DIR=/var/lib/agentcrew/output
Environment=AGENTCREW_LOG_JSON=1
EnvironmentFile=/etc/agentcrew/env          # 放 Provider Key，权限 0600
ExecStart=/opt/agentcrew/.venv/bin/python main.py serve --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=5
User=agentcrew
Group=agentcrew

[Install]
WantedBy=multi-user.target
```

Windows 用 NSSM / 计划任务托管同一条命令；`Ctrl-C` 会走 `server.stop()` 正常收尾，
所以停服优先发 SIGTERM / `systemctl stop`，不要直接 `kill -9`。

**反向代理示例**（TLS 终止 + SSE 要关缓冲、放长读超时）：

```nginx
server {
    listen 443 ssl;
    server_name agentcrew.example.com;
    ssl_certificate     /etc/ssl/agentcrew.crt;
    ssl_certificate_key /etc/ssl/agentcrew.key;

    location / {
        proxy_pass       http://127.0.0.1:8765;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For  $remote_addr;   # 覆盖，别让客户端伪造
        client_max_body_size 4m;           # 与服务端 4 MiB 上限一致
    }
    location ~ /runs/[^/]+/events$ {      # SSE
        proxy_pass         http://127.0.0.1:8765;
        proxy_buffering    off;
        proxy_read_timeout 3600s;
        proxy_http_version 1.1;
        proxy_set_header   Connection "";
        proxy_set_header   X-Forwarded-For $remote_addr;
    }
}
```

服务本身**不提供 TLS、不校验来源 IP、没有多用户与 RBAC**；这些必须由反代 / 网络策略承担。
客户端 IP 取 `X-Forwarded-For` 首地址（限流按它计数），所以反代必须**覆盖**而不是追加
这个头，否则调用方可伪造 IP 绕过限流。开了认证之前不要把 8765 暴露到公网。

### 2.3 Studio 桌面端（仅本机）

```bat
scripts\launch-studio.bat
```

脚本会清掉 `ELECTRON_RUN_AS_NODE`（否则 Electron 会以纯 Node 启动并崩），首次运行自动
`npm install`。开发自检：`cd studio && npm run check`（架构）、`npm run selftest`、
`npm run e2e`。Studio 是 Electron GUI，不适合服务器无人值守部署。

---

## 3. 配置

### 3.1 合并顺序（红线，不要改）

```
代码默认 → .env（python-dotenv）→ agentcrew.yaml → AGENTCREW_* 环境变量
        → CLI 参数 / POST /runs 的 options
```

- 配置文件默认是当前目录的 `agentcrew.yaml`，可用 `AGENTCREW_CONFIG=/path/to.yaml` 改。
- `.env` 与 `.env.*` 已被 `.gitignore` 忽略（只保留 `.env.example`）；生产用
  `EnvironmentFile` 或密钥管理注入，别把 Key 写进仓库。
- `AGENTCREW_OUTPUT_DIR` 会被解析成绝对路径，配置快照里保留生效值。
- 三通道写入的完整字段见 `python main.py config export`。

```bash
# 导出当前合并视图（密钥字段一律脱敏），变更前留档
python main.py config export > /etc/agentcrew/snapshot-$(date +%F).yaml

# 导入：校验后合并写回 agentcrew.yaml，写前自动备份为 agentcrew.yaml.bak
python main.py config import /etc/agentcrew/new.yaml
```

`config export` 只导出可再导入的段（模型、MCP、门禁、工具权限、委派治理、Webhook
地址等），**不导出** `output_dir` / `log_level` 这类运行环境字段，也不导出任何 secret。
所以它适合留档与迁移，但不能当作完整备份——密钥要单独走密钥管理。

### 3.2 生产必配清单

| 变量 / 配置 | 默认 | 生产建议 | 说明 |
|---|---|---|---|
| Provider Key（`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DASHSCOPE_API_KEY` …） | 空 | 必填 | `.env.example` 列了全部支持的 Key；`--mock` 时不需要 |
| `AGENTCREW_DEFAULT_MODEL` | `openai:gpt-4o-mini` | 按实际改 | `<provider>:<model>` 别名，`doctor` 会校验可解析性 |
| `AGENTCREW_OUTPUT_DIR` | `./output` | 独立数据盘绝对路径 | 所有 run 与跨 run 数据都在这里 |
| `AGENTCREW_API_AUTH_ENABLED` | `false` | **`true`** | 不开就是匿名可访问 |
| `AGENTCREW_API_KEYS` | 空 | 随机长串，逗号分隔 | 轮换时新旧 key 可并存一段时间 |
| `AGENTCREW_API_RATE_LIMIT_ENABLED` / `_MAX` / `_WINDOW` | 关 / 100 次 / 60s | 按调用方调 | 按客户端 IP 滑动窗口，超限 429；三者都支持 env / YAML / `config export`（R18） |
| `AGENTCREW_LOG_LEVEL` / `AGENTCREW_LOG_JSON` | `INFO` / 关 | 生产开 JSON | 结构化日志进采集系统，已脱敏 |
| `AGENTCREW_WEBHOOK_URL` 或 `webhooks` | 空 | 按需 | 关键节点外推；R15 起失败会落盘补投 |
| `AGENTCREW_MAX_CONCURRENCY` | 4 | 按 Provider 配额调 | 峰值并发受 DAG 宽度约束 |
| `AGENTCREW_MAX_CONCURRENT_RUNS` | 8 | 按内存与 Provider 配额调 | `serve` 同时运行的 run 数上限，超限 `POST /runs` 回 429 |
| `AGENTCREW_GATE` | `both`（TTY）/ 非 TTY 降级 `none` | 无人值守加 `--auto` | 非 TTY 降级会在 stderr 明说，不会偷偷放行 |

大多数能力开关默认关闭是刻意的（升级不该静默改变成本结构），完整清单见
HANDOVER §4 与 `python main.py config export`。

### 3.3 密钥与网络边界

| 边界 | 现状 |
|---|---|
| 版本库 | `.env` / `.env.*` 被忽略；`output/` 被忽略 |
| 配置导出 | `config export` 剔除 secret；`config import` 写前备份 |
| run 快照 | `output/<run_id>/run.json` 的 `config_snapshot` 把密钥打码 |
| 日志 / 审计 | `secret_values()` 脱敏词表覆盖 stderr 日志、ledger 事件、Web UI 启用信息 |
| Web 启用信息 | `webhook_enabled` 只记打码 URL（丢掉查询串，那里常带 token） |
| R15 spool | `<output_dir>/_webhook_spool/` 只存 URL 短哈希与脱敏 payload，不存 URL / 密钥 |
| HTTP 入口 | `POST /runs` 的 options 不接收 `webhook_secret`；API Key 只在服务端配置 |
| 工具侧 | `web_fetch` 仅公网 80/443 且拒绝私网 / 重定向（防 SSRF）；Webhook **不做**内网黑名单，因为目标是运维显式配置的 |
| MCP | 连接的是外部子进程，命令来自配置；`tools_enabled=false` 时零子进程、零网络 |

---

## 4. 数据与容量

### 4.1 目录布局

```
<output_dir>/
  <run_id>/                     # YYYYMMDD-HHMMSS-<4hex>
    run.json                   # 记录 + config_snapshot（权威状态）
    tasks.json                 # 任务表与状态
    ledger.jsonl               # 全事件审计（逐事件追加）
    heartbeat.json             # R17：活 run 每 30s 刷新；正常收尾即删
    resume.lock                # R17：resume 期间存在，正常/异常收尾即删
    artifacts/  context/  review/  diagnostics/
      stall-report.md          # 停滞诊断
    MANIFEST.json              # 产物清单 + sha256
  knowledge/                   # 跨 run 经验库 / 实体记忆（不属于任何单个 run）
  affinity_state.json  quota_state.json
  _webhook_spool/              # R15：未投递成功的通知，等待下次启动补投
```

`list_runs` / `clean` / `delete_run` **只认含 `run.json` 的目录**，所以 `knowledge/`、
`_webhook_spool/`、以及 `output/` 下的其它辅助文件不会被清理逻辑误删。

### 4.2 备份什么

| 优先级 | 内容 | 说明 |
|---|---|---|
| 必须 | `<output_dir>/` 全部 | run 目录、跨 run 知识库、状态文件、Webhook spool |
| 必须 | `agentcrew.yaml`、`.env`（或密钥管理里的同一份） | 配置与凭据；注意备份本身也要加密与限权 |
| 建议 | 部署目录的 `config export` 快照 | 变更前留档，便于回滚比对 |
| 不需要 | `.venv/`、`__pycache__/`、`studio/node_modules/` | 可重建 |

恢复顺序：先恢复配置与凭据 → 再恢复 `output_dir` → `python main.py doctor --json`
确认 `output_dir` 可写、Provider 可达 → `python main.py status <run_id>` 抽查一个历史
run → 再起服务。**Webhook spool 里的待投记录也会随备份恢复**，起服后会自动补投，
接收方需按 `X-AgentCrew-Delivery` 幂等。

### 4.3 清理

```bash
# 先干跑看会删什么
python main.py clean --keep 20 --protect-running --dry-run
# 确认后真删
python main.py clean --keep 20 --protect-running
```

- `--protect-running` 跳过**有心跳的活 run**（僵尸 RUNNING 仍可清），生产环境务必显式加上。
- 删除范围被限制在 `--output_dir` 之下，越界会整次拒绝。
- `python main.py delete <run_id>` 删单个 run，不可恢复；`RUNNING` 状态会被拒绝。
- 别用 `rm -rf output/<run_id>/` 手工删：`clean` / `delete_run` 带边界校验与状态保护。

**R17 起，僵尸 `RUNNING` 不用等清理**：CLI `run` / `serve` 启动时会自动把
「`running` 且心跳过期 15min」的孤儿改为 `interrupted`，记 `run_reconciled` 并在
stderr 提示可 `resume`。升级前的老 run 没有 `heartbeat.json`，仍走 `updated_at` + 24h
的旧判据（不会因一次升级被批量改状态）；`awaiting_approval` 挂起态不参与收敛。
`clean --protect-running` 的删除守卫与这套收敛是两回事：前者决定「能不能删」，
后者只修正状态、从不删目录。

### 4.4 容量与扩容

事实约束（不是建议，是现状）：

- `serve` 是**单进程 asyncio**；内存中维护运行中 run 表，服务重启后 `/runs` 的实时状态
  从盘上 `run.json` 重建，但「本进程启动过的 run」的实时视图不共享。
- `Ledger.emit` 每次 open/append/fsync，**没有跨进程锁**；两个进程写同一 `ledger.jsonl`
  会交错。
- R15 的 spool 是跨进程共享目录，多实例可能重复补投（at-least-once，靠 delivery 幂等）。

因此：

| 方案 | 结论 |
|---|---|
| **单实例 `serve` + 前面挂反代**（推荐） | 简单可靠；提高吞吐靠调 `AGENTCREW_MAX_CONCURRENCY` 与 Provider 配额 |
| 多实例共享同一 `output_dir` | **不要**：内存状态不共享、ledger 并发写、审批单与 checkpoint 可能互相踩 |
| 多实例各用独立 `output_dir`（分片） | 可行；代价是每个实例的 `/runs` 列表互不包含，需要上层自己路由与汇总 |
| CLI 批处理并发 | 每次 run 有独立 run 目录，**可以**并发；但别让两个进程 `resume` 同一个 run（目前没有 resume 锁文件） |

磁盘增长主要来自 `artifacts/`、`context/`、`ledger.jsonl` 与 `knowledge/`。给
`output_dir` 单独挂盘并设容量告警；清理用 §4.3 的命令，不要按目录名批量删。

---

## 5. 监控

### 5.1 三类信号，各管一件事

| 信号 | 位置 | 回答的问题 |
|---|---|---|
| 进程 / 结构化日志 | stderr（`AGENTCREW_LOG_JSON=1`） | 服务起没起、有没有异常与脱敏后的错误信息 |
| 审计事件 | `<output_dir>/<run_id>/ledger.jsonl`，或 `GET /runs/{id}/ledger` | 每个 run 到底发生了什么、门禁驳回了什么、钱花在哪 |
| 对外推送 | Webhook（`run_started` / `run_finished` / `approval_requested` / `run_stalled` / `deadlock`，可配 `webhooks` 多接收端与「只推失败」） | 让外部系统不用轮询就知道关键节点 |

巡检命令：

```bash
python main.py doctor --json                 # 部署后 / 变更后（有 Provider 探测，别高频）
curl -H "Authorization: Bearer <key>" http://127.0.0.1:8765/health
python main.py status <run_id>               # 任务表 + 统计 + 待审批条数
python main.py diff <run_a> <run_b> --json   # 两次 run 的配置 / 产物 / 事件差异
curl -H "Authorization: Bearer <key>" \
  "http://127.0.0.1:8765/runs/<run_id>/stats"
```

巡检脚本可以直接用 SDK（省掉 curl 拼头与错误处理）：

```python
from agentcrew_sdk import AgentCrewClient, AgentCrewError

with AgentCrewClient("http://127.0.0.1:8765", api_key="<key>") as client:
    try:
        print(client.health(), len(client.list_runs()))
    except AgentCrewError as exc:
        print("巡检失败：", exc.status_code, exc)
```

### 5.2 建议的告警规则

| 触发 | 级别 | 动作 |
|---|---|---|
| `/health` 连续失败 / 进程退出 | P1 | 查 serve 日志与端口占用；`systemctl status` |
| `doctor --json` 的 `ok=false` | P1 | 按 `checks[].name` 定位：依赖 / Key / 配置 / output_dir / Provider |
| ledger 出现 `error` / `deadlock` / `run_stalled` / `budget_exhausted` | P2 | 看 `diagnostics/stall-report.md` 与 `gate_result.issues` |
| `webhook_failed`（`shutdown_timeout` / 重试耗尽） | P2 | 检查接收端；R15 起记录留盘会补投，但要盯 spool 是否堆积 |
| `webhook_failed(replay_discarded: …)` / `replay_target_missing` | P2 | 通知被淘汰或地址变了，核对 `webhooks` 配置 |
| `api_auth_failed` 突增 | P2 | 疑似凭据泄露或扫描；轮换 `AGENTCREW_API_KEYS` |
| 429 变多 | P3 | 调 `AGENTCREW_API_RATE_LIMIT_MAX` / `_WINDOW`，或排查调用方 |
| `output_dir` 剩余空间 < 20% | P2 | 按 §4.3 清理，避免 ledger 写失败 |
| 退出码 3 / 4 的 CLI run | P2 | 配置/参数错误或停滞，见 §7.3 |

出站 Webhook 的语义是 **at-least-once**：进程退出、收尾超时、接收端故障都会把
未投递记录留在 `_webhook_spool/`，下一次启用 Webhook 的 run 启动补投；24 小时或累计
5 次后丢弃并记 `replay_discarded`。接收端必须按 `X-AgentCrew-Delivery` 幂等。

---

## 6. 升级与变更

```bash
# 1) 停服（让 server.stop() 正常收尾，别 kill -9）
sudo systemctl stop agentcrew

# 2) 留档 + 备份
python main.py config export > /etc/agentcrew/snapshot-$(date +%F).yaml
cp -a /var/lib/agentcrew/output /backup/agentcrew-output-$(date +%F)

# 3) 更新代码与依赖
git pull
pip install -r requirements.txt

# 4) 验证（低风险、不联网）
python main.py doctor --json
python main.py run --sample todo --no-live --auto

# 5) 启服 + 抽查
sudo systemctl start agentcrew
curl -H "Authorization: Bearer <key>" http://127.0.0.1:8765/health
python main.py status <最近一次 run_id>
```

注意事项：

- **配置快照**：每个 run 的 `run.json` 记着创建时的配置快照；升级后 `resume` 会比对，
  不一致时以退出码 3 拒绝，确认无误才加 `--force`。别把 `--force` 写进自动化脚本。
- **落盘结构**：`schema_version=1` 与 10 态状态机是兼容红线，升级不应要求手工改
  `run.json` / `tasks.json`。
- **密钥**：`config export` 的输出是脱敏的，升级后要把密钥从密钥管理重新注入。
- **CI 等价检查**（R18 起本地可完整跑通，三步全部零错误）：

  ```bash
  pip install "ruff==0.17.3" "mypy==2.4.1"   # 与 ci.yml 钉同一版本
  ruff check .
  mypy agentcrew/
  pytest -q                                  # CI 单进程；本机 Windows 需分 5 批
  ```

  `.github/workflows/ci.yml` 在 3.10–3.13 四版本跑同一套检查；Windows 下用
  `scripts/ci-check.ps1`（自动建 `.venv-ci`、装同版本工具）一键复现。本机
  Windows 全量回归需分 5 批，见 HANDOVER §5.1。

---

## 7. 排障

### 7.1 五分钟定位流程

```
服务没起来？
  ├─ 看 serve 的 stderr（端口 / 认证 / 限流 / 配置 / output_dir 可写）
  ├─ python main.py doctor --json        → 定位依赖 / Key / 配置 / Provider
  └─ 端口占用：ss -ltnp | grep 8765（Windows: netstat -ano | findstr 8765）

服务在跑，某个 run 不对？
  ├─ python main.py status <run_id>      → 任务状态与统计
  ├─ 看 <run_id>/ledger.jsonl            → 逐事件：error / gate_result / llm_call
  ├─ python main.py diff <a> <b>         → 与一次正常 run 比配置 / 产物 / 事件
  └─ <run_id>/diagnostics/stall-report.md → 卡住时看它

需要知道「配置到底生效了什么」？
  └─ <run_id>/run.json 的 config_snapshot（密钥已打码）
```

### 7.2 启动期故障

| 症状 | 原因 | 处置 |
|---|---|---|
| `serve 启动失败（…: 地址已在使用）` | 端口被占 | 换 `--port` 或停掉占用进程 |
| `--port 必须是 1–65535` | 参数越界 | 改成合法端口（0 不允许，虽能绑随机端口但打印地址不可用） |
| `已启用 API 认证但未配置任何密钥` | fail-closed 生效 | 配 `AGENTCREW_API_KEYS` / `--api-key`，或明确关掉 `api_auth_enabled` |
| `api_rate_limit_window 必须 >= 1` | 配置里给了 0 / 负数，或程序内 `model_copy` 注入了非法窗口 | 配置加载期通常已先报错；出现这条说明绕过了 pydantic，检查二次集成的 `cli_overrides` |
| `配置错误：…` | YAML / 环境变量值非法 | 用 `config export` 看合并视图，逐项核对 |
| `doctor` 的 `output_dir` 失败 | 目录不存在 / 不可写 / 磁盘满 | 建目录、修权限、清空间；`AGENTCREW_OUTPUT_DIR` 用绝对路径 |
| 401 | 未带 / 带错 key | `Authorization: Bearer <key>` 或 `X-API-Key`；密钥不回显在错误信息里 |
| 429 | 触发按 IP 限流 | 看 `Retry-After`，调限流参数或排查调用方 |
| 413 | 请求体 > 4 MiB | 缩小 body；反代 `client_max_body_size` 与之一致 |
| SSE 一连上就断 / 收不到事件 | 反代缓冲或读超时太短 | `proxy_buffering off` + `proxy_read_timeout 3600s` |
| `/health` 返回 401 | 认证覆盖所有路由（含健康检查） | 探活请求也带上 key，或由反代在本地做探活 |
| Studio 启动报 `Cannot read properties of undefined (app)` | 继承了 `ELECTRON_RUN_AS_NODE` | 用 `scripts/launch-studio.bat`，它会清掉该变量 |

### 7.3 运行期故障

| 症状 | 排查起点 |
|---|---|
| 所有任务都是同一个通用角色在做 | 角色 key 拼错；R6 起会被四道闸拒绝并留 `subtask_rejected_by_policy`，查 ledger |
| 任务反复返工到 FAILED | 对比每轮 `gate_result.issues`：在变 = 有进展（调 `max_reworks`），不变 = 提示词 / 评审标准问题 |
| `awaiting_approval` 一直挂起 | `python main.py approvals <run_id>` 列单 → `approve` 写裁决 → `resume`（可跨进程） |
| resume 报「状态非法」 | `run.json` 的 status 是权威；checkpoint 与 ledger 不一致时先 `doctor` |
| 僵尸 `RUNNING` | R17 起 CLI `run` / `serve` 启动会自动收敛为 `interrupted`（看 ledger 的 `run_reconciled`）；`clean --protect-running` 仍可清僵尸目录，必要时 `cancel <run_id>` |
| `resume` 提示「另一个进程续跑」/ API 409 | 该 run 的 `resume.lock` 被占，或残留自被强杀的进程 | 先确认 `heartbeat.json` 不新鲜且确实没有活进程；等 15 分钟让锁自动过期，或人工删除 `resume.lock` 后重试。**别在活 run 上删锁**——那会让两个进程共写同一 checkpoint |
| 退出码 4 / 卡住 | `diagnostics/stall-report.md`；任务级超时与停滞检测默认 300s |
| Provider 报错 / 变慢 | `doctor --json` 的 `provider:*` 三级健康；故障转移与冷却矩阵（402 / 429 / 5xx）在模型层 |
| Webhook 一条都没收到 | 先看 `webhook_enabled` 的 `targets`；R14 修过「只配 `webhooks` 不生效」的入口 bug |
| 通知重复 / 一直补投 | R15 是 at-least-once：按 `X-AgentCrew-Delivery` 幂等；看 `_webhook_spool/` 残留与 `replay_*` 事件 |
| MCP 工具没出现在提示词里 | `tools_enabled` 必须为 true；看 `mcp_connected` / `mcp_failed` |
| 磁盘写失败 / ledger 截断 | 先看空间与权限；`Ledger.emit` 无锁，别让两个进程写同一 run |

### 7.4 常用修复动作

```bash
python main.py status <run_id>                         # 汇总视图
python main.py approvals <run_id>                      # 列待裁决审批单
python main.py approve <run_id> <request_id> --decision approve --by ops
python main.py resume <run_id>                         # 续跑（配置变了会拒绝，别乱加 --force）
python main.py cancel <run_id>                         # 软取消，下次 resume 直接收尾
python main.py clean --keep 20 --protect-running --dry-run
python main.py diff <run_a> <run_b> --json
```

`delete` / `clean` 是真删，先 `--dry-run`。任何批量删除前按 §4.2 备份。

**单步调试（R20）**：`run --debug-step` 让每个任务派发前都挂起为 `step` 审批单
（任务 / 角色 / 优先级写在审批单上下文里）——`approve --decision step` 只放行
当前断点上的任务，`--decision run` 解除断点一路跑到终局，`--decision abort`
取消 run。断点开关随 run 快照落盘，resume 不重供 `--debug-step` 也继续逐任务停；
`status` / `approvals` / ledger 负责看「任务为什么做成这样」。开关默认关闭，
关闭时执行路径与开启前逐字一致。

**可视化编排（R26）**：看板「编排」标签页拖节点连线，或
`python main.py flow validate <file>` 在 CI / 提交前校验流程 YAML（不合法退出码 2）。
流程存 `<output>/_flows/`，与 run 目录互不影响；`flow list` 列出已保存流程。
画布可「一键起跑」：`python main.py run --flow <名字|文件>`，或 `POST /runs`
带 `options.flow` / `options.flow_name`（见 README R28）。

**单工具试跑（R24）**：`python main.py tools` 列工具，`python main.py tool
<name> --args '{"path":"a.txt"}'` 在 `<output>/_tool_trial/` 沙箱里试跑一个工具；
HTTP 等价物是 `GET /tools` 与 `POST /tools/{name}/test`，看板首页「工具」标签页
也能跑。**需要 `tools_enabled=true`**（未开时试跑回 409/退出码 3，清单仍可看）；
试跑只写自己的沙箱，不碰 run 目录与仓库文件。

### 7.5 已知环境问题（Windows + Git Bash）

| 现象 | 处理 |
|---|---|
| 全量 pytest 偶发 `access violation` / `SystemError: bad format string` | 本机 pytest 在大量用例下的内存问题，**不是回归**；按 HANDOVER §5.1 分 5 批跑（3.12.10 + `--assert=plain`） |
| `SandboxError: center 规则快照不可用` | 环境问题，命令实际会执行（输出标注 bypassed），重试即可 |
| `api/server.py` 写入偶发被独占锁 | 用「写临时文件 → `os.replace`」原子替换 |
| 中文输出乱码 | CLI 入口已包编码安全流；重定向到文件时用 UTF-8 读取 |

---

## 8. 上线前检查清单

- [ ] `python main.py doctor --json` 的 `ok=true`，`provider:*` 为 healthy
- [ ] `AGENTCREW_API_AUTH_ENABLED=true` 且已下发随机长 key；`/health` 带 key 返回 200
- [ ] 反代已配 TLS、`client_max_body_size 4m`、SSE 关闭缓冲并放长读超时
- [ ] `8765` 只监听内网地址，公网入口只有反代
- [ ] `AGENTCREW_OUTPUT_DIR` 指向独立数据盘，权限仅服务账号可写，已设容量告警
- [ ] `AGENTCREW_LOG_JSON=1`，stderr 已接入日志采集；确认日志脱敏生效
- [ ] `.env` / `agentcrew.yaml` 不在版本库；密钥走密钥管理，备份加密限权
- [ ] 跑过一次 `python main.py run --sample todo --no-live --auto`（离线冒烟）
- [ ] 出站 Webhook 已配且接收端按 `X-AgentCrew-Delivery` 幂等；知道 `_webhook_spool/` 的位置
- [ ] 备份与恢复演练过一次（含配置导出快照），`clean --protect-running` 干跑过
- [ ] 停服用 SIGTERM / `systemctl stop`，不是 `kill -9`
