/**
 * 通知偏好（O2）：系统通知开关、静默时段、最低级别。纯计算，供通知模块与设置页共用。
 *
 * `levelOf(event)` 把事件折成等级：`error` > `warn` > `info`。
 * 静默时段允许跨午夜（22:00–08:00）：起 > 止 时视为「穿过零点」。
 */
const LEVELS = [
  { id: 'all', label: '全部（含审批与预算预警）' },
  { id: 'warn', label: '仅警告及以上（预算 / 出错 / 失败）' },
  { id: 'error', label: '仅出错与失败' },
];

const LEVEL_RANK = { all: 0, info: 0, warn: 1, error: 2 };

/** 三类目：审批 / 预算 / 运行结果（O2 里可逐类静音，面板与系统通知同时生效）。 */
const CATEGORIES = [
  { id: 'approvals', label: '审批与挂起' },
  { id: 'budget', label: '预算预警与耗尽' },
  { id: 'runs', label: '运行结束 / 失败 / 出错' },
];

function categoryOf(event) {
  const e = String(event || '');
  if (e === 'approval_requested' || e === 'approval_decided' || e === 'run_paused') return 'approvals';
  if (e === 'budget_warning' || e === 'budget_exhausted') return 'budget';
  if (e === 'run_finished' || e === 'run_cancelled' || e === 'run_failed' || e === 'error') return 'runs';
  return '';
}

function normalizePrefs(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const level = LEVELS.some((l) => l.id === src.minLevel) ? src.minLevel : 'all';
  const hhmm = (v) => (/^([01]\d|2[0-3]):[0-5]\d$/.test(String(v || '')) ? String(v) : '');
  const muteSrc = src.mute && typeof src.mute === 'object' ? src.mute : {};
  const mute = {};
  for (const c of CATEGORIES) mute[c.id] = muteSrc[c.id] === true;
  return {
    system: src.system !== false,
    quietFrom: hhmm(src.quietFrom),
    quietTo: hhmm(src.quietTo),
    minLevel: level,
    mute,
  };
}

/** 该类是否被静音（未知事件不静音）。 */
function mutedCategory(prefs, event) {
  const p = normalizePrefs(prefs);
  const cat = categoryOf(event);
  return !!cat && p.mute[cat] === true;
}

/** 事件 → 等级；未知事件按 info 处理。 */
function levelOf(event) {
  const e = String(event || '');
  if (e === 'error' || e === 'run_failed') return 'error';
  if (e === 'budget_warning' || e === 'budget_exhausted' || e === 'run_cancelled') return 'warn';
  return 'info';
}

/** 静默时段判定：两端都填才生效；起 > 止 表示跨零点。 */
function inQuietHours(prefs, now) {
  const p = normalizePrefs(prefs);
  if (!p.quietFrom || !p.quietTo) return false;
  const at = now instanceof Date ? now : new Date(now || Date.now());
  if (Number.isNaN(at.getTime())) return false;
  const minutes = at.getHours() * 60 + at.getMinutes();
  const toMin = (s) => { const [h, m] = s.split(':').map(Number); return h * 60 + m; };
  const from = toMin(p.quietFrom);
  const to = toMin(p.quietTo);
  if (from === to) return false;
  return from < to ? (minutes >= from && minutes < to) : (minutes >= from || minutes < to);
}

/**
 * 是否值得弹系统通知：开关 + 等级门槛 + 静默时段 + 窗口焦点。
 * `ctx = { focused, now }`，由调用方提供（lib 不碰 window/document）。
 */
function shouldNotifySystem(prefs, item, ctx) {
  const p = normalizePrefs(prefs);
  if (!p.system) return false;
  if (mutedCategory(p, item && item.event)) return false;
  if (ctx && ctx.focused) return false;
  if (inQuietHours(p, ctx && ctx.now)) return false;
  const need = LEVEL_RANK[p.minLevel] == null ? 0 : LEVEL_RANK[p.minLevel];
  const got = LEVEL_RANK[levelOf(item && item.event)] || 0;
  return got >= need;
}

export {
  CATEGORIES, LEVELS, categoryOf, inQuietHours, levelOf, mutedCategory, normalizePrefs, shouldNotifySystem,
};
