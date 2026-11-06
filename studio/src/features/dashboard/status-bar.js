/**
 * 运行状态条：当前 run 的目标、阶段、指标与操作按钮。
 */

import { pushEvent, startSim, stopSim } from '../../app/mock.js';
import { PHASES, RUN_META } from '../../contracts/enums.js';
import { renderDashboard } from './index.js';
import { exportReport, exportRun } from '../events/export.js';
import { openNewRunModal } from '../new-run/index.js';
import { openModelSwitcher } from '../providers/models.js';
import { switchRun } from '../runs/index.js';
import { $, h } from '../../lib/dom.js';
import { fmtCost, fmtDur, fmtTokens, parseTs, relTime, simplifyGoal } from '../../lib/format.js';
import { S } from '../../lib/store.js';
import { iconPause, iconPlay, iconX } from '../../ui/icons.js';
import { confirmAction } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

// 「恢复」后延迟 2s 刷新当前 run；期间用户可能已经切到别的 run，旧的定时器
// 不该再强行 switchRun 回来。模块级持有句柄，重复点击也只保留最后一个。
let resumeSwitchTimer = null;

function renderRunStatusBar() {
  const host=$('#run-status-bar'); if(!host)return;
  const run=S.dash.run; const meta=RUN_META[run.status]||RUN_META.idle;
  host.className = ''; host.classList.add('status-'+(run.status||'idle'));
  const stats=run.stats||{};
  const elapsed=run.status==='running'&&stats.started_at ? (Date.now()-parseTs(stats.started_at).getTime()) : null;
  const cost=stats.estimated_cost_usd||0;
  const actions=[];
  const isReal = !!S.realMode;
  if (run.status==='running'){
    if (!isReal) {
      actions.push(h('button',{class:'btn',onclick:()=>confirmAction('暂停运行','暂停后所有 Agent 将在当前任务完成后停止，可随时恢复。',
        ()=>{ run.status='paused'; toast('warning','已暂停','运行已挂起'); renderDashboard(); })},
        iconPause(),'暂停'));
    }
    actions.push(h('button',{class:'btn btn-danger',onclick:()=>confirmAction('取消运行','取消后本次运行将被终止，未完成任务标记为失败。',
      async ()=>{
        if (isReal && run.dir) {
          try { await window.studio.cancelOffline(run.dir); toast('info','已发送取消','等待进程退出…'); }
          catch(e) { toast('error','取消失败',String(e.message||e)); }
        } else {
          run.status='cancelled'; pushEvent('run_cancelled',{}); stopSim(); renderDashboard();
          toast('error','已取消','运行已终止');
        }
      })},
      iconX(),'取消'));
  } else if (run.status==='paused' || run.status==='cancelled' || run.status==='failed'){
    actions.push(h('button',{class:'btn btn-primary',onclick:async ()=>{
      if (isReal && run.dir) {
        try {
          // 按原运行的模式恢复（config_snapshot.mock 是落盘事实），不能硬编码 mock
          const origMock = !!(run.config_snapshot && run.config_snapshot.mock);
          await window.studio.resumeRun({runDir: run.dir, mock: origMock, auto: true});
          toast('success','已恢复', origMock ? '从检查点继续运行（Mock）' : '从检查点继续运行（真实模型）');
          if (resumeSwitchTimer) clearTimeout(resumeSwitchTimer);
          const resumeDir = run.dir;
          resumeSwitchTimer = setTimeout(async ()=>{
            resumeSwitchTimer = null;
            if (S.watchDir && S.watchDir !== resumeDir) return;
            try { await switchRun(resumeDir); } catch {}
          }, 2000);
        } catch(e) { toast('error','恢复失败',String(e.message||e)); }
      } else {
        run.status='running'; pushEvent('human_approved',{note:'resume from checkpoint'}); startSim(); renderDashboard();
        toast('success','已恢复','从检查点继续运行');
      }
    }},
      iconPlay(),'恢复'));
  }
  if (isReal) {
    actions.push(h('button',{class:'btn',onclick:()=>{ openNewRunModal(); $('#nr-goal').value = run.project_goal || ''; }},'重新运行'));
    actions.push(h('button',{class:'btn',onclick:openModelSwitcher},'切换模型'));
    actions.push(h('button',{class:'btn',onclick:()=>window.studio.openPath(run.dir)},'打开目录'));
    actions.push(h('button',{class:'btn',onclick:exportRun},'导出运行'));
    actions.push(h('button',{class:'btn',onclick:exportReport},'导出报告'));
  } else {
    actions.push(h('button',{class:'btn',onclick:()=>toast('info','重新运行','已用相同目标启动新运行')},'重新运行'));
    actions.push(h('button',{class:'btn',onclick:()=>{ pushEvent('checkpoint_saved',{manual:true}); toast('success','检查点','已从最近检查点继续'); }},'从检查点继续'));
  }

  host.innerHTML='';
  host.appendChild(h('div',{},
    h('div',{class:'rs-top'},
      h('span',{class:'rs-id',title:'点击复制 Run ID',onclick:(e)=>{navigator.clipboard.writeText(run.run_id);toast('success','已复制',run.run_id);}},run.run_id),
      h('span',{class:'rs-goal',title:run.project_goal}, simplifyGoal(run.project_goal)),
      h('div',{class:'rs-stage-seg'}, PHASES.map((p,i)=>h('button',{class:(['planning','execution','delivery'][i])===run.phase?'on':'',disabled:true},p))),
      h('span',{class:`rs-status-chip ${meta.chip}`}, h('span',{class:`dot ${meta.dot}`}), meta.label)),
    h('div',{class:'rs-stats'},
      rsStat('已运行时间', elapsed!=null?fmtDur(elapsed):'—'),
      rsStat('Token 用量', fmtTokens((stats.prompt_tokens||0)+(stats.completion_tokens||0))),
      rsStat('估算费用', `${fmtCost(cost)} / $${run.budget_limit_usd}`),
      rsStat('运行模型', run.model),
      rsStat('任务进度', `${stats.succeeded||0}/${stats.total_tasks||0} 完成`),
      rsStat('最近更新', relTime(run.updated_at))),
    h('div',{class:'rs-actions'}, actions)));
}

function rsStat(k,v){ return h('div',{class:'rs-stat'},h('div',{class:'k'},k),h('div',{class:'v'},v)); }

export { renderRunStatusBar, rsStat };
