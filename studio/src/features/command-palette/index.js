/**
 * B4：Ctrl/Cmd+K 命令面板。搜三类目标：
 *  - 页面跳转（14 页，含流程库与设置）；
 *  - 运行切换（listRuns + runMeta 别名，回车切换）；
 *  - 当前 run 的任务（回车打开任务详情）。
 * 键盘：↑/↓ 选择、Enter 执行、Esc 关闭（modal 层已处理）。
 */
import { h } from '../../lib/dom.js';
import { applyRunMeta, runDisplayName } from '../../lib/run-meta.js';
import { S } from '../../lib/store.js';

import { closeModal, showModal } from '../../ui/modal.js';

import { switchNav } from '../../app/router.js';
import { openTaskDetail } from '../detail/panel.js';
import { switchRun } from '../runs/index.js';

const PAGES = [
  { nav: 'conversation', label: '对话' },
  { nav: 'dashboard', label: '运行总览' },
  { nav: 'tasks', label: '计划审查' },
  { nav: 'dag', label: '任务图' },
  { nav: 'agents', label: 'Agents' },
  { nav: 'profiles', label: '执行体档案' },
  { nav: 'gates', label: '质量门禁' },
  { nav: 'rewind', label: '回滚重跑' },
  { nav: 'artifacts', label: '产物' },
  { nav: 'events', label: '事件日志' },
  { nav: 'providers', label: '供应商' },
  { nav: 'schedules', label: '定时任务' },
  { nav: 'flows', label: '流程库' },
  { nav: 'settings', label: '设置' },
];

const MAX_RESULTS = 12;

let cachedRuns = null;
let cachedRunsAt = 0;

async function getRuns() {
  if (cachedRuns && Date.now() - cachedRunsAt < 10000) return cachedRuns;
  try {
    cachedRuns = (await window.studio.listRuns()) || [];
    cachedRunsAt = Date.now();
  } catch { cachedRuns = cachedRuns || []; }
  return cachedRuns;
}

/** 查询 → 结果数组（pages / runs / tasks），query 为空时给默认列表。 */
function buildResults(query, runs) {
  const q = String(query || '').trim().toLowerCase();
  const out = [];
  const hit = (text) => !q || String(text || '').toLowerCase().includes(q);

  for (const p of PAGES) {
    if (hit(p.label) || hit(p.nav)) {
      out.push({ kind: '页面', label: p.label, sub: p.nav, action: () => switchNav(p.nav) });
    }
  }
  const meta = S.settings && S.settings.runMeta;
  for (const r of applyRunMeta(runs, meta).slice(0, 50)) {
    if (hit(r.alias) || hit(r.goal) || hit(r.runId)) {
      out.push({
        kind: '运行', label: runDisplayName(r),
        sub: `${r.runId || ''} · ${r.status || ''}`,
        action: () => { void switchRun(r.dir); },
      });
    }
  }
  const tasks = (S.dash && S.dash.tasks) || [];
  for (const t of tasks.slice(0, 40)) {
    if (hit(t.id) || hit(t.title)) {
      out.push({
        kind: '任务', label: `${t.id} ${t.title || ''}`.trim(),
        sub: t.status || '', action: () => openTaskDetail(t.id),
      });
    }
  }
  return out.slice(0, MAX_RESULTS);
}

function openPalette() {
  const input = h('input', {
    id: 'palette-input', class: 'palette-input', type: 'text', spellcheck: 'false',
    placeholder: '搜索页面 / 运行 / 任务（↑↓ 选择，Enter 执行）',
    'aria-label': '命令面板搜索',
  });
  const list = h('div', { class: 'palette-list', role: 'listbox', 'aria-label': '搜索结果' });
  let results = [];
  let selected = 0;

  const renderResults = () => {
    list.replaceChildren();
    if (!results.length) {
      list.appendChild(h('div', { class: 'palette-empty' }, '没有匹配项'));
      return;
    }
    results.forEach((r, i) => {
      list.appendChild(h('button', {
        class: `palette-item${i === selected ? ' active' : ''}`, type: 'button', role: 'option',
        'aria-selected': i === selected ? 'true' : 'false',
        onclick: () => { closeModal(); r.action(); },
      },
        h('span', { class: 'palette-kind' }, r.kind),
        h('span', { class: 'palette-label' }, r.label),
        h('span', { class: 'palette-sub' }, r.sub || '')));
    });
    const active = list.children[selected];
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
  };

  const rebuild = (runs) => {
    results = buildResults(input.value, runs);
    selected = 0;
    renderResults();
  };

  input.addEventListener('input', () => { rebuild(cachedRuns || []); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); selected = Math.min(selected + 1, results.length - 1); renderResults(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); selected = Math.max(selected - 1, 0); renderResults(); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const r = results[selected];
      if (r) { closeModal(); r.action(); }
    }
  });

  showModal(h('div', { class: 'palette' },
    h('div', { class: 'palette-head' }, input,
      h('span', { class: 'palette-kbd' }, 'Ctrl K')),
    list), { wide: false });
  rebuild([]);
  void getRuns().then((runs) => {
    // 弹窗已被用户 Esc 关掉时不再写 DOM
    if (document.getElementById('palette-input')) rebuild(runs);
  });
  input.focus();
}

export { openPalette };
