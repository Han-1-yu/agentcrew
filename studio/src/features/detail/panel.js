/**
 * 右侧详情抽屉：任务详情 / Agent 详情，Esc 或遮罩关闭。
 */
import { STATUS_META, roleMeta } from '../../contracts/enums.js';
import { agentStatusLabel } from '../../contracts/enums.js';

import { $, h } from '../../lib/dom.js';
import { fmtBytes, fmtClock, fmtCost, fmtDur, fmtTokens, trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

import { metaCell, renderAgents } from '../dashboard/agents.js';
import { renderDashboard } from '../dashboard/index.js';
import { buildTaskAgentControl } from './task-agent.js';
function openDetailPanel(node){
  const p=$('#detail-panel'); p.innerHTML=''; p.appendChild(node);
  $('#detail-mask').classList.remove('hidden'); $('#detail-mask').classList.add('show');
  p.classList.remove('hidden'); p.classList.add('show');
}
function closeDetailPanel(){
  const p=$('#detail-panel'); p.classList.remove('show'); $('#detail-mask').classList.remove('show');
  setTimeout(()=>{p.classList.add('hidden');$('#detail-mask').classList.add('hidden');},220);
}

function openTaskDetail(taskId){
  const t=S.dash.tasks.find(x=>x.id===taskId); if(!t)return;
  S.detail={kind:'task',id:taskId};
  const meta=STATUS_META[t.status]||STATUS_META.unknown;
  const rm=roleMeta(t.role);
  const up=(t.dependencies||[]).map(d=>S.dash.tasks.find(x=>x.id===d)).filter(Boolean);
  const down=S.dash.tasks.filter(x=>(x.dependencies||[]).includes(t.id));
  const res=t.result;
  openDetailPanel(h('div',{style:'display:flex;flex-direction:column;height:100%'},
    h('div',{class:'dp-head'},
      h('div',{class:'dp-title-row'},
        h('div',{},
          h('div',{class:'mono',style:`color:${meta.color};font-size:12px`},t.id),
          h('div',{class:'dp-title'},t.title)),
        h('button',{class:'btn btn-icon',style:'width:28px;height:28px','aria-label':'关闭',title:'关闭',onclick:closeDetailPanel},'✕'))),
    h('div',{class:'dp-body'},
      sec('状态', h('span',{class:'dp-tag',style:`color:${meta.color}`},meta.label),
        h('span',{class:'dp-tag'},`阶段：${phaseLabel(S.dash.run.phase)}`)),
      sec('负责 Agent',
        h('span',{class:'dp-tag'},h('span',{class:'dot',style:`background:${rm.color};display:inline-block;margin-right:4px`}),rm.full),
        buildTaskAgentControl(t)),
      t.brief?sec('一句话摘要',h('div',{class:'dp-text dim'},t.brief)):null,
      sec('描述',h('div',{class:'dp-text dim'},t.description||'（无描述）')),
      sec('上游依赖',up.length?h('div',{class:'dep-list'},up.map(u=>h('span',{class:'dp-dep-link',onclick:()=>openTaskDetail(u.id)},`${u.id} ${trunc(u.title,10)}`))):h('div',{class:'dp-text dim'},'无')),
      sec('下游依赖',down.length?h('div',{class:'dep-list'},down.map(u=>h('span',{class:'dp-dep-link',onclick:()=>openTaskDetail(u.id)},`${u.id} ${trunc(u.title,10)}`))):h('div',{class:'dp-text dim'},'无')),
      sec('执行配置',h('div',{class:'dp-meta-grid'},
        metaCell('尝试次数',`${t.attempts||1}/${t.max_attempts}`),
        metaCell('返工次数',`${t.rework_count||0}/${t.max_reworks}`),
        metaCell('超时',`${t.timeout_seconds||300}s`),
        metaCell('优先级',`P${t.priority||5}`))),
      sec('输出产物', res&&res.artifacts&&res.artifacts.length
        ? h('div',{},res.artifacts.map(a=>h('div',{class:'dp-text',style:'margin:2px 0'},`📄 ${a.path} · ${fmtBytes(a.size_bytes)}`)))
        : h('div',{class:'dp-text dim'},'待生成')),
      t.error?sec('最近错误',h('div',{class:'dp-text',style:'color:var(--error)'},t.error)):null,
      res&&(res.tokens_used||res.duration_ms)?sec('执行统计',h('div',{class:'dp-meta-grid'},
        metaCell('Token',fmtTokens(res.tokens_used||0)),
        metaCell('耗时',fmtDur(res.duration_ms||0)),
        metaCell('费用',res.cost_usd?`$${res.cost_usd.toFixed(4)}`:'—'),
        metaCell('模型',res.model||'—'))):null,
      t.reviews&&t.reviews.length?sec('门禁评审',h('div',{class:'dp-reviews'},
        t.reviews.map(r=>h('div',{class:'dp-review-item'},
          h('span',{class:`dp-review-verdict ${r.verdict}`},r.verdict),
          h('span',{class:'dp-review-layer'},r.layer||''),
          h('span',{class:'dp-review-text'},trunc(r.summary||(r.issues&&r.issues.join('; '))||'',60)))))):null,
      t.status_history&&t.status_history.length?sec('状态历史',h('div',{class:'dp-status-history'},
        t.status_history.slice(-8).map(sh=>h('div',{class:'dp-sh-item'},
          h('span',{class:'dp-sh-status'},sh.status||''),
          h('span',{class:'dp-sh-time'},fmtClock(sh.at||sh.time||sh.timestamp||'')))))):null,
      sec('运行日志',h('div',{class:'dp-log'},
        h('div',{},`$ running ${t.id}`),
        h('div',{},`> role=${rm.full} model=${S.dash.run.model}`),
        res&&res.summary?h('div',{},`> ${trunc(res.summary,80)}`):h('div',{},'> loading context…'),
        t.status==='failed'?h('div',{class:'err'},`✗ ${t.error||'失败'}`):h('div',{},'> done')))),
    h('div',{class:'dp-actions'},
      S.realMode
        ? h('div',{class:'dp-text dim',style:'font-size:11px'},'重试 / 跳过需通过 CLI 或 API；执行体可在上方「负责 Agent」里设置')
        : h('div',{style:'display:flex;gap:6px;flex-wrap:wrap'},
            h('button',{class:'btn btn-sm',onclick:async()=>{
              if (S.realMode && S.dash.run?.dir) { try { await window.studio.taskAction(S.dash.run.dir, t.id, 'retry'); toast('success','已重试',t.id); } catch(e){ toast('error','重试失败',String(e.message||e)); } }
              else { t.status='ready'; t.attempts=(t.attempts||0)+1; toast('info','重试任务',t.id); }
              renderDashboard();
            }},'重试任务'),
            h('button',{class:'btn btn-sm',onclick:async()=>{
              if (S.realMode && S.dash.run?.dir) { try { await window.studio.taskAction(S.dash.run.dir, t.id, 'skip'); toast('success','已跳过',t.id); } catch(e){ toast('error','跳过失败',String(e.message||e)); } }
              else { t.status='skipped'; toast('info','已跳过',t.id); }
              renderDashboard();
            }},'跳过任务'),
            h('button',{class:'btn btn-sm',onclick:async()=>{
              if (S.realMode && S.dash.run?.dir) { try { await window.studio.taskAction(S.dash.run.dir, t.id, 'manual'); toast('success','已标记',t.id+' 人工处理'); } catch(e){ toast('error','标记失败',String(e.message||e)); } }
              else { t.status='review'; toast('info','人工处理',t.id+' 标记为人工处理'); }
              renderDashboard();
            }},'人工处理'),
            h('button',{class:'btn btn-sm btn-primary',onclick:()=>toast('info','恢复运行','从 '+t.id+' 恢复')},'从此恢复')))));
  renderAgents();
}
function openAgentDetail(roleId){
  const a=S.dash.agents[roleId]; if(!a)return;
  S.detail={kind:'agent',id:roleId};
  const rm=roleMeta(roleId);
  const myTasks=S.dash.tasks.filter(t=>t.role===roleId);
  openDetailPanel(h('div',{style:'display:flex;flex-direction:column;height:100%'},
    h('div',{class:'dp-head'},h('div',{class:'dp-title-row'},
      h('div',{},h('div',{class:'mono',style:'color:var(--text-ter);font-size:12px'},'AGENT'),h('div',{class:'dp-title'},rm.full)),
      h('button',{class:'btn btn-icon',style:'width:28px;height:28px','aria-label':'关闭',title:'关闭',onclick:closeDetailPanel},'✕'))),
    h('div',{class:'dp-body'},
      sec('状态',h('span',{class:'dp-tag'},agentStatusLabel(a.status)),h('span',{class:'dp-tag'},a.model)),
      sec('当前任务',h('div',{class:'dp-text'},a.task)),
      sec('最近动作',h('div',{class:'dp-text dim'},a.action)),
      sec('统计',h('div',{class:'dp-meta-grid'},
        metaCell('已完成',String(a.done)),metaCell('失败/重试',String(a.fail)),
        metaCell('Token',fmtTokens(a.tokens)),metaCell('费用',fmtCost(a.cost)))),
      sec('负责任务列表', myTasks.length?h('div',{style:'display:flex;flex-direction:column;gap:4px'},
        myTasks.map(t=>{const m=STATUS_META[t.status]||STATUS_META.unknown;
          return h('div',{class:'dag-task-row',onclick:()=>openTaskDetail(t.id)},
            h('span',{class:'dot',style:`background:${m.color}`}),
            h('span',{class:'mono',style:'color:var(--text-ter)'},t.id),
            h('span',{},trunc(t.title,16)),
            h('span',{style:`margin-left:auto;font-size:11px;color:${m.color}`},m.label));}))
        :h('div',{class:'dp-text dim'},'暂无任务'))),
    h('div',{class:'dp-actions'},
      h('button',{class:'btn btn-sm',onclick:()=>toast('info','查看事件',rm.full+' 的事件')},'查看事件'),
      h('button',{class:'btn btn-sm',onclick:()=>toast('info','查看产物',rm.full+' 的产物')},'查看产物'))));
  renderAgents();
}
function phaseLabel(p){ return {planning:'规划',execution:'执行',delivery:'交付'}[p]||p; }
function sec(title,...kids){ return h('div',{class:'dp-section'},h('h4',{},title),...kids); }

/* ============================================================
   模拟实时更新
   ============================================================ */

export {
  closeDetailPanel,
  openAgentDetail,
  openDetailPanel,
  openTaskDetail,
  phaseLabel,
  sec,
};
