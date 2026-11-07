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
  <button class="tab-btn" onclick="switchTab('keys', this)">密钥</button>
  <button class="tab-btn" onclick="switchTab('flow', this)">编排</button>
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
<div class="tab-pane" id="pane-keys">
  <div class="controls">
    <span id="keys-hint" style="color:#8b949e;font-size:12px"></span>
    <button class="small" onclick="loadKeys()">刷新</button>
  </div>
  <div id="keys-list" style="color:#6e7681;font-size:13px">加载中…</div>
  <div style="margin-top:12px">
    <h2 style="margin-bottom:6px">新建密钥</h2>
    <div class="controls">
      <input id="key-name" placeholder="身份名，如 alice" style="width:160px">
      <select id="key-role" style="background:#161b22;border:1px solid #30363d;color:#e1e4e8;padding:6px 10px;border-radius:6px">
        <option value="viewer">viewer</option>
        <option value="operator" selected>operator</option>
        <option value="admin">admin</option>
      </select>
      <input id="key-runs" placeholder="可见 run（逗号分隔，可空）" style="width:260px">
      <input id="key-expires" placeholder="过期 ISO 时间（可空）" style="width:200px">
      <button class="small" onclick="createKey()">创建</button>
    </div>
    <pre id="key-created" class="log" style="display:none;height:120px;white-space:pre-wrap;margin-top:8px"></pre>
  </div>
</div>
<div class="tab-pane" id="pane-flow">
  <div class="controls">
    <input id="flow-name" placeholder="流程名" style="width:130px">
    <button class="small" onclick="flowAddNode()">新建节点</button>
    <button class="small btn-danger" onclick="flowDeleteSelected()">删除选中</button>
    <button class="small" onclick="flowAutoLayout()">自动布局</button>
    <button class="small" onclick="flowValidate()">校验</button>
    <button class="small" onclick="flowSave()">保存</button>
    <button class="small" onclick="flowSample()">示例</button>
    <button class="small" onclick="flowToggleYaml()">YAML</button>
    <input id="flow-goal" placeholder="起跑目标（可空）" style="width:180px">
    <button class="small" onclick="flowStartRun()">起跑</button>
    <select id="flow-select" onchange="flowLoadSelected()" style="background:#161b22;border:1px solid #30363d;color:#e1e4e8;padding:6px;border-radius:6px">
      <option value="">载入…</option>
    </select>
    <span id="flow-status" style="color:#8b949e;font-size:12px"></span>
  </div>
  <div style="display:flex;gap:10px">
    <div style="flex:1;min-width:420px;background:#0d1117;border:1px solid #30363d;border-radius:8px;overflow:auto">
      <svg id="flow-canvas" width="100%" height="520" style="display:block"></svg>
    </div>
    <div id="flow-props" style="width:250px;color:#8b949e;font-size:12px"></div>
  </div>
  <div id="flow-yaml-box" style="display:none;margin-top:8px">
    <textarea id="flow-yaml" spellcheck="false" style="width:100%;height:180px;background:#161b22;border:1px solid #30363d;color:#e1e4e8;border-radius:6px;padding:8px;font-family:ui-monospace,Consolas,monospace;font-size:12px"></textarea>
    <div class="controls" style="margin-top:6px">
      <button class="small" onclick="flowApplyYaml()">应用 YAML</button>
      <span style="color:#6e7681;font-size:12px">导入 / 导出都走 /flows/validate 的规范化 YAML，与引擎同一份 schema</span>
    </div>
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
  if (name === "keys") loadKeys();
  if (name === "flow") { flowEnsureState(); flowRender(); flowRefreshList(); }
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

