"""Web UI 面板（V2 第 7 项）。

单文件 HTML 仪表盘，零依赖（vanilla JS + SSE），由 ApiServer 在 GET / 提供。
展示运行状态、任务看板（kanban）和实时事件流。
"""

from __future__ import annotations

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentCrew 控制台</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; background: #0f1117; color: #e1e4e8; padding: 16px; }
  header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #30363d; }
  h1 { font-size: 20px; font-weight: 600; }
  .status-badge { padding: 4px 12px; border-radius: 12px; font-size: 13px; font-weight: 600; }
  .status-pending { background: #30363d; color: #8b949e; }
  .status-running { background: #1f6feb33; color: #58a6ff; }
  .status-completed { background: #23863633; color: #3fb950; }
  .status-failed { background: #da363333; color: #f85149; }
  .controls { margin-bottom: 16px; display: flex; gap: 8px; align-items: center; }
  input { background: #161b22; border: 1px solid #30363d; color: #e1e4e8; padding: 6px 12px; border-radius: 6px; font-size: 14px; width: 200px; }
  button { background: #238636; color: #fff; border: none; padding: 6px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; }
  button:hover { background: #2ea043; }
  .board { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-bottom: 16px; }
  .column { background: #161b22; border-radius: 8px; padding: 8px; min-height: 100px; }
  .column h3 { font-size: 12px; text-transform: uppercase; color: #8b949e; margin-bottom: 8px; text-align: center; }
  .task-card { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 6px 8px; margin-bottom: 6px; font-size: 12px; }
  .task-card .role { color: #58a6ff; font-size: 11px; }
  .log { background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 12px; height: 300px; overflow-y: auto; font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; }
  .log-entry { margin-bottom: 4px; opacity: 0.9; }
  .log-entry .ts { color: #8b949e; }
  .log-entry .evt { color: #d2a8ff; font-weight: 600; }
  .connected { color: #3fb950; }
  .disconnected { color: #f85149; }
</style>
</head>
<body>
<header>
  <h1>AgentCrew 控制台</h1>
  <span id="conn-status" class="status-badge status-pending">未连接</span>
</header>
<div class="controls">
  <input id="run-id" placeholder="Run ID（如 R-1）" value="">
  <button onclick="connect()">连接</button>
  <span id="run-status" class="status-badge status-pending">pending</span>
</div>
<div class="board" id="board"></div>
<div class="log" id="log"></div>

<script>
const STATUSES = ["pending", "ready", "running", "review", "rework", "succeeded", "failed", "blocked", "cancelled", "skipped"];
let eventSource = null;
let currentRun = null;

function initBoard() {
  const board = document.getElementById("board");
  board.innerHTML = "";
  STATUSES.forEach(s => {
    const col = document.createElement("div");
    col.className = "column";
    col.id = "col-" + s;
    col.innerHTML = `<h3>${s} <span id="count-${s}">0</span></h3>`;
    board.appendChild(col);
  });
}

function updateTask(tasks) {
  STATUSES.forEach(s => {
    const col = document.getElementById("col-" + s);
    const cards = col.querySelectorAll(".task-card");
    cards.forEach(c => c.remove());
  });
  const counts = {};
  STATUSES.forEach(s => counts[s] = 0);
  Object.entries(tasks || {}).forEach(([id, t]) => {
    const st = t.status || "pending";
    counts[st] = (counts[st] || 0) + 1;
    const col = document.getElementById("col-" + st);
    if (col) {
      const card = document.createElement("div");
      card.className = "task-card";
      card.innerHTML = `<div>${id}</div><div class="role">${t.role || ""}</div>`;
      col.appendChild(card);
    }
  });
  STATUSES.forEach(s => {
    const el = document.getElementById("count-" + s);
    if (el) el.textContent = counts[s] || 0;
  });
}

function logEvent(event, data) {
  const log = document.getElementById("log");
  const entry = document.createElement("div");
  entry.className = "log-entry";
  const ts = new Date().toLocaleTimeString();
  entry.innerHTML = `<span class="ts">${ts}</span> <span class="evt">${event}</span> ${JSON.stringify(data).substring(0, 120)}`;
  log.appendChild(entry);
  log.scrollTop = log.scrollHeight;
}

function setStatus(status) {
  const el = document.getElementById("run-status");
  el.textContent = status;
  el.className = "status-badge status-" + status;
}

function setConn(connected) {
  const el = document.getElementById("conn-status");
  el.textContent = connected ? "SSE 已连接" : "未连接";
  el.className = "status-badge " + (connected ? "status-completed" : "status-pending");
}

async function connect() {
  const runId = document.getElementById("run-id").value.trim();
  if (!runId) { alert("请输入 Run ID"); return; }
  if (eventSource) eventSource.close();
  currentRun = runId;
  initBoard();

  // 获取初始状态
  try {
    const resp = await fetch(`/runs/${runId}/status`);
    if (resp.ok) {
      const data = await resp.json();
      setStatus(data.status || "pending");
      updateTask(data.tasks || {});
    }
  } catch(e) {}

  // SSE 实时更新
  eventSource = new EventSource(`/runs/${runId}/events`);
  eventSource.onopen = () => setConn(true);
  eventSource.onerror = () => setConn(false);
  eventSource.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      logEvent("message", data);
    } catch(err) {}
  };
  eventSource.addEventListener("connected", (e) => {
    logEvent("connected", JSON.parse(e.data));
  });
  eventSource.addEventListener("task_transition", (e) => {
    const data = JSON.parse(e.data);
    logEvent("task_transition", data);
    // 刷新状态
    fetch(`/runs/${runId}/status`).then(r => r.json()).then(d => {
      setStatus(d.status || "pending");
      updateTask(d.tasks || {});
    });
  });
  eventSource.addEventListener("run_completed", (e) => {
    setStatus("completed");
    logEvent("run_completed", JSON.parse(e.data));
  });
  eventSource.addEventListener("run_failed", (e) => {
    setStatus("failed");
    logEvent("run_failed", JSON.parse(e.data));
  });
}

initBoard();
</script>
</body>
</html>
"""
