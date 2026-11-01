/**
 * Agent 协作区块：8 个角色的当前任务与累计产出。
 */

import { ROLE_ORDER, agentStatusLabel, roleMeta } from '../../contracts/enums.js';
import { openAgentDetail } from '../detail/panel.js';
import { $, h } from '../../lib/dom.js';
import { fmtCost, fmtTokens, relTime } from '../../lib/format.js';
import { S } from '../../lib/store.js';


function renderAgents() {
  const grid=$('#agents-grid'); if(!grid)return; grid.innerHTML='';
  const agents=S.dash.agents;
  for(const rid of ROLE_ORDER){ const a=agents[rid]; const rm=roleMeta(rid);
    const active=S.detail&&S.detail.kind==='agent'&&S.detail.id===rid;
    grid.appendChild(h('div',{class:`agent-card${active?' active':''}`,onclick:()=>openAgentDetail(rid)},
      h('div',{class:'ac-head'},
        h('span',{class:'ac-dot',style:`background:${rm.color}`}),
        h('span',{class:'ac-name'},rm.full),
        h('span',{class:'ac-status'}, agentStatusLabel(a.status))),
      h('div',{class:'ac-task',title:a.task},a.task),
      h('div',{class:'ac-action',title:a.action},a.action),
      h('div',{class:'ac-meta'},
        metaCell('完成',String(a.done)), metaCell('失败/重试',String(a.fail)),
        metaCell('模型',a.model), metaCell('Token',fmtTokens(a.tokens)),
        metaCell('费用',fmtCost(a.cost)), metaCell('更新',relTime(a.updated)))));
  }
}

function metaCell(k,v){ return h('div',{},h('div',{style:'color:var(--text-ter)'},k),h('b',{},v)); }
// agentStatusLabel 由 contracts/enums.js 统一提供（Agents 页面共用同一份文案）

export { metaCell, renderAgents };
