/**
 * 任务依赖图：最长路径分层 + SVG 渲染 + 缩放平移。
 */
import { STATUS_META, roleMeta } from '../../contracts/enums.js';

import { $, h, s } from '../../lib/dom.js';
import { fmtDur, trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { openTaskDetail } from '../detail/panel.js';
function computeLayers(tasks) {
  const byId = new Map(tasks.map(t=>[t.id,t]));
  const layerOf = new Map(tasks.map(t=>[t.id,0]));
  let changed=true, guard=0;
  while (changed && guard++<50) {
    changed=false;
    for (const t of tasks) {
      const deps=t.dependencies||[]; if(!deps.length) continue;
      const up=Math.max(...deps.map(d=>layerOf.get(d)??0));
      if (layerOf.get(t.id)!==up+1){ layerOf.set(t.id,up+1); changed=true; }
    }
  }
  const layers=[];
  for (const [id,l] of layerOf){ while(layers.length<=l) layers.push([]); layers[l].push(id); }
  return { byId, layerOf, layers };
}
const NW=208, NH=86, COL_GAP=72, ROW_GAP=48;
function layoutDAG(tasks) {
  const { byId, layerOf, layers } = computeLayers(tasks);
  // 重心启发式排序：减少层间连线交叉
  for (let pass = 0; pass < 3; pass++) {
    // 前向：根据上游位置排序
    for (let li = 1; li < layers.length; li++) {
      layers[li].sort((a, b) => {
        const aDeps = (byId.get(a).dependencies || []).map(d => layers[li-1].indexOf(d)).filter(i => i >= 0);
        const bDeps = (byId.get(b).dependencies || []).map(d => layers[li-1].indexOf(d)).filter(i => i >= 0);
        const aCenter = aDeps.length ? aDeps.reduce((s,v)=>s+v,0)/aDeps.length : layers[li-1].length/2;
        const bCenter = bDeps.length ? bDeps.reduce((s,v)=>s+v,0)/bDeps.length : layers[li-1].length/2;
        return aCenter - bCenter || a.localeCompare(b);
      });
    }
    // 后向：根据下游位置排序
    for (let li = layers.length - 2; li >= 0; li--) {
      layers[li].sort((a, b) => {
        const aDown = layers[li+1].filter(id => (byId.get(id).dependencies || []).includes(a)).map(id => layers[li+1].indexOf(id));
        const bDown = layers[li+1].filter(id => (byId.get(id).dependencies || []).includes(b)).map(id => layers[li+1].indexOf(id));
        const aCenter = aDown.length ? aDown.reduce((s,v)=>s+v,0)/aDown.length : layers[li+1].length/2;
        const bCenter = bDown.length ? bDown.reduce((s,v)=>s+v,0)/bDown.length : layers[li+1].length/2;
        return aCenter - bCenter || a.localeCompare(b);
      });
    }
  }
  const pos = new Map();
  layers.forEach((ids,li)=>{ ids.forEach((id,ri)=>pos.set(id,{x:li*(NW+COL_GAP), y:ri*(NH+ROW_GAP)})); });
  const maxLayer=layers.length-1, maxRows=Math.max(...layers.map(l=>l.length));
  return { byId, layers, pos, width:(maxLayer+1)*(NW+COL_GAP), height:maxRows*(NH+ROW_GAP) };
}

/* ============================================================
   SVG DAG 渲染
   ============================================================ */
function renderDAG() {
  const svg=$('#dag-svg'); if(!svg) return;
  const tasks=S.dash.tasks; const L=layoutDAG(tasks);
  svg.innerHTML='';
  const pad=32, totalW=L.width+pad*2, totalH=L.height+pad*2;
  svg.setAttribute('viewBox',`0 0 ${totalW} ${totalH}`);

  // SVG defs: 箭头 marker（颜色走 CSS 变量，随主题切换）
  const defs=s('defs',{});
  defs.appendChild(s('marker',{id:'arrow-hot',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto'},
    s('path',{d:'M 0 0 L 10 5 L 0 10 z',style:'fill:var(--accent)'})));
  defs.appendChild(s('marker',{id:'arrow-done',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto'},
    s('path',{d:'M 0 0 L 10 5 L 0 10 z',style:'fill:var(--success)'})));
  defs.appendChild(s('marker',{id:'arrow-pending',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:5,markerHeight:5,orient:'auto'},
    s('path',{d:'M 0 0 L 10 5 L 0 10 z',style:'fill:var(--ink-25)'})));
  svg.appendChild(defs);

  const vp=s('g',{id:'dag-viewport', transform:`translate(${S.view.x} ${S.view.y}) scale(${S.view.zoom})`});

  const runningIds=new Set(tasks.filter(t=>['running','review','rework'].includes(t.status)).map(t=>t.id));
  const hotEdges=new Set();
  for (const rid of runningIds){ const q=[rid],seen=new Set();
    while(q.length){ const cur=q.shift(); if(seen.has(cur))continue; seen.add(cur);
      const t=L.byId.get(cur); if(!t)continue;
      for(const dep of (t.dependencies||[])){ hotEdges.add(dep+'->'+cur); q.push(dep); } } }

  // 连线：正交折线（右侧→左侧），箭头 + 状态色
  for (const t of tasks){
    const to=L.pos.get(t.id); if(!to) continue;
    for(const dep of (t.dependencies||[])){
      const from=L.pos.get(dep); if(!from)continue;
      // 从源节点右侧中心 → 目标节点左侧中心
      const x1=from.x+NW, y1=from.y+NH/2, x2=to.x, y2=to.y+NH/2;
      const midX=(x1+x2)/2;
      const hot=hotEdges.has(dep+'->'+t.id);
      const up=L.byId.get(dep);
      const upDone=up&&up.status==='succeeded';
      const upFailed=up&&up.status==='failed';
      let stroke, sw, marker, dash='none';
      if(hot){ stroke='var(--accent)'; sw=2.2; marker='url(#arrow-hot)'; }
      else if(upDone){ stroke='var(--success)'; sw=1.6; marker='url(#arrow-done)'; }
      else if(upFailed){ stroke='var(--warn)'; sw=1.4; marker='url(#arrow-pending)'; dash='4 3'; }
      else { stroke='var(--ink-25)'; sw=1.2; marker='url(#arrow-pending)'; }
      // 正交折线：右出 → 水平 → 垂直 → 水平 → 左入
      vp.appendChild(s('path',{ d:`M ${x1} ${y1} H ${midX} V ${y2} H ${x2-2}`,
        fill:'none', style:`stroke:${stroke}`, 'stroke-width':sw, 'stroke-dasharray':dash, 'marker-end':marker, opacity: hot?1:0.8 }));
    }
  }

  // 节点
  for (const t of tasks){
    const p=L.pos.get(t.id); if(!p)continue;
    const meta=STATUS_META[t.status]||STATUS_META.unknown;
    const rm=roleMeta(t.role);
    const isRetry=(t.attempts||1)>1 && ['running','ready','rework'].includes(t.status);
    const isRunning=t.status==='running';
    const isDone=t.status==='succeeded';
    const isFailed=t.status==='failed';
    const statusKey = STATUS_META[t.status] ? t.status : 'unknown';

    const node=s('g',{class:'dag-node'+(isRunning?' running':''), transform:`translate(${p.x+pad} ${p.y+pad})`, 'data-task':t.id,
      'data-status':statusKey, 'data-role':t.role||''});

    // 背景（fill/stroke 由 CSS 按 data-status 主题化）
    node.appendChild(s('rect',{class:'node-box', width:NW, height:NH, rx:6,
      'stroke-width':isRunning?2.4:(isDone?1.8:1.3),
      'stroke-dasharray':isRetry?'5 3':'none' }));
    // 左侧状态色条
    node.appendChild(s('rect',{class:'node-accent',x:0,y:0,width:5,height:NH,rx:2.5}));

    // 顶部：task_id + 耗时
    node.appendChild(s('text',{class:'node-meta',x:14,y:17,'font-size':9.5,'font-family':'var(--font-mono)'}, t.id));
    if (t.result && t.result.duration_ms!=null)
      node.appendChild(s('text',{class:'node-meta',x:NW-12,y:17,'font-size':9,'text-anchor':'end','font-family':'var(--font-mono)'}, fmtDur(t.result.duration_ms)));

    // 标题
    node.appendChild(s('text',{class:'node-title',x:14,y:38,'font-size':12.5,'font-weight':600}, trunc(t.title,13)));

    // 底部：角色头像 + 名称 + 状态
    const avatarY=62;
    // 角色圆形头像（fill 由 CSS 按 data-role 主题化）
    node.appendChild(s('circle',{class:'node-avatar',cx:21,cy:avatarY,r:9,opacity:0.9}));
    node.appendChild(s('text',{class:'node-on-solid',x:21,y:avatarY+3.5,'font-size':9,'font-weight':700,'text-anchor':'middle'}, rm.label.charAt(0)));
    // 角色名
    node.appendChild(s('text',{class:'node-role',x:34,y:avatarY+3,'font-size':10}, rm.full));
    // 状态标签（右侧，实心小 chip；fill 由 CSS 按 data-status 主题化）
    const chipW = isRetry?48:42;
    node.appendChild(s('rect',{class:'node-chip',x:NW-chipW-10,y:avatarY-8,width:chipW,height:16,rx:3}));
    node.appendChild(s('text',{class:'node-on-solid',x:NW-chipW/2-10,y:avatarY+3,'font-size':9,'font-weight':600,'text-anchor':'middle'}, isRetry?'重试中':meta.label));

    // 返工次数
    if ((t.rework_count||0)>0)
      node.appendChild(s('text',{class:'node-retry',x:NW-12,y:38,'font-size':9,'text-anchor':'end'},`↻${t.rework_count}`));

    // running：底部进度条
    if (isRunning){
      node.appendChild(s('rect',{class:'node-prog-track',x:14,y:NH-10,width:NW-28,height:3,rx:1.5}));
      node.appendChild(s('rect',{class:'node-prog-fill',x:14,y:NH-10,width:(NW-28)*(t.progress||0)/100,height:3,rx:1.5}));
    }
    // failed：错误摘要
    if (isFailed && t.error)
      node.appendChild(s('text',{class:'node-error',x:14,y:NH-12,'font-size':9}, trunc(t.error,22)));

    node.addEventListener('click',()=>openTaskDetail(t.id));
    vp.appendChild(node);
  }
  svg.appendChild(vp);
  wireDAGControls(svg, vp, totalW, totalH);
}

function wireDAGControls(svg, vp, totalW, totalH) {
  const wrap=$('#dag-canvas-wrap');
  const apply=()=>{ vp.setAttribute('transform',`translate(${S.view.x} ${S.view.y}) scale(${S.view.zoom})`);
    const lbl=$('#dag-zoom-label'); if(lbl) lbl.textContent=Math.round(S.view.zoom*100)+'%';
    try { localStorage.setItem('agentcrew:dagView', JSON.stringify(S.view)); } catch {} };
  const zoomAt=(factor,cx,cy)=>{ const nz=Math.max(0.3,Math.min(3,S.view.zoom*factor));
    const rect=wrap.getBoundingClientRect(); const px=cx??rect.width/2, py=cy??rect.height/2;
    const wx=(px-S.view.x)/S.view.zoom, wy=(py-S.view.y)/S.view.zoom;
    S.view.zoom=nz; S.view.x=px-wx*nz; S.view.y=py-wy*nz; apply(); };
  $('#dag-zoom-in').onclick=()=>zoomAt(1.2);
  $('#dag-zoom-out').onclick=()=>zoomAt(1/1.2);
  $('#dag-reset').onclick=()=>{ S.view.zoom=1;S.view.x=0;S.view.y=0;apply(); };
  $('#dag-fit').onclick=()=>{ const rect=wrap.getBoundingClientRect();
    S.view.zoom=Math.min(1.2,Math.max(0.3,Math.min(rect.width/totalW,rect.height/totalH)));
    S.view.x=(rect.width-totalW*S.view.zoom)/2; S.view.y=(rect.height-totalH*S.view.zoom)/2; apply(); };
  wrap.onwheel=(e)=>{ e.preventDefault(); const r=wrap.getBoundingClientRect();
    zoomAt(e.deltaY<0?1.1:1/1.1, e.clientX-r.left, e.clientY-r.top); };
  let dragging=false,sx=0,sy=0,ox=0,oy=0;
  wrap.onmousedown=(e)=>{ if(e.target.closest('.dag-node'))return;
    dragging=true; wrap.classList.add('panning'); sx=e.clientX;sy=e.clientY;ox=S.view.x;oy=S.view.y; };
  window.addEventListener('mousemove',(e)=>{ if(!dragging)return; S.view.x=ox+(e.clientX-sx); S.view.y=oy+(e.clientY-sy); apply(); });
  window.addEventListener('mouseup',()=>{ dragging=false; wrap.classList.remove('panning'); });
}

function renderDAGFallback() {
  const host=$('#dag-list-fallback'); if(!host)return; host.innerHTML='';
  const tasks=S.dash.tasks; const groups={};
  for(const t of tasks){ (groups[t.status]=groups[t.status]||[]).push(t); }
  const order=['running','review','rework','ready','blocked','pending','succeeded','failed','skipped'];
  for(const st of order){ const items=groups[st]; if(!items||!items.length)continue;
    const meta=STATUS_META[st];
    host.appendChild(h('div',{class:'dag-group-title'},`${meta.label}（${items.length}）`));
    for(const t of items){ const rm=roleMeta(t.role);
      host.appendChild(h('div',{class:'dag-task-row',onclick:()=>openTaskDetail(t.id)},
        h('span',{class:'dot',style:`background:${meta.color}`}),
        h('span',{class:'mono',style:`color:var(--text-ter)`},t.id),
        h('span',{},t.title),
        h('span',{style:`color:${rm.color};margin-left:auto;font-size:11px`},rm.full)));
    }
  }
}

/* ============================================================
   Dashboard 渲染
   ============================================================ */

export {
  COL_GAP,
  NH,
  NW,
  ROW_GAP,
  computeLayers,
  layoutDAG,
  renderDAG,
  renderDAGFallback,
  wireDAGControls,
};
