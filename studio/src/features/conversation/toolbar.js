/**
 * 对话工具栏：运行摘要 + 搜索 + 类型筛选 + 复制整段对话。
 *
 * 只挂一次（mountConversationShell 时），每次渲染只更新计数与选中态：
 * 重建 input 会丢焦点和光标，让「边打字边筛」变成不可能。
 */
import { RUN_META } from '../../contracts/enums.js';
import { copyText } from '../../lib/clipboard.js';
import { CONV_FILTERS, buildTranscript, filterCounts, filterMessages, transcriptFilename } from '../../lib/conversation.js';
import { h, s } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { toast } from '../../ui/toast.js';

function iconSearch() {
  return s('svg', { width: '13', height: '13', viewBox: '0 0 16 16', fill: 'none', 'aria-hidden': 'true' },
    s('circle', { cx: '7', cy: '7', r: '4.4', stroke: 'currentColor', 'stroke-width': '1.3' }),
    s('path', { d: 'm10.4 10.4 3.2 3.2', stroke: 'currentColor', 'stroke-width': '1.3', 'stroke-linecap': 'round' }));
}

function createToolbar(opts) {
  const onChange = opts && opts.onChange;
  let latest = { run: null, msgs: [], taskIndex: {} };

  const statusDot = h('span', { class: 'dot' });
  const statusLabel = h('span', { class: 'conv-tb-status' }, '—');
  const meta = h('span', { class: 'conv-tb-meta' }, '');
  const hitline = h('span', { class: 'conv-hitline hidden' });
  const searchInput = h('input', {
    id: 'conv-search', class: 'conv-search-input', type: 'search', spellcheck: 'false',
    placeholder: '搜索对话（按 / 聚焦）', 'aria-label': '搜索对话',
    value: S.convQuery || '', // 切页回来时保留上次搜索词，避免「列表被过滤但输入框是空的」
  });
  const clearBtn = h('button', {
    class: 'conv-search-clear' + (S.convQuery ? '' : ' hidden'),
    type: 'button', title: '清除搜索', 'aria-label': '清除搜索',
  }, '✕');
  const copyBtn = h('button', {
    id: 'conv-copy', class: 'btn btn-sm conv-copy-btn', type: 'button', title: '复制为 Markdown（整段对话）',
  }, '复制对话');
  // A6：导出为 .md 文件（主进程保存对话框）
  const exportBtn = h('button', {
    id: 'conv-export', class: 'btn btn-sm', type: 'button', title: '导出为 .md 文件',
  }, '导出');
  exportBtn.addEventListener('click', async () => {
    const md = buildTranscript(latest.msgs, latest.taskIndex, latest.run);
    try {
      const r = await window.studio.saveConversationMd(md, transcriptFilename(latest.run));
      if (r && r.canceled) return;
      if (r && r.error) throw new Error(r.error);
      toast('success', '已导出', String((r && r.saved) || ''));
    } catch (e) {
      toast('error', '导出失败', String((e && e.message) || e));
    }
  });

  const chipEls = new Map();
  const filters = h('div', { class: 'conv-filters', role: 'group', 'aria-label': '消息类型筛选' });
  for (const f of CONV_FILTERS) {
    const count = h('span', { class: 'conv-fchip-n' }, '');
    const btn = h('button', {
      class: 'conv-fchip', type: 'button', 'data-filter': f.key, 'aria-pressed': 'false',
      onclick: () => { setFilter(f.key); },
    }, h('span', {}, f.label), count);
    chipEls.set(f.key, { btn, count });
    filters.appendChild(btn);
  }

  let timer = null;
  searchInput.addEventListener('input', () => {
    const v = searchInput.value;
    clearBtn.classList.toggle('hidden', !v);
    clearTimeout(timer);
    timer = setTimeout(() => { S.convQuery = v; if (onChange) onChange(); }, 180);
  });
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      if (searchInput.value) { searchInput.value = ''; clearBtn.classList.add('hidden'); S.convQuery = ''; if (onChange) onChange(); }
      else searchInput.blur();
    }
  });
  clearBtn.addEventListener('click', () => {
    searchInput.value = '';
    clearBtn.classList.add('hidden');
    S.convQuery = '';
    searchInput.focus();
    if (onChange) onChange();
  });
  copyBtn.addEventListener('click', async () => {
    const md = buildTranscript(latest.msgs, latest.taskIndex, latest.run);
    const ok = await copyText(md);
    toast(ok ? 'success' : 'error', ok ? '已复制对话' : '复制失败',
      ok ? `Markdown · ${latest.msgs.length} 条消息` : '剪贴板不可用');
  });

  const node = h('div', { class: 'conv-toolbar' },
    h('div', { class: 'conv-tb-row' },
      h('div', { class: 'conv-tb-summary' }, statusDot, statusLabel, h('span', { class: 'conv-tb-sep' }, '·'), meta),
      h('div', { class: 'conv-tb-tools' },
        h('div', { class: 'conv-search' }, iconSearch(), searchInput, clearBtn),
        exportBtn, copyBtn)),
    h('div', { class: 'conv-tb-row conv-tb-row-sub' }, filters, hitline));

  function setFilter(key) {
    S.convFilter = key;
    if (onChange) onChange();
  }

  function update(next) {
    latest = next || latest;
    const { run, msgs, taskIndex } = latest;
    const filter = S.convFilter || 'all';
    const query = String(S.convQuery || '').trim();

    const rm = RUN_META[(run && run.status) || 'idle'] || RUN_META.idle;
    statusDot.className = `dot ${rm.dot}`;
    statusLabel.textContent = rm.label;

    const stats = (run && run.stats) || {};
    const total = stats.total_tasks != null ? stats.total_tasks : (latest.tasks || []).length;
    const ok = stats.succeeded != null ? stats.succeeded : 0;
    let artifacts = 0;
    for (const m of msgs) for (const it of m.items || []) if (it.kind === 'artifact') artifacts += 1;
    const bits = [];
    if (total) bits.push(`${ok}/${total} 任务完成`);
    bits.push(`${msgs.length} 条消息`);
    if (artifacts) bits.push(`${artifacts} 个产物`);
    meta.textContent = bits.join(' · ');

    // 计数跟随搜索词（类型之间仍可重叠），避免出现「门禁 9」但命中 0 条的自相矛盾
    const counts = filterCounts(filterMessages(msgs, { query }));
    for (const [key, refs] of chipEls) {
      refs.btn.classList.toggle('active', key === filter);
      refs.btn.setAttribute('aria-pressed', key === filter ? 'true' : 'false');
      refs.count.textContent = String(counts[key] || 0);
    }

    const visible = latest.visibleCount == null ? msgs.length : latest.visibleCount;
    const filtering = !!query || filter !== 'all';
    hitline.classList.toggle('hidden', !filtering);
    hitline.textContent = filtering ? `命中 ${visible} / 共 ${msgs.length} 条` : '';
    if (document.activeElement !== searchInput && searchInput.value !== S.convQuery) {
      searchInput.value = S.convQuery || '';
    }
    clearBtn.classList.toggle('hidden', !searchInput.value);
    if (taskIndex) latest.taskIndex = taskIndex;
  }

  return { node, update };
}

export { createToolbar };
