/**
 * 事件日志页：类型筛选 + 展开详情。
 */
import { $, h } from '../../lib/dom.js';
import { fmtClock } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { switchNav } from '../../app/router.js';

import { exportEvents } from './export.js';
function renderEventsPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '事件日志'),
    h('div', { style: 'display:flex;gap:8px;align-items:center' },
      h('input', { id: 'ev-search', type: 'text', placeholder: '搜索事件…',
        style: 'width:200px;padding:6px 10px;border:1px solid var(--border-input);border-radius:6px;font-size:12px;font-family:inherit',
        oninput: () => renderEventsList() }),
      h('button', { class: 'btn', onclick: () => exportEvents() }, '导出 JSON'),
      h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回'))));

  const filterBar = h('div', { class: 'ev-filter-bar', id: 'ev-filter-bar' });
  container.appendChild(filterBar);

  const listEl = h('div', { class: 'ev-full-list', id: 'ev-full-list' });
  container.appendChild(listEl);

  // 构建类型筛选
  const allTypes = [...new Set((S.dash.events || []).map(e => e.event))].sort();
  S.eventFilter = new Set(allTypes);
  for (const t of allTypes) {
    const btn = h('button', { class: 'ev-filter-btn active', 'data-type': t,
      onclick: () => {
        btn.classList.toggle('active');
        if (S.eventFilter.has(t)) S.eventFilter.delete(t); else S.eventFilter.add(t);
        renderEventsList();
      }}, t);
    filterBar.appendChild(btn);
  }

  renderEventsList();
}

function renderEventsList() {
  const listEl = $('#ev-full-list');
  if (!listEl) return;
  const q = ($('#ev-search')?.value || '').toLowerCase();
  const evs = [...(S.dash.events || [])].reverse()
    .filter(e => S.eventFilter.has(e.event))
    .filter(e => !q || JSON.stringify(e).toLowerCase().includes(q));
  listEl.innerHTML = '';
  if (!evs.length) {
    listEl.appendChild(h('div', { class: 'ph-empty' }, '无匹配事件'));
    return;
  }
  const PAGE_SIZE = 100;
  const totalPages = Math.ceil(evs.length / PAGE_SIZE);
  if (!S.evPage || S.evPage > totalPages) S.evPage = 1;
  const start = (S.evPage - 1) * PAGE_SIZE;
  const pageEvs = evs.slice(start, start + PAGE_SIZE);
  for (const e of pageEvs) {
    const item = h('div', { class: 'ev-full-item' },
      h('div', { class: 'ev-full-main', onclick: () => item.classList.toggle('expanded') },
        h('span', { class: 'ev-full-ts' }, fmtClock(e.ts)),
        h('span', { class: 'ev-full-type' }, e.event),
        h('span', { class: 'ev-full-task' }, e.data?.task_id || ''),
        h('span', { class: 'ev-full-role' }, e.data?.role || ''),
        h('span', { class: 'ev-full-summary' }, summarizeEvent(e))));
    const detail = h('pre', { class: 'ev-full-detail' }, JSON.stringify(e, null, 2));
    item.appendChild(detail);
    listEl.appendChild(item);
  }
  // 分页控件
  if (totalPages > 1) {
    const pager = h('div', { class: 'ev-pager' },
      h('button', { class: 'btn btn-sm', disabled: S.evPage <= 1, onclick: () => { S.evPage--; renderEventsList(); } }, '上一页'),
      h('span', { class: 'ev-pager-info' }, `第 ${S.evPage} / ${totalPages} 页 · 共 ${evs.length} 条`),
      h('button', { class: 'btn btn-sm', disabled: S.evPage >= totalPages, onclick: () => { S.evPage++; renderEventsList(); } }, '下一页'));
    listEl.appendChild(pager);
  }
}

function summarizeEvent(e) {
  const d = e.data || {};
  if (e.event === 'task_started') return `${d.task_id || ''} 开始执行（第 ${d.attempt || 1} 次）`;
  if (e.event === 'task_succeeded') return `${d.task_id || ''} 完成`;
  if (e.event === 'task_failed') return `${d.task_id || ''} 失败：${d.error || ''}`;
  if (e.event === 'gate_result') return `${d.layer || ''} ${d.verdict || ''}`;
  if (e.event === 'run_started') return `目标：${d.goal || ''}`;
  if (e.event === 'run_finished') return `状态：${d.status || ''}`;
  if (e.event === 'llm_call') return `${d.role || ''} · ${d.model || ''} · ${d.tokens || 0} tok`;
  if (e.event === 'llm_failover') return `${d.from_alias || ''} → ${d.to_alias || ''}`;
  if (e.event === 'budget_exhausted') return `已用 $${d.used || 0} / 上限 $${d.limit || 0}`;
  return d.summary || Object.keys(d).join(', ');
}

/* ============================================================
   计划审查页面（任务编辑 + 批准流程）
   ============================================================ */

export { renderEventsList, renderEventsPage, summarizeEvent };
