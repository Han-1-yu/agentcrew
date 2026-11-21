import { test } from 'node:test';
import assert from 'node:assert';

import { diffSummary, lineDiff, snapshotVersionsFor, splitLines } from '../src/lib/artifact-versions.js';

test('相同内容：全部 same、无增删', () => {
  const d = lineDiff('a\nb\n', 'a\nb\n');
  assert.equal(d.added, 0);
  assert.equal(d.removed, 0);
  assert.deepEqual(d.rows.map((r) => r.kind), ['same', 'same']);
  assert.equal(diffSummary(d), '两份内容一致');
});

test('结尾换行不算一行（不制造幽灵差异）', () => {
  assert.deepEqual(splitLines('a\nb'), ['a', 'b']);
  assert.deepEqual(splitLines('a\nb\n'), ['a', 'b']);
  assert.deepEqual(splitLines(''), []);
  assert.equal(lineDiff('a\nb\n', 'a\nb').added, 0);
});

test('CRLF 与 LF 视为同一份内容', () => {
  assert.equal(lineDiff('a\r\nb\r\n', 'a\nb\n').added, 0);
});

test('插入 / 删除 / 替换的行号与计数', () => {
  const d = lineDiff('a\nb\nc\n', 'a\nx\nc\nd\n');
  // LCS 保留 a、c：b 被删，x 与 d 是新增
  assert.equal(d.removed, 1);
  assert.equal(d.added, 2);
  const kinds = d.rows.map((r) => r.kind);
  assert.deepEqual(kinds, ['same', 'del', 'add', 'same', 'add']);
  // 行号：del 只有旧行号，add 只有新行号
  for (const r of d.rows) {
    if (r.kind === 'del') assert.equal(r.bNo, null);
    if (r.kind === 'add') assert.equal(r.aNo, null);
  }
  assert.equal(diffSummary(d), '+2 行 · −1 行');
});

test('纯新增（空 → 有内容）', () => {
  const d = lineDiff('', 'x\ny\n');
  assert.deepEqual({ added: d.added, removed: d.removed }, { added: 2, removed: 0 });
  assert.deepEqual(d.rows.map((r) => r.bNo), [1, 2]);
});

test('文件过大退化为整段替换并标 truncated', () => {
  const big = Array.from({ length: 1200 }, (_, i) => `line-${i}`).join('\n');
  const other = Array.from({ length: 1200 }, (_, i) => `other-${i}`).join('\n');
  const d = lineDiff(big, other);
  assert.equal(d.truncated, true);
  assert.equal(d.removed, 1200);
  assert.equal(d.added, 1200);
  assert.match(diffSummary(d), /文件过大/);
});

test('snapshotVersionsFor：只挑含该路径的快照，字段映射', () => {
  const snapshots = [
    { name: 'rewind-002', createdAt: '2026-11-21T10:00:00Z', target: 'T-3',
      artifacts: [{ path: 'docs/a.md', task: 'T-3', sizeBytes: 12 }, { path: 'src/x.py', task: 'T-3', sizeBytes: 9 }] },
    { name: 'rewind-001', createdAt: '2026-11-20T10:00:00Z', target: 'T-1',
      artifacts: [{ path: 'docs/a.md', task: 'T-1', sizeBytes: 5 }] },
  ];
  const v = snapshotVersionsFor(snapshots, 'docs/a.md');
  assert.deepEqual(v.map((x) => x.name), ['rewind-002', 'rewind-001']);
  assert.equal(v[0].task, 'T-3');
  assert.equal(v[0].sizeBytes, 12);
  assert.deepEqual(snapshotVersionsFor(snapshots, 'src/x.py').map((x) => x.name), ['rewind-002']);
  assert.deepEqual(snapshotVersionsFor(snapshots, 'nope.md'), []);
  assert.deepEqual(snapshotVersionsFor(null, 'docs/a.md'), []);
});