// ── R25：密钥管理（多用户 RBAC）─────────────────────────────────────
async function loadKeys() {
  const box = document.getElementById("keys-list");
  const hint = document.getElementById("keys-hint");
  if (!box) return;
  try {
    const resp = await fetch("/keys");
    const data = await resp.json();
    if (!resp.ok) {
      hint.textContent = "";
      box.textContent = resp.status === 403
        ? "当前密钥没有 keys:read 作用域（需要 admin）"
        : (data.error || `加载失败（${resp.status}）`);
      return;
    }
    hint.textContent = `角色 ${Object.keys(data.roles || {}).join(" / ")} · ${data.keys.length} 条`;
    if (!data.keys.length) {
      box.textContent = "密钥库为空——用下方表单或 main.py keys add 新建";
      return;
    }
    box.innerHTML = "";
    for (const key of data.keys) {
      const card = document.createElement("div");
      card.className = "run-card";
      card.style.marginBottom = "8px";
      const head = document.createElement("div");
      head.style.cssText = "display:flex;justify-content:space-between;align-items:center";
      const title = document.createElement("strong");
      title.textContent = `${key.name}（${key.role}）`;
      const right = document.createElement("span");
      right.style.cssText = "display:flex;gap:8px;align-items:center";
      const state = document.createElement("span");
      state.className = "status-badge " + (key.revoked_at ? "status-failed" : "status-completed");
      state.textContent = key.revoked_at ? "已吊销" : "有效";
      right.append(state);
      if (!key.revoked_at) {
        const btn = document.createElement("button");
        btn.className = "small btn-danger";
        btn.textContent = "吊销";
        btn.onclick = () => revokeKey(key.id, key.name);
        right.append(btn);
      }
      head.append(title, right);
      const meta = document.createElement("div");
      meta.style.cssText = "color:#6e7681;font-size:12px;margin-top:4px";
      meta.textContent = `${key.id} · 建 ${key.created_at} · 用 ${key.last_used_at || "—"}`
        + (key.expires_at ? ` · 过期 ${key.expires_at}` : "")
        + (key.run_ids && key.run_ids.length ? ` · 可见 ${key.run_ids.join(",")}` : " · 仅自己的 run");
      card.append(head, meta);
      box.appendChild(card);
    }
  } catch (e) { box.textContent = `请求失败：${e}`; }
}

