/**
 * 导航路由：switchNav 决定当前页面并触发渲染。
 */
import { $, $$, h } from '../lib/dom.js';
import { S, persistState } from '../lib/store.js';

import { renderAgentsPage } from '../features/agents-page/index.js';
import { renderArtifactsPage } from '../features/artifacts/index.js';
import { renderDagPage } from '../features/dag-page/index.js';
import { renderEventsPage } from '../features/events/index.js';
import { renderGatesPage } from '../features/gates-page/index.js';
import { renderPlanPage } from '../features/plan/index.js';
import { renderProvidersPage } from '../features/providers/index.js';
import { renderSettingsPage } from '../features/settings/index.js';
function switchNav(key){
  S.nav=key; persistState();
  $$('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.nav===key));
  const dash=$('#dashboard-view'), ph=$('#placeholder-view');
  if(key==='dashboard'){ ph.classList.add('hidden'); dash.classList.remove('hidden'); return; }
  dash.classList.add('hidden'); ph.classList.remove('hidden');
  ph.innerHTML='';
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
