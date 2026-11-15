import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  buildGuidanceBody,
  canWithdraw,
  currentRunId,
  explainGuidanceError,
  guidanceMeta,
  guidanceTaskOptions,
  isExpired,
  normalizeGuidance,
  normalizeGuidanceItem,
} from '../src/lib/guidance.js';

test('normalizeGuidanceItem 兼容切片一的旧响应', () => {
  const item = normalizeGuidanceItem({ id: 'GD-1', text: '改绿色', author: 'ops' });
  assert.equal(item.status, 'pending');
  assert.equal(item.label, '待吸收');
  assert.equal(item.target, null);
  assert.equal(item.expires_at, null);
});

test('状态优先级：撤回 > 已吸收 > 过期 > 待吸收', () => {
  const now = Date.parse('2026-11-15T00:00:00Z');
  assert.equal(normalizeGuidanceItem({
    id: 'a', text: 'x', consumed_at: 't', withdrawn_at: 't',
  }, now).status, 'withdrawn');
  assert.equal(normalizeGuidanceItem({
    id: 'b', text: 'x', consumed_at: 't',
    expires_at: '2026-11-14T00:00:00Z',
  }, now).status, 'consumed');
  assert.equal(normalizeGuidanceItem({
    id: 'c', text: 'x', expires_at: '2026-11-14T00:00:00Z',
  }, now).status, 'expired');
  assert.equal(normalizeGuidanceItem({
    id: 'd', text: 'x', expires_at: '2026-11-16T00:00:00Z',
  }, now).status, 'pending');
});

test('normalizeGuidance 处理空响应与 pending 计数', () => {
  assert.deepEqual(normalizeGuidance(null), { runId: '', items: [], pending: 0 });
  const out = normalizeGuidance({
    run_id: 'R1',
    pending: 0,
    items: [{ id: 'a', text: 'x', status: 'withdrawn' }, { id: 'b', text: 'y' }],
  });
  assert.equal(out.runId, 'R1');
  assert.equal(out.items.length, 2);
  assert.equal(out.pending, 0);
  assert.equal(normalizeGuidance({ items: [{ id: 'a' }, null] }).pending, 1);
});

test('buildGuidanceBody 只在该有的时候带扩展字段', () => {
  assert.deepEqual(buildGuidanceBody({ text: '  改绿色  ' }), {
    text: '改绿色', author: 'studio',
  });
  assert.deepEqual(
    buildGuidanceBody({ text: 'x', target: 'T-003', expiresMinutes: '30' }),
    { text: 'x', author: 'studio', target: 'T-003', expires_in_seconds: 1800 });
  assert.equal(buildGuidanceBody({ text: 'x', expiresMinutes: 0 }).expires_in_seconds, undefined);
});

test('guidanceTaskOptions 永远带不定向项并跳过脏数据', () => {
  const options = guidanceTaskOptions([
    { id: 'T-001', title: '登录页' }, null, { id: 'T-002' }, { title: '无 id' },
  ]);
  assert.equal(options[0].value, '');
  assert.equal(options.length, 3);
  assert.equal(options[1].label, 'T-001 · 登录页');
});

test('canWithdraw 只放行 pending', () => {
  assert.equal(canWithdraw({ status: 'pending' }), true);
  assert.equal(canWithdraw({ status: 'consumed' }), false);
  assert.equal(canWithdraw(null), false);
});

test('过期判定与 meta 文案', () => {
  const now = Date.parse('2026-11-15T00:00:00Z');
  assert.equal(isExpired('2026-11-14T23:59:59Z', now), true);
  assert.equal(isExpired('not-a-date', now), false);
  assert.equal(isExpired(null, now), false);
  const meta = guidanceMeta({ target: 'T-2', expires_at: 'x', author: 'ops' });
  assert.match(meta, /定向 T-2/);
  assert.match(meta, /by ops/);
});

test('错误码翻译成可操作文案', () => {
  assert.match(explainGuidanceError({ code: 'HTTP_429' }), /上限/);
  assert.match(explainGuidanceError({ code: 'NO_API' }), /设置/);
  assert.match(explainGuidanceError({ code: 'HTTP_500', detail: 'boom' }), /boom/);
});

test('currentRunId 从 run_id / dir / watchDir 里取最后一段', () => {
  assert.equal(currentRunId({ run_id: 'R-1' }, ''), 'R-1');
  assert.equal(currentRunId({ dir: 'E:\\agentcrew\\output\\R-2' }, ''), 'R-2');
  assert.equal(currentRunId(null, '/tmp/output/R-3'), 'R-3');
  assert.equal(currentRunId(null, ''), '');
});