async function createKey() {
  const name = document.getElementById("key-name").value.trim();
  if (!name) { alert("先填身份名"); return; }
  const body = {
    name: name,
    role: document.getElementById("key-role").value,
    run_ids: document.getElementById("key-runs").value
      .split(",").map(s => s.trim()).filter(Boolean),
  };
  const expires = document.getElementById("key-expires").value.trim();
  if (expires) body.expires_at = expires;
  try {
    const resp = await fetch("/keys", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await resp.json();
    if (!resp.ok) { alert(`创建失败（${resp.status}）：${data.error || ""}`); return; }
    const box = document.getElementById("key-created");
    box.style.display = "block";
    box.textContent = `【只显示这一次，请立即保存】\n${data.key}\n\n` + data.notice;
    loadKeys();
  } catch (e) { alert(`请求失败：${e}`); }
}

async function revokeKey(id, name) {
  if (!confirm(`吊销密钥「${name}」？之后用它的请求会立刻 401。`)) return;
  try {
    const resp = await fetch(`/keys/${encodeURIComponent(id)}`, { method: "DELETE" });
    const data = await resp.json();
    if (!resp.ok) { alert(`吊销失败（${resp.status}）：${data.error || ""}`); return; }
    loadKeys();
  } catch (e) { alert(`请求失败：${e}`); }
}

// ── R26：可视化编排画布（节点 / 连线 / 属性面板 / YAML 双向）──────────
const FLOW_NODE_W = 156, FLOW_NODE_H = 58;
let flowState = null;   // {name, description, tasks:[], pos:{id:{x,y}}}
let flowSelected = null;
let flowConnectFrom = null;
let flowDrag = null;

function flowNewTask(id) {
  return {
    id: id, title: "新任务", description: "", role: "backend_dev",
    acceptance_criteria: [], deliverables: [], dependencies: [],
    priority: 5, brief: "", timeout_seconds: 300, when_outcomes: []
  };
}

function flowNextId() {
  let n = 1;
  const ids = new Set(flowState.tasks.map(t => t.id));
  while (ids.has("T-" + String(n).padStart(3, "0"))) n++;
  return "T-" + String(n).padStart(3, "0");
}

function flowEnsureState() {
  if (!flowState) {
    flowState = { name: "untitled", description: "", tasks: [], pos: {} };
  }
  return flowState;
}

function flowAddNode() {
  flowEnsureState();
  const id = flowNextId();
  flowState.tasks.push(flowNewTask(id));
  const index = flowState.tasks.length - 1;
  flowState.pos[id] = { x: 40 + (index % 4) * 200, y: 30 + Math.floor(index / 4) * 100 };
  flowSelected = id;
  flowRender();
}

function flowDeleteSelected() {
  if (!flowSelected || !flowState) return;
  const id = flowSelected;
  flowState.tasks = flowState.tasks.filter(t => t.id !== id);
  for (const task of flowState.tasks) {
    task.dependencies = task.dependencies.filter(d => d !== id);
  }
  delete flowState.pos[id];
  flowSelected = null;
  flowConnectFrom = null;
  flowRender();
}

function flowRenameTask(oldId, newId) {
  if (!newId || newId === oldId) return;
  if (flowState.tasks.some(t => t.id === newId)) { alert("id 已存在"); return; }
  const task = flowState.tasks.find(t => t.id === oldId);
  if (!task) return;
  task.id = newId;
  for (const other of flowState.tasks) {
    other.dependencies = other.dependencies.map(d => d === oldId ? newId : d);
  }
  flowState.pos[newId] = flowState.pos[oldId] || { x: 40, y: 30 };
  delete flowState.pos[oldId];
  flowSelected = newId;
  flowRender();
}

function flowConnect(toId) {
  if (!flowConnectFrom || flowConnectFrom === toId) {
    flowConnectFrom = null;
    flowRender();
    return;
  }
  const target = flowState.tasks.find(t => t.id === toId);
  if (!target) return;
  if (!target.dependencies.includes(flowConnectFrom)) {
    target.dependencies.push(flowConnectFrom);
  }
  flowConnectFrom = null;
  flowRender();
}

function flowRender() {
  const svg = document.getElementById("flow-canvas");
  if (!svg) return;
  flowEnsureState();
  const ns = "http://www.w3.org/2000/svg";
  svg.innerHTML = "";
  const defs = document.createElementNS(ns, "defs");
  defs.innerHTML = '<marker id="flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#58a6ff"/></marker>';
  svg.appendChild(defs);

  for (const task of flowState.tasks) {
    const from = flowState.pos[task.id] || { x: 40, y: 30 };
    for (const dep of task.dependencies) {
      const source = flowState.pos[dep];
      if (!source) continue;
      const x1 = source.x + FLOW_NODE_W, y1 = source.y + FLOW_NODE_H / 2;
      const x2 = from.x, y2 = from.y + FLOW_NODE_H / 2;
      const mid = Math.max(40, Math.abs(x2 - x1) / 2);
      const path = document.createElementNS(ns, "path");
      path.setAttribute("d", `M ${x1} ${y1} C ${x1 + mid} ${y1}, ${x2 - mid} ${y2}, ${x2} ${y2}`);
      path.setAttribute("stroke", (task.when_outcomes || []).length ? "#d29922" : "#58a6ff");
      path.setAttribute("stroke-width", "1.6");
      path.setAttribute("fill", "none");
      path.setAttribute("marker-end", "url(#flow-arrow)");
      path.style.cursor = "pointer";
      path.addEventListener("click", () => {
        if (confirm(`删除连线 ${dep} → ${task.id}？`)) {
          task.dependencies = task.dependencies.filter(d => d !== dep);
          flowRender();
        }
      });
      svg.appendChild(path);
      if ((task.when_outcomes || []).length) {
        const label = document.createElementNS(ns, "text");
        label.setAttribute("x", (x1 + x2) / 2);
        label.setAttribute("y", (y1 + y2) / 2 - 4);
        label.setAttribute("fill", "#d29922");
        label.setAttribute("font-size", "10");
        label.setAttribute("text-anchor", "middle");
        label.textContent = task.when_outcomes.join(",");
        svg.appendChild(label);
      }
    }
  }

  for (const task of flowState.tasks) {
    const pos = flowState.pos[task.id] || { x: 40, y: 30 };
    const g = document.createElementNS(ns, "g");
    g.setAttribute("transform", `translate(${pos.x},${pos.y})`);
    g.style.cursor = "move";
    const rect = document.createElementNS(ns, "rect");
    rect.setAttribute("width", FLOW_NODE_W);
    rect.setAttribute("height", FLOW_NODE_H);
    rect.setAttribute("rx", "8");
    rect.setAttribute("fill", "#161b22");
    rect.setAttribute("stroke", flowSelected === task.id ? "#58a6ff" : "#30363d");
    rect.setAttribute("stroke-width", flowSelected === task.id ? "2" : "1");
    g.appendChild(rect);
    const idText = document.createElementNS(ns, "text");
    idText.setAttribute("x", "10"); idText.setAttribute("y", "18");
    idText.setAttribute("fill", "#e1e4e8"); idText.setAttribute("font-size", "12");
    idText.setAttribute("font-weight", "600");
    idText.textContent = task.id;
    g.appendChild(idText);
    const title = document.createElementNS(ns, "text");
    title.setAttribute("x", "10"); title.setAttribute("y", "34");
    title.setAttribute("fill", "#8b949e"); title.setAttribute("font-size", "11");
    title.textContent = (task.title || "").slice(0, 18);
    g.appendChild(title);
    const role = document.createElementNS(ns, "text");
    role.setAttribute("x", "10"); role.setAttribute("y", "48");
    role.setAttribute("fill", "#6e7681"); role.setAttribute("font-size", "10");
    role.textContent = `${task.role} · p${task.priority}`;
    g.appendChild(role);
    const portIn = document.createElementNS(ns, "circle");
    portIn.setAttribute("cx", "0"); portIn.setAttribute("cy", String(FLOW_NODE_H / 2));
    portIn.setAttribute("r", "5"); portIn.setAttribute("fill", "#30363d");
    portIn.setAttribute("stroke", "#58a6ff");
    portIn.style.cursor = "crosshair";
    portIn.addEventListener("click", (event) => { event.stopPropagation(); flowConnect(task.id); });
    g.appendChild(portIn);
    const portOut = document.createElementNS(ns, "circle");
    portOut.setAttribute("cx", String(FLOW_NODE_W));
    portOut.setAttribute("cy", String(FLOW_NODE_H / 2));
    portOut.setAttribute("r", "5");
    portOut.setAttribute("fill", flowConnectFrom === task.id ? "#d29922" : "#30363d");
    portOut.setAttribute("stroke", "#58a6ff");
    portOut.style.cursor = "crosshair";
    portOut.addEventListener("click", (event) => {
      event.stopPropagation();
      flowConnectFrom = flowConnectFrom === task.id ? null : task.id;
      flowRender();
    });
    g.appendChild(portOut);
    g.addEventListener("mousedown", (event) => {
      if (event.target === portIn || event.target === portOut) return;
      flowSelected = task.id;
      const current = flowState.pos[task.id] || { x: 40, y: 30 };
      flowDrag = { id: task.id, dx: event.clientX - current.x, dy: event.clientY - current.y };
      flowRender();
    });
    svg.appendChild(g);
  }
  flowRenderProps();
}

function flowRenderProps() {
  const box = document.getElementById("flow-props");
  if (!box) return;
  if (!flowState) flowEnsureState();
  const task = flowState.tasks.find(t => t.id === flowSelected);
  if (!task) {
    box.innerHTML = `<div style="margin-bottom:8px;color:#e1e4e8">${flowState.tasks.length} 个节点</div><div>拖节点移动；点右端口再点左端口连线；点连线删除。</div>`;
    return;
  }
  box.innerHTML = `
    <div style="margin-bottom:6px;color:#e1e4e8;font-weight:600">属性 · ${task.id}</div>
    <label>id</label><input id="fp-id" value="${task.id}" style="width:100%;margin:2px 0 6px">
    <label>标题</label><input id="fp-title" value="${(task.title || "").replace(/"/g, "&quot;")}" style="width:100%;margin:2px 0 6px">
    <label>角色</label><input id="fp-role" value="${task.role}" style="width:100%;margin:2px 0 6px">
    <label>优先级 1-9</label><input id="fp-priority" type="number" min="1" max="9" value="${task.priority}" style="width:100%;margin:2px 0 6px">
    <label>描述</label><textarea id="fp-desc" style="width:100%;height:52px;margin:2px 0 6px">${task.description || ""}</textarea>
    <label>依赖（逗号）</label><input id="fp-deps" value="${task.dependencies.join(",")}" style="width:100%;margin:2px 0 6px">
    <label>条件标签（逗号，可空）</label><input id="fp-when" value="${(task.when_outcomes || []).join(",")}" style="width:100%;margin:2px 0 6px">
    <label>产物（每行 path|kind|说明）</label><textarea id="fp-deliv" style="width:100%;height:52px;margin:2px 0 6px">${(task.deliverables || []).map(d => `${d.path}|${d.kind}|${d.description || ""}`).join("\n")}</textarea>
    <label>验收标准（每行一条）</label><textarea id="fp-accept" style="width:100%;height:44px;margin:2px 0 6px">${(task.acceptance_criteria || []).join("\n")}</textarea>
    <button class="small" onclick="flowApplyProps()">应用</button>
  `;
}

function flowApplyProps() {
  if (!flowState) return;
  const task = flowState.tasks.find(t => t.id === flowSelected);
  if (!task) return;
  const value = id => (document.getElementById(id) || {}).value || "";
  const newId = value("fp-id").trim();
  if (newId && newId !== task.id) flowRenameTask(task.id, newId);
  const current = flowState.tasks.find(t => t.id === (newId || flowSelected));
  if (!current) return;
  current.title = value("fp-title").trim() || current.id;
  current.role = value("fp-role").trim() || "backend_dev";
  current.priority = Math.min(9, Math.max(1, parseInt(value("fp-priority"), 10) || 5));
  current.description = value("fp-desc");
  current.dependencies = value("fp-deps").split(",").map(s => s.trim()).filter(Boolean);
  current.when_outcomes = value("fp-when").split(",").map(s => s.trim()).filter(Boolean);
  current.deliverables = value("fp-deliv").split("\n").map(line => line.trim()).filter(Boolean).map(line => {
    const parts = line.split("|");
    return { path: (parts[0] || "").trim(), kind: (parts[1] || "markdown").trim(),
             description: (parts[2] || "").trim() };
  }).filter(d => d.path);
  current.acceptance_criteria = value("fp-accept").split("\n").map(s => s.trim()).filter(Boolean);
  flowRender();
}

function flowAutoLayout() {
  if (!flowState) flowEnsureState();
  const depth = {};
  const byId = Object.fromEntries(flowState.tasks.map(t => [t.id, t]));
  function depthOf(id, seen) {
    if (depth[id] !== undefined) return depth[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    const task = byId[id];
    const deps = (task ? task.dependencies : []).filter(d => byId[d]);
    const value = deps.length ? 1 + Math.max(...deps.map(d => depthOf(d, seen))) : 0;
    depth[id] = value;
    return value;
  }
  flowState.tasks.forEach(t => depthOf(t.id, new Set()));
  const columns = {};
  for (const task of flowState.tasks) {
    const column = depth[task.id] || 0;
    columns[column] = (columns[column] || 0) + 1;
    flowState.pos[task.id] = { x: 40 + column * 210, y: 20 + (columns[column] - 1) * 92 };
  }
  flowRender();
}

function flowJson() {
  flowEnsureState();
  return {
    version: 1, name: flowState.name || "untitled",
    description: flowState.description || "",
    tasks: flowState.tasks.map(t => ({
      id: t.id, title: t.title, description: t.description, role: t.role,
      acceptance_criteria: t.acceptance_criteria || [],
      deliverables: t.deliverables || [],
      dependencies: t.dependencies || [],
      priority: t.priority || 5,
      brief: t.brief || "",
      timeout_seconds: t.timeout_seconds || 300,
      when_outcomes: t.when_outcomes || [],
    })),
  };
}

async function flowValidate() {
  const status = document.getElementById("flow-status");
  status.textContent = "校验中…";
  try {
    const resp = await fetch("/flows/validate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flow: flowJson() })
    });
    const data = await resp.json();
    if (!resp.ok) { status.textContent = `失败（${resp.status}）：${data.error || ""}`; return null; }
    flowState.yaml = data.yaml;
    status.textContent = data.ok
      ? `合法 · ${data.view.nodes.length} 节点 / ${data.view.edges.length} 边`
      : `不合法：${data.errors.join("；")}`;
    status.style.color = data.ok ? "#3fb950" : "#f85149";
    return data;
  } catch (e) { status.textContent = `请求失败：${e}`; return null; }
}

async function flowSave() {
  const status = document.getElementById("flow-status");
  const name = (document.getElementById("flow-name").value || flowState.name || "").trim();
  if (!name) { alert("先填流程名（字母数字开头）"); return; }
  flowState.name = name;
  try {
    const resp = await fetch("/flows", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flow: flowJson() })
    });
    const data = await resp.json();
    if (!resp.ok) { status.textContent = `保存失败（${resp.status}）：${data.error || ""}`; return; }
    status.textContent = `已保存 ${data.saved}`;
    status.style.color = "#3fb950";
    flowRefreshList();
  } catch (e) { status.textContent = `请求失败：${e}`; }
}

