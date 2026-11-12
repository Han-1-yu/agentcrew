/**
 * 组「角色与 Agent」：绑定矩阵。
 *
 * 数据来自 `main.py agents --json`（与执行期同一套 RunPlan 解析），Studio 不解析
 * 也不改写 agentcrew.yaml —— 矩阵改完只生成 YAML 片段供复制，与供应商页同一约定。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

const DUTIES = [
  ['', '未绑定'],
  ['primary', 'primary · 主笔'],
  ['section', 'section · 分工'],
  ['review', 'review · 复核'],
  ['judge', 'judge · 裁判'],
];

const MODES = [
  ['primary_with_review', '主笔 + 复核'],
  ['split_by_path', '按路径分工'],
  ['best_of', 'N 选 1'],
  ['merge', '合并'],
];

async function copyText(text) {
  // writeText 返回 Promise：不 await 时 try/catch 抓不到拒绝，会「报成功但没复制」，
  // 还触发全局 unhandledrejection。与 providers/forms.js 的 copyYaml 同款兜底。
  try {
    await navigator.clipboard.writeText(text);
    toast('success', '已复制', 'YAML 片段已写入剪贴板');
  } catch {
    toast('error', '复制失败', '请手动选择文本复制');
  }
}

/** 按当前 draft 生成 roles: 片段（只含有绑定的角色）。 */
function buildSnippet(state) {
  const lines = ['roles:'];
  let wrote = false;
  for (const role of state.roles) {
    const picks = Object.entries(state.assign[role.role] || {})
      .filter(([, duty]) => duty);
    if (!picks.length) continue;
    wrote = true;
    lines.push(`  ${role.role}:`, '    agents:');
    for (const [ref, duty] of picks) {
      lines.push(`      - ref: ${ref}`, `        duty: ${duty}`);
    }
    if (picks.length > 1) {
      const agg = state.modes[role.role] || {};
      lines.push('    aggregation:',
        `      mode: ${agg.mode || 'primary_with_review'}`,
        `      quorum: 1`);
      if (agg.mode === 'best_of' && agg.judge) {
        lines.push(`      judge: ${agg.judge}`);
      }
    }
  }
  return wrote ? lines.join('\n') : '# 还没有任何角色绑定 agent';
}

/** 生成前先自查：这些组合会被后端启动期校验拒掉，别让用户复制出一份跑不通的配置。 */
function validateDraft(state) {
  const problems = [];
  for (const role of state.roles) {
    const picks = Object.entries(state.assign[role.role] || {}).filter(([, d]) => d);
    if (picks.length <= 1) continue;
    const duties = picks.map(([, d]) => d);
    const mode = (state.modes[role.role] || {}).mode || 'primary_with_review';
    if (!duties.some((d) => d === 'primary' || d === 'section')) {
      problems.push(`${role.role}：多执行体里没有 primary/section`);
    }
    if (mode === 'primary_with_review' && !duties.includes('review')) {
      problems.push(`${role.role}：主笔+复核模式缺少 review`);
    }
    if (mode === 'split_by_path') {
      if (duties.filter((d) => d === 'primary' || d === 'section').length < 2) {
        problems.push(`${role.role}：按路径分工至少要两个 primary/section`);
      }
      const noScope = picks.filter(([ref, d]) => (d === 'primary' || d === 'section')
        && !((state.profiles.find((p) => p.key === ref) || {}).path_scope || []).length);
      if (noScope.length) {
        problems.push(`${role.role}：${noScope.map(([r]) => r).join('、')} 没有 path_scope`);
      }
      const scopes = new Map();
      for (const [ref, d] of picks) {
        if (d !== 'primary' && d !== 'section') continue;
        for (const scope of ((state.profiles.find((p) => p.key === ref) || {}).path_scope || [])) {
          if (scopes.has(scope)) problems.push(`${role.role}：路径域 ${scope} 与 ${scopes.get(scope)} 重叠`);
          scopes.set(scope, ref);
        }
      }
    }
    if (mode === 'best_of' && !(state.modes[role.role] || {}).judge) {
      problems.push(`${role.role}：N 选 1 需要指定 judge`);
    }
  }
  return problems;
}

