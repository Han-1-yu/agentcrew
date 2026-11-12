/**
 * 状态推演：每 2.5s 推进一次任务状态，让演示模式看起来在跑。
 * 真实运行时由 app/realtime.js 接管，不经过这里。
 */

import { RUN_ID, T, buildMockAgents, buildMockEvents, buildMockGates, buildMockRun, buildMockTasks, ev, initMockData, nowTs } from './fixtures.js';
import { deriveAgents, deriveGates } from '../data.js';
import { renderDashboard } from '../../features/dashboard/index.js';
import { $, h } from '../../lib/dom.js';
import { parseTs } from '../../lib/format.js';
import { S } from '../../lib/store.js';
import { toast } from '../../ui/toast.js';

function pushEvent(event, data, taskId, role){
  S.dash.events.push({ts:nowTs(0), run_id:RUN_ID, event, data:Object.assign(taskId?{task_id:taskId}:{},role?{role}:{},data||{})});
  window.__st.ledgerBatches += 1;
}

function startSim(){ if(S.simTimer)return; S.simTimer=setInterval(simTick,2500); }

function stopSim(){ if(S.simTimer){clearInterval(S.simTimer);S.simTimer=null;} }


function simTick(){
  const d=S.dash; if(!d)return;
  const run=d.run; if(run.status!=='running')return;
  window.__st.snapshots += 1;

  for(const t of d.tasks){
    if(t.status==='running'){
      t.progress=Math.min(100,(t.progress||0)+15+Math.random()*12);
      if(t.progress>=100){
        // T-08 第一次失败后重试：running → ready（reason:retry），attempts+1
        if(t.id==='T-08' && t.attempts===1){
          t.status='ready'; t.error='连接数据库超时（ETIMEDOUT）'; t.attempts=2; t.progress=0;
          pushEvent('task_transition',{from:'running',to:'ready',reason:'retry: ETIMEDOUT'} ,t.id,t.role);
          pushEvent('error',{where:t.id,message:'ETIMEDOUT'},t.id,t.role);
          toast('error','任务失败将重试',`${t.id} 第 ${t.attempts} 次尝试`);
        } else {
          t.status='review'; t.progress=0;
          const dur=300000+Math.random()*400000;
          t.result={summary:'任务产物已生成并通过初检', duration_ms:dur, artifacts:[{path:`artifacts/${t.id}/output.md`,size_bytes:2000+Math.random()*8000}]};
          pushEvent('task_finished',{attempt:t.attempts,duration_ms:dur},t.id,t.role);
          // 门禁：G1 static approve → succeeded
          t.reviews=[{round:1,layer:'static',verdict:'approve',score:null,reviewer:'static:contract'}];
          pushEvent('gate_result',{task_id:t.id,round:1,layer:'static',verdict:'approve',score:null});
          t.status='succeeded';
          pushEvent('task_transition',{from:'review',to:'succeeded',reason:'gate-succeeded'},t.id,t.role);
          // 解除下游
          for(const down of d.tasks){
            if((down.dependencies||[]).includes(t.id)){
              const deps=down.dependencies.map(x=>d.tasks.find(z=>z.id===x));
              if(deps.every(z=>z.status==='succeeded') && down.status==='blocked'){
                down.status='ready'; pushEvent('task_transition',{from:'blocked',to:'ready',reason:'dependencies-recovered'},down.id,down.role);
              }
            }
          }
        }
      }
    } else if(t.status==='ready'){
      t.status='running'; t.progress=5;
      const ag=d.agents[t.role]; if(ag){ag.status='executing';ag.task=`${t.id} ${t.title}`;ag.action='正在执行';ag.updated=nowTs(0);}
      pushEvent('task_started',{attempt:t.attempts},t.id,t.role);
    }
  }
  // 依赖满足：pending → ready；上游失败 → blocked
  for(const t of d.tasks){
    if(t.status==='pending'||t.status==='blocked'){
      const deps=(t.dependencies||[]).map(x=>d.tasks.find(z=>z.id===x)).filter(Boolean);
      if(!deps.length){ if(t.status==='pending')t.status='ready'; }
      else{
        const allOk=deps.every(z=>z.status==='succeeded');
        const anyFail=deps.some(z=>['failed','cancelled'].includes(z.status));
        if(allOk) t.status='ready';
        else if(anyFail) t.status='blocked';
      }
    }
  }
  // 统计递增
  const st=run.stats;
  st.llm_calls+=2; st.prompt_tokens+=Math.round(1500+Math.random()*2000);
  st.completion_tokens+=Math.round(400+Math.random()*800);
  st.estimated_cost_usd+=0.004+Math.random()*0.006;
  st.succeeded=d.tasks.filter(t=>t.status==='succeeded').length;
  run.updated_at=nowTs(0);

  const pct=st.estimated_cost_usd/run.budget_limit_usd*100;
  if(pct>=90 && !d._budgetWarned){ d._budgetWarned=true; toast('warning','预算预警',`已使用 ${Math.round(pct)}%`); pushEvent('budget_warning',{pct:Math.round(pct)}); }

  const unfinished=d.tasks.filter(t=>!['succeeded','failed','skipped','cancelled'].includes(t.status));
  if(unfinished.length===0){
    run.status='completed'; run.phase='delivery';
    // C-13：parseTs 非法时间返回 null，直接 .getTime() 会中断模拟器。
    const startedAt = parseTs(st.started_at);
    pushEvent('run_finished',{status:'completed',exit_code:0,
      duration_ms: startedAt ? Date.now()-startedAt.getTime() : 0});
    toast('success','运行完成','所有任务已通过，产物已就绪'); stopSim();
  }
  renderDashboard();
}

