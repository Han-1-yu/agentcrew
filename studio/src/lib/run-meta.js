/**
 * run 元数据（B3）：conversation = run 的 1:1 别名层（方案 §3.1 决策 A）。
 *
 * 存储契约：settings.json 的 ``runMeta: { [run_id]: { alias, pinned, archived } }``。
 * 纯增量键——旧版本忽略未知键、缺键按默认值，无迁移。
 * 这里只做归一化 / 更新 / 裁剪 / 筛选 / 分组的纯函数，写盘
 * （window.studio.setSettings）与回滚留在 features 层。
 */

const META_FIELDS = ['alias', 'pinned', 'archived'];

/** 容错读取：坏形状（旧版本/手改）一律退回默认元数据，不让列表崩掉。 */
function normalizeRunMeta(raw) {
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const [runId, value] of Object.entries(raw)) {
    if (!runId || !value || typeof value !== 'object') continue;
    const item = {};
    for (const key of META_FIELDS) {
      if (value[key] === undefined) continue;
      if (key === 'alias') item.alias = String(value[key]).slice(0, 80);
      else item[key] = !!value[key];
    }
    out[runId] = { alias: item.alias || '', pinned: !!item.pinned, archived: !!item.archived };
  }
  return out;
}

/** run 的元数据键：runId 优先（与 scanRuns / _validated_run_id 同口径）。 */
function runMetaKey(run) {
  const raw = (run && (run.runId || run.dir)) || '';
  return String(raw).replace(/\\/g, '/').split('/').filter(Boolean).pop() || '';
}

/** 复制并应用一条 patch（undefined 字段不覆盖）；返回新对象，不改动入参。 */
function nextRunMeta(meta, runId, patch) {
  const base = normalizeRunMeta(meta);
  if (!runId) return base;
  const cur = base[runId] || { alias: '', pinned: false, archived: false };
  const next = { ...cur };
  for (const key of META_FIELDS) {
    if (patch && patch[key] !== undefined) {
      next[key] = key === 'alias' ? String(patch[key]).slice(0, 80) : !!patch[key];
    }
  }
  if (!next.alias) delete next.alias;
  base[runId] = { alias: next.alias || '', pinned: !!next.pinned, archived: !!next.archived };
  return base;
}

/** deleteRun 后裁掉幽灵键。 */
function pruneRunMeta(meta, removedRunIds) {
  const base = normalizeRunMeta(meta);
  for (const id of removedRunIds || []) delete base[id];
  return base;
}

/** 把元数据挂到运行列表条目上（浅拷贝，不污染 listRuns 结果）。 */
function applyRunMeta(runs, meta) {
  const base = normalizeRunMeta(meta);
  return (runs || []).map((r) => {
    const m = base[runMetaKey(r)];
    return m ? { ...r, alias: m.alias, pinned: m.pinned, archived: m.archived } : r;
  });
}

/** 侧栏 / 下拉共用的显示名：别名 > 目标 > runId。 */
function runDisplayName(run) {
  const r = run || {};
  return String(r.alias || r.goal || r.runId || '未命名');
}

/* ---- 侧栏列表筛选（B1）---- */

const RUN_STATUS_FILTERS = [
  { key: 'all', label: '全部', match: () => true },
  { key: 'active', label: '进行中', match: (s) => ['running', 'planning', 'preparing', 'review', 'awaiting_approval'].includes(s) },
  { key: 'done', label: '已完成', match: (s) => s === 'completed' },
  { key: 'failed', label: '失败', match: (s) => ['failed', 'cancelled', 'interrupted'].includes(s) },
];

/**
 * 过滤侧栏可见条目。
 * 归档默认隐藏；带搜索词时放行（ChatGPT 口径：归档可搜不可见）。
 */
function filterRuns(runs, { query = '', status = 'all', showArchived = false } = {}) {
  const q = String(query || '').trim().toLowerCase();
  const filter = RUN_STATUS_FILTERS.find((f) => f.key === status) || RUN_STATUS_FILTERS[0];
  return (runs || []).filter((r) => {
    if (r.archived && !showArchived && !q) return false;
    if (!filter.match(String(r.status || 'unknown'))) return false;
    if (!q) return true;
    return [r.alias, r.goal, r.runId].some((v) => String(v || '').toLowerCase().includes(q));
  });
}

/* ---- 时间分组（B1）：置顶单列一组，其余按创建时间 ---- */

const DAY_MS = 86400000;

function runTimeKey(run, now = Date.now()) {
  const r = run || {};
  const raw = r.createdAt || r.updatedAt || '';
  const ts = raw ? Date.parse(raw) : Number(r.mtime) || 0;
  return Number.isFinite(ts) && ts > 0 ? ts : 0;
}

function timeGroupLabel(ts, now = Date.now()) {
  if (!ts) return { key: 'older', label: '更早' };
  const dayStart = new Date(now); dayStart.setHours(0, 0, 0, 0);
  const start = dayStart.getTime();
  if (ts >= start) return { key: 'today', label: '今天' };
  if (ts >= start - DAY_MS) return { key: 'yesterday', label: '昨天' };
  if (ts >= start - 7 * DAY_MS) return { key: 'week', label: '近 7 天' };
  return { key: 'older', label: '更早' };
}

/** 返回 [{ key, label, runs }]；置顶组在最前，组内保持传入顺序（最新在前）。 */
function groupRunsByTime(runs, now = Date.now()) {
  const pinned = [];
  const byKey = new Map();
  for (const r of runs || []) {
    if (r.pinned) { pinned.push(r); continue; }
    const g = timeGroupLabel(runTimeKey(r, now), now);
    if (!byKey.has(g.key)) byKey.set(g.key, { key: g.key, label: g.label, runs: [] });
    byKey.get(g.key).runs.push(r);
  }
  const order = ['today', 'yesterday', 'week', 'older'];
  const groups = [];
  if (pinned.length) groups.push({ key: 'pinned', label: '置顶', runs: pinned });
  for (const key of order) {
    const g = byKey.get(key);
    if (g && g.runs.length) groups.push(g);
  }
  return groups;
}

/** 侧栏列表重绘签名：内容（含筛选态）不变则跳过渲染，保住 hover。 */
function runListSignature(runs, state) {
  const s = state || {};
  const head = [s.query || '', s.status || 'all', s.showArchived ? '1' : '0', (runs || []).length].join('|');
  const body = (runs || []).slice(0, 40).map((r) => [
    r.runId, r.status, r.alias || '', r.pinned ? '1' : '0', r.archived ? '1' : '0',
  ].join('.')).join('|');
  return head + '#' + body;
}

export {
  RUN_STATUS_FILTERS,
  applyRunMeta,
  filterRuns,
  groupRunsByTime,
  nextRunMeta,
  normalizeRunMeta,
  pruneRunMeta,
  runDisplayName,
  runListSignature,
  runMetaKey,
  runTimeKey,
  timeGroupLabel,
};
