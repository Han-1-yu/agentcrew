/**
 * 实时通道：SSE 事件流 + 主进程文件监听，合并到同一条 handleRealtimeEvent 处理。
 *
 * SSE 走主进程代理（apiStreamStart）：后端 R40 发的是「命名事件」帧
 * （`event: task_transition` 等），原生 EventSource.onmessage 一帧都收不到；
 * 而且 R36 起认证默认强制开启，EventSource 无法带 Authorization 头。
 * 主进程解析帧后通过 `sse:frame` 推 { runId, event, data }，这里按 event 分派。
 *
 * 纪律：任一时刻只允许一条实时通道（SSE 或文件 watch）。resetRealtime 会同时
 * 关掉两者，否则断线补过 watch 之后再连上 SSE 就会双通道推同一事件。
 */
import { parseTs } from '../lib/format.js';
import { S } from '../lib/store.js';

import { toast } from '../ui/toast.js';
import { updateConnIndicator } from '../ui/conn-indicator.js';

import { renderEvents } from '../features/dashboard/events.js';
import { appendStreamDelta, bindStreamPanel, resetStreamPanel } from '../features/dashboard/stream.js';
import { appendConversationDelta, refreshConversation } from '../features/conversation/index.js';

import { deriveAgents, deriveGates, enrichRun, mapRealTask, currentLoadSeq } from './data.js';
import { canRetry, sseRetryDelay } from './realtime-retry.js';

/* 事件去重：文件监听会把整份 ledger 再回放一遍，SSE 重连也可能重发，
   直接 push 会让事件页凭空多出一倍条目（审计卡最先暴露这一点）。 */
function eventKey(e) {
  return `${e.ts || ''}|${e.event || ''}|${JSON.stringify(e.data || {})}`;
}

/* 跨通道语义键：SSE 帧的 data 是裸业务 dict、ts 是渲染端本地时间，ledger 行
   是 {ts, event, data} 包装且内层不含 run_id，原键在两种来源间必然失配。
   用「事件语义 + 3 秒窗口」识别同一事件，避免双通道/重连时条目翻倍；
   精确键仍保留，窗口只看最近 12 条，不会误杀稍后的同名重试。 */
function semanticKey(e) {
  const d = e.data || {};
  return [e.event || '', d.task_id || d.taskId || '', d.to_status || d.to || d.status || '',
    d.from_status || d.from || '', d.checkpoint || '', d.id || d.approval_id || '',
    d.reason || ''].join('|');
}

function nearDuplicate(e) {
  const list = (S.dash && S.dash.events) || [];
  const key = semanticKey(e);
  const t = Date.parse(e.ts || '') || 0;
  for (let i = list.length - 1, n = 0; i >= 0 && n < 12; i--, n++) {
    const prev = list[i];
    if (semanticKey(prev) !== key) continue;
    if (Math.abs(t - (Date.parse(prev.ts || '') || 0)) <= 3000) return true;
  }
  return false;
}

function appendEvents(events) {
  if (!S.dash || !events || !events.length) return 0;
  if (!S._eventKeys) S._eventKeys = new Set((S.dash.events || []).map(eventKey));
  let added = 0;
  for (const e of events) {
    const key = eventKey(e);
    if (S._eventKeys.has(key) || nearDuplicate(e)) continue;
    S._eventKeys.add(key);
    S.dash.events.push(e);
    added += 1;
  }
  if (added && S.dash.events.length > 2000) {
    S.dash.events = S.dash.events.slice(-2000);
    S._eventKeys = new Set(S.dash.events.map(eventKey));
  }
  return added;
}

/* 后端真实事件名（api/server.py on_event、crew.py、_decide_approval）。
   旧列表里的 task_started / gate_result / budget_warning 等后端从未发布。 */
const STATE_EVENTS = new Set([
  'task_transition', 'run_started', 'run_paused', 'run_completed', 'run_failed',
  'run_finished', 'approval_requested', 'approval_decided',
]);
const STREAM_EVENT = 'agent_stream_delta';

let sseRunId = null;
let sseLive = false;
let sseSeq = 0;
let sseReloadTimer = null;
let eventsRenderTimer = null;
// C-18：SSE 断线重连状态。此前只在改设置或切 run 时才重连，网络抖动/后端重启
// 后实时面板会一直静止，只能手动切页恢复。策略见 ./realtime-retry.js。
let sseRetryTimer = null;
let sseRetryCount = 0;

/* 看板在其它页只是 .hidden，重绘整个 DAG / 事件列表纯属浪费。
   交给 boot 统一决定：在 dashboard 就全量重绘，否则只刷顶栏数字。 */
function refreshDashboard() {
  window.dispatchEvent(new Event('studio:refresh'));
}

