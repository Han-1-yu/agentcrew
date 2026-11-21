import { test } from 'node:test';
import assert from 'node:assert';

import { cmpTaskId, diffSummary, diffTaskPlans, normDeps } from '../src/lib/task-diff.js';

const task = (id, over = {}) => ({
  id, title: `任务 ${id}`, role: 'backend_dev', status: 'succeeded',
  priority: 3, dependencies: [], ...over,
});

test('同义状态（passed / succeeded）不算变更', () => {
  const diff = diffTaskPlans(
    [task('T-001', { status: 'succeeded' })],
    [task('T-001', { status: 'passed' })]);
  assert.equal(diff.changed, 0);
  assert.equal(diff.same, 1);
  assert.equal(diff.rows[0].kind, 'same');
});

test('新增与移除按 id 对齐，标题/状态差异都能读出来', () => {
  const diff = diffTaskPlans(
    [task('T-001'), task('T-002', { status: 'succeeded', title: '旧标题' }), task('T-004')],
    [task('T-001', { status: 'passed' }), task('T-002', { status: 'failed' }), task('T-003')]);
  assert.deepEqual([diff.added, diff.removed, diff.changed, diff.same], [1, 1, 1, 1]);
  const changed = diff.rows.find((r) => r.kind === 'changed');
  assert.equal(changed.id, 'T-002');
  assert.ok(changed.changes.some((c) => c.includes('状态 succeeded→failed')));
  assert.ok(changed.changes.some((c) => c.includes('标题「旧标题」→「任务 T-002」')));
  const added = diff.rows.find((r) => r.kind === 'added');
  assert.equal(added.left, null);
  assert.equal(added.right.id, 'T-003');
  const removed = diff.rows.find((r) => r.kind === 'removed');
  assert.equal(removed.id, 'T-004');
  assert.equal(removed.right, null);
});

test('角色 / 优先级 / 依赖变更都进差异说明', () => {
  const diff = diffTaskPlans(
    [task('T-001', { role: 'qa_engineer', priority: 5, dependencies: ['T-002', 'T-003'] })],
    [task('T-001', { role: 'backend_dev', priority: 3, dependencies: ['T-003'] })]);
  const changes = diff.rows[0].changes;
  assert.equal(diff.changed, 1);
  assert.ok(changes.some((c) => c.includes('角色 qa_engineer→backend_dev')));
  assert.ok(changes.some((c) => c.includes('优先级 5→3')));
  assert.ok(changes.some((c) => c.includes('依赖 T-002,T-003→T-003')));
});

test('依赖只看集合：顺序不同 / 重复项都不算变更', () => {
  assert.equal(normDeps(['T-003', 'T-002']), normDeps(['T-002', 'T-003', 'T-002']));
  const diff = diffTaskPlans(
    [task('T-001', { dependencies: ['T-003', 'T-002'] })],
    [task('T-001', { dependencies: ['T-002', 'T-003'] })]);
  assert.equal(diff.changed, 0);
});

test('任务 id 自然序：数字尾巴按数值排，无数字的垫后', () => {
  const ids = ['T-FINAL', 'T-010', 'T-2', 'T-001'].sort(cmpTaskId);
  assert.deepEqual(ids, ['T-001', 'T-2', 'T-010', 'T-FINAL']);
});

test('diffSummary：先报变化，全一致时给「N 项一致」，空表单独说明', () => {
  const diff = diffTaskPlans([task('T-001')], [task('T-001')]);
  assert.equal(diffSummary(diff), '1 项一致');
  const mixed = diffTaskPlans([task('T-001'), task('T-002')], [task('T-002', { status: 'failed' }), task('T-003')]);
  assert.equal(diffSummary(mixed), '1 新增 · 1 移除 · 1 变更');
  assert.equal(diffSummary(diffTaskPlans([], [])), '两次运行都没有任务表');
});

test('两侧都空 / 只有一侧有任务表时不抛错', () => {
  assert.equal(diffTaskPlans(null, undefined).rows.length, 0);
  const onlyRight = diffTaskPlans([], [task('T-001')]);
  assert.equal(onlyRight.added, 1);
  assert.equal(onlyRight.rows[0].right.id, 'T-001');
  assert.equal(onlyRight.rows[0].left, null);
});
