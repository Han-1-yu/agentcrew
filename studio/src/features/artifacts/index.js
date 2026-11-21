/**
 * 产物页：目录选择 + 代码预览 + 语法着色 + 导出。
 */
import { artifactExportOutcome, artifactIcon, groupArtifacts, normalizeArtifactList } from '../../lib/artifacts.js';
import { snapshotVersionsFor } from '../../lib/artifact-versions.js';
import { $$, h } from '../../lib/dom.js';
import { fmtBytes } from '../../lib/format.js';
import { renderMarkdown } from '../../lib/markdown.js';
import { S } from '../../lib/store.js';

import { emptyState, errorState, isStaleRender, loadingState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';

import { switchNav } from '../../app/router.js';

import { openTaskDetail } from '../detail/panel.js';
import { renderDiffInto, timeText, versionBar } from './versions.js';
const ARTIFACT_ICONS = {
  code: '📄', doc: '📄', test: '🧪', report: '📊', config: '⚙️', other: '📦',
};
const MD_RE = /\.(md|markdown)$/i;
let previewSeq = 0;

async function renderArtifactsPage(container) {
  const token = S.navToken;
  const reload = () => { container.innerHTML = ''; renderArtifactsPage(container); };
  container.appendChild(h('div', { class: 'page-head' },
    h('h2', {}, '产物'),
    h('div', { style: 'display:flex;gap:8px' },
      h('button', { class: 'btn', onclick: async () => {
        if (!S.dash.run || !S.dash.run.dir) return;
        try {
          const result = await window.studio.exportArtifacts(S.dash.run.dir);
          const outcome = artifactExportOutcome(result);
          if (outcome.type === 'canceled') toast('info', '已取消', '未导出产物');
          else if (outcome.type === 'saved') toast('success', '已导出', `产物已打包为 ZIP（${fmtBytes(outcome.size)}）`);
          else throw new Error(outcome.message);
        }
        catch (e) { toast('error', '导出失败', String(e.message || e)); }
      } }, '导出 ZIP'),
      h('button', { class: 'btn', onclick: () => switchNav('dashboard') }, '返回'))));

  const body = h('div', { class: 'art-body' });
  container.appendChild(body);

  if (!S.dash.run || !S.dash.run.dir) {
    body.appendChild(emptyState('未选择运行',
      '请从右上角的运行列表中选择一次运行，再查看其产物。'));
    return;
  }

  // 左侧文件树
  const tree = h('div', { class: 'art-tree' });
  const preview = h('div', { class: 'art-preview' });
  body.appendChild(tree); body.appendChild(preview);

  tree.appendChild(h('div', { class: 'art-tree-head' }, '文件列表'));
  const listEl = h('div', { class: 'art-tree-list' });
  tree.appendChild(listEl);

  listEl.appendChild(loadingState('正在读取产物清单…'));
  let entries = [];
  let manifestError = null;
  try {
    const payload = await window.studio.listArtifacts(S.dash.run.dir);
    ({ entries, manifestError } = normalizeArtifactList(payload));
  } catch (e) {
    listEl.innerHTML = '';
    listEl.appendChild(errorState('产物清单加载失败', String(e.message || e), reload));
    return;
  }
  // rewind 快照（J1）：没有 history/ 就是空清单，不影响正常预览。
  let snapshots = [];
  if (window.studio.listArtifactSnapshots) {
    try { ({ snapshots } = await window.studio.listArtifactSnapshots(S.dash.run.dir)); } catch { snapshots = []; }
    if (isStaleRender(token, container)) return;
  }
  // await 期间可能已经切页：不再往已卸载的 DOM 继续渲染。
  if (isStaleRender(token, container)) return;
  listEl.innerHTML = '';
  if (manifestError) {
    listEl.appendChild(h('div', { class: 'art-manifest-warning' }, `MANIFEST 异常，已改用目录扫描：${manifestError}`));
  }

  if (!entries || !entries.length) {
    listEl.appendChild(emptyState('该运行暂无产物', '任务完成后，产物会出现在这里。'));
    preview.appendChild(emptyState('选择左侧文件查看内容'));
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
    ...Object.entries(kinds).map(([k, v]) => [h('span', { class: 'art-summary-sep' }, '·'), h('span', { class: 'art-summary-kind', innerHTML: `${artifactIcon(k)} ${v}` })]).flat());
  tree.insertBefore(summary, listEl);

  for (const [dir, files] of groupArtifacts(entries)) {
    listEl.appendChild(h('div', { class: 'art-dir' }, dir));
    for (const f of files) {
      const item = h('button', { class: 'art-file', type: 'button', onclick: async () => {
        $$('.art-file').forEach(x => x.classList.remove('active'));
        item.classList.add('active');
        await previewArtifact(preview, f, snapshots);
      }},
        h('span', { class: 'art-file-icon', innerHTML: artifactIcon(f.kind) }),
        h('span', { class: 'art-file-name', title: f.path }, (f.path || '').split('/').pop()),
        h('span', { class: 'art-file-size' }, fmtBytes(f.sizeBytes)));
      listEl.appendChild(item);
    }
  }
  preview.appendChild(emptyState('选择左侧文件查看内容'));
}

async function previewArtifact(preview, f, snapshots = []) {
  const seq = ++previewSeq;
  const versions = snapshotVersionsFor(snapshots, f.path);
  const isMd = MD_RE.test(f.path || '');
  const state = { version: '', rendered: isMd };
  preview.innerHTML = '';
  const body = h('div', { class: 'art-preview-body' });
  const draw = () => { drawArtifact(seq, body, f, versions, state); };
  preview.appendChild(h('div', { class: 'art-preview-head' },
    h('span', { class: 'art-preview-path' }, f.path || ''),
    h('span', { class: 'art-preview-meta' },
      `${fmtBytes(f.sizeBytes)} · ${f.role || '—'} · ${f.task || '—'}`),
    h('div', { class: 'art-preview-acts' },
      versionBar(versions, state, draw),
      isMd ? h('button', { class: 'btn btn-sm art-view-toggle', type: 'button',
        onclick: () => { state.rendered = !state.rendered; draw(); } },
        state.rendered ? '源码' : '渲染') : null,
      h('button', { class: 'btn btn-sm art-save-as', type: 'button', title: '把当前版本另存到本地',
        onclick: () => saveArtifactCopy(f) }, '另存为'),
      // 直接打开即可：openDetailPanel 会先清空抽屉。此前调用的
      // closeTaskDetail 全仓未定义（且 closeDetailPanel 的 220ms 延时收起
      // 会把刚打开的面板又藏起来），点这个按钮必抛 ReferenceError。
      f.task ? h('button', { class: 'btn btn-sm', onclick: () => openTaskDetail(f.task) }, '关联任务') : null,
      h('button', { class: 'btn btn-sm', title: '在文件管理器中显示',
        onclick: () => { const p = S.dash.run.dir + '\\' + (f.path || '').replace(/\//g, '\\'); window.studio.revealFile(p); } },
        h('span', { class: 'art-reveal-label', innerHTML: `${artifactIcon('folder')} 显示` })))));
  preview.appendChild(body);
  draw();
}

async function saveArtifactCopy(f) {
  try {
    const r = await window.studio.saveArtifactAs(S.dash.run.dir, f.path);
    if (r && r.canceled) toast('info', '已取消', '未另存文件');
    else toast('success', '已另存', String((r && r.path) || ''));
  } catch (e) { toast('error', '另存失败', String(e.message || e)); }
}

/**
 * 画当前版本或 diff：`state.version` 非空时拿快照与当前做行级对比；
 * 否则按 `state.rendered` 决定 Markdown 渲染还是语法高亮源码。
 */
async function drawArtifact(seq, body, f, versions, state) {
  const alive = () => seq === previewSeq && body.isConnected;
  body.innerHTML = '';
  body.appendChild(h('div', { class: 'dp-text dim' }, '加载中…'));
  try {
    if (state.version) {
      const [now, old] = await Promise.all([
        window.studio.readArtifact(S.dash.run.dir, f.path),
        window.studio.readSnapshotArtifact(S.dash.run.dir, state.version, f.path),
      ]);
      if (!alive()) return;
      body.innerHTML = '';
      const v = versions.find((x) => x.name === state.version) || {};
      renderDiffInto(body, {
        before: old && old.content != null ? String(old.content) : '',
        after: now && now.content != null ? String(now.content) : '',
        beforeLabel: [state.version, v.createdAt ? timeText(v.createdAt) : ''].filter(Boolean).join(' · '),
        afterLabel: '当前',
      });
      return;
    }
    const r = await window.studio.readArtifact(S.dash.run.dir, f.path);
    if (!alive()) return;
    body.innerHTML = '';
    if (r && r.image && r.dataBase64) {
      const img = h('img', { alt: f.path || '图像产物' });
      img.src = `data:${r.mime};base64,${r.dataBase64}`;
      img.style.maxWidth = '100%';
      img.style.borderRadius = '6px';
      body.appendChild(img);
      return;
    }
    const text = r && r.content != null ? String(r.content) : (r && typeof r === 'string' ? r : '');
    if (!text) { body.appendChild(h('div', { class: 'art-preview-content' }, '（空文件或二进制）')); return; }
    const truncated = text.length > 100000 ? text.slice(0, 100000) + '\n…（已截断，共 ' + text.length + ' 字符）' : text;
    if (state.rendered) {
      const md = h('div', { class: 'art-markdown' });
      md.innerHTML = renderMarkdown(truncated);
      body.appendChild(md);
      return;
    }
    const content = h('pre', { class: 'art-preview-content' });
    content.innerHTML = highlightCode(truncated, f.path || '');
    body.appendChild(content);
  } catch (e) {
    if (!alive()) return;
    body.innerHTML = '';
    body.appendChild(h('div', { class: 'art-preview-content' }, '读取失败：' + String(e.message || e)));
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
