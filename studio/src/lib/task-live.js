/**
 * F1 的纯函数部分：把 `task_transition` 事件折成任务对象补丁。
 *
 * 只返回**事件里真实出现**的字段，避免把 `undefined` 当成「清空」：后端
 * `task_transition` 的载荷随迁移而不同（有的带 attempts，有的只带 from/to），
 * 无脑 Object.assign 会把任务上的统计抹掉。
 */

const STATUS_VALUES = new Set([
  'pending', 'ready', 'running', 'blocked', 'review', 'rework',
  'succeeded', 'failed', 'skipped', 'cancelled',
]);

/** 事件载荷 → 补丁；没有可用的目标状态时返回 null。 */
function transitionPatch(data) {
  if (!data || typeof data !== 'object') return null;
  const rawTo = data.to_status != null ? data.to_status : data.status;
  if (rawTo == null) return null;
  const status = String(rawTo).trim();
  if (!STATUS_VALUES.has(status)) return null;
  const patch = { status };
  if (data.attempts != null) patch.attempts = Number(data.attempts);
  if (data.rework_count != null) patch.rework_count = Number(data.rework_count);
  if (data.error != null && String(data.error).trim()) patch.error = String(data.error);
  return patch;
}

/** 把补丁落到任务对象上；返回实现的状态迁移（用于判断是否需要同步 DOM）。 */
function applyTransition(task, patch) {
  if (!task || !patch) return null;
  const from = task.status || '';
  Object.assign(task, patch);
  return { from, to: task.status };
}

export { applyTransition, transitionPatch };