function renderMatrix(state, body) {
  body.innerHTML = '';
  const profiles = state.profiles;
  if (!profiles.length) {
    body.appendChild(h('div', { class: 'ag-hint' },
      '尚未定义执行体。先在 agentcrew.yaml 的 agents.profiles 下声明，再回来绑定。'));
    return;
  }

  const table = h('table', { class: 'ag-matrix' });
  const head = h('tr', {}, h('th', {}, '角色'),
    ...profiles.map((p) => h('th', { title: p.model || '（继承角色模型）' }, p.key)));
  table.appendChild(h('thead', {}, head));

  const tbody = h('tbody', {});
  for (const role of state.roles) {
    const tr = h('tr', {}, h('td', { class: 'ag-role' },
      h('div', { class: 'ag-role-name' }, role.title || role.role),
      h('div', { class: 'ag-role-key' }, role.role)));
    for (const profile of profiles) {
      const current = (state.assign[role.role] || {})[profile.key] || '';
      const select = h('select', {
        class: 'ag-select' + (current ? ' bound' : ''),
        onchange: (e) => {
          const duty = e.target.value;
          state.assign[role.role] = state.assign[role.role] || {};
          if (duty) state.assign[role.role][profile.key] = duty;
          else delete state.assign[role.role][profile.key];
          select.classList.toggle('bound', !!duty);
          renderSummary(state, body);
        },
      }, ...DUTIES.map(([val, label]) => {
        const opt = h('option', { value: val }, label);
        if (val === current) opt.selected = true;
        return opt;
      }));
      tr.appendChild(h('td', {}, select));
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  body.appendChild(table);
  renderSummary(state, body);
}

/** 多 agent 的角色要选汇总模式；单 agent 不需要。 */
function renderSummary(state, body) {
  const old = body.querySelector('.ag-agg');
  if (old) old.remove();
  const multi = state.roles.filter(
    (r) => Object.values(state.assign[r.role] || {}).filter(Boolean).length > 1);
  if (!multi.length) return;

  const box = h('div', { class: 'ag-agg' }, h('div', { class: 'ag-agg-title' }, '多执行体汇总'));
  for (const role of multi) {
    const picks = Object.entries(state.assign[role.role]).filter(([, d]) => d);
    const mode = (state.modes[role.role] || {}).mode || 'primary_with_review';
    state.modes[role.role] = { ...(state.modes[role.role] || {}), mode };
    const select = h('select', { class: 'ag-select',
      onchange: (e) => {
        state.modes[role.role].mode = e.target.value;
        renderSummary(state, body);
      } }, ...MODES.map(([val, label]) => {
      const opt = h('option', { value: val }, label);
      if (val === mode) opt.selected = true;
      return opt;
    }));
    const judgeOptions = [['', '选择 judge'], ...picks
      .filter(([, d]) => d === 'judge').map(([ref]) => [ref, ref])];
    const judge = h('select', { class: 'ag-select' + (mode === 'best_of' ? '' : ' hidden'),
      onchange: (e) => { state.modes[role.role].judge = e.target.value; } },
    ...judgeOptions.map(([val, label]) => h('option', { value: val }, label)));
    box.appendChild(h('div', { class: 'ag-agg-row' },
      h('span', { class: 'ag-agg-role' }, role.role),
      select, judge));
  }
  body.appendChild(box);

  const problems = validateDraft(state);
  const oldWarn = body.querySelector('.ag-warn');
  if (oldWarn) oldWarn.remove();
  if (problems.length) {
    body.appendChild(h('div', { class: 'ag-warn' },
      h('div', { class: 'ag-warn-title' }, '这份配置会被后端拒绝：'),
      ...problems.map((p) => h('div', { class: 'ag-warn-line' }, `· ${p}`))));
  }
}

function buildGroup() {
  const state = { profiles: [], roles: [], assign: {}, modes: {} };
  const body = h('div', { class: 'ag-body' }, h('div', { class: 'ag-hint' }, '正在读取…'));

  const actions = h('div', { class: 'ag-actions' },
    h('button', { class: 'btn btn-sm', onclick: () => load() }, '重新读取'),
    h('button', { class: 'btn btn-sm btn-primary', onclick: () => copyText(buildSnippet(state)) },
      '生成配置片段'));

  async function load() {
    // 只用 navToken 判断过期：buildGroup 在 section 还没挂进 document 时就调
    // load()，isConnected 此刻恒为 false，不能拿它做判据。
    const token = S.navToken;
    body.innerHTML = '';
    body.appendChild(h('div', { class: 'ag-hint' }, '正在读取…'));
    let res;
    try { res = await window.studio.agentsConfig(); }
    catch (e) { res = { ok: false, error: String(e.message || e) }; }
    if (token !== S.navToken) return;
    if (!res || !res.ok) {
      body.innerHTML = '';
      body.appendChild(h('div', { class: 'ag-hint err' },
        `读取失败：${(res && res.error) || '未知错误'}`,
        h('div', { class: 'ag-hint-sub' }, '确认 agentcrew.yaml 可被 `python main.py agents --json` 解析')));
      return;
    }
    // ok=true 但 data 缺失时别抛 TypeError（async 续体没人接会变成全局提示）。
    const cfg = res.data || {};
    state.profiles = cfg.profiles || [];
    state.roles = cfg.roles || [];
    for (const role of state.roles) {
      state.assign[role.role] = {};
      // C-10：role.agents 可能缺失（旧配置/解析异常），此前直接 for...of
      // 抛 TypeError，又被 load().catch(()=>{}) 静默吞掉，界面永久停在
      // 「正在读取…」。兜底空数组。
      for (const a of (role.agents || [])) state.assign[role.role][a.ref] = a.duty;
      if (role.aggregation) state.modes[role.role] = { ...role.aggregation };
    }
    body.innerHTML = '';
    renderMatrix(state, body);
  }

  const section = h('section', { class: 'settings-group' },
    h('div', { class: 'settings-group-title' }, '角色与 Agent'),
    h('div', { class: 'ag-desc' },
      '一个角色可绑定多个执行体：primary 写产物，review 只出意见，judge 在 N 选 1 时择优。'),
    actions, body);
  // C-10：不能静默吞掉——否则渲染异常时用户只看到空白/「正在读取…」，
  // 无从判断是配置问题还是前端 bug。
  load().catch((e) => {
    body.innerHTML = '';
    body.appendChild(h('div', { class: 'ag-error' },
      `读取失败：${(e && e.message) || e}`,
      h('div', { class: 'ag-hint-sub' }, '这是前端异常，请刷新页面重试；若反复出现请反馈控制台报错')));
  });
  return section;
}

export { buildGroup as buildAgentsGroup };