/* ============================================================
   导航
   ============================================================ */
/* ============================================================
   产物页
   ============================================================ */

function applyDemoState(state){
  stopSim();
  if(state==='empty'){
    // 无运行记录。此前直接 S.dash=null：renderDashboard / renderChrome 会提前
    // return，看板残留上一次运行的旧 DOM；而 8 个页面都直接读 S.dash.run，
    // 之后任意一次导航都抛 TypeError。改成「形状完整、内容为空」的数据，
    // 让各页既有的 || [] 兜底自然生效，并显式重渲染一次清掉旧内容。
    stopSim();
    S.dash={
      run:{ run_id:null, dir:null, status:'idle', phase:'planning', project_goal:'',
            stats:{}, provider:'—', model:'—', budget_limit_usd:0 },
      tasks:[], agents:deriveAgents([]), gates:deriveGates([]), events:[],
    };
    S.realMode=false; S.watchDir=null; S._eventKeys=null;
    renderDashboard();
    const bar=$('#run-status-bar');
    if(bar){
      bar.innerHTML='';
      bar.appendChild(h('div',{class:'banner info',style:'margin:0'},
        '没有运行记录。点击右上角「新建运行」开始第一次多智能体协作。'));
      bar.appendChild(h('button',{class:'btn btn-primary',style:'margin-top:8px',onclick:()=>location.reload()},'创建示例运行'));
    }
    return;
  }
  initMockData(); const d=S.dash; const run=d.run;
  if(state==='planning'){ run.status='planning'; run.phase='planning'; for(const t of d.tasks)t.status='pending'; d.gates.forEach(g=>g.status='pending'); }
  if(state==='budget_warn'){ run.stats.estimated_cost_usd=0.92; toast('warning','预算预警','已使用 92%'); }
  if(state==='budget_exhausted'){ run.status='running'; run.stats.estimated_cost_usd=1.05; pushEvent('budget_exhausted',{}); toast('error','预算耗尽','运行将优雅收尾'); }
  if(state==='paused'){ run.status='paused'; }
  if(state==='failed'){ run.status='failed'; const t=d.tasks.find(x=>x.id==='T-11'); t.status='failed'; t.error='集成测试 12 项断言失败'; pushEvent('error',{where:'T-11',message:'断言失败'},'T-11','backend_dev'); }
  if(state==='gates_failed'){ const g=d.gates[1]; g.status='failed'; g.checks={total:8,passed:5,failed:3};
    g.fail_reason='关键代码评审发现鉴权中间件漏洞'; g.impact='影响交付物 T-11、T-14'; g.suggestion='修复鉴权后重新提交'; g.return_task='T-07'; g.extra_cost='$0.08';
    pushEvent('gate_result',{layer:'llm',verdict:'reject',gate:'G2'}); }
  if(state==='completed'){ run.status='completed'; run.phase='delivery'; for(const t of d.tasks)t.status='succeeded'; d.gates.forEach(g=>{g.status='passed';g.checks={total:g.checks.total,passed:g.checks.total,failed:0};}); }
  renderDashboard();
  if(state==='running') startSim();
}

/* ============================================================
   Boot
   ============================================================ */
/* ============================================================
   真实数据接入：从 IPC 加载 run.json/tasks.json/ledger.jsonl
   ============================================================ */


export {
  applyDemoState,
  pushEvent,
  simTick,
  startSim,
  stopSim,
};
