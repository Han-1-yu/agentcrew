'use strict';
// AgentCrew Studio — 渲染进程（原生 JS，无框架）。
// 全部数据经 preload 暴露的 window.studio IPC 获取；不在渲染进程接触 Node。

/* ---------- 常量 ---------- */

const STATUS_META = {
  pending:   { label: '待启动', color: 'var(--st-pending)' },
  ready:     { label: '就绪',   color: 'var(--st-ready)' },
  running:   { label: '运行中', color: 'var(--st-running)' },
  review:    { label: '评审中', color: 'var(--st-review)' },
  rework:    { label: '返工中', color: 'var(--st-rework)' },
  succeeded: { label: '成功',   color: 'var(--st-succeeded)' },
  failed:    { label: '失败',   color: 'var(--st-failed)' },
  blocked:   { label: '阻塞',   color: 'var(--st-blocked)' },
  cancelled: { label: '已取消', color: 'var(--st-cancelled)' },
  skipped:   { label: '已跳过', color: 'var(--st-skipped)' },
  unknown:   { label: '未知',   color: 'var(--st-pending)' },
};
const STATUS_ORDER = ['pending', 'ready', 'running', 'review', 'rework', 'succeeded', 'failed', 'blocked', 'cancelled', 'skipped'];

/* 运行级状态（run.json status）与任务级状态是两套枚举，配色分开 */
const RUN_META = {
  running:     { label: '运行中',   color: 'var(--st-running)' },
  completed:   { label: '成功完成', color: 'var(--st-succeeded)' },
  failed:      { label: '失败',     color: 'var(--st-failed)' },
  cancelled:   { label: '已取消',   color: 'var(--st-cancelled)' },
  interrupted: { label: '已中断',   color: 'var(--st-rework)' },
  unknown:     { label: '未知',     color: 'var(--st-pending)' },
};

const ROLE_META = {
  project_manager:  { label: '项目经理', color: '#e879f9' },
  product_manager:  { label: '产品经理', color: '#fb7185' },
  architect:        { label: '架构师',   color: '#38bdf8' },
  backend_dev:      { label: '后端开发', color: '#34d399' },
  frontend_dev:     { label: '前端开发', color: '#a3e635' },
  qa_engineer:      { label: '测试工程师', color: '#fbbf24' },
  devops_engineer:  { label: '运维工程师', color: '#22d3ee' },
  technical_writer: { label: '文档工程师', color: '#c4b5fd' },
};
const roleMeta = (id) => ROLE_META[id] || { label: id || '未知角色', color: '#9aa7bd' };

const EVENT_META = {
  run_started:      { label: 'run_started',      color: '#38bdf8' },
  plan_created:     { label: 'plan_created',     color: '#34d399' },
  plan_rejected:    { label: 'plan_rejected',    color: '#f97316' },
  task_started:     { label: 'task_started',     color: '#f59e0b' },
  task_finished:    { label: 'task_finished',    color: '#22c55e' },
  task_transition:  { label: 'task_transition',  color: '#94a3b8' },
  task_notes:       { label: 'task_notes',       color: '#94a3b8' },
  gate_result:      { label: 'gate_result',      color: '#8b5cf6' },
  artifact_written: { label: 'artifact_written', color: '#2dd4bf' },
  llm_call:         { label: 'llm_call',         color: '#67e8f9' },
  llm_degraded:     { label: 'llm_degraded',     color: '#fb923c' },
  llm_failover:     { label: 'llm_failover',     color: '#fbbf24' },
  budget_exhausted: { label: 'budget_exhausted', color: '#f43f5e' },
  tool_loop_exhausted: { label: 'tool_loop_exhausted', color: '#f472b6' },
  error:            { label: 'error',            color: '#ef4444' },
  run_finished:     { label: 'run_finished',     color: '#a78bfa' },
};
const EVENT_TYPES = Object.keys(EVENT_META);

const FINISHED_STATUSES = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
const STALE_MS = 90 * 1000;

/* ---------- 状态 ---------- */

const S = {
  settings: null,
  env: null,
  pythonInfo: null,
  runs: [],
  selectedDir: null,
  run: null,
  tasks: [],
  loadErrors: null,
  events: [],
  eventsSkipped: 0,
  eventFilter: new Set(EVENT_TYPES),
  eventSearch: '',
  autoScroll: true,
  tab: 'board',
  artifacts: null,        // { entries, manifestError }
  artifactSel: null,      // 当前预览的相对路径
  procsByRunDir: new Map(),
  activeProcKey: null,    // 最近一次由 UI 启动的进程
  search: '',
  elapsedTimer: null,
};

// 自测遥测：主进程 selftest 通过 executeJavaScript 读取
window.__st = {
  snapshots: 0,
  ledgerBatches: 0,
  state: () => ({
    dir: S.selectedDir,
    status: S.run && S.run.status,
    stats: S.run && S.run.stats ? S.run.stats : null,
    tasks: (S.tasks || []).length,
    events: S.events.length,
  }),
};

/* ---------- 工具 ---------- */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => [...(root || document).querySelectorAll(sel)];

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style') el.setAttribute('style', v);
      else if (k.startsWith('data-')) el.setAttribute(k, v);
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el[k] = v;
    }
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
  }
  return el;
}

