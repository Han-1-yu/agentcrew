/**
 * 定时任务表单的纯逻辑：日程 → 表单草稿（编辑预填）、草稿 → CLI 载荷（新建/更新）。
 *
 * 拆出来是为了两条边界能直接单测：
 * 1. **时区口径** —— 后端存 ISO8601 本机时间，`datetime-local` 输入框要的是
 *    `YYYY-MM-DDTHH:MM`，两边都不带时区；直接塞秒级 ISO 会让输入框空白。
 * 2. **互斥字段** —— once 只带 `at`、recurring 只带 `daily + weekdays`，两套字段同时
 *    上送后端会让 `add_schedule` 按 kind 丢掉一半，用户看到的是「填了没生效」。
 */

const WEEKDAY_KEYS = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'];

/** 秒级 ISO（`2026-11-23T09:00:00`）→ `datetime-local` 需要的 `2026-11-23T09:00`。 */
function localValue(iso) {
  const text = String(iso || '').trim();
  if (!text) return '';
  // 只截到分钟：带秒的字符串在 datetime-local 里会被当成非法值清空。
  const m = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})/.exec(text);
  return m ? m[1] : '';
}

/** 日程 → 表单草稿（编辑时预填；新建用空草稿）。 */
function draftFromSchedule(schedule) {
  const s = schedule || {};
  const kind = s.kind === 'recurring' ? 'recurring' : 'once';
  return {
    goal: String(s.goal || ''),
    kind,
    mode: ['agent', 'plan', 'ask'].includes(s.mode) ? s.mode : 'agent',
    mock: !!s.mock,
    at: kind === 'once' ? localValue(s.at || s.next_run) : '',
    time: kind === 'recurring' ? String(s.time || '09:00') : '09:00',
    weekdays: kind === 'recurring' && Array.isArray(s.weekdays) && s.weekdays.length
      ? s.weekdays.filter(w => WEEKDAY_KEYS.includes(w))
      : ['MO', 'TU', 'WE', 'TH', 'FR'],
  };
}

/**
 * 表单草稿 → `scheduleAdd` 载荷。返回 `{payload}` 或 `{error}`（给调用方 toast 用），
 * 校验规则与后端 `add_schedule` 一致，避免一次白跑子进程。
 */
function payloadFromDraft(draft) {
  const d = draft || {};
  const goal = String(d.goal || '').trim();
  if (!goal) return { error: '还没有目标' };
  const payload = { goal, mode: d.mode || 'agent', mock: !!d.mock };
  if (d.kind === 'recurring') {
    const time = String(d.time || '').trim();
    if (!/^\d{2}:\d{2}$/.test(time)) return { error: '请选择每天触发时间' };
    const weekdays = (d.weekdays || []).filter(w => WEEKDAY_KEYS.includes(w));
    if (!weekdays.length) return { error: '至少勾选一个星期几' };
    payload.daily = time;
    payload.weekdays = weekdays.join(',');
    return { payload };
  }
  const at = localValue(d.at);
  if (!at) return { error: '请选择触发时间' };
  payload.at = `${at}:00`;
  return { payload };
}

/** 卡片上一行「什么时候跑」的说明（不翻译 weekday，直接给中文星期与时间）。 */
function whenLabel(schedule) {
  const s = schedule || {};
  if (s.kind === 'recurring') {
    const days = (s.weekdays || []).join('·') || '—';
    return `每周 ${days} ${s.time || '—'}`;
  }
  return s.at || s.next_run || '—';
}

/** 日程状态 → 胶囊文案与 class 后缀；一次性已触发与手动停用要能分开读。 */
function stateOf(schedule) {
  const s = schedule || {};
  if (s.enabled) return { cls: 'on', label: '启用' };
  if (s.kind === 'once' && s.last_run_at) return { cls: 'off', label: '已触发' };
  return { cls: 'off', label: '已停用' };
}

export { WEEKDAY_KEYS, localValue, draftFromSchedule, payloadFromDraft, whenLabel, stateOf };
