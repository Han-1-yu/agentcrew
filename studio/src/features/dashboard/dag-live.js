/**
 * F1：`task_transition` 到达时就地切节点 class，不等 500ms 的防抖快照。
 *
 * 边界（与方案一致）：
 * - 只改**现有节点**的 class / data-status / chip 文案 / 描边宽度——不重建 SVG、
 *   不加逐帧动画。节点有增删或布局变化时，仍由 500ms 快照刷新整图兜齐；
 * - 看板不在前台时 `#dag-svg` 里找不到节点，此时只更新 `S.dash.tasks`，
 *   回看板时 `studio:dashboard-show` 的全量重绘会带回正确状态。
 */
import { STATUS_META } from '../../contracts/enums.js';

import { S } from '../../lib/store.js';
import { applyTransition, transitionPatch } from '../../lib/task-live.js';

const KNOWN = new Set(Object.keys(STATUS_META));

/** 在当前 DAG SVG 里找任务节点（`data-task` 是渲染时写死的任务 id）。 */
function findTaskNode(taskId) {
  const svg = document.getElementById('dag-svg');
  if (!svg) return null;
  for (const node of svg.querySelectorAll('.dag-node')) {
    if (node.dataset.task === String(taskId)) return node;
  }
  return null;
}

/** 状态色 / 运行标识 / chip 文案与描边宽度：与 dag-task-view.js 的渲染口径一致。 */
function syncTaskNode(task) {
  const node = findTaskNode(task.id);
  if (!node) return false;
  const key = KNOWN.has(task.status) ? task.status : 'unknown';
  node.setAttribute('data-status', key);
  node.classList.toggle('running', task.status === 'running');
  const chip = node.querySelector('.node-chip-label');
  if (chip) chip.textContent = (STATUS_META[key] || STATUS_META.unknown).label;
  const box = node.querySelector('.node-box');
  if (box) {
    box.setAttribute('stroke-width', task.status === 'running'
      ? '2.4' : task.status === 'succeeded' ? '1.8' : '1.3');
  }
  return true;
}

/**
 * 处理一条 task_transition：折补丁 → 改内存任务 → 同步节点。
 * @returns {object|null} 被更新的任务（找不到任务 / 载荷不合法时 null）
 */
function applyTaskTransitionLive(data) {
  const patch = transitionPatch(data);
  if (!patch) return null;
  const tasks = (S.dash && S.dash.tasks) || [];
  const task = tasks.find((t) => t.id === data.task_id);
  if (!task) return null;
  applyTransition(task, patch);
  syncTaskNode(task);
  return task;
}

export { applyTaskTransitionLive, syncTaskNode };
