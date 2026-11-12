/**
 * 应用外壳：顶栏 / 侧栏 chrome、主题切换、全局快捷键、账户菜单。
 */
import { RUN_META, STATUS_META, roleMeta } from '../contracts/enums.js';

import { $, h } from '../lib/dom.js';
import { fmtCost, fmtTokens, simplifyGoal } from '../lib/format.js';
import { S } from '../lib/store.js';

import { closeModal, showModal } from '../ui/modal.js';
import { toast } from '../ui/toast.js';

import { closeDetailPanel, openTaskDetail } from '../features/detail/panel.js';

import { applyDemoState } from './mock.js';
import { updateConnIndicator } from './realtime.js';
$('#detail-mask').addEventListener('click',closeDetailPanel);
document.addEventListener('keydown',(e)=>{ if(e.key==='Escape'){ closeDetailPanel(); closeModal(); } });

/* ============================================================
   chrome 渲染
   ------------------------------------------------------------
   顶栏只留一行指标（运行状态 / Agent 数 / 任务数 / Token·费用·预算），
   侧栏负责品牌、导航与分组列表。所有写入都走 setText/守卫，
   某个元素在别的页面里不存在时不会连累整轮渲染。
   ============================================================ */

/** 只在值真的变了才写 DOM：renderChrome 每秒跑一次，避免无谓的重排。 */
function setText(id, value){
  const el = document.getElementById(id);
  if(!el) return;
  const v = value == null ? '' : String(value);
  if(el.textContent !== v) el.textContent = v;
}
function setWidth(id, pct){
  const el = document.getElementById(id);
  if(el) el.style.width = pct + '%';
}

function renderChrome(){
  const d=S.dash; if(!d) return;
  const run=d.run; const st=run.stats||{};
  const meta=RUN_META[run.status]||RUN_META.idle;

  const dotEl=$('#tb-run-dot');
  if(dotEl) dotEl.className='dot '+meta.dot;
  setText('tb-run-label', meta.label);
  setText('tb-workspace', simplifyGoal(run.project_goal));
  setText('tb-agents', Object.keys(d.agents||{}).length);
  setText('tb-tasks', (d.tasks||[]).length);

  const cost=st.estimated_cost_usd||0;
  const limit=run.budget_limit_usd||0;
  const pct=limit>0?Math.min(100,cost/limit*100):0;
  setText('tb-budget-tokens', fmtTokens((st.prompt_tokens||0)+(st.completion_tokens||0)));
  setText('tb-budget-cost', fmtCost(cost));
  setText('tb-budget-pct', Math.round(pct)+'%');
  setWidth('tb-budget-fill', pct);
  const tb=$('#tb-budget');
  if(tb){ tb.classList.toggle('warn',pct>=90&&pct<100); tb.classList.toggle('err',pct>=100); }

  setText('sb-provider', run.provider);
  setText('sb-model', run.model);
  setText('sb-budget-pct', Math.round(pct)+'%');
  setWidth('sb-budget-fill', pct);
  const sbf=document.querySelector('.sb-progress');
  if(sbf){ sbf.classList.toggle('warn',pct>=90&&pct<100); sbf.classList.toggle('err',pct>=100); }

  const sbDot=$('#sb-conn-dot');
  if(sbDot) sbDot.className='dot '+(S.realMode?'running':'idle');
  setText('sb-conn-label', S.realMode?'已连接':'演示模式');
  const ds=$('#sb-datasource');
  if(ds){ ds.textContent=S.realMode?'真实':'演示'; ds.style.color=S.realMode?'var(--success)':'var(--warn)'; }

  renderSidebarTasks();
  updateConnIndicator();
}

/* ---- 侧栏 Projects 分组：当前运行的缩进任务列表（搜索 + 状态筛选 + 角色分组） ---- */
const TASK_FILTERS = [
  { key: 'all', label: '全部', statuses: null },
  { key: 'active', label: '进行中', statuses: ['running', 'review', 'rework'] },
  { key: 'pending', label: '待处理', statuses: ['pending', 'ready', 'blocked'] },
  { key: 'done', label: '完成', statuses: ['succeeded', 'skipped'] },
  { key: 'failed', label: '失败', statuses: ['failed', 'cancelled'] },
];
let lastTaskSig = '';
let taskToolsBound = false;

function taskFilterKey() { return S.taskStatusFilter || 'all'; }

/* 搜索框 + 筛选 chips 的事件只在首次绑定（元素常驻，不被重渲染清空）。 */
function bindTaskTools() {
  if (taskToolsBound) return;
  taskToolsBound = true;
  const box = document.getElementById('sb-task-search');
  if (box) box.addEventListener('input', (e) => { S.taskSearch = e.target.value; lastTaskSig = ''; renderSidebarTasks(); });
}

