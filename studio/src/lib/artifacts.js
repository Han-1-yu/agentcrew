/* 产物 IPC 响应与列表模型：供产物页和结果抽屉共用，避免两处解析规则漂移。 */

/* 文件类型线性图标（SF Symbols 风格：统一 1.6 线宽、圆头端点，颜色走 currentColor）。 */
const ARTIFACT_ICON_PATHS = {
  doc: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h6"/>',
  code: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="m10 12-2 2 2 2M14 12l2 2-2 2"/>',
  test: '<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3"/>',
  report: '<path d="M5 20v-6M11 20V8M17 20V10"/><path d="M3 20h18"/>',
  config: '<path d="M4 6h8M18 6h2M4 12h2M10 12h10M4 18h8M18 18h2"/><circle cx="15" cy="6" r="2"/><circle cx="7" cy="12" r="2"/><circle cx="15" cy="18" r="2"/>',
  other: '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
};
function artifactIcon(kind, size = 14) {
  const paths = ARTIFACT_ICON_PATHS[kind] || ARTIFACT_ICON_PATHS.doc;
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}

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

export { artifactExportOutcome, artifactIcon, groupArtifacts, normalizeArtifactList };
