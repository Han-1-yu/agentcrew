/**
 * 右侧结果区（对齐 WorkBuddy 的「结果区」）：概览 + 产物 双 tab 一体化面板。
 *
 * 概览 tab 汇总运行统计（任务 / LLM / token / 成本 / 耗时）、任务状态分布与
 * 三层门禁；产物 tab 复用产物页的文件清单 + 内联预览 + 语法高亮。面板复用
 * ``#detail-panel`` 容器（同一套遮罩 / Esc 关闭 / 淡入动效）。
 */
import { artifactExportOutcome, groupArtifacts, normalizeArtifactList } from '../../lib/artifacts.js';
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';
import { RUN_META, STATUS_META, STATUS_ORDER } from '../../contracts/enums.js';
import { fmtBytes, fmtCost, fmtDur, fmtTokens } from '../../lib/format.js';

import { emptyState, loadingState } from '../../ui/states.js';
import { toast } from '../../ui/toast.js';

import { openDetailPanel } from '../detail/panel.js';
import { ARTIFACT_ICONS, highlightCode } from '../artifacts/index.js';

function currentTab() { return S.resultTab || 'overview'; }
let resultPreviewSeq = 0;

function openResultPanel(tab) {
  S.resultTab = tab || currentTab();
  openDetailPanel(buildResultPanel());
}

function tabBar() {
  const tab = currentTab();
  const mk = (key, label) => h('button', {
    class: `result-tab${tab === key ? ' active' : ''}`,
    type: 'button', role: 'tab', id: `result-tab-${key}`, 'aria-selected': tab === key ? 'true' : 'false',
    'aria-controls': 'result-tab-panel',
    onclick: () => openResultPanel(key),
  }, label);
  return h('div', { class: 'result-tabs', role: 'tablist', 'aria-label': '结果视图' }, mk('overview', '概览'), mk('artifacts', '产物'));
}

function buildResultPanel() {
  return h('div', { style: 'display:flex;flex-direction:column;height:100%' },
    h('div', { class: 'dp-head' },
      h('div', { class: 'dp-title-row' },
        h('div', { class: 'dp-title' }, '结果'),
        h('button', { class: 'btn btn-icon', style: 'width:28px;height:28px', 'aria-label': '关闭', title: '关闭',
          onclick: () => document.getElementById('detail-mask').click() }, '✕')),
      tabBar()),
    h('div', { class: 'dp-body', id: 'result-tab-panel', role: 'tabpanel', 'aria-labelledby': `result-tab-${currentTab()}`, style: 'padding:0' },
      currentTab() === 'overview' ? renderOverview() : mountArtifactsTab()));
}

/* ---------- 概览 tab ---------- */

/* 已运行时长：结束的 run 用 finished_at - started_at，运行中用 now - started_at。 */
function runDuration(run, stats) {
  const start = run.started_at ? Date.parse(String(run.started_at).replace(' ', 'T')) : NaN;
  if (Number.isNaN(start)) return '—';
  const end = stats.finished_at ? Date.parse(String(stats.finished_at).replace(' ', 'T')) : Date.now();
  return fmtDur(Math.max(0, end - start));
}

/* C4：图片放大查看——自建一次性遮罩，Esc / 点击关闭。 */
function openImageOverlay(r) {
  const mask = h('div', {
    style: 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;'
           + 'display:flex;align-items:center;justify-content:center',
    onclick: () => mask.remove(),
  });
  mask.appendChild(h('img', {
    src: `data:${r.mime};base64,${r.dataBase64}`,
    alt: r.name,
    style: 'max-width:92vw;max-height:92vh;border-radius:8px',
  }));
  document.body.appendChild(mask);
}