async function flowRefreshList() {
  const select = document.getElementById("flow-select");
  if (!select) return;
  try {
    const resp = await fetch("/flows");
    const data = await resp.json();
    const current = select.value;
    select.innerHTML = '<option value="">载入…</option>';
    for (const item of data.flows || []) {
      const option = document.createElement("option");
      option.value = item.name;
      option.textContent = `${item.name}（${item.tasks} 任务${item.errors ? " · " + item.errors + " 错" : ""}）`;
      select.appendChild(option);
    }
    select.value = current;
  } catch (e) { /* 列表失败不打断画布 */ }
}

async function flowLoadSelected() {
  const name = document.getElementById("flow-select").value;
  if (!name) return;
  try {
    const resp = await fetch(`/flows/${encodeURIComponent(name)}`);
    const data = await resp.json();
    if (!resp.ok) { alert(`载入失败：${data.error || resp.status}`); return; }
    flowHydrate(data.view);
    document.getElementById("flow-name").value = data.view.name;
    document.getElementById("flow-status").textContent = `已载入 ${name}`;
  } catch (e) { alert(`请求失败：${e}`); }
}

function flowHydrate(view) {
  flowState = {
    name: view.name || "untitled",
    description: view.description || "",
    tasks: (view.nodes || []).map(node => ({
      id: node.id, title: node.title, description: node.description,
      role: node.role, acceptance_criteria: node.acceptance_criteria || [],
      deliverables: node.deliverables || [],
      dependencies: node.dependencies || [],
      priority: node.priority || 5, brief: node.brief || "",
      timeout_seconds: node.timeout_seconds || 300,
      when_outcomes: node.when_outcomes || [],
    })),
    pos: {},
  };
  flowSelected = null;
  flowConnectFrom = null;
  flowAutoLayout();
}

