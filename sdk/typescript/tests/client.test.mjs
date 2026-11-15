/**
 * C13：Node SDK 的协议级测试——真实本地 HTTP 服务，断言请求形状与错误分型。
 *
 * 不引入测试框架依赖：`node --test` + `node:assert/strict`。
 */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";

import { AgentCrewClient, AgentCrewError, parseSseFrame } from "../src/index.js";

async function startApi(handler) {
  const requests = [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      requests.push({
        method: req.method,
        url: req.url,
        headers: req.headers,
        body: body ? JSON.parse(body) : null,
      });
      handler(req, res, requests.length);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return {
    url: `http://127.0.0.1:${port}`,
    requests,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

function json(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function mkClient(api, extra = {}) {
  return new AgentCrewClient({ baseUrl: api.url, apiKey: "k1", ...extra });
}

test("createRun 发 POST /runs，带 Bearer 与 goal/options", async () => {
  const api = await startApi((req, res) => json(res, 202, { run_id: "R-1", status: "running" }));
  try {
    const data = await mkClient(api).createRun("做点东西", { mock: true });
    assert.equal(data.run_id, "R-1");
    assert.equal(api.requests[0].method, "POST");
    assert.equal(api.requests[0].url, "/runs");
    assert.equal(api.requests[0].headers.authorization, "Bearer k1");
    assert.deepEqual(api.requests[0].body, {
      goal: "做点东西", options: { mock: true },
    });
  } finally {
    await api.close();
  }
});

test("非 2xx 抛 AgentCrewError，带 status 与服务端 error 文案", async () => {
  const api = await startApi((req, res) => json(res, 404, { error: "run not found" }));
  try {
    await assert.rejects(() => mkClient(api).getStatus("x"), (error) => {
      assert.ok(error instanceof AgentCrewError);
      assert.equal(error.status, 404);
      assert.equal(error.message, "run not found");
      return true;
    });
  } finally {
    await api.close();
  }
});

test("连不上时 status=0，与 4xx 区分得开", async () => {
  const client = new AgentCrewClient({ baseUrl: "http://127.0.0.1:9", timeoutMs: 500 });
  await assert.rejects(() => client.health(), (error) => {
    assert.ok(error instanceof AgentCrewError);
    assert.equal(error.status, 0);
    assert.equal(error.code, "network_error");
    return true;
  });
});

test("readArtifact 的 path / max_bytes 进查询串且被编码", async () => {
  const api = await startApi((req, res) => json(res, 200, {
    path: "docs/PRD.md", content: "# PRD", truncated: false, max_bytes: 100,
  }));
  try {
    const file = await mkClient(api).readArtifact("R-1", "docs/PRD.md", { maxBytes: 100 });
    assert.equal(file.path, "docs/PRD.md");
    assert.match(api.requests[0].url, /path=docs%2FPRD\.md/);
    assert.match(api.requests[0].url, /max_bytes=100/);
  } finally {
    await api.close();
  }
});

test("waitForRun 轮询到终态即停", async () => {
  const api = await startApi((req, res, count) => json(
    res, 200, count < 2 ? { status: "running" } : { status: "completed", run_id: "R-1" },
  ));
  try {
    const done = await mkClient(api).waitForRun("R-1", { intervalMs: 10, timeoutMs: 500 });
    assert.equal(done.status, "completed");
    assert.equal(api.requests.length, 2);
  } finally {
    await api.close();
  }
});

test("waitForRun 超时返回 timedOut 而不抛错", async () => {
  const api = await startApi((req, res) => json(res, 200, { status: "running" }));
  try {
    const done = await mkClient(api).waitForRun("R-1", { intervalMs: 5, timeoutMs: 20 });
    assert.equal(done.timedOut, true);
    assert.equal(done.status, "running");
  } finally {
    await api.close();
  }
});

test("awaiting_approval 不是终态，但会让 waitForRun 提前返回", async () => {
  const api = await startApi((req, res) => json(res, 200, { status: "awaiting_approval" }));
  try {
    const done = await mkClient(api).waitForRun("R-1", { timeoutMs: 500 });
    assert.equal(done.status, "awaiting_approval");
    assert.equal(done.timedOut, undefined);
  } finally {
    await api.close();
  }
});

test("iterEvents 解析命名帧并跳过注释帧", async () => {
  const api = await startApi((req, res) => {
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    res.write('event: task_transition\ndata: {"task_id":"T-1"}\n\n');
    res.write(": ping\n\n");
    res.write('event: run_finished\ndata: {"status":"completed"}\n\n');
    res.end();
  });
  try {
    const events = [];
    for await (const item of mkClient(api).iterEvents("R-1")) events.push(item);
    assert.deepEqual(events.map((item) => item.event), ["task_transition", "run_finished"]);
    assert.deepEqual(events[0].data, { task_id: "T-1" });
  } finally {
    await api.close();
  }
});

test("204 空响应返回 null", async () => {
  const api = await startApi((req, res) => { res.writeHead(204); res.end(); });
  try {
    assert.equal(await mkClient(api).cancelRun("R-1"), null);
  } finally {
    await api.close();
  }
});

test("decideApproval 用 request_id / decision 字段", async () => {
  const api = await startApi((req, res) => json(res, 200, { status: "approved" }));
  try {
    await mkClient(api).decideApproval("R-1", "A-1", { action: "approve" });
    assert.deepEqual(api.requests[0].body, {
      request_id: "A-1", decision: { action: "approve" },
    });
  } finally {
    await api.close();
  }
});

test("rewindRun 把 dryRun 翻译成 dry_run", async () => {
  const api = await startApi((req, res) => json(res, 200, {
    run_id: "R-1", target: "T-2", affected: ["T-2"], dry_run: true,
  }));
  try {
    await mkClient(api).rewindRun("R-1", { task: "T-2", dryRun: true });
    assert.deepEqual(api.requests[0].body, {
      task: "T-2", instruction: "", dry_run: true,
    });
  } finally {
    await api.close();
  }
});

test("parseSseFrame 处理注释、多行 data 与坏 JSON", () => {
  assert.deepEqual(parseSseFrame('event: x\ndata: {"a":1}'), {
    event: "x", data: { a: 1 },
  });
  assert.equal(parseSseFrame(": ping"), null);
  assert.deepEqual(parseSseFrame("data: line1\ndata: line2"), {
    event: "message", data: "line1\nline2",
  });
  assert.deepEqual(parseSseFrame("data: not-json"), {
    event: "message", data: "not-json",
  });
});
