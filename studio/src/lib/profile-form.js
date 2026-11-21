/**
 * 执行体档案的表单模型与纯函数（无 DOM，便于 node:test 直接覆盖）。
 *
 * 与后端的边界：这里只做「用户能不能点保存」的前置校验与**生效预览**；
 * 真正的红线（工具只能收窄、绑定组合是否可启动、文件如何落盘）由
 * `agents --save` 在同一条校验链上判定——前端不复制一份会漂移的规则。
 */

const KEY_RE = /^[a-z][a-z0-9_-]{0,63}$/;
const RESERVED_KEYS = new Set(['default', 'unknown']);

const NUMBER_FIELDS = [
  ['cost_ceiling_usd', '单任务成本上限', { min: 0, exclusive: true }],
  ['run_cost_ceiling_usd', '整场成本上限', { min: 0, exclusive: true }],
  ['max_calls_per_task', '单任务调用上限', { min: 1, integer: true }],
  ['max_rpm', 'RPM', { min: 1, integer: true }],
  ['max_concurrency', '并发', { min: 1, integer: true }],
  ['temperature', '温度', { min: 0, max: 2 }],
  ['max_output_tokens', '最大输出 token', { min: 1, integer: true }],
];

function emptyDraft() {
  return {
    key: '', title: '', description: '', capabilities: '', model: '', fallback: '',
    toolsMode: 'inherit', tools: [], path_scope: '',
    cost_ceiling_usd: '', run_cost_ceiling_usd: '', max_calls_per_task: '',
    max_rpm: '', max_concurrency: '', temperature: '', max_output_tokens: '',
  };
}

function listToText(list) {
  return Array.isArray(list) ? list.join('、') : '';
}

function draftFromProfile(profile) {
  const p = profile || {};
  return {
    ...emptyDraft(),
    key: p.key || '',
    title: p.title || '',
    description: p.description || '',
    capabilities: listToText(p.capabilities),
    model: p.model || '',
    fallback: listToText(p.fallback),
    toolsMode: Array.isArray(p.tools) ? 'list' : 'inherit',
    tools: Array.isArray(p.tools) ? [...p.tools] : [],
    path_scope: Array.isArray(p.path_scope) ? p.path_scope.join('\n') : '',
    cost_ceiling_usd: p.cost_ceiling_usd ?? '',
    run_cost_ceiling_usd: p.run_cost_ceiling_usd ?? '',
    max_calls_per_task: p.max_calls_per_task ?? '',
    max_rpm: p.max_rpm ?? '',
    max_concurrency: p.max_concurrency ?? '',
    temperature: p.temperature ?? '',
    max_output_tokens: p.max_output_tokens ?? '',
  };
}

