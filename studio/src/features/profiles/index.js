/**
 * 执行体档案管理页（R69 重构）。
 *
 * 定位从「只读展板」升级为「档案工作台」：
 * - 读：档案卡片 + 用量 + 角色绑定 + 配置诊断，一屏回答「有哪些、谁在用、有没有问题」；
 * - 写：新建 / 编辑 / 复制 / 删除 / 导入，全部经 `agents --save|--delete`
 *   落到 `agents.d/`，不重写 agentcrew.yaml；
 * - 导出：单份 YAML 复制、全部片段导出，保持与旧版一致的复用路径。
 *
 * 数据来源只有一条：`main.py agents --json`（与执行期同一套解析）。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';
import {
  deleteAgent, exportAgentYaml, loadAgents, loadDiagnostics, loadToolCatalog, loadUsage,
} from '../../lib/agent-binding.js';
import { filterProfiles, summarizeProfiles } from '../../lib/profile-form.js';
import { confirmAction } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

import { profileCard } from './card.js';
import { diagnosticsPanel } from './diagnostics.js';
import { openProfileEditor } from './editor.js';
import { icon } from './icons.js';
import { openImportDialog } from './import-dialog.js';
import { bindingMatrix } from './matrix.js';
import { buildEmptyHero, buildPageHead, statusBar, toolbar } from './page-chrome.js';

const state = {
  data: null, usage: {}, usageError: false,
  query: '', filter: 'all', tools: null, loading: true,
};

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  return Promise.reject(new Error('剪贴板不可用'));
}

function openEditor(profile, mode) {
  openProfileEditor({
    profile, mode,
    roles: (state.data && state.data.roles) || [],
    profiles: (state.data && state.data.profiles) || [],
    tools: state.tools || [],
    onSaved: () => refresh({ force: true }),
  });
}

function handleCopy(profile) {
  exportAgentYaml(profile.key)
    .then((yaml) => copyText(yaml))
    .then(() => toast('success', '已复制', `${profile.key} 的 YAML 片段已复制`))
    .catch((e) => toast('error', '复制失败', String(e.message || e)));
}

function handleDelete(profile) {
  confirmAction(
    `删除执行体「${profile.title || profile.key}」？`,
    `将把 ${profile.key}.yaml 移入 agents.d/.trash（可恢复）。`
    + ((profile.used_by || []).length
      ? `\n注意：它仍被 ${profile.used_by.join('、')} 绑定，删除后这些角色会在下次启动时报错。`
      : ''),
    async () => {
      try {
        const result = await deleteAgent(profile.key, { force: true });
        toast('success', '已删除', result.warning || `${profile.key} 已移入回收站`);
        await refresh({ force: true });
      } catch (e) {
        toast('error', '删除失败', String(e.message || e));
      }
    }, '删除');
}

function handleExportAll() {
  if (!window.studio || !window.studio.agentsExport) {
    toast('error', '导出失败', '当前环境不支持导出');
    return;
  }
  window.studio.agentsExport().then((res) => {
    if (!res || !res.ok) throw new Error((res && res.error) || '导出失败');
    const yaml = String(res.yaml || '').trim();
    if (!yaml) { toast('info', '没有可导出的档案', '先新建一个执行体'); return; }
    return copyText(yaml)
      .then(() => toast('success', '已复制', 'YAML 片段已复制，可粘贴进 agentcrew.yaml'));
  }).catch((e) => toast('error', '导出失败', String(e.message || e)));
}

function issueCounts(diagnostics) {
  const counts = new Map();
  for (const key of ['errors', 'warnings']) {
    for (const item of (diagnostics && diagnostics[key]) || []) {
      if (item.profile) counts.set(item.profile, (counts.get(item.profile) || 0) + 1);
    }
  }
  return counts;
}

function applyFilter(filter) {
  state.filter = filter;
  renderBody();
}

function renderList() {
  const slot = document.getElementById('pf-list');
  const countSlot = document.getElementById('pf-list-count');
  if (!slot) return;
  const profiles = (state.data && state.data.profiles) || [];
  const diagnostics = state.data && state.data.diagnostics;
  const counts = issueCounts(diagnostics);
  const visible = filterProfiles(profiles, {
    query: state.query, filter: state.filter, issues: new Set(counts.keys()),
  });
  if (countSlot) {
    countSlot.textContent = visible.length === profiles.length
      ? `（${profiles.length}）` : `（${visible.length} / ${profiles.length}）`;
  }
  slot.innerHTML = '';
  if (!visible.length) {
    slot.appendChild(h('div', { class: 'pf-empty-inline' },
      h('div', { class: 'pf-empty-title' }, state.query ? '没有匹配的档案' : '这个筛选下没有档案'),
      h('p', { class: 'pf-empty-desc' }, '换个关键词，或把筛选切回「全部」。'),
      h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: () => { state.query = ''; state.filter = 'all'; renderBody(); },
      }, '清除筛选')));
    return;
  }
  const grid = h('div', { class: 'pf-grid' });
  for (const profile of visible) {
    grid.appendChild(profileCard(profile, {
      usage: state.usage[profile.key] || null,
      usageUnavailable: state.usageError,
      roles: (state.data && state.data.roles) || [],
      issues: counts.get(profile.key) || 0,
      onEdit: () => openEditor(profile, 'edit'),
      onDuplicate: () => openEditor(profile, 'duplicate'),
      onDelete: handleDelete,
      onCopy: handleCopy,
    }));
  }
  slot.appendChild(grid);
}

function locateIssue(item) {
  if (item.profile) {
    state.query = item.profile;
    state.filter = 'all';
    renderBody();
    requestAnimationFrame(() => {
      const card = document.querySelector(`.pf-card[data-key="${item.profile}"]`);
      if (!card) { toast('info', '档案不在列表里', `${item.profile} 可能已删除或被覆盖`); return; }
      card.classList.add('is-flash');
      card.scrollIntoView({ block: 'center', behavior: 'smooth' });
      setTimeout(() => card.classList.remove('is-flash'), 1600);
    });
  } else if (item.role) {
    document.querySelector(`.pf-matrix-row[data-role="${item.role}"]`)
      ?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}

function renderBody() {
  const body = document.getElementById('pf-body');
  if (!body) return;
  const data = state.data || { profiles: [], roles: [], diagnostics: null };
  const profiles = data.profiles || [];
  const summary = summarizeProfiles(profiles, data.diagnostics);
  body.innerHTML = '';
  if (state.loading) {
    body.appendChild(h('div', { class: 'pf-loading' }, '读取执行体档案…'));
    return;
  }

  body.appendChild(statusBar({
    summary, storage: data.storage, filter: state.filter, onFilter: applyFilter,
  }));
  if (data.diagnostics) {
    body.appendChild(diagnosticsPanel(data.diagnostics, {
      onLocate: locateIssue, onReload: () => refresh({ force: true }),
    }));
  }
  if (!profiles.length) {
    body.appendChild(buildEmptyHero({
      onNew: () => openEditor(null, 'create'),
      onImport: () => openImportDialog({ onImported: () => refresh({ force: true }) }),
      onSettings: () => document.querySelector('.nav-item[data-nav="settings"]')?.click(),
    }));
    body.appendChild(bindingMatrix({
      roles: data.roles || [], profiles, onEditProfile: (item) => openEditor(item, 'edit'),
    }));
    return;
  }

  body.appendChild(toolbar({
    profiles, diagnostics: data.diagnostics,
    filter: state.filter, query: state.query,
    onFilter: applyFilter,
    onQuery: (value) => { state.query = value; renderList(); },
  }));
  body.appendChild(h('div', { class: 'pf-section-head' },
    h('h3', {}, '执行体档案',
      h('span', { id: 'pf-list-count', class: 'pf-count-inline' }, `（${profiles.length}）`)),
    h('div', { class: 'pf-section-actions' },
      h('button', {
        class: 'pf-icon-btn', type: 'button', title: '导出全部 YAML 片段',
        onclick: handleExportAll,
      }, icon('download', 15)),
      h('button', {
        class: 'pf-icon-btn', type: 'button', title: '用量口径：最近 30 个 run 的 llm_call 事件',
        onclick: () => toast('info', '用量口径',
          '按最近 30 个 run 的 llm_call 事件聚合；未绑定档案的调用计入 default，不显示在卡片上。'),
      }, icon('info', 15)))));
  body.appendChild(h('div', { id: 'pf-list' }));
  body.appendChild(h('div', { class: 'pf-section-head', style: 'margin-top:26px' },
    h('h3', {}, '角色 → 执行体绑定'),
    h('span', { class: 'pf-section-note' }, '只读；绑定编辑在设置页')));
  body.appendChild(bindingMatrix({
    roles: data.roles || [], profiles, onEditProfile: (item) => openEditor(item, 'edit'),
  }));
  renderList();
}

async function refresh({ force = false, silent = true } = {}) {
  if (!silent) state.loading = true;
  renderBody();
  try {
    state.data = await loadAgents({ force });
    state.loading = false;
    renderBody();
    loadUsage({ force }).then((usage) => {
      state.usage = (usage && usage.profiles) || {};
      state.usageError = false;
      renderList();
    }).catch(() => { state.usageError = true; renderList(); });
    if (!state.tools) {
      loadToolCatalog().then((catalog) => { state.tools = (catalog && catalog.tools) || []; });
    }
  } catch (e) {
    state.loading = false;
    state.data = { profiles: [], roles: [], diagnostics: null, storage: null };
    const body = document.getElementById('pf-body');
    if (!body) return;
    body.innerHTML = '';
    const diag = await loadDiagnostics();
    body.appendChild(h('div', { class: 'pf-error-state' },
      h('div', { class: 'pf-error-title' }, icon('warn', 18), '读取执行体档案失败'),
      h('pre', { class: 'pf-error-detail' }, String(e.message || e)),
      h('div', { class: 'pf-error-actions' },
        h('button', {
          class: 'btn', type: 'button',
          onclick: () => refresh({ force: true, silent: false }),
        }, '重新加载'),
        h('button', {
          class: 'btn', type: 'button',
          onclick: () => window.studio.agentsOpenDir()
            .then((res) => { if (res && !res.ok) throw new Error(res.error || '打开失败'); })
            .catch((e) => toast('error', '打开失败', String(e.message || e))),
        }, '打开档案目录'))));
    if (diag) body.appendChild(diagnosticsPanel(diag, { onLocate: locateIssue }));
  }
}

async function renderProfilesPage(container) {
  state.query = '';
  state.filter = 'all';
  state.loading = true;
  state.data = null;
  state.usage = {};
  const token = S.navToken;
  container.appendChild(buildPageHead({
    onRefresh: () => refresh({ force: true, silent: false }),
    onNew: () => openEditor(null, 'create'),
    onImport: () => openImportDialog({ onImported: () => refresh({ force: true }) }),
  }));
  container.appendChild(h('div', { class: 'pf-body', id: 'pf-body' },
    h('div', { class: 'pf-loading' }, '读取执行体档案…')));
  await refresh({ force: true, silent: false });
  if (S.navToken !== token) return;
}

export { renderProfilesPage };
