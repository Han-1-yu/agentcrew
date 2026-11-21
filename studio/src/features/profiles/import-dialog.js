/** YAML 导入：支持单档案、key → 字段映射，或整段 agents.profiles。 */
import { h } from '../../lib/dom.js';
import { saveAgents } from '../../lib/agent-binding.js';
import { closeModal, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';

import { icon } from './icons.js';

function openImportDialog({ onImported }) {
  const textareaEl = h('textarea', {
    class: 'pf-input pf-import-text', rows: 12,
    placeholder: 'agents:\n  profiles:\n    fast-writer:\n      title: 快速写手\n      model: deepseek:deepseek-chat',
  });
  const overwrite = h('input', { type: 'checkbox' });
  const status = h('div', { class: 'pf-import-status' });
  const fileInput = h('input', {
    type: 'file', accept: '.yaml,.yml,.txt', class: 'hidden',
    onchange: async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      textareaEl.value = await file.text();
      status.textContent = `已载入 ${file.name}`;
      status.className = 'pf-import-status ok';
    },
  });

  const renderResult = (data, dryRun) => {
    status.className = 'pf-import-status ok';
    status.textContent = `${dryRun ? '校验通过' : '已导入'}：新建 ${(data.created || []).length}、`
      + `覆盖 ${(data.updated || []).length}`
      + ((data.warnings || []).length ? `；${data.warnings.join('；')}` : '');
  };

  const validateBtn = h('button', { class: 'btn', type: 'button' }, '先校验');
  validateBtn.addEventListener('click', async () => {
    status.className = 'pf-import-status';
    status.textContent = '校验中…';
    try {
      const data = await saveAgents(textareaEl.value, {
        dryRun: true, force: overwrite.checked,
      });
      renderResult(data, true);
    } catch (e) {
      status.className = 'pf-import-status bad';
      status.textContent = String(e.message || e);
    }
  });

  const importBtn = h('button', { class: 'btn btn-primary', type: 'button' }, '导入');
  importBtn.addEventListener('click', async () => {
    if (!textareaEl.value.trim()) {
      status.className = 'pf-import-status bad';
      status.textContent = '先粘贴 YAML，或从文件载入';
      return;
    }
    importBtn.disabled = true;
    status.className = 'pf-import-status';
    status.textContent = '导入中…';
    try {
      const data = await saveAgents(textareaEl.value, { force: overwrite.checked });
      toast('success', '已导入', `新建 ${(data.created || []).length}、覆盖 ${(data.updated || []).length}`);
      closeModal();
      if (onImported) await onImported(data);
    } catch (e) {
      status.className = 'pf-import-status bad';
      status.textContent = String(e.message || e);
    } finally {
      importBtn.disabled = false;
    }
  });

  showModal(h('div', { class: 'pf-import' },
    h('h3', { class: 'modal-title' }, '导入执行体档案'),
    h('p', { class: 'pf-editor-sub' },
      '支持单份档案、key → 字段映射，或整段 agents.profiles；导入前会按执行期同一套规则校验。'),
    textareaEl,
    h('div', { class: 'pf-import-row' },
      h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: () => fileInput.click(),
      }, icon('upload', 14), '从文件载入'),
      fileInput,
      h('label', { class: 'pf-radio' }, overwrite, h('span', {}, '覆盖同名档案'))),
    status,
    h('div', { class: 'modal-actions pf-form-actions' },
      h('button', { class: 'btn', type: 'button', onclick: closeModal }, '取消'),
      validateBtn, importBtn)), { wide: true });
}

export { openImportDialog };
