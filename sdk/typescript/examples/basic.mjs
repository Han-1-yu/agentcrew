/**
 * 最小闭环：派活 → 等完 → 取产物。
 *
 *   AGENTCREW_URL=http://127.0.0.1:8765 AGENTCREW_API_KEY=... \
 *     node sdk/typescript/examples/basic.mjs
 *
 * 服务端可以用 mock 模式跑（不花钱）：
 *   python main.py serve --port 8765
 *   AGENTCREW_MOCK=true python main.py run "写一个 slugify 函数"
 */
import { AgentCrewClient } from "../src/index.js";

const client = new AgentCrewClient({
  baseUrl: process.env.AGENTCREW_URL || "http://127.0.0.1:8765",
  apiKey: process.env.AGENTCREW_API_KEY || "",
});

const run = await client.createRun(
  process.argv[2] || "写一个 slugify 函数并配测试",
  { mock: true },
);
console.log("run:", run.run_id, run.status);

const done = await client.waitForRun(run.run_id, {
  timeoutMs: 120_000,
  intervalMs: 1_000,
});
console.log("status:", done.status, done.timedOut ? "(timed out)" : "");

const { artifacts, count } = await client.listArtifacts(run.run_id);
console.log("artifacts:", count);
for (const item of artifacts.slice(0, 3)) {
  const file = await client.readArtifact(run.run_id, item.path, { maxBytes: 400 });
  console.log(`- ${file.path} (${item.kind}, ${item.size_bytes}B)`);
  console.log(file.content.split("\n").slice(0, 3).join("\n"));
}
