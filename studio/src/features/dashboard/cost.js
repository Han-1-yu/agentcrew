/**
 * 成本区块：总览 / 按 Agent / Top5 任务三段。
 */

import { ROLE_META } from '../../contracts/enums.js';
import { openTaskDetail } from '../detail/panel.js';
import { aggregateCostByModel } from '../../lib/cost-breakdown.js';
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

  // D3：按模型 / 供应商聚合。llm_call 是唯一带 model 的成本事件；
  // 供应商推不出时留空，不按模型名硬猜。
  const models = aggregateCostByModel(S.dash.events || [], {
    defaultModel: (run?.config_snapshot || {}).default_model || run?.model || '',
    limit: 6,
  });
  const modelDelta = Math.abs(models.total - totalCost);

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
      topTasks.length === 0 ? h('div', { class: 'cost-empty' }, '暂无任务成本数据') : null),
    // 按模型分布（占满一行，避免 3 列网格塞第 4 块后错位）
    h('div', { class: 'cost-by-model' },
      h('div', { class: 'cost-section-title' },
        models.rows.length ? `按模型分布 · 合计 $${models.total.toFixed(4)}` : '按模型分布'),
      ...models.rows.map((r) => {
        const label = r.provider ? `${r.provider} / ${r.model}` : r.model;
        const w = (r.cost / models.max * 100).toFixed(1);
        return h('div', { class: 'cost-model-row' },
          h('span', { class: 'cost-model-name', title: label }, label),
          h('div', { class: 'cost-model-bar' }, h('div', { class: 'cost-model-fill', style: `width:${w}%` })),
          h('span', { class: 'cost-model-calls' }, `${r.calls} 次`),
          h('span', { class: 'cost-model-val' }, `$${r.cost.toFixed(4)}`));
      }),
      models.rows.length === 0 ? h('div', { class: 'cost-empty' }, '本次运行还没有 LLM 调用记录') : null,
      models.restCount ? h('div', { class: 'cost-model-note' }, `另有 ${models.restCount} 个模型未展开`) : null,
      // 守恒核对：正常应与 run.json stats 一致；ledger 被截断或数据缺失时如实提示。
      models.rows.length && modelDelta > 0.00005
        ? h('div', { class: 'cost-model-note' },
          `ledger 合计 $${models.total.toFixed(4)} · run.json 统计 $${totalCost.toFixed(4)}`)
        : null)));
}

export { renderCostPanel };
