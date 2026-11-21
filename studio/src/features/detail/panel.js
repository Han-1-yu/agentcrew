/**
 * 右侧详情抽屉：任务详情 / Agent 详情，Esc 或遮罩关闭。
 */
import { STATUS_META, roleMeta } from '../../contracts/enums.js';
import { agentStatusLabel } from '../../contracts/enums.js';

import { $, h } from '../../lib/dom.js';
import { artifactIcon } from '../../lib/artifacts.js';
import { fmtBytes, fmtClock, fmtCost, fmtDur, fmtTokens, trunc } from '../../lib/format.js';
import { S } from '../../lib/store.js';
import { summarizeTaskArtifacts } from '../../lib/task-artifacts.js';

import { toast } from '../../ui/toast.js';

import { metaCell, renderAgents } from '../dashboard/agents.js';
import { renderDashboard } from '../dashboard/index.js';
import { buildTaskAgentControl } from './task-agent.js';
import { openRewindDialog } from '../rewind/index.js';
let detailHideTimer = null;
let detailReturnFocus = null;
const DETAIL_FOCUSABLE = 'input:not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

function openDetailPanel(node){
  const p=$('#detail-panel');
  const mask=$('#detail-mask');
  if (detailHideTimer) { clearTimeout(detailHideTimer); detailHideTimer = null; }
  if (p.classList.contains('hidden')) detailReturnFocus = document.activeElement;
  p.innerHTML=''; p.appendChild(node);
  p.tabIndex = -1;
  p.setAttribute('aria-hidden', 'false');
  mask.classList.remove('hidden'); mask.classList.add('show');
  p.classList.remove('hidden'); p.classList.add('show');
  requestAnimationFrame(() => {
    const target = p.querySelector('[data-autofocus]') || p.querySelector(DETAIL_FOCUSABLE) || p;
    try { target.focus({ preventScroll: true }); } catch { p.focus({ preventScroll: true }); }
  });
}
function closeDetailPanel(){
  const p=$('#detail-panel');
  if (!p || p.classList.contains('hidden')) return;
  p.classList.remove('show'); $('#detail-mask').classList.remove('show');
  p.setAttribute('aria-hidden', 'true');
  if (detailHideTimer) clearTimeout(detailHideTimer);
  detailHideTimer = setTimeout(()=>{
    p.classList.add('hidden'); $('#detail-mask').classList.add('hidden');
    detailHideTimer = null;
    if (detailReturnFocus && detailReturnFocus.isConnected) {
      try { detailReturnFocus.focus({ preventScroll: true }); } catch { /* 触发器已不可聚焦 */ }
    }
    detailReturnFocus = null;
  },220);
}
$('#detail-panel').addEventListener('keydown', (event) => {
  if (event.key !== 'Tab') return;
  const p = event.currentTarget;
  const items = [...p.querySelectorAll(DETAIL_FOCUSABLE)];
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});

/**
 * F2：输入 / 输出产物摘要。异步拿到 MANIFEST entries 后，与任务和依赖一起折成
 * 可读清单；只列路径与大小，不做预览（预览在产物页）。
 */
