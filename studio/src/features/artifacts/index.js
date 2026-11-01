/**
 * 产物页：目录选择 + 代码预览 + 语法着色 + 导出。
 */
import { $, $$, h, s } from '../../lib/dom.js';
import { fmtBytes } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

import { switchNav } from '../../app/router.js';

import { openTaskDetail } from '../detail/panel.js';
const ARTIFACT_ICONS = {
  code: '📄', doc: '📄', test: '🧪', report: '📊', config: '⚙️', other: '📦',
};

async function renderArtifactsPage(container) {
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '产物'),
    h('div', { style: 'display:flex;gap:8px' },
      h('button', { class: 'btn', onclick: async () => {
        if (!S.dash.run || !S.dash.run.dir) return;
        try { await window.studio.exportArtifacts(S.dash.run.dir); toast('success', '已导出', '产物已打包为 ZIP'); }
        catch (e) { toast('error', '导出失败', String(e.message || e)); }
      } }, '导出 ZIP'),
      h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回'))));

  const body = h('div', { class: 'art-body' });
  container.appendChild(body);

  if (!S.dash.run || !S.dash.run.dir) {
    body.appendChild(h('div', { class: 'ph-empty' }, '未选择运行'));
    return;
  }

  // 左侧文件树
  const tree = h('div', { class: 'art-tree' });
  const preview = h('div', { class: 'art-preview' });
  body.appendChild(tree); body.appendChild(preview);

  tree.appendChild(h('div', { class: 'art-tree-head' }, '文件列表'));
  const listEl = h('div', { class: 'art-tree-list' });
  tree.appendChild(listEl);

  let entries = [];
  try {
    entries = await window.studio.listArtifacts(S.dash.run.dir);
  } catch (e) {
    listEl.appendChild(h('div', { class: 'ph-empty' }, '加载失败：' + String(e.message || e)));
    return;
  }

  if (!entries || !entries.length) {
    listEl.appendChild(h('div', { class: 'ph-empty' }, '该运行暂无产物'));
    preview.appendChild(h('div', { class: 'ph-empty' }, '选择左侧文件查看内容'));
    return;
  }

  // 文件统计摘要
  const totalSize = entries.reduce((s, e) => s + (e.sizeBytes || 0), 0);
  const kinds = {};
  for (const e of entries) { const k = e.kind || 'other'; kinds[k] = (kinds[k] || 0) + 1; }
  const summary = h('div', { class: 'art-summary' },
    h('span', {}, `${entries.length} 个文件`),
    h('span', { class: 'art-summary-sep' }, '·'),
    h('span', {}, fmtBytes(totalSize)),
    ...Object.entries(kinds).map(([k, v]) => [h('span', { class: 'art-summary-sep' }, '·'), h('span', {}, `${ARTIFACT_ICONS[k] || '📄'} ${v}`)]).flat());
  tree.insertBefore(summary, listEl);

  // 按目录分组
  const groups = {};
  for (const e of entries) {
    const dir = (e.path || '').split('/')[0] || '根目录';
    if (!groups[dir]) groups[dir] = [];
    groups[dir].push(e);
  }

  for (const [dir, files] of Object.entries(groups)) {
    listEl.appendChild(h('div', { class: 'art-dir' }, dir));
    for (const f of files) {
      const icon = ARTIFACT_ICONS[f.kind] || '📄';
      const item = h('div', { class: 'art-file', onclick: async () => {
        $$('.art-file').forEach(x => x.classList.remove('active'));
        item.classList.add('active');
        await previewArtifact(preview, f);
      }},
        h('span', { class: 'art-file-icon' }, icon),
        h('span', { class: 'art-file-name', title: f.path }, (f.path || '').split('/').pop()),
        h('span', { class: 'art-file-size' }, fmtBytes(f.sizeBytes)));
      listEl.appendChild(item);
    }
  }
  preview.appendChild(h('div', { class: 'ph-empty' }, '选择左侧文件查看内容'));
}

