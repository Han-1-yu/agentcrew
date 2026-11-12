/**
 * 零依赖的轻量 Markdown 渲染（安全优先）。
 *
 * 只覆盖任务说明 / 流式正文里最常见的元素：标题、粗体、斜体、行内代码、
 * 代码块、无序/有序列表、表格、链接、分隔线。安全策略：**先提取代码（占位）→
 * 转义全部 HTML 特殊字符 → 替换白名单 markdown 标记 → 恢复代码**。因此输入里的
 * 任意 `<script>` / `<img onerror>` 都会被转义成文本，绝不会被执行；链接只放行
 * `http(s)://` 与相对路径 / 锚点，`javascript:` / `data:` 等一律丢弃。
 *
 * 未覆盖的复杂语法（嵌套引用、任务列表、脚注、图片）保持为转义后的纯文本，
 * 宁可不渲染也不冒险。
 */

const SAFE_URL = /^(https?:\/\/|#|\/|\.\/|\.\.\/|[a-zA-Z0-9._~-]+\/)/i;

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    // C-16：属性值里的引号必须转义。`href="${url}"` 遇到含 `"` 的 URL 会被
    // 提前闭合，后面拼上的内容就成了可控属性（CSP 挡住了内联事件，不构成
    // 可执行 XSS，但链接可被改写成任意地址）。
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* 提取代码块与行内代码为占位符，避免其中的 markdown 标记被误替换。 */
function extractCode(src) {
  const blocks = [];
  const inlines = [];
  let s = src;
  s = s.replace(/```[^\n]*\n?([\s\S]*?)```/g, (m, code) => {
    const i = blocks.push(code.replace(/\n$/, ''));
    return `\u0000CB${i - 1}\u0000`;
  });
  s = s.replace(/`([^`\n]+)`/g, (m, code) => {
    const i = inlines.push(code);
    return `\u0000IC${i - 1}\u0000`;
  });
  return { s, blocks, inlines };
}

function renderTable(s) {
  // 匹配「表头 + 分隔行 + 数据行」的连续块
  return s.replace(/(?:^[ \t]*\|.+\|[ \t]*$\n?)+/gm, (block) => {
    const lines = block.trim().split('\n').filter((l) => l.trim());
    if (lines.length < 2 || !/^[ \t]*\|[\s:|-]+\|[ \t]*$/.test(lines[1])) return block;
    const parseRow = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
    const header = parseRow(lines[0]);
    const body = lines.slice(2).map(parseRow);
    const th = header.map((c) => `<th>${c}</th>`).join('');
    const trs = body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('');
    return `<table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`;
  });
}

function renderList(s) {
  // 连续的无序列表行（- / * / +）
  s = s.replace(/(?:^[ \t]*[-*+][ \t]+.+(?:\n|$))+/gm, (block) => {
    const items = block.trim().split('\n').map((l) => l.replace(/^[ \t]*[-*+][ \t]+/, ''));
    return `<ul>${items.map((i) => `<li>${i}</li>`).join('')}</ul>`;
  });
  // 连续的有序列表行（1. 2. ...）
  s = s.replace(/(?:^[ \t]*\d+[.)][ \t]+.+(?:\n|$))+/gm, (block) => {
    const items = block.trim().split('\n').map((l) => l.replace(/^[ \t]*\d+[.)][ \t]+/, ''));
    return `<ol>${items.map((i) => `<li>${i}</li>`).join('')}</ol>`;
  });
  return s;
}

function renderInline(s) {
  // 行内代码占位符先保护，其余做 bold/italic/link 替换
  s = s
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
    .replace(/(^|[^_\w])_([^_\n]+)_(?!_)/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]]+)\]\(([^\s()]+)\)/g, (m, text, url) => {
    if (!SAFE_URL.test(url)) return m; // 危险/无法识别：保留原文（已转义）
    return `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  });
  return s;
}

function renderBlock(s) {
  // 标题（先做，避免把列表/表格里的 # 误判）
  s = s.replace(/^(#{1,6})[ \t]+(.+)$/gm, (m, hashes, content) => {
    const level = hashes.length;
    return `<h${level}>${content}</h${level}>`;
  });
  // 分隔线（单独一行的 --- / *** / ___）
  s = s.replace(/^[ \t]*(---|\*\*\*|___)[ \t]*$/gm, '<hr>');
  return s;
}

function restoreCode(s, blocks, inlines) {
  s = s.replace(/\u0000CB(\d+)\u0000/g, (m, i) => {
    const code = escapeHtml(blocks[Number(i)] || '');
    return `<pre><code>${code}</code></pre>`;
  });
  s = s.replace(/\u0000IC(\d+)\u0000/g, (m, i) => {
    const code = escapeHtml(inlines[Number(i)] || '');
    return `<code>${code}</code>`;
  });
  return s;
}

function renderMarkdown(text) {
  if (text == null) return '';
  const { s, blocks, inlines } = extractCode(String(text));
  let out = escapeHtml(s);
  out = renderBlock(out);
  out = renderTable(out);
  out = renderList(out);
  out = renderInline(out);
  out = restoreCode(out, blocks, inlines);
  // 把相邻块之间补成段落换行（markdown 的单个换行不换行，双换行才分段）
  return out.replace(/\n{2,}/g, '<br><br>').replace(/\n/g, '<br>');
}

export { escapeHtml, renderMarkdown };