function renderArtifactSummary(box, task, tasks, entries) {
  const sum = summarizeTaskArtifacts(task, tasks, entries);
  const outLines = [];
  if (sum.outputs.files.length) {
    for (const f of sum.outputs.files.slice(0, 6)) {
      outLines.push(h('div', { class: 'dp-text dp-art-line' },
        h('span', { class: 'dp-art-ico', innerHTML: artifactIcon('doc') }),
        ` ${f.path}${f.sizeBytes != null ? ' · ' + fmtBytes(f.sizeBytes) : ''}`));
    }
    if (sum.outputs.files.length > 6) {
      outLines.push(h('div', { class: 'dp-text dim' }, `…另有 ${sum.outputs.files.length - 6} 个`));
    }
  } else {
    outLines.push(h('div', { class: 'dp-text dim' },
      sum.pending ? '待产出（任务还没完成）' : '没有记录到产物'));
  }
  const inLines = [];
  if (sum.inputs.from.length) {
    inLines.push(h('div', { class: 'dp-art-group' },
      `输入 · ${sum.inputs.files.length} 个文件 · 来自 ${sum.inputs.from.map((x) => x.id).join('、')}`));
    for (const f of sum.inputs.files.slice(0, 4)) {
      inLines.push(h('div', { class: 'dp-text dp-art-line' },
        h('span', { class: 'dp-art-ico', innerHTML: artifactIcon('doc') }), ` ${f.path}`));
    }
    if (sum.inputs.files.length > 4) {
      inLines.push(h('div', { class: 'dp-text dim' }, `…另有 ${sum.inputs.files.length - 4} 个`));
    }
  } else {
    inLines.push(h('div', { class: 'dp-art-group' }, '输入 · 无上游任务'));
  }
  box.replaceChildren(
    h('div', { class: 'dp-art-group' },
      `输出 · ${sum.outputs.files.length} 个文件${sum.outputs.source === 'manifest' ? '（MANIFEST）' : ''}`),
    ...outLines,
    ...inLines,
    sum.missing.length
      ? h('div', { class: 'dp-text', style: 'color:var(--warn-text)' },
        `契约要求未产出：${sum.missing.join('、')}`)
      : null);
}

function openTaskDetail(taskId){
  const t=S.dash.tasks.find(x=>x.id===taskId); if(!t)return;
  S.detail={kind:'task',id:taskId};
  const meta=STATUS_META[t.status]||STATUS_META.unknown;
  const rm=roleMeta(t.role);
  const up=(t.dependencies||[]).map(d=>S.dash.tasks.find(x=>x.id===d)).filter(Boolean);
  const down=S.dash.tasks.filter(x=>(x.dependencies||[]).includes(t.id));
  const res=t.result;
  const artBox=h('div',{class:'dp-art-sum'},h('div',{class:'dp-text dim'},'加载产物清单…'));
  openDetailPanel(h('div',{style:'display:flex;flex-direction:column;height:100%'},
    h('div',{class:'dp-head'},
      h('div',{class:'dp-title-row'},
        h('div',{},
          h('div',{class:'mono',style:`color:${meta.color};font-size:12px`},t.id),
          h('div',{class:'dp-title'},t.title)),
        h('button',{class:'btn btn-icon',id:'detail-close',style:'width:28px;height:28px','aria-label':'关闭',title:'关闭',onclick:closeDetailPanel},'✕'))),
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
      sec('输入 / 输出产物', artBox),
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
        ? h('div',{class:'dp-real-actions'},
            h('div',{class:'dp-text dim',style:'font-size:11px'},'重试 / 跳过需通过 CLI 或 API；执行体可在上方「负责 Agent」里设置'),
            h('button',{class:'btn btn-sm',type:'button',title:'R62：回滚到该任务并重跑（需连接后端 API）',onclick:()=>openRewindDialog(t.id)},'从此恢复'))
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
            h('button',{class:'btn btn-sm',type:'button',title:'R62：回滚到该任务并重跑（需连接后端 API）',onclick:()=>openRewindDialog(t.id)},'从此恢复')))));

  // F2：产物清单走主进程 IPC（MANIFEST.json）；mock / 未连后端时退回任务自带字段。
  const allTasks = S.dash.tasks || [];
  const fill = (entries) => { if (artBox.isConnected) renderArtifactSummary(artBox, t, allTasks, entries); };
  const dir = S.dash.run && S.dash.run.dir;
  if (S.realMode && dir && window.studio.listArtifacts) {
    window.studio.listArtifacts(dir)
      .then((payload) => fill((payload && payload.entries) || []))
      .catch(() => fill([]));
  } else {
    fill([]);
  }
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
      h('button',{class:'btn btn-icon',id:'detail-close',style:'width:28px;height:28px','aria-label':'关闭',title:'关闭',onclick:closeDetailPanel},'✕'))),
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
      h('button',{class:'btn btn-sm',type:'button',onclick:()=>{ closeDetailPanel(); document.querySelector('[data-nav="events"]')?.click(); }},'查看事件'),
      h('button',{class:'btn btn-sm',type:'button',onclick:()=>{ closeDetailPanel(); document.querySelector('[data-nav="artifacts"]')?.click(); }},'查看产物'))));
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
