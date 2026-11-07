# AgentCrew Python SDK

给外部系统调用 AgentCrew HTTP API 用的**零依赖同步客户端**。契约来源是仓库里的
[`artifacts/contracts/openapi.yaml`](../artifacts/contracts/openapi.yaml)（由
`agentcrew/api/server.py` 逆向得到，端点与返回字段都以代码为准）。

- 只用标准库 `urllib`，**不 import `agentcrew`**：不把服务端的 openai / pydantic /
  rich 依赖拖进调用方环境，服务端与 SDK 可各自升级。
- 支持 API Key 认证、按状态码分型的异常、SSE 实时事件流、ledger 自动翻页、
  等待 run 进入终态的便利方法。
- Python 3.10+。部署与认证配置见 [`docs/OPERATIONS.md`](../docs/OPERATIONS.md) §2.2。

## 安装

```bash
# 从仓库安装（推荐：可编辑模式，跟服务端一起升级）
pip install -e ./sdk

# 或者临时把 sdk/ 加进搜索路径，不安装
PYTHONPATH=sdk python sdk/examples/quickstart.py
```

## 快速上手

```python
from agentcrew_sdk import AgentCrewClient

with AgentCrewClient("http://127.0.0.1:8765",
                     api_key="你的 API Key") as client:
    print(client.health())                          # {"status": "ok"}

    created = client.create_run("做一个 Todo 应用", options={"mock": True})
    run_id = created["run_id"]

    final = client.wait_for_run(run_id, timeout=600)
    print(final["status"], client.get_stats(run_id))
```

只读巡检（不会创建 run）：

```python
for run in client.list_runs():
    print(run["run_id"], run["status"])
page = client.get_ledger(run_id, event="llm_call", limit=100)
for event in client.iter_ledger(run_id, event="llm_call"):
    print(event["event"], event.get("data"))
```

订阅事件流：

```python
for event in client.iter_events(run_id):
    print(event.name, event.data)      # 首帧是 connected
    if event.name == "run_finished":
        break
```

两个可直接运行的示例：

```bash
python sdk/examples/quickstart.py                                # 只读巡检
python sdk/examples/quickstart.py --goal "做一个 Todo 应用" --mock
python sdk/examples/stream_events.py <run_id> --max-events 30
```

`--base-url` / `--api-key` 也可由环境变量 `AGENTCREW_SDK_BASE_URL` /
`AGENTCREW_API_KEY` 提供。

## 方法一览

| 方法 | 端点 | 返回 |
|---|---|---|
| `health()` | `GET /health` | `{"status": "ok"}` |
| `list_runs()` | `GET /runs` | run 摘要列表（无分页） |
| `create_run(goal, options=None)` | `POST /runs` | `{"run_id": ...}` |
| `get_status(run_id)` | `GET /runs/{id}/status` | 内存实时快照 |
| `get_ledger(run_id, event=, limit=, offset=)` | `GET /runs/{id}/ledger` | `{"events": [...], "total": N}` |
| `iter_ledger(run_id, event=, page_size=)` | 同上，自动翻页 | 逐条事件的生成器 |
| `get_stats(run_id)` | `GET /runs/{id}/stats` | 事件分布 / LLM 用量 / 成功率 |
| `get_approvals(run_id)` | `GET /runs/{id}/approvals` | 审批单列表 |
| `decide_approval(run_id, request_id, action, task_ids=, decided_by=)` | `POST /runs/{id}/approvals` | 裁决后的审批单 |
| `get_model(run_id)` | `GET /runs/{id}/model` | 当前模型绑定 |
| `set_model(run_id, alias, role=None)` | `PUT /runs/{id}/model` | 更新后的绑定 |
| `cancel_run(run_id)` | `POST /runs/{id}/cancel` | 202 响应体 |
| `resume_run(run_id)` | `POST /runs/{id}/resume` | 202 响应体 |
| `delete_run(run_id, confirm=True)` | `DELETE /runs/{id}?confirm=1` | 释放字节数 |
| `inject_event(run_id, payload, event=None)` | `POST /runs/{id}/events` | 202 响应体（调试用） |
| `iter_events(run_id, timeout=None)` | `GET /runs/{id}/events` | `Event` 生成器（SSE） |
| `wait_for_run(run_id, poll_interval=, timeout=)` | 轮询 `get_status` | 终态快照 |
| `request(method, path, body=, params=)` | 任意 | 未封装端点的逃生口 |

`create_run` 的 `options` 走服务端白名单（`mock` / `default_model` /
`max_concurrency` / `tools_enabled` …）；`webhook_secret`、`api_keys` 这类运维级
配置**不接受**，见 OPERATIONS §2.2。

审批 `action` 由卡点决定：plan 用 `approve` / `cancel` / `save_exit`；delivery 用
`accept` / `abort` / `rework`（`rework` 需给 `task_ids`）。裁决写盘后由 `resume_run`
消费，可跨进程。

## 异常

```
AgentCrewError
├─ TransportError          连不上 / 超时 / 连接断开
├─ APIError                非 2xx（status_code / payload / retry_after）
│  ├─ AuthenticationError  401
│  ├─ NotFoundError        404
│  ├─ ConflictError        409
│  └─ RateLimitError       429（含 Retry-After）
├─ ResponseError           2xx 但不是合法 JSON
└─ WaitTimeoutError        wait_for_run 超时
```

```python
from agentcrew_sdk import RateLimitError, TransportError

try:
    client.create_run("目标")
except RateLimitError as exc:
    print("稍后重试", exc.retry_after)
except TransportError as exc:
    print("服务不可达", exc)
```

错误响应统一是 `{"error": "<消息>"}`；SDK 不会回显请求里的密钥。

## 刻意不做的事

| 不做 | 原因 |
|---|---|
| 隐式重试 | 服务端已有 Provider 级重试与故障转移；客户端再叠一层会把一次 `POST /runs` 变成可能创建多个 run。429 只把 `Retry-After` 交给调用方 |
| 异步 / 线程池客户端 | 运维脚本与集成脚本以同步为主；需要并发时由调用方自己开线程，每个线程一个 client 实例 |
| 把响应包成强类型模型 | 契约里大量可选字段与自由形状（tasks / stats / ledger data），强类型模型会在服务端加字段时立刻失真。SDK 返回原始 dict |
| 封装 `openapi.yaml` 之外的端点 | `/providers` 目录、Agent 热切换等端点未纳入契约；用 `client.request(method, path)` 调用，等契约稳定后再补语义方法 |
| 管理 run 目录 | SDK 只走 HTTP；产物、ledger 文件、审批单都在服务端磁盘上 |

## 已知限制

- **认证覆盖所有路由**，`/health` 也要带 key（服务端中间件挂在路由分发之前）。
- `get_status` 读的是服务端**内存实时快照**：服务重启后历史 run 会 404，此时改用
  `get_ledger` / `get_stats` / `get_approvals`（这些从盘上读）。
- `wait_for_run` 只把 `completed / failed / cancelled / interrupted` 当终态；
  `awaiting_approval` 是非终态挂起，会一直等到超时——先查 `get_approvals` 并裁决。
- SSE 是长连接；经反向代理时必须关闭缓冲并放长读超时（OPERATIONS §2.2 有示例）。
- `delete_run` 是物理删除、不可恢复；`confirm=False` 会被服务端以 400 拒绝。
- 请求体上限沿用服务端的 4 MiB。
