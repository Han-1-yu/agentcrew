# 能力补齐第二批：MCP server / 浏览器接入 / 事件触发（C7 · C6 · C8）

> 立项依据：`完善/AgentCrew落地完善路线-2026-11-12.md` §5 第二批（P1）。
> 本批交付：C7（暴露为 MCP server，杠杆最大）、C6 第一步（借现成浏览器 MCP，
> 本仓零开发）、C8（入站 Webhook 触发；定时触发此前已由 `main.py schedule` 交付）。
> C4 多模态 / C5 文档 RAG 仍在第二批清单中，需更大改动面，见文末「未完成」。

---

## 1. C7 · 暴露为 MCP server（`main.py mcp-serve`）

任何 MCP 客户端（Claude Desktop / Cursor / 任何 stdio MCP host）都能把 AgentCrew
当一个「同事」调用：给活、看进度、裁决审批。

### 1.1 工具面（5 个）

| 工具 | 对应 API | 说明 |
| --- | --- | --- |
| `agentcrew_create_run` | `POST /runs` | 派一个目标，返回 run_id |
| `agentcrew_run_status` | `GET /runs/{id}/status` | 查状态 |
| `agentcrew_list_approvals` | `GET /runs/{id}/approvals` | 列审批单 |
| `agentcrew_approve` | `POST /runs/{id}/approvals` | 裁决（decision 形状同 HTTP API） |
| `agentcrew_wait_for_run` | 轮询 status | 等到终态或超时 |

### 1.2 设计边界（关键）

- **复用现有 API 层**：MCP server 自身不碰 Crew / 审批 / 产物——每个工具调用
  翻译成一次对本机 HTTP API 的请求。RBAC、限流、审计全部由 API 侧执行：
  MCP 客户端拿到的 API Key 是什么角色，就只能做什么；越权被拒并留痕
  （`security_events`），不需要在这里再造一遍权限语义。
- **传输**：stdio（换行分隔 JSON-RPC，与自家 `StdioTransport` 同帧格式——
  我们的客户端可以连自己）。streamable HTTP 传输暂不做：等有真实远程调用方。
- **协议面刻意窄**：`initialize` / `tools/list` / `tools/call` / `ping` 四方法
  + `notifications/initialized`。不做 resources / prompts / sampling。

### 1.3 用法

```bash
# 1) 起 AgentCrew API
python main.py serve --port 8765

# 2) 起 MCP server（stdio），指给客户端
python main.py mcp-serve --api-url http://127.0.0.1:8765 \
    --api-key ac_xxx        # 或环境变量 AGENTCREW_MCP_API_KEY / AGENTCREW_API_KEY
```

Claude Desktop 配置示例（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "agentcrew": {
      "command": "python",
      "args": ["E:/agentcrew/main.py", "mcp-serve",
               "--api-url", "http://127.0.0.1:8765"],
      "env": {"AGENTCREW_MCP_API_KEY": "ac_xxx"}
    }
  }
}
```

---

## 2. C6 第一步 · 浏览器操作（先借后建，本仓零开发）

不内置浏览器：接现成的浏览器 MCP server。模板见
`examples/agentcrew.browser.example.yaml`（Playwright MCP 为主，Chrome DevTools
备用），要点：

- 工具以 `playwright__browser_navigate` 这样的 `server__tool` 前缀登记（R13 语义）；
- **默认只给「读」类浏览器工具**（navigate / snapshot / console），点击 / 填表
  等「写」类工具需要按角色显式放开，并建议配合人工卡点；
- `tool_permissions` 白名单照常生效；`untrusted_inputs: true` 会跳过**全部**
  MCP 工具——「只读预设」与「自动操作网页」互斥，只能选一个；
- 浏览器 MCP 是外部子进程（以你的用户权限运行），不受 AgentCrew 沙箱约束，
  只在信任环境使用。

内置 playwright 工具组（`agentcrew[browser]`）仍按路线留待按需立项。

---

## 3. C8 · 事件触发 / 定时运行

**定时运行**此前已交付（`main.py schedule add/list/remove/tick`，日程持久化在
`<output_dir>/_schedules/`，`tick` 幂等、由 cron / Studio 定时器驱动）。本批补齐
**入站 Webhook 触发**：

### 3.1 端点与认证

```
POST /webhooks/trigger
X-AgentCrew-Timestamp: <unix 秒>
X-AgentCrew-Signature: sha256=<hex>      # HMAC-SHA256(secret, "<ts>.<body>")
body: {"goal": "...", "options": {...}}   # 同 POST /runs
```

两条认证路径：

1. **HMAC 签名**（给不持 API Key 的外部系统）：签名语义复用 R12 出站实现，
   时间戳偏离 **300 秒**拒绝（防重放）；签名失败 / 时间戳异常回 401 并记
   `webhook_signature_invalid` 安全事件；
2. **API Key**（与全站一致）：不带签名头时走普通认证链——无凭证 401，
   不给探测者任何「端点存在但没开」的额外信息。

### 3.2 配置与审计

- `webhook_trigger_secret`（env `AGENTCREW_WEBHOOK_TRIGGER_SECRET`，默认空 =
  **关闭**，端点回 503）；不进配置快照、参与日志脱敏；
- 触发成功的主体记为 `webhook`（**只有 `runs:write`**：不能看别人的 run、
  不能动审批），run 归属与审计身份照常落盘；
- 来源可审计：run ledger 里落 `run_triggered(source=webhook)` 事件。

### 3.3 用法

```bash
# 发起方（任意语言）：签名后 POST
ts=$(date +%s)
body='{"goal":"夜间回归"}'
sig="sha256=$(printf '%s.%s' "$ts" "$body" | openssl dgst -sha256 -hmac "$SECRET" -hex | cut -d' ' -f2)"
curl -X POST http://127.0.0.1:8765/webhooks/trigger \
  -H "X-AgentCrew-Timestamp: $ts" -H "X-AgentCrew-Signature: $sig" \
  -H "Content-Type: application/json" -d "$body"
```

---

## 4. 已验证 / 未完成

| 项 | 状态 |
| --- | --- |
| C7 MCP server | ✅ 协议层 10 例 + 桥接层 3 例（含对真实 in-process ApiServer 的端到端）；真实 Claude Desktop 客户端互通待有环境时补一次 smoke |
| C6 浏览器模板 | ✅ 模板经配置层加载 + 工具前缀登记 + 适配器转发 4 例；真实浏览器端到端（打开网页→提取→截图）待装 Playwright MCP 后补 |
| C8 入站触发 | ✅ 8 例（正确签名触发 / 坏签名 / 时间戳过期 / 缺时间戳 / 未启用 503 / 认证不变量 / 作用域 / 密钥脱敏） |
| C8 定时运行 | ✅ 此前已交付（`main.py schedule`） |
| C4 多模态 | ⬜ 未开始（providers 图像通道 + Studio 预览，约 3–5 人日） |
| C5 文档 RAG | ⬜ 未开始（摄取管线 + 检索注入，约 3–5 人日） |
