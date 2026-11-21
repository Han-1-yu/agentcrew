/**
 * 输入框语义分流（A2）：同一根输入框按当前 run 状态决定行为。
 *
 * 纯函数、零 DOM，判定规则可单测；副作用（REST / CLI / guidance POST）
 * 全部留在 features/composer/submit.js。
 *  - create：空态 / 终态 / 演示模式 → 发送 = 新建运行；
 *  - guidance：run 处于可注入指引的状态（与后端 _GUIDANCE_RUNNABLE 同口径）
 *    → 发送 = POST /runs/{id}/guidance，同时提供「停止」；
 *  - stop：run 活跃但引擎尚不接受指引（planning / preparing）→ 只能停。
 */

/** 与后端 server.py 的 _GUIDANCE_RUNNABLE 一致：running / awaiting_approval。 */
const GUIDANCE_RUNNABLE = new Set(['running', 'awaiting_approval']);

/** 仍在跑、可取消的其余活跃态。 */
const ACTIVE_RUNNABLE = new Set([...GUIDANCE_RUNNABLE, 'planning', 'preparing', 'review']);

/** 终态：发送 = 基于输入新建 run（「基于此对话再问」的过渡形态）。 */
const TERMINAL_RUN = new Set([
  'completed', 'failed', 'cancelled', 'interrupted', 'planned', 'paused',
]);

/**
 * 判定输入框当前语义。
 * @returns {'create'|'guidance'|'stop'}
 */
function resolveComposerIntent({ hasRun, runStatus, realMode } = {}) {
  if (!realMode) return 'create';
  if (!hasRun) return 'create';
  const st = String(runStatus || 'idle');
  if (GUIDANCE_RUNNABLE.has(st)) return 'guidance';
  if (ACTIVE_RUNNABLE.has(st)) return 'stop';
  return 'create';
}

/** 发送键是否应切换成「停止整个运行」。 */
function canStopRun({ hasRun, runStatus, realMode } = {}) {
  if (!realMode || !hasRun) return false;
  return ACTIVE_RUNNABLE.has(String(runStatus || ''));
}

/** run 是否终态（含 paused：挂起的 run 不接受新指引）。 */
function isTerminalRun(status) {
  return TERMINAL_RUN.has(String(status || ''));
}

export {
  ACTIVE_RUNNABLE,
  GUIDANCE_RUNNABLE,
  TERMINAL_RUN,
  canStopRun,
  isTerminalRun,
  resolveComposerIntent,
};
