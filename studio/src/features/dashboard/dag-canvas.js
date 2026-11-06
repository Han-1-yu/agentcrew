/**
 * 依赖图共享件：世界坐标尺寸、SVG 文本/箭头构造、入场动画状态、缩放平移。
 *
 * 为什么拆出来：Agent 视图与任务视图共用同一套画布能力（缩放/平移/箭头/入场），
 * 拆开后两边只关心「节点长什么样、边怎么连」，不再各自维护一份坐标常量。
 */
import { roleMeta } from '../../contracts/enums.js';

import { $, s } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

//: 任务视图尺寸（沿用改造前的值，任务图观感不变）
const NW = 208, NH = 92, COL_GAP = 72, ROW_GAP = 48;

//: Agent 视图尺寸（世界坐标；viewBox 会自动缩放到画布内）
const AW = 220, AH = 148, AGENT_GAP_X = 36, AGENT_GAP_Y = 40;

function svgText(cls, x, y, str, attrs) {
  return s('text', Object.assign({ class: cls, x, y }, attrs || {}), str);
}

function arrowDefs(defs, roles) {
  for (const role of roles) {
    // 箭头统一冷白，与连线同色；id 仍按角色区分，避免多节点共用同一 marker 造成覆盖
    defs.appendChild(s('marker', {
      id: `agent-arrow-${role}`, viewBox: '0 0 9 9',
      refX: 7.5, refY: 4.5, markerWidth: 9, markerHeight: 9,
      markerUnits: 'userSpaceOnUse', orient: 'auto',
    }, s('path', { d: 'M 0 0 L 9 4.5 L 0 9 z', style: 'fill:var(--dag-edge)' })));
  }
}

function arrowMarkerDefs(defs) {
  defs.appendChild(s('marker', { id: 'arrow-hot', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto' },
    s('path', { d: 'M 0 0 L 10 5 L 0 10 z', style: 'fill:var(--accent)' })));
  defs.appendChild(s('marker', { id: 'arrow-done', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto' },
    s('path', { d: 'M 0 0 L 10 5 L 0 10 z', style: 'fill:var(--success)' })));
  defs.appendChild(s('marker', { id: 'arrow-pending', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 5, markerHeight: 5, orient: 'auto' },
    s('path', { d: 'M 0 0 L 10 5 L 0 10 z', style: 'fill:var(--ink-25)' })));
}

//: 入场动画去重：面板按轮询节奏反复重渲染，只有「本 run 第一次出现」的节点才播，
//: 否则节点会每 500ms 闪一次。换 run 时清空。
let dagAnimRun = null;
const seenNodes = new Set();

function resetAnimIfNewRun() {
  const runId = S.dash.run && S.dash.run.run_id;
  if (dagAnimRun !== runId) { dagAnimRun = runId; seenNodes.clear(); }
}

function wireDAGControls(svg, vp, totalW, totalH) {
  const wrap=$('#dag-canvas-wrap');
  if (!wrap) return;
  const apply=()=>{ vp.setAttribute('transform',`translate(${S.view.x} ${S.view.y}) scale(${S.view.zoom})`);
    const lbl=$('#dag-zoom-label'); if(lbl) lbl.textContent=Math.round(S.view.zoom*100)+'%';
    try { localStorage.setItem('agentcrew:dagView', JSON.stringify(S.view)); } catch {} };
  const zoomAt=(factor,cx,cy)=>{ const nz=Math.max(0.3,Math.min(3,S.view.zoom*factor));
    const rect=wrap.getBoundingClientRect(); const px=cx??rect.width/2, py=cy??rect.height/2;
    const wx=(px-S.view.x)/S.view.zoom, wy=(py-S.view.y)/S.view.zoom;
    S.view.zoom=nz; S.view.x=px-wx*nz; S.view.y=py-wy*nz; apply(); };
  const zin=$('#dag-zoom-in'), zout=$('#dag-zoom-out'), fit=$('#dag-fit'), reset=$('#dag-reset');
  if (zin) zin.onclick=()=>zoomAt(1.2);
  if (zout) zout.onclick=()=>zoomAt(1/1.2);
  if (reset) reset.onclick=()=>{ S.view.zoom=1;S.view.x=0;S.view.y=0;apply(); };
  if (fit) fit.onclick=()=>{ const rect=wrap.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    S.view.zoom=Math.min(1.2,Math.max(0.3,Math.min(rect.width/totalW,rect.height/totalH)*0.92));
    S.view.x=0; S.view.y=0; apply(); };
  wrap.onwheel=(e)=>{ e.preventDefault(); const r=wrap.getBoundingClientRect();
    zoomAt(e.deltaY<0?1.1:1/1.1, e.clientX-r.left, e.clientY-r.top); };
  let dragging=false,sx=0,sy=0,ox=0,oy=0;
  wrap.onmousedown=(e)=>{ if(e.target.closest('.dag-node'))return;
    dragging=true; wrap.classList.add('panning'); sx=e.clientX;sy=e.clientY;ox=S.view.x;oy=S.view.y; };
  // 用 on* 赋值而不是 addEventListener：renderDashboard 每 2.5s（或每个 SSE
  // 状态事件）都会重跑 wireDAGControls，addEventListener 会无限累加监听器，
  // 拖拽时重复写 S.view 与 localStorage。赋值天然幂等，且本仓库没有其他
  // window.onmousemove / onmouseup 使用者。
  window.onmousemove=(e)=>{ if(!dragging)return; S.view.x=ox+(e.clientX-sx); S.view.y=oy+(e.clientY-sy); apply(); };
  window.onmouseup=()=>{ dragging=false; wrap.classList.remove('panning'); };
}

export {
  AGENT_GAP_X,
  AGENT_GAP_Y,
  AH,
  AW,
  COL_GAP,
  NH,
  NW,
  ROW_GAP,
  arrowDefs,
  arrowMarkerDefs,
  resetAnimIfNewRun,
  seenNodes,
  svgText,
  wireDAGControls,
};
