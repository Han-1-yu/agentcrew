/**
 * 对话页控制器：累积实时正文、合并高频刷新，并按消息复用 DOM。
 * 消息结构由 lib/messages.js 聚合，具体视图由 view.js 负责。
 */
import { h } from '../../lib/dom.js';
import { simplifyGoal } from '../../lib/format.js';
import { buildMessages } from '../../lib/messages.js';
import { S } from '../../lib/store.js';

import { emptyState } from '../../ui/states.js';
import { openTaskDetail } from '../detail/panel.js';
import { messageKey, messageSignature, renderMessage } from './view.js';

function streams() { return S.convStreams || (S.convStreams = {}); }

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

function reconcileMessages(list, msgs, taskIndex) {
  const expanded = S.convExpanded || new Set();
  const previous = new Map([...list.children].map(node => [node.dataset.messageKey, node]));
  const desired = msgs.map(m => {
    const key = messageKey(m);
    const stream = m.type === 'agent' ? streams()[m.taskId] : null;
    const signature = messageSignature(m, taskIndex, stream, expanded);
    const old = previous.get(key);
    const node = old && old.dataset.signature === signature ? old : renderMessage(m, taskIndex, {
      stream, expanded, onToggle: toggleConv, onOpenTask: openTaskDetail,
    });
    node.dataset.signature = signature;
    return { key, node };
  });

  desired.forEach(({ key, node }, index) => {
    const current = list.children[index];
    if (current === node) return;
    if (current && current.dataset.messageKey === key) list.replaceChild(node, current);
    else list.insertBefore(node, current || null);
  });
  for (const [key, node] of previous) {
    if (!desired.some(item => item.key === key)) node.remove();
  }
}

function mountConversationShell(container, run, hasMessages) {
  container.innerHTML = '';
  container.dataset.convRun = `${run.dir || run.run_id || ''}|${run.project_goal || ''}`;
  container.appendChild(h('div', { class: 'page-head' },
    h('div', {},
      h('h2', {}, '对话'),
      h('div', { class: 'plan-goal', title: run.project_goal }, simplifyGoal(run.project_goal || '未命名项目')))));
  if (!hasMessages) {
    container.appendChild(emptyState('暂无执行记录',
      '运行开始后，目标、规划与各 Agent 的执行过程会以对话形式逐条呈现。'));
    return null;
  }
  const list = h('div', { class: 'conv-list', 'aria-label': '执行消息记录' });
  const toLatest = h('button', {
    class: 'conv-to-latest hidden', type: 'button', 'aria-label': '回到最新消息',
    onclick: () => { list.scrollTop = list.scrollHeight; toLatest.classList.add('hidden'); },
  }, '↓ 回到最新');
  list.addEventListener('scroll', () => {
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 120;
    toLatest.classList.toggle('hidden', nearBottom);
  }, { passive: true });
  container.appendChild(h('div', { class: 'conv-wrap' }, list, toLatest));
  return list;
}

function renderConversation(container) {
  if (!container) return;
  if (!S.dash || !S.dash.run) {
    container.innerHTML = '';
    delete container.dataset.convRun;
    container.appendChild(emptyState('暂无运行数据',
      '启动一次新运行后，执行过程会以对话流形式呈现在这里。'));
    return;
  }
  const run = S.dash.run;
  const { msgs, taskIndex } = buildMessages(S.dash.events, S.dash.tasks, run);
  const runKey = `${run.dir || run.run_id || ''}|${run.project_goal || ''}`;
  let list = container.querySelector('.conv-list');
  if (container.dataset.convRun !== runKey || !!list !== !!msgs.length) {
    list = mountConversationShell(container, run, msgs.length > 0);
  }
  if (!list) return;

  const previousTop = list.scrollTop;
  const wasNearBottom = list.scrollHeight - previousTop - list.clientHeight < 120;
  reconcileMessages(list, msgs, taskIndex);
  const running = run.status === 'running' || run.status === 'planning';
  if (running && wasNearBottom) list.scrollTop = list.scrollHeight;
  else list.scrollTop = previousTop;
  const toLatest = container.querySelector('.conv-to-latest');
  if (toLatest) toLatest.classList.toggle('hidden', wasNearBottom);
}

function renderConversationPage(container) { renderConversation(container); }

function refreshConversation() {
  if (S.nav !== 'conversation') return;
  const ph = document.getElementById('placeholder-view');
  if (ph && !ph.classList.contains('hidden')) renderConversation(ph);
}

export { appendConversationDelta, refreshConversation, renderConversationPage };
