/* 对话消息视图：纯渲染与消息签名，不包含实时状态和调度。 */
import { STATUS_META, roleMeta } from '../../contracts/enums.js';
import { copyText } from '../../lib/clipboard.js';
import { agentMessageText } from '../../lib/conversation.js';
import { h, s } from '../../lib/dom.js';
import { fmtClock, fmtDur, trunc } from '../../lib/format.js';
import { renderMarkdown } from '../../lib/markdown.js';

import { toast } from '../../ui/toast.js';

import { decorateCodeBlocks, renderArtifact, renderGate, renderNote, renderTool } from './items.js';

function messageKey(m) {
  if (m.type === 'agent') return `agent:${m.taskId}`;
  if (m.subtype === 'guidance') return `user:guidance:${m.gid || m.order}`;
  return `${m.type}:${m.order}`;
}

/* DOM 是否可复用只由实际展示内容决定；已完成消息不会因其他任务输出而重绘。
   query 进签名：搜索词变化时原地重建节点，才能把 <mark> 刷成新结果。 */
function messageSignature(m, taskIndex, stream, expanded, query) {
  const task = taskIndex[m.taskId];
  const expandedForTask = m.type === 'agent'
    ? [...expanded].filter(key => key.includes(`:${m.taskId}:`)).sort() : [];
  const taskBits = task ? [task.status, task.title, task.rework_count || 0] : null;
  return JSON.stringify([m, taskBits, stream || null, expandedForTask, query || '']);
}

/* 把搜索词包成 <mark>。只碰文本节点，跳过代码块与操作栏，避免破坏高亮结构。 */
function applyHighlight(root, query) {
  const needle = String(query || '').trim().toLowerCase();
  if (!root || !needle) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent) return NodeFilter.FILTER_REJECT;
      if (/^(PRE|CODE|SCRIPT|STYLE|MARK)$/.test(parent.tagName)) return NodeFilter.FILTER_REJECT;
      if (parent.closest('.conv-actions')) return NodeFilter.FILTER_REJECT;
      const text = node.nodeValue || '';
      return text.toLowerCase().includes(needle) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const hits = [];
  while (walker.nextNode()) hits.push(walker.currentNode);
  for (const node of hits) {
    const text = node.nodeValue || '';
    const lower = text.toLowerCase();
    const frag = document.createDocumentFragment();
    let at = 0;
    let found = lower.indexOf(needle, at);
    while (found >= 0) {
      if (found > at) frag.appendChild(document.createTextNode(text.slice(at, found)));
      const mark = document.createElement('mark');
      mark.className = 'conv-hit';
      mark.textContent = text.slice(found, found + needle.length);
      frag.appendChild(mark);
      at = found + needle.length;
      found = lower.indexOf(needle, at);
    }
    if (at < text.length) frag.appendChild(document.createTextNode(text.slice(at)));
    node.parentNode.replaceChild(frag, node);
  }
}

function iconCopy() {
  return s('svg', { width: '13', height: '13', viewBox: '0 0 16 16', fill: 'none', 'aria-hidden': 'true' },
    s('rect', { x: '5.5', y: '5.5', width: '8', height: '8', rx: '2', stroke: 'currentColor', 'stroke-width': '1.2' }),
    s('path', { d: 'M10.5 5.5v-1a2 2 0 0 0-2-2h-5a2 2 0 0 0-2 2v5a2 2 0 0 0 2 2h1', stroke: 'currentColor', 'stroke-width': '1.2', 'stroke-linecap': 'round' }));
}

