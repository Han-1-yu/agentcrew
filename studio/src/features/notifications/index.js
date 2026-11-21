/**
 * P1：通知中心。聚合实时通道里的重要事件 → 面板列表 + 未读角标 +
 * 点击跳转 + 系统通知（窗口失焦时）。
 *
 * 事件源：app/realtime.js 的 appendEvents（SSE 与文件 watch 的唯一收口），
 * 只收**实时增量**——loadRealRun 的历史 ledger 回放不触发通知。
 * 去重键：watchDir + event + ts + 业务 id（方案 §P1）。
 */
import { RUN_META } from '../../contracts/enums.js';
import { h } from '../../lib/dom.js';
import { fmtBudgetUsage } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { closeModal, showModal } from '../../ui/modal.js';
import { switchNav } from '../../app/router.js';
import { toast } from '../../ui/toast.js';

const INTERESTING = new Set([
  'approval_requested', 'approval_decided', 'run_paused',
  'budget_warning', 'budget_exhausted',
  'run_finished', 'run_cancelled', 'run_failed', 'error',
]);

const MAX_ITEMS = 100;
const STORE_KEY = 'agentcrew:notif-system';

function systemEnabled() {
  try { return localStorage.getItem(STORE_KEY) !== '0'; } catch { return true; }
}

function setSystemEnabled(on) {
  try { localStorage.setItem(STORE_KEY, on ? '1' : '0'); } catch { /* 忽略 */ }
}

/** 事件 → 展示与跳转；未知形状退化为通用文案。 */
function describe(e) {
  const d = (e && e.data) || {};
  const runId = String(d.run_id || S.watchDir || '').split(/[\\/]/).filter(Boolean).pop() || '';
  const goal = String((S.dash && S.dash.run && S.dash.run.project_goal) || '').slice(0, 40);
  const suffix = goal ? `：${goal}` : '';
  switch (e.event) {
    case 'approval_requested':
      return { title: '等待人工审批', body: `run ${runId}${suffix}`, nav: 'gates' };
    case 'approval_decided':
      return { title: `审批已裁决（${d.action || '—'}）`, body: `run ${runId}${suffix}`, nav: 'gates' };
    case 'run_paused':
      return { title: '运行挂起（审批/断点）', body: `run ${runId}${suffix}`, nav: 'gates' };
    case 'budget_warning': {
      const pct = Math.round((Number(d.pct) || Number(d.threshold) || 0.9) * 100);
      return { title: `预算预警（已用 ${pct}%）`, body: `run ${runId} · ${fmtBudgetUsage(d)}${suffix}`, nav: 'dashboard' };
    }
    case 'budget_exhausted':
      return { title: '预算耗尽，运行优雅收尾', body: `run ${runId} · ${fmtBudgetUsage(d, true)}${suffix}`, nav: 'dashboard' };
    case 'run_finished': {
      const rm = RUN_META[d.status] || RUN_META.idle;
      return { title: `运行结束（${rm.label}）`, body: `run ${runId}${suffix}`, nav: 'conversation' };
    }
    case 'run_cancelled':
      return { title: '运行已取消', body: `run ${runId}${suffix}`, nav: 'dashboard' };
    case 'run_failed':
      return { title: '运行失败', body: `run ${runId}${suffix}`, nav: 'dashboard' };
    case 'error':
      return { title: '运行出错', body: String(d.message || d.exc_type || `run ${runId}`).slice(0, 120), nav: 'events' };
    default:
      return null;
  }
}

function eventKey(e) {
  const d = (e && e.data) || {};
  return [S.watchDir || '', e.event || '', e.ts || '', d.id || d.approval_id || ''].join('|');
}

function unreadCount() {
  return (S.notifs || []).filter((n) => !n.read).length;
}

function updateBadge() {
  const dot = document.getElementById('notify-dot');
  if (dot) dot.classList.toggle('hidden', unreadCount() === 0);
}

/** 窗口失焦时发系统通知（HTML5 Notification 在 Electron 里走 OS 通道）。 */
function systemNotify(items) {
  if (!systemEnabled() || !items.length) return;
  if (typeof document !== 'undefined' && document.hasFocus && document.hasFocus()) return;
  try {
    const n = new Notification(`AgentCrew Studio · ${items[0].title}`, {
      body: items.length === 1 ? String(items[0].body || '')
        : `${items[0].title} 等 ${items.length} 条新通知`,
      tag: 'agentcrew-studio',
    });
    n.onclick = () => { try { window.focus(); } catch { /* 忽略 */ } };
  } catch { /* 平台不支持：静默降级为角标 */ }
}

/** realtime.appendEvents 的增量钩子：只处理重要事件，静默去重。 */
function notifyFromEvents(events) {
  for (const e of events || []) {
    if (!e || !INTERESTING.has(String(e.event))) continue;
    const key = eventKey(e);
    if ((S.notifs || []).some((n) => n.key === key)) continue;
    const info = describe(e);
    if (!info) continue;
    const item = {
      key,
      ts: String(e.ts || ''),
      event: String(e.event),
      title: info.title,
      body: info.body,
      nav: info.nav,
      read: false,
    };
    S.notifs = [item, ...(S.notifs || [])].slice(0, MAX_ITEMS);
    updateBadge();
    // D2：预算类事件除进面板外再弹一次 toast。去重已在上面按 key 完成，
    // 后端 `_maybe_emit_budget_warning` 也保证整场只发一条，不会重复弹。
    if (e.event === 'budget_warning' || e.event === 'budget_exhausted') {
      toast(e.event === 'budget_warning' ? 'warn' : 'error', info.title, info.body, 8000);
    }
    systemNotify([item]);
  }
}

function markAllRead() {
  for (const n of S.notifs || []) n.read = true;
  updateBadge();
}

function openNotificationPanel() {
  const items = S.notifs || [];
  const list = h('div', { class: 'notif-list' });
  if (!items.length) {
    list.appendChild(h('div', { class: 'notif-empty' }, '暂无通知。审批请求、预算预警、运行结束等会出现在这里。'));
  }
  for (const n of items) {
    list.appendChild(h('button', {
      class: `notif-item${n.read ? '' : ' unread'}`, type: 'button',
      onclick: () => {
        n.read = true;
        updateBadge();
        closeModal();
        switchNav(n.nav || 'dashboard');
      },
    },
      h('span', { class: 'notif-item-main' },
        h('span', { class: 'notif-item-title' }, n.title),
        h('span', { class: 'notif-item-body' }, n.body || ''),
        h('span', { class: 'notif-item-ts' }, n.ts ? String(n.ts).replace('T', ' ').slice(0, 19) : '')),
      n.read ? null : h('span', { class: 'notif-item-dot' })));
  }
  const sysToggle = h('button', {
    class: 'btn btn-sm', type: 'button',
    title: '窗口失焦时是否发系统通知',
    onclick: () => {
      const next = !systemEnabled();
      setSystemEnabled(next);
      sysToggle.textContent = `系统通知：${next ? '开' : '关'}`;
      toast('info', '已更新', `窗口失焦时系统通知${next ? '开启' : '关闭'}`);
    },
  }, `系统通知：${systemEnabled() ? '开' : '关'}`);

  showModal(h('div', {},
    h('h3', { class: 'modal-title' }, '通知中心'),
    list,
    h('div', { class: 'modal-actions' },
      sysToggle,
      h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: () => { markAllRead(); closeModal(); },
      }, '全部已读'),
      h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: () => { S.notifs = []; updateBadge(); closeModal(); },
      }, '清空'))));
}

export { notifyFromEvents, openNotificationPanel, updateBadge };