/** 逗号 / 中文逗号 / 换行分隔 → 去空去重（保持输入顺序）。 */
function parseList(text) {
  const seen = new Set();
  const out = [];
  String(text || '')
    .split(/[\n,，、;；]+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((item) => {
      if (seen.has(item)) return;
      seen.add(item);
      out.push(item);
    });
  return out;
}

function parsePathScopes(text) {
  return String(text || '')
    .split(/\n+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function toNumber(raw) {
  if (raw === '' || raw == null) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : NaN;
}

/** 表单 → 提交给后端的档案对象（空字段不写，避免把默认值钉进配置）。 */
function toProfilePayload(draft) {
  const payload = {
    key: String(draft.key || '').trim(),
    title: String(draft.title || '').trim(),
    description: String(draft.description || '').trim(),
    capabilities: parseList(draft.capabilities),
    model: String(draft.model || '').trim(),
    fallback: parseList(draft.fallback),
    tools: draft.toolsMode === 'list' ? [...new Set(draft.tools || [])] : null,
    path_scope: parsePathScopes(draft.path_scope),
  };
  for (const [field] of NUMBER_FIELDS) {
    const value = toNumber(draft[field]);
    if (value != null && !Number.isNaN(value)) payload[field] = value;
  }
  return payload;
}

/**
 * 表单校验：返回 `{ errors, warnings }`（field → 文案）。
 * errors 阻止保存；warnings 允许保存但提醒后果。
 */
function validateDraft(draft, { existingKeys = [], editingKey = '' } = {}) {
  const errors = {};
  const warnings = [];
  const key = String(draft.key || '').trim();
  if (!key) errors.key = 'key 不能为空';
  else if (!KEY_RE.test(key)) {
    errors.key = '小写字母开头，只能包含小写字母、数字、- 和 _（最长 64 位）';
  } else if (RESERVED_KEYS.has(key)) {
    errors.key = `“${key}”是引擎保留名`;
  } else if (!editingKey && existingKeys.includes(key)) {
    errors.key = '已有同名档案；换个 key，或从卡片进入编辑';
  }

  for (const [field, label, rule] of NUMBER_FIELDS) {
    const raw = draft[field];
    if (raw === '' || raw == null) continue;
    const value = Number(raw);
    if (!Number.isFinite(value)) { errors[field] = `${label}必须是数字`; continue; }
    if (rule.integer && !Number.isInteger(value)) { errors[field] = `${label}必须是整数`; continue; }
    if (rule.exclusive && value <= rule.min) { errors[field] = `${label}必须大于 0`; continue; }
    if (!rule.exclusive && value < rule.min) { errors[field] = `${label}不能小于 ${rule.min}`; }
    if (rule.max != null && value > rule.max) errors[field] = `${label}不能大于 ${rule.max}`;
  }

  const contract = toNumber(draft.cost_ceiling_usd);
  const run = toNumber(draft.run_cost_ceiling_usd);
  if (Number.isFinite(contract) && Number.isFinite(run) && contract > run) {
    warnings.push(`单任务上限 $${contract} 高于整场上限 $${run}：单任务不可能获准这么多预算`);
  }

  const model = String(draft.model || '').trim();
  if (model && !model.includes(':') && !model.includes('/')) {
    errors.model = '模型要写成 provider:model（如 deepseek:deepseek-chat）';
  }

  if (!String(draft.title || '').trim() && !String(draft.description || '').trim()) {
    warnings.push('没有 title / description：规划菜单只能看到 key，PM 无法据此挑人');
  }
  if (draft.toolsMode === 'list' && !(draft.tools || []).length) {
    warnings.push('工具白名单为空：这个执行体将拿不到任何工具（只输出文本）');
  }
  const scopes = parsePathScopes(draft.path_scope);
  const badScope = scopes.find((s) => s.startsWith('/') || /^[A-Za-z]:/.test(s) || s.includes('..'));
  if (badScope) {
    errors.path_scope = `写入范围必须是产物内的相对路径：${badScope}`;
  }
  return { errors, warnings };
}

/**
 * 生效预览：给一个角色，算出这份档案真正会拿到什么。
 *
 * 与 `RunPlan` 的解析顺序一致：profile.model > role.model > 角色默认；
 * 工具取交集（角色 `*` 时直接用档案列表）。只用于表单提示，不参与执行。
 */
function effectivePreview(profile, role) {
  const p = profile || {};
  const r = role || {};
  const rows = [];
  const model = p.model || (r.model && r.model !== 'default' ? r.model : '');
  rows.push({
    k: '模型',
    v: model || '角色 / 全局默认',
    note: p.model ? '档案指定' : (r.model && r.model !== 'default' ? '继承角色' : '继承默认'),
  });

  const roleTools = Array.isArray(r.allowed_tools) ? r.allowed_tools : ['*'];
  let tools = roleTools;
  let toolsNote = '继承角色';
  if (Array.isArray(p.tools)) {
    tools = roleTools.includes('*')
      ? p.tools
      : p.tools.filter((t) => roleTools.includes(t));
    toolsNote = '档案 ∩ 角色';
  }
  rows.push({
    k: '工具',
    v: tools.includes('*') ? '全部（角色授权）' : (tools.length ? tools.join('、') : '无'),
    note: toolsNote,
  });
  rows.push({
    k: '写入范围',
    v: (p.path_scope || []).length ? p.path_scope.join('、') : '不限（仍受任务契约约束）',
    note: '',
  });
  rows.push({
    k: '成本',
    v: [p.cost_ceiling_usd != null ? `单任务 $${p.cost_ceiling_usd}` : '',
      p.run_cost_ceiling_usd != null ? `整场 $${p.run_cost_ceiling_usd}` : '']
      .filter(Boolean).join(' · ') || '仅受全局预算约束',
    note: '',
  });
  const warnings = [];
  if (Array.isArray(p.tools) && !roleTools.includes('*')) {
    const extra = p.tools.filter((t) => !roleTools.includes(t));
    if (extra.length) warnings.push(`角色没有这些工具：${extra.join('、')}（保存会被拒绝）`);
  }
  return { rows, warnings };
}

/** 搜索 + 状态筛选；filter ∈ all / bound / unbound / issue / file / yaml。 */
function filterProfiles(profiles, { query = '', filter = 'all', issues = new Set() } = {}) {
  const q = String(query || '').trim().toLowerCase();
  return (profiles || []).filter((p) => {
    if (filter === 'bound' && !(p.used_by || []).length) return false;
    if (filter === 'unbound' && (p.used_by || []).length) return false;
    if (filter === 'issue' && !issues.has(p.key)) return false;
    if (filter === 'file' && p.source !== 'agents.d') return false;
    if (filter === 'yaml' && p.source === 'agents.d') return false;
    if (!q) return true;
    const haystack = [p.key, p.title, p.description, p.model,
      ...(p.capabilities || []), ...(p.used_by || [])]
      .filter(Boolean).join(' ').toLowerCase();
    return haystack.includes(q);
  });
}

/** 页面顶部的摘要：档案数 / 已绑定 / 未绑定 / 有问题。 */
function summarizeProfiles(profiles, diagnostics) {
  const list = profiles || [];
  const issues = new Set();
  for (const item of (diagnostics && diagnostics.errors) || []) {
    if (item.profile) issues.add(item.profile);
  }
  for (const item of (diagnostics && diagnostics.warnings) || []) {
    if (item.profile) issues.add(item.profile);
  }
  return {
    total: list.length,
    bound: list.filter((p) => (p.used_by || []).length).length,
    unbound: list.filter((p) => !(p.used_by || []).length).length,
    issue: list.filter((p) => issues.has(p.key)).length,
    issues,
  };
}

function formatMoney(value) {
  if (value == null || Number.isNaN(Number(value))) return '—';
  const n = Number(value);
  if (n === 0) return '$0';
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function formatTokens(value) {
  const n = Number(value) || 0;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** ISO / `YYYY-MM-DD HH:mm:ss+00:00` → 相对时间（超过 7 天显示日期）。 */
function relativeTime(stamp, now = Date.now()) {
  if (!stamp) return '—';
  const text = String(stamp).trim().replace(' ', 'T');
  const time = Date.parse(text);
  if (Number.isNaN(time)) return String(stamp);
  const diff = Math.max(0, now - time);
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return '刚刚';
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`;
  if (diff < 7 * day) return `${Math.floor(diff / day)} 天前`;
  return new Date(time).toLocaleDateString('zh-CN');
}

export {
  draftFromProfile, effectivePreview, emptyDraft, filterProfiles, formatMoney,
  formatTokens, parseList, parsePathScopes, relativeTime, summarizeProfiles,
  toProfilePayload, validateDraft, KEY_RE,
};
