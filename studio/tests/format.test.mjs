import { test } from 'node:test';
import assert from 'node:assert';

import { fmtBudgetUsage } from '../src/lib/format.js';

test('预算预警：按事件里的 pct / 金额出文案', () => {
  assert.equal(
    fmtBudgetUsage({ current_cost_usd: 0.8, limit_usd: 1, pct: 0.8 }),
    '已用 $0.80 / $1.00（80%）');
  // 旧事件只有 threshold 时也能给出百分比
  assert.equal(
    fmtBudgetUsage({ current_cost_usd: 0.9, limit_usd: 1, threshold: 0.9 }),
    '已用 $0.90 / $1.00（90%）');
});

test('预算预警：缺上限只报百分比，不编造金额', () => {
  assert.equal(fmtBudgetUsage({ pct: 0.9 }), '已用 90%');
  assert.equal(fmtBudgetUsage({}), '已用 90%');
});

test('预算耗尽：用 used / limit 字段', () => {
  assert.equal(fmtBudgetUsage({ used: 1.2, limit: 1 }, true), '已用 $1.20 / 上限 $1.00');
  assert.equal(fmtBudgetUsage({ used: 1.2 }, true), '已用 $1.20');
  assert.equal(fmtBudgetUsage(null, true), '已用 $0.00');
});
