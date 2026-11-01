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
const NW=208, NH=92, COL_GAP=72, ROW_GAP=48;

//: 入场动画去重：面板按轮询节奏反复重渲染，只有「本 run 第一次出现」的任务才播
//: 入场，否则节点会每 500ms 闪一次。换 run 时清空。
let dagAnimRun = null;
const seenNodes = new Set();
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
  if (dagAnimRun !== (S.dash.run && S.dash.run.run_id)) {
    dagAnimRun = S.dash.run && S.dash.run.run_id; seenNodes.clear();
  }
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
      // 连线配色交给 CSS（class 语义化），这样 hover/focus 才能覆盖到 stroke；
      // 内联 style 的优先级高于类规则，改色就只能靠 !important，得不偿失。
      const kind = hot ? 'hot' : upDone ? 'done' : upFailed ? 'failed' : 'idle';
      const marker = hot ? 'url(#arrow-hot)' : upDone ? 'url(#arrow-done)' : 'url(#arrow-pending)';
      // 正交折线：右出 → 水平 → 垂直 → 水平 → 左入
      vp.appendChild(s('path',{ class:`dag-edge ${kind}`, d:`M ${x1} ${y1} H ${midX} V ${y2} H ${x2-2}`,
        fill:'none', 'marker-end':marker, 'data-from':dep, 'data-to':t.id }));
    }
  }

  // 节点
  let nodeSeq = 0;
  for (const t of tasks){
    const p=L.pos.get(t.id); if(!p)continue;
    const meta=STATUS_META[t.status]||STATUS_META.unknown;
    const rm=roleMeta(t.role);
    const isRetry=(t.attempts||1)>1 && ['running','ready','rework'].includes(t.status);
    const isRunning=t.status==='running';
    const isDone=t.status==='succeeded';
    const isFailed=t.status==='failed';
    const statusKey = STATUS_META[t.status] ? t.status : 'unknown';

    const fresh = !seenNodes.has(t.id);
    const node=s('g',{class:'dag-node'+(isRunning?' running':'')+(fresh?' enter':''), transform:`translate(${p.x+pad} ${p.y+pad})`, 'data-task':t.id,
      'data-status':statusKey, 'data-role':t.role||'', style:`--i:${nodeSeq++}`});
    seenNodes.add(t.id);
    // 内层分组只承载视觉变换（hover 抬升/聚焦缩放），与定位用的 translate 分离——
    // SVG 的 CSS transform 会整体覆盖 transform 属性，混在一起会把节点挪飞。
    const body=s('g',{class:'node-body'});

    // 背景（fill/stroke 由 CSS 按 data-status 主题化）
    body.appendChild(s('rect',{class:'node-box', width:NW, height:NH, rx:6,
      'stroke-width':isRunning?2.4:(isDone?1.8:1.3),
      'stroke-dasharray':isRetry?'5 3':'none' }));
    // 内嵌细描边：让卡片有"厚度"，而不是一块平色
    body.appendChild(s('rect',{class:'node-inner',x:3,y:3,width:NW-6,height:NH-6,rx:4}));
    // 左侧状态色条
    body.appendChild(s('rect',{class:'node-accent',x:0,y:0,width:5,height:NH,rx:2.5}));

    // 顶部：task_id + 耗时
    body.appendChild(s('text',{class:'node-meta',x:14,y:17,'font-size':9.5,'font-family':'var(--font-mono)'}, t.id));
    if (t.result && t.result.duration_ms!=null)
      body.appendChild(s('text',{class:'node-meta',x:NW-12,y:17,'font-size':9,'text-anchor':'end','font-family':'var(--font-mono)'}, fmtDur(t.result.duration_ms)));

    // 标题：拆两行——单行只放得下 11 个汉字，截断太早会让整张图变成「标题猜谜」
    const full=String(t.title||'');
    body.appendChild(s('text',{class:'node-title',x:14,y:36,'font-size':12.5,'font-weight':600}, full.slice(0,11)));
    if (full.length>11)
      body.appendChild(s('text',{class:'node-title',x:14,y:51,'font-size':11.5,'font-weight':500}, full.slice(11,23)+(full.length>23?'…':'')));

    // 底部：角色头像 + 名称 + 状态
    const avatarY=68;
    // 角色圆形头像（fill 由 CSS 按 data-role 主题化）
    body.appendChild(s('circle',{class:'node-avatar',cx:21,cy:avatarY,r:9,opacity:0.9}));
    body.appendChild(s('text',{class:'node-on-solid',x:21,y:avatarY+3.5,'font-size':9,'font-weight':700,'text-anchor':'middle'}, rm.label.charAt(0)));
    // 角色名
    body.appendChild(s('text',{class:'node-role',x:34,y:avatarY+3,'font-size':10}, rm.full));
    // 状态标签（右侧，实心小 chip；fill 由 CSS 按 data-status 主题化）
    const chipW = isRetry?48:42;
    body.appendChild(s('rect',{class:'node-chip',x:NW-chipW-10,y:avatarY-8,width:chipW,height:16,rx:3}));
    body.appendChild(s('text',{class:'node-on-solid',x:NW-chipW/2-10,y:avatarY+3,'font-size':9,'font-weight':600,'text-anchor':'middle'}, isRetry?'重试中':meta.label));

    // 返工次数
    if ((t.rework_count||0)>0)
      body.appendChild(s('text',{class:'node-retry',x:NW-12,y:36,'font-size':9,'text-anchor':'end'},`↻${t.rework_count}`));

    // running：底部进度条
    if (isRunning){
      body.appendChild(s('rect',{class:'node-prog-track',x:14,y:NH-10,width:NW-28,height:3,rx:1.5}));
      body.appendChild(s('rect',{class:'node-prog-fill',x:14,y:NH-10,width:(NW-28)*(t.progress||0)/100,height:3,rx:1.5}));
    }
    // failed：错误摘要
    if (isFailed && t.error)
      body.appendChild(s('text',{class:'node-error',x:14,y:NH-12,'font-size':9}, trunc(t.error,22)));

    node.appendChild(body);
    // 悬停聚焦：高亮该节点的直接上下游连线，其余节点降透明——在密集图里找关系
    node.addEventListener('mouseenter',()=>{
      svg.classList.add('dag-focus'); node.classList.add('is-focus');
      vp.querySelectorAll('.dag-edge').forEach(e=>{
        if(e.dataset.from===t.id||e.dataset.to===t.id) e.classList.add('edge-focus');
      });
    });
    node.addEventListener('mouseleave',()=>{
      svg.classList.remove('dag-focus'); node.classList.remove('is-focus');
      vp.querySelectorAll('.dag-edge.edge-focus').forEach(e=>e.classList.remove('edge-focus'));
    });
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
