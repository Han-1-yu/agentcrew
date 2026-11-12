/**
 * 执行模式（对齐 WorkBuddy 的 Agent / Plan / Ask 三态）。
 *
 * agent = 自动执行（--auto，跳过人工闸门）
 * plan  = 计划审查（--gate plan --approval-mode deferred，计划完成后挂起审批，批准后执行）
 * ask   = 只读规划（--plan-only，只生成计划并落盘，不执行；可用「恢复」续跑执行）
 *
 * 状态只存 localStorage，键为 'agentcrew:nrMode'，命令框与新建弹窗共用。
 */

const MODE_KEY = 'agentcrew:nrMode';

const MODES = {
  agent: { mode: '自动执行', access: 'Full access' },
  plan:  { mode: '计划审查', access: '先批准' },
  ask:   { mode: '只读规划', access: '只规划' },
};

const MODE_ORDER = ['agent', 'plan', 'ask'];

function currentMode() {
  const v = localStorage.getItem(MODE_KEY);
  return MODES[v] ? v : 'agent';
}

function setMode(mode) {
  if (MODES[mode]) localStorage.setItem(MODE_KEY, mode);
}

function cycleMode() {
  const next = MODE_ORDER[(MODE_ORDER.indexOf(currentMode()) + 1) % MODE_ORDER.length];
  setMode(next);
  return next;
}

export { MODES, MODE_ORDER, currentMode, cycleMode, setMode };
