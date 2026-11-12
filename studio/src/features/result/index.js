/**
 * 右侧结果区（对齐 WorkBuddy 的「结果区」）：概览 + 产物 双 tab 一体化面板。
 *
 * 概览 tab 汇总运行统计（任务 / LLM / token / 成本 / 耗时）、任务状态分布与
 * 三层门禁；产物 tab 复用产物页的文件清单 + 内联预览 + 语法高亮。面板复用
 * ``#detail-panel`` 容器（同一套遮罩 / Esc 关闭 / 淡入动效）。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';
import { RUN_META, STATUS_META, STATUS_ORDER } from '../../contracts/enums.js';
import { fmtBytes, fmtCost, fmtDur, fmtTokens } from '../../lib/format.js';

import { emptyState, loadingState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';

import { openDetailPanel } from '../detail/panel.js';
import { ARTIFACT_ICONS, highlightCode } from '../artifacts/index.js';

function currentTab() { return S.resultTab || 'overview'; }

function openResultPanel(tab) {
  S.resultTab = tab || currentTab();
  openDetailPanel(buildResultPanel());
}

function tabBar() {
  const tab = currentTab();
  const mk = (key, label) => h('button', {
    class: `result-tab${tab === key ? ' active' : ''}`,
    onclick: () => openResultPanel(key),
  }, label);
  return h('div', { class: 'result-tabs' }, mk('overview', '概览'), mk('artifacts', '产物'));
}

function buildResultPanel() {
  return h('div', { style: 'display:flex;flex-direction:column;height:100%' },
    h('div', { class: 'dp-head' },
      h('div', { class: 'dp-title-row' },
        h('div', { class: 'dp-title' }, '结果'),
        h('button', { class: 'btn btn-icon', style: 'width:28px;height:28px', 'aria-label': '关闭', title: '关闭',
          onclick: () => document.getElementById('detail-mask').click() }, '✕')),
      tabBar()),
    h('div', { class: 'dp-body', style: 'padding:0' },
      currentTab() === 'overview' ? renderOverview() : mountArtifactsTab()));
}

/* ---------- 概览 tab ---------- */

/* 已运行时长：结束的 run 用 finished_at - started_at，运行中用 now - started_at。 */
function runDuration(run, stats) {
  const start = run.started_at ? Date.parse(String(run.started_at).replace(' ', 'T')) : NaN;
  if (Number.isNaN(start)) return '—';
  const end = stats.finished_at ? Date.parse(String(stats.finished_at).replace(' ', 'T')) : Date.now();
  return fmtDur(Math.max(0, end - start));
}

function renderOverview() {
  const run = S.dash && S.dash.run;
  const stats = (run && run.stats) || {};
  const tasks = (S.dash && S.dash.tasks) || [];
  const gates = (S.dash && S.dash.gates) || [];
  if (!run) return h('div', { style: 'padding:16px' }, emptyState('暂无运行', '选择一次运行后查看结果概览。'));

  const rm = RUN_META[run.status] || RUN_META.idle;
  const body = h('div', { class: 'result-body' });

  // 状态 + 目标
  body.appendChild(h('div', { class: 'result-head' },
    h('span', { class: 'dot', style: `background:${run.status === 'completed' ? 'var(--success)' : (run.status === 'failed' ? 'var(--error)' : 'var(--accent)')}` }),
    h('span', { class: 'result-status' }, rm.label),
    h('span', { class: 'result-goal', title: run.project_goal }, run.project_goal || '')));

  // 统计网格
  const cell = (v, k) => h('div', { class: 'result-cell' },
    h('div', { class: 'result-cell-v' }, v),
    h('div', { class: 'result-cell-k' }, k));
  body.appendChild(h('div', { class: 'result-grid' },
    cell(String(stats.total_tasks ?? tasks.length), '任务'),
    cell(String(stats.succeeded ?? 0), '成功'),
    cell(String(stats.failed ?? 0), '失败'),
    cell(String(stats.llm_calls ?? 0), 'LLM 调用'),
    cell(fmtTokens((stats.prompt_tokens || 0) + (stats.completion_tokens || 0)), 'Token'),
    cell(fmtCost(stats.estimated_cost_usd), '成本'),
    cell(fmtDur(runDuration(run, stats)), '已运行'),
    cell(fmtCost(run.budget_limit_usd), '预算上限')));

  // 任务状态分布
  const dist = {};
  STATUS_ORDER.forEach((s) => { dist[s] = tasks.filter((t) => t.status === s).length; });
  const total = tasks.length || 1;
  body.appendChild(h('div', { class: 'result-sec-title' }, '任务状态'));
  const bar = h('div', { class: 'result-dist' });
  for (const s of STATUS_ORDER) {
    if (!dist[s]) continue;
    const m = STATUS_META[s] || {};
    bar.appendChild(h('div', { class: 'result-dist-row' },
      h('span', { class: 'result-dist-name' }, m.label || s),
      h('div', { class: 'result-dist-track' },
        h('div', { class: 'result-dist-fill', style: `width:${Math.round(dist[s] / total * 100)}%;background:${m.color}` })),
      h('span', { class: 'result-dist-n' }, String(dist[s]))));
  }
  body.appendChild(bar);

  // 门禁汇总
  if (gates.length) {
    body.appendChild(h('div', { class: 'result-sec-title' }, '质量门禁'));
    for (const g of gates) {
      const ok = g.status === 'passed';
      body.appendChild(h('div', { class: 'result-gate' },
        h('span', { class: `result-gate-dot ${g.status}` }),
        h('span', { class: 'result-gate-name' }, g.name),
        h('span', { class: 'result-gate-verdict' }, g.verdict || (ok ? '通过' : '待触发'))));
    }
  }

  return body;
}

