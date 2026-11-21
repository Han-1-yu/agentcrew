/**
 * 角色 → 执行体绑定矩阵（只读）。
 *
 * 绑定编辑仍在设置页（生成 YAML 片段）；这里负责回答三个问题：
 * 谁绑了谁、用了什么职责/权重、有没有断链（引用了不存在的档案）。
 */
import { h } from '../../lib/dom.js';
import { ROLE_META } from '../../contracts/enums.js';

import { icon } from './icons.js';

const DUTY_LABEL = {
  primary: '主笔', section: '分工', review: '复核', judge: '裁判',
};
const AGG_LABEL = {
  primary_with_review: '主笔 + 复核',
  split_by_path: '按路径分工',
  best_of: '择优（best_of）',
  merge: '合并（merge）',
};

function roleName(role) {
  const meta = ROLE_META[role.role];
  return (meta && meta.full) || role.title || role.role;
}

function roleColor(role) {
  const meta = ROLE_META[role.role];
  return (meta && meta.color) || 'var(--text-ter)';
}

function bindingMatrix({ roles = [], profiles = [], onEditProfile } = {}) {
  const known = new Set(profiles.map((p) => p.key));
  const bound = roles.filter((role) => (role.agents || []).length);
  const unbound = roles.filter((role) => !(role.agents || []).length);
  const wrap = h('div', { class: 'pf-matrix' });

  if (!bound.length) {
    wrap.appendChild(h('div', { class: 'pf-empty-inline' },
      h('div', { class: 'pf-empty-title' }, '还没有角色绑定执行体'),
      h('p', { class: 'pf-empty-desc' },
        '内置角色默认走单执行体（角色模型 / 全局默认）。要让某个角色用上这些档案，去设置页的「角色与 Agent」矩阵勾选职责。'),
      h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: () => document.querySelector('.nav-item[data-nav="settings"]')?.click(),
      }, '打开设置页绑定')));
  } else {
    for (const role of bound) {
      const chips = (role.agents || []).map((agent) => {
        const missing = !known.has(agent.ref);
        const label = `${agent.ref} · ${DUTY_LABEL[agent.duty] || agent.duty}`
          + (agent.weight !== 1 ? ` ×${agent.weight}` : '');
        return h(missing ? 'span' : 'button', {
          class: `pf-bind-chip${missing ? ' is-broken' : ''}`,
          type: missing ? undefined : 'button',
          title: missing ? '引用了未定义的执行体：启动期会直接报错' : '打开档案',
          onclick: missing ? null : () => onEditProfile
            && onEditProfile(profiles.find((p) => p.key === agent.ref)),
        }, missing ? icon('warn', 13) : null, h('span', {}, label));
      });
      const agg = role.aggregation && role.aggregation.mode
        ? (AGG_LABEL[role.aggregation.mode] || role.aggregation.mode)
        : '单执行体';
      const judge = role.aggregation && role.aggregation.judge
        ? ` · 裁判 ${role.aggregation.judge}` : '';
      wrap.appendChild(h('div', { class: 'pf-matrix-row', 'data-role': role.role },
        h('div', { class: 'pf-matrix-role' },
          h('span', { class: 'pf-matrix-dot', style: `background:${roleColor(role)}` }),
          h('span', { class: 'pf-matrix-name' }, roleName(role)),
          h('span', { class: 'pf-matrix-key mono' }, role.role)),
        h('div', { class: 'pf-matrix-agents' }, ...chips),
        h('div', { class: 'pf-matrix-agg' }, `${agg}${judge}`)));
    }
  }

  if (unbound.length) {
    wrap.appendChild(h('details', { class: 'pf-unbound' },
      h('summary', {}, `未绑定执行体的角色（${unbound.length}）`),
      h('div', { class: 'pf-unbound-list' },
        ...unbound.map((role) => h('span', { class: 'pf-unbound-chip' },
          h('span', { class: 'pf-matrix-dot', style: `background:${roleColor(role)}` }),
          roleName(role))))));
  }
  return wrap;
}

export { bindingMatrix };
