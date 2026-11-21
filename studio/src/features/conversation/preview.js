/**
 * 对话内产物预览：右侧抽屉里直接看内容，不用跳去产物页再找文件。
 *
 * 复用 #detail-panel（与任务详情同一套遮罩/焦点/关闭行为），只替换内容；
 * 读取走主进程 artifact:read，图片给 <img>、Markdown 走统一渲染、其余按纯文本塞 <pre>。
 */
import { artifactKind } from '../../lib/artifacts.js';
import { copyText } from '../../lib/clipboard.js';
import { h } from '../../lib/dom.js';
import { fmtBytes } from '../../lib/format.js';
import { renderMarkdown } from '../../lib/markdown.js';
import { S } from '../../lib/store.js';

import { errorState, loadingState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';

import { openDetailPanel, openTaskDetail } from '../detail/panel.js';

import { decorateCodeBlocks } from './items.js';

let previewSeq = 0;

function closePanel() {
  const mask = document.getElementById('detail-mask');
  if (mask) mask.click();
}

async function openArtifactPreview(it, taskId) {
  const run = S.dash && S.dash.run;
  const relPath = String((it && it.path) || '');
  if (!run || !run.dir) {
    toast('error', '无法预览', '当前没有已加载的运行目录');
    return;
  }
  if (!relPath) return;

  const seq = ++previewSeq;
  const body = h('div', { class: 'conv-preview-body' });
  const meta = h('div', { class: 'conv-preview-meta' },
    h('span', {}, relPath),
    h('span', {}, `${fmtBytes(it.size)} · ${artifactKind(relPath)}`));

  const head = h('div', { class: 'dp-head' },
    h('div', { class: 'dp-title-row' },
      h('div', { class: 'dp-title' }, '产物预览'),
      h('button', {
        class: 'btn btn-icon', style: 'width:28px;height:28px', 'aria-label': '关闭', title: '关闭',
        onclick: closePanel,
      }, '✕')),
    meta,
    h('div', { class: 'conv-preview-actions' },
      h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: async () => {
          const ok = await copyText(relPath);
          toast(ok ? 'success' : 'error', ok ? '已复制路径' : '复制失败', relPath);
        },
      }, '复制路径'),
      taskId ? h('button', {
        class: 'btn btn-sm', type: 'button', onclick: () => openTaskDetail(taskId),
      }, '关联任务') : null));

  openDetailPanel(h('div', { class: 'conv-preview' }, head, body));
  body.appendChild(loadingState('正在读取产物…'));

  try {
    const r = await window.studio.readArtifact(run.dir, relPath);
    if (seq !== previewSeq || !body.isConnected) return; // 快速连点：旧请求晚到不覆盖
    body.innerHTML = '';
    if (r && r.image && r.dataBase64) {
      body.appendChild(h('img', {
        class: 'conv-preview-img', alt: relPath, src: `data:${r.mime};base64,${r.dataBase64}`,
      }));
      return;
    }
    const text = r && r.content != null ? String(r.content) : (typeof r === 'string' ? r : '');
    if (!text) {
      body.appendChild(h('div', { class: 'conv-preview-empty' }, '（空文件或二进制内容，暂不支持预览）'));
      return;
    }
    const clipped = text.length > 120000 ? text.slice(0, 120000) : text;
    if (/\.(md|markdown)$/i.test(relPath)) {
      const md = h('div', { class: 'conv-note conv-preview-md' });
      md.innerHTML = renderMarkdown(clipped);
      decorateCodeBlocks(md);
      body.appendChild(md);
    } else {
      const pre = h('pre', { class: 'conv-preview-code' });
      pre.textContent = clipped;
      body.appendChild(pre);
    }
    if (clipped.length !== text.length) {
      body.appendChild(h('div', { class: 'conv-preview-empty' }, `内容较长，仅显示前 ${clipped.length} 个字符`));
    }
  } catch (e) {
    if (seq !== previewSeq || !body.isConnected) return;
    body.innerHTML = '';
    body.appendChild(errorState('读取失败', String((e && e.message) || e)));
  }
}

export { openArtifactPreview };
