/**
 * 启动装配：读设置 → 恢复上次运行与主题 → 绑定交互 → 首帧渲染。
 */
import { $, $$, h } from '../lib/dom.js';
import { S } from '../lib/store.js';

import { anyOverlayOpen, closeModal } from '../ui/modal.js';
import { toast } from '../ui/toast.js';

import { renderDashboard } from '../features/dashboard/index.js';
import { refreshConversation } from '../features/conversation/index.js';
import { openResultPanel } from '../features/result/index.js';
import { closeDetailPanel } from '../features/detail/panel.js';
import { initComposer, syncComposer } from '../features/composer/index.js';
import { openNewRunModal } from '../features/new-run/index.js';
import { closeRunDropdown, loadRunList, toggleRunDropdown } from '../features/runs/index.js';

import { loadRealRun } from './data.js';
import { initMockData, startSim } from './mock.js';
import { initRealtime, onWatchLedger, onWatchSnapshot } from './realtime.js';
import { switchNav } from './router.js';
import { applyTheme, cycleTheme, openUserMenu, renderChrome } from './shell.js';
/** 首屏加载遮罩：getSettings / detectPython / listRuns / loadRealRun 都是异步 IPC，
 *  期间界面是一副「空闲 / 0 Agents / 0 任务」的静态骨架，看起来像数据丢了。
 *  返回一个幂等的收起函数，由 boot 在 finally 里调用，异常路径也不会留遮罩。 */
function showBootLoading(){
  const overlay=h('div',{class:'loading-overlay',id:'boot-loading-overlay'},
    h('div',{class:'loading-spinner'}),
    h('div',{class:'loading-text'},'正在加载工作区…'));
  document.body.appendChild(overlay);
  let removed=false;
  return ()=>{ if(removed)return; removed=true; overlay.remove(); };
}

async function boot(){
  const hideBootLoading=showBootLoading();
  try { return await bootSequence(); }
  finally { hideBootLoading(); }
}