const SAMPLE_FLOW_YAML = `version: 1
name: sample
description: 示例：需求 → 实现 → 交付
tasks:
- id: T-001
  title: 需求文档
  role: project_manager
  description: 输出 PRD
  acceptance_criteria: [含范围内外]
  deliverables:
  - {path: docs/PRD.md, kind: markdown, description: PRD}
  priority: 9
- id: T-002
  title: 后端实现
  role: backend_dev
  description: 按 PRD 实现接口
  acceptance_criteria: [接口可调用]
  deliverables:
  - {path: backend/app/main.py, kind: python, description: 服务入口}
  dependencies: [T-001]
  priority: 6
`;

async function flowSample() {
  try {
    const resp = await fetch("/flows/validate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml: SAMPLE_FLOW_YAML })
    });
    const data = await resp.json();
    if (!resp.ok || !data.view) {
      alert(`示例失败：${(data.errors || [data.error || resp.status]).join("；")}`);
      return;
    }
    flowHydrate(data.view);
    document.getElementById("flow-name").value = data.view.name;
  } catch (e) { alert(`请求失败：${e}`); }
}

function flowToggleYaml() {
  const box = document.getElementById("flow-yaml-box");
  box.style.display = box.style.display === "none" ? "block" : "none";
  if (box.style.display === "block" && flowState && flowState.yaml) {
    document.getElementById("flow-yaml").value = flowState.yaml;
  }
}

