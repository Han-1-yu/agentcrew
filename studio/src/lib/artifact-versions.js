/**
 * 产物版本与行级 diff 的纯计算（零依赖，供产物页与单测共用）。
 *
 * 版本来源是 rewind 快照：`<run>/history/rewind-NNN/artifacts/<path>`，
 * 每份快照的 `rewind.json` 里登记了当时受影响任务的产物路径。
 */
const MAX_CELLS = 1_000_000;

function splitLines(text) {
  const raw = String(text == null ? '' : text).replace(/\r\n?/g, '\n');
  if (!raw) return [];
  const lines = raw.split('\n');
  // 结尾换行不算一行（避免每份文件尾部凭空多一条空行差异）
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

/** 某个产物在历史快照里的版本（新的在前），没有则空数组。 */
function snapshotVersionsFor(snapshots, artifactPath) {
  const path = String(artifactPath || '');
  if (!path) return [];
  return (snapshots || [])
    .map((s) => {
      const hit = (s.artifacts || []).find((a) => a.path === path);
      return hit ? {
        name: s.name,
        createdAt: s.createdAt || '',
        target: s.target || '',
        instruction: s.instruction || '',
        sizeBytes: hit.sizeBytes || 0,
        task: hit.task || '',
      } : null;
    })
    .filter(Boolean);
}

/**
 * 行级 diff：`before` 是旧（快照），`after` 是新（当前）。
 * 返回 `{rows, added, removed, truncated}`；每行 `kind: same|add|del`，
 * `aNo / bNo` 分别是旧/新文件里的行号（不存在该侧为 null）。
 *
 * 文件过大（DP 单元格超过 100 万）时退化为「整段替换」并标 `truncated`，
 * 避免在渲染进程里卡住——此时结果只表示「两份不同」，不给逐行对齐。
 */
function lineDiff(before, after) {
  const a = splitLines(before);
  const b = splitLines(after);
  if (a.length * b.length > MAX_CELLS) {
    const rows = [
      ...a.map((text, i) => ({ kind: 'del', text, aNo: i + 1, bNo: null })),
      ...b.map((text, i) => ({ kind: 'add', text, aNo: null, bNo: i + 1 })),
    ];
    return { rows, added: b.length, removed: a.length, truncated: true };
  }
  // LCS 表：dp[i][j] = a[i..] 与 b[j..] 的最长公共后缀长度
  const n = a.length; const m = b.length;
  const w = m + 1;
  const dp = new Uint32Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * w + j] = a[i] === b[j]
        ? dp[(i + 1) * w + (j + 1)] + 1
        : Math.max(dp[(i + 1) * w + j], dp[i * w + (j + 1)]);
    }
  }
  const rows = [];
  let added = 0; let removed = 0;
  let i = 0; let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { rows.push({ kind: 'same', text: a[i], aNo: i + 1, bNo: j + 1 }); i++; j++; }
    else if (dp[(i + 1) * w + j] >= dp[i * w + (j + 1)]) {
      rows.push({ kind: 'del', text: a[i], aNo: i + 1, bNo: null }); removed++; i++;
    } else { rows.push({ kind: 'add', text: b[j], aNo: null, bNo: j + 1 }); added++; j++; }
  }
  while (i < n) { rows.push({ kind: 'del', text: a[i], aNo: i + 1, bNo: null }); removed++; i++; }
  while (j < m) { rows.push({ kind: 'add', text: b[j], aNo: null, bNo: j + 1 }); added++; j++; }
  return { rows, added, removed, truncated: false };
}

/** 差异行的可读摘要（一致时只说一致，避免「+0 −0」）。 */
function diffSummary(diff) {
  if (!diff || !diff.rows || !diff.rows.length) return '两份内容都为空';
  if (!diff.added && !diff.removed) return '两份内容一致';
  const tail = diff.truncated ? '（文件过大，未逐行对齐）' : '';
  return `+${diff.added} 行 · −${diff.removed} 行${tail}`;
}

export { diffSummary, lineDiff, snapshotVersionsFor, splitLines };
