/**
 * 实时通道：SSE 事件流 + 主进程文件监听，合并到同一条 handleRealtimeEvent 处理。
 */
import { $ } from '../lib/dom.js';
import { S } from '../lib/store.js';

import { toast } from '../ui/toast.js';

import { renderEvents } from '../features/dashboard/events.js';
import { renderDashboard } from '../features/dashboard/index.js';

import { deriveAgents, deriveGates, enrichRun, mapRealTask } from './data.js';
import { ev } from './mock.js';
/* 事件去重：文件监听会把整份 ledger 再回放一遍，SSE 重连也可能重发，
   直接 push 会让事件页凭空多出一倍条目（审计卡最先暴露这一点）。 */
function eventKey(e) {
  return `${e.ts || ''}|${e.event || ''}|${JSON.stringify(e.data || {})}`;
}

function appendEvents(events) {
  if (!S.dash || !events || !events.length) return 0;
  if (!S._eventKeys) S._eventKeys = new Set((S.dash.events || []).map(eventKey));
  let added = 0;
  for (const e of events) {
    const key = eventKey(e);
    if (S._eventKeys.has(key)) continue;
    S._eventKeys.add(key);
    S.dash.events.push(e);
    added += 1;
  }
  if (added && S.dash.events.length > 500) {
    S.dash.events = S.dash.events.slice(-500);
    S._eventKeys = new Set(S.dash.events.map(eventKey));
  }
  return added;
}

/* P3：SSE 实时推送客户端 */
let sseSource = null;
function connectSSE(runId) {
  const base = (S.settings && S.settings.apiBaseUrl) || '';
  if (!base) return false;
  if (sseSource) { try { sseSource.close(); } catch {} sseSource = null; }
  try {
    const url = `${base.replace(/\/$/, '')}/runs/${encodeURIComponent(runId)}/events`;
    sseSource = new EventSource(url);
    sseSource.onopen = () => { S.sseConnected = true; updateConnIndicator(); toast('info', 'SSE 已连接', runId); };
    sseSource.onerror = () => { S.sseConnected = false; updateConnIndicator(); };
    sseSource.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        // 后端 SSE 事件格式：{event, ts, data} 或 {type, ...}
        const evt = {
          event: data.event || data.type || 'unknown',
          ts: data.ts || data.occurred_at || new Date().toISOString(),
          data: data.data || data.payload || data,
        };
        handleRealtimeEvent(evt);
      } catch {}
    };
    return true;
  } catch (e) {
    console.warn('SSE connect failed', e);
    return false;
  }
}
function disconnectSSE() {
  if (sseSource) { try { sseSource.close(); } catch {} sseSource = null; }
  S.sseConnected = false;
}
function onWatchSnapshot(payload) {
  if (!payload || payload.dir !== S.watchDir) return;
  if (payload.run) S.dash.run = Object.assign(enrichRun(payload.run), { dir: S.watchDir });
  if (payload.tasks) {
    S.dash.tasks = payload.tasks.map(mapRealTask);
    S.dash.agents = deriveAgents(S.dash.tasks);
    S.dash.gates = deriveGates(S.dash.tasks);
  }
  renderDashboard();
}

/* watch 事件：ledger 增量事件 */
function onWatchLedger(payload) {
  if (!payload || payload.dir !== S.watchDir) return;
  if (appendEvents(payload.events || [])) renderEvents();
}

/* P3：SSE 实时事件处理 */
let sseReloadTimer = null;
function handleRealtimeEvent(evt) {
  if (!S.dash) return;
  appendEvents([evt]);
  renderEvents();
  // 状态变更类事件触发快照刷新（防抖 500ms）
  const stateEvents = ['task_started', 'task_finished', 'gate_result', 'run_status_changed', 'budget_warning', 'budget_exhausted'];
  if (stateEvents.includes(evt.event)) {
    if (sseReloadTimer) clearTimeout(sseReloadTimer);
    sseReloadTimer = setTimeout(async () => {
      if (S.watchDir) {
        try {
          const snap = await window.studio.loadRun(S.watchDir);
          if (snap && snap.run) {
            S.dash.run = Object.assign(enrichRun(snap.run), { dir: S.watchDir });
            if (snap.tasks) {
              S.dash.tasks = snap.tasks.map(mapRealTask);
              S.dash.agents = deriveAgents(S.dash.tasks);
              S.dash.gates = deriveGates(S.dash.tasks);
            }
            renderDashboard();
          }
        } catch {}
      }
    }, 500);
  }
}

/* P3：连接状态指示器 */
function updateConnIndicator() {
  const el = $('#conn-indicator');
  if (!el) return;
  if (S.sseConnected) {
    el.textContent = 'SSE 实时';
    el.className = 'conn-indicator live';
  } else if (S.realMode && S.watchDir) {
    el.textContent = '文件监听';
    el.className = 'conn-indicator watch';
  } else {
    el.textContent = '演示模式';
    el.className = 'conn-indicator demo';
  }
}

/* ============================================================
   运行列表与切换
   ============================================================ */

export {
  connectSSE,
  disconnectSSE,
  handleRealtimeEvent,
  onWatchLedger,
  onWatchSnapshot,
  sseReloadTimer,
  sseSource,
  updateConnIndicator,
};