/* 事件列表全量重建很贵：一次状态突增会连来几十帧，合并成一次渲染。 */
function scheduleRenderEvents() {
  if (eventsRenderTimer) return;
  eventsRenderTimer = setTimeout(() => {
    eventsRenderTimer = null;
    if (S.nav === 'dashboard') renderEvents();
    else if (S.nav === 'conversation') refreshConversation();
  }, 120);
}

function onSseFrame(payload) {
  if (!payload || payload.runId !== sseRunId) return;
  const name = String(payload.event || 'message');
  const data = payload.data;
  if (name === 'connected') {
    sseLive = true;
    S.sseConnected = true;
    updateConnIndicator();
    return;
  }
  // 流式正文是瞬态高频数据，只进流式面板，不进事件页 / 不触发快照刷新。
  if (name === STREAM_EVENT) {
    if (data && typeof data === 'object') { appendStreamDelta(data); appendConversationDelta(data); }
    return;
  }
  const body = data && typeof data === 'object' ? data : { value: data };
  handleRealtimeEvent({
    event: name,
    ts: body.ts || body.occurred_at || new Date().toISOString(),
    data: body,
  });
}

function onSseClosed(payload) {
  if (!payload || payload.runId !== sseRunId) return;
  S.sseConnected = false;
  sseLive = false;
  updateConnIndicator();
  // 主动断开（切 run / 退出 / 改地址）不重连。
  if (payload.reason === 'stopped') return;
  // C-18：断线先补文件监听兜住数据，再指数退避尝试恢复 SSE。
  // 此前这里以 `sseLive` 为前提，而 sseLive 只在收到 'connected' 帧后才置真——
  // 首帧被代理吞掉时既不补 watch、也不重连，面板彻底静止。
  if (S.watchDir) {
    try { window.studio.watchRun(S.watchDir, true).catch(() => {}); } catch { /* 忽略 */ }
  }
  scheduleReconnect();
}

/** C-18：指数退避重连（参数见 realtime-retry.js）。 */
function scheduleReconnect() {
  if (sseRetryTimer || !S.realMode || !sseRunId) return;
  if (!canRetry(sseRetryCount)) return;
  const delay = sseRetryDelay(sseRetryCount);
  sseRetryCount += 1;
  sseRetryTimer = setTimeout(async () => {
    sseRetryTimer = null;
    const target = sseRunId;                 // 排程时的目标；期间切 run 则作废
    if (!target || !S.realMode) return;
    if (!(await connectSSE(target))) scheduleReconnect();
  }, delay);
}

/** boot 里注册一次主进程推送与设置变更后的重连事件。 */
function initRealtime() {
  try {
    window.studio.on('sse:frame', onSseFrame);
    window.studio.on('sse:closed', onSseClosed);
    window.addEventListener('studio:reconnect', () => { reconnectRealtime(); });
    bindStreamPanel();
  } catch { /* 非 Electron 环境 */ }
}

/** 返回是否成功建立流；调用方据此决定是否回退文件 watch。 */
async function connectSSE(runId) {
  const token = ++sseSeq;
  const id = String(runId);
  const base = (S.settings && S.settings.apiBaseUrl) || '';
  if (!base || !window.studio.apiStreamStart) { updateConnIndicator(); return false; }
  sseRunId = id;
  sseLive = false;
  S.sseConnected = false;
  try {
    const r = await window.studio.apiStreamStart(id);
    // 快速连点两个 run 时，旧请求的返回值不能清掉新 run 的连接态。
    if (token !== sseSeq) return false;
    if (!r || !r.ok) {
      sseRunId = null;
      updateConnIndicator();
      if (r && r.status === 401) {
        toast('warn', 'SSE 未连接', '后端要求认证：请在「设置 → 连接」配置 API Key', 9000);
      }
      return false;
    }
    // C-18：HTTP 200 + 可读流即视为连接成功，不再死等后端首帧 'connected'。
    // 旧版后端或中间代理吞掉首帧时，只依赖帧判断会让 sseLive 永远为 false，
    // 表现为「已连上却显示文件监听、断线也不补数据」。
    sseLive = true;
    S.sseConnected = true;
    sseRetryCount = 0;
    updateConnIndicator();
    return true;
  } catch (e) {
    if (token !== sseSeq) return false;
    sseRunId = null;
    updateConnIndicator();
    return false;
  }
}

function disconnectSSE() {
  // 代次 +1：让所有在途的 apiStreamStart 返回值失效。
  sseSeq += 1;
  sseRunId = null;
  sseLive = false;
  S.sseConnected = false;
  // C-18：主动断开必须清掉重连排程，否则切 run 后旧目标会把连接拉回来。
  sseRetryCount = 0;
  if (sseRetryTimer) { clearTimeout(sseRetryTimer); sseRetryTimer = null; }
  try { Promise.resolve(window.studio.apiStreamStop()).catch(() => {}); } catch { /* 忽略 */ }
}

