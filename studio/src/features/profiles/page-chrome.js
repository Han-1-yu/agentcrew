/** 档案页的静态骨架：页头、状态条、筛选栏与空态。 */
import { h } from '../../lib/dom.js';
import { filterProfiles, summarizeProfiles } from '../../lib/profile-form.js';
import { toast } from '../../ui/toast.js';

import { icon } from './icons.js';

const FILTERS = [
  ['all', '全部'],
  ['bound', '已绑定'],
  ['unbound', '未绑定'],
  ['issue', '有问题'],
  ['file', 'agents.d'],
  ['yaml', 'YAML'],
];

function buildPageHead({ onRefresh, onNew, onImport }) {
  return h('div', { class: 'page-head pf-page-head' },
    h('div', {},
      h('h2', {}, '执行体档案'),
      h('p', { class: 'pf-page-sub' },
        '为角色准备可复用的「怎么干」：模型、工具白名单、写入范围与预算。保存在 agents.d/，下次运行生效。')),
    h('div', { class: 'pf-head-actions' },
      h('button', {
        class: 'pf-icon-btn', type: 'button', title: '重新读取档案与诊断',
        'aria-label': '刷新', onclick: onRefresh,
      }, icon('refresh', 16)),
      h('button', { class: 'btn', type: 'button', onclick: onImport },
        icon('upload', 15), '导入 YAML'),
      h('button', { class: 'btn btn-primary', type: 'button', onclick: onNew },
        icon('plus', 15), '新建档案')));
}

function buildEmptyHero({ onNew, onImport, onSettings }) {
  return h('div', { class: 'pf-hero' },
    h('div', { class: 'pf-hero-icon' }, icon('plus', 22)),
    h('h3', { class: 'pf-hero-title' }, '还没有执行体档案'),
    h('p', { class: 'pf-hero-desc' },
      '执行体档案决定「这个角色用哪个模型、能碰哪些工具、最多花多少钱」。'
      + '同一条职责可以配多个执行体：便宜档主笔、强推理档复核，或按目录分工。'),
    h('div', { class: 'pf-hero-actions' },
      h('button', { class: 'btn btn-primary', type: 'button', onclick: onNew },
        '新建第一个执行体'),
      h('button', { class: 'btn', type: 'button', onclick: onImport }, '从 YAML 导入'),
      h('button', { class: 'btn', type: 'button', onclick: onSettings }, '先看角色绑定')),
    h('div', { class: 'pf-hero-hint' },
      '也可以直接编辑 agentcrew.yaml 的 agents.profiles，或把文件放进 agents.d/。'));
}

function statusBar({ summary, storage, filter, onFilter }) {
  const chip = (key, label, value, cls = '') => h('button', {
    class: `pf-stat ${cls}${filter === key ? ' is-active' : ''}`,
    type: 'button', onclick: () => onFilter(key),
  }, h('span', { class: 'pf-stat-value' }, String(value)), h('span', {}, label));

  return h('div', { class: 'pf-status' },
    h('div', { class: 'pf-stats' },
      chip('all', '档案', summary.total),
      chip('bound', '已绑定', summary.bound),
      chip('unbound', '未绑定', summary.unbound),
      chip('issue', '有问题', summary.issue, summary.issue ? 'is-warn' : '')),
    storage
      ? h('div', { class: 'pf-storage' },
        icon('folder', 14),
        h('span', { class: 'pf-storage-label' }, '档案目录'),
        h('span', { class: 'pf-storage-path mono', title: storage.profiles_dir },
          storage.profiles_dir),
        h('span', { class: 'pf-storage-note' },
          storage.profiles_dir_exists ? '' : '（首次保存时创建）'),
        h('button', {
          class: 'btn btn-sm', type: 'button',
          onclick: () => window.studio.agentsOpenDir()
            .then((res) => { if (res && !res.ok) throw new Error(res.error || '打开失败'); })
            .catch((e) => toast('error', '打开失败', String(e.message || e))),
        }, '打开'))
      : null);
}

function toolbar({ profiles, diagnostics, filter, query, onFilter, onQuery }) {
  const stats = summarizeProfiles(profiles, diagnostics);
  return h('div', { class: 'pf-toolbar' },
    h('div', { class: 'pf-search-wrap' },
      icon('search', 15),
      h('input', {
        class: 'pf-search', type: 'search',
        placeholder: '搜索 key / 名称 / 能力 / 绑定角色',
        value: query,
        oninput: (event) => onQuery(event.target.value),
      })),
    h('div', { class: 'pf-filters' },
      ...FILTERS.map(([key, label]) => h('button', {
        class: `pf-filter${filter === key ? ' is-active' : ''}`,
        type: 'button', 'data-filter': key, onclick: () => onFilter(key),
      }, label, h('span', { class: 'pf-filter-count' }, String(
        filterProfiles(profiles, { filter: key, issues: stats.issues }).length))))));
}

export { buildEmptyHero, buildPageHead, statusBar, toolbar };
