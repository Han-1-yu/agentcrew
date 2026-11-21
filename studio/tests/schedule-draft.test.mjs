/**
 * `lib/schedule-draft.js`：日程 → 表单草稿 / 草稿 → CLI 载荷 的纯逻辑。
 * 重点是两条边界：ISO 秒级 → `datetime-local` 的分钟级截断，以及 once / recurring
 * 字段互斥（填了不该上送的那组，后端按 kind 丢掉，用户会以为没生效）。
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  draftFromSchedule, localValue, payloadFromDraft, stateOf, whenLabel,
} from '../src/lib/schedule-draft.js';

test('localValue 截到分钟，带秒也不丢值', () => {
  assert.equal(localValue('2026-11-23T09:00:00'), '2026-11-23T09:00');
  assert.equal(localValue('2026-11-23T09:00'), '2026-11-23T09:00');
  assert.equal(localValue(''), '');
  assert.equal(localValue(null), '');
  // 非法值返回空串，让输入框留白而不是显示半截
  assert.equal(localValue('明天九点'), '');
});

test('draftFromSchedule 编辑预填：一次性取 at，周期取 time + weekdays', () => {
  const once = draftFromSchedule({
    goal: '写周报', kind: 'once', mode: 'ask', mock: true, at: '2030-01-01T09:00:00',
  });
  assert.deepEqual(
    [once.goal, once.kind, once.mode, once.mock, once.at],
    ['写周报', 'once', 'ask', true, '2030-01-01T09:00']);
  const recurring = draftFromSchedule({
    goal: 'g', kind: 'recurring', mode: 'agent', time: '18:30', weekdays: ['MO', 'WE'],
  });
  assert.equal(recurring.time, '18:30');
  assert.deepEqual(recurring.weekdays, ['MO', 'WE']);
  // 无 at 时 once 的 at 留空（新建不会带出脏值）
  assert.equal(draftFromSchedule({}).at, '');
  // 非法 mode 落回 agent，不让后端 choices 直接拒
  assert.equal(draftFromSchedule({ mode: 'weird' }).mode, 'agent');
});

test('payloadFromDraft 只带一组时间字段', () => {
  const once = payloadFromDraft({ goal: 'g', kind: 'once', mode: 'agent', at: '2030-01-01T09:00' });
  assert.equal(once.payload.at, '2030-01-01T09:00:00');
  assert.equal(once.payload.daily, undefined);
  assert.equal(once.payload.weekdays, undefined);

  const recurring = payloadFromDraft({
    goal: 'g', kind: 'recurring', time: '09:00', weekdays: ['MO', 'FR'],
  });
  assert.equal(recurring.payload.daily, '09:00');
  assert.equal(recurring.payload.weekdays, 'MO,FR');
  assert.equal(recurring.payload.at, undefined);
});

test('payloadFromDraft 的校验与后端 add_schedule 同口径', () => {
  assert.match(payloadFromDraft({ goal: '  ', kind: 'once', at: 'x' }).error, /目标/);
  assert.match(payloadFromDraft({ goal: 'g', kind: 'once', at: '' }).error, /触发时间/);
  assert.match(payloadFromDraft({ goal: 'g', kind: 'recurring', time: '9', weekdays: ['MO'] }).error, /触发时间/);
  assert.match(payloadFromDraft({ goal: 'g', kind: 'recurring', time: '09:00', weekdays: [] }).error, /星期/);
});

test('stateOf 区分「已触发」与「已停用」', () => {
  assert.equal(stateOf({ enabled: true }).label, '启用');
  assert.equal(stateOf({ enabled: false, kind: 'once', last_run_at: '2026-11-21T09:00:00' }).label, '已触发');
  assert.equal(stateOf({ enabled: false, kind: 'recurring' }).label, '已停用');
  // 停用后重新启用又挂了 last_run_at：启用态优先，仍显示「启用」
  assert.equal(stateOf({ enabled: true, last_run_at: '2026-11-21T09:00:00' }).cls, 'on');
});

test('whenLabel 周期给规则、一次性给时间', () => {
  assert.equal(whenLabel({ kind: 'recurring', weekdays: ['MO', 'TU'], time: '09:00' }), '每周 MO·TU 09:00');
  assert.equal(whenLabel({ kind: 'recurring' }), '每周 — —');
  assert.equal(whenLabel({ kind: 'once', at: '2030-01-01T09:00:00' }), '2030-01-01T09:00:00');
  assert.equal(whenLabel({ kind: 'once', next_run: '2030-01-01T09:00:00' }), '2030-01-01T09:00:00');
});
