import { test } from 'node:test';
import assert from 'node:assert';

const {
  draftFromProfile, effectivePreview, emptyDraft, filterProfiles, formatMoney,
  formatTokens, parseList, relativeTime, summarizeProfiles, toProfilePayload,
  validateDraft,
} = await import('../src/lib/profile-form.js');

const PROFILE = {
  key: 'fast-writer', title: '快速写手', description: '短文档',
  capabilities: ['text', 'summary'], model: 'deepseek:deepseek-chat',
  fallback: ['openai:gpt-4o-mini'], tools: ['read_file'], path_scope: ['src/'],
  cost_ceiling_usd: 0.05, run_cost_ceiling_usd: 0.5, max_rpm: 30,
  max_concurrency: 2, temperature: 0.3, used_by: ['backend_dev'],
  source: 'agents.d',
};

test('draftFromProfile 与 toProfilePayload 往返保留字段类型', () => {
  const draft = draftFromProfile(PROFILE);
  assert.equal(draft.capabilities, 'text、summary');
  assert.equal(draft.toolsMode, 'list');
  const payload = toProfilePayload(draft);
  assert.deepEqual(payload.capabilities, ['text', 'summary']);
  assert.deepEqual(payload.tools, ['read_file']);
  assert.deepEqual(payload.path_scope, ['src/']);
  assert.equal(payload.cost_ceiling_usd, 0.05);
  assert.equal(payload.max_rpm, 30);
  assert.equal(payload.temperature, 0.3);
});

test('继承工具时提交 tools=null，而不是空数组', () => {
  const draft = { ...emptyDraft(), key: 'writer', toolsMode: 'inherit', tools: ['read_file'] };
  assert.equal(toProfilePayload(draft).tools, null);
  const list = { ...draft, toolsMode: 'list' };
  assert.deepEqual(toProfilePayload(list).tools, ['read_file']);
});

test('validateDraft：key 规则 / 保留名 / 重名', () => {
  const base = { ...emptyDraft(), key: 'Bad Key' };
  assert.ok(validateDraft(base).errors.key);
  assert.ok(validateDraft({ ...base, key: 'default' }).errors.key.includes('保留'));
  assert.ok(validateDraft(
    { ...base, key: 'writer' }, { existingKeys: ['writer'] }).errors.key.includes('同名'));
  assert.deepEqual(
    validateDraft({ ...base, key: 'writer' }, { existingKeys: ['writer'], editingKey: 'writer' }).errors,
    {});
});

test('validateDraft：模型别名、数字边界与预算冲突', () => {
  const draft = { ...emptyDraft(), key: 'writer', model: 'deepseek-chat' };
  assert.ok(validateDraft(draft).errors.model);

  const negative = { ...emptyDraft(), key: 'writer', max_rpm: '-1' };
  assert.ok(validateDraft(negative).errors.max_rpm);

  const fraction = { ...emptyDraft(), key: 'writer', max_concurrency: '1.5' };
  assert.ok(validateDraft(fraction).errors.max_concurrency);

  const hot = { ...emptyDraft(), key: 'writer', temperature: '3' };
  assert.ok(validateDraft(hot).errors.temperature);

  const budget = { ...emptyDraft(), key: 'writer', cost_ceiling_usd: '0.5', run_cost_ceiling_usd: '0.2' };
  assert.ok(validateDraft(budget).warnings.some((w) => w.includes('高于整场上限')));
});

test('validateDraft：绝对路径与 .. 被拒绝', () => {
  const draft = { ...emptyDraft(), key: 'writer', path_scope: '/etc/\n../secret' };
  assert.ok(validateDraft(draft).errors.path_scope);
});

test('effectivePreview：模型继承顺序与工具交集', () => {
  const role = { role: 'backend_dev', model: 'default', allowed_tools: ['read_file', 'write_file'] };
  const inherited = effectivePreview({}, role);
  assert.equal(inherited.rows[0].v, '角色 / 全局默认');
  assert.equal(inherited.rows[1].v, 'read_file、write_file');

  const narrowed = effectivePreview(
    { model: 'deepseek:deepseek-chat', tools: ['read_file', 'grep'] }, role);
  assert.equal(narrowed.rows[0].v, 'deepseek:deepseek-chat');
  assert.equal(narrowed.rows[1].v, 'read_file');
  assert.ok(narrowed.warnings.some((w) => w.includes('grep')));
});

test('effectivePreview：角色授权 * 时直接采用档案列表', () => {
  const preview = effectivePreview({ tools: ['grep'] }, { allowed_tools: ['*'] });
  assert.equal(preview.rows[1].v, 'grep');
  assert.deepEqual(preview.warnings, []);
});

test('filterProfiles：搜索 key/名称/能力/绑定角色，以及状态筛选', () => {
  const profiles = [
    PROFILE,
    { key: 'reviewer', title: '复核者', capabilities: ['review'], used_by: [], source: 'yaml' },
  ];
  assert.deepEqual(filterProfiles(profiles, { query: '复核' }).map((p) => p.key), ['reviewer']);
  assert.deepEqual(filterProfiles(profiles, { query: 'backend_dev' }).map((p) => p.key), ['fast-writer']);
  assert.deepEqual(filterProfiles(profiles, { filter: 'unbound' }).map((p) => p.key), ['reviewer']);
  assert.deepEqual(filterProfiles(profiles, { filter: 'file' }).map((p) => p.key), ['fast-writer']);
  assert.deepEqual(filterProfiles(profiles, { filter: 'issue', issues: new Set(['reviewer']) })
    .map((p) => p.key), ['reviewer']);
});

test('summarizeProfiles：绑定/未绑定/问题数', () => {
  const diagnostics = {
    errors: [{ profile: 'fast-writer' }],
    warnings: [{ profile: 'fast-writer' }, { role: 'backend_dev' }],
  };
  const summary = summarizeProfiles([
    PROFILE, { key: 'reviewer', used_by: [] },
  ], diagnostics);
  assert.deepEqual(
    { total: summary.total, bound: summary.bound, unbound: summary.unbound, issue: summary.issue },
    { total: 2, bound: 1, unbound: 1, issue: 1 });
  assert.ok(summary.issues.has('fast-writer'));
});

test('格式化：parseList 去重、金额/Token/相对时间', () => {
  assert.deepEqual(parseList('a, b、a\nc'), ['a', 'b', 'c']);
  assert.equal(formatMoney(0), '$0');
  assert.equal(formatMoney(0.004), '$0.0040');
  assert.equal(formatMoney(1.234), '$1.23');
  assert.equal(formatTokens(1234), '1.2k');
  assert.equal(formatTokens(2_400_000), '2.4M');
  const now = Date.parse('2026-11-20T12:00:00Z');
  assert.equal(relativeTime('2026-11-20 11:59:30+00:00', now), '刚刚');
  assert.equal(relativeTime('2026-11-20 11:00:00+00:00', now), '1 小时前');
  assert.equal(relativeTime('', now), '—');
});