/**
 * 设置页改了 API 地址 / 保存了 Key 之后调用：不重连的话会一直停在文件监听，
 * 用户会以为配置没生效（只有切 run 或重启才会走到 loadRealRun 里的 connectSSE）。
 */
async function reconnectRealtime() {
  if (!S.realMode || !S.watchDir) return;
  const dir = S.watchDir;
  resetRealtime();
  const fallbackId = String(dir).split(/[\\/]/).filter(Boolean).pop() || String(dir);
  const runId = (S.dash && S.dash.run && S.dash.run.run_id) || fallbackId;
  if (!(await connectSSE(runId))) {
    try { await window.studio.watchRun(dir, true); } catch { /* 忽略 */ }
  }
  updateConnIndicator();
}

/**
 * 换 run / 退出前收干净实时通道：关 SSE + 停文件监听、取消挂起的定时器、
 * 复位连接指示。少了 unwatchRun，断线补过的 watch 会和后续 SSE 形成双通道。
 */
function resetRealtime() {
  disconnectSSE();
  if (S.watchDir) {
    try { Promise.resolve(window.studio.unwatchRun(S.watchDir)).catch(() => {}); } catch { /* 忽略 */ }
  }
  if (sseReloadTimer) { clearTimeout(sseReloadTimer); sseReloadTimer = null; }
  if (eventsRenderTimer) { clearTimeout(eventsRenderTimer); eventsRenderTimer = null; }
  resetStreamPanel();
  updateConnIndicator();
}

// D-14：时间戳可能是 aware UTC（`+00:00`）或存量 naive 串，混存时字典序会判反
// （"16:00+00:00" 小于 "23:00" 但实际更晚），统一经 parseTs 比较。
// C-19：watch 推送的是「读盘那一刻」快照，可能晚于更近的 loadRun 响应，旧的不落地。
function onWatchSnapshot(payload) {
  if (!payload || payload.dir !== S.watchDir) return;
  const incomingAt = parseTs(payload.run && payload.run.updated_at);
  const currentAt = parseTs(S.dash && S.dash.run && S.dash.run.updated_at);
  if (incomingAt && currentAt && incomingAt < currentAt) return;
  if (payload.run) S.dash.run = Object.assign(enrichRun(payload.run), { dir: S.watchDir });
  if (payload.tasks) {
    S.dash.tasks = payload.tasks.map(mapRealTask);
    S.dash.agents = deriveAgents(S.dash.tasks);
    S.dash.gates = deriveGates(S.dash.tasks);
  }
  refreshDashboard();
}

/* watch 事件：ledger 增量事件 */
function onWatchLedger(payload) {
  if (!payload || payload.dir !== S.watchDir) return;
  if (appendEvents(payload.events || [])) scheduleRenderEvents();
}

/* SSE 实时事件处理 */
function handleRealtimeEvent(evt) {
  if (!S.dash) return;
  appendEvents([evt]);
  scheduleRenderEvents();
  // 状态变更类事件触发快照刷新（防抖 500ms）
  if (STATE_EVENTS.has(evt.event)) {
    if (sseReloadTimer) clearTimeout(sseReloadTimer);
    // 记下调度那一刻的 run 目录：定时器触发期间用户可能已经切了 run，
    // 不能拿旧的 S.watchDir 去刷新新数据（也不能在离开页面后强刷看板）。
    const scheduledDir = S.watchDir;
    // C-19：同时记下载入代次。同一 run 的「手动重刷」与这里的防抖快照会并发
    // 调用 loadRun，只比对目录不足以区分——旧响应晚到就会覆盖新快照（状态
    // 短暂回退）。代次一致才允许落地。
    const scheduledSeq = currentLoadSeq();
    sseReloadTimer = setTimeout(async () => {
      sseReloadTimer = null;
      if (scheduledDir && scheduledDir === S.watchDir && S.dash
          && scheduledSeq === currentLoadSeq()) {
        try {
          const snap = await window.studio.loadRun(scheduledDir);
          if (snap && snap.run && scheduledDir === S.watchDir
              && scheduledSeq === currentLoadSeq()) {
            S.dash.run = Object.assign(enrichRun(snap.run), { dir: scheduledDir });
            if (snap.tasks) {
              S.dash.tasks = snap.tasks.map(mapRealTask);
              S.dash.agents = deriveAgents(S.dash.tasks);
              S.dash.gates = deriveGates(S.dash.tasks);
            }
            refreshDashboard();
          }
        } catch {}
      }
    }, 500);
  }
}

export {
  connectSSE,
  disconnectSSE,
  handleRealtimeEvent,
  initRealtime,
  onWatchLedger,
  onWatchSnapshot,
  reconnectRealtime,
  resetRealtime,
  updateConnIndicator,
};