async function flowApplyYaml() {
  const text = document.getElementById("flow-yaml").value;
  const status = document.getElementById("flow-status");
  try {
    const resp = await fetch("/flows/validate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml: text })
    });
    const data = await resp.json();
    if (!resp.ok || !data.view) {
      status.textContent = `YAML 不合法：${(data.errors || [data.error || resp.status]).join("；")}`;
      status.style.color = "#f85149";
      return;
    }
    flowHydrate(data.view);
    document.getElementById("flow-name").value = data.view.name;
    document.getElementById("flow-yaml").value = data.yaml;
    status.textContent = data.ok
      ? `YAML 已应用 · ${data.view.nodes.length} 节点`
      : `YAML 已应用（图有错误：${data.errors.join("；")}）`;
    status.style.color = data.ok ? "#3fb950" : "#d29922";
  } catch (e) { status.textContent = `请求失败：${e}`; }
}

// R28：按当前画布起跑——把流程内联进 options.flow，跳过 PM 规划。
async function flowStartRun() {
  if (!flowState) flowEnsureState();
  if (!flowState.tasks.length) { alert("画布是空的：先加节点"); return; }
  const status = document.getElementById("flow-status");
  const goalInput = document.getElementById("flow-goal");
  const goal = (goalInput.value || "").trim()
    || flowState.name || "未命名流程";
  status.textContent = "起跑中…";
  try {
    const resp = await fetch("/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal: goal, options: { flow: flowJson() } })
    });
    const data = await resp.json();
    if (!resp.ok) {
      status.textContent = `起跑失败（${resp.status}）：${data.error || ""}`;
      status.style.color = "#f85149";
      return;
    }
    status.textContent = `已起跑 ${data.run_id || ""}（跳过 PM 规划）`;
    status.style.color = "#3fb950";
    loadRuns();
  } catch (e) { status.textContent = `请求失败：${e}`; }
}

document.addEventListener("mousemove", (event) => {
  if (!flowDrag || !flowState) return;
  const svg = document.getElementById("flow-canvas");
  if (!svg) return;
  const bounds = svg.getBoundingClientRect();
  flowState.pos[flowDrag.id] = {
    x: Math.max(0, event.clientX - bounds.left - flowDrag.dx),
    y: Math.max(0, event.clientY - bounds.top - flowDrag.dy),
  };
  flowRender();
});
document.addEventListener("mouseup", () => { flowDrag = null; });

initBoard();
loadRuns();
setInterval(loadRuns, 5000);
</script>
</body>
</html>
"""
