/**
 * 配置诊断面板：把后端的 error / warning / info 变成可逐条定位的列表。
 *
 * 与「启动失败」的区别：run 仍然 fail-fast；面板的作用是让用户在保存前后
 * 就知道哪份档案、哪个角色会出问题，而不是等下一次启动报一行错。
 */
import { h } from '../../lib/dom.js';

import { icon } from './icons.js';

const LEVELS = [
  ['errors', 'error', '错误', '阻止启动 / 保存'],
  ['warnings', 'warn', '警告', '可以保存，但会有后果'],
  ['infos', 'info', '提示', '不处理也能跑'],
];

function diagnosticItem(item, onLocate) {
  const where = item.profile || item.role || '';
  return h('li', { class: 'pf-diag-item' },
    h('div', { class: 'pf-diag-line' },
      h('span', {}, item.message),
      where
        ? h('button', {
          class: 'pf-diag-where', type: 'button', title: '定位到相关档案 / 角色',
          onclick: () => onLocate && onLocate(item),
        }, where)
        : null),
    item.hint ? h('div', { class: 'pf-diag-hint' }, item.hint) : null);
}

function diagnosticsPanel(diagnostics, { onLocate, onReload } = {}) {
  const summary = (diagnostics && diagnostics.summary) || { errors: 0, warnings: 0, infos: 0 };
  if (!summary.errors && !summary.warnings && !summary.infos) {
    return h('div', { class: 'pf-diag is-clean' },
      h('div', { class: 'pf-diag-head' },
        h('span', { class: 'pf-diag-icon is-ok' }, icon('check', 15)),
        h('span', { class: 'pf-diag-title' }, '配置检查通过'),
        h('span', { class: 'pf-diag-sub' }, '没有发现会阻止启动或影响编排的问题')));
  }

  const hasProblem = summary.errors > 0;
  const head = h('summary', { class: 'pf-diag-head' },
    h('span', { class: `pf-diag-icon ${hasProblem ? 'is-bad' : 'is-warn'}` },
      icon(hasProblem ? 'warn' : 'info', 15)),
    h('span', { class: 'pf-diag-title' }, hasProblem ? '配置有问题' : '配置提醒'),
    h('span', { class: 'pf-diag-counts' },
      summary.errors ? h('span', { class: 'pf-count is-bad' }, `${summary.errors} 错误`) : null,
      summary.warnings ? h('span', { class: 'pf-count is-warn' }, `${summary.warnings} 警告`) : null,
      summary.infos ? h('span', { class: 'pf-count is-info' }, `${summary.infos} 提示`) : null),
    onReload
      ? h('button', {
        class: 'pf-icon-btn', type: 'button', title: '重新检查',
        onclick: (event) => { event.preventDefault(); event.stopPropagation(); onReload(); },
      }, icon('refresh', 15))
      : null);

  const body = h('div', { class: 'pf-diag-body' });
  for (const [key, iconName, label, hint] of LEVELS) {
    const items = (diagnostics && diagnostics[key]) || [];
    if (!items.length) continue;
    body.appendChild(h('div', { class: 'pf-diag-group' },
      h('div', { class: 'pf-diag-group-title' },
        icon(iconName, 14), h('span', {}, label),
        h('span', { class: 'pf-diag-group-hint' }, hint)),
      h('ul', { class: 'pf-diag-list' },
        ...items.map((item) => diagnosticItem(item, onLocate)))));
  }

  return h('details', { class: 'pf-diag', open: hasProblem }, head, body);
}

export { diagnosticsPanel };
