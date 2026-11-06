/**
 * 任务视图：改造前的逐任务 DAG（最长路径分层 + 正交折线 + 状态色）。
 *
 * 保留它是为了「具体哪一单卡住了」这类问题——Agent 视图回答「谁等谁」，
 * 任务视图回答「哪一单」。两者共用同一份数据与同一套缩放/详情交互。
 */
import { STATUS_META, roleMeta } from '../../contracts/enums.js';

import { $, s } from '../../lib/dom.js';
import { fmtDur, trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { openTaskDetail } from '../detail/panel.js';

import { COL_GAP, NH, NW, ROW_GAP, arrowMarkerDefs, resetAnimIfNewRun, seenNodes, wireDAGControls } from './dag-canvas.js';

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

function layoutDAG(tasks) {
  const { byId, layerOf, layers } = computeLayers(tasks);
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

function renderTaskDAG(svg, tasks) {
  resetAnimIfNewRun();
  const wrap = $('#dag-canvas-wrap');
  const L = layoutDAG(tasks);
  svg.innerHTML = '';
  const pad = 32, totalW = L.width + pad * 2, totalH = L.height + pad * 2;
  // 与 Agent 视图一致：画布按图形比例撑开，不留信箱边
  if (wrap) { wrap.style.aspectRatio = `${totalW} / ${totalH}`; wrap.style.height = ''; }
  svg.setAttribute('viewBox', `0 0 ${totalW} ${totalH}`);

  const defs = s('defs', {});
  arrowMarkerDefs(defs);
  svg.appendChild(defs);

  const vp = s('g', { id: 'dag-viewport', transform: `translate(${S.view.x} ${S.view.y}) scale(${S.view.zoom})` });

  const runningIds = new Set(tasks.filter(t=>['running','review','rework'].includes(t.status)).map(t=>t.id));
  const hotEdges = new Set();
  for (const rid of runningIds){ const q=[rid],seen=new Set();
    while(q.length){ const cur=q.shift(); if(seen.has(cur))continue; seen.add(cur);
      const t=L.byId.get(cur); if(!t)continue;
      for(const dep of (t.dependencies||[])){ hotEdges.add(dep+'->'+cur); q.push(dep); } } }

  // 连线：正交折线（右侧→左侧），箭头 + 状态色
  for (const t of tasks){
    const to=L.pos.get(t.id); if(!to) continue;
    for(const dep of (t.dependencies||[])){
      const from=L.pos.get(dep); if(!from)continue;
      const x1=from.x+NW, y1=from.y+NH/2, x2=to.x, y2=to.y+NH/2;
      const midX=(x1+x2)/2;
      const hot=hotEdges.has(dep+'->'+t.id);
      const up=L.byId.get(dep);
      const upDone=up&&up.status==='succeeded';
      const upFailed=up&&up.status==='failed';
      // 连线配色交给 CSS（class 语义化），这样 hover/focus 才能覆盖到 stroke
      const kind = hot ? 'hot' : upDone ? 'done' : upFailed ? 'failed' : 'idle';
      const marker = hot ? 'url(#arrow-hot)' : upDone ? 'url(#arrow-done)' : 'url(#arrow-pending)';
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
    // 内层分组只承载视觉变换（hover 抬升/聚焦缩放），与定位用的 translate 分离
    const body=s('g',{class:'node-body'});

    body.appendChild(s('rect',{class:'node-box', width:NW, height:NH, rx:6,
      'stroke-width':isRunning?2.4:(isDone?1.8:1.3),
      'stroke-dasharray':isRetry?'5 3':'none' }));
    body.appendChild(s('rect',{class:'node-inner',x:3,y:3,width:NW-6,height:NH-6,rx:4}));
    body.appendChild(s('rect',{class:'node-accent',x:0,y:0,width:5,height:NH,rx:2.5}));

    body.appendChild(s('text',{class:'node-meta',x:14,y:17,'font-size':9.5,'font-family':'var(--font-mono)'}, t.id));
    if (t.result && t.result.duration_ms!=null)
      body.appendChild(s('text',{class:'node-meta',x:NW-12,y:17,'font-size':9,'text-anchor':'end','font-family':'var(--font-mono)'}, fmtDur(t.result.duration_ms)));

    // 标题拆两行——单行只放得下 11 个汉字
    const full=String(t.title||'');
    body.appendChild(s('text',{class:'node-title',x:14,y:36,'font-size':12.5,'font-weight':600}, full.slice(0,11)));
    if (full.length>11)
      body.appendChild(s('text',{class:'node-title',x:14,y:51,'font-size':11.5,'font-weight':500}, full.slice(11,23)+(full.length>23?'…':'')));

    const avatarY=68;
    body.appendChild(s('circle',{class:'node-avatar',cx:21,cy:avatarY,r:9,opacity:0.9}));
    body.appendChild(s('text',{class:'node-on-solid',x:21,y:avatarY+3.5,'font-size':9,'font-weight':700,'text-anchor':'middle'}, rm.label.charAt(0)));
    body.appendChild(s('text',{class:'node-role',x:34,y:avatarY+3,'font-size':10}, rm.full));
    const chipW = isRetry?48:42;
    body.appendChild(s('rect',{class:'node-chip',x:NW-chipW-10,y:avatarY-8,width:chipW,height:16,rx:3}));
    body.appendChild(s('text',{class:'node-on-solid',x:NW-chipW/2-10,y:avatarY+3,'font-size':9,'font-weight':600,'text-anchor':'middle'}, isRetry?'重试中':meta.label));

    if ((t.rework_count||0)>0)
      body.appendChild(s('text',{class:'node-retry',x:NW-12,y:36,'font-size':9,'text-anchor':'end'},`↻${t.rework_count}`));

    if (isRunning){
      body.appendChild(s('rect',{class:'node-prog-track',x:14,y:NH-10,width:NW-28,height:3,rx:1.5}));
      body.appendChild(s('rect',{class:'node-prog-fill',x:14,y:NH-10,width:(NW-28)*(t.progress||0)/100,height:3,rx:1.5}));
    }
    if (isFailed && t.error)
      body.appendChild(s('text',{class:'node-error',x:14,y:NH-12,'font-size':9}, trunc(t.error,22)));

    node.appendChild(body);
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

export { computeLayers, layoutDAG, renderTaskDAG };
