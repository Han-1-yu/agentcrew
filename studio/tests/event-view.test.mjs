import { test } from 'node:test';
import assert from 'node:assert';

import { filterEvents, jumpTargets, newestTs, withinRange } from '../src/lib/event-view.js';

const ev = (ts, event, data = {}) => ({ ts, event, data });

test('newestTs：取最大时间，坏时间戳忽略', () => {
  const events = [ev('2026-11-21T10:00:00Z', 'a'), ev('2026-11-21T10:05:00Z', 'b'), ev('bad', 'c')];
  assert.equal(newestTs(events), Date.parse('2026-11-21T10:05:00Z'));
  assert.equal(newestTs([]), null);
});

test('withinRange：相对最新事件，0/非法表示不限制', () => {
  const newest = Date.parse('2026-11-21T10:00:00Z');
  assert.equal(withinRange('2026-11-21T09:58:00Z', newest, 5), true);
  assert.equal(withinRange('2026-11-21T09:50:00Z', newest, 5), false);
  assert.equal(withinRange('2026-11-21T09:50:00Z', newest, 0), true);
  assert.equal(withinRange('bad', newest, 5), false);
  assert.equal(withinRange('2026-11-21T09:00:00Z', null, 5), true);
});

test('filterEvents：类型 + 搜索 + 范围，且按时间倒序', () => {
  const events = [
    ev('2026-11-21T09:00:00Z', 'task_started', { task_id: 'T-1' }),
    ev('2026-11-21T09:05:00Z', 'task_succeeded', { task_id: 'T-1' }),
    ev('2026-11-21T09:10:00Z', 'run_finished', { status: 'completed' }),
  ];
  const types = new Set(['task_started', 'task_succeeded']);
  const out = filterEvents(events, { types });
  assert.deepEqual(out.map((e) => e.event), ['task_succeeded', 'task_started']);
  // 搜索命中 JSON 任意字段
  assert.deepEqual(filterEvents(events, { query: 'completed' }).map((e) => e.event), ['run_finished']);
  // 相对最新事件 5 分钟：09:05 与 09:10 在内，09:00 在外
  assert.deepEqual(filterEvents(events, { rangeMinutes: 6 }).map((e) => e.event),
    ['run_finished', 'task_succeeded']);
  // types 为数组也认
  assert.equal(filterEvents(events, { types: ['run_finished'] }).length, 1);
});

test('jumpTargets：任务事件给任务跳转，其余给对话', () => {
  assert.deepEqual(jumpTargets(ev('t', 'task_started', { task_id: 'T-9' })),
    [{ kind: 'task', label: '任务 T-9', id: 'T-9' }]);
  assert.deepEqual(jumpTargets(ev('t', 'run_finished', {})),
    [{ kind: 'conversation', label: '对话', id: '' }]);
});
