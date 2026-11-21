/**
 * 事件视图（L1）：时间范围与筛选。纯函数，时间基准是「最新一条事件」而不是 now——
 * 回看历史 run 时，用 now 会把整场 run 都过滤掉。
 */

/** 事件时间里的最新一刻（毫秒）；解析不出来返回 null。 */
function newestTs(events) {
  let newest = null;
  for (const e of events || []) {
    const t = Date.parse(String((e && e.ts) || ''));
    if (!Number.isNaN(t) && (newest == null || t > newest)) newest = t;
  }
  return newest;
}

/** `minutes` 为 0 / 空 / 非法 时视为不限制。 */
function withinRange(ts, newest, minutes) {
  const m = Number(minutes);
  if (!m || m <= 0 || newest == null) return true;
  const t = Date.parse(String(ts || ''));
  if (Number.isNaN(t)) return false;
  return newest - t <= m * 60 * 1000;
}

/**
 * 过滤并倒序（新的在前）。`types` 是 Set/数组，空集视为「全不选」但由调用方决定；
 * `query` 对整条事件做 JSON 小写包含匹配；`rangeMinutes` 相对最新事件。
 */
function filterEvents(events, { types, query, rangeMinutes } = {}) {
  const list = [...(events || [])];
  const newest = newestTs(list);
  const typed = types == null ? null : (types instanceof Set ? types : new Set(types));
  const q = String(query || '').toLowerCase();
  return list
    .filter((e) => (typed ? typed.has(e.event) : true))
    .filter((e) => !q || JSON.stringify(e).toLowerCase().includes(q))
    .filter((e) => withinRange(e.ts, newest, rangeMinutes))
    .sort((a, b) => (Date.parse(String(b.ts || '')) || 0) - (Date.parse(String(a.ts || '')) || 0));
}

/** 事件能跳去哪里：任务事件 → 任务；运行级事件 → 对话。 */
function jumpTargets(e) {
  const d = (e && e.data) || {};
  const out = [];
  if (d.task_id) out.push({ kind: 'task', label: `任务 ${d.task_id}`, id: String(d.task_id) });
  if (!out.length) out.push({ kind: 'conversation', label: '对话', id: '' });
  return out;
}

export { filterEvents, jumpTargets, newestTs, withinRange };