function renderSidebarTasks() {
  const list = document.getElementById('sb-tasklist');
  const chips = document.getElementById('sb-task-filters');
  if (!list) return;
  bindTaskTools();
  const tasks = (S.dash && S.dash.tasks) || [];
  const q = String(S.taskSearch || '').trim().toLowerCase();
  const fk = taskFilterKey();

  // 状态筛选 chips（只在切换后重绘，避免每秒清掉 hover 态）
  if (chips && chips.dataset.key !== fk) {
    chips.dataset.key = fk;
    chips.innerHTML = '';
    TASK_FILTERS.forEach((f) => {
      chips.appendChild(h('button', {
        class: `sb-task-chip${fk === f.key ? ' on' : ''}`,
        onclick: () => { S.taskStatusFilter = f.key; lastTaskSig = ''; renderSidebarTasks(); },
      }, f.label));
    });
  }

  const filter = TASK_FILTERS.find((f) => f.key === fk) || TASK_FILTERS[0];
  let filtered = tasks;
  if (filter.statuses) filtered = tasks.filter((t) => filter.statuses.includes(t.status));
  if (q) filtered = filtered.filter((t) => String(t.id || '').toLowerCase().includes(q)
    || String(t.title || '').toLowerCase().includes(q)
    || String(t.description || '').toLowerCase().includes(q)
    || String(t.brief || '').toLowerCase().includes(q));

  // 签名含筛选态与任务，变化才重绘（每秒一次的重渲染：内容没变就跳过，避免丢 hover）
  const sig = fk + '|' + q + '|' + ((S.dash && S.dash.run && S.dash.run.dir) || '')
    + '|' + filtered.slice(0, 12).map((t) => t.id + '.' + t.status).join('|') + '|' + filtered.length;
  if (sig === lastTaskSig) return;
  lastTaskSig = sig;

  list.innerHTML = '';
  if (!filtered.length) {
    list.appendChild(h('li', {}, h('span', { class: 'sb-li-text' }, tasks.length ? '无匹配任务' : '暂无任务')));
    return;
  }

  // 按角色分组（对齐 WorkBuddy 任务列表的文件夹分组）
  const top = filtered.slice(0, 12);
  const byRole = new Map();
  top.forEach((t) => {
    if (!byRole.has(t.role)) byRole.set(t.role, []);
    byRole.get(t.role).push(t);
  });
  for (const [role, ts] of byRole) {
    const rm = roleMeta(role);
    list.appendChild(h('li', { class: 'sb-task-group' },
      h('span', { class: 'sb-li-dot', style: `background:${rm.color}` }),
      h('span', { class: 'sb-li-text' }, `${rm.full} · ${ts.length}`)));
    ts.forEach((t) => {
      const sm = STATUS_META[t.status] || STATUS_META.unknown;
      list.appendChild(h('li', { class: 'is-click sb-task-item', title: `${t.id} ${t.title}`, onclick: () => openTaskDetail(t.id) },
        h('span', { class: 'sb-task-dot', style: `background:${sm.color}` }),
        h('span', { class: 'sb-li-text' }, t.title)));
    });
  }
  if (filtered.length > top.length) {
    list.appendChild(h('li', {}, h('span', { class: 'sb-li-text' }, `还有 ${filtered.length - top.length} 个任务…`)));
  }
}

/* ============================================================
   演示边界状态
   ============================================================ */
const THEME_ORDER = ['light', 'dark', 'system'];
const THEME_ICONS = {
  light: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>',
  dark: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
  system: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>',
};
const THEME_LABEL = { light: '浅色', dark: '深色', system: '跟随系统' };

// 把用户选择的 theme 解析为实际生效的 light|dark
function effectiveTheme() {
  const mode = (S.settings && S.settings.theme) || 'light';
  if (mode === 'system') {
    try { return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
    catch { return 'light'; }
  }
  return mode;
}

// 应用主题到 <html data-theme>，并同步顶栏按钮图标
function applyTheme() {
  const eff = effectiveTheme();
  const root = document.documentElement;
  // 换肤是一次性的大范围属性变化：临时禁掉过渡，否则几百个元素同时跑 200ms 动画会掉帧。
  root.classList.add('theme-switching');
  root.dataset.theme = eff;
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove('theme-switching')));
  // 缓存已解析的实际主题，供 index.html 首帧内联脚本读取，避免启动闪白
  try { localStorage.setItem('agentcrew:theme', eff); } catch {}
  const mode = (S.settings && S.settings.theme) || 'light';
  const btn = document.getElementById('btn-theme');
  if (btn) {
    btn.innerHTML = THEME_ICONS[mode] || THEME_ICONS.light;
    btn.title = `主题：${THEME_LABEL[mode] || mode}（点击切换）`;
  }
}

// 顶栏按钮循环：light → dark → system → light
async function cycleTheme() {
  const cur = (S.settings && S.settings.theme) || 'light';
  const next = THEME_ORDER[(THEME_ORDER.indexOf(cur) + 1) % THEME_ORDER.length];
  S.settings.theme = next;   // 乐观更新 UI
  applyTheme();
  try {
    await window.studio.setSettings({ theme: next });
  } catch (e) {
    // 持久化失败：回滚内存态并重应用，避免「看着切了、重启复原」
    S.settings.theme = cur;
    applyTheme();
    toast('error', '主题保存失败', String(e && e.message || e));
  }
}

function openUserMenu(){
  const states=[['running','正常运行（模拟推进）'],['planning','正在规划'],['budget_warn','预算预警（90%）'],['budget_exhausted','预算耗尽'],
    ['paused','用户暂停'],['failed','运行失败'],['gates_failed','门禁失败'],['completed','运行完成'],['empty','无运行记录']];
  showModal(h('div',{},
    h('h3',{class:'modal-title'},'账户与演示'),
    h('div',{class:'confirm-text',style:'color:var(--text-ter)'},'工作区：'+(S.settings&&S.settings.outputDir||'默认')),
    h('div',{style:'margin:10px 0;font-weight:600;font-size:13px'},'演示边界状态：'),
    ...states.map(([k,label])=>h('button',{class:'btn',style:'display:flex;width:100%;margin-bottom:6px;justify-content:flex-start',onclick:()=>{closeModal();applyDemoState(k);}},label)),
    h('div',{class:'modal-actions'},h('button',{class:'btn',onclick:closeModal},'关闭'))));
}

export {
  THEME_ICONS,
  THEME_LABEL,
  THEME_ORDER,
  applyTheme,
  cycleTheme,
  effectiveTheme,
  openUserMenu,
  renderChrome,
};
