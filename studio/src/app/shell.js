/**
 * 应用外壳：顶栏 / 侧栏 chrome、主题切换、全局快捷键、账户菜单。
 */
import { RUN_META } from '../contracts/enums.js';

import { $, h } from '../lib/dom.js';
import { fmtCost, fmtTokens, simplifyGoal } from '../lib/format.js';
import { S } from '../lib/store.js';

import { closeModal, showModal } from '../ui/modal.js';
import { toast } from '../ui/toast.js';

import { closeDetailPanel } from '../features/detail/panel.js';

import { applyDemoState } from './mock.js';
import { updateConnIndicator } from './realtime.js';
$('#detail-mask').addEventListener('click',closeDetailPanel);
document.addEventListener('keydown',(e)=>{ if(e.key==='Escape'){ closeDetailPanel(); closeModal(); } });
function renderChrome(){
  const d=S.dash; if(!d)return; const run=d.run; const st=run.stats||{};
  const meta=RUN_META[run.status]||RUN_META.idle;
  $('#tb-run-dot').className='dot '+meta.dot; $('#tb-run-label').textContent=meta.label;
  $('#tb-workspace').textContent=simplifyGoal(run.project_goal);
  const cost=st.estimated_cost_usd||0, pct=Math.min(100,cost/run.budget_limit_usd*100);
  $('#tb-budget-tokens').textContent=fmtTokens((st.prompt_tokens||0)+(st.completion_tokens||0));
  $('#tb-budget-cost').textContent=fmtCost(cost);
  $('#tb-budget-pct').textContent=Math.round(pct)+'%';
  $('#tb-budget-fill').style.width=pct+'%';
  const tb=$('#tb-budget'); tb.classList.toggle('warn',pct>=90&&pct<100); tb.classList.toggle('err',pct>=100);
  $('#sb-provider').textContent=run.provider; $('#sb-model').textContent=run.model;
  $('#sb-budget-pct').textContent=Math.round(pct)+'%'; $('#sb-budget-fill').style.width=pct+'%';
  const sbf=document.querySelector('.sb-progress'); sbf.classList.toggle('warn',pct>=90&&pct<100); sbf.classList.toggle('err',pct>=100);
  $('#sb-conn-dot').className='dot ' + (S.realMode ? 'running' : 'idle');
  $('#sb-conn-label').textContent = S.realMode ? '已连接' : '演示模式';
  const ds = $('#sb-datasource'); if (ds) { ds.textContent = S.realMode ? '真实' : '演示'; ds.style.color = S.realMode ? 'var(--success)' : 'var(--warn)'; }
  // V7 网关高级功能状态
  const cfg = S.settings || {};
  const routingEl = $('#sb-routing');
  if (routingEl) {
    const strategy = cfg.routingStrategy || 'weighted';
    const routingLabels = { weighted: '权重随机', rotate: '轮询', usage: '最少使用', order: '顺序' };
    routingEl.textContent = routingLabels[strategy] || strategy;
  }
  const affinityEl = $('#sb-affinity');
  if (affinityEl) {
    const mode = cfg.affinityMode || 'off';
    const affinityLabels = { off: '关闭', session: '会话保持', turn: '每轮可换' };
    affinityEl.textContent = affinityLabels[mode] || mode;
    affinityEl.style.color = mode === 'off' ? 'var(--text-ter)' : 'var(--accent)';
  }
  const redactEl = $('#sb-redact');
  if (redactEl) {
    redactEl.textContent = cfg.redactEnabled === false ? '关闭' : '启用';
    redactEl.style.color = cfg.redactEnabled === false ? 'var(--text-ter)' : 'var(--success)';
  }
  updateConnIndicator();
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
  const mode = (S.settings && S.settings.theme) || 'dark';
  if (mode === 'system') {
    try { return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
    catch { return 'dark'; }
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
  const mode = (S.settings && S.settings.theme) || 'dark';
  const btn = document.getElementById('btn-theme');
  if (btn) {
    btn.innerHTML = THEME_ICONS[mode] || THEME_ICONS.dark;
    btn.title = `主题：${THEME_LABEL[mode] || mode}（点击切换）`;
  }
}

// 顶栏按钮循环：light → dark → system → light
async function cycleTheme() {
  const cur = (S.settings && S.settings.theme) || 'dark';
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
