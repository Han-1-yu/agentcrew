/**
 * 设置页「数据管理」组（O3）：输出目录占用统计、归档 run 一键清理（走回收站）、
 * 设置的导出 / 导入。统计与清理都按按钮触发，进页面不扫描磁盘。
 */
import { h } from '../../lib/dom.js';
import { biggestRuns, cleanupCandidates, usageStats } from '../../lib/data-usage.js';
import { fmtBytes } from '../../lib/format.js';
import { S } from '../../lib/store.js';

import { confirmAction } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

function buildDataGroup() {
  const summary = h('div', { class: 'set-hint data-summary' }, '还没有统计。点「刷新统计」扫描输出目录（大目录可能要几秒）。');
  const table = h('div', { class: 'data-runs' });
  const refreshBtn = h('button', { class: 'btn', type: 'button' }, '刷新统计');
  const cleanBtn = h('button', { class: 'btn', type: 'button', disabled: true }, '清理归档 run');
  let lastUsage = null;

  const render = (usage) => {
    lastUsage = usage;
    const s = usageStats(usage);
    summary.textContent = usage && usage.error
      ? usage.error
      : `${usage.root || '输出目录'} · ${s.count} 个 run · 共 ${fmtBytes(s.totalBytes)} · 已归档 ${s.archivedCount} 个`;
    table.replaceChildren();
    if (s.count) {
      table.appendChild(h('div', { class: 'data-run-row data-run-head' },
        h('span', {}, '最占空间的 run'), h('span', {}, '体积')));
      for (const r of biggestRuns(usage, 6)) {
        table.appendChild(h('div', { class: 'data-run-row' },
          h('span', { class: 'mono' }, r.name),
          h('span', { class: 'data-run-size' }, fmtBytes(r.sizeBytes))));
      }
    }
    cleanBtn.disabled = s.candidateCount === 0;
    cleanBtn.textContent = s.candidateCount
      ? `清理归档 run（${s.candidateCount} 个 · ${fmtBytes(s.candidateBytes)}）`
      : '没有可清理的归档 run';
  };

  const refresh = async () => {
    summary.textContent = '正在扫描输出目录…';
    try { render(await window.studio.outputUsage()); }
    catch (e) { summary.textContent = `统计失败：${String((e && e.message) || e)}`; }
  };

  refreshBtn.addEventListener('click', () => { void refresh(); });

  cleanBtn.addEventListener('click', () => {
    const candidates = cleanupCandidates(lastUsage);
    if (!candidates.length) return;
    confirmAction('清理归档 run',
      `将把 ${candidates.length} 个已归档 run（${fmtBytes(usageStats(lastUsage).candidateBytes)}）移到系统回收站，可随时还原。`,
      async () => {
        try {
          const r = await window.studio.trashArchivedRuns(candidates.map((c) => c.dir));
          const ok = (r && r.trashed) || [];
          const bad = (r && r.errors) || [];
          if (bad.length) toast('warn', `已清理 ${ok.length} 个，${bad.length} 个被拒`, bad.map((x) => `${x.dir}：${x.error}`).join('；'));
          else toast('success', '已移到回收站', `${ok.length} 个 run`);
          void refresh();
        } catch (e) { toast('error', '清理失败', String((e && e.message) || e)); }
      }, '移到回收站');
  });

  const exportBtn = h('button', { class: 'btn', type: 'button', title: '把当前设置写成 JSON（不含密钥）' }, '导出设置');
  const importBtn = h('button', { class: 'btn', type: 'button', title: '从 JSON 恢复设置（只接受已知字段）' }, '导入设置');
  exportBtn.addEventListener('click', async () => {
    try {
      const r = await window.studio.exportSettings();
      if (r && r.canceled) toast('info', '已取消', '未导出设置');
      else toast('success', '已导出', String((r && r.path) || ''));
    } catch (e) { toast('error', '导出失败', String((e && e.message) || e)); }
  });
  importBtn.addEventListener('click', async () => {
    try {
      const r = await window.studio.importSettings();
      if (r && r.canceled) { toast('info', '已取消', '未导入设置'); return; }
      if (r && r.settings) S.settings = r.settings;
      toast('success', '已导入', `应用了 ${String((r && r.applied) || 0)} 项设置`);
    } catch (e) { toast('error', '导入失败', String((e && e.message) || e)); }
  });

  return h('section', { class: 'settings-group' },
    h('h3', { class: 'settings-group-title' }, '数据管理'),
    h('div', { class: 'settings-field' },
      h('label', {}, '输出目录占用'),
      summary,
      h('div', { class: 'set-row' }, refreshBtn, cleanBtn),
      table),
    h('div', { class: 'settings-field' },
      h('label', {}, '设置备份'),
      h('div', { class: 'set-row' }, exportBtn, importBtn),
      h('div', { class: 'set-hint' }, '导出的 JSON 只含设置项（主题 / 快捷键 / 通知偏好 / 供应商覆盖等），不含任何密钥。')));
}

export { buildDataGroup };
