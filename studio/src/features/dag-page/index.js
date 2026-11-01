/**
 * 任务图页面：DAG 全屏视图与节点详情。
 */
import { STATUS_META, ROLE_META } from '../../contracts/enums.js';

import { $, $$, h } from '../../lib/dom.js';
import { trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { ev } from '../../app/mock.js';
import { switchNav } from '../../app/router.js';

import { computeLayers } from '../dashboard/dag.js';
import { openTaskDetail } from '../detail/panel.js';
function renderDagPage(container) {
  S.taskFilter = S.taskFilter || { q: '', status: 'all' };
  const renderList = () => {
    wrap.innerHTML = '';
    const allTasks = S.dash.tasks || [];
    const q = (S.taskFilter.q || '').toLowerCase();
    const filtered = allTasks.filter(t => {
      if (S.taskFilter.status !== 'all' && t.status !== S.taskFilter.status) return false;
      if (!q) return true;
      return (t.id || '').toLowerCase().includes(q) ||
             (t.title || '').toLowerCase().includes(q) ||
             (t.role || '').toLowerCase().includes(q) ||
             (t.description || '').toLowerCase().includes(q);
    });
    if (!filtered.length) { wrap.appendChild(h('div', { class: 'ph-empty' }, '无匹配任务')); return; }
    const { layers } = computeLayers(filtered);
    layers.forEach((ids, li) => {
      const layerTasks = ids.map(id => filtered.find(t => t.id === id)).filter(Boolean);
      const layerEl = h('div', { class: 'dag-layer' },
        h('div', { class: 'dag-layer-head' }, `第 ${li + 1} 层 · ${layerTasks.length} 个任务`));
      const listEl = h('div', { class: 'dag-layer-tasks' });
      for (const t of layerTasks) {
        const meta = ROLE_META[t.role] || { label: t.role, color: 'var(--text-ter)' };
        listEl.appendChild(h('div', { class: 'dag-task-row', onclick: () => openTaskDetail(t.id) },
          h('span', { class: `ts-dot ts-${t.status}` }),
          h('span', { class: 'dag-task-id' }, t.id),
          h('span', { class: 'dag-task-title' }, trunc(t.title, 40)),
          h('span', { class: 'dag-task-role', style: `color:${meta.color}` }, meta.label || t.role),
          h('span', { class: 'dag-task-deps' }, (t.dependencies || []).length ? `← ${t.dependencies.join(', ')}` : '—'),
          h('span', { class: `dag-task-status status-${t.status}` }, STATUS_META[t.status]?.label || t.status)));
      }
      layerEl.appendChild(listEl);
      wrap.appendChild(layerEl);
    });
  };

  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '任务依赖图'),
    h('div', { style: 'display:flex;gap:8px;align-items:center' },
      h('input', { type: 'text', placeholder: '搜索任务…', value: S.taskFilter.q,
        style: 'width:180px;padding:6px 10px;border:1px solid var(--border-input);border-radius:6px;font-size:12px;font-family:inherit',
        oninput: (e) => { S.taskFilter.q = e.target.value; renderList(); } }),
      h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回'))));

  // 状态筛选 chips
  const statuses = ['all', 'pending', 'ready', 'running', 'blocked', 'review', 'succeeded', 'failed', 'skipped'];
  const statusLabels = { all: '全部', ...Object.fromEntries(statuses.filter(x => x !== 'all').map(x => [x, STATUS_META[x]?.label || x])) };
  const filterBar = h('div', { class: 'ev-filter-bar', style: 'margin-bottom:12px' });
  statuses.forEach(st => {
    filterBar.appendChild(h('button', { class: 'ev-filter-btn' + (S.taskFilter.status === st ? ' active' : ''),
      onclick: (ev) => { S.taskFilter.status = st; $$('#placeholder-view .ev-filter-btn').forEach(b => b.classList.remove('active')); filterBar.querySelectorAll('.ev-filter-btn').forEach(b => b.classList.remove('active')); ev.currentTarget.classList.add('active'); renderList(); } },
      statusLabels[st] || st));
  });
  container.appendChild(filterBar);

  const wrap = h('div', { class: 'dag-full-wrap' });
  container.appendChild(wrap);
  renderList();
}

/* ============================================================
   Agents 全屏页
   ============================================================ */

export { renderDagPage };
