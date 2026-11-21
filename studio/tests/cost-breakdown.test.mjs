import { test } from 'node:test';
import assert from 'node:assert';

import { aggregateCostByModel, providerOf } from '../src/lib/cost-breakdown.js';

const call = (model, cost, over = {}) => ({
  event: 'llm_call',
  data: { model, estimated_cost_usd: cost, prompt_tokens: 100, completion_tokens: 50, ...over },
});

test('providerOf：别名前缀优先，其次用 default_model 同模型匹配', () => {
  assert.equal(providerOf('deepseek:deepseek-chat', ''), 'deepseek');
  assert.equal(providerOf('deepseek-chat', 'deepseek:deepseek-chat'), 'deepseek');
  assert.equal(providerOf('deepseek-chat', 'openai:gpt-6'), '');
  assert.equal(providerOf('', 'deepseek:deepseek-chat'), '');
});

test('按模型聚合成本 / token / 调用次数，并按成本降序', () => {
  const res = aggregateCostByModel([
    call('deepseek-chat', 0.002, { role: 'product_manager' }),
    call('deepseek-chat', 0.003),
    call('gpt-6', 0.01),
  ], { defaultModel: 'deepseek:deepseek-chat' });
  assert.deepEqual(res.rows.map((r) => r.key), ['gpt-6', 'deepseek/deepseek-chat']);
  const ds = res.rows.find((r) => r.model === 'deepseek-chat');
  assert.equal(ds.calls, 2);
  assert.equal(ds.prompt, 200);
  assert.equal(ds.completion, 100);
  assert.ok(Math.abs(ds.cost - 0.005) < 1e-9);
  assert.ok(Math.abs(res.total - 0.015) < 1e-9);
});

test('limit 截断并返回 restCount；limit<=0 不截断', () => {
  const events = [call('a', 0.03), call('b', 0.02), call('c', 0.01)];
  const cut = aggregateCostByModel(events, { limit: 2 });
  assert.equal(cut.rows.length, 2);
  assert.equal(cut.restCount, 1);
  assert.equal(aggregateCostByModel(events, { limit: 0 }).rows.length, 3);
});

test('忽略非 llm_call 与脏数据，空输入不抛错', () => {
  const res = aggregateCostByModel([
    { event: 'task_transition', data: { model: 'x', estimated_cost_usd: 9 } },
    null,
    { event: 'llm_call' },
    { event: 'llm_call', data: { model: 'm', estimated_cost_usd: 'bad' } },
  ]);
  // 缺 data 的 llm_call 仍是一条真实调用，归到「未知模型」而不是丢掉。
  assert.equal(res.rows.length, 2);
  assert.ok(res.rows.some((r) => r.model === '未知模型'));
  assert.ok(res.rows.every((r) => r.cost === 0));
  assert.equal(res.total, 0);
  assert.deepEqual(aggregateCostByModel(null).rows, []);
});
