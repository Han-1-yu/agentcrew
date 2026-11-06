/**
 * 运行对比：两个 run 的指标差异表。
 */
import { h } from '../../lib/dom.js';
import { fmtDateTime, fmtTokens, simplifyGoal } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';
async function openCompareModal(otherRun) {
  // 加载另一个 run 的快照（run.json + tasks.json）与事件尾部
  let otherData = null;
  try {
    const [snapshot, events] = await Promise.all([
      window.studio.loadRun(otherRun.dir),
      window.studio.ledgerHistory(otherRun.dir, 400).catch(() => []),
    ]);
    otherData = {
      run: (snapshot && snapshot.run) || null,
      tasks: (snapshot && snapshot.tasks) || [],
      events: events || [],
    };
  } catch (e) {
    toast('error', '加载失败', String(e.message || e));
    return;
  }

  const cur = S.dash.run, curTasks = S.dash.tasks || [];
  const curCost = cur?.stats?.estimated_cost_usd || 0;
  const otherCost = otherData.run?.stats?.estimated_cost_usd || 0;
  const curTokens = (cur?.stats?.prompt_tokens || 0) + (cur?.stats?.completion_tokens || 0);
  const otherTokens = (otherData.run?.stats?.prompt_tokens || 0) + (otherData.run?.stats?.completion_tokens || 0);
  const curSuccess = curTasks.filter(t => t.status === 'passed' || t.status === 'succeeded').length;
  const otherSuccess = otherData.tasks.filter(t => t.status === 'passed' || t.status === 'succeeded').length;
  const curFailed = curTasks.filter(t => t.status === 'failed').length;
  const otherFailed = otherData.tasks.filter(t => t.status === 'failed').length;

  const rows = [
    ['状态', cur?.status || '—', otherData.run?.status || '—'],
    ['任务数', String(curTasks.length), String(otherData.tasks.length)],
    ['成功', String(curSuccess), String(otherSuccess)],
    ['失败', String(curFailed), String(otherFailed)],
    ['费用', `$${curCost.toFixed(4)}`, `$${otherCost.toFixed(4)}`],
    ['Token', fmtTokens(curTokens), fmtTokens(otherTokens)],
    ['LLM 调用', String(cur?.stats?.llm_calls || 0), String(otherData.run?.stats?.llm_calls || 0)],
    ['模型', cur?.model || cur?.config_snapshot?.default_model || '—', otherData.run?.model || otherData.run?.config_snapshot?.default_model || '—'],
    ['创建时间', fmtDateTime(cur?.created_at || cur?.start_time), fmtDateTime(otherData.run?.created_at || otherData.run?.start_time)],
  ];

  // h() 对字符串子节点走 createTextNode，不解析 HTML。这里必须返回**节点**，
  // 否则「差值」列会把 <span class="cmp-diff ..."> 当成字面文本显示出来。
  const diff = (a, b) => {
    const na = parseFloat(a), nb = parseFloat(b);
    if (!Number.isFinite(na) || !Number.isFinite(nb)) return null;
    const d = na - nb;
    if (Math.abs(d) < 0.0001) return h('span', { class: 'cmp-diff zero' }, '0');
    return h('span', { class: `cmp-diff ${d > 0 ? 'pos' : 'neg'}` },
      `${d > 0 ? '+' : ''}${d.toFixed(4)}`);
  };

  showModal(h('div', { class: 'cmp-modal' },
    h('h3', { class: 'modal-title' }, '运行对比'),
    h('div', { class: 'cmp-wrap' },
    h('div', { class: 'cmp-headers' },
      h('div', { class: 'cmp-header' }, '指标'),
      h('div', { class: 'cmp-header cur' }, simplifyGoal(cur?.goal || cur?.run_id || '当前')),
      h('div', { class: 'cmp-header' }, simplifyGoal(otherRun.goal || otherRun.runId || '对比')),
      h('div', { class: 'cmp-header' }, '差值')),
    ...rows.map(([label, a, b]) => h('div', { class: 'cmp-row' },
      h('div', { class: 'cmp-label' }, label),
      h('div', { class: 'cmp-val' }, String(a)),
      h('div', { class: 'cmp-val' }, String(b)),
      h('div', { class: 'cmp-diff-cell' }, diff(a, b)))))));
}

export { openCompareModal };