function renderOverview() {
  const run = S.dash && S.dash.run;
  const stats = (run && run.stats) || {};
  const tasks = (S.dash && S.dash.tasks) || [];
  const gates = (S.dash && S.dash.gates) || [];
  if (!run) return h('div', { style: 'padding:16px' }, emptyState('暂无运行', '选择一次运行后查看结果概览。'));

  const rm = RUN_META[run.status] || RUN_META.idle;
  const body = h('div', { class: 'result-body' });

  // 状态 + 目标
  body.appendChild(h('div', { class: 'result-head' },
    h('span', { class: 'dot', style: `background:${run.status === 'completed' ? 'var(--success)' : (run.status === 'failed' ? 'var(--error)' : 'var(--accent)')}` }),
    h('span', { class: 'result-status' }, rm.label),
    h('span', { class: 'result-goal', title: run.project_goal }, run.project_goal || '')));

  // C4：随 run 输入图片（<runDir>/context/images/）——有图才显示，无图整段隐藏。
  // 异步填充：读列表 → 每张转 data URI 缩略图；任何一步失败都静默跳过
  // （无图 run 是常态，预览是增强不是必需品）。
  if (run.dir && typeof window.studio.listRunImages === 'function') {
    const imagesSec = h('div', { class: 'result-sec-title', style: 'display:none' }, '输入图片');
    const imagesWrap = h('div', { class: 'result-images', style: 'display:none' });
    body.appendChild(imagesSec);
    body.appendChild(imagesWrap);
    window.studio.listRunImages(run.dir).then((imgs) => {
      if (!imgs || !imgs.length) return;
      imagesSec.style.display = '';
      imagesWrap.style.display = '';
      for (const img of imgs) {
        window.studio.readRunImage(run.dir, img.name).then((r) => {
          imagesWrap.appendChild(h('img', {
            src: `data:${r.mime};base64,${r.dataBase64}`,
            title: `${img.name} · ${fmtBytes(r.sizeBytes)}`,
            alt: `输入图片 ${img.name}`,
            style: 'max-width:180px;max-height:120px;margin:4px;'
                   + 'border:1px solid var(--border);border-radius:6px;'
                   + 'cursor:zoom-in;object-fit:cover',
            onclick: () => openImageOverlay(r),
          }));
        }).catch(() => { /* 单张读取失败只跳过 */ });
      }
    }).catch(() => { /* 列表读取失败整段隐藏 */ });
  }

  // 统计网格
  const cell = (v, k) => h('div', { class: 'result-cell' },
    h('div', { class: 'result-cell-v' }, v),
    h('div', { class: 'result-cell-k' }, k));
  body.appendChild(h('div', { class: 'result-grid' },
    cell(String(stats.total_tasks ?? tasks.length), '任务'),
    cell(String(stats.succeeded ?? 0), '成功'),
    cell(String(stats.failed ?? 0), '失败'),
    cell(String(stats.llm_calls ?? 0), 'LLM 调用'),
    cell(fmtTokens((stats.prompt_tokens || 0) + (stats.completion_tokens || 0)), 'Token'),
    cell(fmtCost(stats.estimated_cost_usd), '成本'),
    cell(runDuration(run, stats), '已运行'),
    cell(fmtCost(run.budget_limit_usd), '预算上限')));

  // 任务状态分布
  const dist = {};
  STATUS_ORDER.forEach((s) => { dist[s] = tasks.filter((t) => t.status === s).length; });
  const total = tasks.length || 1;
  body.appendChild(h('div', { class: 'result-sec-title' }, '任务状态'));
  const bar = h('div', { class: 'result-dist' });
  for (const s of STATUS_ORDER) {
    if (!dist[s]) continue;
    const m = STATUS_META[s] || {};
    bar.appendChild(h('div', { class: 'result-dist-row' },
      h('span', { class: 'result-dist-name' }, m.label || s),
      h('div', { class: 'result-dist-track' },
        h('div', { class: 'result-dist-fill', style: `width:${Math.round(dist[s] / total * 100)}%;background:${m.color}` })),
      h('span', { class: 'result-dist-n' }, String(dist[s]))));
  }
  body.appendChild(bar);

  // 门禁汇总
  if (gates.length) {
    body.appendChild(h('div', { class: 'result-sec-title' }, '质量门禁'));
    for (const g of gates) {
      const ok = g.status === 'passed';
      body.appendChild(h('div', { class: 'result-gate' },
        h('span', { class: `result-gate-dot ${g.status}` }),
        h('span', { class: 'result-gate-name' }, g.name),
        h('span', { class: 'result-gate-verdict' }, g.verdict || (ok ? '通过' : '待触发'))));
    }
  }

  return body;
}

/* ---------- 产物 tab ---------- */

