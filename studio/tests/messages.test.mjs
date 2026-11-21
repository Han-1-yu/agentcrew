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

test('缺少 task_notes 时用 task.result.summary 兜底为正文', () => {
  const events = [
    { ts: 't1', event: 'task_started', data: { task_id: 'T-9', role: 'architect' } },
    { ts: 't2', event: 'task_finished', data: { task_id: 'T-9', duration_ms: 10 } },
  ];
  const tasks = [{ id: 'T-9', role: 'architect', status: 'succeeded', result: { summary: '完成架构设计' } }];
  const { msgs } = buildMessages(events, tasks, {});
  assert.equal(msgs[0].items[0].kind, 'note');
  assert.equal(msgs[0].items[0].text, '完成架构设计');
});

test('已有 task_notes 时不用 result.summary 重复填充', () => {
  const events = [
    { ts: 't1', event: 'task_notes', data: { task_id: 'T-8', role: 'qa_engineer', notes: '真实正文' } },
    { ts: 't2', event: 'task_finished', data: { task_id: 'T-8' } },
  ];
  const tasks = [{ id: 'T-8', role: 'qa_engineer', status: 'succeeded', result: { summary: '摘要不该出现' } }];
  const { msgs } = buildMessages(events, tasks, {});
  const notes = msgs[0].items.filter((i) => i.kind === 'note');
  assert.equal(notes.length, 1);
  assert.equal(notes[0].text, '真实正文');
});

/* ---- A3：guidance 气泡 / 指引审计事件 / 图片附件 ---- */

const gItem = (id, text, ts, over = {}) => ({
  id, text, ts, status: 'pending', target: null, consumedTask: '', ...over,
});

test('guidance 附加层渲染为 user 指引气泡，并按 ts 插进时间线', () => {
  const events = [
    { ts: '2026-11-21T10:00:00', event: 'run_started', data: { goal: 'g' } },
    { ts: '2026-11-21T11:00:00', event: 'task_started', data: { task_id: 'T-1', role: 'backend_dev' } },
  ];
  const { msgs } = buildMessages(events, [], {}, {
    guidance: [gItem('g1', '注意错误处理', '2026-11-21T10:30:00')],
  });
  assert.equal(msgs.length, 3);
  assert.deepEqual(
    msgs.map((m) => (m.type === 'user' ? (m.subtype === 'guidance' ? `user:${m.gid}` : 'goal') : m.taskId)),
    ['goal', 'user:g1', 'T-1']);
  const bubble = msgs[1];
  assert.equal(bubble.subtype, 'guidance');
  assert.equal(bubble.text, '注意错误处理');
  assert.equal(bubble.status, 'pending');
});

test('guidance_injected 命中已知指引 → 只更新吸收 chip，不产生 system 消息', () => {
  const events = [
    { ts: '2026-11-21T10:00:00', event: 'guidance_injected', data: { ids: ['g1'], count: 1, task_id: 'T-2' } },
  ];
  const { msgs } = buildMessages(events, [], {}, {
    guidance: [gItem('g1', 'x', '2026-11-21T09:00:00')],
  });
  const bubble = msgs.find((m) => m.subtype === 'guidance');
  assert.ok(bubble);
  assert.equal(bubble.consumedTask, 'T-2');
  assert.equal(msgs.filter((m) => m.type === 'system').length, 0);
});

test('guidance_injected 全未命中（外部客户端）→ system 消息，不造空 agent 消息', () => {
  const events = [
    { ts: 't', event: 'guidance_injected', data: { ids: ['zzz'], count: 1, task_id: 'T-3' } },
  ];
  const { msgs } = buildMessages(events, [], {});
  assert.equal(msgs.length, 1);
  assert.equal(msgs[0].type, 'system');
  assert.match(msgs[0].text, /T-3/);
  assert.equal(msgs.filter((m) => m.type === 'agent').length, 0);
});

test('guidance_expired / withdrawn / compacted 的状态回写与兜底 system 消息', () => {
  const events = [
    { ts: 't1', event: 'guidance_expired', data: { ids: ['g1'], count: 1 } },
    { ts: 't2', event: 'guidance_withdrawn', data: { id: 'g2' } },
    { ts: 't3', event: 'guidance_withdrawn', data: { id: 'unknown-id' } },
    { ts: 't4', event: 'guidance_compacted', data: {} },
  ];
  const { msgs } = buildMessages(events, [], {}, {
    guidance: [gItem('g1', 'a', 't0'), gItem('g2', 'b', 't0')],
  });
  const byId = new Map(msgs.filter((m) => m.subtype === 'guidance').map((m) => [m.gid, m]));
  assert.equal(byId.get('g1').status, 'expired');
  assert.equal(byId.get('g2').status, 'withdrawn');
  const sys = msgs.filter((m) => m.type === 'system').map((m) => m.text);
  assert.ok(sys.some((t) => /过期未吸收/.test(t)));
  assert.ok(sys.some((t) => /撤回一条/.test(t))); // 未知 id 的撤回
  assert.ok(sys.some((t) => /压缩/.test(t)));
});

test('已吸收的指引被 withdrawn 事件命中时不改成 withdrawn', () => {
  const events = [
    { ts: 't1', event: 'guidance_withdrawn', data: { id: 'g1' } },
  ];
  const { msgs } = buildMessages(events, [], {}, {
    guidance: [gItem('g1', 'a', 't0', { status: 'consumed' })],
  });
  assert.equal(msgs.find((m) => m.subtype === 'guidance').status, 'consumed');
});

test('乐观插入（无 ts）的指引排到时间线末尾', () => {
  const events = [{ ts: 't1', event: 'run_started', data: { goal: 'g' } }];
  const { msgs } = buildMessages(events, [], {}, {
    guidance: [gItem('g9', '刚发的', '')],
  });
  assert.equal(msgs.length, 2);
  assert.equal(msgs[1].subtype, 'guidance');
  assert.equal(msgs[1].gid, 'g9');
});

test('run_started 附带图片清单（≤4 张，A5）', () => {
  const events = [{ ts: 't', event: 'run_started', data: { goal: '看图' } }];
  const { msgs } = buildMessages(events, [], {}, { images: ['01.png', '02.png'] });
  assert.deepEqual(msgs[0].images, ['01.png', '02.png']);
  assert.equal(buildMessages(events, [], {}).msgs[0].images, null);
});

test('approval_decided → system 消息（I1 审批闭环可见）', () => {
  const { msgs } = buildMessages(
    [{ ts: 't', event: 'approval_decided', data: { action: 'approve' } }], [], {});
  assert.equal(msgs[0].type, 'system');
  assert.match(msgs[0].text, /批准执行/);
});
