/**
 * 审批单的纯逻辑（门禁页与对话页内联审批卡共用，避免两边各写一份动作表）。
 *
 * 只放「不碰网络、不碰 DOM」的部分：动作清单、状态文案、要不要自动续跑、
 * 内联卡该走哪种模式。真正的裁决请求在 `features/approvals/flow.js`。
 */

const APPROVAL_ACTIONS = {
  plan: [['approve', '批准执行'], ['cancel', '取消运行']],
  delivery: [['accept', '接受交付'], ['abort', '放弃交付']],
};

const APPROVAL_STATUS_LABEL = {
  pending: '等待裁决', decided: '已裁决，待 resume', consumed: '已消费',
};

const DECISION_LABEL = {
  approve: '批准执行', accept: '接受交付', cancel: '取消运行', abort: '放弃交付',
};

/** 这两个动作要接着调 resume（I1）；取消 / 放弃是让 run 按决定收尾。 */
const AUTO_RESUME_ACTIONS = ['approve', 'accept'];

function actionsFor(checkpoint) {
  return APPROVAL_ACTIONS[checkpoint] || [];
}

function statusLabel(status) {
  return APPROVAL_STATUS_LABEL[status] || String(status || '');
}

function decisionLabel(action) {
  return DECISION_LABEL[action] || String(action || '');
}

function shouldAutoResume(action) {
  return AUTO_RESUME_ACTIONS.includes(action);
}

/** 只留待裁决的单：已裁决的再点一次会被后端拒（审批单已消费）。 */
function pendingOf(items) {
  return (items || []).filter((item) => item && item.status === 'pending');
}

/**
 * run 目录名（去掉盘符与上级路径），与后端 `_validated_run_id` 同口径。
 * `run` 是 `{run_id?, dir?}`，`watchDir` 是当前监视目录（真实运行态才有）。
 */
function runIdFrom(run, watchDir) {
  const raw = (run && (run.run_id || run.dir)) || watchDir || '';
  return String(raw).replace(/\\/g, '/').split('/').filter(Boolean).pop() || '';
}

/**
 * 内联审批卡的三种模式：
 * - `offline`：没连 API / 没选 run —— 只能引导去门禁页（那边有完整说明）；
 * - `ready`：有 pending 单，就地渲染裁决按钮；
 * - `empty`：连上了但没有待裁决的单（可能已在别处处理）。
 */
function inlineApprovalMode({ realMode, apiBaseUrl, runId, approvals } = {}) {
  if (!realMode || !apiBaseUrl || !runId) return 'offline';
  return pendingOf(approvals).length ? 'ready' : 'empty';
}

export {
  APPROVAL_ACTIONS,
  APPROVAL_STATUS_LABEL,
  AUTO_RESUME_ACTIONS,
  DECISION_LABEL,
  actionsFor,
  decisionLabel,
  inlineApprovalMode,
  pendingOf,
  runIdFrom,
  shouldAutoResume,
  statusLabel,
};
