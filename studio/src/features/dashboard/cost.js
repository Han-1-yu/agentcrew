/**
 * 成本区块：总览 / 按 Agent / Top5 任务三段。
 */

import { ROLE_META } from '../../contracts/enums.js';
import { openTaskDetail } from '../detail/panel.js';
import { $, h } from '../../lib/dom.js';
import { fmtTokens, trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';


function renderCostPanel() {
  const panel = $('#cost-panel'); if (!panel) return;
  const run = S.dash.run, st = run?.stats || {}, tasks = S.dash.tasks || [];
  const totalCost = st.estimated_cost_usd || 0;
  const budget = run?.budget_limit_usd || 1;
  const pct = Math.min(100, totalCost / budget * 100);
  const promptTokens = st.prompt_tokens || 0;
  const completionTokens = st.completion_tokens || 0;
  const llmCalls = st.llm_calls || 0;

  // 按 Agent 汇总成本
  const byAgent = {};
  for (const t of tasks) {
    const role = t.role || 'unknown';
    const cost = (t.result && t.result.cost_usd) || 0;
    const toks = (t.result && t.result.tokens) || 0;
    if (!byAgent[role]) byAgent[role] = { cost: 0, tokens: 0, count: 0 };
    byAgent[role].cost += cost;
    byAgent[role].tokens += toks;
    byAgent[role].count++;
  }
  const agentEntries = Object.entries(byAgent).sort((a, b) => b[1].cost - a[1].cost);
  const maxAgentCost = Math.max(...agentEntries.map(e => e[1].cost), 0.001);

  // Top 5 最贵任务
  const topTasks = [...tasks]
    .filter(t => t.result && t.result.cost_usd)
    .sort((a, b) => (b.result.cost_usd || 0) - (a.result.cost_usd || 0))
    .slice(0, 5);

  panel.innerHTML = '';
  panel.appendChild(h('div', { class: 'cost-grid' },
    // 总览卡片
    h('div', { class: 'cost-overview' },
      h('div', { class: 'cost-row' },
        h('div', { class: 'cost-big' }, `$${totalCost.toFixed(4)}`),
        h('div', { class: 'cost-sub' }, `预算 $${budget.toFixed(2)} · ${Math.round(pct)}%`)),
      h('div', { class: 'cost-bar' },
        h('div', { class: `cost-bar-fill ${pct >= 100 ? 'err' : pct >= 90 ? 'warn' : ''}`, style: `width:${pct}%` })),
      h('div', { class: 'cost-meta' },
        h('span', {}, `LLM 调用 ${llmCalls} 次`),
        h('span', {}, `输入 ${fmtTokens(promptTokens)}`),
        h('span', {}, `输出 ${fmtTokens(completionTokens)}`))),
    // 按 Agent 分布
    h('div', { class: 'cost-by-agent' },
      h('div', { class: 'cost-section-title' }, '按 Agent 分布'),
      ...agentEntries.map(([role, d]) => {
        const meta = ROLE_META[role] || { label: role, color: 'var(--text-ter)' };
        const w = (d.cost / maxAgentCost * 100).toFixed(1);
        return h('div', { class: 'cost-agent-row' },
          h('span', { class: 'cost-agent-name', style: `color:${meta.color}` }, meta.label || role),
          h('div', { class: 'cost-agent-bar' }, h('div', { class: 'cost-agent-fill', style: `width:${w}%;background:${meta.color}` })),
          h('span', { class: 'cost-agent-val' }, `$${d.cost.toFixed(4)}`));
      }),
      agentEntries.length === 0 ? h('div', { class: 'cost-empty' }, '暂无成本数据') : null),
    // Top 5 任务
    h('div', { class: 'cost-top-tasks' },
      h('div', { class: 'cost-section-title' }, '最贵任务 Top 5'),
      ...topTasks.map(t => h('div', { class: 'cost-task-row', onclick: () => openTaskDetail(t.id) },
        h('span', { class: 'cost-task-id' }, t.id),
        h('span', { class: 'cost-task-title' }, trunc(t.title, 24)),
        h('span', { class: 'cost-task-val' }, `$${(t.result.cost_usd || 0).toFixed(4)}`))),
      topTasks.length === 0 ? h('div', { class: 'cost-empty' }, '暂无任务成本数据') : null)));
}

export { renderCostPanel };
