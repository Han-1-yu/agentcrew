import { test } from 'node:test';
import assert from 'node:assert';

import {
  entriesFor, expectedPaths, resultArtifacts, summarizeTaskArtifacts,
} from '../src/lib/task-artifacts.js';

const task = (id, over = {}) => ({
  id, title: `任务 ${id}`, status: 'succeeded', dependencies: [], ...over,
});
const entry = (path, taskId, size = 100) => ({ path, task: taskId, sizeBytes: size, exists: true });

test('result.artifacts 是字符串（真实 tasks.json 的空白序列化）时当没有产物', () => {
  assert.deepEqual(resultArtifacts({ result: { artifacts: ' ' } }), []);
  assert.deepEqual(resultArtifacts({ result: { artifacts: null } }), []);
  assert.deepEqual(resultArtifacts({}), []);
});

test('result.artifacts 数组归一：字符串元素 / size_bytes 两种写法都认', () => {
  assert.deepEqual(
    resultArtifacts({ result: { artifacts: ['a.md', { path: 'b.py', size_bytes: 7 }] } }),
    [{ path: 'a.md', sizeBytes: undefined }, { path: 'b.py', sizeBytes: 7 }]);
});

test('MANIFEST 优先于 result.artifacts；输入按依赖任务聚合并去重', () => {
  const tasks = [
    task('T-001'), task('T-002', { dependencies: ['T-001'] }),
  ];
  const entries = [
    entry('docs/a.md', 'T-001'), entry('docs/b.md', 'T-001'), entry('src/x.py', 'T-002'),
  ];
  const sum = summarizeTaskArtifacts(tasks[1], tasks, entries);
  assert.equal(sum.outputs.source, 'manifest');
  assert.deepEqual(sum.outputs.files.map((f) => f.path), ['src/x.py']);
  assert.deepEqual(sum.inputs.from.map((f) => f.id), ['T-001']);
  assert.deepEqual(sum.inputs.files.map((f) => f.path), ['docs/a.md', 'docs/b.md']);
});

test('没有 MANIFEST 时退回 result.artifacts（mock / 旧 run）', () => {
  const tasks = [
    task('T-001', { result: { artifacts: [{ path: 'docs/a.md', size_bytes: 5 }] } }),
    task('T-002', { dependencies: ['T-001'] }),
  ];
  const sum = summarizeTaskArtifacts(tasks[1], tasks, []);
  assert.equal(sum.outputs.source, 'result');
  assert.deepEqual(sum.outputs.files, []);
  assert.deepEqual(sum.inputs.files.map((f) => f.path), ['docs/a.md']);
});

test('契约要求未产出只在任务完成后报；未完成只标「待产出」', () => {
  const done = task('T-002', { deliverables: ['docs/a.md', { path: 'docs/b.md' }] });
  const sum = summarizeTaskArtifacts(done, [done], [entry('docs/a.md', 'T-002')]);
  assert.deepEqual(sum.missing, ['docs/b.md']);
  assert.equal(sum.pending, false);

  const running = { ...done, status: 'running' };
  assert.deepEqual(summarizeTaskArtifacts(running, [running], []).missing, []);
  assert.equal(summarizeTaskArtifacts(running, [running], []).pending, true);
});

test('无依赖任务时 from 为空；expectedPaths 兼容字符串与对象', () => {
  assert.deepEqual(expectedPaths({ deliverables: ['a', { path: 'b' }, { kind: 'md' }] }), ['a', 'b']);
  const solo = task('T-009', { deliverables: ['a'] });
  const sum = summarizeTaskArtifacts(solo, [solo], []);
  assert.deepEqual(sum.inputs, { files: [], from: [] });
  assert.deepEqual(sum.missing, ['a']);
});

test('entriesFor：只返回指定任务且字段归一', () => {
  const raw = [{ path: 'p', task: 'T-1', sizeBytes: 3 }, { path: 'q', task: 'T-2', sizeBytes: 4 }];
  assert.deepEqual(entriesFor('T-1', raw), [{ path: 'p', sizeBytes: 3 }]);
  assert.deepEqual(entriesFor('T-1', null), []);
});
