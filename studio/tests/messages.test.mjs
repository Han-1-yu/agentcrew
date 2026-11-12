import { test } from 'node:test';
import assert from 'node:assert';
import { buildMessages } from '../src/lib/messages.js';

test('run_started 映射为 user 消息', () => {
  const events = [{ ts: 't', event: 'run_started', data: { goal: '做一个 Todo 应用' } }];
  const { msgs } = buildMessages(events, [], {});
  assert.equal(msgs.length, 1);
  assert.equal(msgs[0].type, 'user');
  assert.equal(msgs[0].goal, '做一个 Todo 应用');
});

test('plan_created 映射为 system 消息', () => {
  const events = [{ ts: 't', event: 'plan_created', data: { task_count: 8, roles: ['a', 'b'] } }];
  const { msgs } = buildMessages(events, [], {});
  assert.equal(msgs[0].type, 'system');
  assert.match(msgs[0].text, /8 个任务/);
});

test('同一 task 的事件聚合为一个 agent 消息，items 按序', () => {
  const events = [
    { ts: 't1', event: 'task_started', data: { task_id: 'T-1', role: 'backend_dev' } },
    { ts: 't2', event: 'tool_call', data: { task_id: 'T-1', tool_name: 'read_file', args: { path: 'x' }, success: true } },
    { ts: 't3', event: 'task_finished', data: { task_id: 'T-1', duration_ms: 100 } },
  ];
  const tasks = [{ id: 'T-1', role: 'backend_dev', status: 'succeeded', title: '后端' }];
  const { msgs, taskIndex } = buildMessages(events, tasks, {});
  const agents = msgs.filter((m) => m.type === 'agent');
  assert.equal(agents.length, 1);
  assert.equal(agents[0].taskId, 'T-1');
  assert.deepEqual(agents[0].items.map((i) => i.kind), ['tool', 'finish']);
  assert.equal(taskIndex['T-1'].title, '后端');
});

test('消息按事件到达顺序排序（含并发任务交错）', () => {
  const events = [
    { ts: 't1', event: 'run_started', data: { goal: 'g' } },
    { ts: 't2', event: 'task_started', data: { task_id: 'A', role: 'r' } },
    { ts: 't3', event: 'task_started', data: { task_id: 'B', role: 'r' } },
    { ts: 't4', event: 'task_finished', data: { task_id: 'A' } },
    { ts: 't5', event: 'task_finished', data: { task_id: 'B' } },
  ];
  const { msgs } = buildMessages(events, [], {});
  const kinds = msgs.map((m) => (m.type === 'user' ? 'user' : m.taskId));
  assert.deepEqual(kinds, ['user', 'A', 'B']);
  assert.equal(msgs[1].items.length, 1); // A 只有 finish
  assert.equal(msgs[2].items.length, 1); // B 只有 finish
});

test('run_finished 的终态映射 kind（completed→done / failed→error）', () => {
  const ok = buildMessages([{ ts: 't', event: 'run_finished', data: { status: 'completed' } }], [], {});
  assert.equal(ok.msgs[0].kind, 'done');
  const bad = buildMessages([{ ts: 't', event: 'run_finished', data: { status: 'failed' } }], [], {});
  assert.equal(bad.msgs[0].kind, 'error');
});

test('空输入与无 task_id 事件不产生消息', () => {
  assert.equal(buildMessages([], [], {}).msgs.length, 0);
  assert.equal(buildMessages([{ ts: 't', event: 'unknown', data: {} }], [], {}).msgs.length, 0);
});