async function bootSequence(){
  $('#btn-new-run').addEventListener('click',openNewRunModal);
  // 侧栏搜索：直接打开运行切换下拉（下拉里已带项目筛选）
  const btnSearch = $('#btn-search');
  if (btnSearch) btnSearch.addEventListener('click',(e)=>{ e.stopPropagation(); toggleRunDropdown(); });
  // 侧栏 Projects 分组的 Show more → 计划审查页
  const sbMore = $('#sb-more');
  if (sbMore) sbMore.addEventListener('click',()=>switchNav('tasks'));
  initComposer();
  $('#btn-result').addEventListener('click', () => openResultPanel('overview'));
  $('#btn-refresh').addEventListener('click', async () => {
    if (S.realMode && S.watchDir) {
      try { await loadRealRun(S.watchDir); renderDashboard(); toast('info', '已刷新', '数据已重新加载'); }
      catch (e) { toast('error', '刷新失败', String(e.message || e)); }
    } else { renderDashboard(); toast('info', '已刷新', '演示数据已重新渲染'); }
  });
  $('#btn-notify').addEventListener('click',()=>{ $('#notify-dot').classList.add('hidden'); toast('info','通知','暂无新通知'); });
  $('#btn-user').addEventListener('click',openUserMenu);
  $('#btn-theme').addEventListener('click', cycleTheme);
  $('#tb-workspace').addEventListener('click',(e)=>{ e.stopPropagation(); toggleRunDropdown(); });
  document.addEventListener('click',(e)=>{ if(!e.target.closest('#run-dropdown')&&!e.target.closest('#tb-workspace')) closeRunDropdown(); });
  $$('.nav-item').forEach(b=>b.addEventListener('click',()=>switchNav(b.dataset.nav)));
  window.addEventListener('error',(e)=>toast('error','界面错误',e.message));
  window.addEventListener('unhandledrejection',(e)=>toast('error','未处理异常',String(e.reason&&e.reason.message||e.reason)));

  // 键盘快捷键
  window.addEventListener('keydown',(e)=>{
    // Escape：关闭面板/弹窗/下拉
    if(e.key==='Escape'){
      closeDetailPanel(); closeModal(); closeRunDropdown();
      return;
    }
    // Cmd/Ctrl + N：新建运行（已有弹窗/面板打开时不重复堆叠）
    if((e.metaKey||e.ctrlKey)&&e.key==='n'){ e.preventDefault(); if(!anyOverlayOpen()) openNewRunModal(); return; }
    // 数字键 1-9：切换导航（弹窗/详情打开时屏蔽，避免在遮罩层背后切页）
    if(!e.metaKey&&!e.ctrlKey&&!e.altKey&&/^[1-9]$/.test(e.key)){
      if(anyOverlayOpen()) return;
      const pages=['conversation','dashboard','tasks','dag','agents','profiles','gates','artifacts','events'];
      const idx=parseInt(e.key)-1;
      if(pages[idx]){ switchNav(pages[idx]); }
    }
    // 空格：暂停/恢复（仅在 dashboard 且运行中）
    if(e.key===' '&&S.nav==='dashboard'&&S.dash&&S.dash.run){
      const run=S.dash.run;
      if(run.status==='running'){ e.preventDefault(); /* 暂停按钮逻辑 */ }
    }
  });

  try {
    const r=await window.studio.getSettings();
    S.settings=r.settings; S.env=r.env;
    S.apiKeyConfigured=!!r.apiKeyConfigured;
  } catch { S.settings={}; S.apiKeyConfigured=false; }
  // 应用初始主题（light/dark/system）
  applyTheme();
  // 主进程推送系统主题变化（system 模式下自动跟随）
  try {
    window.studio.on('theme:changed', () => applyTheme());
    // 兜底：渲染进程侧也监听系统主题变化
    if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if ((S.settings && S.settings.theme) === 'system') applyTheme();
    });
  } catch {}
  try { await window.studio.detectPython(); } catch {}

  // 注册 watch 事件监听（真实数据模式下实时更新）
  try {
    window.studio.on('watch:snapshot', onWatchSnapshot);
    window.studio.on('watch:ledger', onWatchLedger);
    initRealtime();   // sse:frame / sse:closed + 流式面板按钮
  } catch {}
  // 实时通道只发「该刷新了」的信号，装配层决定重绘范围：看板可见时全量重绘，
  // 否则只刷顶栏（其它页也看得到运行数字）；切回看板时补一次全量重绘。
  window.addEventListener('studio:refresh', () => {
    if (S.nav === 'dashboard') renderDashboard();
    else if (S.nav === 'conversation') refreshConversation();
    else renderChrome();
  });
  window.addEventListener('studio:dashboard-show', () => renderDashboard());

  // CLI 进程生命周期：秒退且未产生 run 目录时正常监控链路完全看不到，必须显式报错
  try {
    window.studio.on('proc:error', (p) => {
      toast('error', 'CLI 启动失败', (p && p.message) || '无法启动 Python 进程，请到设置页检查 Python 命令', 9000);
    });
    window.studio.on('proc:exit', (p) => {
      if (!p || p.code === 0 || p.hadRunDir || p.killed) return;
      const tail = String(p.stderrTail || '').trim().split('\n').filter(Boolean).slice(-2).join('；');
      toast('error', `运行启动失败（${p.meaning || '退出码 ' + p.code}）`, tail.slice(0, 200) || '详见 studio/logs/', 12000);
    });
  } catch {}

  // 真实数据优先：有运行则加载完整真实数据，否则 mock
  S.realMode = false;
  try {
    const runs=await window.studio.listRuns();
    if(runs&&runs.length){
      // 优先恢复上次查看的 run，否则用最新的
      const lastDir = localStorage.getItem('agentcrew:lastRunDir');
      const target = lastDir && runs.find(r=>r.dir===lastDir) ? lastDir : runs[0].dir;
      const ok = await loadRealRun(target);
      if (ok) S.realMode = true;
    }
  } catch (e) {
    // C-10：静默降级会让用户以为看的是真实数据（实际是演示夹具）。
    // 启动阶段不弹 toast 打扰，但必须留痕可查。
    console.warn('[boot] 加载真实运行失败，已降级为演示数据：', e);
  }
  if(!S.realMode) initMockData();

  // 恢复 DAG 缩放/平移状态
  try {
    const savedView = localStorage.getItem('agentcrew:dagView');
    if (savedView) { const v = JSON.parse(savedView); S.view = { zoom: v.zoom || 1, x: v.x || 0, y: v.y || 0 }; }
  } catch {}

  renderDashboard();
  // 侧栏 Recents / 下拉共用的运行列表（一次 IPC，两处渲染）
  loadRunList();
  // 恢复上次查看的页面
  const lastPage = localStorage.getItem('agentcrew:lastPage');
  switchNav(lastPage || 'dashboard');
  if (!S.realMode) startSim();
  setInterval(()=>{ renderChrome(); syncComposer(); },1000);
}

/* 状态持久化 */

boot();

export { boot };
