/**
 * 启动装配：读设置 → 恢复上次运行与主题 → 绑定交互 → 首帧渲染。
 */
import { $, $$ } from '../lib/dom.js';
import { S } from '../lib/store.js';

import { anyOverlayOpen, closeModal } from '../ui/modal.js';
import { toast } from '../ui/toast.js';

import { renderDashboard } from '../features/dashboard/index.js';
import { closeDetailPanel } from '../features/detail/panel.js';
import { openNewRunModal } from '../features/new-run/index.js';
import { closeRunDropdown, toggleRunDropdown } from '../features/runs/index.js';

import { loadRealRun } from './data.js';
import { initMockData, startSim } from './mock.js';
import { onWatchLedger, onWatchSnapshot } from './realtime.js';
import { switchNav } from './router.js';
import { applyTheme, cycleTheme, openUserMenu, renderChrome } from './shell.js';
async function boot(){
  $('#btn-new-run').addEventListener('click',openNewRunModal);
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
      const pages=['dashboard','tasks','dag','agents','gates','artifacts','events','providers','settings'];
      const idx=parseInt(e.key)-1;
      if(pages[idx]){ switchNav(pages[idx]); }
    }
    // 空格：暂停/恢复（仅在 dashboard 且运行中）
    if(e.key===' '&&S.nav==='dashboard'&&S.dash.run){
      const run=S.dash.run;
      if(run.status==='running'){ e.preventDefault(); /* 暂停按钮逻辑 */ }
    }
  });

  try { const r=await window.studio.getSettings(); S.settings=r.settings; S.env=r.env; } catch { S.settings={}; }
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
  } catch {}

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
  } catch {}
  if(!S.realMode) initMockData();

  // 恢复 DAG 缩放/平移状态
  try {
    const savedView = localStorage.getItem('agentcrew:dagView');
    if (savedView) { const v = JSON.parse(savedView); S.view = { zoom: v.zoom || 1, x: v.x || 0, y: v.y || 0 }; }
  } catch {}

  renderDashboard();
  // 恢复上次查看的页面
  const lastPage = localStorage.getItem('agentcrew:lastPage');
  switchNav(lastPage || 'dashboard');
  if (!S.realMode) startSim();
  setInterval(renderChrome,1000);
}

/* 状态持久化 */

boot();

export { boot };
