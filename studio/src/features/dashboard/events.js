/**
 * 事件时间线区块：类型筛选 + 最近事件列表。
 */

import { ev } from '../../app/mock.js';
import { EVENT_META, roleMeta } from '../../contracts/enums.js';
import { metaCell, renderAgents } from './agents.js';
import { renderCostPanel } from './cost.js';
import { acceptGate, gateStatusLabel, rejectGate, renderGates } from './gates.js';
import { renderRunStatusBar, rsStat } from './status-bar.js';
import { openTaskDetail } from '../detail/panel.js';
import { $, h } from '../../lib/dom.js';
import { fmtDur, relTime, trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';


function renderEvents() {
  const list=$('#events-list'), filter=$('#events-filter'); if(!list)return;
  const mainTypes=['run_started','plan_created','task_started','task_finished','gate_result','llm_failover','budget_warning','checkpoint_saved','artifact_written','human_approved','error'];
  filter.innerHTML='';
  for(const ty of mainTypes){ const m=EVENT_META[ty];
    filter.appendChild(h('button',{class:`fchip${S.eventFilter.has(ty)?' on':''}`,
      style:`--ev-color:${m.color}`,
      onclick:()=>{ S.eventFilter.has(ty)?S.eventFilter.delete(ty):S.eventFilter.add(ty); renderEvents(); }},
      h('span',{class:'fdot'}), m.label));
  }
  list.innerHTML='';
  const evs=[...S.dash.events].reverse().filter(e=>S.eventFilter.has(e.event)).slice(0,50);
  for(const e of evs){
    const m=EVENT_META[e.event]||{color:'var(--text-ter)',label:e.event};
    const key=e.ts+e.event; const expanded=S.eventExpanded.has(key);
    const tid=e.data&&e.data.task_id;
    list.appendChild(h('div',{class:`event-row${['error','gate_failed'].includes(e.event)?' ev-error':''}`},
      h('span',{class:'event-ts'},relTime(e.ts)),
      h('span',{class:'event-type',style:`color:${m.color}`},m.label),
      h('div',{class:'event-body'},
        h('div',{class:'event-text'}, eventSummary(e),
          tid?h('span',{class:'event-task-link',onclick:()=>openTaskDetail(tid)},` ${tid}`):null,
          h('span',{class:'event-toggle',onclick:(ev2)=>{ev2.stopPropagation(); expanded?S.eventExpanded.delete(key):S.eventExpanded.add(key); renderEvents();}}, expanded?'收起':'详情')),
        expanded?h('div',{class:'event-payload'},h('pre',{},JSON.stringify(e.data,null,2))):null)));
  }
}

function eventSummary(e) {
  const d=e.data||{};
  switch(e.event){
    case 'run_started': return `运行创建：${trunc(d.goal||'',40)}`;
    case 'plan_created': return `任务生成：${d.task_count} 个任务`;
    case 'task_started': return `${d.task_id||''} 开始执行（${roleMeta(d.role).full}，第 ${d.attempt||1} 次）`;
    case 'task_finished': return `${d.task_id||''} 完成，耗时 ${fmtDur(d.duration_ms)}`;
    case 'task_transition': return `${d.task_id||''} ${d.from} → ${d.to}（${d.reason||''}）`;
    case 'artifact_written': return `产物生成：${d.path}`;
    case 'gate_result': return `门禁 ${d.gate||d.layer}：${d.verdict}${d.score!=null?`（分数 ${d.score}）`:''}`;
    case 'llm_failover': return `Provider 故障转移：${d.from||'?'} → ${d.to||'?'}（${trunc(d.reason||'',30)}）`;
    case 'budget_warning': return `预算预警：已使用 ${d.pct}%`;
    case 'budget_exhausted': return '预算硬上限耗尽，运行优雅收尾';
    case 'task_timeout': return `${d.task_id||''} 超时`;
    case 'checkpoint_saved': return `检查点保存${d.manual?'（手动）':''}`;
    case 'human_approved': return `人工批准：${trunc(d.note||'',40)}`;
    case 'error': return `${d.where||''}：${trunc(d.message||'',50)}`;
    default: return JSON.stringify(d).slice(0,80);
  }
}

/* ============================================================
   右侧详情面板
   ============================================================ */


export { eventSummary, renderEvents };
