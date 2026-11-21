import { test } from 'node:test';
import assert from 'node:assert';

// contracts/providers.js 会把目录挂到 window 上（渲染进程用），Node 里先给个壳
globalThis.window = globalThis.window || {};

const { BUILTIN_PROVIDERS, MODEL_ALIASES, providerType } = await import('../src/contracts/providers.js');
const { sortProviders } = await import('../src/lib/providers.js');

test('providerType：mock 是内部替身，CLI 是 Agent，Ollama/OpenAI 是大模型', () => {
  assert.equal(providerType({ key: 'mock' }), 'internal');
  assert.equal(providerType({ key: 'cli' }), 'agent');
  assert.equal(providerType({ key: 'claude-code' }), 'agent');
  assert.equal(providerType({ key: 'codex' }), 'agent');
  assert.equal(providerType({ key: 'ollama' }), 'llm');
  assert.equal(providerType({ key: 'openai' }), 'llm');
  assert.equal(providerType({ key: 'my-local', family: 'cli' }), 'agent');
  assert.equal(providerType({ key: 'my-mock', family: 'mock' }), 'internal');
});

test('内置目录：Mock 被标为内部，Agent 与模型供应商数量对得上', () => {
  const llm = BUILTIN_PROVIDERS.filter(p => p.type === 'llm');
  const agents = BUILTIN_PROVIDERS.filter(p => p.type === 'agent');
  const internal = BUILTIN_PROVIDERS.filter(p => p.type === 'internal');
  assert.ok(llm.length >= 20, `大模型供应商只有 ${llm.length} 个`);
  assert.deepEqual(agents.map(p => p.key).sort(), ['claude-code', 'cli', 'codex']);
  assert.deepEqual(internal.map(p => p.key), ['mock']);
});

test('默认模型别名只收大模型：不出现 Agent 与 Mock', () => {
  const providers = new Set(MODEL_ALIASES.map(m => m.provider));
  assert.equal(providers.has('claude-code'), false);
  assert.equal(providers.has('codex'), false);
  assert.equal(providers.has('cli'), false);
  assert.equal(providers.has('mock'), false);
  assert.equal(providers.has('openai'), true);
});

test('排序：大模型在前，分类按 国内→国际→聚合→本地，同组按名称', () => {
  const list = [
    { key: 'x-agent', label: 'Beta Agent', type: 'agent', category: 'specialized' },
    { key: 'x-local', label: 'Local B', type: 'llm', category: 'local' },
    { key: 'x-int-b', label: 'Beta', type: 'llm', category: 'international' },
    { key: 'x-agg', label: 'Agg', type: 'llm', category: 'aggregator' },
    { key: 'x-nat-b', label: 'Zeta', type: 'llm', category: 'national' },
    { key: 'x-int-a', label: 'Alpha', type: 'llm', category: 'international' },
    { key: 'x-nat-a', label: 'DeepSeek', type: 'llm', category: 'national' },
  ];
  assert.deepEqual(sortProviders(list).map(p => p.key), [
    'x-nat-a', 'x-nat-b', 'x-int-a', 'x-int-b', 'x-agg', 'x-local', 'x-agent',
  ]);
});

test('排序不改原数组，未知分类排到本组末尾', () => {
  const list = [
    { key: 'u', label: 'Unknown', type: 'llm', category: 'weird' },
    { key: 'l', label: 'Local', type: 'llm', category: 'local' },
  ];
  const out = sortProviders(list);
  assert.deepEqual(out.map(p => p.key), ['l', 'u']);
  assert.deepEqual(list.map(p => p.key), ['u', 'l']);
});

test('组内常用优先：国内 DeepSeek/豆包/通义 排在未收录供应商之前', () => {
  const list = [
    { key: 'x-unknown', label: 'AAA', type: 'llm', category: 'national' },
    { key: 'qwen', label: '通义千问', type: 'llm', category: 'national' },
    { key: 'deepseek', label: 'DeepSeek', type: 'llm', category: 'national' },
    { key: 'doubao', label: '豆包', type: 'llm', category: 'national' },
  ];
  assert.deepEqual(sortProviders(list).map(p => p.key), ['deepseek', 'doubao', 'qwen', 'x-unknown']);
});

test('按公开调用量排序：DeepSeek → OpenAI → Gemini → 智谱 → Anthropic → 通义 → Mistral', () => {
  const llm = BUILTIN_PROVIDERS.filter(p => p.type === 'llm');
  assert.deepEqual(sortProviders(llm).slice(0, 7).map(p => p.key),
    ['deepseek', 'openai', 'gemini', 'zhipu', 'anthropic', 'qwen', 'mistral']);
});

test('调用量口径：Anthropic 请求份额低于 OpenAI，但企业 API 支出份额更高', () => {
  const byKey = Object.fromEntries(BUILTIN_PROVIDERS.map(p => [p.key, p]));
  assert.ok(byKey.anthropic.usage.rank > byKey.openai.usage.rank);
  assert.ok(byKey.anthropic.usage.share < byKey.openai.usage.share);
  assert.ok(byKey.anthropic.usage.spendShare > byKey.openai.usage.spendShare);
});

test('Agent 按 Token 份额排序：Claude Code → Codex CLI → 自定义 CLI（无数据）', () => {
  const agents = BUILTIN_PROVIDERS.filter(p => p.type === 'agent');
  assert.deepEqual(sortProviders(agents).map(p => p.key), ['claude-code', 'codex', 'cli']);
  const cli = agents.find(p => p.key === 'cli');
  assert.equal(cli.usage, null);
});
