# AgentCrew Node / TypeScript SDK

给外部 Node 程序调用 AgentCrew HTTP API 用的**零依赖薄封装**。契约来源是仓库里的
[`artifacts/contracts/openapi.yaml`](../../artifacts/contracts/openapi.yaml)（以
`agentcrew/api/server.py` 为准）。

- **零运行时依赖**：只用全局 `fetch`（Node 18+）与标准 `URL` / `AbortController`；
  `package.json` 没有任何 `dependencies`，也**不需要构建**——`src/index.js` 是可直接
  运行的 ESM，`src/index.d.ts` 手写提供类型。
- **薄**：每个方法就是一次 REST 调用；不缓存、不重试（`waitForRun` 的轮询除外）。
- **错误可分支**：非 2xx 抛 `AgentCrewError`（`status` / `code` / `body`）；网络错误
  `status = 0`，与「服务端返回 4xx」区分得开。
- 与 [`sdk/`](../) 下的 Python SDK 是并列的两套客户端，各自零依赖、互不引用。

## 安装

```bash
# 从仓库本地安装（推荐：跟服务端一起升级）
npm install ../agentcrew/sdk/typescript

# 或者不安装，直接把 src/index.js 加进项目
```

## 快速上手

```js
import { AgentCrewClient } from "agentcrew-sdk";

const client = new AgentCrewClient({
  baseUrl: "http://127.0.0.1:8765",
  apiKey: process.env.AGENTCREW_API_KEY,   // 没开认证可省略
});

const run = await client.createRun("写一个 slugify 函数", { mock: true });
const done = await client.waitForRun(run.run_id, { timeoutMs: 120_000 });
console.log(done.status);                    // completed

const { artifacts } = await client.listArtifacts(run.run_id);
const prd = await client.readArtifact(run.run_id, artifacts[0].path);
console.log(prd.truncated, prd.content.slice(0, 200));
```

## 方法一览

| 分组 | 方法 | 对应端点 |
|---|---|---|
| 运行 | `health` / `listRuns` / `createRun` / `getStatus` / `getStats` | `GET /health`、`/runs`、`POST /runs`、`/runs/{id}/status`、`/stats` |
| 控制 | `cancelRun` / `resumeRun` / `rewindRun` / `waitForRun` | `POST /cancel`、`/resume`、`/rewind`（`dryRun` → `dry_run`） |
| 事件 | `getLedger` / `iterEvents` | `GET /ledger`、`GET /events`（SSE 逐帧） |
| 审批 | `listApprovals` / `decideApproval` | `GET/POST /approvals`（`request_id` / `decision`） |
| 指引 | `getGuidance` / `addGuidance` / `withdrawGuidance` / `compactGuidance` | `GET/POST /guidance`、`DELETE /guidance/{id}`、`/guidance/compact` |
| 产物 | `listArtifacts` / `readArtifact` | `GET /artifacts`（`?path=` 读单文件，`maxBytes` 截断） |

`waitForRun` 在 `awaiting_approval` 就返回（不是终态，但值得让调用方知道）；
超时返回最后一次状态 + `timedOut: true`，不抛错——与 MCP 的 `wait_for_run` 语义一致。

```js
for await (const { event, data } of client.iterEvents(run.run_id)) {
  if (event === "run_finished") break;
  console.log(event, data);
}
```

## 错误处理

```js
import { AgentCrewError } from "agentcrew-sdk";

try {
  await client.getStatus("不存在");
} catch (error) {
  if (error instanceof AgentCrewError && error.status === 404) {
    // 服务端明确回了 404
  } else if (error.status === 0) {
    // 连不上 / 超时，error.cause 是底层错误
  }
}
```

`AgentCrewError` 的 `code` 取自服务端 body 的 `code`（若有），`body` 是原始响应体。

## 测试

零依赖、无网络外部依赖：用本地 `node:http` 假服务跑。

```bash
cd sdk/typescript
node --test tests/*.test.mjs
```