function fmtTokens(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString('en-US');
}
function fmtCost(n) {
  if (n == null) return '—';
  const s = Number(n).toFixed(10).replace(/0+$/, '').replace(/\.$/, '');
  return `$${s}`;
}
function fmtDur(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${Math.round(s % 60)}s`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}
function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}
function parseTs(ts) {
  if (!ts) return null;
  const d = new Date(String(ts).replace(' ', 'T'));
  return isNaN(d) ? null : d;
}
function fmtClock(ts) {
  const d = parseTs(ts);
  return d ? d.toTimeString().slice(0, 8) : String(ts || '');
}
function fmtDateTime(ts) {
  const d = parseTs(ts);
  if (!d) return String(ts || '—');
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function trunc(s, n) {
  s = String(s == null ? '' : s);
  return s.length > n ? `${s.slice(0, n)}…` : s;
}
function runDurationMs(run, tasks) {
  if (run && run.stats && run.stats.started_at) {
    const a = parseTs(run.stats.started_at);
    const b = parseTs(run.stats.finished_at || run.updated_at) || new Date();
    if (a) return b - a;
  }
  return null;
}
function statusCounts(tasks) {
  const c = {};
  for (const st of STATUS_ORDER) c[st] = 0;
  for (const t of tasks || []) c[t.status] = (c[t.status] || 0) + 1;
  return c;
}

async function callIpc(fn, ...args) {
  try { return await fn(...args); }
  catch (e) {
    toast('error', '后端调用失败', String(e && e.message || e));
    throw e;
  }
}

/* ---------- Toast ---------- */

function toast(type, title, msg, ms) {
  const box = $('#toasts');
  const t = h('div', { class: `toast ${type || ''}` },
    h('div', { class: 'toast-title' }, title),
    msg ? h('div', { class: 'toast-msg' }, msg) : null);
  box.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .25s'; setTimeout(() => t.remove(), 300); }, ms || (type === 'error' ? 8000 : 4000));
}

/* ---------- 启动 ---------- */

async function boot() {
  wireTopbar();
  wireProcPush();
  wireWatchPush();
  window.addEventListener('error', (e) => toast('error', '界面错误', e.message));
  window.addEventListener('unhandledrejection', (e) => toast('error', '未处理的异常', String(e.reason && e.reason.message || e.reason)));

  try {
    const r = await window.studio.getSettings();
    S.settings = r.settings;
    S.env = r.env;
  } catch (e) {
    S.settings = { theme: 'dark' };
    toast('error', '读取设置失败', String(e));
  }
  applyTheme(S.settings.theme || 'dark');
  refreshPythonFoot();
  await refreshRuns(false);
  // 启动时显示运行列表视图
  $('#runs-view').classList.remove('hidden');
  $('#run-view').classList.add('hidden');
  $('#empty-view').classList.add('hidden');
  setInterval(() => { refreshRuns(); refreshProcMap(); }, 2000);
  setInterval(tickElapsed, 1000);
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
}

function wireTopbar() {
  $('#btn-new-run').addEventListener('click', openNewRunModal);
  $('#btn-empty-new').addEventListener('click', openNewRunModal);
  $('#btn-refresh-runs').addEventListener('click', () => refreshRuns());
  $('#run-search').addEventListener('input', (e) => { S.search = e.target.value.trim().toLowerCase(); renderRunsView(); });
  // 图标栏导航
  document.querySelectorAll('.rail-item[data-view]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const view = btn.dataset.view;
      document.querySelectorAll('.rail-item[data-view]').forEach((b) => b.classList.toggle('active', b === btn));
      if (view === 'runs') goBackToList();
      else if (view === 'settings') openSettingsModal();
    });
  });
  $('#btn-theme').addEventListener('click', async () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    S.settings.theme = next;
    try { await window.studio.setSettings({ theme: next }); } catch { /* 保存失败不阻塞切主题 */ }
  });
}

async function refreshPythonFoot() {
  // Python 探测结果存入状态，供设置模态和运行列表底部显示
  try {
    const r = await window.studio.detectPython();
    S.pythonInfo = r || null;
    if (r && !S.settings.pythonCmd) {
      S.settings.pythonCmd = r.cmd;
      await window.studio.setSettings({ pythonCmd: r.cmd });
    }
  } catch { S.pythonInfo = null; }
}

/* ---------- 运行列表 ---------- */

async function refreshRuns(autoSelect) {
  let runs;
  try { runs = await callIpc(window.studio.listRuns); }
  catch { return; }
  S.runs = runs;
  $('#runs-count').textContent = runs.length ? `${runs.length}` : '';
  renderRunsView();
  if (autoSelect && !S.selectedDir && runs.length) selectRun(runs[0].dir);
}

function renderRunsView() {
  const list = $('#run-cards');
  list.innerHTML = '';
  const q = S.search;
  const filtered = S.runs.filter((r) => !q
    || (r.goal || '').toLowerCase().includes(q)
    || r.runId.toLowerCase().includes(q)
    || (r.group || '').toLowerCase().includes(q));
  if (!filtered.length) {
    list.appendChild(h('div', { class: 'runs-empty' },
      S.runs.length ? '没有匹配的运行' : '输出目录下暂无运行，点击右上角「新运行」开始'));
    return;
  }
  for (const r of filtered) {
    const live = r.status === 'running';
    const st = RUN_META[r.status] || RUN_META.unknown;
    const s = r.stats || {};
    const total = s.total_tasks || 0;
    const ok = s.succeeded || 0;
    const fail = s.failed || 0;
    const blk = s.blocked || 0;
    list.appendChild(h('div', {
      class: `run-card${r.dir === S.selectedDir ? ' active' : ''}${live ? ' is-live' : ''}`,
      'data-dir': r.dir, role: 'option',
      onclick: () => selectRun(r.dir),
    },
      // 左侧状态图标
      h('div', { class: 'run-card-icon', style: `--ic:${st.color}` },
        h('span', { class: 'run-card-dot' })),
      // 中间内容
      h('div', { class: 'run-card-body' },
        h('div', { class: 'run-card-head' },
          h('span', { class: 'run-card-name', title: r.goal }, r.goal || (r.error ? '（run.json 不可读）' : '（无目标）')),
          h('span', { class: 'flex-spacer' }),
          live ? h('span', { class: 'run-card-live' }, '运行中') : h('span', { class: 'run-card-status', style: `color:${st.color}` }, st.label)),
        h('div', { class: 'run-card-sub' },
          h('span', { class: 'run-card-id' }, r.runId),
          h('span', { class: 'run-card-sep' }, '·'),
          h('span', {}, fmtDateTime(r.createdAt || '')),
          r.group ? h('span', { class: 'run-card-sep' }, '·') : null,
          r.group ? h('span', { class: 'run-card-group', title: r.group }, r.group) : null),
        total ? h('div', { class: 'run-card-progress' },
          h('div', { class: 'run-card-bar' },
            h('i', { style: `width:${total ? (ok / total * 100) : 0}%;background:var(--st-succeeded)` }),
            fail ? h('i', { style: `width:${total ? (fail / total * 100) : 0}%;background:var(--st-failed)` }) : null),
          h('span', { class: 'run-card-stats' },
            h('span', { class: 'rc-ok' }, `${ok}✓`),
            fail ? h('span', { class: 'rc-fail' }, `${fail}✗`) : null,
            blk ? h('span', { class: 'rc-block' }, `${blk}⛔`) : null,
            h('span', { class: 'rc-total' }, `/${total}`)),
        ) : null),
      // 右侧操作
      h('div', { class: 'run-card-actions' },
        h('button', {
          class: 'btn btn-icon btn-sm run-card-del',
          title: live ? '运行中无法删除，请先取消' : '删除此运行（不可恢复）',
          disabled: live,
          onclick: (e) => { e.stopPropagation(); confirmDeleteRun(r); },
        }, '✕')),
    ));
  }
}

/* ---------- 选中运行 & 监视 ---------- */

async function selectRun(dir) {
  if (!dir) return;
  if (S.selectedDir && S.selectedDir !== dir) {
    try { await window.studio.unwatchRun(S.selectedDir); } catch { /* 忽略 */ }
  }
  S.selectedDir = dir;
  S.run = null; S.tasks = []; S.loadErrors = null;
  S.events = []; S.eventsSkipped = 0; S.eventFilter = new Set(EVENT_TYPES); S.eventSearch = ''; S.autoScroll = true;
  S.artifacts = null; S.artifactSel = null;
  S.tab = 'board';
  renderRunsView();

  // 先同步读一次快照，立刻有内容；再启动轮询（tail 模式带出历史事件）
  try {
    const snap = await callIpc(window.studio.loadRun, dir);
    S.run = snap.run; S.tasks = snap.tasks || []; S.loadErrors = snap.errors;
  } catch { S.loadErrors = { run: '读取运行数据失败' }; }
  $('#runs-view').classList.add('hidden');
  $('#empty-view').classList.add('hidden');
  $('#run-view').classList.remove('hidden');
  renderRunView();
  try { await window.studio.watchRun(dir, true); } catch (e) { toast('error', '启动实时监视失败', String(e && e.message || e)); }
}

function goBackToList() {
  if (S.selectedDir) {
    try { window.studio.unwatchRun(S.selectedDir); } catch { /* 忽略 */ }
  }
  S.selectedDir = null;
  S.run = null; S.tasks = []; S.events = [];
  $('#run-view').classList.add('hidden');
  $('#empty-view').classList.add('hidden');
  $('#runs-view').classList.remove('hidden');
  renderRunsView();
  // 重置图标栏选中态
  document.querySelectorAll('.rail-item[data-view]').forEach((b) => b.classList.toggle('active', b.dataset.view === 'runs'));
}

function wireWatchPush() {
  window.studio.on('watch:ledger', (p) => {
    if (p.dir !== S.selectedDir) return;
    window.__st.ledgerBatches += 1;
    if (p.initial && p.skipped) S.eventsSkipped = p.skipped;
    S.events.push(...p.events);
    if (S.events.length > 3000) S.events = S.events.slice(S.events.length - 2600);
    if (S.tab === 'events') renderEventList();
    if (S.tab === 'charts') renderCharts();
  });
  window.studio.on('watch:snapshot', (p) => {
    if (p.dir !== S.selectedDir) return;
    window.__st.snapshots += 1;
    const prevStatus = S.run && S.run.status;
    if (p.run) S.run = p.run;
    if (p.tasks) S.tasks = p.tasks;
    if (p.errors) S.loadErrors = p.errors;
    renderRunView();
    const now = p.run && p.run.status;
    if (prevStatus && prevStatus !== now && FINISHED_STATUSES.has(now) && !FINISHED_STATUSES.has(prevStatus)) {
      refreshRuns();
      openReportModal();
      toast(now === 'completed' ? 'success' : 'error', `运行结束：${RUN_META[now] ? RUN_META[now].label : now}`, (S.run && S.run.project_goal ? trunc(S.run.project_goal, 60) : ''));
    }
  });
}

/* ---------- 运行主视图 ---------- */

function renderRunView() {
  const root = $('#run-view');
  const scroll = root.scrollTop;
  root.innerHTML = '';
  if (S.loadErrors && !S.run) {
    root.appendChild(h('div', { class: 'error-banner' },
      h('h4', {}, '无法读取这次运行'),
      h('p', {}, S.loadErrors.run || S.loadErrors.tasks || '未知错误'),
      h('button', { class: 'btn', onclick: () => selectRun(S.selectedDir) }, '重试'),
      h('button', { class: 'btn', onclick: () => window.studio.openPath(S.selectedDir) }, '打开目录'),
    ));
    return;
  }
  const run = S.run || {};
  const tasks = S.tasks || [];
  const st = RUN_META[run.status] || RUN_META.unknown;
  const stats = run.stats || {};
  const derived = tasks.length ? {
    total: tasks.length,
    succeeded: tasks.filter((t) => t.status === 'succeeded').length,
    failed: tasks.filter((t) => t.status === 'failed').length,
    blocked: tasks.filter((t) => t.status === 'blocked').length,
    skipped: tasks.filter((t) => t.status === 'skipped').length,
  } : null;
  const live = run.status === 'running';
  const stale = live && isStale(run) && !S.procsByRunDir.has(S.selectedDir);
  const counts = statusCounts(tasks);

  // P0-3：预算状态（从 config_snapshot 读取上限，从 stats/事件读取当前用量）
  const cfg = run.config_snapshot || {};
  const maxCost = cfg.max_cost_usd;
  const maxLlm = cfg.max_llm_calls;
  const curCost = (stats && stats.total_cost_usd != null) ? stats.total_cost_usd
    : (S.events || []).reduce((a, e) => a + ((e.event === 'llm_call' && e.data && e.data.estimated_cost_usd) || 0), 0);
  const curLlm = (stats && stats.llm_calls != null) ? stats.llm_calls
    : (S.events || []).filter((e) => e.event === 'llm_call').length;
  const budgetExhausted = (S.events || []).some((e) => e.event === 'budget_exhausted');
  const budgetBits = [];
  if (maxCost != null) {
    const pct = Math.min(100, (curCost / maxCost) * 100);
    budgetBits.push(h('span', { class: `budget-chip${pct >= 90 ? ' warn' : ''}`, title: `成本上限 $${maxCost}` }, `$${curCost.toFixed(4)}/$${maxCost}`));
  }
  if (maxLlm != null) {
    const pct = Math.min(100, (curLlm / maxLlm) * 100);
    budgetBits.push(h('span', { class: `budget-chip${pct >= 90 ? ' warn' : ''}`, title: `LLM 调用上限 ${maxLlm}` }, `${curLlm}/${maxLlm} 次`));
  }
  if (budgetExhausted) budgetBits.push(h('span', { class: 'budget-chip err', title: '预算已耗尽，run 优雅收尾' }, '预算耗尽'));

  // 顶部状态比例缝
  const seam = h('div', { id: 'live-seam' });
  for (const k of STATUS_ORDER) {
    if (counts[k]) seam.appendChild(h('i', { style: `width:${(counts[k] / Math.max(1, tasks.length)) * 100}%;background:${STATUS_META[k].color}` }));
  }
  root.appendChild(seam);

  // 头部
  const actions = [];
  if (live && S.procsByRunDir.has(S.selectedDir)) {
    actions.push(h('button', { class: 'btn btn-danger', id: 'btn-cancel-run', onclick: cancelSelected }, '取消运行'));
  } else if (live && stale) {
    actions.push(h('button', { class: 'btn', onclick: resumeSelected }, '恢复运行'));
    actions.push(h('button', { class: 'btn', id: 'btn-cancel-run', onclick: cancelSelected }, '标记取消'));
  } else if (['interrupted', 'failed', 'cancelled'].includes(run.status)) {
    actions.push(h('button', { class: 'btn', onclick: resumeSelected }, '恢复运行'));
  }
  if (FINISHED_STATUSES.has(run.status)) actions.push(h('button', { class: 'btn', onclick: () => openReportModal() }, '结束报告'));
  actions.push(h('button', { class: 'btn', onclick: () => window.studio.openPath(S.selectedDir) }, '打开目录'));
  actions.push(h('button', { class: 'btn', onclick: exportArtifactsNow }, '导出产物'));

  root.appendChild(h('div', { class: 'run-header' },
    h('div', { class: 'run-header-main' },
      h('div', { class: 'run-header-top' },
        h('button', { class: 'btn btn-icon btn-back', onclick: goBackToList, title: '返回运行列表', 'aria-label': '返回' },
          h('svg', { width: '16', height: '16', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
            h('path', { d: 'M19 12H5' }),
            h('path', { d: 'M12 19l-7-7 7-7' }))),
        h('span', { class: `status-chip big${live ? ' live' : ''}`, style: `--chip-color:${st.color}` }, st.label),
        h('span', { class: 'run-sub', style: 'display:inline-flex;margin-left:10px;vertical-align:middle' },
          h('span', { class: 'run-id' }, run.run_id || '')),
        stale ? h('span', { class: 'warn-banner', style: 'display:inline-block;margin-left:10px;padding:2px 10px' }, '长时间无更新，疑似已中断') : null,
      ),
      h('div', { class: 'run-goal-full' }, run.project_goal || '（无目标）'),
      h('div', { class: 'run-sub' },
        run.group ? h('span', {}, `${run.group}`) : null,
        h('span', {}, `创建 ${fmtDateTime(run.created_at)}`),
        h('span', { class: 'sep' }, '·'),
        h('span', { id: 'elapsed-cell' }, live ? '计时中…' : `更新 ${fmtDateTime(run.updated_at)}`),
        budgetBits.length ? h('span', { class: 'sep' }, '·') : null,
        ...budgetBits)),
    h('div', { class: 'run-actions' }, actions),
  ));

  // 统计行（运行结束后与 run.json stats 逐项一致；早期无 stats 时用任务派生值）
  const total = stats.total_tasks != null ? stats.total_tasks : (derived ? derived.total : 0);
  const ok = stats.succeeded != null ? stats.succeeded : (derived ? derived.succeeded : 0);
  const bad = stats.failed != null ? stats.failed : (derived ? derived.failed : 0);
  const blk = stats.blocked != null ? stats.blocked : (derived ? derived.blocked : 0);
  const skip = stats.skipped != null ? stats.skipped : (derived ? derived.skipped : 0);
  const durMs = live ? null : runDurationMs(run, tasks);
  const cards = [
    statCard('任务', `${ok}/${total}`, `${skip} 跳过 · ${blk} 阻塞`, { pct: total ? ok / total : 0, color: STATUS_META.succeeded.color }),
    statCard('失败', String(bad), bad ? '需要关注' : '', { pct: total ? bad / total : 0, color: STATUS_META.failed.color, valueColor: bad ? STATUS_META.failed.color : null }),
    statCard('LLM 调用', fmtTokens(stats.llm_calls), stats.estimated_cost_usd != null ? `成本 ${fmtCost(stats.estimated_cost_usd)}` : '', null),
    statCard('Tokens', fmtTokens((stats.prompt_tokens || 0) + (stats.completion_tokens || 0)), `提示 ${fmtTokens(stats.prompt_tokens || 0)} · 补全 ${fmtTokens(stats.completion_tokens || 0)}`, null),
    statCard('成本（估算）', fmtCost(stats.estimated_cost_usd != null ? stats.estimated_cost_usd : null), '', null),
    statCard('耗时', live ? '计时中…' : fmtDur(durMs), live ? '' : (stats.finished_at ? `结束 ${fmtDateTime(stats.finished_at)}` : ''), null),
  ];
  const statsRow = h('div', { class: 'stats-row' }, cards);
  root.appendChild(statsRow);

  // 页签
  const tabs = h('div', { class: 'tabs' },
    tabBtn('board', '看板', tasks.length ? String(tasks.length) : ''),
    tabBtn('events', '事件流', S.events.length ? String(S.events.length) : ''),
    tabBtn('artifacts', '产物'),
    tabBtn('charts', '图表'),
  );
  root.appendChild(tabs);

  const pane = h('div', { id: 'tab-pane' });
  root.appendChild(pane);
  renderTabPane();
  root.scrollTop = scroll;
}

function statCard(label, value, sub, bar) {
  return h('div', { class: 'stat-card' },
    h('div', { class: 'stat-label' }, label),
    h('div', { class: 'stat-value', style: bar && bar.valueColor ? `color:${bar.valueColor}` : '' }, value),
    sub ? h('div', { class: 'stat-sub' }, sub) : null,
    bar && bar.pct ? h('div', { class: 'stat-bar' }, h('i', { style: `width:${Math.min(100, bar.pct * 100)}%;background:${bar.color}` })) : null,
  );
}

function tabBtn(key, label, badge) {
  return h('button', {
    class: `tab${S.tab === key ? ' active' : ''}`, 'data-tab': key,
    onclick: () => { S.tab = key; $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === key)); renderTabPane(); },
  }, label, badge ? h('span', { class: 'tab-badge' }, badge) : null);
}

function renderTabPane() {
  const pane = $('#tab-pane');
  if (!pane) return;
  pane.innerHTML = '';
  if (S.tab === 'board') renderBoard(pane);
  else if (S.tab === 'events') { renderEventsToolbar(pane); renderEventList(pane); }
  else if (S.tab === 'artifacts') { renderArtifacts(pane); }
  else if (S.tab === 'charts') renderCharts(pane);
}

function isStale(run) {
  const u = parseTs(run.updated_at);
  return !u || (Date.now() - u.getTime() > STALE_MS);
}

function tickElapsed() {
  const cell = $('#elapsed-cell');
  if (!cell || !S.run || S.run.status !== 'running') return;
  const a = parseTs(S.run.stats && S.run.stats.started_at || S.run.created_at);
  if (a) cell.textContent = `已运行 ${fmtDur(Date.now() - a.getTime())}`;
}

/* ---------- 看板 ---------- */

function renderBoard(pane) {
  const wrap = h('div', { class: 'board-wrap' });
  const prevLeft = wrap.scrollLeft;
  const board = h('div', { class: 'board' });
  const tasks = S.tasks || [];
  for (const k of STATUS_ORDER) {
    const meta = STATUS_META[k];
    const items = tasks.filter((t) => (t.status || 'pending') === k);
    const col = h('div', { class: 'board-col', 'data-status': k },
      h('div', { class: 'board-col-head', style: `border-bottom-color:${meta.color}` },
        h('span', { class: 'board-col-title', style: `color:${meta.color}` }, meta.label),
        h('span', { class: 'board-col-count' }, items.length || '')),
      h('div', { class: 'board-col-body' },
        items.length ? items.map((t) => taskCard(t)) : h('div', { class: 'board-col-empty' }, '—')));
    board.appendChild(col);
  }
  wrap.appendChild(board);
  pane.appendChild(wrap);
  wrap.scrollLeft = prevLeft;
}

function taskCard(t) {
  const meta = STATUS_META[t.status] || STATUS_META.unknown;
  const rm = roleMeta(t.role);
  const isLive = ['running', 'review', 'rework'].includes(t.status);
  const lastReviews = (t.reviews || []).slice(-3);
  const okN = lastReviews.filter((r) => r.verdict === 'approve').length;
  const badN = lastReviews.filter((r) => r.verdict === 'reject').length;
  const dur = t.result && t.result.duration_ms;
  const pri = t.priority || 5;
  const priClass = pri <= 3 ? 'pri-high' : pri >= 7 ? 'pri-low' : 'pri-normal';
  return h('div', {
    class: `task-card${isLive ? ' is-live' : ''}`,
    'data-task': t.id, style: `--card-color:${meta.color};border-left-color:${meta.color}`,
    onclick: () => openTaskDrawer(t.id),
  },
    h('div', { class: 'task-id-row' },
      h('span', { class: 'task-id', style: `color:${meta.color}` }, t.id),
      h('span', { class: `pri-badge ${priClass}`, title: `优先级 ${pri}/9（数字小者优先）` }, `P${pri}`),
      h('span', { class: 'role-chip', style: `--chip-color:${rm.color}` }, rm.label)),
    h('div', { class: 'task-title' }, t.title || '（无标题）'),
    h('div', { class: 'task-meta' },
      h('span', { title: '尝试次数/上限' }, `⭯${t.attempts || 0}/${t.max_attempts || '—'}`),
      (t.rework_count || 0) ? h('span', { title: '返工次数' }, `↺${t.rework_count}`) : null,
      dur != null ? h('span', {}, fmtDur(dur)) : null,
      lastReviews.length ? h('span', { title: '最近评审' }, `${okN ? `✓${okN}` : ''}${okN && badN ? ' ' : ''}${badN ? `✗${badN}` : ''}`) : null,
    ));
}

/* ---------- 任务详情抽屉 ---------- */

function closeDrawer() {
  $('#drawer').classList.add('hidden');
  $('#drawer-mask').classList.add('hidden');
}

function openTaskDrawer(taskId) {
  const t = (S.tasks || []).find((x) => x.id === taskId);
  if (!t) return;
  const meta = STATUS_META[t.status] || STATUS_META.unknown;
  const rm = roleMeta(t.role);
  const d = $('#drawer');
  d.innerHTML = '';
  d.appendChild(h('div', { class: 'drawer-head' },
    h('div', { class: 'drawer-title' },
      h('span', { class: 'task-id', style: `color:${meta.color};font-size:13px` }, t.id + ' '),
      h('span', {}, t.title || ''),
      h('div', { class: 'drawer-sub' },
        h('span', { class: 'status-chip', style: `--chip-color:${meta.color}` }, meta.label),
        h('span', { class: 'role-chip', style: `--chip-color:${rm.color}` }, rm.label),
        h('span', { class: 'run-id' }, `尝试 ${t.attempts || 0}/${t.max_attempts || '—'} · 返工 ${t.rework_count || 0}/${t.max_reworks || '—'}`))),
    h('button', { class: 'btn btn-icon drawer-close', id: 'drawer-close', onclick: closeDrawer, title: '关闭' }, '✕')));

  const body = h('div', { class: 'drawer-body' });
  const sec = (title, ...kids) => body.appendChild(h('div', { class: 'dsection' }, h('h4', {}, title), ...kids));
  const none = () => h('div', { class: 'dtext dim' }, '（空）');

  if (t.brief) sec('一句话摘要', h('div', { class: 'dtext dim' }, t.brief));
  sec('描述', t.description ? h('div', { class: 'dtext' }, t.description) : none());
  sec('验收标准', (t.acceptance_criteria || []).length
    ? h('ul', { class: 'crit-list' }, t.acceptance_criteria.map((c) => h('li', {}, c))) : none());
  if ((t.dependencies || []).length) sec('依赖', h('div', { class: 'dtext dim' }, t.dependencies.join('、')));

  sec('执行配置',
    h('div', { class: 'cfg-grid' },
      h('div', { class: 'cfg-item' }, h('span', { class: 'cfg-label' }, '优先级'), h('span', {}, `P${t.priority || 5}/9`)),
      h('div', { class: 'cfg-item' }, h('span', { class: 'cfg-label' }, '单次超时'), h('span', {}, `${t.timeout_seconds || 300}s`)),
      h('div', { class: 'cfg-item' }, h('span', { class: 'cfg-label' }, '执行重试'), h('span', {}, `${t.max_attempts || 3} 次`)),
      h('div', { class: 'cfg-item' }, h('span', { class: 'cfg-label' }, '返工上限'), h('span', {}, `${t.max_reworks || 2} 轮`))));

  if ((t.deliverables || []).length) {
    sec('交付物',
      h('table', { class: 'dtable' },
        h('tr', {}, h('th', {}, '路径'), h('th', {}, '类型'), h('th', {}, '必选')),
        t.deliverables.map((dv) => h('tr', {},
          h('td', { class: 'mono' }, dv.path), h('td', {}, dv.kind || '—'),
          h('td', {}, dv.required ? '是' : '否')))));
  }

  if ((t.reviews || []).length) {
    sec('评审记录', t.reviews.map((r) => {
      const vCls = r.verdict === 'approve' ? 'verdict-approve' : (r.verdict === 'reject' ? 'verdict-reject' : 'verdict-warn');
      return h('div', { class: 'gate-item' },
        h('div', { class: 'gate-item-head' },
          h('span', { class: 'verdict-chip ' + vCls }, r.verdict || '—'),
          h('span', { class: 'gate-layer' }, `${r.layer || ''} · 第 ${r.round || '?'} 轮`),
          r.score != null ? h('span', { class: 'gate-layer' }, `分数 ${r.score}`) : null,
          h('span', { class: 'gate-layer', style: 'margin-left:auto' }, r.reviewer || '')),
        (r.issues || []).length ? h('ul', { class: 'gate-issues' }, r.issues.map((i) => h('li', {}, typeof i === 'string' ? i : JSON.stringify(i)))) : null,
        (r.fix_hints || []).length ? h('ul', { class: 'gate-issues' }, r.fix_hints.map((i) => h('li', {}, `建议：${typeof i === 'string' ? i : JSON.stringify(i)}`))) : null);
    }));
  }

  if (t.result && (t.result.summary || (t.result.artifacts || []).length)) {
    sec('执行结果',
      t.result.summary ? h('div', { class: 'dtext' }, t.result.summary) : null,
      t.result.duration_ms != null ? h('div', { class: 'dtext dim', style: 'margin-top:4px' }, `耗时 ${fmtDur(t.result.duration_ms)}`) : null,
      (t.result.artifacts || []).length ? h('table', { class: 'dtable', style: 'margin-top:8px' },
        h('tr', {}, h('th', {}, '产物'), h('th', {}, '大小')),
        t.result.artifacts.map((a) => h('tr', {},
          h('td', { class: 'mono' }, h('a', {
            href: '#', style: 'color:var(--accent);text-decoration:none',
            onclick: (e) => { e.preventDefault(); previewArtifactByPath(a.path); },
          }, a.path)),
          h('td', { class: 'mono' }, fmtBytes(a.size_bytes))))) : null);
  }

  if ((t.status_history || []).length) {
    sec('状态历史', t.status_history.map((s) => h('div', { class: 'hist-item' },
      h('span', { class: 'hist-time' }, fmtDateTime(s.at)),
      h('span', { class: 'hist-flow' },
        h('span', {}, STATUS_META[s.from_status] ? STATUS_META[s.from_status].label : s.from_status),
        h('span', { class: 'arrow' }, ' → '),
        h('span', {}, STATUS_META[s.to_status] ? STATUS_META[s.to_status].label : s.to_status)),
      s.reason ? h('span', { class: 'hist-reason' }, s.reason) : null)));
  }

  d.appendChild(body);
  $('#drawer-mask').classList.remove('hidden');
  d.classList.remove('hidden');
  $('#drawer-close').focus();
}

$('#drawer-mask').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeDrawer(); closeModal(); }
});

/* ---------- 事件流 ---------- */

function renderEventsToolbar(pane) {
  const chips = EVENT_TYPES.map((t) => {
    const m = EVENT_META[t];
    return h('button', {
      class: `fchip${S.eventFilter.has(t) ? ' on' : ''}`, 'data-ev': t,
      style: `--ev-color:${m.color}`,
      onclick: (e) => {
        if (S.eventFilter.has(t)) S.eventFilter.delete(t); else S.eventFilter.add(t);
        e.currentTarget.classList.toggle('on');
        renderEventList();
      },
    }, h('span', { class: 'fdot' }), m.label);
  });
  const bar = h('div', { class: 'events-toolbar' },
    h('div', { class: 'filter-chips' }, chips),
    h('div', { class: 'events-search' },
      h('input', { type: 'search', placeholder: '搜索事件内容…', value: S.eventSearch,
        oninput: (e) => { S.eventSearch = e.target.value.trim().toLowerCase(); renderEventList(); } })),
    h('label', { class: 'autoscroll-toggle' },
      h('input', { type: 'checkbox', checked: S.autoScroll, onchange: (e) => { S.autoScroll = e.target.checked; } }), '自动滚动'),
  );
  (pane || $('#tab-pane')).appendChild(bar);
}

function eventSummary(ev) {
  const d = ev.data || {};
  switch (ev.event) {
    case 'run_started': return `目标：${trunc(d.goal || '', 90)}`;
    case 'plan_created': return `计划生成：${d.task_count ?? '?'} 个任务（${(d.roles || []).join('、') || '?'}）`;
    case 'plan_rejected': return `第 ${d.attempt ?? '?'} 次计划被驳回：${trunc((d.errors || [])[0] || '', 90)}`;
    case 'task_started': return `${d.task_id} 开始执行（${roleMeta(d.role).label}，第 ${d.attempt ?? 1} 次尝试）`;
    case 'task_finished': return `${d.task_id} 执行完成，耗时 ${fmtDur(d.duration_ms)}`;
    case 'task_transition': return `${d.task_id} ${STATUS_META[d.from] ? STATUS_META[d.from].label : d.from} → ${STATUS_META[d.to] ? STATUS_META[d.to].label : d.to}（${d.reason || ''}）`;
    case 'task_notes': return `${d.task_id} 备注：${trunc(d.notes || '', 90)}`;
    case 'gate_result': return `${d.task_id} 门禁 ${d.layer} 第 ${d.round} 轮：${d.verdict}${d.score != null ? `（分数 ${d.score}）` : ''}${(d.issues || []).length ? `，${d.issues.length} 个问题` : ''}`;
    case 'artifact_written': return `写入产物 ${d.path}（${fmtBytes(d.size_bytes)}）`;
    case 'llm_call': return `${roleMeta(d.role).label} 调用 ${d.model}：${fmtTokens(d.prompt_tokens)}+${fmtTokens(d.completion_tokens)} tokens，${fmtCost(d.estimated_cost_usd)}（${d.latency_ms}ms，${d.purpose || ''}）`;
    case 'llm_degraded': return `${d.provider} 级别 ${d.from_level}→${d.to_level}：${trunc(d.reason || '', 80)}`;
    case 'error': return `${d.where || '?'}：${d.exc_type || ''} ${trunc(d.message || '', 100)}`;
    case 'run_finished': return `运行结束：${RUN_META[d.status] ? RUN_META[d.status].label : d.status}（退出码 ${d.exit_code ?? '?'}，耗时 ${fmtDur(d.duration_ms)}）`;
    default: return JSON.stringify(d).slice(0, 120);
  }
}

function renderEventList(pane) {
  const host = pane || $('#tab-pane');
  if (!host) return;
  let list = $('.event-list', host);
  if (!list) {
    list = h('div', { class: 'event-list', id: 'event-list' });
    host.appendChild(list);
  }
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  list.innerHTML = '';
  if (S.eventsSkipped) list.appendChild(h('div', { class: 'event-note' }, `已省略更早的 ${S.eventsSkipped} 条事件（仅保留最近记录）`));
  if (!S.events.length) {
    list.appendChild(h('div', { class: 'event-note' }, S.run && S.run.status === 'running' ? '等待第一批事件…' : '该运行没有 ledger 事件。'));
    return;
  }
  const q = S.eventSearch;
  const shown = [];
  for (let i = S.events.length - 1; i >= 0 && shown.length < 500; i--) {
    const ev = S.events[i];
    if (!S.eventFilter.has(ev.event)) continue;
    if (q) {
      const hay = `${ev.event} ${eventSummary(ev)}`.toLowerCase();
      if (!hay.includes(q)) continue;
    }
    shown.push(ev);
  }
  if (shown.length >= 500) list.appendChild(h('div', { class: 'event-note' }, '最多显示最近 500 条，可按类型过滤'));
  for (let i = shown.length - 1; i >= 0; i--) {
    const ev = shown[i];
    const m = EVENT_META[ev.event] || { color: '#9aa7bd', label: ev.event };
    list.appendChild(h('div', { class: `event-row${ev.event === 'error' ? ' ev-error' : ''}` },
      h('span', { class: 'event-ts' }, fmtClock(ev.ts)),
      h('span', { class: 'event-type', style: `--ev-color:${m.color};color:${m.color}` }, m.label),
      h('span', { class: 'event-text' }, eventSummary(ev))));
  }
  const badge = $$('.tab').find((t) => t.dataset.tab === 'events');
  if (badge) badge.innerHTML = '事件流 ' + (S.events.length ? `<span class="tab-badge">${S.events.length}</span>` : '');
  if (S.autoScroll || nearBottom) list.scrollTop = list.scrollHeight;
}

/* ---------- 产物 ---------- */

async function renderArtifacts(pane) {
  if (!S.artifacts) {
    try { S.artifacts = await callIpc(window.studio.listArtifacts, S.selectedDir); }
    catch { S.artifacts = { entries: [], manifestError: '读取产物列表失败' }; }
  }
  const { entries, manifestError } = S.artifacts;
  const layout = h('div', { class: 'art-layout' });

  const list = h('div', { class: 'art-list' });
  if (manifestError) list.appendChild(h('div', { class: 'event-note', style: 'color:var(--st-running)' }, `⚠ ${manifestError}，已回退为目录扫描`));
  if (!entries.length) list.appendChild(h('div', { class: 'art-placeholder' }, '没有产物。运行结束后 artifacts/ 会写入交付物。'));

  const groups = new Map();
  for (const e of entries) {
    const top = e.path.includes('/') ? e.path.split('/')[0] : '';
    if (!groups.has(top)) groups.set(top, []);
    groups.get(top).push(e);
  }
  for (const [g, items] of groups) {
    if (g) list.appendChild(h('div', { class: 'art-group-title' }, g + '/'));
    for (const e of items) {
      const rm = roleMeta(e.role);
      list.appendChild(h('button', {
        class: `art-item${S.artifactSel === e.path ? ' active' : ''}`, 'data-path': e.path,
        onclick: () => previewArtifact(e),
      },
        h('span', { class: 'art-path', title: e.path }, e.path.split('/').pop()),
        e.role ? h('span', { class: 'role-chip', style: `--chip-color:${rm.color}` }, rm.label) : null,
        h('span', { class: 'art-size' }, fmtBytes(e.sizeBytes)),
        e.exists ? null : h('span', { class: 'art-missing', title: '文件缺失' }, '缺失')));
    }
  }

  const preview = h('div', { class: 'art-preview', id: 'art-preview' },
    h('div', { class: 'art-placeholder', id: 'art-placeholder' }, '选择左侧文件预览内容'));
  layout.appendChild(list);
  layout.appendChild(preview);
  (pane || $('#tab-pane')).appendChild(layout);
  // 快照重渲染后恢复正在预览的产物
  if (S.artifactSel) {
    const cur = entries.find((x) => x.path === S.artifactSel);
    if (cur) previewArtifact(cur);
  }
}

async function previewArtifact(e) {
  S.artifactSel = e.path;
  $$('.art-item').forEach((n) => n.classList.toggle('active', n.dataset.path === e.path));
  const pv = $('#art-preview');
  if (!pv) return;
  pv.innerHTML = '';
  try {
    const r = await callIpc(window.studio.readArtifact, S.selectedDir, e.path);
    if (r.binary) {
      pv.appendChild(h('div', { class: 'art-preview-head' },
        h('span', { class: 'art-preview-path' }, e.path),
        h('span', { class: 'art-size' }, fmtBytes(r.sizeBytes)),
        h('button', { class: 'btn btn-sm', onclick: () => revealArtifact(e.path) }, '在资源管理器中显示')));
      pv.appendChild(h('div', { class: 'art-placeholder' }, '二进制文件，不支持文本预览'));
      return;
    }
    pv.appendChild(h('div', { class: 'art-preview-head' },
      h('span', { class: 'art-preview-path', title: e.path }, `artifacts/${e.path}`),
      h('span', { class: 'art-size' }, `${fmtBytes(r.sizeBytes)}${r.truncated ? '（超过 2MB，已截断）' : ''}`),
      h('button', {
        class: 'btn btn-sm', onclick: async (ev) => {
          try { await navigator.clipboard.writeText(r.content); ev.target.textContent = '已复制'; setTimeout(() => { ev.target.textContent = '复制'; }, 1500); } catch { toast('error', '复制失败', '剪贴板不可用'); }
        },
      }, '复制'),
      h('button', { class: 'btn btn-sm', onclick: () => revealArtifact(e.path) }, '打开位置')));
    const pre = h('pre', { class: 'art-preview-body' }, r.content);
    pv.appendChild(pre);
  } catch (err) {
    pv.appendChild(h('div', { class: 'art-placeholder' }, `无法读取：${trunc(String(err && err.message || err), 120)}`));
  }
}

function revealArtifact(relPath) {
  const full = `${S.selectedDir}\\artifacts\\${relPath}`.replace(/\//g, '\\');
  window.studio.revealFile(full);
}

function previewArtifactByPath(relPath) {
  closeDrawer();
  S.tab = 'artifacts';
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === 'artifacts'));
  const pane = $('#tab-pane');
  if (pane) { pane.innerHTML = ''; renderArtifacts(pane).then(() => { const item = $(`.art-item[data-path="${CSS.escape(relPath)}"]`); if (item) item.click(); }); }
}

async function exportArtifactsNow() {
  try {
    const r = await callIpc(window.studio.exportArtifacts, S.selectedDir);
    if (r && r.saved) toast('success', '产物已导出', `${r.saved}（${fmtBytes(r.size)}）`);
  } catch (e) {
    toast('error', '导出失败', String(e && e.message || e));
  }
}

/* ---------- 图表 ---------- */

function llmStatsByRole() {
  const m = new Map();
  for (const ev of S.events) {
    if (ev.event !== 'llm_call') continue;
    const d = ev.data || {};
    const cur = m.get(d.role) || { role: d.role, calls: 0, cost: 0, tokens: 0 };
    cur.calls += 1;
    cur.cost += Number(d.estimated_cost_usd || 0);
    cur.tokens += Number(d.prompt_tokens || 0) + Number(d.completion_tokens || 0);
    m.set(d.role, cur);
  }
  return [...m.values()].sort((a, b) => b.cost - a.cost);
}

function renderCharts(pane) {
  const host = pane || $('#tab-pane');
  if (!host) return;
  host.innerHTML = '';
  const rows = llmStatsByRole();
  host.appendChild(h('div', { class: 'chart-block' },
    h('h3', {}, '各角色 LLM 用量'),
    S.eventsSkipped ? h('div', { class: 'form-hint', style: 'margin-bottom:10px' }, `仅基于已加载的最近 ${S.events.length} 条事件计算（更早 ${S.eventsSkipped} 条未加载）`) : null,
    rows.length ? rows.map((r) => chartRow(r)) : h('div', { class: 'form-hint' }, '暂无 llm_call 事件。')));
  const gates = S.events.filter((e) => e.event === 'gate_result');
  if (gates.length) {
    const appr = gates.filter((g) => g.data && g.data.verdict === 'approve').length;
    const rej = gates.filter((g) => g.data && g.data.verdict === 'reject').length;
    host.appendChild(h('div', { class: 'chart-block' },
      h('h3', {}, '门禁裁决'),
      h('div', { class: 'chart-bar-track', style: 'height:16px;display:flex' },
        h('i', { class: 'chart-bar-fill', style: `width:${(appr / gates.length) * 100}%;background:var(--st-succeeded)` }),
        h('i', { class: 'chart-bar-fill', style: `width:${(rej / gates.length) * 100}%;background:var(--st-failed)` })),
      h('div', { class: 'form-hint', style: 'margin-top:8px' }, `通过 ${appr} 次 · 驳回 ${rej} 次（共 ${gates.length} 轮）`)));
  }
}

function chartRow(r) {
  const rm = roleMeta(r.role);
  const maxCalls = Math.max(1, ...llmStatsByRole().map((x) => x.calls));
  const maxCost = Math.max(0.0000001, ...llmStatsByRole().map((x) => x.cost));
  return h('div', { class: 'chart-row' },
    h('div', { class: 'chart-role-label' },
      h('span', { class: 'role-chip', style: `--chip-color:${rm.color}` }, rm.label)),
    h('div', { class: 'chart-bars' },
      h('div', { class: 'chart-bar-track', title: `${r.calls} 次调用` },
        h('i', { class: 'chart-bar-fill', style: `width:${(r.calls / maxCalls) * 100}%;background:${rm.color}` })),
      h('div', { class: 'chart-bar-track', title: fmtCost(r.cost) },
        h('i', { class: 'chart-bar-fill', style: `width:${(r.cost / maxCost) * 100}%;background:var(--accent)` }))),
    h('div', { class: 'chart-value' }, `${r.calls} 次 · ${fmtCost(r.cost)}`));
}

/* ---------- 子进程：启动 / 取消 / 恢复 ---------- */

function wireProcPush() {
  window.studio.on('proc:log', (p) => {
    const log = $('#nr-log');
    if (log && S.activeProcKey === p.key) {
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
      const span = h('span', { class: p.stream === 'err' ? 'err-line' : '' }, p.text);
      log.appendChild(span);
      while (log.childNodes.length > 900) log.removeChild(log.firstChild);
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
  });
  window.studio.on('proc:run-dir', (p) => {
    S.procsByRunDir.set(p.runDir, p.key);
    if (S.activeProcKey === p.key) {
      toast('success', '运行已建立', `run 目录：${p.runId}`);
      refreshRuns();
      selectRun(p.runDir);
      const btn = $('#nr-goto');
      if (btn) { btn.classList.remove('hidden'); }
    }
  });
  window.studio.on('proc:exit', (p) => {
    refreshRuns();
    if (S.activeProcKey !== p.key) return;
    const log = $('#nr-log');
    if (log) log.appendChild(h('span', { class: 'err-line' }, `\n[进程退出：退出码 ${p.code}（${p.meaning}）]\n`));
    const startBtn = $('#nr-start');
    if (startBtn) {
      startBtn.textContent = '重新启动';
      startBtn.disabled = false;
    }
    const cancelBtn = $('#nr-cancel');
    if (cancelBtn) cancelBtn.classList.add('hidden');
  });
  window.studio.on('proc:error', (p) => {
    toast('error', 'CLI 进程异常', p.message);
    const startBtn = $('#nr-start');
    if (startBtn) { startBtn.disabled = false; startBtn.textContent = '启动'; }
  });
}

async function refreshProcMap() {
  try {
    const list = await window.studio.listProcs();
    S.procsByRunDir.clear();
    for (const p of list) if (p.runDir) S.procsByRunDir.set(p.runDir, p.key);
    if (S.run && S.run.status === 'running') {
      // 进程状态可能影响“取消/恢复”按钮可见性，轻量刷新头部
      const hasBtn = $('#btn-cancel-run');
      const should = S.procsByRunDir.has(S.selectedDir) || isStale(S.run);
      if (!!hasBtn !== should) renderRunView();
    }
  } catch { /* 忽略 */ }
}

async function cancelSelected() {
  const key = S.procsByRunDir.get(S.selectedDir);
  try {
    if (key) {
      await callIpc(window.studio.cancelRun, key);
      toast('info', '已发送终止信号', 'CLI 进程已终止，正在补写取消状态…');
    } else {
      await callIpc(window.studio.cancelOffline, S.selectedDir);
      toast('info', '已请求离线取消', '后台将把该 run 标记为 cancelled');
    }
    setTimeout(refreshRuns, 600);
  } catch { /* callIpc 已提示 */ }
}

async function resumeSelected() {
  try {
    const r = await callIpc(window.studio.resumeRun, {
      runDir: S.selectedDir,
      mock: !!S.settings.defaultMock,
      auto: !!S.settings.defaultAuto,
    });
    S.activeProcKey = r.key;
    toast('success', '已发起恢复', `${S.run ? S.run.run_id : ''} 继续执行`);
    selectRun(S.selectedDir);
  } catch { /* callIpc 已提示 */ }
}

async function confirmDeleteRun(r) {
  // P0-4：删除单个 run，不可恢复；running 状态按钮已禁用，这里双重守卫
  if (r.status === 'running') {
    toast('warn', '无法删除', '运行中的 run 请先取消');
    return;
  }
  const runId = r.runId || r.dir;
  const ok = window.confirm(`确认删除 run ${runId}？\n\n目标：${r.goal || '（无）'}\n状态：${r.status || 'unknown'}\n\n此操作不可恢复，整个 run 目录将被删除。`);
  if (!ok) return;
  try {
    await callIpc(window.studio.deleteRun, r.dir);
    toast('success', '已删除', `run ${runId} 已删除`);
    if (S.selectedDir === r.dir) {
      goBackToList();
    }
    refreshRuns();
  } catch (e) {
    toast('error', '删除失败', String(e && e.message || e));
  }
}

/* ---------- 模态框架 ---------- */

function closeModal() {
  $('#modal').classList.add('hidden');
  $('#modal-mask').classList.add('hidden');
}
$('#modal-mask').addEventListener('click', closeModal);

function showModal(node, kind) {
  const m = $('#modal');
  m.innerHTML = '';
  m.dataset.kind = kind;
  m.appendChild(node);
  $('#modal-mask').classList.remove('hidden');
  m.classList.remove('hidden');
}

/* ---------- 新运行 ---------- */

function openNewRunModal() {
  const s = S.settings || {};
  const form = h('div', {},
    h('h3', { class: 'modal-title' }, '启动新运行'),
    h('div', { class: 'form-row' },
      h('label', { for: 'nr-goal' }, '项目目标'),
      h('textarea', { id: 'nr-goal', placeholder: '用一句话描述要交付的东西，例如：开发一个短链接生成服务，含 API、文档与测试…' }),
      h('div', { class: 'form-hint' }, '将执行 python main.py run --goal …（需求拆解 → 代码生成 → 门禁审查 → 交付物产出）')),
    h('div', { class: 'form-row form-checks' },
      h('label', { class: 'form-check' }, h('input', { type: 'checkbox', id: 'nr-demo' }), '使用内置示例目标（--demo，Todo Web）'),
      h('label', { class: 'form-check' }, h('input', { type: 'checkbox', id: 'nr-mock', checked: !!s.defaultMock }), '离线模式（--mock，无需 API Key）'),
      h('label', { class: 'form-check' }, h('input', { type: 'checkbox', id: 'nr-auto', checked: !!s.defaultAuto }), '跳过人工闸门（--auto）')),
    h('div', { class: 'form-row' },
      h('label', { class: 'form-check' }, h('input', { type: 'checkbox', id: 'nr-slow' }), '慢速 mock（每次模拟调用延时 1.2s，便于观察实时进度）')),
    h('div', { class: 'form-row' },
      h('label', { for: 'nr-outdir' }, '输出目录'),
      h('input', { type: 'text', id: 'nr-outdir', value: s.outputDir || '' }),
      h('div', { class: 'form-hint' }, '运行产物写入该目录下的 <run_id>/ 子目录；GUI 始终附加 --no-live')),
    h('div', { class: 'form-row form-row-inline' },
      h('div', { class: 'form-field' },
        h('label', { for: 'nr-max-cost' }, '成本上限 (USD)'),
        h('input', { type: 'number', id: 'nr-max-cost', step: '0.01', min: '0', placeholder: '不限制' }),
        h('div', { class: 'form-hint' }, '累计 LLM 成本超限则优雅收尾')),
      h('div', { class: 'form-field' },
        h('label', { for: 'nr-max-llm' }, 'LLM 调用上限'),
        h('input', { type: 'number', id: 'nr-max-llm', step: '1', min: '0', placeholder: '不限制' }),
        h('div', { class: 'form-hint' }, '累计调用次数超限则优雅收尾'))),
    h('div', { class: 'form-hint err hidden', id: 'nr-error' }),
    h('div', { class: 'modal-log hidden', id: 'nr-log' }),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn btn-danger hidden', id: 'nr-cancel', onclick: async () => {
        const key = S.activeProcKey;
        if (key) { try { await callIpc(window.studio.cancelRun, key); } catch { /* 已提示 */ } }
      } }, '终止'),
      h('button', { class: 'btn hidden', id: 'nr-goto', onclick: () => closeModal() }, '转到看板'),
      h('button', { class: 'btn', id: 'nr-close', onclick: () => closeModal() }, '关闭'),
      h('button', { class: 'btn btn-primary', id: 'nr-start' }, '启动')),
  );
  showModal(form, 'new-run');

  $('#nr-demo').addEventListener('change', (e) => { $('#nr-goal').disabled = e.target.checked; });
  $('#nr-start').addEventListener('click', startNewRun);
}

async function startNewRun() {
  const errBox = $('#nr-error');
  const goal = $('#nr-goal').value.trim();
  const demo = $('#nr-demo').checked;
  const mock = $('#nr-mock').checked;
  const auto = $('#nr-auto').checked;
  const slow = $('#nr-slow').checked;
  const outputDir = $('#nr-outdir').value.trim() || (S.settings && S.settings.outputDir);
  const maxCost = $('#nr-max-cost').value.trim();
  const maxLlmCalls = $('#nr-max-llm').value.trim();
  if (!demo && !goal) {
    errBox.textContent = '请填写项目目标，或勾选“使用内置示例目标”。';
    errBox.classList.remove('hidden');
    return;
  }
  errBox.classList.add('hidden');
  const btn = $('#nr-start');
  btn.disabled = true;
  btn.textContent = '启动中…';
  try {
    const r = await callIpc(window.studio.startRun, {
      goal, demo, mock, auto, outputDir,
      maxCost: maxCost || undefined,
      maxLlmCalls: maxLlmCalls || undefined,
      env: slow ? { AGENTCREW_MOCK_LATENCY_MS: '1200' } : undefined,
    });
    S.activeProcKey = r.key;
    const log = $('#nr-log');
    log.classList.remove('hidden');
    log.textContent = '已发起 CLI：python main.py run …（--no-live 模式下 CLI 输出很少，实时进度请“转到看板”查看）\n';
    $('#nr-cancel').classList.remove('hidden');
    btn.textContent = '运行中…';
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '启动';
    errBox.textContent = `启动失败：${String(e && e.message || e)}`;
    errBox.classList.remove('hidden');
  }
}

/* ---------- 结束报告 ---------- */

function openReportModal() {
  const run = S.run;
  if (!run) return;
  const st = RUN_META[run.status] || RUN_META.unknown;
  const stats = run.stats || {};
  const fin = S.events.filter((e) => e.event === 'run_finished').pop();
  const exitInfo = fin && fin.data ? `退出码 ${fin.data.exit_code} · ${fmtDur(fin.data.duration_ms)}` : '';
  const grid = h('div', { class: 'report-grid' },
    cell('成功', String(stats.succeeded ?? '—'), 'color:var(--st-succeeded)'),
    cell('失败', String(stats.failed ?? '—'), stats.failed ? 'color:var(--st-failed)' : ''),
    cell('阻塞', String(stats.blocked ?? '—'), stats.blocked ? 'color:var(--st-blocked)' : ''),
    cell('跳过', String(stats.skipped ?? '—'), ''),
    cell('任务总数', String(stats.total_tasks ?? '—'), ''),
    cell('LLM 调用', fmtTokens(stats.llm_calls), ''),
    cell('Tokens', fmtTokens((stats.prompt_tokens || 0) + (stats.completion_tokens || 0)),
      `提示 ${fmtTokens(stats.prompt_tokens || 0)} / 补全 ${fmtTokens(stats.completion_tokens || 0)}`),
    cell('成本（估算）', fmtCost(stats.estimated_cost_usd), ''),
    cell('耗时', fmtDur(runDurationMs(run, S.tasks)), ''));
  const budgetEvt = S.events.filter((e) => e.event === 'budget_exhausted').pop();
  const failoverCount = S.events.filter((e) => e.event === 'llm_failover').length;
  const node = h('div', {},
    h('h3', { class: 'modal-title' }, '结束报告'),
    h('div', { class: 'report-status' },
      h('span', { class: 'status-chip big', style: `--chip-color:${st.color}` }, st.label),
      exitInfo ? h('span', { class: 'run-id' }, exitInfo) : null),
    budgetEvt ? h('div', { class: 'warn-banner', style: 'border-color:color-mix(in srgb,var(--st-failed) 40%,transparent);color:var(--st-failed)' },
      `预算耗尽：${budgetEvt.data && budgetEvt.data.kind === 'cost_usd' ? `成本 $${budgetEvt.data.current?.toFixed(4)} 超过上限 $${budgetEvt.data.limit}` : `LLM 调用 ${budgetEvt.data?.current} 次超过上限 ${budgetEvt.data?.limit} 次`}。run 已优雅收尾，在跑任务完成后停止派发。`) : null,
    failoverCount ? h('div', { class: 'form-hint', style: 'margin-bottom:8px' }, `Provider 故障转移 ${failoverCount} 次（详见事件流 llm_failover）`) : null,
    grid,
    h('div', { class: 'form-hint', style: 'margin-top:10px' }, `${run.run_id || ''} · ${trunc(run.project_goal || '', 100)}`),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', onclick: () => { closeModal(); S.tab = 'artifacts'; $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === 'artifacts')); renderTabPane(); } }, '查看产物'),
      h('button', { class: 'btn btn-primary', onclick: closeModal }, '关闭')));
  showModal(node, 'report');

  function cell(label, value, colorStyle, sub) {
    return h('div', { class: 'report-cell' },
      h('div', { class: 'stat-label' }, label),
      h('div', { class: 'stat-value', style: colorStyle || '' }, value),
      sub ? h('div', { class: 'stat-sub' }, sub) : null);
  }
}

/* ---------- 设置 ---------- */

function openSettingsModal() {
  const s = S.settings || {};
  const node = h('div', {},
    h('h3', { class: 'modal-title' }, '设置'),
    h('div', { class: 'form-row' },
      h('label', { for: 'set-python' }, 'Python 命令'),
      h('div', { style: 'display:flex;gap:8px' },
        h('input', { type: 'text', id: 'set-python', value: s.pythonCmd || '', placeholder: 'python（留空自动探测：python / python3 / py -3）', style: 'flex:1' }),
        h('button', { class: 'btn', id: 'set-python-test' }, '检测')),
      h('div', { class: 'form-hint', id: 'set-python-result' }, '点击“检测”验证命令可用性')),
    h('div', { class: 'form-row' },
      h('label', { for: 'set-repo' }, '仓库根目录（main.py 所在处）'),
      h('input', { type: 'text', id: 'set-repo', value: s.repoRoot || '' }),
      h('div', { class: 'form-hint', id: 'set-repo-hint' }, S.env && S.env.repoOk ? '✓ 已找到 main.py' : '⚠ 未找到 main.py，启动运行会失败')),
    h('div', { class: 'form-row' },
      h('label', { for: 'set-outdir' }, '输出目录（扫描运行列表的根）'),
      h('input', { type: 'text', id: 'set-outdir', value: s.outputDir || '' })),
    h('div', { class: 'form-row form-checks' },
      h('label', { class: 'form-check' }, h('input', { type: 'checkbox', id: 'set-mock', checked: !!s.defaultMock }), '新运行默认 --mock'),
      h('label', { class: 'form-check' }, h('input', { type: 'checkbox', id: 'set-auto', checked: !!s.defaultAuto }), '新运行默认 --auto')),
    h('div', { class: 'form-row' },
      h('label', {}, '主题'),
      h('div', { class: 'form-checks' },
        h('label', { class: 'form-check' }, h('input', { type: 'radio', name: 'set-theme', value: 'dark', checked: (s.theme || 'dark') === 'dark' }), '深色'),
        h('label', { class: 'form-check' }, h('input', { type: 'radio', name: 'set-theme', value: 'light', checked: s.theme === 'light' }), '浅色'))),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', onclick: closeModal }, '取消'),
      h('button', { class: 'btn btn-primary', id: 'set-save' }, '保存')),
  );
  showModal(node, 'settings');

  $('#set-python-test').addEventListener('click', async () => {
    const out = $('#set-python-result');
    out.className = 'form-hint';
    out.textContent = '检测中…';
    try {
      const r = await callIpc(window.studio.detectPython, $('#set-python').value.trim() || null);
      if (r) { out.className = 'form-hint ok'; out.textContent = `✓ ${r.version}`; }
      else { out.className = 'form-hint err'; out.textContent = '✗ 该命令不可用或不是 Python 3'; }
    } catch (e) { out.className = 'form-hint err'; out.textContent = `✗ ${trunc(String(e), 80)}`; }
  });

  $('#set-save').addEventListener('click', async () => {
    const next = {
      pythonCmd: $('#set-python').value.trim(),
      repoRoot: $('#set-repo').value.trim(),
      outputDir: $('#set-outdir').value.trim(),
      defaultMock: $('#set-mock').checked,
      defaultAuto: $('#set-auto').checked,
      theme: ($('input[name="set-theme"]:checked') || {}).value || 'dark',
    };
    try {
      const saved = await callIpc(window.studio.setSettings, next);
      S.settings = saved;
      const env = await window.studio.getSettings();
      S.env = env.env;
      applyTheme(S.settings.theme);
      closeModal();
      toast('success', '设置已保存', '正在按新配置刷新…');
      refreshPythonFoot();
      refreshRuns();
    } catch { /* callIpc 已提示 */ }
  });
}

/* ---------- 启动 ---------- */

boot();
