/* 对话消息视图：纯渲染与消息签名，不包含实时状态和调度。 */
import { h } from '../../lib/dom.js';
import { STATUS_META, roleMeta } from '../../contracts/enums.js';
import { fmtBytes, fmtClock, fmtDur, trunc } from '../../lib/format.js';
import { renderMarkdown } from '../../lib/markdown.js';

const markdownCache = new Map();
function noteMarkdown(text) {
  if (markdownCache.has(text)) return markdownCache.get(text);
  const html = renderMarkdown(text);
  if (markdownCache.size >= 60) markdownCache.delete(markdownCache.keys().next().value);
  markdownCache.set(text, html);
  return html;
}

function messageKey(m) {
  return m.type === 'agent' ? `agent:${m.taskId}` : `${m.type}:${m.order}`;
}

/* DOM 是否可复用只由实际展示内容决定；已完成消息不会因其他任务输出而重绘。 */
function messageSignature(m, taskIndex, stream, expanded) {
  const task = taskIndex[m.taskId];
  const expandedForTask = m.type === 'agent'
    ? [...expanded].filter(key => key.includes(`:${m.taskId}:`)).sort() : [];
  return JSON.stringify([m, task && task.status, task && task.title, stream || null, expandedForTask]);
}

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

function renderNote(it, key, expanded, onToggle) {
  const text = it.text || '';
  const MAX = 400;
  if (text.length <= MAX) return h('div', { class: 'conv-note', innerHTML: noteMarkdown(text) });
  if (expanded) {
    return h('div', { class: 'conv-note', innerHTML: noteMarkdown(text) },
      h('button', { class: 'conv-note-toggle', type: 'button', onclick: ev => { ev.stopPropagation(); onToggle(key); } }, ' 收起'));
  }
  return h('div', { class: 'conv-note' }, `${text.slice(0, MAX)}…`,
    h('button', { class: 'conv-note-toggle', type: 'button', onclick: ev => { ev.stopPropagation(); onToggle(key); } }, ' 展开全文'));
}

function renderTool(it, key, expanded, onToggle) {
  const tone = it.ok === false ? 'conv-tool-fail' : 'conv-tool-ok';
  return h('div', { class: 'conv-item conv-tool' },
    h('button', { class: 'conv-item-head', type: 'button', 'aria-expanded': String(expanded), onclick: () => onToggle(key) },
      h('span', { class: 'conv-item-chev' }, expanded ? '▾' : '▸'),
      h('span', { class: `conv-tool-dot ${tone}` }),
      h('span', { class: 'conv-tool-name' }, it.tool || '工具'),
      h('span', { class: 'conv-item-dim' }, it.dur != null ? fmtDur(it.dur) : ''),
      h('span', { class: 'conv-item-dim', style: 'margin-left:auto' }, expanded ? '收起' : '详情')),
    expanded ? h('div', { class: 'conv-item-body' },
      h('pre', {}, JSON.stringify(it.args == null ? {} : it.args, null, 2))) : null);
}

function renderGate(it, key, expanded, onToggle) {
  const label = { static: '静态校验', llm: 'LLM 评审', human: '人工确认', supervisor: '主管复核' }[it.layer] || it.layer || '门禁';
  const verdict = it.verdict === 'approve' ? '通过' : (it.verdict === 'reject' ? '驳回' : String(it.verdict || ''));
  return h('div', { class: 'conv-item conv-gate' },
    h('button', { class: 'conv-item-head', type: 'button', 'aria-expanded': String(expanded), onclick: () => onToggle(key) },
      h('span', { class: 'conv-item-chev' }, expanded ? '▾' : '▸'),
      h('span', { class: `conv-gate-dot ${it.verdict === 'reject' ? 'conv-gate-reject' : ''}` }),
      h('span', { class: 'conv-gate-name' }, `${label} · ${verdict}`),
      h('span', { class: 'conv-item-dim', style: 'margin-left:auto' }, expanded ? '收起' : '详情')),
    expanded ? h('div', { class: 'conv-item-body' },
      it.score != null ? h('div', { class: 'conv-item-dim' }, `评分 ${it.score}`) : null,
      (it.issues || []).map(issue => h('div', { class: 'conv-gate-issue' }, `· ${issue}`))) : null);
}

function renderArtifact(it, onOpenTask) {
  return h('button', { class: 'conv-item conv-artifact', type: 'button', onclick: () => onOpenTask(it.taskId || '') },
    h('span', { class: 'conv-artifact-icon' }, '📄'),
    h('span', { class: 'conv-artifact-path' }, it.path || ''),
    h('span', { class: 'conv-item-dim', style: 'margin-left:auto' }, it.size != null ? fmtBytes(it.size) : ''));
}

function renderAgent(m, taskIndex, stream, expanded, onToggle, onOpenTask) {
  const rm = roleMeta(m.role);
  const task = taskIndex[m.taskId];
  const status = task && task.status;
  const sm = status ? STATUS_META[status] : null;
  const body = h('div', { class: 'conv-agent-body' },
    h('div', { class: 'conv-agent-head' },
      h('span', { class: 'conv-agent-role', style: `color:${rm.color}` }, rm.full),
      h('button', { class: 'conv-agent-task', type: 'button', onclick: () => onOpenTask(m.taskId) },
        m.taskId + (task && task.title ? ` · ${trunc(task.title, 24)}` : '')),
      sm ? h('span', { class: `status-chip status-${status}`, style: 'margin-left:auto' }, sm.label) : null));

  (m.items || []).forEach((it, index) => {
    const key = `${it.kind}:${m.taskId}:${index}`;
    const isExpanded = expanded.has(key);
    if (it.kind === 'note') body.appendChild(renderNote(it, key, isExpanded, onToggle));
    else if (it.kind === 'tool') body.appendChild(renderTool(it, key, isExpanded, onToggle));
    else if (it.kind === 'gate') body.appendChild(renderGate(it, key, isExpanded, onToggle));
    else if (it.kind === 'artifact') body.appendChild(renderArtifact({ ...it, taskId: m.taskId }, onOpenTask));
  });
  if (stream && stream.text) {
    body.appendChild(h('div', { class: 'conv-stream' },
      h('div', { class: 'conv-stream-label' }, '正在输出…'),
      h('div', { class: 'conv-stream-body', innerHTML: renderMarkdown(stream.text) })));
  }
  return h('div', { class: 'conv-msg conv-agent' },
    h('span', { class: 'conv-agent-avatar', style: `background:${rm.color}` }, rm.label), body);
}

function renderMessage(m, taskIndex, options) {
  const { stream, expanded, onToggle, onOpenTask } = options;
  const node = m.type === 'user' ? renderUser(m)
    : m.type === 'system' ? renderSystem(m)
    : renderAgent(m, taskIndex, stream, expanded, onToggle, onOpenTask);
  node.dataset.messageKey = messageKey(m);
  return node;
}

export { messageKey, messageSignature, renderMessage };
