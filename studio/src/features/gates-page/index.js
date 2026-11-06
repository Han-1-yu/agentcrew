/**
 * 门禁页面：三层门禁状态与人工审批。
 */
import { h } from '../../lib/dom.js';
import { trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { emptyState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';

import { switchNav } from '../../app/router.js';

import { acceptGate, gateStatusLabel, rejectGate } from '../dashboard/gates.js';
function renderGatesPage(container) {
  // 审批后只重渲染本页容器。此前用 container.parentElement（即 <main>），
  // 新的页头/列表被追加到 dashboard 之外、既不清理旧内容也不受 switchNav
  // 的 ph.innerHTML='' 管理，点一次审批就永久多出一套重复页面。
  const rerender = () => { container.innerHTML = ''; renderGatesPage(container); };
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '质量门禁'),
    h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回')));
  const list = h('div', { class: 'gates-full-list' });
  container.appendChild(list);
  const gates = S.dash.gates || [];
  if (!gates.length) {
    list.appendChild(emptyState('暂无门禁记录', '本次运行还没有产生质量门禁检查。'));
    return;
  }
  for (const g of gates) {
    const reviews = (S.dash.tasks || []).flatMap(t => (t.reviews || []).filter(r => r.layer === g.layer));
    list.appendChild(h('div', { class: 'gate-full-card' },
      h('div', { class: 'gate-full-head' },
        h('span', { class: 'gate-full-id' }, g.id),
        h('span', { class: 'gate-full-name' }, g.name),
        h('span', { class: `gate-full-status status-${g.status}` }, gateStatusLabel(g.status))),
      h('div', { class: 'gate-full-stats' },
        h('div', { class: 'gate-full-stat' }, h('div', { class: 'n' }, g.checks.total), h('div', { class: 'l' }, '检查项')),
        h('div', { class: 'gate-full-stat ok' }, h('div', { class: 'n' }, g.checks.passed), h('div', { class: 'l' }, '通过')),
        h('div', { class: 'gate-full-stat err' }, h('div', { class: 'n' }, g.checks.failed), h('div', { class: 'l' }, '失败'))),
      h('div', { class: 'gate-full-verdict' }, g.verdict || ''),
      reviews.length ? h('div', { class: 'gate-full-reviews' },
        h('div', { class: 'gate-full-reviews-title' }, `评审记录（${reviews.length}）`),
        ...reviews.slice(0, 10).map(r => h('div', { class: 'gate-review-item' },
          h('span', { class: `gate-review-verdict ${r.verdict}` }, r.verdict),
          h('span', { class: 'gate-review-task' }, r.task_id || ''),
          h('span', { class: 'gate-review-text' }, trunc(r.summary || r.issues?.join('; ') || '', 80))))) : null,
      !S.realMode ? h('div', { class: 'gate-full-actions' },
        h('button', { class: 'btn btn-sm btn-primary', onclick: () => { acceptGate(g); rerender(); } }, '批准通过'),
        h('button', { class: 'btn btn-sm btn-danger', onclick: () => { rejectGate(g); rerender(); } }, '驳回返工'),
        h('button', { class: 'btn btn-sm', onclick: () => { g.status = 'running'; g.checks = { total: g.checks.total, passed: 0, failed: 0 }; toast('info', '重试门禁', g.name + ' 重新运行'); rerender(); } }, '重试'))
        : h('div', { class: 'gate-full-readonly' }, '真实模式下门禁审批需通过 CLI 或 API 执行')));
  }
}

export { renderGatesPage };
