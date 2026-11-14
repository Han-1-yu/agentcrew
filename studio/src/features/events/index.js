/* 事件日志页：类型筛选、可恢复展开态、分页与实时增量刷新。 */
import { $, $$, h } from '../../lib/dom.js';
import { fmtClock } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { emptyState } from '../../ui/states.js';

import { switchNav } from '../../app/router.js';

import { renderAgentAudit } from './agent-audit.js';
import { exportEvents } from './export.js';

function allEventTypes() {
  return [...new Set((S.dash.events || []).map(e => e.event))].sort();
}

function syncEventFilters() {
  const types = allEventTypes();
  if (!(S.eventFilter instanceof Set)) S.eventFilter = new Set();
  if (!S._eventTypeSeen) S._eventTypeSeen = new Set();
  for (const type of types) {
    if (!S._eventTypeSeen.has(type)) {
      S._eventTypeSeen.add(type);
      S.eventFilter.add(type);
    }
  }
  return types;
}

function renderFilterButtons(types) {
  const filterBar = $('#ev-filter-bar');
  if (!filterBar) return;
  const sig = types.join('|');
  if (filterBar.dataset.types === sig) return;
  filterBar.dataset.types = sig;
  filterBar.innerHTML = '';
  for (const type of types) {
    const btn = h('button', {
      class: 'ev-filter-btn' + (S.eventFilter.has(type) ? ' active' : ''),
      type: 'button', 'aria-pressed': S.eventFilter.has(type) ? 'true' : 'false', 'data-type': type,
      onclick: () => {
        btn.classList.toggle('active');
        const active = btn.classList.contains('active');
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
        if (active) S.eventFilter.add(type); else S.eventFilter.delete(type);
        S.evPage = 1;
        renderEventsList();
      },
    }, type);
    filterBar.appendChild(btn);
  }
}

function eventDomKey(e) {
  return `${e.ts || ''}|${e.event || ''}|${JSON.stringify(e.data || {})}`;
}

function toggleEventDetail(item, e) {
  const key = eventDomKey(e);
  if (!(S.eventExpanded instanceof Set)) S.eventExpanded = new Set();
  const expanded = !item.classList.contains('expanded');
  item.classList.toggle('expanded', expanded);
  if (expanded) S.eventExpanded.add(key); else S.eventExpanded.delete(key);
  let detail = item.querySelector('.ev-full-detail');
  if (expanded && !detail) item.appendChild(h('pre', { class: 'ev-full-detail' }, JSON.stringify(e, null, 2)));
  if (!expanded && detail) detail.remove();
  return expanded;
}

function renderEventsPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '事件日志'),
    h('div', { style: 'display:flex;gap:8px;align-items:center' },
      h('input', { id: 'ev-search', type: 'text', placeholder: '搜索事件…', value: S.eventSearch || '',
        style: 'width:200px;padding:6px 10px;border:1px solid var(--border-input);border-radius:6px;font-size:12px;font-family:inherit',
        oninput: ev => { S.eventSearch = ev.target.value; S.evPage = 1; renderEventsList(); } }),
      h('button', { class: 'btn', onclick: () => exportEvents() }, '导出 JSON'),
      h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回'))));

  const audit = renderAgentAudit(S.dash.events || []);
  if (audit) container.appendChild(audit);
  container.appendChild(h('div', { class: 'ev-filter-bar', id: 'ev-filter-bar' }));
  container.appendChild(h('div', { class: 'ev-full-list', id: 'ev-full-list' }));

  const types = syncEventFilters();
  renderFilterButtons(types);
  renderEventsList();
}

function renderEventsList() {
  const listEl = $('#ev-full-list');
  if (!listEl) return;
  const q = String(S.eventSearch || '').toLowerCase();
  const evs = [...(S.dash.events || [])].reverse()
    .filter(e => S.eventFilter.has(e.event))
    .filter(e => !q || JSON.stringify(e).toLowerCase().includes(q));
  listEl.innerHTML = '';
  if (!evs.length) {
    const all = S.dash.events || [];
    listEl.appendChild(!all.length
      ? emptyState('暂无事件', '本次运行还没有产生事件记录。')
      : emptyState('无匹配事件', '当前搜索词或类型筛选没有命中任何事件。',
        { label: '重置筛选', onclick: () => {
          S.eventSearch = '';
          const search = $('#ev-search'); if (search) search.value = '';
          S.eventFilter = new Set(allEventTypes());
          S.evPage = 1;
          $$('.ev-filter-btn').forEach(b => {
            b.classList.add('active');
            b.setAttribute('aria-pressed', 'true');
          });
          renderEventsList();
        } }));
    return;
  }
  const PAGE_SIZE = 100;
  const totalPages = Math.ceil(evs.length / PAGE_SIZE);
  if (!S.evPage || S.evPage > totalPages) S.evPage = 1;
  const pageEvs = evs.slice((S.evPage - 1) * PAGE_SIZE, S.evPage * PAGE_SIZE);
  if (!(S.eventExpanded instanceof Set)) S.eventExpanded = new Set();
  for (const e of pageEvs) {
    const key = eventDomKey(e);
    const expanded = S.eventExpanded.has(key);
    const item = h('div', { class: `ev-full-item${expanded ? ' expanded' : ''}` },
      h('button', { class: 'ev-full-main', type: 'button', 'aria-expanded': expanded ? 'true' : 'false',
        onclick: () => {
          const nowExpanded = toggleEventDetail(item, e);
          item.querySelector('.ev-full-main').setAttribute('aria-expanded', nowExpanded ? 'true' : 'false');
        } },
        h('span', { class: 'ev-full-ts' }, fmtClock(e.ts)),
        h('span', { class: 'ev-full-type' }, e.event),
        h('span', { class: 'ev-full-task' }, e.data?.task_id || ''),
        h('span', { class: 'ev-full-role' }, e.data?.role || ''),
        h('span', { class: 'ev-full-summary' }, summarizeEvent(e))));
    if (expanded) item.appendChild(h('pre', { class: 'ev-full-detail' }, JSON.stringify(e, null, 2)));
    listEl.appendChild(item);
  }
  if (totalPages > 1) {
    listEl.appendChild(h('div', { class: 'ev-pager' },
      h('button', { class: 'btn btn-sm', type: 'button', disabled: S.evPage <= 1, onclick: () => { S.evPage--; renderEventsList(); } }, '上一页'),
      h('span', { class: 'ev-pager-info' }, `第 ${S.evPage} / ${totalPages} 页 · 共 ${evs.length} 条`),
      h('button', { class: 'btn btn-sm', type: 'button', disabled: S.evPage >= totalPages, onclick: () => { S.evPage++; renderEventsList(); } }, '下一页')));
  }
}

function refreshEventsView() {
  if (S.nav !== 'events') return;
  renderFilterButtons(syncEventFilters());
  renderEventsList();
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
  if (e.event === 'agent_switched') {
    const from = d.from_profile ? `${d.from_profile} → ` : '→ ';
    return `${d.role || ''}：${from}${d.to || ''}（${d.duty || 'primary'}）${d.reason ? ` · ${d.reason}` : ''}`;
  }
  if (e.event === 'agent_binding_reset') return `${d.role || ''}：${d.from_profile ? `${d.from_profile} → ` : ''}恢复默认绑定`;
  if (e.event === 'agent_degraded') return `${d.role || ''} 复核降级：${d.reason || ''}`;
  return d.summary || Object.keys(d).join(', ');
}

export { refreshEventsView, renderEventsList, renderEventsPage, summarizeEvent };
