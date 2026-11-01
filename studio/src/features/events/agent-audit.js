/**
 * 执行体变更审计：把 ledger 里的 `agent_switched` / `agent_binding_reset`
 * 单独摊成一列时间线。
 *
 * 为什么值得单独一块：切换是"运行中被悄悄改变的行为"，混在几千条事件里
 * 等于没记。这里按发生顺序列出来，谁在什么时候把哪个角色换成了谁、为什么，
 * 一眼能读完。
 */
import { h } from '../../lib/dom.js';
import { fmtClock } from '../../lib/format.js';

const AUDIT_EVENTS = ['agent_switched', 'agent_binding_reset'];

/** 按时间正序取出审计事件（ledger 本身是追加写，顺序即发生顺序）。 */
function collectAgentAudit(events) {
  return (events || []).filter((e) => AUDIT_EVENTS.includes(e.event));
}

function flowText(e) {
  const d = e.data || {};
  if (e.event === 'agent_binding_reset') {
    return d.from_profile ? `${d.from_profile} → 恢复默认` : '恢复默认绑定';
  }
  return d.from_profile ? `${d.from_profile} → ${d.to || ''}` : `→ ${d.to || ''}`;
}

function renderRow(e) {
  const d = e.data || {};
  const isReset = e.event === 'agent_binding_reset';
  return h('div', { class: 'ev-audit-row' + (isReset ? ' reset' : '') },
    h('span', { class: 'ev-audit-ts' }, fmtClock(e.ts)),
    h('span', { class: 'ev-audit-role' }, d.role || '—'),
    h('span', { class: 'ev-audit-flow' }, flowText(e)),
    h('span', { class: 'ev-audit-reason' }, isReset ? '' : (d.reason || '')),
    h('span', { class: 'ev-audit-tag' }, isReset ? '回滚' : (d.duty || '切换')));
}

/**
 * 渲染审计卡；没有审计事件时返回 null（调用方据此决定要不要占位）。
 */
function renderAgentAudit(events) {
  const audit = collectAgentAudit(events);
  if (!audit.length) return null;
  const resets = audit.filter((e) => e.event === 'agent_binding_reset').length;
  const box = h('div', { class: 'ev-audit' });
  box.appendChild(h('div', { class: 'ev-audit-head' },
    h('span', { class: 'ev-audit-title' }, '执行体变更'),
    h('span', { class: 'ev-audit-count' }, `${audit.length} 次`),
    h('span', { class: 'ev-audit-sub' },
      resets ? `其中 ${resets} 次回滚` : '全部为切换，未回滚')));
  const rows = h('div', { class: 'ev-audit-rows' });
  for (const e of audit) rows.appendChild(renderRow(e));
  box.appendChild(rows);
  return box;
}

export { AUDIT_EVENTS, collectAgentAudit, renderAgentAudit };
