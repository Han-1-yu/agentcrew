/**
 * 对话页控制器：累积实时正文、合并高频刷新，并按消息复用 DOM。
 * 消息结构由 lib/messages.js 聚合（含 guidance 附加层与输入图片），
 * 筛选/搜索走 lib/conversation.js 的纯函数，具体视图由 view.js 负责；
 * guidance 对账 / 输入图片 / 新对话空态见 ./assist.js。
 */
import { filterMessages } from '../../lib/conversation.js';
import { h } from '../../lib/dom.js';
import { simplifyGoal } from '../../lib/format.js';
import { buildMessages } from '../../lib/messages.js';
import { S } from '../../lib/store.js';

import { emptyState } from '../../ui/states.js';

import { openTaskDetail } from '../detail/panel.js';
import { openNewRunModal } from '../new-run/index.js';
import { openRewindDialog } from '../rewind/index.js';

import {
  appendGuidanceLocal,
  ensureGuidancePoll,
  loadRunImages,
  renderNewChatState,
} from './assist.js';
import { openArtifactPreview } from './preview.js';
import { createToolbar } from './toolbar.js';
import { messageKey, messageSignature, renderMessage } from './view.js';

function streams() { return S.convStreams || (S.convStreams = {}); }

let convRenderTimer = null;
let toolbar = null;
let resetScroll = false;   // 搜索/筛选变化：回到顶部看命中
let jumpBottom = false;    // 换 run：直接落到最新一条
//: 未读计数只属于当前 run：换 run 或滚到底就清零
let seen = { key: '', count: 0, unread: 0 };

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
  const query = S.convQuery || '';
  const imageSrc = (S.convImages && S.convImages.src) || null;
  const previous = new Map([...list.children].map(node => [node.dataset.messageKey, node]));
  const desired = msgs.map(m => {
    const key = messageKey(m);
    const stream = m.type === 'agent' ? streams()[m.taskId] : null;
    const signature = messageSignature(m, taskIndex, stream, expanded, query);
    const old = previous.get(key);
    const node = old && old.dataset.signature === signature ? old : renderMessage(m, taskIndex, {
      stream, expanded, query, onToggle: toggleConv, onOpenTask: openTaskDetail,
      onPreview: openArtifactPreview,
      onRedo: (taskId) => openRewindDialog(taskId),
      onEditResend: (goal) => openNewRunModal({ goal }),
      images: imageSrc,
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
  // 按「节点身份」清旧节点，不能按 key 判断：签名变化时同 key 的新节点已插入，
  // 再按 key 判断会把旧节点当成「仍在用」而留下，出现两条重复消息。
  const keep = new Set(desired.map(item => item.node));
  for (const node of previous.values()) {
    if (!keep.has(node)) node.remove();
  }
}

function mountConversationShell(container, run, hasMessages) {
  container.innerHTML = '';
  toolbar = null;
  container.dataset.convRun = `${run.dir || run.run_id || ''}|${run.project_goal || ''}`;
  container.appendChild(h('div', { class: 'page-head' },
    h('div', {},
      h('h2', {}, '对话'),
      h('div', { class: 'plan-goal', title: run.project_goal }, simplifyGoal(run.project_goal || '未命名项目')))));

  if (!hasMessages) {
    container.appendChild(emptyState('暂无执行记录',
      '运行开始后，目标、规划与各 Agent 的执行过程会以对话形式逐条呈现。',
      { label: '查看任务图', onclick: () => document.querySelector('[data-nav="dag"]')?.click() }));
    return null;
  }

  toolbar = createToolbar({ onChange: () => { resetScroll = true; refreshConversation(); } });
  container.appendChild(toolbar.node);

  // role=log 表意「追加式记录」；但流式输出会持续改 DOM，用 aria-live=off 防读屏刷屏
  const list = h('div', { class: 'conv-list', role: 'log', 'aria-live': 'off', 'aria-label': '执行消息记录' });
  const toLatest = h('button', {
    class: 'conv-to-latest hidden', type: 'button', 'aria-label': '回到最新消息',
    onclick: () => {
      list.scrollTop = list.scrollHeight;
      seen.unread = 0;
      toLatest.classList.add('hidden');
      toLatest.textContent = '↓ 回到最新';
    },
  }, '↓ 回到最新');
  list.addEventListener('scroll', () => {
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 120;
    if (nearBottom) seen.unread = 0;
    toLatest.classList.toggle('hidden', nearBottom);
  }, { passive: true });

  const noHit = h('div', { class: 'conv-no-hit hidden' },
    h('div', { class: 'conv-no-hit-title' }, '没有匹配的消息'),
    h('div', { class: 'conv-no-hit-detail' }, '换个关键词，或把类型筛选切回「全部」。'),
    h('button', {
      class: 'btn btn-sm', type: 'button',
      onclick: () => { S.convQuery = ''; S.convFilter = 'all'; resetScroll = true; refreshConversation(); },
    }, '清除筛选'));

  container.appendChild(h('div', { class: 'conv-wrap' }, list, toLatest, noHit));
  return { list, toLatest, noHit };
}

function renderConversation(container) {
  if (!container) return;
  // A1：新对话空态——隐藏旧 run 的内容，直接给模板 chips 与输入框
  if (S.newChat) { renderNewChatState(container); return; }
  if (!S.dash || !S.dash.run) {
    container.innerHTML = '';
    toolbar = null;
    delete container.dataset.convRun;
    container.appendChild(emptyState('暂无运行数据',
      '启动一次新运行后，执行过程会以对话流形式呈现在这里。',
      { label: '新建运行', onclick: () => document.getElementById('btn-new-run')?.click() }));
    return;
  }
  const run = S.dash.run;
  const { msgs, taskIndex } = buildMessages(S.dash.events, S.dash.tasks, run, {
    guidance: S.convGuidance || [],
    images: (S.convImages && S.convImages.dir === String(run.dir)) ? S.convImages.names : null,
  });
  const runKey = `${run.dir || run.run_id || ''}|${run.project_goal || ''}`;
  const isNewRun = container.dataset.convRun !== runKey;
  if (isNewRun) {
    // 换 run 时清掉搜索/筛选与未读，避免「新对话里还顶着上一轮的过滤条件」
    S.convQuery = '';
    S.convFilter = 'all';
    S.convGuidance = [];   // 指引附加层属于 run：换 run 即清，由对账重灌
    seen = { key: runKey, count: 0, unread: 0 };
    resetScroll = false;
    jumpBottom = true;
  }
  ensureGuidancePoll();
  void loadRunImages(run);
  let mounted = container.querySelector('.conv-list');
  if (isNewRun || !!mounted !== (msgs.length > 0)) {
    mounted = mountConversationShell(container, run, msgs.length > 0);
  } else if (mounted) {
    // 复用已有 DOM：把挂载时的三件套重新取回来，不要直接把元素当对象用
    mounted = {
      list: mounted,
      toLatest: container.querySelector('.conv-to-latest'),
      noHit: container.querySelector('.conv-no-hit'),
    };
  }
  if (!mounted || !mounted.list) return;
  const { list, toLatest, noHit } = mounted;

  const query = S.convQuery || '';
  const filter = S.convFilter || 'all';
  const visible = filterMessages(msgs, { query, filter }, taskIndex);

  const previousTop = list.scrollTop;
  const wasNearBottom = list.scrollHeight - previousTop - list.clientHeight < 120;
  reconcileMessages(list, visible, taskIndex);

  const running = run.status === 'running' || run.status === 'planning';
  if (seen.key !== runKey) seen = { key: runKey, count: 0, unread: 0 };
  if (msgs.length > seen.count && seen.count > 0 && !wasNearBottom && !resetScroll) {
    seen.unread += msgs.length - seen.count;
  }
  seen.count = msgs.length;

  if (jumpBottom) list.scrollTop = list.scrollHeight;
  else if (resetScroll) list.scrollTop = 0;
  else if (running && wasNearBottom) list.scrollTop = list.scrollHeight;
  else list.scrollTop = previousTop;
  jumpBottom = false;
  resetScroll = false;

  const filtering = !!String(query).trim() || filter !== 'all';
  noHit.classList.toggle('hidden', !(filtering && !visible.length && msgs.length > 0));
  toLatest.textContent = seen.unread > 0 ? `↓ ${seen.unread} 条新消息` : '↓ 回到最新';
  const nearBottomNow = list.scrollHeight - list.scrollTop - list.clientHeight < 120;
  toLatest.classList.toggle('hidden', nearBottomNow && seen.unread === 0);

  if (toolbar) toolbar.update({ run, msgs, taskIndex, visibleCount: visible.length });
}

function renderConversationPage(container) { renderConversation(container); }

function refreshConversation() {
  if (S.nav !== 'conversation') return;
  const ph = document.getElementById('placeholder-view');
  if (ph && !ph.classList.contains('hidden')) renderConversation(ph);
}

// 键盘：/ 聚焦搜索（不在输入框里时）；Esc 已由输入框自己处理
document.addEventListener('keydown', (e) => {
  if (S.nav !== 'conversation' || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key !== '/') return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  const input = document.getElementById('conv-search');
  if (!input) return;
  e.preventDefault();
  input.focus();
  input.select();
});

export {
  appendConversationDelta,
  refreshConversation,
  renderConversationPage,
};
