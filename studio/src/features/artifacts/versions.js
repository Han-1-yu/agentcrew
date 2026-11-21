/**
 * 产物版本条与行级 diff 的渲染（J1）：只碰 DOM，计算都在 lib/artifact-versions.js。
 */
import { h } from '../../lib/dom.js';
import { diffSummary, lineDiff } from '../../lib/artifact-versions.js';
import { fmtBytes } from '../../lib/format.js';

/** `2026-11-21T13:57:56Z` → `11-21 21:57`（本地时区，取不到就原样返回）。 */
function timeText(iso) {
  const raw = String(iso || '');
  if (!raw) return '';
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * 版本选择器：`当前版本` + 每次 rewind 快照里同路径的那份。
 * `state.version` 存选中的快照名（空串=当前），`onChange` 负责重绘。
 */
function versionBar(versions, state, onChange) {
  if (!versions.length) return null;
  const sel = h('select', {
    class: 'art-version-select',
    title: '与 rewind 快照对比（选快照后显示行级 diff）',
    onchange: (e) => { state.version = e.target.value; onChange(); },
  },
    h('option', { value: '' }, '当前版本'),
    ...versions.map((v) => h('option', { value: v.name },
      `${v.name}${v.createdAt ? ' · ' + timeText(v.createdAt) : ''}${v.sizeBytes ? ' · ' + fmtBytes(v.sizeBytes) : ''}`)));
  return h('span', { class: 'art-version' }, '版本 ', sel);
}

/** 把「旧 → 新」的行级 diff 画进 `box`（统一视图：+ 绿 / − 红 / 未变灰）。 */
function renderDiffInto(box, { before, after, beforeLabel, afterLabel }) {
  const diff = lineDiff(before, after);
  box.appendChild(h('div', { class: 'art-diff-head' },
    h('span', { class: 'art-diff-sum' }, diffSummary(diff)),
    h('span', { class: 'art-diff-path' }, `${beforeLabel} → ${afterLabel}`)));
  if (!diff.added && !diff.removed) {
    box.appendChild(h('div', { class: 'dp-text dim' }, '没有内容差异，只是同一份文件。'));
    return diff;
  }
  const rows = h('div', { class: 'art-diff-rows' });
  for (const r of diff.rows) {
    rows.appendChild(h('div', { class: `art-diff-row is-${r.kind}` },
      h('span', { class: 'art-diff-no' }, r.aNo == null ? '' : String(r.aNo)),
      h('span', { class: 'art-diff-no' }, r.bNo == null ? '' : String(r.bNo)),
      h('span', { class: 'art-diff-mark' }, r.kind === 'add' ? '+' : r.kind === 'del' ? '−' : ' '),
      h('span', { class: 'art-diff-text' }, r.text || ' ')));
  }
  box.appendChild(rows);
  return diff;
}

export { renderDiffInto, timeText, versionBar };
