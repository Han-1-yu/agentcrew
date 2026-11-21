import { test } from 'node:test';
import assert from 'node:assert';

import { applyTransition, transitionPatch } from '../src/lib/task-live.js';

test('to_status 优先，缺省回落到 status', () => {
  assert.deepEqual(transitionPatch({ task_id: 'T-1', to_status: 'running' }), { status: 'running' });
  assert.deepEqual(transitionPatch({ task_id: 'T-1', status: 'failed' }), { status: 'failed' });
  const both = transitionPatch({ task_id: 'T-1', status: 'running', to_status: 'succeeded' });
  assert.equal(both.status, 'succeeded');
});

test('只带事件里出现的字段：不把 attempts / error 抹成 undefined', () => {
  const patch = transitionPatch({ task_id: 'T-1', to_status: 'failed', attempts: 2, error: 'boom' });
  assert.equal(patch.attempts, 2);
  assert.equal(patch.error, 'boom');
  const bare = transitionPatch({ task_id: 'T-1', to_status: 'running' });
  assert.deepEqual(Object.keys(bare), ['status']);
  assert.deepEqual(transitionPatch({ task_id: 'T-1', to_status: 'failed', error: '   ' }), { status: 'failed' });
});

test('非法 / 缺失状态返回 null（不写坏任务）', () => {
  assert.equal(transitionPatch(null), null);
  assert.equal(transitionPatch({}), null);
  assert.equal(transitionPatch({ task_id: 'T-1', to_status: 'bogus' }), null);
  assert.equal(transitionPatch({ task_id: 'T-1' }), null);
});

test('applyTransition 返回 from/to 并落地字段', () => {
  const task = { id: 'T-1', status: 'ready', attempts: 1 };
  const move = applyTransition(task, { status: 'running', attempts: 2 });
  assert.deepEqual(move, { from: 'ready', to: 'running' });
  assert.equal(task.status, 'running');
  assert.equal(task.attempts, 2);
  assert.equal(applyTransition(null, { status: 'running' }), null);
});
