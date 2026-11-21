import { test } from 'node:test';
import assert from 'node:assert';

import { TEMPLATES } from '../src/features/profiles/templates.js';

const KEY_RE = /^[a-z][a-z0-9_-]{0,63}$/;

test('内置档案模板：覆盖写作/复核/代码/数据分析四类', () => {
  const keys = TEMPLATES.filter((t) => t.draft.key).map((t) => t.draft.key);
  assert.ok(keys.length >= 5, '至少 5 个可导入预设（含四类典型角色）');
  assert.equal(new Set(keys).size, keys.length, '模板 key 不能重复');
  assert.ok(keys.includes('data-analyst'), '数据分析类缺失');
});

test('内置档案模板：key 合法、描述非空、数字字段是数字', () => {
  for (const tpl of TEMPLATES) {
    if (!tpl.draft.key) continue;
    assert.match(tpl.draft.key, KEY_RE, `${tpl.label} key 不合法`);
    assert.ok(String(tpl.draft.title || '').trim(), `${tpl.label} 缺名称`);
    assert.ok(String(tpl.draft.description || '').trim(), `${tpl.label} 缺描述`);
    for (const field of ['cost_ceiling_usd', 'run_cost_ceiling_usd', 'temperature', 'max_output_tokens']) {
      const value = tpl.draft[field];
      if (value == null) continue;
      assert.equal(typeof value, 'number', `${tpl.label}.${field} 必须是数字`);
    }
  }
});
