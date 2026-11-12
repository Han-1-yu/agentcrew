/**
 * 执行体档案管理页（对齐 WorkBuddy 的「专家 / 技能」概念）：
 * 浏览执行体档案（AgentProfile）与角色绑定矩阵，并可一键导出可复用 YAML 片段。
 * 只读 + 导出，不写配置——导入即「复制片段粘贴进 agentcrew.yaml」。
 */
import { h } from '../../lib/dom.js';
import { ROLE_META } from '../../contracts/enums.js';
import { loadAgents } from '../../lib/agent-binding.js';
import { toast } from '../../ui/toast.js';

const DUTY_LABEL = {
  primary: { label: '主笔', chip: 'primary' },
  section: { label: '分区', chip: 'primary' },
  review:  { label: '复核', chip: 'review' },
  judge:   { label: '裁判', chip: 'judge' },
};
const AGG_LABEL = {
  primary_with_review: '主笔 + 复核',
  split_by_path: '按路径拆分',
  best_of: '择优（best_of）',
  merge: '合并（merge）',
};

function fmt(v) {
  if (v === undefined || v === null || v === '') return null;
  if (Array.isArray(v)) return v.length ? v.join('、') : null;
  return String(v);
}

function profileCard(p) {
  const rows = [
    ['模型', fmt(p.model) || '继承角色'],
    ['工具白名单', fmt(p.tools) || '继承角色'],
    ['写入范围', fmt(p.path_scope) || '不限'],
    ['单任务成本上限', p.cost_ceiling_usd != null ? `$${p.cost_ceiling_usd}` : null],
    ['整场成本上限', p.run_cost_ceiling_usd != null ? `$${p.run_cost_ceiling_usd}` : null],
    ['RPM', p.max_rpm != null ? String(p.max_rpm) : null],
    ['并发', p.max_concurrency != null ? String(p.max_concurrency) : null],
    ['温度', p.temperature != null ? String(p.temperature) : null],
  ].filter(([, v]) => v != null);
  return h('div', { class: 'profile-card' },
    h('div', { class: 'profile-head' },
      h('span', { class: 'profile-key mono' }, p.key),
      h('span', { class: 'profile-title' }, p.title || ''),
      h('span', { class: 'profile-fallback mono' }, (p.fallback && p.fallback.length)
        ? `备用：${p.fallback.join('、')}` : '')),
    h('div', { class: 'profile-rows' },
      ...rows.map(([k, v]) => h('div', { class: 'profile-row' },
        h('span', { class: 'k' }, k), h('span', { class: 'v' }, v)))));
}

function bindingMatrix(roles) {
  const list = h('div', { class: 'binding-list' });
  (roles || []).forEach(r => {
    const meta = ROLE_META[r.role] || { label: r.role, full: r.role, color: 'var(--text-ter)' };
    const agents = r.agents || [];
    if (!agents.length) return;
    list.appendChild(h('div', { class: 'binding-row' },
      h('div', { class: 'binding-role' },
        h('span', { class: 'binding-role-dot', style: `background:${meta.color}` }),
        h('span', { class: 'binding-role-name' }, meta.full || r.role),
        h('span', { class: 'binding-role-key mono' }, r.role)),
      h('div', { class: 'binding-agents' },
        ...agents.map(a => h('span', {
          class: `binding-agent duty-${DUTY_LABEL[a.duty] ? DUTY_LABEL[a.duty].chip : ''}`,
        }, `${a.ref} · ${DUTY_LABEL[a.duty] ? DUTY_LABEL[a.duty].label : a.duty}${a.weight !== 1 ? ` ×${a.weight}` : ''}`))),
      h('div', { class: 'binding-agg' },
        r.aggregation && r.aggregation.mode ? (AGG_LABEL[r.aggregation.mode] || r.aggregation.mode) : '单执行体')));
  });
  if (!list.children.length) {
    list.appendChild(h('div', { class: 'page-state' },
      h('div', { class: 'page-state-title' }, '未配置多执行体绑定'),
      h('div', { class: 'page-state-detail' },
        '内置角色默认走单执行体（role_bindings / default_model）。在 agentcrew.yaml 的 roles 段可为角色声明多个执行体与汇总策略（primary_with_review / split_by_path / best_of / merge）。')));
  }
  return list;
}

function buildPageHead() {
  return h('div', { class: 'page-head' },
    h('h2', {}, '执行体档案'),
    h('button', { class: 'btn', onclick: async () => {
      try {
        const res = await window.studio.agentsExport();
        if (!res || !res.ok) throw new Error((res && res.error) || '导出失败');
        const yaml = String(res.yaml || '').trim();
        if (!yaml) { toast('info', '没有可导出的档案', '当前未配置自定义执行体，片段为空'); return; }
        try {
          await navigator.clipboard.writeText(yaml);
          toast('success', '已复制', 'YAML 片段已复制，可粘贴进 agentcrew.yaml');
        } catch {
          toast('info', '已生成片段', yaml.split('\n').slice(0, 4).join('\n'));
        }
      } catch (e) { toast('error', '导出失败', String(e.message || e)); }
    } }, '导出 YAML 片段'));
}

async function renderProfilesPage(container) {
  container.appendChild(buildPageHead());
  const wrap = h('div', { class: 'profiles-wrap' });
  container.appendChild(wrap);

  let data = { profiles: [], roles: [] };
  try { data = await loadAgents(); }
  catch (e) {
    wrap.appendChild(h('div', { class: 'page-state is-error' },
      h('div', { class: 'page-state-title' }, '读取失败'),
      h('div', { class: 'page-state-detail' }, String(e.message || e))));
    return;
  }
  const profiles = data.profiles || [];
  const roles = data.roles || [];

  // 执行体档案
  wrap.appendChild(h('div', { class: 'block-head' },
    h('h2', { class: 'block-title' }, `执行体档案（${profiles.length}）`)));
  if (!profiles.length) {
    wrap.appendChild(h('div', { class: 'page-state' },
      h('div', { class: 'page-state-title' }, '未配置自定义执行体'),
      h('div', { class: 'page-state-detail' },
        '在 agentcrew.yaml 的 agents.profiles 段定义执行体后，这里会列出其模型、工具与成本上限。内置角色默认使用单执行体。')));
  } else {
    wrap.appendChild(h('div', { class: 'profiles-grid' }, ...profiles.map(profileCard)));
  }

  // 角色绑定矩阵
  wrap.appendChild(h('div', { class: 'block-head', style: 'margin-top:24px' },
    h('h2', { class: 'block-title' }, '角色 → 执行体绑定')));
  wrap.appendChild(bindingMatrix(roles));
}

export { renderProfilesPage };
