/**
 * Agents 页面：8 角色负载与产出统计。
 */
import { agentStatusLabel, ROLE_META } from '../../contracts/enums.js';

import { h, s } from '../../lib/dom.js';
import { fmtDur, fmtTokens, trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { switchNav } from '../../app/router.js';

import { openTaskDetail } from '../detail/panel.js';
import { buildAgentSwitch } from './switch.js';
function renderAgentsPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, 'Agents'),
    h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回')));
  const grid = h('div', { class: 'agents-full-grid' });
  container.appendChild(grid);
  const agents = S.dash.agents || {};
  for (const [role, a] of Object.entries(agents)) {
    const meta = ROLE_META[role] || { label: role, color: 'var(--text-ter)' };
    const myTasks = S.dash.tasks.filter(t => t.role === role);
    // 派生统计
    const totalCost = myTasks.reduce((s, t) => s + ((t.result && t.result.cost_usd) || 0), 0);
    const totalRetries = myTasks.reduce((s, t) => s + Math.max(0, (t.attempts || 1) - 1), 0);
    const durations = myTasks.filter(t => t.result && t.result.duration_ms).map(t => t.result.duration_ms);
    const avgDur = durations.length ? durations.reduce((s, v) => s + v, 0) / durations.length : 0;
    const total = myTasks.length || 1;
    const successRate = Math.round((a.done || 0) / total * 100);
    grid.appendChild(h('div', { class: 'agent-full-card' },
      h('div', { class: 'agent-full-head' },
        h('span', { class: 'agent-full-dot', style: `background:${meta.color}` }),
        h('span', { class: 'agent-full-name' }, meta.label || role),
        h('span', { class: `agent-full-status status-${a.status}` }, agentStatusLabel(a.status))),
      h('div', { class: 'agent-full-body' },
        h('div', { class: 'agent-full-row' }, h('span', { class: 'k' }, '当前任务'), h('span', { class: 'v' }, a.task || '—')),
        h('div', { class: 'agent-full-row' }, h('span', { class: 'k' }, '最近动作'), h('span', { class: 'v' }, a.action || '—')),
        buildAgentSwitch(role),
        h('div', { class: 'agent-full-stats' },
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, `${a.done || 0}`), h('div', { class: 'af-stat-label' }, '已完成')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, `${a.fail || 0}`), h('div', { class: 'af-stat-label' }, '失败')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, `${successRate}%`), h('div', { class: 'af-stat-label' }, '成功率')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, `${totalRetries}`), h('div', { class: 'af-stat-label' }, '重试')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, fmtTokens(a.tokens || 0)), h('div', { class: 'af-stat-label' }, 'Token')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, totalCost ? `$${totalCost.toFixed(3)}` : '—'), h('div', { class: 'af-stat-label' }, '费用')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, avgDur ? fmtDur(avgDur) : '—'), h('div', { class: 'af-stat-label' }, '平均耗时')),
          h('div', { class: 'af-stat' }, h('div', { class: 'af-stat-val' }, a.model || '—'), h('div', { class: 'af-stat-label' }, '模型'))),
        h('div', { class: 'agent-full-tasks' },
          h('div', { class: 'agent-full-tasks-title' }, `任务列表（${myTasks.length}）`),
          ...myTasks.map(t => h('div', { class: 'agent-full-task-item',
            onclick: () => { switchNav('dashboard'); setTimeout(() => openTaskDetail(t.id), 100); } },
            h('span', { class: `ts-dot ts-${t.status}` }),
            h('span', { class: 'agent-full-task-id' }, t.id),
            h('span', { class: 'agent-full-task-title' }, trunc(t.title, 30))))))));
  }
}

/* ============================================================
   门禁全屏页
   ============================================================ */

export { renderAgentsPage };
