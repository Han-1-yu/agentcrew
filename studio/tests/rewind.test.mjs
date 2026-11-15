import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  buildRewindBody,
  explainRewindError,
  needsResume,
  normalizeRewindResult,
  rewindImpactText,
  rewindTaskOptions,
} from '../src/lib/rewind.js';

const RESULT = {
  run_id: 'R-1', target: 'T-002', affected: ['T-002', 'T-003', 'T-FINAL'],
  snapshot_dir: 'history/rewind-001', discarded_artifacts: 5,
  status_before: 'completed', status_after: 'interrupted',
  instruction: '改绿色', dry_run: true,
};

test('buildRewindBody 归一化字段且默认只预览', () => {
  assert.deepEqual(buildRewindBody({}), {
    task: '', instruction: '', dry_run: false,
  });
  assert.deepEqual(buildRewindBody({ task: ' T-002 ', instruction: 42, dryRun: true }), {
    task: 'T-002', instruction: '42', dry_run: true,
  });
});

test('normalizeRewindResult 兼容缺字段与坏类型', () => {
  const empty = normalizeRewindResult(null);
  assert.deepEqual(empty.affected, []);
  assert.equal(empty.discardedArtifacts, 0);
  assert.equal(empty.dryRun, false);
  const full = normalizeRewindResult(RESULT);
  assert.equal(full.affected.length, 3);
  assert.equal(full.snapshotDir, 'history/rewind-001');
  assert.equal(normalizeRewindResult({ discarded_artifacts: '7' }).discardedArtifacts, 0);
});

test('rewindImpactText 带出影响集、产物数与快照', () => {
  const text = rewindImpactText(RESULT);
  assert.match(text, /预览/);
  assert.match(text, /影响 3 个任务/);
  assert.match(text, /T-002、T-003、T-FINAL/);
  assert.match(text, /移除 5 个产物/);
  assert.match(text, /history\/rewind-001/);
  assert.match(rewindImpactText({ ...RESULT, dry_run: false }), /需 resume/);
});

test('needsResume 只在真正回滚后为真', () => {
  assert.equal(needsResume(RESULT), false);
  assert.equal(needsResume({ ...RESULT, dry_run: false }), true);
  assert.equal(needsResume({ ...RESULT, dry_run: false, affected: [] }), false);
});

test('rewindTaskOptions 保留首选任务并跳过脏数据', () => {
  const options = rewindTaskOptions(
    [{ id: 'T-001', title: '登录页' }, null, { title: '无 id' }], 'T-007');
  assert.equal(options[0].value, 'T-007');
  assert.equal(options.length, 2);
  assert.equal(options[1].label, 'T-001 · 登录页');
  assert.equal(rewindTaskOptions([], '').length, 0);
});

test('explainRewindError 把 409 翻成「先取消」', () => {
  assert.match(explainRewindError({ code: 'HTTP_409' }), /取消/);
  assert.match(explainRewindError({ code: 'NO_API' }), /设置/);
  assert.match(explainRewindError({ detail: 'disk full' }), /disk full/);
});
