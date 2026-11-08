/**
 * 导航路由：switchNav 决定当前页面并触发渲染。
 */
import { $, $$, h } from '../lib/dom.js';
import { S, persistState } from '../lib/store.js';

import { emptyState } from '../ui/states.js';

import { renderAgentsPage } from '../features/agents-page/index.js';
import { renderArtifactsPage } from '../features/artifacts/index.js';
import { renderDagPage } from '../features/dag-page/index.js';
import { renderEventsPage } from '../features/events/index.js';
import { renderGatesPage } from '../features/gates-page/index.js';
import { renderPlanPage } from '../features/plan/index.js';
import { renderProvidersPage } from '../features/providers/index.js';
import { renderSettingsPage } from '../features/settings/index.js';
function switchNav(key){
  S.nav=key;
  // 让上一版页面的异步续体失效：页面 render 里捕获 S.navToken，await 之后
  // 用 isStaleRender 比对，切走后不再往已卸载（或新版）的 DOM 写内容。
  S.navToken += 1;
  persistState();
  $$('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.nav===key));
  // 命令输入框只在运行总览出现：其他页面是表单/列表，悬浮框会挡住内容
  const composer=document.getElementById('composer');
  if(composer) composer.classList.toggle('hidden',key!=='dashboard');
  // 同时开关底部渐隐带，避免输入框消失后仍压着一层暗角
  const mainCol=document.querySelector('.main-col');
  if(mainCol) mainCol.classList.toggle('has-composer',key==='dashboard');
  const dash=$('#dashboard-view'), ph=$('#placeholder-view');
  // 无论去哪一页都先清空占位区：dashboard 分支此前只加 .hidden，上一页的
  // 监听器与 DOM 会一直挂到下次切换，行为与其余分支不一致。
  ph.innerHTML='';
  if(key==='dashboard'){
    ph.classList.add('hidden'); dash.classList.remove('hidden');
    // 隐藏期间实时通道跳过了全量重绘，切回来必须补一次，否则看到旧 DOM。
    window.dispatchEvent(new Event('studio:dashboard-show'));
    return;
  }
  dash.classList.add('hidden'); ph.classList.remove('hidden');
  // 统一的无运行数据态：8 个页面都直接读 S.dash.tasks / S.dash.run，为空时
  // 逐页抛 TypeError。给一个明确的空态 + 启动入口，而不是空白页或报错。
  if(!S.dash || !S.dash.run){
    ph.appendChild(emptyState('暂无运行数据',
      '当前没有可展示的运行。启动一次新运行，或从右上角切换到已有记录。',
      { label:'新建运行', onclick:()=>document.getElementById('btn-new-run')?.click() }));
    return;
  }
  if(key==='artifacts'){ renderArtifactsPage(ph); return; }
  if(key==='settings'){ renderSettingsPage(ph); return; }
  if(key==='events'){ renderEventsPage(ph); return; }
  if(key==='dag'){ renderDagPage(ph); return; }
  if(key==='tasks'){ renderPlanPage(ph); return; }
  if(key==='agents'){ renderAgentsPage(ph); return; }
  if(key==='gates'){ renderGatesPage(ph); return; }
  if(key==='providers'){ renderProvidersPage(ph); return; }
  const p={title:key,desc:'该页面开发中。'};
  ph.appendChild(h('div',{class:'ph-icon'},h('svg',{width:30,height:30,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':1.6,'stroke-linecap':'round','stroke-linejoin':'round'},h('rect',{x:'3',y:'3',width:'18',height:'18',rx:'3'}),h('path',{d:'M8 12h8'}))));
  ph.appendChild(h('h2',{},p.title)); ph.appendChild(h('p',{},p.desc));
  ph.appendChild(h('button',{class:'btn btn-primary',onclick:()=>switchNav('dashboard')},'返回运行总览'));
}

/* ============================================================
   Dashboard 主渲染 & 顶栏同步
   ============================================================ */

export { switchNav };
