import { test } from 'node:test';
import assert from 'node:assert';

import { biggestRuns, cleanupCandidates, usageStats } from '../src/lib/data-usage.js';

const run = (name, sizeBytes, extra = {}) => ({ dir: `E:/out/${name}`, name, sizeBytes, status: 'completed', archived: false, ...extra });

test('cleanupCandidates：只要已归档且不在运行中的', () => {
  const usage = {
    runs: [
      run('a', 10, { archived: true }),
      run('b', 20, { archived: true, status: 'running' }),
      run('c', 30),
      run('d', 40, { archived: true, status: 'failed' }),
    ],
  };
  assert.deepEqual(cleanupCandidates(usage).map((r) => r.name), ['a', 'd']);
  assert.deepEqual(cleanupCandidates(null), []);
});

test('usageStats：总数 / 体积 / 归档 / 可清理', () => {
  const usage = {
    totalBytes: 100,
    runs: [run('a', 10, { archived: true }), run('b', 20), run('c', 70, { archived: true, status: 'running' })],
  };
  const s = usageStats(usage);
  assert.equal(s.count, 3);
  assert.equal(s.totalBytes, 100);
  assert.equal(s.archivedCount, 2);
  assert.equal(s.candidateCount, 1);
  assert.equal(s.candidateBytes, 10);
});

test('usageStats：缺 totalBytes 时按各 run 求和', () => {
  const s = usageStats({ runs: [run('a', 5), run('b', 7)] });
  assert.equal(s.totalBytes, 12);
  assert.equal(s.candidateCount, 0);
});

test('biggestRuns：按体积降序取前 N，非法 limit 归零', () => {
  const usage = { runs: [run('a', 5), run('b', 50), run('c', 20)] };
  assert.deepEqual(biggestRuns(usage, 2).map((r) => r.name), ['b', 'c']);
  assert.deepEqual(biggestRuns(usage, 0), []);
  assert.deepEqual(biggestRuns(null, 3), []);
});