function mountArtifactsTab() {
  const wrap = h('div', { class: 'result-art' });
  renderArtifactsInto(wrap);
  return wrap;
}

async function renderArtifactsInto(wrap) {
  const run = S.dash && S.dash.run;
  if (!run || !run.dir) {
    wrap.appendChild(h('div', { style: 'padding:16px' }, emptyState('未选择运行', '请先选择一次运行。')));
    return;
  }
  wrap.appendChild(loadingState('正在读取产物清单…'));
  let entries = [];
  let manifestError = null;
  try {
    const payload = await window.studio.listArtifacts(run.dir);
    ({ entries, manifestError } = normalizeArtifactList(payload));
  } catch (e) {
    wrap.innerHTML = '';
    wrap.appendChild(h('div', { style: 'padding:16px' }, emptyState('产物加载失败', String(e.message || e))));
    return;
  }
  wrap.innerHTML = '';
  if (manifestError) toast('warn', '产物清单降级', `MANIFEST 异常，已改用目录扫描：${manifestError}`);

  if (!entries || !entries.length) {
    wrap.appendChild(h('div', { style: 'padding:16px' }, emptyState('该运行暂无产物', '任务完成后产物会出现在这里。')));
    return;
  }

  const toolbar = h('div', { class: 'result-art-toolbar' },
    h('button', { class: 'btn btn-sm', onclick: async () => {
      try {
        const result = await window.studio.exportArtifacts(run.dir);
        const outcome = artifactExportOutcome(result);
        if (outcome.type === 'canceled') toast('info', '已取消', '未导出产物');
        else if (outcome.type === 'saved') toast('success', '已导出', `产物已打包为 ZIP（${fmtBytes(outcome.size)}）`);
        else throw new Error(outcome.message);
      }
      catch (e) { toast('error', '导出失败', String(e.message || e)); }
    } }, '导出 ZIP'));
  const list = h('div', { class: 'result-art-list' });
  const prev = h('div', { class: 'result-art-preview' });
  wrap.appendChild(toolbar);
  wrap.appendChild(list);
  wrap.appendChild(prev);

  for (const [dir, files] of groupArtifacts(entries)) {
    list.appendChild(h('div', { class: 'art-dir' }, dir));
    for (const f of files) {
      list.appendChild(h('button', { class: 'art-file', type: 'button', onclick: () => previewResult(prev, f) },
        h('span', { class: 'art-file-icon' }, ARTIFACT_ICONS[f.kind] || '📄'),
        h('span', { class: 'art-file-name', title: f.path }, (f.path || '').split('/').pop()),
        h('span', { class: 'art-file-size' }, fmtBytes(f.sizeBytes))));
    }
  }
  prev.appendChild(h('div', { style: 'padding:16px' }, emptyState('选择文件查看内容')));
}

async function previewResult(prev, f) {
  const seq = ++resultPreviewSeq;
  prev.innerHTML = '';
  prev.appendChild(h('div', { class: 'result-art-head' },
    h('span', { class: 'result-art-path' }, f.path || ''),
    h('span', { class: 'result-art-meta' }, `${fmtBytes(f.sizeBytes)} · ${f.role || '—'} · ${f.task || '—'}`)));
  const content = h('pre', { class: 'result-art-content' });
  content.textContent = '加载中…';
  prev.appendChild(content);
  try {
    const r = await window.studio.readArtifact(S.dash.run.dir, f.path);
    if (seq !== resultPreviewSeq || !content.isConnected) return;
    if (r && r.image && r.dataBase64) {
      const img = h('img', { alt: f.path || '图像产物' });
      img.src = `data:${r.mime};base64,${r.dataBase64}`;
      img.style.maxWidth = '100%';
      img.style.borderRadius = '6px';
      content.replaceWith(img);
      return;
    }
    const text = r && r.content != null ? String(r.content) : (r && typeof r === 'string' ? r : '');
    if (!text) { content.textContent = '（空文件或二进制）'; return; }
    const truncated = text.length > 100000 ? text.slice(0, 100000) + '\n…（已截断）' : text;
    content.innerHTML = highlightCode(truncated, f.path || '');
  } catch (e) {
    if (seq === resultPreviewSeq && content.isConnected) content.textContent = '读取失败：' + String(e.message || e);
  }
}

export { openResultPanel };
