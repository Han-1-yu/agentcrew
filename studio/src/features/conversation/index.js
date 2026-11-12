/**
 * 对话式执行流：把 run 的 ledger 事件重建成 WorkBuddy 式消息流。
 *
 * 用户目标 = 第一条消息（右对齐气泡）；PM 规划 = 系统消息（居中）；每个 Agent
 * 任务 = 一个左对齐气泡，内部按时间聚合该任务的说明 / 工具调用 / 门禁评审 /
 * 产物 / 流式正文。工具调用与评审默认折叠、可点开看细节；产物卡片可点击跳详情。
 *
 * 数据源是 ``S.dash.events``（ledger 事件，历史回溯）+ ``S.convStreams``（SSE
 * 的 ``agent_stream_delta`` 流式正文，运行中实时累加、刷新不回溯）。正文是模型
 * 输出，一律用文本节点 / pre 写入，绝不拼 innerHTML。
 */
import { h } from '../../lib/dom.js';
import { STATUS_META, roleMeta } from '../../contracts/enums.js';
import { fmtBytes, fmtClock, fmtDur, simplifyGoal, trunc } from '../../lib/format.js';
import { renderMarkdown } from '../../lib/markdown.js';
import { buildMessages } from '../../lib/messages.js';
import { S } from '../../lib/store.js';

import { emptyState } from '../../ui/states.js';
import { openTaskDetail } from '../detail/panel.js';

/* 流式正文累积：{ taskId: { role, text } }，仅运行中由 SSE 实时累加。 */
function streams() { return S.convStreams || (S.convStreams = {}); }

/* 流式正文逐 token 高频到达，渲染合并到 100ms 内一次，避免每帧全量重建消息流。 */
let convRenderTimer = null;
function scheduleConvRender() {
  if (convRenderTimer) return;
  convRenderTimer = setTimeout(() => {
    convRenderTimer = null;
    refreshConversation();
  }, 100);
}

function appendConversationDelta(data) {
  const tid = String((data && data.task_id) || '');
  if (!tid) return;
  const st = streams();
  const cur = st[tid] || (st[tid] = { role: '', text: '' });
  if (data.role) cur.role = data.role;
  cur.text += String(data.delta == null ? '' : data.delta);
  if (cur.text.length > 80000) cur.text = cur.text.slice(-80000);
  scheduleConvRender();
}

function toggleConv(key) {
  const set = S.convExpanded || (S.convExpanded = new Set());
  if (set.has(key)) set.delete(key); else set.add(key);
  refreshConversation();
}

/* 消息聚合逻辑抽到 lib/messages.js（纯函数，便于单测），这里只负责渲染。 */

function renderUser(m) {
  return h('div', { class: 'conv-msg conv-user' },
    h('div', { class: 'conv-user-bubble' },
      h('div', { class: 'conv-user-goal' }, m.goal || '（未命名目标）'),
      h('div', { class: 'conv-ts' }, fmtClock(m.ts))));
}

function renderSystem(m) {
  return h('div', { class: 'conv-msg conv-system' },
    h('div', { class: `conv-system-bubble${m.kind ? ' conv-' + m.kind : ''}` }, m.text));
}

function renderNote(it, key) {
  const text = it.text || '';
  const MAX = 400;
  if (text.length <= MAX) return h('div', { class: 'conv-note', innerHTML: renderMarkdown(text) });
  const expanded = S.convExpanded && S.convExpanded.has(key);
  if (expanded) {
    return h('div', { class: 'conv-note', innerHTML: renderMarkdown(text) },
      h('button', { class: 'conv-note-toggle', type: 'button', onclick: (ev) => { ev.stopPropagation(); toggleConv(key); } }, ' 收起'));
  }
  // 折叠态用纯文本截断（避免截断破坏 markdown 标记），展开才渲染
  return h('div', { class: 'conv-note' }, text.slice(0, MAX) + '…',
    h('button', { class: 'conv-note-toggle', type: 'button', onclick: (ev) => { ev.stopPropagation(); toggleConv(key); } }, ' 展开全文'));
}

function renderTool(it, key, expanded) {
  const ok = it.ok === false ? 'conv-tool-fail' : 'conv-tool-ok';
  return h('div', { class: 'conv-item conv-tool' },
    h('button', { class: 'conv-item-head', type: 'button', 'aria-expanded': String(expanded), onclick: () => toggleConv(key) },
      h('span', { class: 'conv-item-chev' }, expanded ? '▾' : '▸'),
      h('span', { class: `conv-tool-dot ${ok}` }),
      h('span', { class: 'conv-tool-name' }, it.tool || '工具'),
      h('span', { class: 'conv-item-dim' }, it.dur != null ? fmtDur(it.dur) : ''),
      h('span', { class: 'conv-item-dim', style: 'margin-left:auto' }, expanded ? '收起' : '详情')),
    expanded ? h('div', { class: 'conv-item-body' },
      h('pre', {}, JSON.stringify(it.args == null ? {} : it.args, null, 2))) : null);
}