/* ---------- 产物 tab ---------- */

function mountArtifactsTab() {
  const wrap = h('div', { class: 'result-art' });
  renderArtifactsInto(wrap);
  return wrap;
}

async function renderArtifactsInto(wrap) {
  const run = S.dash && S.dash.run;
  if (!run || !run.dir) {
    wrap.appendChild(h('div', { style: 'padding:16px' }, emptyState('未选择运行', '请先选择一次运行。')));
    return;
  }
  wrap.appendChild(loadingState('正在读取产物清单…'));
  let entries = [];
  try {
    entries = await window.studio.listArtifacts(run.dir);
  } catch (e) {
    wrap.innerHTML = '';
    wrap.appendChild(h('div', { style: 'padding:16px' }, emptyState('产物加载失败', String(e.message || e))));
    return;
  }
  wrap.innerHTML = '';

  if (!entries || !entries.length) {
    wrap.appendChild(h('div', { style: 'padding:16px' }, emptyState('该运行暂无产物', '任务完成后产物会出现在这里。')));
    return;
  }

  const toolbar = h('div', { class: 'result-art-toolbar' },
    h('button', { class: 'btn btn-sm', onclick: async () => {
      try { await window.studio.exportArtifacts(run.dir); toast('success', '已导出', '产物已打包为 ZIP'); }
      catch (e) { toast('error', '导出失败', String(e.message || e)); }
    } }, '导出 ZIP'));
  const list = h('div', { class: 'result-art-list' });
  const prev = h('div', { class: 'result-art-preview' });
  wrap.appendChild(toolbar);
  wrap.appendChild(list);
  wrap.appendChild(prev);

  // 分组
  const groups = {};
  entries.forEach((e) => {
    const dir = (e.path || '').split('/')[0] || '根目录';
    (groups[dir] = groups[dir] || []).push(e);
  });
  for (const [dir, files] of Object.entries(groups)) {
    list.appendChild(h('div', { class: 'art-dir' }, dir));
    for (const f of files) {
      list.appendChild(h('div', { class: 'art-file', onclick: () => previewResult(prev, f) },
        h('span', { class: 'art-file-icon' }, ARTIFACT_ICONS[f.kind] || '📄'),
        h('span', { class: 'art-file-name', title: f.path }, (f.path || '').split('/').pop()),
        h('span', { class: 'art-file-size' }, fmtBytes(f.sizeBytes))));
    }
  }
  prev.appendChild(h('div', { style: 'padding:16px' }, emptyState('选择文件查看内容')));
}

async function previewResult(prev, f) {
  prev.innerHTML = '';
  prev.appendChild(h('div', { class: 'result-art-head' },
    h('span', { class: 'result-art-path' }, f.path || ''),
    h('span', { class: 'result-art-meta' }, `${fmtBytes(f.sizeBytes)} · ${f.role || '—'} · ${f.task || '—'}`)));
  const content = h('pre', { class: 'result-art-content' });
  content.textContent = '加载中…';
  prev.appendChild(content);
  try {
    const r = await window.studio.readArtifact(S.dash.run.dir, f.path);
    const text = r && r.content != null ? String(r.content) : (r && typeof r === 'string' ? r : '');
    if (!text) { content.textContent = '（空文件或二进制）'; return; }
    const truncated = text.length > 100000 ? text.slice(0, 100000) + '\n…（已截断）' : text;
    content.innerHTML = highlightCode(truncated, f.path || '');
  } catch (e) {
    content.textContent = '读取失败：' + String(e.message || e);
  }
}

export { openResultPanel };
