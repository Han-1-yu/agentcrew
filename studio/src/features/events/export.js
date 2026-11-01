/**
 * 导出：事件 JSONL / 运行快照 / Markdown 报告。
 */
import { ROLE_META } from '../../contracts/enums.js';

import { fmtDur, fmtTokens } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

import { summarizeEvent } from './index.js';
/* 导出事件为 JSON 文件下载 */
function exportEvents() {
  const events = S.dash.events || [];
  if (!events.length) { toast('warning', '无事件', '当前运行没有事件记录'); return; }
  const data = JSON.stringify({ run_id: S.dash.run.run_id, exported_at: new Date().toISOString(), count: events.length, events }, null, 2);
  const blob = new Blob([data], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `events-${S.dash.run.run_id || 'export'}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('success', '已导出', `${events.length} 条事件`);
}

/* 导出完整运行数据（run + tasks + events）为 JSON */
function exportRun() {
  const run = S.dash.run; if (!run) { toast('warning', '无运行', '当前没有可导出的运行'); return; }
  const data = {
    exported_at: new Date().toISOString(),
    run: run,
    tasks: S.dash.tasks || [],
    agents: S.dash.agents || {},
    gates: S.dash.gates || [],
    events: S.dash.events || [],
  };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `run-${run.run_id || 'export'}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('success', '已导出', `运行 ${run.run_id} 完整数据`);
}

/* 导出 Markdown 格式运行报告 */
function exportReport() {
  const run = S.dash.run; if (!run) { toast('warning', '无运行', '当前没有可导出的运行'); return; }
  const tasks = S.dash.tasks || [];
  const agents = S.dash.agents || {};
  const gates = S.dash.gates || [];
  const events = S.dash.events || [];
  const st = run.stats || {};

  const success = tasks.filter(t => t.status === 'passed' || t.status === 'succeeded').length;
  const failed = tasks.filter(t => t.status === 'failed').length;
  const running = tasks.filter(t => t.status === 'running').length;
  const pending = tasks.filter(t => ['pending', 'ready', 'blocked'].includes(t.status)).length;

  let md = `# AgentCrew 运行报告\n\n`;
  md += `## 基本信息\n\n`;
  md += `- **Run ID**: ${run.run_id || '—'}\n`;
  md += `- **目标**: ${run.goal || '—'}\n`;
  md += `- **状态**: ${run.status || '—'}\n`;
  md += `- **模型**: ${run.model || run.config_snapshot?.default_model || '—'}\n`;
  md += `- **创建时间**: ${run.created_at || run.start_time || '—'}\n`;
  md += `- **更新时间**: ${run.updated_at || '—'}\n\n`;

  md += `## 统计\n\n`;
  md += `| 指标 | 值 |\n|---|---|\n`;
  md += `| 任务总数 | ${tasks.length} |\n`;
  md += `| 成功 | ${success} |\n`;
  md += `| 失败 | ${failed} |\n`;
  md += `| 运行中 | ${running} |\n`;
  md += `| 等待中 | ${pending} |\n`;
  md += `| LLM 调用 | ${st.llm_calls || 0} |\n`;
  md += `| 输入 Token | ${fmtTokens(st.prompt_tokens || 0)} |\n`;
  md += `| 输出 Token | ${fmtTokens(st.completion_tokens || 0)} |\n`;
  md += `| 估算费用 | $${(st.estimated_cost_usd || 0).toFixed(4)} |\n`;
  md += `| 预算上限 | $${(run.budget_limit_usd || 0).toFixed(2)} |\n\n`;

  md += `## 任务列表\n\n`;
  md += `| ID | 标题 | 负责 | 状态 | 耗时 | 费用 |\n|---|---|---|---|---|---|\n`;
  for (const t of tasks) {
    const dur = t.result?.duration_ms ? fmtDur(t.result.duration_ms) : '—';
    const cost = t.result?.cost_usd ? `$${t.result.cost_usd.toFixed(4)}` : '—';
    md += `| ${t.id} | ${(t.title || '').replace(/\|/g, '\\|')} | ${t.role || '—'} | ${t.status || '—'} | ${dur} | ${cost} |\n`;
  }
  md += `\n`;

  md += `## Agent 状态\n\n`;
  md += `| 角色 | 状态 | 当前任务 | 已完成 | 失败 | Token | 费用 |\n|---|---|---|---|---|---|---|\n`;
  for (const [role, a] of Object.entries(agents)) {
    const meta = ROLE_META[role] || { label: role };
    md += `| ${meta.label || role} | ${a.status || '—'} | ${a.task || '—'} | ${a.done || 0} | ${a.fail || 0} | ${fmtTokens(a.tokens || 0)} | $${(a.cost || 0).toFixed(4)} |\n`;
  }
  md += `\n`;

  if (gates.length) {
    md += `## 质量门禁\n\n`;
    md += `| 层级 | 状态 | 通过 | 失败 | 结论 |\n|---|---|---|---|---|\n`;
    for (const g of gates) {
      md += `| ${g.layer || g.name || '—'} | ${g.status || '—'} | ${g.checks?.passed || 0} | ${g.checks?.failed || 0} | ${g.verdict || '—'} |\n`;
    }
    md += `\n`;
  }

  md += `## 最近事件（前 20 条）\n\n`;
  for (const e of events.slice(-20)) {
    md += `- \`${e.ts || ''}\` **${e.event || ''}** — ${summarizeEvent(e) || ''}\n`;
  }
  md += `\n---\n*由 AgentCrew Studio 生成于 ${new Date().toISOString()}*\n`;

  const blob = new Blob([md], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `agentcrew-report-${run.run_id || 'run'}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast('success', '已导出', `运行报告 ${run.run_id || ''}.md`);
}

export { exportEvents, exportReport, exportRun };