function renderGate(it, key, expanded) {
  const label = { static: '静态校验', llm: 'LLM 评审', human: '人工确认', supervisor: '主管复核' }[it.layer] || it.layer || '门禁';
  const verdict = it.verdict === 'approve' ? '通过' : (it.verdict === 'reject' ? '驳回' : String(it.verdict || ''));
  return h('div', { class: 'conv-item conv-gate' },
    h('button', { class: 'conv-item-head', type: 'button', 'aria-expanded': String(expanded), onclick: () => toggleConv(key) },
      h('span', { class: 'conv-item-chev' }, expanded ? '▾' : '▸'),
      h('span', { class: `conv-gate-dot ${it.verdict === 'reject' ? 'conv-gate-reject' : ''}` }),
      h('span', { class: 'conv-gate-name' }, `${label} · ${verdict}`),
      h('span', { class: 'conv-item-dim', style: 'margin-left:auto' }, expanded ? '收起' : '详情')),
    expanded ? h('div', { class: 'conv-item-body' },
      it.score != null ? h('div', { class: 'conv-item-dim' }, `评分 ${it.score}`) : null,
      (it.issues || []).map((x) => h('div', { class: 'conv-gate-issue' }, `· ${x}`))) : null);
}

function renderArtifact(it) {
  return h('div', { class: 'conv-item conv-artifact', onclick: () => openTaskDetail(it.taskId || '') },
    h('span', { class: 'conv-artifact-icon' }, '📄'),
    h('span', { class: 'conv-artifact-path' }, it.path || ''),
    h('span', { class: 'conv-item-dim', style: 'margin-left:auto' }, it.size != null ? fmtBytes(it.size) : ''));
}

function renderAgent(m, taskIndex) {
  const rm = roleMeta(m.role);
  const t = taskIndex[m.taskId];
  const status = t && t.status;
  const sm = status ? STATUS_META[status] : null;
  const stream = streams()[m.taskId];

  const body = h('div', { class: 'conv-agent-body' },
    h('div', { class: 'conv-agent-head' },
      h('span', { class: 'conv-agent-role', style: `color:${rm.color}` }, rm.full),
      h('span', { class: 'conv-agent-task', onclick: () => openTaskDetail(m.taskId) },
        m.taskId + (t && t.title ? ' · ' + trunc(t.title, 24) : '')),
      sm ? h('span', { class: `status-chip status-${status}`, style: 'margin-left:auto' }, sm.label) : null));

  (m.items || []).forEach((it, i) => {
    const key = `${it.kind}:${m.taskId}:${i}`;
    const expanded = S.convExpanded && S.convExpanded.has(key);
    if (it.kind === 'note') body.appendChild(renderNote(it, key));
    else if (it.kind === 'tool') body.appendChild(renderTool(it, key, expanded));
    else if (it.kind === 'gate') body.appendChild(renderGate(it, key, expanded));
    else if (it.kind === 'artifact') { const a = renderArtifact(it); a.addEventListener('click', (ev) => { ev.stopPropagation(); openTaskDetail(m.taskId); }); body.appendChild(a); }
  });

  if (stream && stream.text) {
    body.appendChild(h('div', { class: 'conv-stream' },
      h('div', { class: 'conv-stream-label' }, '正在输出…'),
      h('div', { class: 'conv-stream-body', innerHTML: renderMarkdown(stream.text) })));
  }

  return h('div', { class: 'conv-msg conv-agent' },
    h('span', { class: 'conv-agent-avatar', style: `background:${rm.color}` }, rm.label),
    body);
}

function renderConversation(container) {
  if (!container) return;
  container.innerHTML = '';
  if (!S.dash || !S.dash.run) {
    container.appendChild(emptyState('暂无运行数据',
      '启动一次新运行后，执行过程会以对话流形式呈现在这里。'));
    return;
  }
  const { msgs, taskIndex } = buildMessages(S.dash.events, S.dash.tasks, S.dash.run);
  const run = S.dash.run;

  container.appendChild(h('div', { class: 'page-head' },
    h('div', {},
      h('h2', {}, '对话'),
      h('div', { class: 'plan-goal', title: run.project_goal }, simplifyGoal(run.project_goal || '未命名项目')))));

  if (!msgs.length) {
    container.appendChild(emptyState('暂无执行记录',
      '运行开始后，目标、规划与各 Agent 的执行过程会以对话形式逐条呈现。'));
    return;
  }

  const wrap = h('div', { class: 'conv-wrap' });
  const list = h('div', { class: 'conv-list', 'aria-live': 'polite' });
  for (const m of msgs) {
    if (m.type === 'user') list.appendChild(renderUser(m));
    else if (m.type === 'system') list.appendChild(renderSystem(m));
    else list.appendChild(renderAgent(m, taskIndex));
  }
  const toLatest = h('button', { class: 'conv-to-latest hidden', onclick: () => { list.scrollTop = list.scrollHeight; } }, '↓ 回到最新');
  list.addEventListener('scroll', () => {
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 120;
    toLatest.classList.toggle('hidden', nearBottom);
  });
  wrap.appendChild(list);
  wrap.appendChild(toLatest);
  container.appendChild(wrap);
  // 运行中跟随最新（仅在贴近底部时，避免打断用户回看历史）
  const runStatus = run.status;
  if (runStatus === 'running' || runStatus === 'planning') {
    requestAnimationFrame(() => {
      const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 120;
      if (nearBottom) list.scrollTop = list.scrollHeight;
      else toLatest.classList.remove('hidden');
    });
  }
}

/* 供 router 调用（switchNav → renderConversationPage(ph)） */
function renderConversationPage(container) { renderConversation(container); }

/* 实时刷新：对话页可见时重渲染到占位区。 */
function refreshConversation() {
  if (S.nav !== 'conversation') return;
  const ph = document.getElementById('placeholder-view');
  if (ph && !ph.classList.contains('hidden')) renderConversation(ph);
}

export {
  appendConversationDelta,
  refreshConversation,
  renderConversationPage,
};
