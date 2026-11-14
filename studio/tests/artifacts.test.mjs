import { test } from 'node:test';
import assert from 'node:assert/strict';

import { artifactExportOutcome, groupArtifacts, normalizeArtifactList } from '../src/lib/artifacts.js';

test('normalizeArtifactList 支持正式 IPC 契约并保留 MANIFEST 错误', () => {
  const payload = { entries: [{ path: 'docs/a.md', sizeBytes: 3 }], manifestError: 'bad json' };
  assert.deepEqual(normalizeArtifactList(payload), {
    entries: [{ path: 'docs/a.md', sizeBytes: 3 }],
    manifestError: 'bad json',
  });
});

test('normalizeArtifactList 兼容旧数组响应和异常响应', () => {
  assert.deepEqual(normalizeArtifactList([{ path: 'a.txt' }]).entries, [{ path: 'a.txt' }]);
  assert.deepEqual(normalizeArtifactList(null), { entries: [], manifestError: null });
});

test('groupArtifacts 按首级目录分组并保留顺序', () => {
  const groups = groupArtifacts([
    { path: 'docs/a.md' }, { path: 'src/index.js' }, { path: 'docs/b.md' },
  ]);
  assert.deepEqual(groups.map(([dir]) => dir), ['docs', 'src']);
  assert.deepEqual(groups[0][1].map(x => x.path), ['docs/a.md', 'docs/b.md']);
});

test('artifactExportOutcome 区分取消、保存成功和异常契约', () => {
  assert.equal(artifactExportOutcome({ canceled: true }).type, 'canceled');
  assert.deepEqual(artifactExportOutcome({ saved: 'a.zip', size: 12 }), { type: 'saved', path: 'a.zip', size: 12 });
  assert.equal(artifactExportOutcome({}).type, 'error');
});
