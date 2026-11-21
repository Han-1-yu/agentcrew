/**
 * B1：侧栏「对话」全量列表 —— 时间分组 + 置顶 + 搜索 + 状态筛选 + 归档开关。
 *
 * 数据 = listRuns() 全量 + runMeta（B3 别名/置顶/归档），本地分页
 * （S.runsLimit，每页 30）；下拉（≤50 条 + 项目筛选）保留原样，两者共用
 * 一次 IPC。重绘签名（runListSignature）不变则跳过，保住 hover 与焦点。
 */
import { RUN_STATUS_COLOR } from '../../contracts/enums.js';

import { h } from '../../lib/dom.js';
import { applyRunMeta, filterRuns, groupRunsByTime, runDisplayName, runListSignature, runMetaKey, RUN_STATUS_FILTERS } from '../../lib/run-meta.js';
import { S } from '../../lib/store.js';

import { closeRunDropdown, switchRun } from './index.js';
import { openRunItemMenu } from './item-menu.js';

const PAGE_SIZE = 30;

let toolsBound = false;
let lastSig = '';
let latestRuns = [];

function resetRender() { lastSig = ''; renderSidebarConversation(); }

/* 搜索框 / 筛选 chips / 显示更多：元素常驻，事件只绑一次 */
function bindRunTools() {
  if (toolsBound) return;
  toolsBound = true;
  const box = document.getElementById('sb-run-search');
  if (box) {
    box.value = S.runQuery || '';
    box.addEventListener('input', (e) => { S.runQuery = e.target.value; resetRender(); });
    box.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        if (box.value) { box.value = ''; S.runQuery = ''; resetRender(); }
        else box.blur();
      }
    });
  }
  const more = document.getElementById('sb-run-more');
  if (more) more.addEventListener('click', () => {
    S.runsLimit = (S.runsLimit || PAGE_SIZE) + PAGE_SIZE;
    resetRender();
  });
}

function renderRunFilters() {
  const box = document.getElementById('sb-run-filters');
  if (!box) return;
  const key = `${S.runStatusFilter || 'all'}|${S.runShowArchived ? '1' : '0'}`;
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.innerHTML = '';
  for (const f of RUN_STATUS_FILTERS) {
    box.appendChild(h('button', {
      class: `sb-task-chip${(S.runStatusFilter || 'all') === f.key ? ' on' : ''}`,
      type: 'button',
      onclick: () => { S.runStatusFilter = f.key; resetRender(); },
    }, f.label));
  }
  box.appendChild(h('button', {
    class: `sb-task-chip${S.runShowArchived ? ' on' : ''}`,
    type: 'button', title: '显示 / 隐藏已归档对话',
    onclick: () => { S.runShowArchived = !S.runShowArchived; resetRender(); },
  }, '归档'));
}

function renderItem(r) {
  const active = !!(S.dash && S.dash.run && S.dash.run.dir === r.dir);
  const color = RUN_STATUS_COLOR[r.status] || 'var(--text-ter)';
  const archived = !!r.archived;
  return h('li', {
    class: `is-click sb-run-item${active ? ' is-active' : ''}${archived ? ' is-archived' : ''}`,
    title: `${runDisplayName(r)}\n${r.runId || ''} · ${r.status || 'unknown'}`,
    onclick: () => { closeRunDropdown(); switchRun(r.dir); },
  },
    h('span', { class: 'sb-li-dot sb-run-dot', style: `background:${color}` }),
    h('span', { class: 'sb-li-text' }, runDisplayName(r)),
    r.pinned ? h('span', { class: 'sb-run-pin', title: '已置顶' }, '置顶') : null,
    h('button', {
      class: 'sb-run-menu', type: 'button', title: '会话操作', 'aria-label': '会话操作',
      onclick: (e) => { e.stopPropagation(); openRunItemMenu(r); },
    }, '⋯'));
}

/** loadRunList 每次拉到全量 runs 后调用；签名为空或变化才真正重绘。 */
function renderSidebarConversation(runs) {
  const list = document.getElementById('sb-recents');
  if (!list) return;
  if (runs) latestRuns = runs;
  bindRunTools();
  renderRunFilters();

  const state = {
    query: S.runQuery || '',
    status: S.runStatusFilter || 'all',
    showArchived: !!S.runShowArchived,
  };
  const visible = filterRuns(applyRunMeta(latestRuns, S.settings && S.settings.runMeta), state);
  const limit = S.runsLimit || PAGE_SIZE;
  const shown = visible.slice(0, limit);
  const sig = runListSignature(shown, state) + '#' + String((S.dash && S.dash.run && S.dash.run.dir) || '');
  if (sig === lastSig) return;
  lastSig = sig;

  list.innerHTML = '';
  const more = document.getElementById('sb-run-more');
  if (more) {
    more.hidden = visible.length <= limit;
    const rest = visible.length - shown.length;
    if (rest > 0) more.textContent = `显示更多（${rest}）`;
  }
  if (!shown.length) {
    list.appendChild(h('li', {}, h('span', { class: 'sb-li-text' },
      state.query || state.status !== 'all' ? '没有匹配的对话' : '暂无对话记录')));
    return;
  }
  for (const g of groupRunsByTime(shown)) {
    list.appendChild(h('li', { class: 'sb-run-group' },
      h('span', { class: 'sb-li-text' }, `${g.label} · ${g.runs.length}`)));
    for (const r of g.runs) list.appendChild(renderItem(r));
  }
}

/** runMeta 变化后由菜单层调用：签名必然变化，强制重绘。 */
function refreshSidebarRuns() {
  lastSig = '';
  renderSidebarConversation();
}

export { refreshSidebarRuns, renderSidebarConversation, runMetaKey };
