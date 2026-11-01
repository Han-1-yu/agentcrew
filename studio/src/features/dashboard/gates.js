/**
 * 质量门禁区块：三层门禁状态 + 人工通过 / 驳回（驳回走返工）。
 */

import { pushEvent } from '../../app/mock.js';
import { renderDashboard } from './index.js';
import { openTaskDetail } from '../detail/panel.js';
import { $, h } from '../../lib/dom.js';
import { fmtDur } from '../../lib/format.js';
import { S } from '../../lib/store.js';
import { confirmAction } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';


function renderGates() {
  const grid=$('#gates-grid'); if(!grid)return; grid.innerHTML='';
  for(const g of S.dash.gates){
    const c={pending:'pending',running:'running',passed:'passed',failed:'failed',review:'review'}[g.status]||'pending';
    const failDetail=g.status==='failed'?h('div',{class:'gate-fail-detail'},
      h('div',{},h('span',{class:'lbl'},'失败原因：'),g.fail_reason||'关键检查未通过'),
      h('div',{},h('span',{class:'lbl'},'影响范围：'),g.impact||g.task_ref),
      h('div',{},h('span',{class:'lbl'},'建议处理：'),g.suggestion||'修复后重新提交'),
      h('div',{},h('span',{class:'lbl'},'返回节点：'),g.return_task||'T-11'),
      h('div',{},h('span',{class:'lbl'},'预计成本：'),g.extra_cost||'$0.05')):null;
    grid.appendChild(h('div',{class:`gate-card ${g.status==='failed'?'failed':g.status==='running'?'running':''}`},
      h('div',{class:'gate-head'},
        h('span',{class:'gate-num'},g.id),
        h('span',{class:'gate-name'},g.name),
        h('span',{class:`gate-status-chip ${c}`},gateStatusLabel(g.status)),
        h('div',{class:'gate-tags'},
          g.rework?h('span',{class:'gate-tag'},'允许返工'):h('span',{class:'gate-tag'},'不可返工'),
          g.need_human?h('span',{class:'gate-tag'},'需人工确认'):null)),
      h('div',{class:'gate-summary'},
        `检查项 ${g.checks.total} · 通过 ${g.checks.passed} · 失败 ${g.checks.failed}`+
        (g.duration_ms!=null?` · 耗时 ${fmtDur(g.duration_ms)}`:'')+` — ${g.verdict}`),
      failDetail,
      h('div',{class:'gate-actions'},
        h('button',{class:'btn btn-sm',onclick:()=>toast('info','门禁详情',`${g.id} 检查项：${g.checks.total} 项`)},'查看详情'),
        h('button',{class:'btn btn-sm btn-primary',onclick:()=>acceptGate(g)},'接受结果'),
        h('button',{class:'btn btn-sm btn-danger',onclick:()=>rejectGate(g)},'驳回并返工'),
        h('button',{class:'btn btn-sm',onclick:()=>{g.status='running';g.checks={total:g.checks.total,passed:0,failed:0};toast('info','重试门禁',g.name+' 重新运行');renderDashboard();}},'重试门禁'),
        h('button',{class:'btn btn-sm',onclick:()=>{const t=(g.task_ref||'').split(',')[0].trim();if(t)openTaskDetail(t);}},'相关任务'))));
  }
}

function gateStatusLabel(st){ return {pending:'未开始',running:'进行中',passed:'通过',failed:'失败',review:'等待人工确认'}[st]||st; }

function acceptGate(g){
  if (S.realMode && S.dash.run?.dir) {
    window.studio.gateAction(S.dash.run.dir, g.layer, 'approve')
      .then(() => { g.status='passed'; g.checks={...g.checks,failed:0}; pushEvent('gate_result',{layer:g.layer,verdict:'approve',gate:g.id}); toast('success','门禁通过',g.name+' 已接受'); renderDashboard(); })
      .catch(e => toast('error','批准失败',String(e.message||e)));
  } else {
    g.status='passed'; g.checks={...g.checks,failed:0}; pushEvent('gate_result',{layer:g.layer,verdict:'approve',gate:g.id}); toast('success','门禁通过',g.name+' 已接受'); renderDashboard();
  }
}

function rejectGate(g){ confirmAction('驳回并返工',`将 ${g.name} 打回返工，预计增加约 $0.05 模型成本。`,
  ()=>{
    if (S.realMode && S.dash.run?.dir) {
      window.studio.gateAction(S.dash.run.dir, g.layer, 'reject')
        .then(() => { g.status='failed'; pushEvent('gate_result',{layer:g.layer,verdict:'reject',gate:g.id}); toast('warning','已驳回',g.name+' 已退回相关任务'); renderDashboard(); })
        .catch(e => toast('error','驳回失败',String(e.message||e)));
    } else {
      g.status='failed'; pushEvent('gate_result',{layer:g.layer,verdict:'reject',gate:g.id}); toast('warning','已驳回',g.name+' 已退回相关任务'); renderDashboard();
    }
  }); }

export { acceptGate, gateStatusLabel, rejectGate, renderGates };
