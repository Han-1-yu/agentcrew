/**
 * 运行列表与切换：下拉分组、加载快照、切换当前 run。
 * 侧栏「对话」全量列表（B1）见 ./sidebar.js，两者共用一次 listRuns IPC。
 */
import { RUN_STATUS_COLOR } from '../../contracts/enums.js';

import { $, h } from '../../lib/dom.js';
import { fmtDateTime, simplifyGoal } from '../../lib/format.js';
import { runMetaKey } from '../../lib/run-meta.js';
import { S, persistState } from '../../lib/store.js';

import { emptyState, errorState, loadingState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';

import { loadRealRun } from '../../app/data.js';
import { initMockData } from '../../app/mock.js';
import { resetRealtime, updateConnIndicator } from '../../app/realtime.js';

import { renderDashboard } from '../dashboard/index.js';
import { openCompareModal } from './compare.js';
import { pruneRunMetaAfterDelete } from './meta-store.js';
import { renderSidebarConversation } from './sidebar.js';
async function loadRunList() {
  const listEl = $('#run-dropdown-list');
  if (!listEl) return;
  listEl.innerHTML = '';
  listEl.appendChild(loadingState('正在读取运行列表…'));
  try {
    const runs = await window.studio.listRuns();
    if (!runs || !runs.length) {
      listEl.innerHTML = '';
      listEl.appendChild(emptyState('暂无运行记录', '点击右上角「新建运行」开始第一次协作。'));
      return;
    }
    listEl.innerHTML = '';
    // 按项目分组
    const groups = {};
    for (const r of runs.slice(0, 50)) {
      const g = r.group || '未分组';
      if (!groups[g]) groups[g] = [];
      groups[g].push(r);
    }
    // 项目筛选器
    const groupNames = Object.keys(groups);
    if (groupNames.length > 1) {
      const filterBar = h('div', { class: 'rd-filter' },
        h('span', { class: 'rd-filter-label' }, '项目：'),
        ...groupNames.map(g => h('button', {
          class: 'rd-filter-chip' + (S.runFilter === g ? ' on' : ''),
          onclick: () => { S.runFilter = (S.runFilter === g ? null : g); loadRunList(); }
        }, g)));
      listEl.appendChild(filterBar);
    }
    for (const [gName, gRuns] of Object.entries(groups)) {
      if (S.runFilter && S.runFilter !== gName) continue;
      if (groupNames.length > 1) {
        listEl.appendChild(h('div', { class: 'rd-group-header' }, gName));
      }
      for (const r of gRuns) {
        const color = RUN_STATUS_COLOR[r.status] || 'var(--text-ter)';
        const isActive = S.dash.run && S.dash.run.dir === r.dir;
        const canDelete = r.status !== 'running';
        const item = h('div', { class: 'rd-item' + (isActive ? ' active' : '') },
          h('span', { class: 'rd-dot', style: `background:${color}`, onclick: () => { closeRunDropdown(); switchRun(r.dir); } }),
          h('div', { class: 'rd-item-info', onclick: () => { closeRunDropdown(); switchRun(r.dir); } },
            h('div', { class: 'rd-item-title', title: r.goal || r.runId }, simplifyGoal(r.goal || r.runId || '未命名')),
            h('div', { class: 'rd-item-meta' }, `${r.runId || ''} · ${r.status || 'unknown'} · ${fmtDateTime(r.createdAt || r.updatedAt)}`)),
          h('button', { class: 'rd-del', title: canDelete ? '删除运行' : '运行中无法删除',
            disabled: !canDelete,
            onclick: async (e) => {
              e.stopPropagation();
              if (!confirm(`确认删除运行「${simplifyGoal(r.goal || r.runId)}」？\n此操作不可恢复。`)) return;
              await deleteRunItem(r);
            } },
            h('svg', { width: 13, height: 13, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
              h('polyline', { points: '3 6 5 6 21 6' }),
              h('path', { d: 'M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2' }))),
          !isActive ? h('button', { class: 'rd-cmp', title: '与当前运行对比',
            onclick: (e) => { e.stopPropagation(); closeRunDropdown(); openCompareModal(r); } },
            h('svg', { width: 13, height: 13, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
              h('path', { d: 'M14 3v4a1 1 0 0 0 1 1h4' }),
              h('path', { d: 'M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z' }),
              h('path', { d: 'M9 13h6M9 17h6' }))) : null);
        listEl.appendChild(item);
      }
    }
    renderSidebarRecents(runs);
  } catch (e) {
    listEl.innerHTML = '';
    listEl.appendChild(errorState('运行列表加载失败', String(e.message || e), loadRunList));
  }
}

/**
 * B3+B2：删除运行（下拉与 ••• 菜单共用）。删除成功后裁掉 runMeta 幽灵键；
 * 删的是当前 run 时按既有兜底链路切走。
 */
async function deleteRunItem(r) {
  const isActive = !!(S.dash.run && S.dash.run.dir === r.dir);
  try {
    await window.studio.deleteRun(r.dir);
    await pruneRunMetaAfterDelete([runMetaKey(r)]);
    toast('success', '已删除', r.runId || '');
    if (isActive) {
      const remaining = await window.studio.listRuns();
      if (remaining && remaining.length) await switchRun(remaining[0].dir);
      else {
        // 删掉最后一个 run：resetRealtime 会停掉 watcher 与 SSE，
        // 否则 500ms 轮询会持续扫已删除目录、指示器也停在「文件监听」。
        resetRealtime();
        S.watchDir = null; S._eventKeys = null; S.realMode = false;
        initMockData();
        renderDashboard();
        updateConnIndicator();
      }
    }
    loadRunList();
  } catch (err) { toast('error', '删除失败', String(err.message || err)); }
}

/**
 * 侧栏「对话」列表（B1）：升级为全量会话列表（时间分组 + 置顶 + 搜索 +
 * 状态筛选 + 归档 + 本地分页），实现见 ./sidebar.js；与运行下拉共用
 * 同一份 listRuns IPC 结果，点击 = switchRun（同一动作，不新增数据通道）。
 */
function renderSidebarRecents(runs) {
  renderSidebarConversation(runs);
}

function toggleRunDropdown() {
  const dd = $('#run-dropdown');
  if (!dd) return;
  if (dd.classList.contains('hidden')) {
    loadRunList();
    dd.classList.remove('hidden');
  } else {
    dd.classList.add('hidden');
  }
}
function closeRunDropdown() {
  const dd = $('#run-dropdown');
  if (dd) dd.classList.add('hidden');
}

async function switchRun(dir) {
  // 新对话态随切 run 退出（A1）：显示的就是被选中的会话
  S.newChat = false;
  // 停止旧 watch
  if (S.watchDir) {
    try { await window.studio.unwatchRun(S.watchDir); } catch {}
  }
  // 显示加载态
  const overlay = h('div', { class: 'loading-overlay', id: 'loading-overlay' },
    h('div', { class: 'loading-spinner' }),
    h('div', { class: 'loading-text' }, '加载运行数据…'));
  document.body.appendChild(overlay);
  try {
    const ok = await loadRealRun(dir);
    if (ok) {
      renderDashboard();
      persistState();
      loadRunList();   // 刷新侧栏 Recents 的当前项高亮
      toast('info', '已切换', simplifyGoal(S.dash.run.project_goal));
    } else {
      toast('error', '切换失败', '无法加载该运行数据');
    }
  } finally {
    overlay.remove();
  }
}

/* ============================================================
   主题（light / dark / system）
   ============================================================ */

export { closeRunDropdown, deleteRunItem, loadRunList, switchRun, toggleRunDropdown };