function iconOpen() {
  return s('svg', { width: '13', height: '13', viewBox: '0 0 16 16', fill: 'none', 'aria-hidden': 'true' },
    s('path', { d: 'M6.5 3.5h6v6M12.5 3.5 7 9', stroke: 'currentColor', 'stroke-width': '1.2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }),
    s('path', { d: 'M12 9.5v3a1.5 1.5 0 0 1-1.5 1.5h-6A1.5 1.5 0 0 1 3 12.5v-6A1.5 1.5 0 0 1 4.5 5h3', stroke: 'currentColor', 'stroke-width': '1.2', 'stroke-linecap': 'round' }));
}

/* A4/K1：从此重做（rewind dry-run → resume）；A4：编辑后重发（预填新建运行）。 */
function iconRedo() {
  return s('svg', { width: '13', height: '13', viewBox: '0 0 16 16', fill: 'none', 'aria-hidden': 'true' },
    s('path', { d: 'M13 8a5 5 0 1 1-1.9-3.9', stroke: 'currentColor', 'stroke-width': '1.3', 'stroke-linecap': 'round' }),
    s('path', { d: 'M13 2.6V5h-2.4', stroke: 'currentColor', 'stroke-width': '1.3', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
}

function iconEdit() {
  return s('svg', { width: '13', height: '13', viewBox: '0 0 16 16', fill: 'none', 'aria-hidden': 'true' },
    s('path', { d: 'M11.3 2.9a1.6 1.6 0 0 1 2.3 2.3l-7 7-3 0.7 0.7-3 7-7Z', stroke: 'currentColor', 'stroke-width': '1.2', 'stroke-linejoin': 'round' }));
}

async function copyMessage(text, label) {
  const ok = await copyText(text);
  toast(ok ? 'success' : 'error', ok ? '已复制' : '复制失败', ok ? label : '剪贴板不可用');
}

function actionBar(actions) {
  const bar = h('div', { class: 'conv-actions' });
  for (const a of actions) {
    if (!a) continue;
    bar.appendChild(h('button', {
      class: 'conv-act', type: 'button', title: a.title, 'aria-label': a.title, onclick: a.onclick,
    }, a.icon || null, a.label ? h('span', {}, a.label) : null));
  }
  return bar;
}

/* A5：用户目标气泡下的输入图片缩略图（src 由装配层异步读好后传入）。 */
function renderUserImages(names, srcMap) {
  const list = Array.isArray(names) ? names.slice(0, 4) : [];
  if (!list.length) return null;
  const row = h('div', { class: 'conv-user-images' });
  for (const name of list) {
    const src = srcMap && srcMap[name];
    row.appendChild(h('span', { class: 'conv-user-img', title: name },
      src ? h('img', { src, alt: name, loading: 'lazy' }) : h('span', { class: 'conv-user-img-ph' }, name)));
  }
  return row;
}

function renderUser(m, query, opts) {
  const o = opts || {};
  const bubble = h('div', { class: 'conv-user-bubble' },
    h('div', { class: 'conv-user-goal' }, m.goal || '（未命名目标）'),
    renderUserImages(m.images, o.images),
    h('div', { class: 'conv-ts' }, fmtClock(m.ts)));
  applyHighlight(bubble, query);
  return h('div', { class: 'conv-msg conv-user' },
    h('div', { class: 'conv-user-main' }, bubble,
      actionBar([
        { title: '复制目标', icon: iconCopy(), onclick: () => copyMessage(m.goal || '', '目标已复制') },
        // A4：编辑后重发 = 预填新建运行弹窗（run 不可变，是审计资产）
        { title: '编辑后重发（预填新建运行）', icon: iconEdit(), onclick: () => o.onEditResend && o.onEditResend(m.goal || '') },
      ])));
}

/* A3：指引气泡 —— 正文来自 GET 列表 / 乐观插入，chip 表达对账状态。 */
const GUIDANCE_CHIP = {
  pending: '待吸收',
  consumed: '已吸收',
  withdrawn: '已撤回',
  expired: '已过期',
};

function renderGuidance(m, query) {
  const status = m.status || 'pending';
  const chip = status === 'consumed' && m.consumedTask
    ? `已被 ${m.consumedTask} 吸收`
    : (GUIDANCE_CHIP[status] || status);
  const bubble = h('div', { class: `conv-user-bubble conv-guidance conv-guidance-${status}` },
    h('div', { class: 'conv-guidance-head' },
      h('span', { class: 'conv-guidance-tag' }, '中途指引'),
      m.target ? h('span', { class: 'conv-guidance-target' }, `定向 ${m.target}`) : null,
      h('span', { class: 'conv-guidance-chip' }, chip)),
    h('div', { class: 'conv-user-goal' }, m.text || ''),
    h('div', { class: 'conv-ts' }, m.ts ? fmtClock(m.ts) : '刚刚' + (m.optimistic ? ' · 待对账' : '')));
  applyHighlight(bubble, query);
  return h('div', { class: 'conv-msg conv-user' },
    h('div', { class: 'conv-user-main' }, bubble,
      actionBar([
        { title: '复制指引', icon: iconCopy(), onclick: () => copyMessage(m.text || '', '指引已复制') },
      ])));
}

function gotoPage(nav) {
  document.querySelector(`[data-nav="${nav}"]`)?.click();
}

function renderSystem(m, query) {
  const bubble = h('div', { class: `conv-system-bubble${m.kind ? ' conv-' + m.kind : ''}` }, m.text);
  applyHighlight(bubble, query);
  const actions = [];
  if (m.kind === 'plan') {
    actions.push({ title: '查看计划', icon: iconOpen(), onclick: () => gotoPage('tasks') });
  } else if (m.kind === 'done' || m.kind === 'error') {
    actions.push({ title: '查看结果', icon: iconOpen(), onclick: () => document.getElementById('btn-result')?.click() });
  } else if (m.kind === 'warn' && /审批/.test(m.text || '')) {
    actions.push({ title: '去审批（门禁页，批准后自动恢复）', icon: iconOpen(), onclick: () => gotoPage('gates') });
  }
  actions.push({ title: '复制这条提示', icon: iconCopy(), onclick: () => copyMessage(m.text || '', '提示已复制') });
  return h('div', { class: 'conv-msg conv-system' },
    h('div', { class: 'conv-system-main' }, bubble, actionBar(actions)));
}

function agentMeta(m, task) {
  const items = m.items || [];
  const finish = [...items].reverse().find(it => it.kind === 'finish');
  const artifacts = items.filter(it => it.kind === 'artifact');
  const gates = items.filter(it => it.kind === 'gate');
  const bits = [];
  if (finish && finish.dur != null) bits.push(fmtDur(finish.dur));
  if (artifacts.length) bits.push(`${artifacts.length} 个产物`);
  if (gates.length) bits.push(gates.some(g => g.verdict === 'reject') ? '门禁有驳回' : '门禁已通过');
  if (task && task.rework_count) bits.push(`返工 ${task.rework_count} 次`);
  return bits;
}

function renderAgent(m, taskIndex, stream, expanded, onToggle, onOpenTask, onPreview, query, opts) {
  const rm = roleMeta(m.role);
  const task = taskIndex[m.taskId];
  const status = task && task.status;
  const sm = status ? STATUS_META[status] : null;
  const body = h('div', { class: 'conv-agent-body' },
    h('div', { class: 'conv-agent-head' },
      h('span', { class: 'conv-agent-role' }, rm.full),
      h('button', { class: 'conv-agent-task', type: 'button', onclick: () => onOpenTask(m.taskId) },
        m.taskId + (task && task.title ? ` · ${trunc(task.title, 24)}` : '')),
      sm ? h('span', { class: `status-chip status-${status}`, style: 'margin-left:auto' }, sm.label) : null));

  (m.items || []).forEach((it, index) => {
    const key = `${it.kind}:${m.taskId}:${index}`;
    const isExpanded = expanded.has(key);
    if (it.kind === 'note') body.appendChild(renderNote(it, key, isExpanded, onToggle));
    else if (it.kind === 'tool') body.appendChild(renderTool(it, key, isExpanded, onToggle));
    else if (it.kind === 'gate') body.appendChild(renderGate(it, key, isExpanded, onToggle));
    else if (it.kind === 'artifact') body.appendChild(renderArtifact(it, (art) => onPreview(art, m.taskId)));
  });
  if (stream && stream.text) {
    const streamBody = h('div', { class: 'conv-stream-body', innerHTML: renderMarkdown(stream.text) });
    decorateCodeBlocks(streamBody);
    body.appendChild(h('div', { class: 'conv-stream' },
      h('div', { class: 'conv-stream-label' }, '正在输出…'), streamBody));
  }

  const metaBits = agentMeta(m, task);
  body.appendChild(h('div', { class: 'conv-msg-foot' },
    h('span', { class: 'conv-foot-meta' }, metaBits.join(' · ')),
    actionBar([
      { title: '复制整条消息', icon: iconCopy(), onclick: () => copyMessage(agentMessageText(m, task), '消息已复制') },
      { title: '打开任务详情', icon: iconOpen(), onclick: () => onOpenTask(m.taskId) },
      // K1/A4：气泡级「从此重做」＝ rewind dry-run → resume（需连接后端 API）
      { title: '从此重做（回滚该任务并重跑下游）', icon: iconRedo(), onclick: () => opts && opts.onRedo && opts.onRedo(m.taskId) },
    ])));

  applyHighlight(body, query);
  return h('div', { class: 'conv-msg conv-agent' },
    h('span', { class: 'conv-agent-avatar', style: `background:${rm.color}` }, rm.label), body);
}

function renderMessage(m, taskIndex, options) {
  const { stream, expanded, query, onToggle, onOpenTask, onPreview, onRedo, onEditResend, images } = options || {};
  const node = m.subtype === 'guidance' ? renderGuidance(m, query)
    : m.type === 'user' ? renderUser(m, query, { onEditResend, images })
    : m.type === 'system' ? renderSystem(m, query)
    : renderAgent(m, taskIndex, stream, expanded, onToggle, onOpenTask, onPreview, query, { onRedo });
  node.dataset.messageKey = messageKey(m);
  return node;
}

export { messageKey, messageSignature, renderMessage };
