/**
 * 数据管理（O3）：占用统计与「可清理」筛选。纯函数，不动文件系统。
 *
 * `usage` 形状来自主进程 `data:usage`：
 * `{ root, totalBytes, count, runs: [{ dir, name, sizeBytes, status, archived, mtime }] }`。
 */

/** 只有「已归档且不在运行中」的 run 才允许清理（走回收站）。 */
function cleanupCandidates(usage) {
  return ((usage && usage.runs) || []).filter((r) => r && r.archived && r.status !== 'running');
}

/** 汇总：总数 / 总体积 / 归档数 / 可清理数与体积。 */
function usageStats(usage) {
  const runs = ((usage && usage.runs) || []).filter(Boolean);
  const archived = runs.filter((r) => r.archived);
  const candidates = cleanupCandidates(usage);
  return {
    count: runs.length,
    totalBytes: Number((usage && usage.totalBytes) || runs.reduce((s, r) => s + (Number(r.sizeBytes) || 0), 0)),
    archivedCount: archived.length,
    candidateCount: candidates.length,
    candidateBytes: candidates.reduce((s, r) => s + (Number(r.sizeBytes) || 0), 0),
  };
}

/** 按体积降序取前 N 个（给设置页的「最占空间」列表用）。 */
function biggestRuns(usage, limit = 6) {
  const runs = ((usage && usage.runs) || []).filter(Boolean).slice();
  runs.sort((a, b) => (Number(b.sizeBytes) || 0) - (Number(a.sizeBytes) || 0));
  return runs.slice(0, Math.max(0, Number(limit) || 0));
}

export { biggestRuns, cleanupCandidates, usageStats };
