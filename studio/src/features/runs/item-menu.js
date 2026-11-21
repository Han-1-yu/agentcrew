/**
 * B2：会话条目 ••• 菜单（弹窗形态，复用 ui/modal）。
 *
 * 重命名（本地别名）/ 置顶 / 归档 / 删除 / 对比 / 打开目录。
 * 元数据写盘统一走 meta-store（乐观更新 + 失败回滚）；删除后裁剪幽灵键。
 */
import { h } from '../../lib/dom.js';
import { runMetaKey } from '../../lib/run-meta.js';
import { S } from '../../lib/store.js';

import { closeModal, confirmAction, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

import { deleteRunItem } from './index.js';
import { patchRunMeta } from './meta-store.js';
import { openCompareModal } from './compare.js';
import { refreshSidebarRuns } from './sidebar.js';

function menuButton(label, { primary = false, danger = false, onclick } = {}) {
  return h('button', {
    class: `btn${primary ? ' btn-primary' : ''}${danger ? ' btn-danger' : ''}`,
    type: 'button',
    style: 'display:flex;width:100%;justify-content:flex-start',
    onclick,
  }, label);
}

async function toggleMeta(r, field, onLabel, offLabel) {
  const next = !r[field];
  const ok = await patchRunMeta(runMetaKey(r), { [field]: next });
  if (ok) {
    toast('success', next ? onLabel : offLabel, r.alias || r.goal || r.runId || '');
    refreshSidebarRuns();
  }
  closeModal();
}

function openRunItemMenu(r) {
  const runId = runMetaKey(r);
  if (!runId) { toast('error', '无法操作', '该条目缺少 run 标识'); return; }
  const aliasInput = h('input', {
    class: 'nr-input', type: 'text', value: r.alias || '',
    placeholder: r.goal ? String(r.goal).slice(0, 60) : '未命名',
    'aria-label': '会话别名',
  });
  showModal(h('div', {},
    h('h3', { class: 'modal-title' }, '会话操作'),
    h('div', { class: 'confirm-text', style: 'color:var(--text-ter)' },
      `${r.runId || ''} · ${r.status || 'unknown'}`),
    h('div', { class: 'nr-row', style: 'margin-top:10px' },
      h('label', { class: 'nr-label', for: 'run-item-alias' }, '别名（对话列表显示名，仅本机生效）'),
      aliasInput),
    h('div', { style: 'display:flex;flex-direction:column;gap:6px;margin-top:12px' },
      menuButton('保存别名', {
        primary: true,
        onclick: async () => {
          const ok = await patchRunMeta(runId, { alias: aliasInput.value.trim() });
          if (ok) {
            toast('success', '已保存别名', aliasInput.value.trim() || '已清除');
            refreshSidebarRuns();
            closeModal();
          }
        },
      }),
      menuButton(r.pinned ? '取消置顶' : '置顶', {
        onclick: () => void toggleMeta(r, 'pinned', '已置顶', '已取消置顶'),
      }),
      menuButton(r.archived ? '取消归档' : '归档（列表默认隐藏，可搜索）', {
        onclick: () => void toggleMeta(r, 'archived', '已归档', '已取消归档'),
      }),
      S.dash && S.dash.run && S.dash.run.dir !== r.dir
        ? menuButton('与当前运行对比', {
          onclick: () => { closeModal(); openCompareModal(r); },
        }) : null,
      r.dir ? menuButton('打开运行目录', {
        onclick: () => {
          closeModal();
          window.studio.openPath(r.dir).catch((e) => toast('error', '打开失败', String((e && e.message) || e)));
        },
      }) : null,
      r.status !== 'running' ? menuButton('删除运行（不可恢复）', {
        danger: true,
        onclick: () => {
          closeModal();
          confirmAction('删除运行',
            `确认删除「${r.alias || r.goal || r.runId}」？此操作不可恢复。`,
            () => void deleteRunItem(r), '删除');
        },
      }) : h('div', { class: 'confirm-text', style: 'color:var(--warn)' }, '运行中的会话不可删除')),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', type: 'button', onclick: closeModal }, '关闭'))));
}

export { openRunItemMenu };
