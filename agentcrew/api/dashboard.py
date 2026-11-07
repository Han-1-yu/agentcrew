"""Web UI 面板（V2 第 7 项；P2-10 起从只读看板升级为可操作）。

单文件 HTML 仪表盘，零依赖（vanilla JS + SSE），由 ApiServer 在 GET / 提供。
P2-10 新增：run 列表（打开 / 取消 / 删除）、「事件日志」标签页（Ledger 查询）、
「统计」标签页（LLM 用量 / 任务成功率）；原有看板、实时事件流、模型切换保持不变。
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
  h2 { font-size: 15px; font-weight: 600; }
  .status-badge { padding: 4px 12px; border-radius: 12px; font-size: 13px; font-weight: 600; display: inline-block; }
  .status-pending, .status-ready, .status-cancelled, .status-interrupted,
  .status-review, .status-rework, .status-blocked, .status-succeeded, .status-skipped { background: #30363d; color: #8b949e; }
  .status-running { background: #1f6feb33; color: #58a6ff; }
  .status-completed { background: #23863633; color: #3fb950; }
  .status-failed { background: #da363333; color: #f85149; }
  .controls { margin-bottom: 16px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  input { background: #161b22; border: 1px solid #30363d; color: #e1e4e8; padding: 6px 12px; border-radius: 6px; font-size: 14px; width: 200px; }
  button { background: #238636; color: #fff; border: none; padding: 6px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; }
  button:hover { background: #2ea043; }
  button:disabled { background: #30363d; color: #6e7681; cursor: not-allowed; }
  button.small { padding: 4px 10px; font-size: 12px; }
  .btn-danger { background: #da3633; }
  .btn-danger:hover { background: #f85149; }
  .btn-danger:disabled { background: #30363d; color: #6e7681; }
  .btn-cancel { background: #9e6a03; }
  .btn-cancel:hover { background: #d29922; }
  .run-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; margin-bottom: 16px; }
  .run-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 10px; }
  .run-card .rid { font-family: "Cascadia Code", Consolas, monospace; font-size: 13px; color: #58a6ff; }
  .run-card .goal { font-size: 12px; color: #8b949e; margin: 6px 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .run-card .actions { display: flex; gap: 6px; margin-top: 8px; }
  .tabs { display: flex; gap: 4px; margin-bottom: 8px; }
  .tab-btn { background: #21262d; color: #8b949e; }
  .tab-btn.active { background: #1f6feb; color: #fff; }
  .tab-pane { display: none; }
  .tab-pane.active { display: block; }
  .board { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-bottom: 16px; }
  .column { background: #161b22; border-radius: 8px; padding: 8px; min-height: 100px; }
  .column h3 { font-size: 12px; text-transform: uppercase; color: #8b949e; margin-bottom: 8px; text-align: center; }
  .task-card { background: #0d1117; border-radius: 6px; padding: 6px 8px; margin-bottom: 6px; font-size: 12px; border: 1px solid #30363d; }
  .task-card .role { color: #58a6ff; font-size: 11px; }
  .log { background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 12px; height: 300px; overflow-y: auto; font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; }
  .log-entry { margin-bottom: 4px; opacity: 0.9; }
  .log-entry .ts { color: #8b949e; }
  .log-entry .evt { color: #d2a8ff; font-weight: 600; }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
  .stat-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px; }
  .stat-card .num { font-size: 22px; font-weight: 700; color: #58a6ff; }
  .stat-card .label { font-size: 12px; color: #8b949e; margin-top: 4px; }
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
  <input id="run-id" placeholder="Run ID（如 20261006-xxxxxx-ab12）" value="">
  <button onclick="connect()">连接</button>
  <button id="btn-delete" class="btn-danger" onclick="deleteRun()" disabled>删除当前 Run</button>
  <span id="run-status" class="status-badge status-pending">pending</span>
</div>
<div class="controls">
  <span style="opacity:.7">模型：</span>
  <input id="model-alias" placeholder="别名 openai:gpt-4o-mini" style="width:200px">
  <input id="model-role" placeholder="角色(可选)" style="width:130px">
  <button onclick="switchModel()">切换模型</button>
  <span id="model-current" class="status-badge status-pending">未连接</span>
</div>

<section>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <h2>运行列表</h2>
    <button class="small" onclick="loadRuns()">刷新列表</button>
  </div>
  <div class="run-list" id="run-list"><div style="color:#6e7681;font-size:13px">加载中…</div></div>
</section>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('board', this)">看板</button>
  <button class="tab-btn" onclick="switchTab('ledger', this)">事件日志</button>
  <button class="tab-btn" onclick="switchTab('stats', this)">统计</button>
  <button class="tab-btn" onclick="switchTab('tools', this)">工具</button>
</div>
<div class="tab-pane active" id="pane-board">
  <div class="board" id="board"></div>
  <div class="log" id="log"></div>
</div>
<div class="tab-pane" id="pane-ledger">
  <div class="controls">
    <input id="ledger-event-filter" placeholder="事件类型过滤，如 llm_call" style="width:220px">
    <button class="small" onclick="loadLedger()">查询</button>
    <span id="ledger-total" style="color:#8b949e;font-size:12px"></span>
  </div>
  <div class="log" id="ledger-list" style="height:420px"></div>
</div>
<div class="tab-pane" id="pane-stats">
  <div id="stats-panel" style="color:#6e7681">连接一个 run 后显示统计</div>
</div>
<div class="tab-pane" id="pane-tools">
  <div class="controls">
    <span id="tools-state" style="color:#8b949e;font-size:12px"></span>
    <button class="small" onclick="loadTools()">刷新工具</button>
  </div>
  <div id="tools-list" style="color:#6e7681;font-size:13px">加载中…</div>
  <div id="tool-trial" style="display:none;margin-top:12px">
    <h2 id="tool-trial-title" style="margin-bottom:6px"></h2>
    <textarea id="tool-trial-args" spellcheck="false" style="width:100%;height:120px;background:#161b22;border:1px solid #30363d;color:#e1e4e8;border-radius:6px;padding:8px;font-family:ui-monospace,Consolas,monospace;font-size:13px"></textarea>
    <div class="controls" style="margin-top:8px">
      <button class="small" onclick="runToolTrial()">运行</button>
      <button class="small" id="tool-trial-close" onclick="closeToolTrial()">关闭</button>
      <span id="tool-trial-status" style="color:#8b949e;font-size:12px"></span>
    </div>
    <pre id="tool-trial-result" class="log" style="height:240px;white-space:pre-wrap"></pre>
  </div>
</div>

<script>
const STATUSES = ["pending", "ready", "running", "review", "rework", "succeeded", "failed", "blocked", "cancelled", "skipped"];
let eventSource = null;
let currentRun = null;

// ── Tab 切换 ──────────────────────────────────────────────────────────
function switchTab(name, btn) {
  document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
  document.getElementById("pane-" + name).classList.add("active");
  btn.classList.add("active");
  if (name === "ledger" && currentRun) loadLedger();
  if (name === "stats" && currentRun) loadStats();
  if (name === "tools") loadTools();
}

// ── Run 列表（P2-10）───────────────────────────────────────────────────
async function loadRuns() {
  try {
    const resp = await fetch("/runs");
    const data = await resp.json();
    renderRunList(data.runs || []);
  } catch (e) {
    document.getElementById("run-list").innerHTML =
      '<div style="color:#f85149;font-size:13px">运行列表加载失败：' + e + "</div>";
  }
}

function renderRunList(runs) {
  const wrap = document.getElementById("run-list");
  if (!runs.length) {
    wrap.innerHTML = '<div style="color:#6e7681;font-size:13px">暂无 run</div>';
    return;
  }
  wrap.innerHTML = "";
  runs.forEach(r => {
    const card = document.createElement("div");
    card.className = "run-card";
    const status = r.status || "pending";
    const cancelBtn = status === "running"
      ? `<button class="small btn-cancel" onclick="cancelRun('${r.run_id}')">取消</button>`
      : "";
    const deleteBtn = status !== "running"
      ? `<button class="small btn-danger" onclick="deleteRunById('${r.run_id}')">删除</button>`
      : "";
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span class="rid">${r.run_id}</span>
        <span class="status-badge status-${status}">${status}</span>
      </div>
      <div class="goal" title="${r.goal || ""}">${r.goal || ""}</div>
      <div class="actions">
        <button class="small" onclick="connectById('${r.run_id}')">打开</button>
        ${cancelBtn}
        ${deleteBtn}
      </div>`;
    wrap.appendChild(card);
  });
}

async function cancelRun(runId) {
  if (!confirm(`确认取消运行中的 run ${runId}？不再派发新任务，在跑任务收尾后以 cancelled 封版。`)) return;
  try {
    const resp = await fetch(`/runs/${runId}/cancel`, { method: "POST" });
    const data = await resp.json();
    if (resp.ok) {
      alert(`已发送取消请求：${runId}`);
    } else {
      alert(`取消失败（${resp.status}）：${data.error || resp.statusText}`);
    }
  } catch (e) {
    alert(`取消请求失败：${e}`);
  }
  loadRuns();
}

async function deleteRunById(runId) {
  if (!confirm(`确认删除 run ${runId}？此操作不可恢复。`)) return;
  if (!confirm(`二次确认：真的要删除 ${runId} 吗？`)) return;
  try {
    const resp = await fetch(`/runs/${runId}?confirm=1`, { method: "DELETE" });
    const data = await resp.json();
    if (resp.ok) {
      alert(`已删除 run ${runId}，释放 ${(data.freed_bytes || 0).toLocaleString()} 字节。`);
      if (currentRun === runId) {
        currentRun = null;
        if (eventSource) { eventSource.close(); eventSource = null; }
        setStatus("pending");
        initBoard();
      }
    } else if (resp.status === 409) {
      alert(`删除被拒绝：${data.error}`);
    } else {
      alert(`删除失败（${resp.status}）：${data.error || resp.statusText}`);
    }
  } catch (e) {
    alert(`删除请求失败：${e}`);
  }
  loadRuns();
}

function connectById(runId) {
  document.getElementById("run-id").value = runId;
  connect();
}

// ── 事件日志标签页（P2-9 端点消费）──────────────────────────────────────
async function loadLedger() {
  if (!currentRun) return;
  const filter = document.getElementById("ledger-event-filter").value.trim();
  const list = document.getElementById("ledger-list");
  try {
    const url = `/runs/${currentRun}/ledger?limit=200` +
      (filter ? `&event=${encodeURIComponent(filter)}` : "");
    const resp = await fetch(url);
    const data = await resp.json();
    if (!resp.ok) {
      list.innerHTML = `<div style="color:#f85149">${data.error || resp.statusText}</div>`;
      return;
    }
    document.getElementById("ledger-total").textContent = `共 ${data.total} 条`;
    if (!(data.events || []).length) {
      list.innerHTML = '<div style="color:#6e7681">（无事件）</div>';
      return;
    }
    list.innerHTML = "";
    data.events.forEach(ev => {
      const entry = document.createElement("div");
      entry.className = "log-entry";
      entry.innerHTML = `<span class="ts">${(ev.ts || "").replace("T", " ")}</span> ` +
        `<span class="evt">${ev.event}</span> ${JSON.stringify(ev.data || {}).substring(0, 160)}`;
      list.appendChild(entry);
    });
    list.scrollTop = 0;
  } catch (e) {
    list.innerHTML = `<div style="color:#f85149">事件日志加载失败：${e}</div>`;
  }
}

// ── 统计标签页 ─────────────────────────────────────────────────────────
async function loadStats() {
  if (!currentRun) return;
  const panel = document.getElementById("stats-panel");
  try {
    const resp = await fetch(`/runs/${currentRun}/stats`);
    const data = await resp.json();
    if (!resp.ok) {
      panel.innerHTML = `<div style="color:#f85149">${data.error || resp.statusText}</div>`;
      return;
    }
    const llm = data.llm || {};
    const tasks = data.tasks || {};
    const tools = data.tools || {};
    const sr = tasks.started ? ((tasks.success_rate || 0) * 100).toFixed(1) + "%" : "n/a";
    const avg = tasks.avg_duration_ms != null ? tasks.avg_duration_ms + " ms" : "n/a";
    panel.innerHTML = `
      <div class="stats-grid">
        <div class="stat-card"><div class="num">${llm.calls || 0}</div><div class="label">LLM 调用次数</div></div>
        <div class="stat-card"><div class="num">${(llm.total_tokens || 0).toLocaleString()}</div><div class="label">总 tokens（入 ${llm.prompt_tokens || 0} / 出 ${llm.completion_tokens || 0}）</div></div>
        <div class="stat-card"><div class="num">$${(llm.cost_usd || 0).toFixed(4)}</div><div class="label">估算成本 (USD)</div></div>
        <div class="stat-card"><div class="num">${tasks.finished || 0}/${tasks.started || 0}</div><div class="label">任务完成（成功率 ${sr}）</div></div>
        <div class="stat-card"><div class="num">${avg}</div><div class="label">平均任务时长</div></div>
        <div class="stat-card"><div class="num">${tools.calls || 0}</div><div class="label">工具调用（成功 ${tools.success || 0} / 失败 ${tools.failed || 0}）</div></div>
      </div>
      <h3 style="font-size:13px;margin:14px 0 8px;color:#8b949e">事件分布</h3>
      <div>${Object.entries(data.event_counts || {}).map(([k, v]) =>
        `<span class="status-badge" style="margin:2px">${k}: ${v}</span>`).join("")}</div>`;
  } catch (e) {
    panel.innerHTML = `<div style="color:#f85149">统计加载失败：${e}</div>`;
  }
}

// ── 以下为原有看板 / SSE 逻辑 ──────────────────────────────────────────
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
    col.querySelectorAll(".task-card").forEach(c => c.remove());
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
  const btn = document.getElementById("btn-delete");
  if (btn) btn.disabled = (status === "running" || !currentRun);
}

async function deleteRun() {
  if (!currentRun) { alert("请先连接一个 Run"); return; }
  if (!confirm(`确认删除 run ${currentRun}？此操作不可恢复。`)) return;
  try {
    const resp = await fetch(`/runs/${currentRun}?confirm=1`, { method: "DELETE" });
    const data = await resp.json();
    if (resp.ok) {
      alert(`已删除 run ${currentRun}，释放 ${data.freed_bytes?.toLocaleString() || 0} 字节。`);
      if (eventSource) { eventSource.close(); eventSource = null; }
      currentRun = null;
      setStatus("pending");
      initBoard();
    } else if (resp.status === 409) {
      alert(`删除被拒绝：${data.error}`);
    } else {
      alert(`删除失败（${resp.status}）：${data.error || resp.statusText}`);
    }
  } catch (e) {
    alert(`删除请求失败：${e}`);
  }
  loadRuns();
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

  try {
    const resp = await fetch(`/runs/${runId}/status`);
    if (resp.ok) {
      const data = await resp.json();
      setStatus(data.status || "pending");
      updateTask(data.tasks || {});
    }
  } catch (e) {}

  refreshModel();
  loadLedger();
  loadStats();

  eventSource = new EventSource(`/runs/${runId}/events`);
  eventSource.onopen = () => setConn(true);
  eventSource.onerror = () => setConn(false);
  eventSource.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      logEvent("message", data);
    } catch (err) {}
  };
  eventSource.addEventListener("connected", (e) => {
    logEvent("connected", JSON.parse(e.data));
  });
  eventSource.addEventListener("task_transition", (e) => {
    const data = JSON.parse(e.data);
    logEvent("task_transition", data);
    fetch(`/runs/${runId}/status`).then(r => r.json()).then(d => {
      setStatus(d.status || "pending");
      updateTask(d.tasks || {});
    });
  });
  eventSource.addEventListener("run_completed", (e) => {
    setStatus("completed");
    logEvent("run_completed", JSON.parse(e.data));
    loadRuns();
  });
  eventSource.addEventListener("run_failed", (e) => {
    setStatus("failed");
    logEvent("run_failed", JSON.parse(e.data));
    loadRuns();
  });
}

async function refreshModel() {
  if (!currentRun) return;
  try {
    const resp = await fetch(`/runs/${currentRun}/model`);
    if (!resp.ok) {
      document.getElementById("model-current").textContent = "n/a";
      return;
    }
    const data = await resp.json();
    const roles = Object.entries(data.role_bindings || {})
      .map(([r, a]) => `${r}→${a}`).join(" ");
    document.getElementById("model-current").textContent =
      `默认 ${data.default}` + (roles ? `（${roles}）` : "");
  } catch (e) {}
}

async function switchModel() {
  if (!currentRun) { alert("请先连接一个 Run"); return; }
  const alias = document.getElementById("model-alias").value.trim();
  if (!alias) { alert("请输入模型别名"); return; }
  const role = document.getElementById("model-role").value.trim() || null;
  try {
    const resp = await fetch(`/runs/${currentRun}/model`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alias, role })
    });
    const data = await resp.json();
    if (resp.ok) {
      alert(`已切换：默认 ${data.default}\\n角色绑定：${JSON.stringify(data.role_bindings)}`);
      refreshModel();
    } else {
      alert(`切换失败（${resp.status}）：${data.error || resp.statusText}`);
    }
  } catch (e) { alert(`请求失败：${e}`); }
}

// ── R24：工具管理 + 单工具试跑 ──────────────────────────────────────
let toolsCache = null;
let trialTool = null;

async function loadTools() {
  const box = document.getElementById("tools-list");
  const state = document.getElementById("tools-state");
  if (!box) return;
  try {
    const resp = await fetch("/tools");
    const data = await resp.json();
    if (!resp.ok) {
      box.textContent = `加载失败（${resp.status}）：${data.error || ""}`;
      return;
    }
    toolsCache = data.tools || [];
    state.textContent = data.tools_enabled
      ? `工具执行已开启 · 试跑沙箱：${data.trial_dir}/（相对 output 目录）`
      : "工具执行未开启（AGENTCREW_TOOLS_ENABLED=true）——可查看，不能试跑";
    renderTools();
  } catch (e) {
    box.textContent = `请求失败：${e}`;
  }
}

function renderTools() {
  const box = document.getElementById("tools-list");
  if (!toolsCache || !toolsCache.length) { box.textContent = "没有工具"; return; }
  box.innerHTML = "";
  for (const tool of toolsCache) {
    const schema = tool.input_schema || {};
    const req = (schema.required || []).join("、") || "无必填";
    const row = document.createElement("div");
    row.className = "run-card";
    row.style.marginBottom = "8px";
    const head = document.createElement("div");
    head.style.cssText = "display:flex;justify-content:space-between;align-items:center";
    const title = document.createElement("strong");
    title.textContent = tool.name;
    const right = document.createElement("span");
    right.style.cssText = "display:flex;gap:8px;align-items:center";
    const kind = document.createElement("span");
    kind.className = "status-badge status-pending";
    kind.textContent = tool.kind;
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = "试跑";
    btn.onclick = () => openToolTrial(tool.name);
    right.append(kind, btn);
    head.append(title, right);
    const desc = document.createElement("div");
    desc.style.cssText = "color:#8b949e;font-size:12px;margin-top:4px";
    desc.textContent = tool.description;
    const meta = document.createElement("div");
    meta.style.cssText = "color:#6e7681;font-size:12px;margin-top:2px";
    meta.textContent = `必填：${req}`;
    row.append(head, desc, meta);
    box.appendChild(row);
  }
}

function openToolTrial(name) {
  trialTool = name;
  const tool = (toolsCache || []).find(item => item.name === name);
  document.getElementById("tool-trial").style.display = "block";
  document.getElementById("tool-trial-title").textContent = `试跑 ${name}`;
  document.getElementById("tool-trial-status").textContent = "";
  document.getElementById("tool-trial-result").textContent = "";
  const sample = {};
  const props = (tool && tool.input_schema && tool.input_schema.properties) || {};
  for (const key of Object.keys(props)) {
    const rule = props[key] || {};
    if (rule.type === "string") sample[key] = "";
    else if (rule.type === "integer" || rule.type === "number") sample[key] = 0;
    else if (rule.type === "boolean") sample[key] = false;
    else sample[key] = null;
  }
  document.getElementById("tool-trial-args").value = JSON.stringify(sample, null, 2);
}

function closeToolTrial() {
  document.getElementById("tool-trial").style.display = "none";
  trialTool = null;
}

async function runToolTrial() {
  if (!trialTool) return;
  const status = document.getElementById("tool-trial-status");
  const out = document.getElementById("tool-trial-result");
  let args;
  try {
    args = JSON.parse(document.getElementById("tool-trial-args").value || "{}");
  } catch (e) {
    status.textContent = `入参不是合法 JSON：${e}`;
    return;
  }
  status.textContent = "运行中…";
  out.textContent = "";
  try {
    const resp = await fetch(`/tools/${encodeURIComponent(trialTool)}/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args })
    });
    const data = await resp.json();
    if (!resp.ok) {
      status.textContent = `失败（${resp.status}）`;
      out.textContent = data.error || "";
      return;
    }
    status.textContent = `${data.ok ? "成功" : "工具报错"} · ${data.duration_ms}ms · 沙箱 ${data.sandbox}`;
    out.textContent = data.ok ? (data.result || "") : (data.error || "");
  } catch (e) {
    status.textContent = `请求失败：${e}`;
  }
}

initBoard();
loadRuns();
setInterval(loadRuns, 5000);
</script>
</body>
</html>
"""
