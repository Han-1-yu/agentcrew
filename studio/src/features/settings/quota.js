/**
 * 配额弹窗：日 / 周 / 月 token 上限与当前用量。
 */
import { h } from '../../lib/dom.js';
import { S } from '../../lib/store.js';

import { readAlias, readNumber } from '../../lib/validate.js';

import { closeModal, showModal } from '../../ui/modal.js';
import { toast } from '../../ui/toast.js';
function openQuotaModal() {
  const quotas = (S.settings && S.settings.quotas) || {};
  const rows = [];

  const buildRow = (alias, q) => {
    q = q || {};
    const daily = q.daily || {};
    const weekly = q.weekly || {};
    const monthly = q.monthly || {};
    const aliasInput = h('input', { class: 'quota-alias-input', type: 'text', value: alias || '', placeholder: 'provider:model，如 openai:gpt-4o' });
    const dailyCalls = h('input', { class: 'quota-num', type: 'number', min: '0', value: daily.max_calls || '', placeholder: '次数' });
    const dailyTokens = h('input', { class: 'quota-num', type: 'number', min: '0', value: daily.max_tokens || '', placeholder: 'Token' });
    const dailyCost = h('input', { class: 'quota-num', type: 'number', min: '0', step: '0.01', value: daily.max_cost_usd || '', placeholder: '$' });
    const weeklyCalls = h('input', { class: 'quota-num', type: 'number', min: '0', value: weekly.max_calls || '', placeholder: '次数' });
    const weeklyTokens = h('input', { class: 'quota-num', type: 'number', min: '0', value: weekly.max_tokens || '', placeholder: 'Token' });
    const weeklyCost = h('input', { class: 'quota-num', type: 'number', min: '0', step: '0.01', value: weekly.max_cost_usd || '', placeholder: '$' });
    const monthlyCalls = h('input', { class: 'quota-num', type: 'number', min: '0', value: monthly.max_calls || '', placeholder: '次数' });
    const monthlyTokens = h('input', { class: 'quota-num', type: 'number', min: '0', value: monthly.max_tokens || '', placeholder: 'Token' });
    const monthlyCost = h('input', { class: 'quota-num', type: 'number', min: '0', step: '0.01', value: monthly.max_cost_usd || '', placeholder: '$' });
    const delBtn = h('button', { class: 'btn btn-sm btn-danger', type: 'button', onclick: () => { rowEl.remove(); } }, '删除');
    const rowEl = h('div', { class: 'quota-row' },
      h('div', { class: 'quota-row-head' },
        aliasInput, delBtn),
      h('div', { class: 'quota-grid' },
        h('div', { class: 'quota-col' },
          h('div', { class: 'quota-col-title' }, '日'),
          dailyCalls, dailyTokens, dailyCost),
        h('div', { class: 'quota-col' },
          h('div', { class: 'quota-col-title' }, '周'),
          weeklyCalls, weeklyTokens, weeklyCost),
        h('div', { class: 'quota-col' },
          h('div', { class: 'quota-col-title' }, '月'),
          monthlyCalls, monthlyTokens, monthlyCost)));
    rows.push({
      aliasInput,
      daily:   { calls: dailyCalls, tokens: dailyTokens, cost: dailyCost },
      weekly:  { calls: weeklyCalls, tokens: weeklyTokens, cost: weeklyCost },
      monthly: { calls: monthlyCalls, tokens: monthlyTokens, cost: monthlyCost },
    });
    return rowEl;
  };

  const listEl = h('div', { class: 'quota-list' });
  Object.entries(quotas).forEach(([alias, q]) => listEl.appendChild(buildRow(alias, q)));

  const addBtn = h('button', { class: 'btn btn-sm', type: 'button', onclick: () => {
    listEl.appendChild(buildRow('', {}));
  } }, '+ 添加别名配额');

  const saveBtn = h('button', { class: 'btn btn-primary', type: 'button', onclick: async () => {
    // 三个窗口共用同一份校验：此前 parseInt('-5') = -5、parseFloat('1e999')
    // = Infinity 会原样写进 quotas（负值配额等于永久耗尽），另有一份从未被
    // 调用的死 collect() 与三份重复读取。
    const collectWindow = (fields, label) => {
      const out = {};
      let error = '';
      const specs = [
        ['max_calls', fields.calls, { min: 0, max: 1e9, integer: true, fallback: 0, label: `${label} 调用次数` }],
        ['max_tokens', fields.tokens, { min: 0, max: 1e15, integer: true, fallback: 0, label: `${label} Token` }],
        ['max_cost_usd', fields.cost, { min: 0, max: 1e9, fallback: 0, label: `${label} 费用` }],
      ];
      for (const [key, el, opts] of specs) {
        const r = readNumber(el, opts);
        if (r.error) { error = error || r.error; el.classList.add('input-error'); }
        else {
          el.classList.remove('input-error');
          if (String(el.value || '').trim()) out[key] = r.value;
        }
      }
      return { out, error };
    };
    const next = {};
    let firstError = '';
    for (const r of rows) {
      const aliasInput = r.aliasInput;
      const alias = aliasInput.value.trim();
      const filled = [r.daily, r.weekly, r.monthly]
        .some(w => [w.calls, w.tokens, w.cost].some(el => String(el.value || '').trim()));
      if (!alias) {
        // 完全空白的行直接跳过；填了数值却没写别名的行要报错，不能静默丢弃。
        if (filled) {
          firstError = firstError || '请为每个配额行填写别名（provider:model）';
          aliasInput.classList.add('input-error');
        }
        continue;
      }
      const aliasCheck = readAlias(alias, { label: '配额别名' });
      if (aliasCheck.error) {
        firstError = firstError || aliasCheck.error;
        aliasInput.classList.add('input-error');
        continue;
      }
      aliasInput.classList.remove('input-error');
      const d = collectWindow(r.daily, '日');
      const w = collectWindow(r.weekly, '周');
      const m = collectWindow(r.monthly, '月');
      firstError = firstError || d.error || w.error || m.error;
      const quota = {};
      if (Object.keys(d.out).length) quota.daily = d.out;
      if (Object.keys(w.out).length) quota.weekly = w.out;
      if (Object.keys(m.out).length) quota.monthly = m.out;
      if (Object.keys(quota).length) next[alias] = quota;
    }
    if (firstError) { toast('error', '配额未保存', firstError); return; }
    if (!S.settings) S.settings = {};
    S.settings.quotas = next;
    S.settings.quota_enabled = Object.keys(next).length > 0;
    try {
      await window.studio.setSettings({ quotas: next, quota_enabled: S.settings.quota_enabled });
      toast('success', '配额已保存', `${Object.keys(next).length} 个别名`);
      closeModal();
    } catch (e) { toast('error', '保存失败', String(e.message || e)); }
  } }, '保存配额');

  showModal(h('div', { class: 'ap-modal quota-modal' },
    h('h3', { class: 'modal-title' }, '多窗口配额管理 (V7)'),
    h('div', { class: 'ap-hint' }, '按别名设置日/周/月配额。配额耗尽时自动触发故障转移到下一个可用别名。留空表示不限制。'),
    h('div', { class: 'quota-header' },
      h('span', { class: 'quota-h-alias' }, '别名'),
      h('span', { class: 'quota-h-col' }, '日 (calls / tokens / $)'),
      h('span', { class: 'quota-h-col' }, '周 (calls / tokens / $)'),
      h('span', { class: 'quota-h-col' }, '月 (calls / tokens / $)'),
      h('span', { class: 'quota-h-action' }, '')),
    listEl,
    addBtn,
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn', onclick: closeModal }, '取消'),
      saveBtn)));
}

/* ============================================================
   事件日志页（全量）
   ============================================================ */

export { openQuotaModal };
