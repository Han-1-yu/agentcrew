/* 产物 IPC 响应与列表模型：供产物页和结果抽屉共用，避免两处解析规则漂移。 */
function normalizeArtifactList(payload) {
  if (Array.isArray(payload)) return { entries: payload, manifestError: null };
  const entries = payload && Array.isArray(payload.entries) ? payload.entries : [];
  return { entries, manifestError: payload && payload.manifestError ? String(payload.manifestError) : null };
}

function groupArtifacts(entries) {
  const groups = new Map();
  for (const entry of entries || []) {
    const dir = String((entry && entry.path) || '').split('/')[0] || '根目录';
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push(entry);
  }
  return [...groups.entries()];
}

function artifactExportOutcome(result) {
  if (result && result.canceled) return { type: 'canceled' };
  if (result && result.saved) return { type: 'saved', path: String(result.saved), size: Number(result.size) || 0 };
  return { type: 'error', message: '导出未返回保存路径' };
}

export { artifactExportOutcome, groupArtifacts, normalizeArtifactList };