async function previewArtifact(preview, f) {
  preview.innerHTML = '';
  preview.appendChild(h('div', { class: 'art-preview-head' },
    h('span', { class: 'art-preview-path' }, f.path || ''),
    h('span', { class: 'art-preview-meta' },
      `${fmtBytes(f.sizeBytes)} · ${f.role || '—'} · ${f.task || '—'}`),
    h('div', { style: 'display:flex;gap:4px' },
      f.task ? h('button', { class: 'btn btn-sm', onclick: () => { closeTaskDetail(); openTaskDetail(f.task); } }, '关联任务') : null,
      h('button', { class: 'btn btn-sm', title: '在文件管理器中显示',
        onclick: () => { const p = S.dash.run.dir + '\\' + (f.path || '').replace(/\//g, '\\'); window.studio.revealFile(p); } },
        '📂 显示'))));
  const content = h('pre', { class: 'art-preview-content' });
  preview.appendChild(content);
  try {
    const r = await window.studio.readArtifact(S.dash.run.dir, f.path);
    const text = r && r.content != null ? String(r.content) : (r && typeof r === 'string' ? r : '');
    if (!text) { content.textContent = '（空文件或二进制）'; return; }
    const truncated = text.length > 100000 ? text.slice(0, 100000) + '\n…（已截断，共 ' + text.length + ' 字符）' : text;
    content.innerHTML = highlightCode(truncated, f.path || '');
  } catch (e) {
    content.textContent = '读取失败：' + String(e.message || e);
  }
}

/* 轻量语法高亮（零依赖，基于正则） */
function highlightCode(text, filename) {
  const ext = (filename.split('.').pop() || '').toLowerCase();
  const lang = ({
    py: 'python', js: 'javascript', ts: 'javascript', jsx: 'javascript', tsx: 'javascript',
    json: 'json', yaml: 'yaml', yml: 'yaml', md: 'markdown', markdown: 'markdown',
    html: 'html', htm: 'html', css: 'css', sh: 'shell', bash: 'shell',
    toml: 'toml', ini: 'ini', cfg: 'ini', xml: 'html', svg: 'html',
  })[ext] || 'text';
  let escaped = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  if (lang === 'text') return escaped;
  const patterns = {
    python: [
      [/(&quot;.*?&quot;|'.*?')/g, 'tok-str'],
      [/#[^\n]*/g, 'tok-com'],
      [/\b(def|class|return|if|elif|else|for|while|import|from|as|try|except|finally|with|lambda|yield|raise|pass|break|continue|global|nonlocal|assert|del|in|is|not|and|or|True|False|None|async|await)\b/g, 'tok-kw'],
      [/\b(self|cls)\b/g, 'tok-self'],
    ],
    javascript: [
      [/(&quot;.*?&quot;|'.*?'|`.*?`)/g, 'tok-str'],
      [/\/\/[^\n]*/g, 'tok-com'],
      [/\/\*[\s\S]*?\*\//g, 'tok-com'],
      [/\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|class|extends|super|this|import|export|from|default|try|catch|finally|throw|async|await|yield|typeof|instanceof|in|of|true|false|null|undefined)\b/g, 'tok-kw'],
    ],
    json: [
      [/&quot;([^&]+?)&quot;(\s*:)/g, '<span class="tok-key">&quot;$1&quot;</span>$2'],
      [/(&quot;.*?&quot;)/g, 'tok-str'],
      [/\b(true|false|null)\b/g, 'tok-kw'],
    ],
    yaml: [
      [/#[^\n]*/g, 'tok-com'],
      [/^(\s*[\w\-]+:)/gm, 'tok-key'],
      [/(&quot;.*?&quot;|'.*?')/g, 'tok-str'],
      [/\b(true|false|null|yes|no|on|off)\b/g, 'tok-kw'],
    ],
    markdown: [
      [/^(#{1,6}\s.+)$/gm, 'tok-md-h'],
      [/(\*\*[^*]+\*\*|__[^_]+__)/g, 'tok-md-bold'],
      [/(`[^`]+`)/g, 'tok-md-code'],
      [/^(\s*[-*+]\s)/gm, 'tok-md-list'],
    ],
    html: [
      [/(&lt;\/?[\w\-]+)/g, 'tok-tag'],
      [/([\w\-]+=)(&quot;.*?&quot;|'.*?')/g, '$1<span class="tok-str">$2</span>'],
      [/(&lt;!--[\s\S]*?--&gt;)/g, 'tok-com'],
    ],
    css: [
      [/\/\*[\s\S]*?\*\//g, 'tok-com'],
      [/([\w\-]+)\s*:/g, '<span class="tok-key">$1</span>:'],
      [/(&quot;.*?&quot;|'.*?')/g, 'tok-str'],
      [/(#[0-9a-fA-F]{3,8})\b/g, 'tok-num'],
    ],
    shell: [
      [/#[^\n]*/g, 'tok-com'],
      [/(&quot;.*?&quot;|'.*?')/g, 'tok-str'],
      [/\b(echo|cd|ls|mkdir|rm|cp|mv|cat|grep|sed|awk|export|source|if|then|else|fi|for|do|done|while|case|esac|function|return|exit)\b/g, 'tok-kw'],
    ],
    toml: [
      [/#[^\n]*/g, 'tok-com'],
      [/^(\s*[\w\-.]+\s*=)/gm, 'tok-key'],
      [/(&quot;.*?&quot;|'.*?')/g, 'tok-str'],
      [/\b(true|false)\b/g, 'tok-kw'],
    ],
    ini: [
      [/^(\[.*\])/gm, 'tok-key'],
      [/^(\s*[\w\-.]+\s*=)/gm, 'tok-key'],
      [/[;#][^\n]*/g, 'tok-com'],
    ],
  };
  const rules = patterns[lang] || [];
  for (const [pat, cls] of rules) {
    if (typeof cls === 'string') {
      escaped = escaped.replace(pat, (m) => `<span class="${cls}">${m}</span>`);
    } else {
      escaped = escaped.replace(pat, cls);
    }
  }
  return escaped;
}

/* ============================================================
   设置页
   ============================================================ */

export { ARTIFACT_ICONS, highlightCode, previewArtifact, renderArtifactsPage };
