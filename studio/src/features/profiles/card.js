/**
 * 档案卡片：身份（key / 来源）、能力、生效参数、用量、绑定与操作。
 *
 * 卡片只负责呈现与转发事件；写操作全部由页面层调用后端完成。
 */
import { h } from '../../lib/dom.js';
import { ROLE_META } from '../../contracts/enums.js';
import { formatMoney, formatTokens, relativeTime } from '../../lib/profile-form.js';

import { icon } from './icons.js';

const DUTY_LABEL = {
  primary: { label: '主笔', cls: 'primary' },
  section: { label: '分工', cls: 'primary' },
  review: { label: '复核', cls: 'review' },
  judge: { label: '裁判', cls: 'judge' },
};

function actionButton(name, label, onclick, { disabled = false, title = '' } = {}) {
  return h('button', {
    class: 'pf-icon-btn', type: 'button', title: title || label,
    'aria-label': label, disabled,
    onclick: (event) => { event.stopPropagation(); if (!disabled) onclick(); },
  }, icon(name, 16));
}

function usageLine(usage, unavailable) {
  if (unavailable) return h('span', { class: 'pf-muted' }, '用量暂不可用');
  if (!usage || !usage.calls) return h('span', { class: 'pf-muted' }, '尚未被调用');
  return h('span', {},
    `${usage.calls} 次调用`,
    h('span', { class: 'pf-dot-sep' }, '·'),
    formatMoney(usage.cost_usd),
    h('span', { class: 'pf-dot-sep' }, '·'),
    `${formatTokens(usage.total_tokens)} tok`,
    h('span', { class: 'pf-dot-sep' }, '·'),
    relativeTime(usage.last_used));
}

function budgetText(profile) {
  const parts = [];
  if (profile.cost_ceiling_usd != null) parts.push(`单任务 $${profile.cost_ceiling_usd}`);
  if (profile.run_cost_ceiling_usd != null) parts.push(`整场 $${profile.run_cost_ceiling_usd}`);
  if (profile.max_rpm != null) parts.push(`${profile.max_rpm} RPM`);
  if (profile.max_concurrency != null) parts.push(`并发 ${profile.max_concurrency}`);
  return parts.join(' · ') || '仅受全局预算';
}

function profileCard(profile, {
  usage = null, usageUnavailable = false, roles = [], issues = 0,
  onEdit, onDuplicate, onDelete, onCopy, onExport,
} = {}) {
  const bindings = [];
  for (const role of roles) {
    for (const agent of role.agents || []) {
      if (agent.ref === profile.key) bindings.push({ role, agent });
    }
  }
  const toolList = Array.isArray(profile.tools) ? profile.tools : null;
  const capList = profile.capabilities || [];

  const head = h('header', { class: 'pf-card-head' },
    h('div', { class: 'pf-card-title-row' },
      h('h3', { class: 'pf-card-title' }, profile.title || profile.key),
      h('span', {
        class: `pf-source ${profile.source === 'agents.d' ? 'is-file' : 'is-yaml'}`,
        title: profile.file || '定义在 agentcrew.yaml',
      }, profile.source === 'agents.d' ? 'agents.d' : 'YAML')),
    h('div', { class: 'pf-card-key mono' }, profile.key),
    h('p', { class: 'pf-card-desc' },
      profile.description || '未填写能力描述（规划菜单只能看到 key）'));

  const flags = [];
  if (!bindings.length) flags.push(h('span', { class: 'pf-flag is-muted' }, '未绑定'));
  if (issues) {
    flags.push(h('span', { class: 'pf-flag is-warn' }, `${issues} 个问题`));
  }
  if (profile.shadowed) {
    flags.push(h('span', {
      class: 'pf-flag is-info', title: 'agentcrew.yaml 里有同名定义，当前生效的是 agents.d',
    }, '覆盖 YAML'));
  }

  const chips = capList.length
    ? h('div', { class: 'pf-cap-chips' },
      ...capList.map((cap) => h('span', { class: 'pf-cap' }, cap)))
    : h('div', { class: 'pf-cap-chips' }, h('span', { class: 'pf-muted' }, '未标注能力'));

  const meta = h('dl', { class: 'pf-meta' },
    h('dt', {}, '模型'), h('dd', {}, profile.model || h('span', { class: 'pf-muted' }, '继承角色')),
    h('dt', {}, '工具'), h('dd', {},
      toolList == null
        ? h('span', { class: 'pf-muted' }, '继承角色')
        : h('span', { title: toolList.join('、') || '无' },
          toolList.length ? `${toolList.length} 项：${toolList.join('、')}` : '无（纯文本）')),
    h('dt', {}, '写入'), h('dd', {},
      (profile.path_scope || []).length
        ? profile.path_scope.join('、')
        : h('span', { class: 'pf-muted' }, '不限')),
    h('dt', {}, '预算'), h('dd', {}, budgetText(profile)));

  const bindingRow = bindings.length
    ? h('div', { class: 'pf-bindings' },
      ...bindings.map(({ role, agent }) => {
        const metaInfo = ROLE_META[role.role] || { full: role.title || role.role, color: 'var(--text-ter)' };
        const duty = DUTY_LABEL[agent.duty] || { label: agent.duty, cls: '' };
        return h('span', {
          class: `pf-binding duty-${duty.cls}`,
          title: `${role.title || role.role} · ${duty.label}${agent.weight !== 1 ? ` ×${agent.weight}` : ''}`,
        },
        h('span', { class: 'pf-binding-dot', style: `background:${metaInfo.color}` }),
        h('span', {}, metaInfo.full || role.role),
        h('span', { class: 'pf-binding-duty' }, duty.label));
      }))
    : h('div', { class: 'pf-bindings' },
      h('span', { class: 'pf-muted' }, '未绑定角色 · 在设置页的绑定矩阵里挂上'));

  return h('article', {
    class: `pf-card${issues ? ' has-issue' : ''}`,
    'data-key': profile.key,
    onclick: () => onEdit && onEdit(profile),
  },
  head,
  flags.length ? h('div', { class: 'pf-flags' }, ...flags) : null,
  chips,
  meta,
  h('div', { class: 'pf-usage' }, icon('refresh', 13), usageLine(usage, usageUnavailable)),
  bindingRow,
  h('footer', { class: 'pf-card-actions' },
    actionButton('edit', '编辑', () => onEdit && onEdit(profile)),
    actionButton('copy', '复制为新档案', () => onDuplicate && onDuplicate(profile)),
    actionButton('download', '复制 YAML', () => onCopy && onCopy(profile)),
    actionButton('trash', '删除', () => onDelete && onDelete(profile), {
      disabled: profile.source !== 'agents.d',
      title: profile.source === 'agents.d'
        ? '删除（移入 agents.d/.trash）'
        : '该档案定义在 agentcrew.yaml，请直接编辑主配置',
    })));
}

export { profileCard };
