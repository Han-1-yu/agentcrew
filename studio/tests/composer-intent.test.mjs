import { test } from 'node:test';
import assert from 'node:assert';

import {
  ACTIVE_RUNNABLE,
  GUIDANCE_RUNNABLE,
  canStopRun,
  isTerminalRun,
  resolveComposerIntent,
} from '../src/lib/composer-intent.js';

test('分流：无 run / 演示模式 / 终态 → create', () => {
  assert.equal(resolveComposerIntent({ hasRun: false, runStatus: '', realMode: true }), 'create');
  assert.equal(resolveComposerIntent({ hasRun: true, runStatus: 'running', realMode: false }), 'create');
  assert.equal(resolveComposerIntent({ hasRun: true, runStatus: 'completed', realMode: true }), 'create');
  assert.equal(resolveComposerIntent({ hasRun: true, runStatus: 'failed', realMode: true }), 'create');
  assert.equal(resolveComposerIntent({ hasRun: true, runStatus: 'paused', realMode: true }), 'create');
});

test('分流：running / awaiting_approval → guidance（与后端 _GUIDANCE_RUNNABLE 同口径）', () => {
  assert.equal(resolveComposerIntent({ hasRun: true, runStatus: 'running', realMode: true }), 'guidance');
  assert.equal(resolveComposerIntent({ hasRun: true, runStatus: 'awaiting_approval', realMode: true }), 'guidance');
  for (const st of GUIDANCE_RUNNABLE) assert.ok([...GUIDANCE_RUNNABLE].includes(st));
});

test('分流：planning / preparing 活跃但不可注入 → 只能停止', () => {
  assert.equal(resolveComposerIntent({ hasRun: true, runStatus: 'planning', realMode: true }), 'stop');
  assert.equal(resolveComposerIntent({ hasRun: true, runStatus: 'preparing', realMode: true }), 'stop');
  assert.ok(ACTIVE_RUNNABLE.has('planning'));
});

test('停止键：仅真实模式 + 活跃 run 出现', () => {
  assert.equal(canStopRun({ hasRun: true, runStatus: 'running', realMode: true }), true);
  assert.equal(canStopRun({ hasRun: true, runStatus: 'running', realMode: false }), false);
  assert.equal(canStopRun({ hasRun: true, runStatus: 'completed', realMode: true }), false);
  assert.equal(canStopRun({ hasRun: false, runStatus: 'running', realMode: true }), false);
});

test('终态判定：paused 也按终态处理（不接受新指引）', () => {
  assert.equal(isTerminalRun('completed'), true);
  assert.equal(isTerminalRun('planned'), true);
  assert.equal(isTerminalRun('paused'), true);
  assert.equal(isTerminalRun('running'), false);
  assert.equal(isTerminalRun(undefined), false);
});
