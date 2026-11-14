/**
 * 依赖图入口：Agent 视图（默认）+ 任务视图，共用一份 S.dash.tasks。
 * 图模型由 dashboard 装配层一次计算并传入，避免画布、摘要、窄屏列表重复构建。
 */
import { STATUS_META, roleMeta } from '../../contracts/enums.js';

import { $, h, s } from '../../lib/dom.js';
import { trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { openTaskDetail } from '../detail/panel.js';

import { AGENT_GAP_X, AGENT_GAP_Y, AH, AW, COL_GAP, NH, NW, ROW_GAP, wireDAGControls } from './dag-canvas.js';
import { buildAgentGraph } from './agent-graph.js';
import { renderAgentDAG } from './dag-agent-view.js';
import { computeLayers, layoutDAG, renderTaskDAG } from './dag-task-view.js';

function currentMode() { return S.dagMode === 'task' ? 'task' : 'agent'; }
function buildDagModel(tasks, mode = currentMode()) {
  if (!tasks.length && mode === 'task') return { byId: new Map(), layers: [], pos: new Map(), width: 0, height: 0 };
  return mode === 'task' ? layoutDAG(tasks) : buildAgentGraph(tasks);
}

function syncDagChrome(model, tasks, mode) {
  const seg = $('#dag-mode-switch');
  if (seg) {
    seg.querySelectorAll('[data-dag-mode]').forEach(btn => {
      const on = btn.dataset.dagMode === mode;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
      if (!btn.dataset.bound) {
        btn.dataset.bound = '1';
        btn.addEventListener('click', () => {
          if (currentMode() === btn.dataset.dagMode) return;
          S.dagMode = btn.dataset.dagMode;
          const nextTasks = (S.dash && S.dash.tasks) || [];
          const nextModel = buildDagModel(nextTasks, currentMode());
          renderDAG(nextModel);
          renderDAGFallback(nextModel);
        });
      }
    });
  }
  const summary = $('#dag-summary');
  if (summary) {
    if (!tasks.length) summary.textContent = '等待任务派发';
    else if (mode === 'task') {
      const deps = tasks.reduce((n, t) => n + (t.dependencies || []).length, 0);
      summary.textContent = `${tasks.length} 个任务 · ${deps} 条依赖 · ${model.layers.length} 层`;
    } else {
      summary.textContent = `${model.nodes.length} 个 Agent · ${model.edges.length} 条依赖 · ${model.layers.length} 层`;
    }
  }
  const hint = $('#dag-hint');
  if (hint) {
    hint.textContent = mode === 'task'
      ? '箭头指向下游任务；点节点看任务详情'
      : '箭头指向下游 Agent；线宽 = 跨 Agent 依赖任务数';
  }
}

function renderDAG(model = null) {
  const svg = $('#dag-svg'); if (!svg) return;
  const mode = currentMode();
  const tasks = (S.dash && S.dash.tasks) || [];
  const graph = model || buildDagModel(tasks, mode);
  syncDagChrome(graph, tasks, mode);
  if (!tasks.length) {
    svg.innerHTML = '';
    svg.appendChild(s('text', {
      x: '50%', y: '48%', 'text-anchor': 'middle', class: 'dag-empty-text', 'font-size': 12,
    }, '等待任务派发…'));
    return;
  }
  if (mode === 'task') renderTaskDAG(svg, tasks, graph);
  else renderAgentDAG(svg, tasks, graph);
}

function renderDAGFallback(model = null) {
  const host = $('#dag-list-fallback'); if (!host) return;
  // 该列表只在窄屏展示；桌面窗口下不要为不可见 DOM 做整图构建。
  if (!window.matchMedia || !window.matchMedia('(max-width: 768px)').matches) {
    host.innerHTML = '';
    return;
  }
  host.innerHTML = '';
  const tasks = (S.dash && S.dash.tasks) || [];
  if (currentMode() !== 'task') {
    const graph = model || buildAgentGraph(tasks);
    if (!graph.nodes.length) { host.appendChild(h('div', { class: 'ph-empty' }, '等待任务派发')); return; }
    for (const n of graph.nodes) {
      const rm = roleMeta(n.role);
      host.appendChild(h('div', { class: 'dag-group-title' },
        h('span', { class: 'dag-agent-dot', style: `background:${rm.color}` }),
        `${rm.full}（${n.done}/${n.total}）`));
      for (const t of n.tasks) {
        host.appendChild(h('div', { class: 'dag-task-row', onclick: () => openTaskDetail(t.id) },
          h('span', { class: `dot ${t.status === 'succeeded' ? 'done' : t.status === 'running' ? 'running' : t.status === 'failed' ? 'error' : 'idle'}` }),
          h('span', { class: 'mono', style: 'color:var(--text-ter)' }, t.id),
          h('span', {}, trunc(t.title, 28)),
          h('span', { style: 'margin-left:auto;font-size:11px;color:var(--text-ter)' }, (STATUS_META[t.status] || {}).label || t.status)));
      }
    }
    return;
  }

  const groups = {};
  for (const t of tasks) { (groups[t.status] = groups[t.status] || []).push(t); }
  const order = ['running', 'review', 'rework', 'ready', 'blocked', 'pending', 'succeeded', 'failed', 'skipped'];
  for (const st of order) {
    const items = groups[st]; if (!items || !items.length) continue;
    const meta = STATUS_META[st];
    host.appendChild(h('div', { class: 'dag-group-title' }, `${meta.label}（${items.length}）`));
    for (const t of items) {
      const rm = roleMeta(t.role);
      host.appendChild(h('div', { class: 'dag-task-row', onclick: () => openTaskDetail(t.id) },
        h('span', { class: 'dot', style: `background:${meta.color}` }),
        h('span', { class: 'mono', style: 'color:var(--text-ter)' }, t.id),
        h('span', {}, t.title),
        h('span', { style: `color:${rm.color};margin-left:auto;font-size:11px` }, rm.full)));
    }
  }
}

export {
  AGENT_GAP_X, AGENT_GAP_Y, AH, AW, COL_GAP, NH, NW, ROW_GAP,
  buildAgentGraph, buildDagModel, computeLayers, layoutDAG,
  renderDAG, renderDAGFallback, wireDAGControls,
};
