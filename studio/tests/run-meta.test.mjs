import { test } from 'node:test';
import assert from 'node:assert';

import {
  applyRunMeta,
  filterRuns,
  groupRunsByTime,
  nextRunMeta,
  normalizeRunMeta,
  pruneRunMeta,
  runDisplayName,
  runListSignature,
  runMetaKey,
} from '../src/lib/run-meta.js';

const NOW = new Date('2026-11-21T12:00:00').getTime();
const run = (over = {}) => ({
  runId: 'run-2026-11-21-01', dir: 'output/run-2026-11-21-01',
  goal: '开发短链接服务', status: 'completed',
  createdAt: '2026-11-21T10:00:00', ...over,
});

test('normalize：坏形状退默认、未知键忽略、alias 截断', () => {
  const meta = normalizeRunMeta({
    'run-a': { alias: '会话A', pinned: 1, archived: false, junk: 'x' },
    'run-b': null,
    '': { alias: 'x' },
    'run-c': { alias: 'y'.repeat(200) },
  });
  assert.deepEqual(meta['run-a'], { alias: '会话A', pinned: true, archived: false });
  assert.equal(meta['run-b'], undefined);
  assert.equal(meta['run-c'].alias.length, 80);
  assert.deepEqual(normalizeRunMeta(null), {});
  assert.deepEqual(normalizeRunMeta('junk'), {});
});

test('runMetaKey：取目录名末段（Windows 反斜杠也兼容）', () => {
  assert.equal(runMetaKey(run()), 'run-2026-11-21-01');
  assert.equal(runMetaKey({ dir: 'C:\\output\\proj\\run-x' }), 'run-x');
  assert.equal(runMetaKey({}), '');
});

test('nextRunMeta：patch 只覆盖给定字段，不改动入参', () => {
  const base = { 'run-a': { alias: '旧名', pinned: false, archived: false } };
  const next = nextRunMeta(base, 'run-a', { alias: '新名' });
  assert.equal(next['run-a'].alias, '新名');
  assert.equal(next['run-a'].pinned, false);
  assert.equal(base['run-a'].alias, '旧名');
  const pinned = nextRunMeta(next, 'run-a', { pinned: true });
  assert.equal(pinned['run-a'].pinned, true);
  assert.equal(pinned['run-a'].alias, '新名');
  // 新 run 无既有条目也能写
  assert.equal(nextRunMeta({}, 'run-b', { archived: true })['run-b'].archived, true);
});

test('pruneRunMeta：删除 run 后清幽灵键', () => {
  const meta = { 'run-a': { alias: 'A' }, 'run-b': { alias: 'B' } };
  const next = pruneRunMeta(meta, ['run-a']);
  assert.equal(next['run-a'], undefined);
  assert.equal(next['run-b'].alias, 'B');
  assert.equal(meta['run-a'].alias, 'A');
});

test('applyRunMeta：挂别名/置顶/归档到列表条目', () => {
  const items = applyRunMeta([run(), run({ runId: 'run-02' })], {
    'run-2026-11-21-01': { alias: '链接服务', pinned: true, archived: false },
  });
  assert.equal(items[0].alias, '链接服务');
  assert.equal(items[0].pinned, true);
  assert.equal(items[1].alias, undefined);
  assert.equal(items[1].pinned, undefined);
});

test('runDisplayName：别名 > 目标 > runId', () => {
  assert.equal(runDisplayName(run({ alias: '周报' })), '周报');
  assert.equal(runDisplayName(run()), '开发短链接服务');
  assert.equal(runDisplayName({ runId: 'run-x' }), 'run-x');
});

test('filterRuns：归档默认隐藏，带搜索词放行；状态筛选', () => {
  const runs = [
    run({ runId: 'run-a', status: 'running' }),
    run({ runId: 'run-b', status: 'completed', archived: true, goal: '周报归档' }),
    run({ runId: 'run-c', status: 'failed', goal: '另一个目标' }),
  ];
  assert.equal(filterRuns(runs).length, 2);
  assert.equal(filterRuns(runs, { showArchived: true }).length, 3);
  assert.equal(filterRuns(runs, { query: '周报' }).length, 1); // 归档可搜
  assert.equal(filterRuns(runs, { status: 'active' }).length, 1);
  assert.equal(filterRuns(runs, { status: 'failed' }).length, 1);
  assert.equal(filterRuns(runs, { query: 'run-c' }).length, 1); // runId 可搜
});

test('groupRunsByTime：置顶组最前，今天/昨天/近7天/更早', () => {
  const day = 86400000;
  const mk = (id, tsAgo) => run({ runId: id, createdAt: new Date(NOW - tsAgo).toISOString() });
  const groups = groupRunsByTime([
    mk('old', 30 * day),
    mk('today', 2 * 3600000),
    mk('yest', day + 3600000),
    mk('week', 3 * day),
    run({ runId: 'pin', pinned: true, createdAt: '2026-10-01T00:00:00' }),
  ], NOW);
  assert.deepEqual(groups.map((g) => g.key), ['pinned', 'today', 'yesterday', 'week', 'older']);
  assert.equal(groups[0].runs[0].runId, 'pin');
  assert.equal(groups[1].runs[0].runId, 'today');
  assert.deepEqual(groupRunsByTime([], NOW), []);
});

test('runListSignature：内容或筛选态变化才变', () => {
  const a = [run()];
  const sig1 = runListSignature(a, { query: '', status: 'all', showArchived: false });
  assert.equal(sig1, runListSignature([run()], { query: '', status: 'all', showArchived: false }));
  assert.notEqual(sig1, runListSignature(a, { query: 'x', status: 'all', showArchived: false }));
  assert.notEqual(sig1, runListSignature([run({ status: 'running' })], { query: '', status: 'all', showArchived: false }));
});
