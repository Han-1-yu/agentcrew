/**
 * 实时流式正文面板：批量消费 SSE 的 `agent_stream_delta`。
 * 高频 delta 先进内存队列，再由 requestAnimationFrame 合并提交，避免逐 token
 * 触发布局；离开看板时只累积不绘制，切回后一次性补齐。
 */
import { $, h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

const MAX_CHARS = 120000;
let lastTaskId = '';
let charCount = 0;
let pending = [];
let pendingChars = 0;
let flushFrame = 0;
let flushTimer = null;

function panel() { return $('#stream-section'); }
function output() { return $('#stream-output'); }

function trim(out) {
  while (charCount > MAX_CHARS && out.firstChild) {
    const node = out.firstChild;
    charCount -= (node.textContent || '').length;
    out.removeChild(node);
  }
}

function flushPending() {
  flushFrame = 0;
  if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
  if (!pending.length) return;
  const out = output();
  const sec = panel();
  if (!out || !sec) return;

  const wasNearBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 120;
  for (const data of pending) {
    let delta = String(data.delta == null ? '' : data.delta);
    if (!delta) continue;
    const room = MAX_CHARS - 2000;
    if (delta.length > room) delta = delta.slice(-room);
    sec.classList.remove('hidden');
    const taskId = String(data.task_id || '');
    if (taskId && taskId !== lastTaskId) {
      lastTaskId = taskId;
      const label = `── ${taskId}${data.role ? ' · ' + String(data.role) : ''} ──`;
      out.appendChild(h('div', { class: 'stream-task-head' }, label));
      charCount += label.length;
    }
    const last = out.lastChild;
    if (last && last.nodeType === 3) last.appendData(delta);
    else out.appendChild(document.createTextNode(delta));
    charCount += delta.length;
  }
  pending = [];
  pendingChars = 0;
  trim(out);
  if (wasNearBottom) out.scrollTop = out.scrollHeight;
  const live = $('#stream-live');
  if (live) live.textContent = '接收中';
}

function scheduleFlush() {
  if (S.nav !== 'dashboard' || document.hidden) return;
  if (flushFrame || flushTimer) return;
  if (typeof requestAnimationFrame === 'function') {
    flushFrame = requestAnimationFrame(flushPending);
  } else {
    flushTimer = setTimeout(flushPending, 50);
  }
}

function bindStreamPanel() {
  const clear = $('#stream-clear');
  if (clear && !clear.dataset.bound) {
    clear.dataset.bound = '1';
    clear.addEventListener('click', () => resetStreamPanel());
  }
  if (!bindStreamPanel.bound) {
    bindStreamPanel.bound = true;
    window.addEventListener('studio:dashboard-show', scheduleFlush);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) scheduleFlush(); });
  }
}

function resetStreamPanel() {
  if (flushFrame) { cancelAnimationFrame(flushFrame); flushFrame = 0; }
  if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
  pending = [];
  pendingChars = 0;
  const out = output();
  if (out) out.replaceChildren();
  const sec = panel();
  if (sec) sec.classList.add('hidden');
  const live = $('#stream-live');
  if (live) live.textContent = '待接收';
  lastTaskId = '';
  charCount = 0;
}

function appendStreamDelta(data) {
  const delta = String(data && data.delta == null ? '' : data.delta);
  if (!delta) return;
  pending.push(data);
  pendingChars += delta.length;
  // 页面长期不可见时也要有上限；保留最新一段，回看板后仍能接上下文。
  while (pendingChars > MAX_CHARS && pending.length > 1) {
    pendingChars -= String(pending[0].delta || '').length;
    pending.shift();
  }
  scheduleFlush();
}

export { appendStreamDelta, bindStreamPanel, resetStreamPanel };
