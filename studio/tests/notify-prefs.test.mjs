import { test } from 'node:test';
import assert from 'node:assert';

import {
  categoryOf, inQuietHours, mutedCategory, levelOf, normalizePrefs, shouldNotifySystem,
} from '../src/lib/notify-prefs.js';

const at = (h, m = 0) => new Date(2026, 10, 21, h, m, 0);

test('categoryOf：三类目归属', () => {
  assert.equal(categoryOf('approval_requested'), 'approvals');
  assert.equal(categoryOf('run_paused'), 'approvals');
  assert.equal(categoryOf('budget_exhausted'), 'budget');
  assert.equal(categoryOf('run_finished'), 'runs');
  assert.equal(categoryOf('error'), 'runs');
  assert.equal(categoryOf('weird'), '');
});

test('mutedCategory：逐类静音，未知事件不静音', () => {
  const prefs = { mute: { approvals: true, budget: false } };
  assert.equal(mutedCategory(prefs, 'approval_decided'), true);
  assert.equal(mutedCategory(prefs, 'budget_warning'), false);
  assert.equal(mutedCategory(prefs, 'run_finished'), false);
  assert.equal(mutedCategory(prefs, 'weird'), false);
  assert.equal(mutedCategory(null, 'approval_requested'), false);
});

test('normalizePrefs：缺省全开、级别兜底 all、非法时间清空', () => {
  const p = normalizePrefs(null);
  assert.deepEqual(p, { system: true, quietFrom: '', quietTo: '', minLevel: 'all',
    mute: { approvals: false, budget: false, runs: false } });
  const q = normalizePrefs({ system: false, quietFrom: '22:30', quietTo: '08:00', minLevel: 'bogus' });
  assert.equal(q.system, false);
  assert.equal(q.minLevel, 'all');
  assert.equal(q.quietFrom, '22:30');
  assert.equal(normalizePrefs({ quietFrom: '25:00', minLevel: 'error' }).quietFrom, '');
  assert.equal(normalizePrefs({ minLevel: 'error' }).minLevel, 'error');
});

test('levelOf：错误 > 警告 > 信息', () => {
  assert.equal(levelOf('error'), 'error');
  assert.equal(levelOf('run_failed'), 'error');
  assert.equal(levelOf('budget_warning'), 'warn');
  assert.equal(levelOf('approval_requested'), 'info');
  assert.equal(levelOf(undefined), 'info');
});

test('inQuietHours：跨午夜窗口', () => {
  const prefs = { quietFrom: '22:00', quietTo: '08:00' };
  assert.equal(inQuietHours(prefs, at(23, 30)), true);
  assert.equal(inQuietHours(prefs, at(7, 59)), true);
  assert.equal(inQuietHours(prefs, at(8, 0)), false);
  assert.equal(inQuietHours(prefs, at(21, 59)), false);
});

test('inQuietHours：同日窗口 / 未设置 / 两端相同', () => {
  assert.equal(inQuietHours({ quietFrom: '12:00', quietTo: '13:00' }, at(12, 30)), true);
  assert.equal(inQuietHours({ quietFrom: '12:00', quietTo: '13:00' }, at(13, 1)), false);
  assert.equal(inQuietHours({}, at(12, 30)), false);
  assert.equal(inQuietHours({ quietFrom: '09:00', quietTo: '09:00' }, at(9, 0)), false);
  assert.equal(inQuietHours({}, 'not-a-date'), false);
});

test('shouldNotifySystem：开关 / 焦点 / 静默 / 级别四道闸', () => {
  const base = { system: true, minLevel: 'all' };
  const item = { event: 'approval_requested' };
  assert.equal(shouldNotifySystem(base, item, { focused: false }), true);
  assert.equal(shouldNotifySystem(base, item, { focused: true }), false);
  assert.equal(shouldNotifySystem({ system: false }, item, { focused: false }), false);
  assert.equal(shouldNotifySystem({ ...base, quietFrom: '22:00', quietTo: '08:00' }, item,
    { focused: false, now: at(23) }), false);
  // 门槛：仅 error 时，warn 与 info 都不弹
  assert.equal(shouldNotifySystem({ system: true, minLevel: 'error' }, { event: 'budget_warning' }, {}), false);
  assert.equal(shouldNotifySystem({ system: true, minLevel: 'error' }, { event: 'error' }, {}), true);
  assert.equal(shouldNotifySystem({ system: true, minLevel: 'warn' }, { event: 'approval_requested' }, {}), false);
  // 类目静音也挡系统通知
  assert.equal(shouldNotifySystem({ system: true, mute: { budget: true } }, { event: 'budget_warning' }, {}), false);
});
