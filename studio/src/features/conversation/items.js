/**
 * 对话消息里的条目渲染：说明文字 / 工具调用 / 门禁 / 产物。
 *
 * 与 view.js 的分工：这里只产出「气泡内部」的节点，不关心消息头、操作栏与滚动；
 * 工具调用把「路径 / 命令 / 查询」这类关键入参提到摘要行，避免用户必须展开 JSON
 * 才知道这一步做了什么。
 */
import { artifactIcon, artifactKind } from '../../lib/artifacts.js';
import { copyText } from '../../lib/clipboard.js';
import { gateLabel, gateVerdict, summarizeToolArgs } from '../../lib/conversation.js';
import { h } from '../../lib/dom.js';
import { fmtBytes, fmtDur } from '../../lib/format.js';
import { renderMarkdown } from '../../lib/markdown.js';

import { toast } from '../../ui/toast.js';

/* Markdown 代码块右上角的复制键：渲染完再挂，避免影响 renderMarkdown 的安全策略。 */
function decorateCodeBlocks(root) {
  if (!root) return;
  root.querySelectorAll('pre').forEach((pre) => {
    if (pre.querySelector('.conv-code-copy')) return;
    const btn = h('button', {
      class: 'conv-code-copy', type: 'button', 'aria-label': '复制代码', title: '复制代码',
    }, '复制');
    btn.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      const code = pre.querySelector('code') || pre;
      const ok = await copyText(code.textContent);
      toast(ok ? 'success' : 'error', ok ? '已复制' : '复制失败', ok ? '代码已复制到剪贴板' : '剪贴板不可用');
    });
    pre.appendChild(btn);
  });
}

function renderNote(it, key, expanded, onToggle) {
  const text = String(it.text || '');
  const MAX = 400;
  const node = h('div', { class: 'conv-note' });
  if (text.length <= MAX) {
    node.innerHTML = renderMarkdown(text);
    decorateCodeBlocks(node);
    return node;
  }
  if (expanded) {
    node.innerHTML = renderMarkdown(text);
    decorateCodeBlocks(node);
    node.appendChild(h('button', {
      class: 'conv-note-toggle', type: 'button', onclick: (ev) => { ev.stopPropagation(); onToggle(key); },
    }, ' 收起'));
    return node;
  }
  node.appendChild(document.createTextNode(`${text.slice(0, MAX)}…`));
  node.appendChild(h('button', {
    class: 'conv-note-toggle', type: 'button', onclick: (ev) => { ev.stopPropagation(); onToggle(key); },
  }, ' 展开全文'));
  return node;
}

function renderTool(it, key, expanded, onToggle) {
  const tone = it.ok === false ? 'conv-tool-fail' : (it.ok === true ? 'conv-tool-ok' : 'conv-tool-unknown');
  const summary = summarizeToolArgs(it.args, 68);
  return h('div', { class: 'conv-item conv-tool' },
    h('button', {
      class: 'conv-item-head', type: 'button', 'aria-expanded': String(!!expanded), onclick: () => onToggle(key),
    },
      h('span', { class: 'conv-item-chev' }, expanded ? '▾' : '▸'),
      h('span', { class: `conv-tool-dot ${tone}` }),
      h('span', { class: 'conv-tool-name' }, it.tool || '工具'),
      summary ? h('span', { class: 'conv-tool-arg', title: summary }, summary) : null,
      h('span', { class: 'conv-item-dim', style: 'margin-left:auto' }, it.dur != null ? fmtDur(it.dur) : ''),
      h('span', { class: 'conv-item-dim' }, expanded ? '收起' : '详情')),
    expanded ? h('div', { class: 'conv-item-body' },
      it.error ? h('div', { class: 'conv-tool-error' }, String(it.error)) : null,
      h('pre', {}, JSON.stringify(it.args == null ? {} : it.args, null, 2))) : null);
}

function renderGate(it, key, expanded, onToggle) {
  const label = gateLabel(it.layer);
  const verdict = gateVerdict(it.verdict);
  const issues = it.issues || [];
  return h('div', { class: 'conv-item conv-gate' },
    h('button', {
      class: 'conv-item-head', type: 'button', 'aria-expanded': String(!!expanded), onclick: () => onToggle(key),
    },
      h('span', { class: 'conv-item-chev' }, expanded ? '▾' : '▸'),
      h('span', { class: `conv-gate-dot ${it.verdict === 'reject' ? 'conv-gate-reject' : ''}` }),
      h('span', { class: 'conv-gate-name' }, `${label} · ${verdict}`),
      it.score != null ? h('span', { class: 'conv-item-dim' }, `${it.score} 分`) : null,
      issues.length ? h('span', { class: 'conv-gate-count' }, `${issues.length} 个问题`) : null,
      h('span', { class: 'conv-item-dim', style: 'margin-left:auto' }, expanded ? '收起' : '详情')),
    expanded ? h('div', { class: 'conv-item-body' },
      issues.length
        ? issues.map((issue) => h('div', { class: 'conv-gate-issue' }, `· ${issue}`))
        : h('div', { class: 'conv-item-dim' }, '无问题')) : null);
}

function renderArtifact(it, onPreview) {
  const kind = artifactKind(it.path);
  return h('button', {
    class: 'conv-item conv-artifact', type: 'button', title: `预览 ${it.path || ''}`,
    onclick: () => onPreview(it),
  },
    h('span', { class: 'conv-artifact-icon', innerHTML: artifactIcon(kind) }),
    h('span', { class: 'conv-artifact-path' }, it.path || ''),
    h('span', { class: 'conv-item-dim', style: 'margin-left:auto' }, it.size != null ? fmtBytes(it.size) : ''),
    h('span', { class: 'conv-artifact-open' }, '预览'));
}

export { decorateCodeBlocks, renderArtifact, renderGate, renderNote, renderTool };
